"""Stage 2/3 groundwork - Knowledge Graph.

Builds a lightweight, dependency-free property graph on top of the Stage 1
unified entities:

    (Asset) -[:HAS_ALARM]-> (AlarmEvent)
    (Asset) -[:HAS_ALARM_CONFIG]-> (AlarmConfig)
    (Asset) -[:HAS_WORK_ORDER]-> (WorkOrder)
    (Asset) -[:HAS_SETPOINT_CHANGE]-> (OperatorAction)
    (Asset) -[:HAS_HEALTH_EVENT]-> (HealthEvent)
    (Asset) -[:HAS_COST_POSTING]-> (CostPosting)
    (Asset) -[:HAS_HISTORIAN_TAG]-> (HistorianTag)
    (CostPosting) -[:REFERENCES_WORK_ORDER]-> (WorkOrder)

Design notes:
  - `historian_timeseries.csv` is ~518k rows -- we never materialize it as
    graph nodes/edges. Instead each physical tag becomes one lightweight
    `HistorianTag` node, and `historian_series()` streams the raw CSV
    filtered by tag (+ optional time window) on demand. This is what a
    Stage 4 reasoning agent should call to pull just the trend it needs.
  - Every transactional record is attached to a unified Asset via the
    (system, local_id) -> unified_id lookup produced in Stage 1 -- there is
    no re-guessing of identity here, entity resolution has already been done.
  - Export helpers (`to_node_link_json`, `to_cypher`) let you load this into
    a real graph store (Neo4j, etc.) or a visualization tool later; the
    stack is otherwise unopinionated per the team handbook.
"""
from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Iterator, Optional

from . import config

if TYPE_CHECKING:
    from .llm_provider import TextGenerationProvider

# Physical process flow line (SCENARIO.md Sec 5b): Crude Feed -> P-101 ->
# E-101 -> H-101 -> Column C-101 -> (naphtha ->) reflux drum V-201 -> P-102
# back to the column; utility valve CV-400 cools K-101's lube oil and
# supplies H-101's BFW/steam. Anchors are (system, local_id) pairs already
# resolved in Stage 1 -- used only to look up each asset's unified_id.
#
# This is now only a FALLBACK, used when no LLM is configured (see
# `build_graph`'s `prose_text`/`text_provider` params) -- the primary path
# is `topology_extraction.extract_topology_with_llm`, which derives these
# same relationships straight from prose instead of a human transcribing
# them. Kept here so the pipeline still produces a sensible graph with
# zero installs/credentials, matching the rest of the codebase's "never
# let a missing AI provider break the deterministic path" philosophy.
_PROCESS_TOPOLOGY: list[tuple[tuple[str, str], tuple[str, str], str, str]] = [
    (("AM", "PMP-100-101"), ("APM", "Exchanger_014"), "FEEDS", "P-101 pumps crude feed into preheat exchanger E-101"),
    (("APM", "Exchanger_014"), ("DCS", "U100_H101"), "FEEDS", "E-101 preheated crude flows to fired heater H-101"),
    (("DCS", "U100_H101"), ("DCS", "U100_C101"), "FEEDS", "Heated crude enters distillation column C-101"),
    (("DCS", "U100_C101"), ("DCS", "U100_V201"), "FEEDS", "Naphtha overhead (via condenser E-301, not in this dataset) collects in reflux drum V-201"),
    (("DCS", "U100_V201"), ("APM", "Pump_002"), "FEEDS", "Reflux drum V-201 feeds Reflux Pump P-102"),
    (("APM", "Pump_002"), ("DCS", "U100_C101"), "FEEDS", "P-102 returns reflux back to column C-101"),
    (("CMMS", "EQ-8001"), ("CMMS", "EQ-6001"), "COOLS", "CV-400 supplies cooling water to K-101's lube-oil cooler"),
    (("CMMS", "EQ-8001"), ("DCS", "U100_H101"), "SUPPLIES_UTILITY", "CV-400 supplies BFW/steam & cooling water utilities to H-101"),
]


@dataclass
class Node:
    id: str
    label: str
    properties: dict[str, Any] = field(default_factory=dict)


@dataclass
class Edge:
    source: str
    target: str
    rel_type: str
    properties: dict[str, Any] = field(default_factory=dict)


