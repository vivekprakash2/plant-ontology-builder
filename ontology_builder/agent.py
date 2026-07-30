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
from typing import Any, Iterator, Optional

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
    truncated: bool = False  # model hit its output limit (finish_reason == "length")


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


# The two windows that matter for this plant's signals: fast-moving ones
# (vibration) reveal themselves in ~48h, slow ones (fouling, lube-oil
# temperature) need ~14 days. See `_trend_with_comparison` for why both are
# always reported to the agent.
_SHORT_WINDOW_HOURS = 48
_LONG_WINDOW_HOURS = 336


def _summarize_series(tag: str, series: list[dict[str, Any]], window_hours: int) -> Optional[dict[str, Any]]:
    """Direction/percent-change summary for one already-fetched slice."""
    if len(series) < 2:
        return None
    first, last = float(series[0]["value"]), float(series[-1]["value"])
    delta = last - first
    pct = (delta / abs(first) * 100) if first else 0.0
    return {
        "tag": tag,
        "window_hours": window_hours,
        "first": first,
        "last": last,
        "pct_change": round(pct, 1),
        "direction": "rising" if pct > 3 else ("falling" if pct < -3 else "stable"),
        "n_readings": len(series),
        "start_ts": series[0]["timestamp"],
        "end_ts": series[-1]["timestamp"],
    }


def _trend(tag: str, now: datetime = NOW, window_hours: int = 48) -> Optional[dict[str, Any]]:
    start = (now - timedelta(hours=window_hours)).isoformat().replace("+00:00", "Z")
    end = now.isoformat().replace("+00:00", "Z")
    series = historian_series(tag, start=start, end=end)
    summary = _summarize_series(tag, series, window_hours)
    if summary is None:
        return None
    # Chart-ready downsampled points -- kept out of the LLM's tool-call
    # results (see _tool_get_historian_trend) to avoid bloating the
    # prompt, but used by build_ui_panel() for the frontend trend chart.
    summary["points"] = _downsample_points(series)
    return summary


def _trend_with_comparison(
    tag: str, now: datetime = NOW, window_hours: int = _SHORT_WINDOW_HOURS
) -> Optional[dict[str, Any]]:
    """The requested window PLUS the complementary one, from a single pass
    over the historian file.

    Why both: a retest measured the agent's S4 answer flipping purely on which
    window it happened to ask for. K-101's lube-oil temperature is
    `stable -0.3%` over 48h but `rising +32.1%` over 336h -- it climbed
    54 -> 72 degC over two weeks and then plateaued. Both readings are true.
    Every run that asked for 48h concluded the cooling valve CV-400 was "not
    implicated" (one of them said so having never even checked it); every run
    that asked for 336h named CV-400 correctly. The prompt already asked for a
    long window on slow signals and was followed only half the time, so this
    removes the choice instead of restating the instruction: whichever window
    is requested, the other comes back alongside it, plus an explicit note when
    the two disagree.

    The requested window's fields stay at the TOP LEVEL so every existing
    consumer (chart building in `build_ui_panel`, `_describe_record`,
    `_flatten_evidence`) keeps working unchanged.
    """
    windows = sorted({int(window_hours), _SHORT_WINDOW_HOURS, _LONG_WINDOW_HOURS})
    widest = max(windows)
    end_dt = now
    end = end_dt.isoformat().replace("+00:00", "Z")
    # One scan of the ~518k-row file for the widest window; the narrower ones
    # are sliced out of it in memory.
    series = historian_series(tag, start=(now - timedelta(hours=widest)).isoformat().replace("+00:00", "Z"), end=end)
    if len(series) < 2:
        return None

    def slice_for(hours: int) -> list[dict[str, Any]]:
        if hours >= widest:
            return series
        cutoff = now - timedelta(hours=hours)
        return [r for r in series if _parse_ts(r["timestamp"]) >= cutoff]

    requested = _summarize_series(tag, slice_for(int(window_hours)), int(window_hours))
    if requested is None:
        return None
    requested["points"] = _downsample_points(slice_for(int(window_hours)))

    others = []
    for hours in windows:
        if hours == int(window_hours):
            continue
        summary = _summarize_series(tag, slice_for(hours), hours)
        if summary:
            summary.pop("tag", None)
            others.append(summary)
    if others:
        requested["other_windows"] = others
        disagreeing = [o for o in others if o["direction"] != requested["direction"]]
        if disagreeing:
            longest = max(others, key=lambda o: o["window_hours"])
            requested["trend_note"] = (
                f"WINDOW MATTERS for this tag: over {requested['window_hours']}h it reads "
                f"'{requested['direction']}' ({requested['pct_change']:+.1f}%), but over "
                f"{longest['window_hours']}h it reads '{longest['direction']}' "
                f"({longest['pct_change']:+.1f}%). A signal that rose and then plateaued looks "
                "flat in a short window. Judge slow-moving signals (lube-oil temperature, "
                "exchanger fouling, bearing temperature) on the LONGER window before concluding "
                "that nothing is wrong."
            )
    return requested


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

