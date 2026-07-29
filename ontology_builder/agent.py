"""Stage 4 - Reasoning agent.

Two modes, selected automatically by `answer_question`:

1. **Agentic (when a tool-calling LLM is configured)**: the LLM is given
   read-only tools (`TOOL_SCHEMAS` + `_run_agentic`) to query the knowledge
   graph itself -- asset lookup, cross-app context, historian trends, and
   physical process-flow relationships (for multi-hop reasoning). The model
   decides what to look up and writes the final answer; every tool call and
   its raw result is logged as evidence for audit. This is the "expose the
   graph via tools, build an agent that reasons" pattern from the handbook.

2. **Deterministic fallback (`_dispatch`, always available)**: rule-based
   causal reasoning built to answer the demo questions in
   TEAM_HANDBOOK.md Sec 7. Used whenever no LLM is configured, or the
   agentic loop fails for any reason (network error, malformed response,
   tool-calling unsupported) -- so the chat never breaks.

Each `_dispatch` handler:
  1. Resolves the asset(s) the question is about (keyword match against a
     small alias table anchored to Stage 1 (system, local_id) pairs).
  2. Gathers evidence via `KnowledgeGraph.context_for_asset` + targeted
     `historian_series` pulls (never the whole 518k-row file).
  3. Applies a small ranking heuristic and returns a natural-language
     answer plus a structured evidence list so the UI can cite sources.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Optional

from .graph import KnowledgeGraph, historian_series
from .llm_provider import NullTextGenerationProvider, get_text_generation_provider

# Scenario "now" -- the story is set on this date/time; used to compute
# human-readable "N hours/days ago" phrasing consistently.
NOW = datetime.fromisoformat("2026-07-28T06:00:00+00:00")

# (keywords, anchor) -- anchor is a (system, local_id) pair from Stage 1,
# used to look up the asset's current unified_id at query time.
_ALIASES: list[dict[str, Any]] = [
    {"name": "P-101", "anchor": ("AM", "PMP-100-101"),
     "keywords": ["p-101", "p101", "charge pump", "crude charge pump"]},
    {"name": "P-102", "anchor": ("APM", "Pump_002"),
     "keywords": ["p-102", "p102", "reflux pump"]},
    {"name": "K-101", "anchor": ("CMMS", "EQ-6001"),
     "keywords": ["k-101", "k101", "recycle gas compressor", "compressor"]},
    {"name": "Column C-101", "anchor": ("DCS", "U100_C101"),
     "keywords": ["column", "distillation column", "differential pressure", "flooding"]},
    {"name": "E-101", "anchor": ("APM", "Exchanger_014"),
     "keywords": ["e-101", "e101", "preheat exchanger", "exchanger"]},
    {"name": "H-101", "anchor": ("DCS", "U100_H101"),
     "keywords": ["h-101", "h101", "fired heater", "heater", "fuel"]},
    {"name": "CV-400", "anchor": ("CMMS", "EQ-8001"),
     "keywords": ["cv-400", "cv400", "cooling water valve", "control valve"]},
    {"name": "TK-201", "anchor": ("AM", "TNK-200-101"),
     "keywords": ["tk-201", "tk201", "storage tank", "tank 201", "tank"]},
]


@dataclass
class AgentAnswer:
    asset: Optional[str]
    scenario: str
    answer: str
    confidence: str
    evidence: list[dict[str, Any]] = field(default_factory=list)
    raw_answer: Optional[str] = None  # the deterministic draft, kept for audit
    presented_by: str = "rule-based"  # "rule-based" | the model name that polished it
    asset_id: Optional[str] = None  # unified_id, used to build the UI's relationship panel
    recommendation: Optional[str] = None  # shown in its own "Recommended Actions" panel
    headline: Optional[str] = None  # short, punchy summary for the big bold UI title


def _anchor_to_unified_id(entities: list[dict[str, Any]], anchor: tuple[str, str]) -> Optional[str]:
    system, local_id = anchor
    for entity in entities:
        for member in entity["members"]:
            if member["system"] == system and member["local_id"] == local_id:
                return entity["unified_id"]
    return None


def _match_aliases(question: str) -> list[dict[str, Any]]:
    q = question.lower()
    scored = []
    for alias in _ALIASES:
        hits = [kw for kw in alias["keywords"] if kw in q]
        if hits:
            scored.append((max(len(h) for h in hits), alias))
    scored.sort(key=lambda t: -t[0])
    return [a for _, a in scored]


def _parse_ts(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _ago(ts: str, now: datetime = NOW) -> str:
    delta = now - _parse_ts(ts)
    hours = delta.total_seconds() / 3600
    if hours < 36:
        return f"{hours:.0f} hours ago"
    return f"{hours / 24:.1f} days ago"


def _downsample_points(series: list[dict[str, Any]], max_points: int = 120) -> list[dict[str, Any]]:
    """Reduce a (potentially thousands-of-rows) historian slice to at most
    `max_points` {t, v} pairs for charting, always keeping the first and
    last reading so the chart's endpoints match the reported trend."""
    n = len(series)
    if n <= max_points:
        return [{"t": r["timestamp"], "v": float(r["value"])} for r in series]
    stride = n / max_points
    picked = [series[int(i * stride)] for i in range(max_points)]
    picked[-1] = series[-1]
    return [{"t": r["timestamp"], "v": float(r["value"])} for r in picked]


def _trend(tag: str, now: datetime = NOW, window_hours: int = 48) -> Optional[dict[str, Any]]:
    start = (now - timedelta(hours=window_hours)).isoformat().replace("+00:00", "Z")
    end = now.isoformat().replace("+00:00", "Z")
    series = historian_series(tag, start=start, end=end)
    if len(series) < 2:
        return None
    first, last = float(series[0]["value"]), float(series[-1]["value"])
    delta = last - first
    pct = (delta / abs(first) * 100) if first else 0.0
    direction = "rising" if pct > 3 else ("falling" if pct < -3 else "stable")
    return {
        "tag": tag,
        "first": first,
        "last": last,
        "pct_change": round(pct, 1),
        "direction": direction,
        "n_readings": len(series),
        "start_ts": series[0]["timestamp"],
        "end_ts": series[-1]["timestamp"],
        # Chart-ready downsampled points -- kept out of the LLM's tool-call
        # results (see _tool_get_historian_trend) to avoid bloating the
        # prompt, but used by build_ui_panel() for the frontend trend chart.
        "points": _downsample_points(series),
    }


def _find_tag(context: dict[str, list[dict[str, Any]]], suffix: str) -> Optional[str]:
    for rec in context.get("HistorianTag", []):
        if rec["id"].endswith(suffix):
            return rec["id"]
    return None


def _latest(records: list[dict[str, Any]], key: str = "timestamp") -> Optional[dict[str, Any]]:
    dated = [r for r in records if r.get(key)]
    return max(dated, key=lambda r: r[key]) if dated else None