class KnowledgeGraph:
    def __init__(self) -> None:
        self.nodes: dict[str, Node] = {}
        self.edges: list[Edge] = []

    def add_node(self, node_id: str, label: str, **properties: Any) -> None:
        self.nodes[node_id] = Node(id=node_id, label=label, properties=properties)

    def add_edge(self, source: str, target: str, rel_type: str, **properties: Any) -> None:
        if source not in self.nodes or target not in self.nodes:
            return  # skip dangling edges (e.g. unresolved asset reference)
        self.edges.append(Edge(source=source, target=target, rel_type=rel_type, properties=properties))

    def neighbors(self, node_id: str, rel_type: Optional[str] = None) -> list[tuple[Node, Edge]]:
        result = []
        for e in self.edges:
            if e.source == node_id and (rel_type is None or e.rel_type == rel_type):
                result.append((self.nodes[e.target], e))
            elif e.target == node_id and (rel_type is None or e.rel_type == rel_type):
                result.append((self.nodes[e.source], e))
        return result

    def context_for_asset(self, unified_id: str) -> dict[str, list[dict[str, Any]]]:
        """Everything directly connected to one unified asset, grouped by
        record type -- the cross-app 'show me everything about P-101' view."""
        grouped: dict[str, list[dict[str, Any]]] = {}
        for node, edge in self.neighbors(unified_id):
            grouped.setdefault(node.label, []).append({"id": node.id, **node.properties})
        for records in grouped.values():
            records.sort(key=lambda r: r.get("timestamp") or r.get("created") or "")
        return grouped

    def to_node_link_json(self) -> dict[str, Any]:
        return {
            "nodes": [{"id": n.id, "label": n.label, **n.properties} for n in self.nodes.values()],
            "edges": [
                {"source": e.source, "target": e.target, "type": e.rel_type, **e.properties}
                for e in self.edges
            ],
        }

    @classmethod
    def from_node_link_json(cls, data: dict[str, Any]) -> "KnowledgeGraph":
        """Rehydrate a KnowledgeGraph from `to_node_link_json()`'s output --
        the counterpart loader that lets `output/graph.json` act as a real
        on-disk cache (skip re-reading every transactional CSV + rebuilding
        the graph from scratch) instead of being a write-only viz export.
        Raises KeyError/TypeError on malformed input -- callers should
        catch and fall back to a full rebuild rather than trust a
        corrupt/partial cache file.
        """
        kg = cls()
        for raw in data["nodes"]:
            raw = dict(raw)
            node_id = raw.pop("id")
            label = raw.pop("label")
            kg.add_node(node_id, label, **raw)
        for raw in data["edges"]:
            raw = dict(raw)
            source = raw.pop("source")
            target = raw.pop("target")
            rel_type = raw.pop("type")
            kg.add_edge(source, target, rel_type, **raw)
        return kg

    def to_cypher(self) -> str:
        """Generate CREATE statements for a quick Neo4j import."""
        lines = []
        for n in self.nodes.values():
            props = ", ".join(f"{k}: {json.dumps(v)}" for k, v in n.properties.items() if v is not None)
            lines.append(f"CREATE (:{n.label} {{id: {json.dumps(n.id)}{', ' + props if props else ''}}});")
        for e in self.edges:
            lines.append(
                f"MATCH (a {{id: {json.dumps(e.source)}}}), (b {{id: {json.dumps(e.target)}}}) "
                f"CREATE (a)-[:{e.rel_type}]->(b);"
            )
        return "\n".join(lines)


def _build_reverse_lookup(entities: list[dict[str, Any]]) -> dict[tuple[str, str], str]:
    """(system, local_id) -> unified_id, from Stage 1 output."""
    lookup: dict[tuple[str, str], str] = {}
    for entity in entities:
        for member in entity["members"]:
            lookup[(member["system"], member["local_id"])] = entity["unified_id"]
    return lookup


def _read_csv(path) -> Iterator[dict[str, str]]:
    with open(path, newline="", encoding="utf-8") as f:
        yield from csv.DictReader(f)