def _causal_factors(entities, kg, unified_id, now: datetime = NOW) -> list[dict[str, Any]]:
    """Evidence-backed candidate causes for one asset's mechanical symptoms
    (vibration / overheating / degradation). Each factor is only emitted when
    the supporting records actually exist in the graph -- never asserted from
    a scenario script -- so the same function is correct for P-101 (setpoint +
    seal), K-101 (upstream cooling-valve problem overheating the lube oil),
    P-102 (falling suction pressure), or any future asset with none of these.

    Returns [{kind, sentence, evidence: [...], recommendation}, ...].
    """
    context = kg.context_for_asset(unified_id)
    factors: list[dict[str, Any]] = []

    # 1. Recent operator setpoint INCREASE on the asset's own loop (last 7d) --
    # pushing equipment past its design point is a classic self-inflicted cause.
    recent_sps = [
        op for op in context.get("OperatorAction", [])
        if op.get("action_type") == "SP_CHANGE" and (now - _parse_ts(op["timestamp"])).days < 7
    ]
    latest_sp = max(recent_sps, key=lambda r: r["timestamp"]) if recent_sps else None
    if latest_sp:
        old, new = float(latest_sp["old_value"]), float(latest_sp["new_value"])
        pct = (new - old) / old * 100 if old else 0
        if pct > 0:
            factors.append({
                "kind": "setpoint_increase",
                "sentence": (
                    f"the operator raised the flow setpoint {old:g} -> {new:g} ({pct:+.0f}%) "
                    f"{_ago(latest_sp['timestamp'], now)} (shift {latest_sp['shift']}, "
                    f"{latest_sp['operator']}, action {latest_sp['id']})"
                ),
                "evidence": [{"type": "operator_action", **latest_sp}],
                "recommendation": "- Reduce the flow setpoint toward the prior value.",
            })

    # 2. Recent seal/mechanical work on the asset ITSELF (last 14d) -- a fresh
    # repair that shifted the baseline (e.g. possible misalignment).
    work_orders = sorted(context.get("WorkOrder", []), key=lambda r: r["created"], reverse=True)
    seal_wo = next(
        (
            wo for wo in work_orders
            if "seal" in ((wo.get("notes") or "") + " " + (wo.get("parts_used") or "")).lower()
            and (now - _parse_ts(wo["created"])).days <= 14
        ),
        None,
    )
    if seal_wo:
        factors.append({
            "kind": "recent_seal_work",
            "sentence": (
                f"a mechanical seal was replaced {_ago(seal_wo['created'], now)} "
                f"(work order {seal_wo['id']}); notes: \"{seal_wo['notes']}\""
            ),
            "evidence": [{"type": "work_order", **seal_wo}],
            "recommendation": (
                f"- Inspect and complete the seal alignment recheck flagged on work order {seal_wo['id']}."
            ),
        })

    # 3. Cooling/utility problem UPSTREAM: an asset that COOLS or
    # SUPPLIES_UTILITY to this one has a non-Closed work order -- corroborated
    # by this asset's own lube-oil temperature trend when it has one.
    for rel_type in ("COOLS", "SUPPLIES_UTILITY"):
        for up_id in _upstream_neighbors(kg, unified_id, rel_type):
            up_context = kg.context_for_asset(up_id)
            open_wos = [wo for wo in up_context.get("WorkOrder", []) if wo.get("status") != "Closed"]
            if not open_wos:
                continue
            wo = max(open_wos, key=lambda r: r["created"])
            up_name = _asset_name(entities, up_id)
            evidence = [{"type": "work_order", **wo}]
            sentence = (
                f"{up_name} (which {rel_type.replace('_', ' ').lower()} this asset) has an "
                f"{wo['status'].lower()} work order ({wo['id']}): \"{wo['notes']}\""
            )
            lube_tag = _find_tag(context, "LUBE_01")
            lube_trend = _trend(lube_tag, window_hours=24 * 14) if lube_tag else None
            if lube_trend and lube_trend["direction"] == "rising":
                evidence.append({"type": "historian_trend", **lube_trend})
                sentence += (
                    f"; the asset's own lube-oil temperature is rising "
                    f"({lube_trend['pct_change']:+.1f}%), consistent with lost cooling"
                )
            factors.append({
                "kind": "utility_cooling_problem",
                "sentence": sentence,
                "evidence": evidence,
                "recommendation": (
                    f"- Expedite the {up_name} corrective work order ({wo['id']}) and verify "
                    "cooling/utility supply is restored."
                ),
            })

    # 4. Cavitation signature: the asset's OWN suction pressure falling.
    psuc_tag = _find_tag(context, "PSUC_01") or _find_tag(context, "PSUC_02")
    psuc_trend = _trend(psuc_tag, window_hours=24 * 14) if psuc_tag else None
    if psuc_trend and psuc_trend["direction"] == "falling":
        factors.append({
            "kind": "cavitation_risk",
            "sentence": (
                f"its suction pressure is falling ({psuc_trend['pct_change']:+.1f}%) -- "
                "possible cavitation"
            ),
            "evidence": [{"type": "historian_trend", **psuc_trend}],
            "recommendation": "- Check the suction strainer and verify NPSH margin (cavitation risk).",
        })

    return factors