def _upstream_neighbors(kg: KnowledgeGraph, unified_id: str, rel_type: str) -> list[str]:
    """Assets `unified_id` is on the *target* side of `rel_type` edges from."""
    return [
        node.id
        for node, edge in kg.neighbors(unified_id, rel_type)
        if edge.target == unified_id
    ]


def _asset_name(entities: list[dict[str, Any]], unified_id: str) -> str:
    for e in entities:
        if e["unified_id"] == unified_id:
            return e["canonical_name"]
    return unified_id


# --------------------------------------------------------------------------
# Scenario handlers
# --------------------------------------------------------------------------

def _answer_vibration(entities, kg, unified_id) -> AgentAnswer:
    name = _asset_name(entities, unified_id)
    context = kg.context_for_asset(unified_id)
    evidence = []

    vib_tag = _find_tag(context, "VIB_01") or _find_tag(context, "VIB_02")
    vib_trend = _trend(vib_tag) if vib_tag else None
    if vib_trend:
        evidence.append({"type": "historian_trend", **vib_trend})

    setpoint_changes = sorted(context.get("OperatorAction", []), key=lambda r: r["timestamp"], reverse=True)
    latest_sp = setpoint_changes[0] if setpoint_changes else None
    if latest_sp:
        evidence.append({"type": "operator_action", **latest_sp})

    work_orders = sorted(context.get("WorkOrder", []), key=lambda r: r["created"], reverse=True)
    seal_wo = next((wo for wo in work_orders if "seal" in (wo.get("notes") or "").lower()
                    or "seal" in (wo.get("parts_used") or "").lower()), None)
    if seal_wo:
        evidence.append({"type": "work_order", **seal_wo})

    health_events = context.get("HealthEvent", [])
    if health_events:
        evidence.append({"type": "health_event", **health_events[-1]})

    lines = [f"{name}'s vibration"]
    if vib_trend:
        lines[0] += (
            f" has been {vib_trend['direction']} ({vib_trend['first']:.1f} -> "
            f"{vib_trend['last']:.1f} mm/s, {vib_trend['pct_change']:+.0f}%) "
            f"over the last {vib_trend['n_readings']} readings."
        )
    else:
        lines[0] += " trend could not be retrieved (no vibration tag / data)."

    causes = []
    if latest_sp and latest_sp["action_type"] == "SP_CHANGE":
        old, new = float(latest_sp["old_value"]), float(latest_sp["new_value"])
        pct = (new - old) / old * 100 if old else 0
        causes.append(
            f"the operator raised the flow setpoint {old:g} -> {new:g} ({pct:+.0f}%) "
            f"{_ago(latest_sp['timestamp'])} (shift {latest_sp['shift']}, {latest_sp['operator']}, "
            f"action {latest_sp['id']})"
        )
    if seal_wo:
        causes.append(
            f"a mechanical seal was replaced {_ago(seal_wo['created'])} (work order {seal_wo['id']}); "
            f"notes: \"{seal_wo['notes']}\""
        )

    recommendation = None
    headline = f"No clear cause found for {name}'s vibration"
    if causes:
        lines.append("Likely contributing factors, most recent first: " + "; and ".join(causes) + ".")
        if latest_sp and seal_wo:
            lines.append(
                "Most likely root cause: the recent setpoint increase is pushing the pump above its "
                "efficiency curve, compounding a recent seal job that was flagged for alignment recheck."
            )
            recommendation = (
                "- Reduce the flow setpoint toward the prior value.\n"
                f"- Inspect and complete the seal alignment recheck flagged on work order {seal_wo['id']}."
            )
            headline = "Setpoint hike + seal misalignment driving vibration"
        elif latest_sp:
            headline = "Recent setpoint increase driving vibration"
        elif seal_wo:
            headline = "Recent seal work may be driving vibration"
        confidence = "medium-high"
    else:
        lines.append("No recent setpoint changes or seal-related work orders were found for this asset.")
        confidence = "low"

    return AgentAnswer(
        _asset_name(entities, unified_id), "vibration", " ".join(lines), confidence, evidence,
        asset_id=unified_id, recommendation=recommendation, headline=headline,
    )


def _answer_fouling_context(entities, kg, e101_id) -> dict[str, Any]:
    context = kg.context_for_asset(e101_id)
    tout_tag = _find_tag(context, "TOUT_01")
    trend = _trend(tout_tag, window_hours=24 * 14) if tout_tag else None
    cleaning_wo = next(
        (wo for wo in context.get("WorkOrder", []) if "clean" in (wo.get("notes") or "").lower()), None
    )
    return {"trend": trend, "work_order": cleaning_wo}


def _answer_fuel_rising(entities, kg, unified_id) -> AgentAnswer:
    name = _asset_name(entities, unified_id)
    context = kg.context_for_asset(unified_id)
    fuel_tag = _find_tag(context, "FUEL_01")
    fuel_trend = _trend(fuel_tag, window_hours=24 * 14) if fuel_tag else None

    upstream = _upstream_neighbors(kg, unified_id, "FEEDS")
    evidence: list[dict[str, Any]] = []
    if fuel_trend:
        evidence.append({"type": "historian_trend", **fuel_trend})

    fouling_lines = []
    open_cleaning_wo = None
    for up_id in upstream:
        fouling = _answer_fouling_context(entities, kg, up_id)
        if fouling["trend"]:
            evidence.append({"type": "historian_trend", **fouling["trend"]})
            fouling_lines.append(
                f"{_asset_name(entities, up_id)}'s outlet temperature has been "
                f"{fouling['trend']['direction']} ({fouling['trend']['pct_change']:+.1f}% over "
                f"{fouling['trend']['n_readings']} readings), consistent with fouling"
            )
        if fouling["work_order"]:
            evidence.append({"type": "work_order", **fouling["work_order"]})
            fouling_lines.append(
                f"a bundle-cleaning work order ({fouling['work_order']['id']}) is "
                f"{fouling['work_order']['status'].lower()} -- notes: \"{fouling['work_order']['notes']}\""
            )
            if fouling["work_order"]["status"] != "Closed":
                open_cleaning_wo = fouling["work_order"]

    lines = [f"{name} fuel gas flow"]
    lines[0] += (
        f" has been {fuel_trend['direction']} ({fuel_trend['pct_change']:+.1f}% over "
        f"{fuel_trend['n_readings']} readings)." if fuel_trend else " trend could not be retrieved."
    )
    recommendation = None
    headline = f"{name} fuel trend inconclusive"
    if fouling_lines:
        lines.append(
            "Root cause: " + "; ".join(fouling_lines) +
            ". A fouled exchanger reduces preheat, forcing the heater to burn more fuel to hit its outlet setpoint."
        )
        confidence = "medium-high"
        headline = "Exchanger fouling driving higher fuel use"
        if open_cleaning_wo:
            recommendation = (
                f"- Expedite the deferred bundle-cleaning work order {open_cleaning_wo['id']} rather than "
                "deferring it further.\n"
                "- Re-check the heater outlet temperature after cleaning to confirm fuel use returns to baseline."
            )
    else:
        confidence = "low"

    return AgentAnswer(
        name, "fuel_rising", " ".join(lines), confidence, evidence,
        asset_id=unified_id, recommendation=recommendation, headline=headline,
    )