def build_graph(
    entities: list[dict[str, Any]],
    prose_text: Optional[str] = None,
    text_provider: Optional["TextGenerationProvider"] = None,
) -> KnowledgeGraph:
    kg = KnowledgeGraph()
    lookup = _build_reverse_lookup(entities)

    for entity in entities:
        kg.add_node(
            entity["unified_id"],
            "Asset",
            canonical_name=entity["canonical_name"],
            confidence=entity["confidence"],
            system_ids={m["system"]: m["local_id"] for m in entity["members"]},
        )

    # --- Alarm Management ---
    for row in _read_csv(config.AM_DIR / "am_alarm_events.csv"):
        asset_id = lookup.get(("AM", row["equipment_ref"]))
        kg.add_node(
            row["event_id"],
            "AlarmEvent",
            timestamp=row["timestamp"],
            alarm_point=row["alarm_point"],
            alarm_type=row["alarm_type"],
            priority=row["priority"],
            state=row["state"],
            value=row["value"],
            operator=row["operator"],
        )
        if asset_id:
            kg.add_edge(asset_id, row["event_id"], "HAS_ALARM")

    # --- Alarm Management: alarm-point CONFIGURATION (limits/deadband) ---
    # Config-only metadata, one node per configured alarm point -- mirrors
    # HistorianTag's "tag metadata, not raw values" pattern. Without this,
    # the agent could only infer a mis-set-limit/chattering-alarm conclusion
    # *behaviorally* from am_alarm_events.csv's timing/value pattern (which
    # works, see docs/CHAT_AGENT.md Sec 6's Q5 live test) but could never cite
    # the actual configured HH/H/L/LL limit or deadband/hysteresis value as
    # hard evidence the way it cites work order IDs or setpoint values
    # elsewhere. `deadband` matters most: TK-201's S5 scenario has a real
    # deadband of 0.3-0.4 in the level reading but only 0.1 configured --
    # too tight relative to normal process noise, causing the point to
    # chatter across the H threshold repeatedly instead of alarming once.
    am_config = json.loads((config.AM_DIR / "am_config.json").read_text())
    for alarm in am_config.get("alarms", []):
        asset_id = lookup.get(("AM", alarm["equipment_ref"]))
        alarm_point = alarm["alarm_point"]
        limits = alarm.get("limits") or {}
        rationalization = alarm.get("rationalization") or {}
        kg.add_node(
            alarm_point,
            "AlarmConfig",
            measurement=alarm.get("measurement"),
            eng_unit=alarm.get("eng_unit"),
            limit_hh=limits.get("HH"),
            limit_h=limits.get("H"),
            limit_l=limits.get("L"),
            limit_ll=limits.get("LL"),
            deadband=alarm.get("deadband"),
            priority=alarm.get("priority"),
            cause=rationalization.get("cause"),
            consequence=rationalization.get("consequence"),
            recommended_action=rationalization.get("action"),
        )
        if asset_id:
            kg.add_edge(asset_id, alarm_point, "HAS_ALARM_CONFIG")

    # --- CMMS work orders ---
    for row in _read_csv(config.CMMS_DIR / "cmms_workorders.csv"):
        asset_id = lookup.get(("CMMS", row["asset_code"]))
        kg.add_node(
            row["wo_id"],
            "WorkOrder",
            wo_type=row["wo_type"],
            status=row["status"],
            created=row["created"],
            completed=row["completed"] or None,
            technician=row["technician"],
            parts_used=row["parts_used"] or None,
            notes=row["notes"],
        )
        if asset_id:
            kg.add_edge(asset_id, row["wo_id"], "HAS_WORK_ORDER")

    # --- DCS operator actions (setpoint changes etc.) ---
    for row in _read_csv(config.DCS_DIR / "dcs_operator_actions.csv"):
        asset_id = lookup.get(("DCS", row["equipment_ref"]))
        kg.add_node(
            row["action_id"],
            "OperatorAction",
            timestamp=row["timestamp"],
            loop_id=row["loop_id"],
            action_type=row["action_type"],
            old_value=row["old_value"],
            new_value=row["new_value"],
            operator=row["operator"],
            shift=row["shift"],
        )
        if asset_id:
            kg.add_edge(asset_id, row["action_id"], "HAS_SETPOINT_CHANGE")

    # --- APM predicted-failure / anomaly events ---
    for row in _read_csv(config.APM_DIR / "apm_events.csv"):
        asset_id = lookup.get(("APM", row["apm_id"]))
        kg.add_node(
            row["event_id"],
            "HealthEvent",
            timestamp=row["timestamp"],
            event_type=row["event_type"],
            symptom=row["symptom"],
            confidence=row["confidence"],
            recommendation=row["recommendation"],
        )
        if asset_id:
            kg.add_edge(asset_id, row["event_id"], "HAS_HEALTH_EVENT")

    # --- ERP cost postings (also the hard FK back to CMMS work orders) ---
    for row in _read_csv(config.ERP_DIR / "erp_cost_postings.csv"):
        asset_id = lookup.get(("ERP", row["erp_asset_id"]))
        kg.add_node(
            row["posting_id"],
            "CostPosting",
            posting_date=row["posting_date"],
            amount_usd=row["amount_usd"],
            type=row["type"],
            linked_wo=row["linked_wo"],
        )
        if asset_id:
            kg.add_edge(asset_id, row["posting_id"], "HAS_COST_POSTING")
        if row["linked_wo"] in kg.nodes:
            kg.add_edge(row["posting_id"], row["linked_wo"], "REFERENCES_WORK_ORDER")

    # --- Historian: tag-level metadata only, never the raw 518k rows ---
    hist_config = json.loads((config.HIST_DIR / "historian_config.json").read_text())
    for tag in hist_config.get("tags", []):
        parts = tag["tag"].split(".")
        if len(parts) < 4:
            continue
        equipment_key = ".".join(parts[:3])
        asset_id = lookup.get(("Historian", equipment_key))
        kg.add_node(
            tag["tag"],
            "HistorianTag",
            description=tag.get("description"),
            eng_unit=tag.get("eng_unit"),
            min=tag.get("min"),
            max=tag.get("max"),
        )
        if asset_id:
            kg.add_edge(asset_id, tag["tag"], "HAS_HISTORIAN_TAG")

    # --- Process topology: what physically feeds/cools/supplies what.
    # This is domain knowledge that no system's structured records encode
    # anywhere -- it only exists as unstructured prose (a scenario doc, a
    # P&ID narrative, a maintenance-log excerpt, ...). It lets Stage 4
    # reasoning walk "upstream/downstream" relationships (e.g. E-101
    # fouling -> H-101 fuel use -> column feed temperature; CV-400 ->
    # K-101 lube-oil cooling).
    #
    # Primary path: ask the configured LLM to read `prose_text` and
    # propose these relationships directly (see
    # `topology_extraction.extract_topology_with_llm`) -- no human
    # transcribes anything, and a *different* deployment's prose (a new
    # plant, a different P&ID) works with zero code changes. Every
    # proposal is re-validated against real resolved entities before
    # becoming an edge (see that module's docstring for the safety design).
    # Fallback (no LLM configured, or no prose supplied): the hardcoded
    # `_PROCESS_TOPOLOGY` list transcribed once from SCENARIO.md Sec 5b --
    # keeps the pipeline fully functional with zero installs/credentials,
    # matching this codebase's "never let a missing AI provider break the
    # deterministic path" philosophy everywhere else (open-vocab
    # classification, name-similarity scoring, LLM answer polishing).
    used_llm_topology = False
    if prose_text and text_provider is not None:
        from .llm_provider import NullTextGenerationProvider

        if not isinstance(text_provider, NullTextGenerationProvider):
            from .topology_extraction import extract_topology_with_llm

            result = extract_topology_with_llm(prose_text, entities, text_provider)
            for edge in result.edges:
                kg.add_edge(
                    edge.from_unified_id,
                    edge.to_unified_id,
                    edge.rel_type,
                    note=edge.note,
                    extracted_by=edge.extracted_by,
                )
            if result.dropped:
                print(
                    f"topology_extraction: {len(result.edges)} edges accepted, "
                    f"{len(result.dropped)} proposals dropped (unresolved/invalid) -- "
                    "see TopologyExtractionResult.dropped for details"
                )
            used_llm_topology = True

    if not used_llm_topology:
        _add_hardcoded_topology(kg, lookup)

    return kg