def _answer_vibration(entities, kg, unified_id) -> AgentAnswer:
    name = _asset_name(entities, unified_id)
    context = kg.context_for_asset(unified_id)
    evidence = []

    vib_tag = _find_tag(context, "VIB_01") or _find_tag(context, "VIB_02")
    vib_trend = _trend(vib_tag) if vib_tag else None
    if vib_trend:
        evidence.append({"type": "historian_trend", **vib_trend})

    factors = _causal_factors(entities, kg, unified_id)
    for f in factors:
        evidence.extend(f["evidence"])

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

    recommendation = None
    headline = f"No clear cause found for {name}'s vibration"
    if factors:
        lines.append(
            "Likely contributing factors, most recent first: "
            + "; and ".join(f["sentence"] for f in factors) + "."
        )
        kinds = {f["kind"] for f in factors}
        if {"setpoint_increase", "recent_seal_work"} <= kinds:
            lines.append(
                "Most likely root cause: the recent setpoint increase is pushing the pump above its "
                "efficiency curve, compounding a recent seal job that was flagged for alignment recheck."
            )
            headline = "Setpoint hike + seal misalignment driving vibration"
        elif "utility_cooling_problem" in kinds:
            lines.append(
                "Most likely root cause: lube-oil overheating from the upstream cooling/utility "
                "problem -- a utility issue, not a mechanical fault in the machine itself."
            )
            headline = "Lube-oil overheating from upstream cooling problem"
        elif "setpoint_increase" in kinds:
            headline = "Recent setpoint increase driving vibration"
        elif "recent_seal_work" in kinds:
            headline = "Recent seal work may be driving vibration"
        elif "cavitation_risk" in kinds:
            headline = "Falling suction pressure -- possible cavitation"
        recommendation = "\n".join(f["recommendation"] for f in factors)
        confidence = "medium-high"
    else:
        lines.append(
            "No recent setpoint changes, seal-related work orders, upstream utility problems, or "
            "cavitation signatures were found for this asset."
        )
        confidence = "low"

    return AgentAnswer(
        name, "vibration", " ".join(lines), confidence, evidence,
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
        # Headline + recommendations reflect only the causes actually found in
        # the graph -- never assert the full scripted S3 chain if half of it
        # (e.g. the reflux-pump edge) isn't present in this build's topology.
        fouling_found = any("cold feed" in c for c in causes)
        cavitation_found = any("cavitation" in c for c in causes)
        rec_parts = []
        if fouling_found:
            rec_parts.append(
                "- Address the upstream fouling (see the linked exchanger's work order) to restore feed temperature."
            )
        if cavitation_found:
            rec_parts.append("- Inspect the reflux pump for cavitation if suction pressure continues to fall.")
        recommendation = "\n".join(rec_parts) or None
        if fouling_found and cavitation_found:
            headline = "Cold feed + reflux cavitation raising column dP"
        elif fouling_found:
            headline = "Cold feed from upstream fouling raising column dP"
        else:
            headline = "Reflux pump cavitation raising column dP"
    else:
        confidence = "low"
    return AgentAnswer(
        name, "high_dp", " ".join(lines), confidence, evidence,
        asset_id=unified_id, recommendation=recommendation, headline=headline,
    )


# Short human phrases for factor kinds, used when summarizing a comparison.
_FACTOR_PHRASE = {
    "setpoint_increase": "a recent operator setpoint increase",
    "recent_seal_work": "recent seal work",
    "utility_cooling_problem": "an upstream cooling/utility problem",
    "cavitation_risk": "a falling-suction-pressure (cavitation) signature",
}


def _answer_same_problem(entities, kg, a_id, b_id) -> AgentAnswer:
    """Generic 'is A experiencing the same problem as B?' comparison: gather
    each asset's evidence-backed causal factors independently, then compare
    the factor KINDS. The verdict comes entirely from what the evidence
    shows for each asset -- there is no scripted per-scenario conclusion, so
    this is correct for K-101-vs-P-101 (S4's distractor), P-102-vs-P-101, or
    any other pairing a judge improvises."""
    a_name, b_name = _asset_name(entities, a_id), _asset_name(entities, b_id)
    evidence: list[dict[str, Any]] = []
    lines: list[str] = []

    per_asset: dict[str, dict[str, Any]] = {}
    for asset_id, asset_name in ((a_id, a_name), (b_id, b_name)):
        context = kg.context_for_asset(asset_id)
        vib_tag = _find_tag(context, "VIB_01") or _find_tag(context, "VIB_02")
        vib_trend = _trend(vib_tag) if vib_tag else None
        if vib_trend:
            evidence.append({"type": "historian_trend", **vib_trend})
            lines.append(
                f"{asset_name} vibration: {vib_trend['direction']} ({vib_trend['pct_change']:+.1f}%)."
            )
        factors = _causal_factors(entities, kg, asset_id)
        for f in factors:
            evidence.extend(f["evidence"])
        if factors:
            lines.append(
                f"{asset_name}'s likely cause: " + "; and ".join(f["sentence"] for f in factors) + "."
            )
        else:
            lines.append(f"No clear causal evidence was found for {asset_name}.")
        per_asset[asset_id] = {"name": asset_name, "factors": factors}

    a_kinds = {f["kind"] for f in per_asset[a_id]["factors"]}
    b_kinds = {f["kind"] for f in per_asset[b_id]["factors"]}
    shared = a_kinds & b_kinds

    if a_kinds and b_kinds and not shared:
        a_phrases = " + ".join(_FACTOR_PHRASE.get(k, k) for k in sorted(a_kinds))
        b_phrases = " + ".join(_FACTOR_PHRASE.get(k, k) for k in sorted(b_kinds))
        verdict = (
            f"No -- the symptoms look similar, but the evidence points to different root causes: "
            f"{a_name} shows {a_phrases}, while {b_name} shows {b_phrases}."
        )
        headline = f"No -- {a_name}'s cause differs from {b_name}'s"
        confidence = "medium-high"
    elif shared:
        shared_phrases = " + ".join(_FACTOR_PHRASE.get(k, k) for k in sorted(shared))
        verdict = f"Partly -- both assets show {shared_phrases}, so the problems may be related."
        headline = f"{a_name} and {b_name} may share a cause"
        confidence = "medium"
    else:
        missing = [info["name"] for info in per_asset.values() if not info["factors"]]
        verdict = (
            "Inconclusive -- no clear causal evidence was found for "
            + " or ".join(missing) + ", so the two problems can't be confidently compared."
        )
        headline = "Not enough evidence to compare the two problems"
        confidence = "low"
    lines.insert(0, verdict)

    recommendations = []
    for info in per_asset.values():
        for f in info["factors"]:
            if f["recommendation"] not in recommendations:
                recommendations.append(f["recommendation"])

    return AgentAnswer(
        a_name, "same_problem_comparison", " ".join(lines), confidence, evidence,
        asset_id=a_id, recommendation="\n".join(recommendations) or None, headline=headline,
    )


# Minimum alarm transitions before a "so many alarms" question is treated as
# an alarm FLOOD (S5's config-chatter pattern) rather than a handful of
# genuine process alarms. TK-201's planted flood has 120 transitions; every
# real process alarm in the data has ~3 -- the gate just has to sit between.
_ALARM_FLOOD_MIN_TRANSITIONS = 15


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

    # --- Gate: only assert the S5 "nuisance / mis-set limit" conclusion when
    # the evidence actually shows a flood (many rapid transitions). A few
    # genuine alarms (e.g. P-101's 3 real vibration-high alarms) must NOT get
    # the nuisance verdict -- instead describe them honestly and, if they're
    # vibration alarms, hand off to the vibration root-cause analysis.
    if len(alarms) < _ALARM_FLOOD_MIN_TRANSITIONS:
        limit_h = cfg.get("limit_h") if cfg else None
        unit = (cfg.get("eng_unit") or "") if cfg else ""
        intro = (
            f"{name} has only {len(alarms)} alarm transition{'s' if len(alarms) != 1 else ''} "
            f"({active_count} ACTIVE) -- not an alarm flood."
        )
        if values and limit_h is not None and max(values) > float(limit_h):
            over_pct = (max(values) - float(limit_h)) / float(limit_h) * 100
            intro += (
                f" The values reach {max(values):.1f}{unit}, {over_pct:.0f}% past the configured "
                f"H limit of {limit_h}{unit} -- a real excursion, not threshold chatter."
            )
        if any("VIB" in (a.get("alarm_point") or "") for a in alarms):
            # These are genuine vibration alarms -- the real question is what's
            # driving the vibration, so answer that.
            vib = _answer_vibration(entities, kg, unified_id)
            vib.answer = f"{intro} {vib.answer}"
            vib.evidence = evidence + vib.evidence
            return vib
        return AgentAnswer(
            name, "genuine_alarms",
            intro + " These look like genuine process alarms worth investigating individually.",
            "medium", evidence, asset_id=unified_id,
            recommendation="- Investigate the underlying process signal rather than the alarm configuration.",
            headline=f"{name}'s alarms look genuine, not config noise",
        )

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
    # Tag each record with the type key `_describe_record()` dispatches on --
    # NOT the raw graph node label. Using the raw label ("AlarmEvent" instead
    # of "alarm_event") meant every single record was silently dropped from
    # the UI panel's timeline/evidence AND from the graph walk, so the one
    # question whose entire point is "here is everything we unified across
    # six systems" rendered as a bare sentence of counts with a 2-node walk.
    # Neighbouring Asset nodes are skipped: they aren't records, and they
    # already appear in the panel's `relationships` list.
    evidence = [
        {**rec, "type": _CONTEXT_LABEL_TO_TYPE[label]}
        for label, records in context.items()
        if label in _CONTEXT_LABEL_TO_TYPE
        for rec in records
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
    "0. Before doing anything else, call write_plan with your initial step-by-step investigation "
    "plan (3-6 short steps covering what you intend to check and why). Call write_plan again "
    "whenever a step's status changes (mark it 'in_progress' as you start it, 'done' once its "
    "finding is in hand) or your plan needs to change based on what you find -- this is shown "
    "live to the user as a checklist, so keep it accurate and current.\n"
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
    "4a. CRITICAL: get_related_assets only tells you a neighbour EXISTS -- that is not evidence "
    "about it. If a related asset could plausibly explain the symptom (especially a COOLS or "
    "SUPPLIES_UTILITY neighbour when something is overheating, or an upstream FEEDS neighbour "
    "when a downstream unit is working harder), you MUST call get_asset_context on that "
    "neighbour and get_historian_trend on its relevant tag before concluding. Never report a "
    "cause as 'unconfirmed' or 'no supporting evidence' while a named related asset is still "
    "unchecked -- go and check it. When the question compares two assets, gather the same depth "
    "of evidence for BOTH, not just the one you looked at first.\n"
    "4b. list_assets and get_asset_context both return each asset's own 'monitored_params' (or "
    "'asset_monitored_params') when APM covers it -- e.g. a pump's own suction pressure. If one of "
    "an asset's own monitored parameters is trending abnormally, treat that as a legitimate "
    "candidate root cause FOR THAT ASSET in its own right, not automatically just a downstream "
    "symptom of some other cause -- weigh it against the alternative explanations using the actual "
    "evidence, don't dismiss it by default just because another plausible cause is also present.\n"
    "5. get_historian_trend ALWAYS returns both a 48-hour and a 336-hour (14-day) view -- the "
    "window you asked for at the top level, the other under 'other_windows'. Read both before "
    "concluding. When they disagree the result includes a 'trend_note': a signal that rose and "
    "then plateaued reads 'stable' over 48h while still sitting at a badly elevated level, so a "
    "short-window 'stable' is NOT evidence that a slow-moving signal (lube-oil temperature, "
    "exchanger fouling, bearing temperature) is healthy. Never dismiss a suspected cause on a "
    "short-window reading alone.\n"
    "6. If the evidence doesn't support a confident conclusion, say so explicitly rather than "
    "guessing.\n"
    "6b. The conversation may include earlier question/answer turns. Use them to resolve "
    "follow-up references ('it', 'that pump', 'the work order you mentioned') to the right asset "
    "or record -- but treat earlier answers as conversational context only, NOT as evidence: "
    "re-verify any fact you rely on with the tools before citing it.\n"
    "7. Structure your final answer in Markdown using these exact headings and no others, in "
    "this order. Match the sections to what was actually ASKED:\n"
    "   '## Headline' -- ALWAYS required. ONE short, punchy phrase (max 12 words, no trailing "
    "period), e.g. 'Setpoint hike plus seal misalignment driving vibration'. This is shown as a "
    "large bold title in the UI, so it must be brief -- not a full sentence.\n"
    "   '## Root Cause' (for diagnostic questions -- 'why is X happening', 'what caused Y', "
    "'is A the same problem as B') OR '## Summary' (for informational/lookup questions -- 'show "
    "me everything about X', 'what's alarming right now', 'what maintenance happened last week') "
    "-- pick whichever actually fits; use exactly one of the two. A concise 3-5 sentence "
    "analysis. Be economical: cite the key evidence, don't restate every tool result.\n"
    "   '## Recommended Actions' -- 2-4 short Markdown bullet points, grounded only in the "
    "evidence you found. Include this ONLY when the question is diagnostic, or when the evidence "
    "genuinely calls for operator action. OMIT this section entirely for pure lookup/status "
    "questions -- do not invent advice just to fill it in.\n"
    "8. IMPORTANT: You have a limited output budget. If you are running long, shorten the "
    "analysis section rather than truncating a section mid-sentence. When "
    "'## Recommended Actions' applies, always finish it."
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
                "Get the direction and percent change of a historian tag's value (never returns "
                "raw rows -- only computed trend summaries). ALWAYS returns BOTH a short (48h) "
                "and long (336h/14-day) view: the window you request at the top level, and the "
                "other under 'other_windows'. If they disagree a 'trend_note' explains which to "
                "trust -- a signal that rose then plateaued reads 'stable' over 48h while still "
                "being badly elevated, so check the long window before concluding nothing is wrong."
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
    {
        "type": "function",
        "function": {
            "name": "write_plan",
            "description": (
                "Write or update your step-by-step investigation plan, shown live to the user as a "
                "checklist. Call this FIRST, before any other tool, with your initial plan (3-6 "
                "short steps). Call it again any time a step's status changes (mark 'in_progress' "
                "while working on it, 'done' once its finding is in hand) or your plan changes based "
                "on what you find."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "steps": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "text": {
                                    "type": "string",
                                    "description": "Short description of this step, e.g. 'Check P-101 vibration trend'.",
                                },
                                "status": {
                                    "type": "string",
                                    "enum": ["pending", "in_progress", "done"],
                                },
                            },
                            "required": ["text", "status"],
                            "additionalProperties": False,
                        },
                    }
                },
                "required": ["steps"],
                "additionalProperties": False,
            },
        },
    },
]