def _answer_high_dp(entities, kg, unified_id) -> AgentAnswer:
    name = _asset_name(entities, unified_id)
    context = kg.context_for_asset(unified_id)
    dp_tag = _find_tag(context, "PDP_01")
    dp_trend = _trend(dp_tag, window_hours=24 * 14) if dp_tag else None

    evidence: list[dict[str, Any]] = []
    if dp_trend:
        evidence.append({"type": "historian_trend", **dp_trend})

    causes = []
    for up_id in _upstream_neighbors(kg, unified_id, "FEEDS"):
        up_name = _asset_name(entities, up_id)
        # S2 cascade: cold feed from an upstream exchanger's fouling.
        fouling = _answer_fouling_context(entities, kg, up_id)
        if fouling["trend"] and fouling["trend"]["direction"] == "falling":
            evidence.append({"type": "historian_trend", **fouling["trend"]})
            causes.append(f"cold feed from {up_name} (outlet temp trending down -- fouling, see WO evidence)")
        if fouling["work_order"]:
            evidence.append({"type": "work_order", **fouling["work_order"]})

        # Two-hop: the immediate upstream (e.g. H-101) may itself be fed by
        # a fouled exchanger (e.g. E-101) -- this is the genuine multi-hop
        # chain S3 tests: Column <- H-101 <- E-101 fouling (from S2).
        for up2_id in _upstream_neighbors(kg, up_id, "FEEDS"):
            up2_name = _asset_name(entities, up2_id)
            fouling2 = _answer_fouling_context(entities, kg, up2_id)
            if fouling2["trend"] and fouling2["trend"]["direction"] == "falling":
                evidence.append({"type": "historian_trend", **fouling2["trend"]})
                causes.append(
                    f"cold feed cascading from {up2_name} (fouled, outlet temp "
                    f"{fouling2['trend']['pct_change']:+.1f}%) through {up_name}"
                )
            if fouling2["work_order"]:
                evidence.append({"type": "work_order", **fouling2["work_order"]})

        # Reflux pump cavitation signature: falling suction pressure.
        up_context = kg.context_for_asset(up_id)
        psuc_tag = _find_tag(up_context, "PSUC_02") or _find_tag(up_context, "PSUC_01")
        if psuc_tag:
            psuc_trend = _trend(psuc_tag, window_hours=24 * 14)
            if psuc_trend:
                evidence.append({"type": "historian_trend", **psuc_trend})
                if psuc_trend["direction"] == "falling":
                    causes.append(f"{up_name} suction pressure falling ({psuc_trend['pct_change']:+.1f}%) -- possible cavitation")

    lines = [f"{name} differential pressure"]
    lines[0] += (
        f" has been {dp_trend['direction']} ({dp_trend['pct_change']:+.1f}% over {dp_trend['n_readings']} readings)."
        if dp_trend else " trend could not be retrieved."
    )
    recommendation = None
    headline = f"{name} pressure trend inconclusive"
    if causes:
        lines.append("Multi-hop cause chain: " + "; and ".join(causes) + ".")
        confidence = "medium"
        recommendation = (
            "- Address the upstream fouling (see the linked exchanger's work order) to restore feed temperature.\n"
            "- Inspect the reflux pump for cavitation if suction pressure continues to fall."
        )
        headline = "Cold feed + reflux cavitation raising column dP"
    else:
        confidence = "low"
    return AgentAnswer(
        name, "high_dp", " ".join(lines), confidence, evidence,
        asset_id=unified_id, recommendation=recommendation, headline=headline,
    )


def _answer_same_problem(entities, kg, k101_id, p101_id) -> AgentAnswer:
    k_name, p_name = _asset_name(entities, k101_id), _asset_name(entities, p101_id)
    k_context = kg.context_for_asset(k101_id)
    evidence: list[dict[str, Any]] = []

    vib_tag = _find_tag(k_context, "VIB_02")
    vib_trend = _trend(vib_tag) if vib_tag else None
    lube_tag = _find_tag(k_context, "LUBE_01")
    lube_trend = _trend(lube_tag, window_hours=24 * 14) if lube_tag else None
    if vib_trend:
        evidence.append({"type": "historian_trend", **vib_trend})
    if lube_trend:
        evidence.append({"type": "historian_trend", **lube_trend})

    cv400_wo = None
    for up_id in _upstream_neighbors(kg, k101_id, "COOLS"):
        up_context = kg.context_for_asset(up_id)
        cv400_wo = next(iter(sorted(up_context.get("WorkOrder", []), key=lambda r: r["created"], reverse=True)), None)
        if cv400_wo:
            evidence.append({"type": "work_order", **cv400_wo})

    own_wo = k_context.get("WorkOrder", [])

    lines = [f"No -- {k_name} shows a vibration alarm like {p_name}, but the root cause is different."]
    if vib_trend:
        lines.append(f"{k_name} vibration: {vib_trend['direction']} ({vib_trend['pct_change']:+.1f}%).")
    if lube_trend:
        lines.append(f"Lube oil temperature: {lube_trend['direction']} ({lube_trend['pct_change']:+.1f}%).")
    if cv400_wo:
        lines.append(
            f"Cooling-water valve CV-400 (which cools {k_name}'s lube oil) has an {cv400_wo['status'].lower()} "
            f"work order ({cv400_wo['id']}): \"{cv400_wo['notes']}\"."
        )
    if not own_wo:
        lines.append(f"No maintenance/seal work orders exist directly on {k_name} itself.")
    lines.append(
        f"Root cause for {k_name}: lube-oil overheating from a stuck cooling valve -- a utility problem, "
        f"NOT a seal/setpoint issue like {p_name}."
    )
    wo_suffix = f" ({cv400_wo['id']})" if cv400_wo else ""
    recommendation = (
        f"- Expedite the CV-400 corrective work order{wo_suffix}.\n"
        f"- Verify lube-oil cooling is restored before continuing normal {k_name} operation."
    )
    return AgentAnswer(
        k_name, "same_problem_comparison", " ".join(lines), "medium-high", evidence,
        asset_id=k101_id, recommendation=recommendation,
        headline=f"No -- {k_name}'s cause differs from {p_name}'s",
    )