def _add_hardcoded_topology(kg: KnowledgeGraph, lookup: dict[tuple[str, str], str]) -> None:
    """Fallback topology source -- see `_PROCESS_TOPOLOGY`'s docstring."""
    for anchor_a, anchor_b, rel_type, note in _PROCESS_TOPOLOGY:
        a_id = lookup.get(anchor_a)
        b_id = lookup.get(anchor_b)
        if a_id and b_id:
            kg.add_edge(a_id, b_id, rel_type, note=note, doc_source="SCENARIO.md Sec 5b")


def historian_series(
    tag: str, start: Optional[str] = None, end: Optional[str] = None
) -> list[dict[str, Any]]:
    """Stream `historian_timeseries.csv` filtered by tag (+ optional
    ISO8601 time bounds) instead of loading the ~518k-row file into memory.
    """
    start_dt = datetime.fromisoformat(start.replace("Z", "+00:00")) if start else None
    end_dt = datetime.fromisoformat(end.replace("Z", "+00:00")) if end else None

    results = []
    with open(config.HIST_DIR / "historian_timeseries.csv", newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row["tag"] != tag:
                continue
            if start_dt or end_dt:
                ts = datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00"))
                if start_dt and ts < start_dt:
                    continue
                if end_dt and ts > end_dt:
                    continue
            results.append(row)
    return results