# Output budget per turn. Tool-call-only turns emit almost nothing regardless,
# so this is really the budget for the FINAL answer. Raised 1000 -> 1600 -> 2400
# after a live run still cut a Recommended Actions bullet off mid-word; a
# `finish_reason == "length"` response now also gets one retry at
# _ANSWER_RETRY_MAX_TOKENS before the answer is flagged `truncated`.
_ANSWER_MAX_TOKENS = 2400
_ANSWER_RETRY_MAX_TOKENS = 3600

_MAX_AGENT_TURNS = 14
# Raised from 7 to 12 after live-testing found a genuine 2-hop question (Column
# dP <- H-101 <- E-101 fouling -- the exact S3 cascade from SCENARIO.md) hitting
# "inconclusive within the tool-call budget" despite the model having already
# gathered every piece of correct evidence (dP +121%, fuel flow +8.7%, suction
# pressure -11.8%). Root cause: `write_plan` update calls each consume a full
# turn (one `provider.chat()` round trip) just like a real tool call, and a
# multi-hop question needs genuinely SEQUENTIAL tool calls (can't look up
# H-101's own upstream neighbor E-101 until get_related_assets(H-101) has
# already returned) -- those can't be batched into fewer turns the way
# independent lookups can. 7 was sized before `write_plan` existed; 12 gives
# enough headroom for plan-update turns + a real 2-3 hop causal chain without
# materially hurting latency (each turn's cost is dominated by the model's own
# response time, not the turn-loop overhead). Nudged 12 -> 14 alongside prompt
# rule 4a, which asks the model to pull a related asset's OWN evidence rather
# than stopping at "the neighbour exists" -- that's 1-2 extra sequential calls
# on comparison/cascade questions.

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
            # Which of this asset's own signals APM recognizes as a health/
            # failure-monitoring parameter for it specifically (e.g. a pump's
            # own suction pressure) -- None if APM doesn't cover this asset.
            "monitored_params": kg.nodes[e["unified_id"]].properties.get("monitored_params")
            if e["unified_id"] in kg.nodes
            else None,
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
    return {
        "reference_time": NOW.isoformat(),
        "asset_monitored_params": kg.nodes[unified_id].properties.get("monitored_params"),
        "records": annotated,
    }


def _tool_get_historian_trend(entities: list[dict[str, Any]], kg: KnowledgeGraph, args: dict) -> Any:
    tag = args.get("tag", "")
    window_hours = args.get("window_hours", 48)
    try:
        window_hours = int(window_hours)
    except (TypeError, ValueError):
        window_hours = 48
    trend = _trend_with_comparison(tag, window_hours=window_hours)
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


# --------------------------------------------------------------------------
# Completion gate -- a DETERMINISTIC refusal to accept an answer that makes a
# claim about a related asset the agent never actually examined.
#
# Prompt rule 4a asks for this behaviour and is only advisory. A live retest
# measured it holding 3 times out of 4: in the fourth run the agent asked
# "is K-101 the same problem as P-101?", called get_related_assets(K-101), saw
# cooling valve CV-400 listed, never queried CV-400, and then asserted
# "its cooling-water supplier CV-400 is not implicated" -- which contradicts
# the data (CV-400 has an OPEN corrective work order for exactly that fault).
# The gate turns that from a prompt the model may ignore into a turn it cannot
# finish on, borrowing the idea from a sibling project's investigation
# checklist. See docs/CHAT_AGENT.md Sec 5.
# --------------------------------------------------------------------------

# Phrases that mean "I could not establish a cause". Combined with an
# unexamined neighbour, these are the second thing worth blocking: declaring
# defeat while a named lead is still unchecked.
_INCONCLUSIVE_MARKERS = (
    "unconfirmed",
    "not implicated",
    "no supporting evidence",
    "unexplained",
    "cannot be determined",
    "remains unclear",
    "unresolved",
)


def _neighbours_discovered(trace: list[dict[str, Any]]) -> dict[str, str]:
    """{unified_id: canonical_name} for every asset `get_related_assets` surfaced."""
    found: dict[str, str] = {}
    for call in trace:
        if call.get("tool") != "get_related_assets":
            continue
        result = call.get("result")
        if not isinstance(result, list):
            continue
        for row in result:
            if isinstance(row, dict) and row.get("unified_id"):
                found[row["unified_id"]] = row.get("canonical_name") or row["unified_id"]
    return found