def _answer_alarm_flood(entities, kg, unified_id) -> AgentAnswer:
    name = _asset_name(entities, unified_id)
    context = kg.context_for_asset(unified_id)
    alarms = sorted(context.get("AlarmEvent", []), key=lambda r: r["timestamp"])
    evidence = [{"type": "alarm_event", **a} for a in alarms]

    cfg = context.get("AlarmConfig", [None])[0] if context.get("AlarmConfig") else None
    if cfg:
        evidence.append({"type": "alarm_config", **cfg})

    values = [float(a["value"]) for a in alarms if a.get("value")]
    active_count = sum(1 for a in alarms if a["state"] == "ACTIVE")

    lines = [f"{name} has {len(alarms)} alarm transitions ({active_count} ACTIVE) in the data."]
    if values:
        lines.append(
            f"The underlying value oscillates narrowly between {min(values):.1f} and {max(values):.1f} "
            f"-- a tight band right around the alarm threshold, not a real process excursion."
        )

    unit = (cfg.get("eng_unit") or "") if cfg else ""
    limit_h = cfg.get("limit_h") if cfg else None
    deadband = cfg.get("deadband") if cfg else None
    if cfg and (limit_h is not None or deadband is not None):
        limit_text = f"H={limit_h}{unit}" if limit_h is not None else "no H limit configured"
        lines.append(
            f"Configured alarm limits: {limit_text}, deadband {deadband}{unit} -- a deadband this tight "
            "relative to the observed value swing lets the reading chatter back and forth across the "
            "threshold instead of alarming once and staying."
        )

    lines.append(
        "This pattern (rapid ACTIVE/RTN cycling with no corresponding work order or real process trend) "
        "is a classic mis-set alarm limit / nuisance alarm, not a genuine process upset."
    )

    action = "- Widen the deadband"
    if deadband is not None:
        action += f" (currently {deadband}{unit})"
    action += " and/or review the H threshold"
    if limit_h is not None:
        action += f" (currently {limit_h}{unit})"
    action += " rather than investigating the tank itself."

    return AgentAnswer(
        name, "alarm_flood_config", " ".join(lines), "medium-high", evidence,
        asset_id=unified_id, recommendation=action,
        headline="Nuisance alarm from a mis-set limit, not a real upset",
    )


def _answer_full_context(entities, kg, unified_id) -> AgentAnswer:
    name = _asset_name(entities, unified_id)
    context = kg.context_for_asset(unified_id)
    evidence = [
        {"type": label, **rec} for label, records in context.items() for rec in records
    ]
    counts = ", ".join(f"{label}: {len(records)}" for label, records in context.items())
    entity = next(e for e in entities if e["unified_id"] == unified_id)
    system_ids = ", ".join(f"{m['system']}={m['local_id']}" for m in entity["members"])
    answer = (
        f"{name} (confidence {entity['confidence']}) is known across systems as: {system_ids}. "
        f"Connected records -> {counts}."
    )
    return AgentAnswer(
        name, "full_context", answer, "n/a", evidence, asset_id=unified_id,
        headline=f"Everything known about {name}",
    )


def _answer_maintenance_ops_join(entities, kg, unified_id) -> AgentAnswer:
    name = _asset_name(entities, unified_id)
    context = kg.context_for_asset(unified_id)
    cutoff = NOW - timedelta(days=7)

    recent_wo = [wo for wo in context.get("WorkOrder", []) if _parse_ts(wo["created"]) >= cutoff]
    recent_ops = [op for op in context.get("OperatorAction", []) if _parse_ts(op["timestamp"]) >= cutoff]
    evidence = [{"type": "work_order", **wo} for wo in recent_wo] + [
        {"type": "operator_action", **op} for op in recent_ops
    ]

    lines = []
    if recent_wo:
        lines.append(
            "Maintenance in the last 7 days: " +
            "; ".join(f"{wo['id']} ({wo['wo_type']}, {wo['status']}) - \"{wo['notes']}\"" for wo in recent_wo)
        )
    else:
        lines.append("No maintenance work orders in the last 7 days.")
    if recent_ops:
        lines.append(
            "Operations changes in the same window: " +
            "; ".join(
                f"{op['action_type']} {op['old_value']}->{op['new_value']} by {op['operator']} ({_ago(op['timestamp'])})"
                for op in recent_ops
            )
        )
        lines.append("Yes -- both a maintenance event and an operations change occurred in the same window.")
    else:
        lines.append("No operator setpoint changes in the same window.")

    headline = (
        "Maintenance and an operations change both occurred recently"
        if recent_wo and recent_ops
        else "No recent maintenance/operations overlap found"
    )
    return AgentAnswer(
        name, "maintenance_ops_join", " ".join(lines), "high", evidence, asset_id=unified_id, headline=headline,
    )


def _dispatch(question: str, entities: list[dict[str, Any]], kg: KnowledgeGraph) -> AgentAnswer:
    """Deterministic routing + evidence gathering -- the part that must
    stay stable/reproducible regardless of whether a language model is available."""
    matches = _match_aliases(question)
    if not matches:
        known = ", ".join(a["name"] for a in _ALIASES)
        return AgentAnswer(
            None, "unresolved", f"I couldn't identify which asset you mean. Known assets: {known}.", "n/a",
            headline="Couldn't identify which asset you mean",
        )

    q = question.lower()
    primary_id = _anchor_to_unified_id(entities, matches[0]["anchor"])
    if primary_id is None:
        return AgentAnswer(
            matches[0]["name"], "unresolved",
            f"{matches[0]['name']} was matched but not found in the resolved entity graph.", "n/a",
            headline=f"{matches[0]['name']} not found in the resolved graph",
        )

    if ("same problem" in q or "same as" in q) and len(matches) > 1:
        secondary_id = _anchor_to_unified_id(entities, matches[1]["anchor"])
        if secondary_id:
            return _answer_same_problem(entities, kg, primary_id, secondary_id)

    if "everything" in q or "show me" in q or "across all systems" in q:
        return _answer_full_context(entities, kg, primary_id)

    if "maintenance" in q and ("last week" in q or "operations" in q):
        return _answer_maintenance_ops_join(entities, kg, primary_id)

    if "vibrat" in q:
        return _answer_vibration(entities, kg, primary_id)

    if "fuel" in q:
        return _answer_fuel_rising(entities, kg, primary_id)

    if "differential pressure" in q or "flooding" in q or " dp" in q:
        return _answer_high_dp(entities, kg, primary_id)

    if "alarm" in q:
        return _answer_alarm_flood(entities, kg, primary_id)

    return _answer_full_context(entities, kg, primary_id)


