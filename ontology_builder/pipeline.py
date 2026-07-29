"""Orchestrates Stage 0 (ingest) + Stage 1 (entity resolution) and writes
`output/unified_entities.json`.
"""
from __future__ import annotations

import json
from typing import Any, Optional

from . import config
from .ingest import load_all
from .resolution import ResolutionResult, resolve


def _canonical_name(member_keys: list[str], profiles_by_key: dict) -> str:
    # Prefer the most "operator-facing" systems for the display name.
    priority = ["AM", "APM", "ERP", "DCS", "CMMS", "Historian"]
    by_system = {profiles_by_key[k].system: profiles_by_key[k].name for k in member_keys}
    for system in priority:
        if system in by_system and by_system[system]:
            return _strip_hierarchy_prefix(by_system[system])
    return _strip_hierarchy_prefix(next(iter(by_system.values()), "Unknown"))


def _strip_hierarchy_prefix(name: str) -> str:
    """APM's `display_name` is always a "Facility X > Unit Y > <equipment
    name>" breadcrumb (confirmed: every entry in apm_config.json follows
    this exact template) -- fine as internal provenance, but a poor
    display label ("Facility 1 > Unit 100 > Centrifugal Pump 102" instead
    of just "Centrifugal Pump 102"). This is a deterministic string split,
    not an LLM call: the format is a known, consistent, structured
    template from one specific system, not arbitrary free text that would
    need real language understanding to clean up -- an LLM call here would
    add latency, cost, and a small chance of altering the name's meaning,
    for a problem a plain `str.split()` already solves perfectly and
    reproducibly.
    """
    if " > " in name:
        return name.rsplit(" > ", 1)[-1]
    return name


def build_unified_entities(result: ResolutionResult) -> list[dict[str, Any]]:
    entities = []
    # Sort clusters: larger (more cross-system evidence) first, then alphabetically.
    clusters_sorted = sorted(
        result.clusters, key=lambda members: (-len(members), sorted(members)[0])
    )
    for idx, member_keys in enumerate(clusters_sorted, start=1):
        member_keys_sorted = sorted(member_keys)
        confidence = result.cluster_confidence(member_keys_sorted)
        evidence = sorted(
            {
                reason
                for edge in result.edges
                for reason in edge.reasons
                if edge.a in member_keys_sorted and edge.b in member_keys_sorted
            }
        )
        low_signal_edges = result.low_signal_edges(member_keys_sorted)
        entities.append(
            {
                "unified_id": f"ASSET-{idx:03d}",
                "canonical_name": _canonical_name(member_keys_sorted, result.profiles_by_key),
                "confidence": round(confidence, 3) if confidence is not None else None,
                "system_count": len({result.profiles_by_key[k].system for k in member_keys_sorted}),
                "members": [
                    result.profiles_by_key[k].to_dict() for k in member_keys_sorted
                ],
                "evidence": evidence,
                # True when at least one merge in this cluster relied solely on
                # unit+equipment_number (no confirmed class match, no
                # high-confidence name match, no hard FK) -- these matches
                # can't be reliably distinguished from a coincidental false
                # merge by any threshold/weight tuning, so they're surfaced
                # for human review instead of silently trusted. See
                # `ResolutionResult.low_signal_edges` / repo memory.
                "needs_review": bool(low_signal_edges),
                "review_notes": sorted(
                    {
                        f"{e.a} <-> {e.b}: score {e.score:.3f}, no confirmed class match "
                        "and no high-confidence name match -- verify this is the same "
                        "physical asset"
                        for e in low_signal_edges
                    }
                ),
            }
        )
    return entities