def _assets_examined(trace: list[dict[str, Any]], kg: KnowledgeGraph) -> set[str]:
    """Assets the agent actually pulled evidence for -- directly via
    `get_asset_context`, via a trend on one of their tags, or as the owner of a
    `search_evidence` hit."""
    seen: set[str] = set()
    for call in trace:
        tool = call.get("tool")
        args = call.get("arguments") or {}
        if tool == "get_asset_context" and args.get("unified_id"):
            seen.add(args["unified_id"])
        elif tool == "get_historian_trend" and args.get("tag"):
            asset_id, _ = _asset_for_record(kg, args["tag"])
            if asset_id:
                seen.add(asset_id)
        elif tool == "search_evidence":
            result = call.get("result")
            if isinstance(result, dict):
                for row in result.get("results", []):
                    if isinstance(row, dict) and row.get("asset_id"):
                        seen.add(row["asset_id"])
    return seen


def _asset_mention_terms(entities: list[dict[str, Any]], unified_id: str) -> set[str]:
    """Strings an answer might use to refer to one asset: its unified_id,
    canonical name, per-system local ids, and curated aliases.

    The alias keywords are filtered to CODE-SHAPED ones only (they contain a
    digit, e.g. "cv-400"). This matters: the plain words in that table --
    "valve", "tank", "compressor" -- would match almost any refinery answer and
    make the gate fire constantly. It also matters in the other direction: the
    answer that triggered this work said "CV-400", which is neither the
    canonical name ("Boiler Feed Flow Control Valve") nor any local_id, so
    matching on those alone would have missed it entirely.
    """
    terms = {unified_id.lower()}
    entity = next((e for e in entities if e["unified_id"] == unified_id), None)
    if entity:
        if entity.get("canonical_name"):
            terms.add(entity["canonical_name"].lower())
        for member in entity.get("members", []):
            if member.get("local_id"):
                terms.add(str(member["local_id"]).lower())
    for alias in _ALIASES:
        if _anchor_to_unified_id(entities, alias["anchor"]) != unified_id:
            continue
        terms.add(alias["name"].lower())
        terms.update(k.lower() for k in alias["keywords"] if any(c.isdigit() for c in k))
    return {t for t in terms if len(t) >= 4}


def _unexamined_neighbour_claim(
    content: str, trace: list[dict[str, Any]], entities: list[dict[str, Any]], kg: KnowledgeGraph
) -> list[str]:
    """Names of related assets this answer talks about (or gives up in front of)
    without ever having queried them. Empty list = nothing to block on."""
    discovered = _neighbours_discovered(trace)
    if not discovered:
        return []  # no neighbour was ever surfaced -- nothing to check
    examined = _assets_examined(trace, kg)
    unchecked = {aid: name for aid, name in discovered.items() if aid not in examined}
    if not unchecked:
        return []

    lower = content.lower()
    named = [
        f"{name} ({aid})"
        for aid, name in sorted(unchecked.items())
        if any(term in lower for term in _asset_mention_terms(entities, aid))
    ]
    if named:
        return named
    # Nothing named, but the answer admits defeat while a lead is open.
    if any(marker in lower for marker in _INCONCLUSIVE_MARKERS):
        return [f"{name} ({aid})" for aid, name in sorted(unchecked.items())]
    return []


_HEADLINE_HEADING_RE = re.compile(r"^#{1,3}\s*Headline\s*$", re.IGNORECASE | re.MULTILINE)
_RECOMMENDED_ACTIONS_HEADING_RE = re.compile(r"^#{1,3}\s*Recommended Actions?\s*$", re.IGNORECASE | re.MULTILINE)
# The body heading is "Root Cause" for diagnostic questions and "Summary" for
# informational ones (see rule 7) -- accept either, plus "Analysis" as a
# tolerated near-miss, since the format is requested but never enforced.
_ROOT_CAUSE_HEADING_RE = re.compile(
    r"^#{1,3}\s*(?:Root Cause|Summary|Analysis)\s*$", re.IGNORECASE | re.MULTILINE
)


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


# Phrases the model sometimes uses to clear/dismiss an asset despite having
# been handed a `trend_note` (see `_trend_with_comparison`) explicitly
# warning it not to. A retest measured this happening even though the
# warning text and _AGENT_SYSTEM_PROMPT rule 5 both already say "never
# dismiss a suspected cause on a short-window reading alone" -- the model
# received the exact right evidence and wrote past it anyway on a fraction
# of runs. `_find_dismissed_trend_warnings` below is a deterministic
# backstop for that residual instruction-following miss, in the same spirit
# as the finish_reason == "length" retry: don't just repeat the instruction
# harder, catch the specific failure and force one corrective turn.
_DISMISSAL_PHRASES_RE = re.compile(
    r"not implicated|ruled out|rules? out|no(?:t)? (?:supporting )?evidence|"
    r"isn'?t the cause|not the (?:cause|driver)|confirmed healthy|cooling (?:is |confirmed )?healthy|"
    r"do not chase|don'?t chase|nothing (?:is |looks )?wrong|unconfirmed",
    re.IGNORECASE,
)


def _tag_owner_aliases(kg: KnowledgeGraph, entities: list[dict[str, Any]], tag: str) -> list[str]:
    """Every name a final answer might plausibly use for the asset that owns
    a historian tag (via its HAS_HISTORIAN_TAG edge): the unified
    canonical_name, every per-system member's own name/local_id, AND any
    short equipment code (e.g. "CV-400", "P-101") embedded in one of those
    strings -- the plant's informal codes often only show up parenthetically
    in a system's own label (e.g. the Historian profile name "Cooling Water
    Flow (CV-400)"), never as a clean alias field, but that's exactly the
    name the model uses when writing about the asset in prose."""
    unified_id = next((e.source for e in kg.edges if e.rel_type == "HAS_HISTORIAN_TAG" and e.target == tag), None)
    if not unified_id:
        return []
    entity = next((e for e in entities if e["unified_id"] == unified_id), None)
    if not entity:
        return []
    names = {entity["canonical_name"]}
    for member in entity.get("members", []):
        if member.get("name"):
            names.add(member["name"])
        if member.get("local_id"):
            names.add(member["local_id"])
    for name in list(names):
        names.update(_EQUIPMENT_CODE_RE.findall(name))
    return sorted(names)


_EQUIPMENT_CODE_RE = re.compile(r"\b[A-Z]{1,4}-\d{2,4}\b")


def _find_dismissed_trend_warnings(
    trace: list[dict[str, Any]], kg: KnowledgeGraph, entities: list[dict[str, Any]], content: str
) -> list[str]:
    """The `trend_note` text for any historian trend this run fetched that
    (a) disagreed between its short- and long-window readings and (b) whose
    owning asset the final answer appears to clear/dismiss with language
    like "not implicated" or "ruled out". Empty if nothing looks
    contradicted. Deliberately conservative -- requires both the asset's
    name (or one of its per-system aliases/equipment codes) AND dismissive
    language to appear in the answer, so it only fires on the narrow case
    this exists for, not on every mention of a slow-moving tag."""
    warnings = []
    content_lower = content.lower()
    for call in trace:
        if call.get("tool") != "get_historian_trend":
            continue
        result = call.get("result")
        if not isinstance(result, dict) or "trend_note" not in result:
            continue
        aliases = _tag_owner_aliases(kg, entities, call.get("arguments", {}).get("tag", ""))
        if any(alias.lower() in content_lower for alias in aliases) and _DISMISSAL_PHRASES_RE.search(content):
            owner = aliases[0] if aliases else call.get("arguments", {}).get("tag", "")
            warnings.append(f"{owner} ({call['arguments'].get('tag')}): {result['trend_note']}")
    return warnings