_AGENT_SYSTEM_PROMPT = (
    "You are a plant reliability engineer's assistant for the Hindenberg Refinery, Unit 100. "
    "You have read-only tools to query a knowledge graph that unifies six plant IT systems "
    "(Alarm Management, Asset Performance Monitoring, DCS/control, Historian, CMMS/maintenance, "
    "ERP) into one physical-asset view, plus the plant's physical process-flow relationships.\n"
    "Rules:\n"
    "1. ALWAYS use the tools to look up real data before answering -- never invent facts, IDs, "
    "numbers, or dates. If a tool returns an error or empty result, say so rather than guessing.\n"
    "2. Start with list_assets if you don't already know the exact unified_id for the asset in "
    "question.\n"
    "2b. If the question isn't about one specific known asset -- e.g. it asks about plant-wide "
    "status ('what's currently alarming', 'what maintenance is open right now'), or you need to "
    "find records by keyword/content rather than by asset ('which work orders mention a shim "
    "kit') -- use get_plant_status_summary and/or search_evidence instead of guessing an asset "
    "from list_assets.\n"
    "3. Cite the specific evidence you used (work order IDs, action IDs, historian tag names) in "
    "your final answer.\n"
    "4. When reasoning about causes, consider both the asset's own history AND related "
    "upstream/downstream assets via get_related_assets -- some problems cascade across equipment "
    "(e.g. a fouled exchanger raising a downstream heater's fuel use).\n"
    "5. Use get_historian_trend with a short window (e.g. 48 hours) for fast-moving signals like "
    "vibration, and a longer window (e.g. 336 hours / 14 days) for slow trends like fouling or "
    "lube-oil temperature.\n"
    "6. If the evidence doesn't support a confident conclusion, say so explicitly rather than "
    "guessing.\n"
    "7. Structure your final answer in Markdown with EXACTLY three top-level sections, in this "
    "order, using these exact headings and no others:\n"
    "   '## Headline' -- ONE short, punchy phrase (max 12 words, no trailing period) stating the "
    "likely cause, e.g. 'Setpoint hike plus seal misalignment driving vibration'. This is shown as "
    "a large bold title in the UI, so it must be brief -- not a full sentence.\n"
    "   '## Root Cause' -- a concise 3-5 sentence analysis (not 4-8) ranking the most likely cause "
    "when there are multiple contributing factors. Be economical: cite the key evidence, don't "
    "restate every tool result.\n"
    "   '## Recommended Actions' -- 2-4 short Markdown bullet points, one line each, grounded "
    "only in the evidence you found.\n"
    "8. IMPORTANT: You have a limited output budget. ALWAYS finish the '## Recommended Actions' "
    "section -- it is required. If you are running long, shorten the '## Root Cause' section "
    "rather than omitting or truncating '## Recommended Actions'."
)


TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "list_assets",
            "description": (
                "List every physical asset the ontology knows about, with its canonical name "
                "and per-system IDs (e.g. which CMMS work orders or DCS tags belong to it)."
            ),
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_asset_context",
            "description": (
                "Get every alarm, alarm-point configuration (configured HH/H/L/LL limits and "
                "deadband), work order, operator setpoint change, health event, cost posting, and "
                "historian tag connected to one physical asset."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "unified_id": {
                        "type": "string",
                        "description": "The asset's unified_id from list_assets, e.g. 'ASSET-002'.",
                    }
                },
                "required": ["unified_id"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_historian_trend",
            "description": (
                "Get the direction and percent change of a historian tag's value over a recent "
                "time window (never returns raw rows -- only a computed trend summary)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "tag": {
                        "type": "string",
                        "description": "Full historian tag id, e.g. 'FAC1.UNIT100.CENTRIFUGAL_PUMP_101.VIB_01' (see HistorianTag entries from get_asset_context).",
                    },
                    "window_hours": {
                        "type": "number",
                        "description": "Lookback window in hours. Default 48.",
                    },
                },
                "required": ["tag"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_related_assets",
            "description": (
                "Get the physical process-flow relationships (e.g. FEEDS, COOLS, "
                "SUPPLIES_UTILITY) connected to an asset -- use this to reason across multi-hop "
                "causal chains, such as an upstream exchanger's fouling affecting a downstream "
                "heater or column."
            ),
            "parameters": {
                "type": "object",
                "properties": {"unified_id": {"type": "string"}},
                "required": ["unified_id"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_evidence",
            "description": (
                "Free-text search across alarm, work order, operator action, health-event, and "
                "cost-posting records (notes, symptoms, alarm points, technicians, etc.) for a "
                "keyword or phrase -- use this when you don't yet know which asset a question is "
                "about, or want to find records by CONTENT rather than by asset (e.g. 'which work "
                "orders mention a shim kit', 'find any alarm about high vibration')."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Keyword or phrase to search for (case-insensitive substring match).",
                    },
                    "systems": {
                        "type": "array",
                        "items": {"type": "string", "enum": ["AM", "CMMS", "DCS", "APM", "ERP"]},
                        "description": "Optional: restrict the search to these systems only.",
                    },
                    "max_results": {
                        "type": "number",
                        "description": "Max matches to return. Default 20, capped at 50.",
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_plant_status_summary",
            "description": (
                "Get a plant-wide snapshot across ALL assets: every currently ACTIVE alarm, every "
                "non-Closed work order, and recent APM health events (predicted failures / "
                "anomalies). Use this for open-ended 'what's going on right now' / 'what's "
                "currently alarming' / 'what maintenance is open' questions that aren't about one "
                "specific asset."
            ),
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
]

_MAX_AGENT_TURNS = 6

# Node labels that hold free-text/content fields worth searching, mapped to
# the plant IT system that produces them (matches the system names used
# throughout docs/TEAM_HANDBOOK.md and the other tool schemas).
_SEARCHABLE_NODE_LABELS: dict[str, str] = {
    "AlarmEvent": "AM",
    "AlarmConfig": "AM",
    "WorkOrder": "CMMS",
    "OperatorAction": "DCS",
    "HealthEvent": "APM",
    "CostPosting": "ERP",
}

# Which properties of each record type are worth substring-matching against
# a search query -- deliberately excludes pure identifiers/timestamps.
_SEARCH_FIELDS: dict[str, tuple[str, ...]] = {
    "AlarmEvent": ("alarm_point", "alarm_type", "state", "operator"),
    "AlarmConfig": ("measurement", "cause", "consequence", "recommended_action"),
    "WorkOrder": ("wo_type", "status", "technician", "parts_used", "notes"),
    "OperatorAction": ("loop_id", "action_type", "operator", "shift"),
    "HealthEvent": ("event_type", "symptom", "recommendation"),
    "CostPosting": ("type", "linked_wo"),
}


def _asset_for_record(kg: KnowledgeGraph, record_id: str) -> tuple[Optional[str], Optional[str]]:
    """Given a record node id (AlarmEvent/WorkOrder/OperatorAction/HealthEvent/
    CostPosting), find the one Asset it's attached to via its HAS_* edge --
    returns (unified_id, canonical_name), both None if unattached."""
    for neighbor_node, _edge in kg.neighbors(record_id):
        if neighbor_node.label == "Asset":
            return neighbor_node.id, neighbor_node.properties.get("canonical_name")
    return None, None


def _tool_list_assets(entities: list[dict[str, Any]], kg: KnowledgeGraph, args: dict) -> Any:
    return [
        {
            "unified_id": e["unified_id"],
            "canonical_name": e["canonical_name"],
            "systems": {m["system"]: m["local_id"] for m in e["members"]},
        }
        for e in entities
    ]


def _tool_get_asset_context(entities: list[dict[str, Any]], kg: KnowledgeGraph, args: dict) -> Any:
    unified_id = args.get("unified_id", "")
    if unified_id not in kg.nodes:
        return {"error": f"Unknown unified_id '{unified_id}'. Call list_assets to see valid ids."}
    context = kg.context_for_asset(unified_id)
    annotated: dict[str, list[dict[str, Any]]] = {}
    for label, records in context.items():
        rows = []
        for r in records:
            r = dict(r)
            ts = r.get("timestamp") or r.get("created")
            if ts:
                try:
                    r["time_ago"] = _ago(ts)
                except Exception:
                    pass
            rows.append(r)
        annotated[label] = rows
    return {"reference_time": NOW.isoformat(), "records": annotated}


def _tool_get_historian_trend(entities: list[dict[str, Any]], kg: KnowledgeGraph, args: dict) -> Any:
    tag = args.get("tag", "")
    window_hours = args.get("window_hours", 48)
    try:
        window_hours = int(window_hours)
    except (TypeError, ValueError):
        window_hours = 48
    trend = _trend(tag, window_hours=window_hours)
    if trend is None:
        return {"error": f"No/insufficient historian data for tag '{tag}' in a {window_hours}h window."}
    return trend


def _tool_get_related_assets(entities: list[dict[str, Any]], kg: KnowledgeGraph, args: dict) -> Any:
    unified_id = args.get("unified_id", "")
    if unified_id not in kg.nodes:
        return {"error": f"Unknown unified_id '{unified_id}'. Call list_assets to see valid ids."}
    related = []
    for node, edge in kg.neighbors(unified_id):
        if node.label != "Asset" or edge.rel_type not in ("FEEDS", "COOLS", "SUPPLIES_UTILITY"):
            continue
        related.append(
            {
                "unified_id": node.id,
                "canonical_name": node.properties.get("canonical_name"),
                "relationship": edge.rel_type,
                "direction": "upstream" if edge.target == unified_id else "downstream",
                "note": edge.properties.get("note"),
            }
        )
    return related


def _tool_search_evidence(entities: list[dict[str, Any]], kg: KnowledgeGraph, args: dict) -> Any:
    query = (args.get("query") or "").strip().lower()
    if not query:
        return {"error": "'query' must be a non-empty string."}

    systems = args.get("systems")
    wanted_systems = {s.upper() for s in systems if isinstance(s, str)} if systems else None

    max_results = args.get("max_results", 20)
    try:
        max_results = max(1, min(int(max_results), 50))
    except (TypeError, ValueError):
        max_results = 20

    results = []
    for node in kg.nodes.values():
        system = _SEARCHABLE_NODE_LABELS.get(node.label)
        if system is None or (wanted_systems and system not in wanted_systems):
            continue
        matched_field = next(
            (
                f
                for f in _SEARCH_FIELDS.get(node.label, ())
                if node.properties.get(f) and query in str(node.properties[f]).lower()
            ),
            None,
        )
        if matched_field is None:
            continue
        asset_id, asset_name = _asset_for_record(kg, node.id)
        results.append(
            {
                "record_type": node.label,
                "system": system,
                "id": node.id,
                "matched_field": matched_field,
                "asset_id": asset_id,
                "asset_name": asset_name,
                **node.properties,
            }
        )
        if len(results) >= max_results:
            break
    return {"query": args.get("query", ""), "count": len(results), "results": results}


def _tool_get_plant_status_summary(entities: list[dict[str, Any]], kg: KnowledgeGraph, args: dict) -> Any:
    active_alarms, open_work_orders, recent_health_events = [], [], []
    for node in kg.nodes.values():
        if node.label == "AlarmEvent" and node.properties.get("state") == "ACTIVE":
            asset_id, asset_name = _asset_for_record(kg, node.id)
            active_alarms.append({"id": node.id, "asset_id": asset_id, "asset_name": asset_name, **node.properties})
        elif node.label == "WorkOrder" and node.properties.get("status") != "Closed":
            asset_id, asset_name = _asset_for_record(kg, node.id)
            open_work_orders.append({"id": node.id, "asset_id": asset_id, "asset_name": asset_name, **node.properties})
        elif node.label == "HealthEvent":
            asset_id, asset_name = _asset_for_record(kg, node.id)
            recent_health_events.append(
                {"id": node.id, "asset_id": asset_id, "asset_name": asset_name, **node.properties}
            )

    active_alarms.sort(key=lambda r: r.get("timestamp") or "", reverse=True)
    open_work_orders.sort(key=lambda r: r.get("created") or "", reverse=True)
    recent_health_events.sort(key=lambda r: r.get("timestamp") or "", reverse=True)
    return {
        "reference_time": NOW.isoformat(),
        # Capped, not just for token budget -- these are meant as a
        # plant-wide snapshot, not an exhaustive dump.
        "active_alarms": active_alarms[:30],
        "open_work_orders": open_work_orders[:30],
        "recent_health_events": recent_health_events[:15],
    }


_TOOL_EXECUTORS = {
    "list_assets": _tool_list_assets,
    "get_asset_context": _tool_get_asset_context,
    "get_historian_trend": _tool_get_historian_trend,
    "get_related_assets": _tool_get_related_assets,
    "search_evidence": _tool_search_evidence,
    "get_plant_status_summary": _tool_get_plant_status_summary,
}


def _primary_asset_id_from_trace(trace: list[dict[str, Any]]) -> Optional[str]:
    """Best-effort: the first asset the agent looked up via get_asset_context
    (falling back to get_related_assets) -- used to build the UI's
    relationship panel for agentic answers."""
    for call in trace:
        if call.get("tool") == "get_asset_context":
            unified_id = call.get("arguments", {}).get("unified_id")
            if unified_id:
                return unified_id
    for call in trace:
        if call.get("tool") == "get_related_assets":
            unified_id = call.get("arguments", {}).get("unified_id")
            if unified_id:
                return unified_id
    # Lower-priority fallback: an asset surfaced by a plant-wide/content
    # search tool rather than looked up directly -- still useful for the
    # UI's relationship panel when that's all the agent called.
    for call in trace:
        if call.get("tool") == "search_evidence":
            result = call.get("result")
            if isinstance(result, dict):
                for row in result.get("results", []):
                    if isinstance(row, dict) and row.get("asset_id"):
                        return row["asset_id"]
    for call in trace:
        if call.get("tool") == "get_plant_status_summary":
            result = call.get("result")
            if isinstance(result, dict):
                for key in ("active_alarms", "open_work_orders", "recent_health_events"):
                    for row in result.get(key, []):
                        if isinstance(row, dict) and row.get("asset_id"):
                            return row["asset_id"]
    return None


_HEADLINE_HEADING_RE = re.compile(r"^#{1,3}\s*Headline\s*$", re.IGNORECASE | re.MULTILINE)
_RECOMMENDED_ACTIONS_HEADING_RE = re.compile(r"^#{1,3}\s*Recommended Actions?\s*$", re.IGNORECASE | re.MULTILINE)
_ROOT_CAUSE_HEADING_RE = re.compile(r"^#{1,3}\s*Root Cause\s*$", re.IGNORECASE | re.MULTILINE)


def _split_agent_response(content: str) -> tuple[Optional[str], str, Optional[str]]:
    """Split the model's Markdown answer into (headline, root_cause,
    recommendation) using the '## Headline' / '## Root Cause' /
    '## Recommended Actions' headings requested by `_AGENT_SYSTEM_PROMPT`.
    Falls back gracefully (missing sections become None, everything else
    stays in root_cause) if the model didn't follow the requested
    structure -- never raises."""
    rec_match = _RECOMMENDED_ACTIONS_HEADING_RE.search(content)
    before_rec = content[: rec_match.start()] if rec_match else content
    recommendation = content[rec_match.end():].strip() if rec_match else None

    root_match = _ROOT_CAUSE_HEADING_RE.search(before_rec)
    before_root = before_rec[: root_match.start()] if root_match else ""
    root_cause = before_rec[root_match.end():].strip() if root_match else before_rec.strip()

    headline_match = _HEADLINE_HEADING_RE.search(before_root)
    headline = before_root[headline_match.end():].strip() if headline_match else None
    if headline:
        # Headline should be a single short line -- guard against a model
        # accidentally writing multiple lines/paragraphs under it.
        headline = headline.splitlines()[0].strip() or None

    return headline, root_cause or content.strip(), (recommendation or None)


def _trend_result_for_llm(result: Any) -> Any:
    """Strip the chart-only 'points' array (per-minute readings, up to 120 of
    them) before a tool result is sent back to the model -- it only needs
    the summary stats (first/last/pct_change/direction) to reason, and the
    raw points would waste a large slice of the per-turn token budget. The
    full result (with points) stays in `trace` for build_ui_panel's chart."""
    if isinstance(result, dict) and "points" in result:
        return {k: v for k, v in result.items() if k != "points"}
    return result


def _run_agentic(
    question: str, entities: list[dict[str, Any]], kg: KnowledgeGraph, provider: Any
) -> AgentAnswer:
    """Let the configured LLM query the knowledge graph itself via tools and
    write its own answer. Every tool call + raw result is logged as
    evidence. Raises if the provider doesn't support tool calling or the
    call otherwise fails -- callers (see `answer_question`) must catch this
    and fall back to `_dispatch`."""
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": _AGENT_SYSTEM_PROMPT},
        {"role": "user", "content": question},
    ]
    trace: list[dict[str, Any]] = []

    for _ in range(_MAX_AGENT_TURNS):
        # 1600 tokens -- enough headroom for a full "## Headline" + "## Root
        # Cause" + "## Recommended Actions" answer without truncating before
        # the final section (tool-call-only turns produce short output
        # regardless, so this is a safe budget for every turn, not just the
        # final one). Raised from 1000 after observing Recommended Actions
        # still getting cut off on verbose Root Cause responses.
        message = provider.chat(messages, tools=TOOL_SCHEMAS, max_tokens=1600)
        messages.append(message)
        tool_calls = message.get("tool_calls") or []
        if not tool_calls:
            content = (message.get("content") or "").strip()
            primary_id = _primary_asset_id_from_trace(trace)
            headline, root_cause, recommendation = _split_agent_response(
                content or "The model returned an empty response."
            )
            return AgentAnswer(
                asset=_asset_name(entities, primary_id) if primary_id else None,
                scenario="agentic",
                answer=root_cause,
                confidence="model-reasoned",
                evidence=trace,
                presented_by=getattr(provider, "model", type(provider).__name__),
                asset_id=primary_id,
                recommendation=recommendation,
                headline=headline,
            )
        for call in tool_calls:
            name = call["function"]["name"]
            try:
                call_args = json.loads(call["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                call_args = {}
            executor = _TOOL_EXECUTORS.get(name)
            result = executor(entities, kg, call_args) if executor else {"error": f"Unknown tool '{name}'"}
            trace.append({"type": "tool_call", "tool": name, "arguments": call_args, "result": result})
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.get("id"),
                    "content": json.dumps(_trend_result_for_llm(result), default=str),
                }
            )

    primary_id = _primary_asset_id_from_trace(trace)
    return AgentAnswer(
        asset=_asset_name(entities, primary_id) if primary_id else None,
        scenario="agentic",
        answer="I wasn't able to reach a conclusion within the tool-call budget.",
        confidence="low",
        evidence=trace,
        presented_by=getattr(provider, "model", type(provider).__name__),
        asset_id=primary_id,
        headline="Inconclusive within the tool-call budget",
    )


def answer_question(question: str, entities: list[dict[str, Any]], kg: KnowledgeGraph) -> AgentAnswer:
    """Public entry point. Uses the LLM-driven agentic tool-calling loop when
    a language model is configured; falls back to the deterministic
    `_dispatch` handlers (unchanged, always available) if no LLM is
    configured or the agentic loop fails for any reason."""
    provider = get_text_generation_provider()
    if isinstance(provider, NullTextGenerationProvider):
        return _dispatch(question, entities, kg)
    try:
        return _run_agentic(question, entities, kg, provider)
    except Exception:
        return _dispatch(question, entities, kg)


# --------------------------------------------------------------------------
# UI relationship/timeline panel -- flattens either evidence shape
# (deterministic mode's flat typed records, or agentic mode's tool-call
# trace) into one common structure the frontend renders: entity aliases +
# process-flow relationships, a chronological timeline, and evidence cards.
# --------------------------------------------------------------------------

_RECORD_LABEL_TO_TYPE = {
    "AlarmEvent": "alarm_event",
    "AlarmConfig": "alarm_config",
    "WorkOrder": "work_order",
    "OperatorAction": "operator_action",
    "HealthEvent": "health_event",
    "CostPosting": "cost_posting",
}

_TIMELINE_SOURCE = {
    "alarm_event": "AM",
    "alarm_config": "AM",
    "work_order": "CMMS",
    "operator_action": "DCS",
    "health_event": "APM",
    "cost_posting": "ERP",
    "historian_trend": "HIST",
}


def _describe_record(item: dict[str, Any]) -> Optional[dict[str, str]]:
    """Return {time, source, text, ref} for one flattened evidence record,
    or None if it's not a describable/temporal record type."""
    t = item.get("type")
    if t == "alarm_event":
        return {
            "type": t,
            "time": item.get("timestamp") or "",
            "source": _TIMELINE_SOURCE[t],
            "ref": item.get("id", ""),
            "text": f"Alarm {item.get('alarm_point')} reached {item.get('alarm_type')} "
                    f"({item.get('state')}) at {item.get('value')}.",
        }
    if t == "work_order":
        return {
            "type": t,
            "time": item.get("created") or "",
            "source": _TIMELINE_SOURCE[t],
            "ref": item.get("id", ""),
            "text": f"Work order {item.get('id')} ({item.get('wo_type')}, {item.get('status')}): "
                    f"{item.get('notes')}",
        }
    if t == "operator_action":
        return {
            "type": t,
            "time": item.get("timestamp") or "",
            "source": _TIMELINE_SOURCE[t],
            "ref": item.get("id", ""),
            "text": f"{item.get('action_type')} on {item.get('loop_id')}: {item.get('old_value')} -> "
                    f"{item.get('new_value')} ({item.get('operator')}, shift {item.get('shift')}).",
        }
    if t == "health_event":
        return {
            "type": t,
            "time": item.get("timestamp") or "",
            "source": _TIMELINE_SOURCE[t],
            "ref": item.get("id", ""),
            "text": f"{item.get('event_type')}: {item.get('symptom')} "
                    f"(confidence {item.get('confidence')}) -- {item.get('recommendation')}",
        }
    if t == "cost_posting":
        return {
            "type": t,
            "time": item.get("posting_date") or "",
            "source": _TIMELINE_SOURCE[t],
            "ref": item.get("id", ""),
            "text": f"Cost posting {item.get('id')} (${item.get('amount_usd')}, {item.get('type')}) "
                    f"linked to work order {item.get('linked_wo')}.",
        }
    if t == "alarm_config":
        unit = item.get("eng_unit") or ""
        limit_bits = [
            f"{label}={item[key]}{unit}"
            for label, key in (("LL", "limit_ll"), ("L", "limit_l"), ("H", "limit_h"), ("HH", "limit_hh"))
            if item.get(key) is not None
        ]
        limits_text = ", ".join(limit_bits) if limit_bits else "no limits configured"
        return {
            "type": t,
            # No timestamp -- this is static configuration, not a dated
            # event, so it's deliberately excluded from the Timeline (build_
            # ui_panel filters on a truthy "time") but still shown as an
            # evidence card.
            "time": "",
            "source": _TIMELINE_SOURCE[t],
            "ref": item.get("id", ""),
            "text": f"Configured alarm limits for {item.get('id')}: {limits_text}, "
                    f"deadband {item.get('deadband')}{unit}.",
        }
    if t == "historian_trend" and "direction" in item:
        pct = item.get("pct_change")
        pct_text = f"{pct:+.1f}%" if isinstance(pct, (int, float)) else "n/a"
        return {
            "type": t,
            "time": item.get("end_ts") or "",
            "source": _TIMELINE_SOURCE[t],
            "ref": item.get("tag", ""),
            "text": f"{item.get('tag')} trend: {item.get('direction')} ({pct_text} over "
                    f"{item.get('n_readings')} readings).",
        }
    return None


def _flatten_evidence(evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalize both evidence shapes into one flat list of typed records."""
    flat: list[dict[str, Any]] = []
    for item in evidence:
        if item.get("type") == "tool_call":
            tool = item.get("tool")
            result = item.get("result")
            if tool == "get_asset_context" and isinstance(result, dict):
                for label, rows in result.get("records", {}).items():
                    mapped_type = _RECORD_LABEL_TO_TYPE.get(label)
                    if not mapped_type or not isinstance(rows, list):
                        continue
                    for row in rows:
                        if isinstance(row, dict):
                            flat.append({**row, "type": mapped_type})
            elif tool == "get_historian_trend" and isinstance(result, dict) and "direction" in result:
                flat.append({**result, "type": "historian_trend"})
            elif tool == "search_evidence" and isinstance(result, dict):
                for row in result.get("results", []):
                    if not isinstance(row, dict):
                        continue
                    mapped_type = _RECORD_LABEL_TO_TYPE.get(row.get("record_type"))
                    if mapped_type:
                        flat.append({**row, "type": mapped_type})
            elif tool == "get_plant_status_summary" and isinstance(result, dict):
                for key, label in (
                    ("active_alarms", "AlarmEvent"),
                    ("open_work_orders", "WorkOrder"),
                    ("recent_health_events", "HealthEvent"),
                ):
                    mapped_type = _RECORD_LABEL_TO_TYPE.get(label)
                    for row in result.get(key, []):
                        if isinstance(row, dict) and mapped_type:
                            flat.append({**row, "type": mapped_type})
            # list_assets / get_related_assets results aren't temporal records.
        else:
            flat.append(item)
    return flat


def build_ui_panel(
    entities: list[dict[str, Any]], kg: KnowledgeGraph, answer: AgentAnswer
) -> Optional[dict[str, Any]]:
    """Build the entity/timeline/evidence panel the frontend renders
    alongside the answer. Returns None if no asset could be resolved for
    this answer (e.g. the model never looked up a specific asset)."""
    if not answer.asset_id or answer.asset_id not in kg.nodes:
        return None
    entity = next((e for e in entities if e["unified_id"] == answer.asset_id), None)
    if entity is None:
        return None

    relationships = [
        {"type": "alias", "label": f'{m["system"]}:{m["local_id"]}', "ref": answer.asset_id}
        for m in entity["members"]
    ]
    for node, edge in kg.neighbors(answer.asset_id):
        if node.label != "Asset" or edge.rel_type not in ("FEEDS", "COOLS", "SUPPLIES_UTILITY"):
            continue
        other_name = node.properties.get("canonical_name", node.id)
        label = (
            f'{entity["canonical_name"]} {edge.rel_type} {other_name}'
            if edge.source == answer.asset_id
            else f'{other_name} {edge.rel_type} {entity["canonical_name"]}'
        )
        relationships.append({"type": "linked", "label": label, "ref": node.id})

    flat = _flatten_evidence(answer.evidence)
    described = [d for d in (_describe_record(item) for item in flat) if d]

    timeline = sorted(
        (
            {"time": d["time"], "source": d["source"], "text": d["text"], "ref": d["ref"]}
            for d in described
            if d["time"]
        ),
        key=lambda e: e["time"],
    )
    # Historian trend records are excluded here -- they're shown as charts in
    # the "Sensor Trends" panel instead (with strictly more detail: the full
    # series, not just a one-line summary), so listing them again as plain
    # text cards here would just be a duplicate of the same information.
    # They're still included in the Timeline above since that's a distinct,
    # useful chronological view alongside the other cross-system events.
    evidence_cards = [
        {
            "title": d["source"] + " Record",
            "source": d["ref"] or d["source"],
            "record": d["text"],
            "ref": d["ref"],
        }
        for d in described
        if d["type"] != "historian_trend"
    ]

    # Sensor trend charts -- one per distinct historian tag actually cited
    # as evidence for this answer (e.g. VIB_01 for a vibration question,
    # FUEL_01 for a heater question), so the chart shown always matches
    # what the answer is actually about, with no per-scenario special-casing.
    charts = []
    seen_tags: set[str] = set()
    for item in flat:
        if item.get("type") != "historian_trend" or not item.get("points"):
            continue
        tag = item.get("tag")
        if not tag or tag in seen_tags:
            continue
        seen_tags.add(tag)
        tag_node = kg.nodes.get(tag)
        props = tag_node.properties if tag_node else {}
        charts.append(
            {
                "tag": tag,
                "label": props.get("description") or tag,
                "unit": props.get("eng_unit") or "",
                "direction": item.get("direction"),
                "pct_change": item.get("pct_change"),
                "n_readings": item.get("n_readings"),
                "start_ts": item.get("start_ts"),
                "end_ts": item.get("end_ts"),
                "points": item["points"],
            }
        )
        if len(charts) >= 3:
            break

    return {
        "entity": entity["canonical_name"],
        "entity_id": answer.asset_id,
        "relationships": relationships,
        "timeline": timeline,
        "evidence": evidence_cards,
        "charts": charts,
    }