def run() -> list[dict[str, Any]]:
    all_profiles = load_all()

    # Stage 0.5: for any profile classify_asset() couldn't recognize (e.g.
    # a genuinely new equipment type outside the fixed keyword list), ask
    # the configured LLM to propose a free-form class label instead of
    # leaving it unclassified. Skipped entirely if no LLM is configured --
    # `classify_asset()`'s deterministic keyword path is always primary and
    # always available regardless of this optional step.
    from .llm_provider import (
        NullTextGenerationProvider,
        get_similarity_provider,
        get_text_generation_provider,
    )
    from .open_vocab_classify import classify_open_vocabulary

    text_provider = get_text_generation_provider()
    if not isinstance(text_provider, NullTextGenerationProvider):
        flat_profiles = [p for plist in all_profiles.values() for p in plist]
        classify_open_vocabulary(flat_profiles, text_provider, get_similarity_provider())

    result = resolve(all_profiles)
    entities = build_unified_entities(result)

    config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = config.OUTPUT_DIR / "unified_entities.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(entities, f, indent=2)

    return entities


def run_with_graph() -> tuple[list[dict[str, Any]], "KnowledgeGraph"]:
    """Stage 0/1 (entities) + Stage 2/3 groundwork (knowledge graph),
    writing `output/unified_entities.json`, `output/graph.json`, and an
    interactive `output/graph.html` visualization. Always does a full
    rebuild from `Data/*.csv` -- this is the "regenerate everything"
    entry point (used by `run_pipeline.py`/`run_graph.py` and whenever
    source data has changed). For a cache-aware load, see
    `load_or_build()`.

    Process topology (what feeds/cools/supplies what) is derived by the
    configured LLM reading `config.PROCESS_DESCRIPTION_PATH` (see
    `docs/ONTOLOGY.md`), falling back to a hardcoded list if no LLM is
    configured or that file is missing -- never raises either way.
    """
    from .graph import build_graph  # local import avoids a cycle at module load
    from .llm_provider import NullTextGenerationProvider, get_text_generation_provider
    from .viz import write_html

    entities = run()

    text_provider = get_text_generation_provider()
    prose_text = None
    if not isinstance(text_provider, NullTextGenerationProvider):
        try:
            prose_text = config.PROCESS_DESCRIPTION_PATH.read_text(encoding="utf-8")
        except OSError:
            prose_text = None  # no prose available -- build_graph falls back to the hardcoded list

    kg = build_graph(entities, prose_text=prose_text, text_provider=text_provider)

    graph_path = config.OUTPUT_DIR / "graph.json"
    with open(graph_path, "w", encoding="utf-8") as f:
        json.dump(kg.to_node_link_json(), f, indent=2)

    write_html(kg, config.OUTPUT_DIR / "graph.html")

    return entities, kg


def load_from_cache() -> Optional[tuple[list[dict[str, Any]], "KnowledgeGraph"]]:
    """Try to rehydrate (entities, graph) from `output/unified_entities.json`
    + `output/graph.json` without touching `Data/*.csv` or re-running
    resolution at all. Returns None (never raises) if either file is
    missing or malformed -- callers should fall back to `run_with_graph()`
    in that case."""
    from .graph import KnowledgeGraph  # local import avoids a cycle at module load

    entities_path = config.OUTPUT_DIR / "unified_entities.json"
    graph_path = config.OUTPUT_DIR / "graph.json"
    if not entities_path.exists() or not graph_path.exists():
        return None
    try:
        with open(entities_path, encoding="utf-8") as f:
            entities = json.load(f)
        with open(graph_path, encoding="utf-8") as f:
            graph_data = json.load(f)
        kg = KnowledgeGraph.from_node_link_json(graph_data)
    except (json.JSONDecodeError, KeyError, TypeError, OSError):
        return None
    return entities, kg


def load_or_build(force_rebuild: bool = False) -> tuple[list[dict[str, Any]], "KnowledgeGraph"]:
    """Cache-aware entry point for normal startup (`server.py`): loads the
    on-disk cache written by a previous `run_with_graph()` if present and
    valid, otherwise does a full rebuild (which also (re)writes the cache).
    Pass `force_rebuild=True` to always regenerate from `Data/*.csv` (e.g.
    after source data changes) instead of trusting a stale cache."""
    if not force_rebuild:
        cached = load_from_cache()
        if cached is not None:
            return cached
    return run_with_graph()