_VALID_PLAN_STATUSES = {"pending", "in_progress", "done"}


def _normalize_plan_steps(raw: Any) -> list[dict[str, str]]:
    """Validate/normalize the LLM's write_plan(steps=[...]) argument into a
    safe [{"text": str, "status": "pending"|"in_progress"|"done"}, ...]
    list -- never raises, drops anything malformed rather than crashing the
    stream over a bad plan update."""
    if not isinstance(raw, list):
        return []
    steps = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text", "")).strip()
        if not text:
            continue
        status = str(item.get("status", "pending")).strip().lower()
        if status not in _VALID_PLAN_STATUSES:
            status = "pending"
        steps.append({"text": text, "status": status})
    return steps


def _tool_call_label(entities: list[dict[str, Any]], tool: str, args: dict[str, Any]) -> str:
    """Human-readable "doing X" label shown the moment a tool call starts
    (before its result is known) -- e.g. for a live "thinking..." feed.
    Deliberately simpler/faster than `_walk_step_for_tool_call` (which
    needs the RESULT to know which graph nodes were touched, only
    available after execution)."""
    if tool == "list_assets":
        return "Listing known assets"
    if tool == "get_asset_context":
        return f"Looking up context for {_asset_name(entities, args.get('unified_id', ''))}"
    if tool == "get_historian_trend":
        return f"Checking the {args.get('tag', '?')} historian trend"
    if tool == "get_related_assets":
        return f"Checking process-flow relationships for {_asset_name(entities, args.get('unified_id', ''))}"
    if tool == "search_evidence":
        return f'Searching evidence for "{args.get("query", "")}"'
    if tool == "get_plant_status_summary":
        return "Checking plant-wide alarm & work-order status"
    return f"Calling {tool}"


# Conversation-history bounds for the agentic loop. Kept deliberately small:
# earlier turns are context for resolving follow-up references, not a
# transcript to re-reason over, and every prior character competes with the
# per-turn tool-result token budget.
_MAX_HISTORY_TURNS = 4
_MAX_HISTORY_CHARS = 2000


def _history_messages(history: Any) -> list[dict[str, str]]:
    """Validate/normalize client-supplied conversation history (a list of
    {"question": str, "answer": str} dicts, oldest first) into alternating
    user/assistant chat messages for the agentic loop. Hostile or malformed
    input degrades to fewer/no messages -- never raises. Only the most
    recent `_MAX_HISTORY_TURNS` turns are kept, each side truncated to
    `_MAX_HISTORY_CHARS` characters."""
    if not isinstance(history, list):
        return []
    messages: list[dict[str, str]] = []
    for turn in history[-_MAX_HISTORY_TURNS:]:
        if not isinstance(turn, dict):
            continue
        question = str(turn.get("question") or "").strip()
        answer = str(turn.get("answer") or "").strip()
        if not question or not answer:
            continue
        messages.append({"role": "user", "content": question[:_MAX_HISTORY_CHARS]})
        messages.append({"role": "assistant", "content": answer[:_MAX_HISTORY_CHARS]})
    return messages


def _run_agentic_events(
    question: str,
    entities: list[dict[str, Any]],
    kg: KnowledgeGraph,
    provider: Any,
    history: Any = None,
) -> Iterator[dict[str, Any]]:
    """Generator form of the agentic loop -- yields one event per
    meaningful step AS IT HAPPENS (plan updates, a tool call starting, a
    tool call's result), so a caller (see `stream_answer`) can forward them
    to the frontend live instead of only learning about them after the
    entire answer is done. Always ends with exactly one
    `{"type": "final", "answer": AgentAnswer, "plan": [...]}` event.

    Event shapes:
      - `{"type": "plan", "steps": [{"text", "status"}, ...]}` -- from the
        model calling `write_plan`.
      - `{"type": "tool_call", "tool": ..., "arguments": ..., "label": ...}`
        -- emitted right before executing a (non-plan) tool, so the UI can
        show "in progress" the moment the model decides to look something
        up, not just after the lookup finishes.
      - `{"type": "tool_result", "tool": ..., "walk_step": {...} | None}` --
        emitted right after execution; `walk_step` (see
        `_walk_step_for_tool_call`) is the graph node(s) this call touched,
        for live graph-walk highlighting, or None if it touched nothing
        resolvable (e.g. an error result).

    Raises if the provider doesn't support tool calling or the call
    otherwise fails -- same contract as the old `_run_agentic` had; callers
    must catch this and fall back to `_dispatch`. Note this means a caller
    that already forwarded some events to a client before a later turn
    fails will still fall back afterward -- those already-sent events
    can't be un-sent (a real, accepted tradeoff of streaming vs. a single
    atomic batch response; see `docs/CHAT_AGENT.md`).
    """
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": _AGENT_SYSTEM_PROMPT},
        # Prior turns (if any) so follow-ups like "what about its work
        # orders?" resolve -- see _history_messages for bounds/validation.
        *_history_messages(history),
        {"role": "user", "content": question},
    ]
    trace: list[dict[str, Any]] = []
    plan_steps: list[dict[str, str]] = []
    gate_used = False  # the completion gate fires at most once per question

    for _ in range(_MAX_AGENT_TURNS):
        message = provider.chat(messages, tools=TOOL_SCHEMAS, max_tokens=_ANSWER_MAX_TOKENS)
        # Pop before appending: this is our own bookkeeping field, and echoing
        # it back to the API on the next turn could be rejected.
        finish_reason = message.pop("_finish_reason", None)
        messages.append(message)
        tool_calls = message.get("tool_calls") or []
        if not tool_calls:
            content = (message.get("content") or "").strip()

            # Completion gate: refuse an answer that talks about (or gives up in
            # front of) a related asset it never queried. Fires at most once so
            # a stubborn model can't burn the whole turn budget here.
            if not gate_used:
                unexamined = _unexamined_neighbour_claim(content, trace, entities, kg)
                if unexamined:
                    gate_used = True
                    reason = (
                        "Your answer refers to " + ", ".join(unexamined) + ", which "
                        "get_related_assets surfaced but you never queried. A related asset's "
                        "existence is not evidence about it. Call get_asset_context on it, and "
                        "get_historian_trend on its relevant tag (remember you get BOTH a 48h "
                        "and a 336h view -- judge slow signals on the long one), then answer "
                        "again. Do not state or dismiss a cause for an asset you have not "
                        "examined."
                    )
                    messages.append({"role": "user", "content": reason})
                    trace.append(
                        {
                            "type": "tool_call",
                            "tool": "completion_gate",
                            "arguments": {"unexamined": unexamined},
                            "result": {"rejected": True, "reason": reason},
                        }
                    )
                    yield {
                        "type": "gate",
                        "label": "Checking a related asset before concluding",
                        "unexamined": unexamined,
                    }
                    continue

            # One retry at a larger budget if the model was cut off
            # mid-sentence. Truncation used to be silent -- a half-written
            # Recommended Actions bullet rendered as if it were the finished
            # answer (see docs/CHAT_AGENT.md Sec 5).
            if finish_reason == "length":
                retry = provider.chat(messages[:-1], tools=TOOL_SCHEMAS, max_tokens=_ANSWER_RETRY_MAX_TOKENS)
                retry_reason = retry.pop("_finish_reason", None)
                retry_content = (retry.get("content") or "").strip()
                if retry_content and not (retry.get("tool_calls") or []):
                    messages[-1] = retry
                    content = retry_content
                    finish_reason = retry_reason

            # Deterministic backstop for the same class of bug the retry
            # above fixes for truncation, but for contradicted evidence: a
            # retest found the model can be handed an explicit trend_note
            # warning (see _trend_with_comparison) not to clear an asset on
            # a short-window reading, and still write a final answer that
            # does exactly that. One corrective turn, same bounded shape as
            # the truncation retry: hand the model its own warning back
            # verbatim and let it revise; keep the original answer if the
            # model doesn't produce a clean revision (e.g. it calls more
            # tools instead -- we don't re-enter the tool loop here).
            dismissed = _find_dismissed_trend_warnings(trace, kg, entities, content)
            if dismissed:
                nudge = {
                    "role": "user",
                    "content": (
                        "Before finalizing, re-check your draft answer above. It appears to clear or "
                        "dismiss an asset that has an unresolved long-window trend warning:\n- "
                        + "\n- ".join(dismissed)
                        + "\nIf this changes your conclusion, rewrite the full answer (same Markdown "
                        "heading structure as before). If you still believe it's not the cause after "
                        "weighing the long-window reading, keep your answer but say so explicitly and "
                        "explain why the short-window reading is the relevant one here."
                    ),
                }
                messages.append(nudge)
                retry = provider.chat(messages, tools=TOOL_SCHEMAS, max_tokens=_ANSWER_MAX_TOKENS)
                retry_reason = retry.pop("_finish_reason", None)
                retry_content = (retry.get("content") or "").strip()
                if retry_content and not (retry.get("tool_calls") or []):
                    messages.append(retry)
                    content = retry_content
                    finish_reason = retry_reason
                else:
                    messages.pop()  # nudge didn't help -- drop it, keep the original answer

            truncated = finish_reason == "length"
            primary_id = _primary_asset_id_from_trace(trace)
            headline, root_cause, recommendation = _split_agent_response(
                content or "The model returned an empty response."
            )
            if truncated:
                # Be honest in the answer text itself, not just in a flag the
                # UI might not render.
                root_cause = (
                    f"{root_cause}\n\n_(This answer was cut off by the model's output limit -- "
                    "the reasoning above is complete as far as it goes, but may be missing its "
                    "final points.)_"
                )
            answer = AgentAnswer(
                asset=_asset_name(entities, primary_id) if primary_id else None,
                scenario="agentic",
                answer=root_cause,
                confidence="model-reasoned",
                evidence=trace,
                presented_by=getattr(provider, "model", type(provider).__name__),
                asset_id=primary_id,
                recommendation=recommendation,
                headline=headline,
                truncated=truncated,
            )
            yield {"type": "final", "answer": answer, "plan": plan_steps}
            return

        for call in tool_calls:
            name = call["function"]["name"]
            try:
                call_args = json.loads(call["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                call_args = {}

            if name == "write_plan":
                plan_steps = _normalize_plan_steps(call_args.get("steps"))
                trace.append(
                    {"type": "tool_call", "tool": name, "arguments": call_args, "result": {"ok": True}}
                )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.get("id"),
                        "content": json.dumps({"ok": True, "steps": plan_steps}),
                    }
                )
                yield {"type": "plan", "steps": plan_steps}
                continue

            yield {
                "type": "tool_call",
                "tool": name,
                "arguments": call_args,
                "label": _tool_call_label(entities, name, call_args),
            }

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
            yield {
                "type": "tool_result",
                "tool": name,
                "walk_step": _walk_step_for_tool_call(kg, entities, trace[-1]),
            }

    primary_id = _primary_asset_id_from_trace(trace)
    answer = AgentAnswer(
        asset=_asset_name(entities, primary_id) if primary_id else None,
        scenario="agentic",
        answer="I wasn't able to reach a conclusion within the tool-call budget.",
        confidence="low",
        evidence=trace,
        presented_by=getattr(provider, "model", type(provider).__name__),
        asset_id=primary_id,
        headline="Inconclusive within the tool-call budget",
    )
    yield {"type": "final", "answer": answer, "plan": plan_steps}


def _run_agentic(
    question: str,
    entities: list[dict[str, Any]],
    kg: KnowledgeGraph,
    provider: Any,
    history: Any = None,
) -> AgentAnswer:
    """Non-streaming callers (e.g. `answer_question`) that only want the
    finished answer: drains `_run_agentic_events` and returns its one
    `"final"` event's answer, discarding the intermediate plan/tool-call
    events. Same raises-on-failure contract as before this was refactored
    into a generator."""
    for event in _run_agentic_events(question, entities, kg, provider, history=history):
        if event["type"] == "final":
            return event["answer"]
    raise RuntimeError("_run_agentic_events ended without a final event")  # pragma: no cover -- defensive


def answer_question(
    question: str, entities: list[dict[str, Any]], kg: KnowledgeGraph, history: Any = None
) -> AgentAnswer:
    """Public entry point. Uses the LLM-driven agentic tool-calling loop when
    a language model is configured; falls back to the deterministic
    `_dispatch` handlers (unchanged, always available) if no LLM is
    configured or the agentic loop fails for any reason. `history` (prior
    {question, answer} turns) is only used by the agentic path -- the
    deterministic fallback is stateless per question."""
    provider = get_text_generation_provider()
    if isinstance(provider, NullTextGenerationProvider):
        return _dispatch(question, entities, kg)
    try:
        return _run_agentic(question, entities, kg, provider, history=history)
    except Exception:
        return _dispatch(question, entities, kg)


def _final_payload(
    entities: list[dict[str, Any]], kg: KnowledgeGraph, answer: AgentAnswer, plan: list[dict[str, str]]
) -> dict[str, Any]:
    """The same JSON shape `/api/chat` has always returned (asset/scenario/
    answer/headline/recommendation/raw_answer/presented_by/confidence/
    evidence/panel), plus `plan`, wrapped as a `{"type": "final", ...}` SSE
    event by `stream_answer`. Factored out so both the streaming and any
    future non-streaming path build the exact same response shape."""
    return {
        "type": "final",
        "asset": answer.asset,
        "scenario": answer.scenario,
        "answer": answer.answer,
        "headline": answer.headline,
        "recommendation": answer.recommendation,
        "raw_answer": answer.raw_answer,
        "presented_by": answer.presented_by,
        "confidence": answer.confidence,
        "truncated": answer.truncated,
        "evidence": answer.evidence,
        "panel": build_ui_panel(entities, kg, answer),
        "plan": plan,
    }


def stream_answer(
    question: str, entities: list[dict[str, Any]], kg: KnowledgeGraph, history: Any = None
) -> Iterator[dict[str, Any]]:
    """Streaming counterpart to `answer_question()`, for `/api/chat`'s SSE
    response: yields `_run_agentic_events`'s plan/tool_call/tool_result
    events live, ending with one `_final_payload()` event. Falls back to a
    single final event wrapping the deterministic `_dispatch` answer (no
    intermediate events -- it's instant, there's nothing to stream) if no
    LLM is configured or the agentic loop fails for any reason, same
    fallback discipline as `answer_question`. `history` (prior {question,
    answer} turns from the client) only feeds the agentic path -- the
    deterministic fallback stays stateless per question."""
    provider = get_text_generation_provider()
    if not isinstance(provider, NullTextGenerationProvider):
        try:
            plan: list[dict[str, str]] = []
            for event in _run_agentic_events(question, entities, kg, provider, history=history):
                if event["type"] == "final":
                    yield _final_payload(entities, kg, event["answer"], event.get("plan", []))
                    return
                if event["type"] == "plan":
                    plan = event["steps"]
                yield event
        except Exception:
            pass  # fall through to the deterministic path below
    answer = _dispatch(question, entities, kg)
    yield _final_payload(entities, kg, answer, plan=[])


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

# Superset of _RECORD_LABEL_TO_TYPE used ONLY by `_answer_full_context` (the
# "show me everything known about X across all systems" question), which is
# meant to enumerate every artifact attached to an asset. HistorianTag is
# metadata rather than a dated event, so it's deliberately NOT in the base map
# -- a diagnostic answer shouldn't list all of an asset's tags as evidence
# cards (its relevant trends already render as charts), but a "show me
# everything" answer absolutely should.
_CONTEXT_LABEL_TO_TYPE = {**_RECORD_LABEL_TO_TYPE, "HistorianTag": "historian_tag"}

_TIMELINE_SOURCE = {
    "alarm_event": "AM",
    "alarm_config": "AM",
    "work_order": "CMMS",
    "operator_action": "DCS",
    "health_event": "APM",
    "cost_posting": "ERP",
    "historian_trend": "HIST",
    "historian_tag": "HIST",
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
    if t == "historian_tag":
        unit = item.get("eng_unit") or ""
        desc = item.get("description") or item.get("id", "")
        return {
            "type": t,
            # Tag metadata, not a dated event -- excluded from the Timeline
            # (build_ui_panel filters on a truthy "time"), shown as an
            # evidence card, and walkable as a real graph node.
            "time": "",
            "source": _TIMELINE_SOURCE[t],
            "ref": item.get("id", ""),
            "text": f"Historian tag {item.get('id')}: {desc}" + (f" ({unit})" if unit else ""),
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


# --------------------------------------------------------------------------
# Graph walk -- the ordered sequence of nodes the reasoning process actually
# touched, replayed by the frontend (`animateGraphWalk` in frontend/app.js)
# as a step-by-step highlight across the explorer before settling into
# `focusAnswerInGraph`'s final all-at-once highlighted state. This is what
# visually ties "the agent is reasoning" to "here's where on the graph it's
# currently looking", one step at a time, instead of only ever showing the
# finished result.
# --------------------------------------------------------------------------


def _walk_step_for_tool_call(
    kg: KnowledgeGraph, entities: list[dict[str, Any]], call: dict[str, Any]
) -> Optional[dict[str, Any]]:
    """One walk step per agentic tool call, in the exact order the LLM made
    them. Returns None for a call that touched no real graph node (e.g.
    `list_assets`, or a tool call whose result was empty/an error) --
    nothing to visually walk to for that step."""
    tool = call.get("tool")
    args = call.get("arguments") or {}
    result = call.get("result")

    if tool == "get_asset_context":
        unified_id = args.get("unified_id")
        if not unified_id or unified_id not in kg.nodes or not isinstance(result, dict):
            return None
        node_ids = [unified_id]
        for rows in result.get("records", {}).values():
            if not isinstance(rows, list):
                continue
            node_ids += [r["id"] for r in rows if isinstance(r, dict) and r.get("id") in kg.nodes]
        return {
            "label": f"Pulled cross-system context for {_asset_name(entities, unified_id)}",
            "node_ids": node_ids,
        }

    if tool == "get_historian_trend":
        tag = args.get("tag")
        if not tag or tag not in kg.nodes:
            return None
        return {"label": f"Checked the {tag} historian trend", "node_ids": [tag]}

    if tool == "get_related_assets":
        unified_id = args.get("unified_id")
        if not unified_id or unified_id not in kg.nodes or not isinstance(result, list):
            return None
        node_ids = [unified_id] + [
            r["unified_id"] for r in result if isinstance(r, dict) and r.get("unified_id") in kg.nodes
        ]
        return {
            "label": f"Checked process-flow relationships for {_asset_name(entities, unified_id)}",
            "node_ids": node_ids,
        }

    if tool == "search_evidence":
        if not isinstance(result, dict):
            return None
        node_ids = [
            r["id"] for r in result.get("results", []) if isinstance(r, dict) and r.get("id") in kg.nodes
        ]
        if not node_ids:
            return None
        return {"label": f'Searched evidence for "{args.get("query", "")}"', "node_ids": node_ids}

    if tool == "get_plant_status_summary":
        if not isinstance(result, dict):
            return None
        node_ids = []
        for key in ("active_alarms", "open_work_orders", "recent_health_events"):
            node_ids += [
                r["id"] for r in result.get(key, []) if isinstance(r, dict) and r.get("id") in kg.nodes
            ]
        if not node_ids:
            return None
        # Capped -- this is a plant-wide snapshot, not meant to highlight
        # dozens of nodes in one animation frame.
        return {"label": "Reviewed plant-wide alarm & work-order status", "node_ids": node_ids[:25]}

    # list_assets: touches every asset -- not a meaningful single "step" to
    # walk to (would just highlight the whole graph at once).
    return None


def build_graph_walk(
    entities: list[dict[str, Any]], kg: KnowledgeGraph, answer: AgentAnswer
) -> list[dict[str, Any]]:
    """Ordered [{label, node_ids}, ...] steps for the frontend to animate
    through. Works for both reasoning modes:
      - Agentic mode: one step per tool call (`answer.evidence` is
        `_run_agentic`'s raw trace) via `_walk_step_for_tool_call`.
      - Deterministic mode: one step per evidence item gathered by
        `_dispatch`'s handler, in the order it was gathered -- not as rich
        as a real tool-call trace, but still reflects the actual order the
        rule-based logic examined evidence.
    Returns [] if no asset was resolved for this answer (nothing to walk).
    """
    if not answer.asset_id or answer.asset_id not in kg.nodes:
        return []

    steps = [{"label": f"Start at {_asset_name(entities, answer.asset_id)}", "node_ids": [answer.asset_id]}]
    seen_node_sets: set[tuple[str, ...]] = set()

    for item in answer.evidence:
        if item.get("type") == "tool_call":
            step = _walk_step_for_tool_call(kg, entities, item)
        else:
            described = _describe_record(item)
            if described and described.get("ref") in kg.nodes:
                step = {"label": described["text"], "node_ids": [described["ref"]]}
            else:
                step = None

        if not step or not step["node_ids"]:
            continue
        key = tuple(step["node_ids"])
        if key in seen_node_sets:
            continue  # skip an exact repeat of the same node set (e.g. a duplicate tool call)
        seen_node_sets.add(key)
        steps.append(step)

    return steps


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
        {
            "type": "alias",
            "label": f'{m["system"]}:{m["local_id"]}',
            # Alias rows are metadata about the selected entity itself.
            "ref": answer.asset_id,
        }
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
            {
                "time": d["time"],
                "source": d["source"],
                "text": d["text"],
                "ref": d.get("ref", ""),
            }
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
            "ref": d.get("ref", ""),
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
        "entity_id": entity["unified_id"],
        "relationships": relationships,
        "timeline": timeline,
        "evidence": evidence_cards,
        "charts": charts,
        # See build_graph_walk()'s docstring: ordered steps for the
        # frontend's animated graph-walk, distinct from the flattened/
        # sorted timeline+evidence above.
        "walk": build_graph_walk(entities, kg, answer),
    }
    