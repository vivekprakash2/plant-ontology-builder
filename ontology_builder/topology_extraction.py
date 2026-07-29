"""Autonomous process-topology extraction (no human transcription step).

`graph.py`'s `_PROCESS_TOPOLOGY` constant encodes the plant's physical
process-flow relationships (FEEDS/COOLS/SUPPLIES_UTILITY) as a hardcoded
Python list, manually transcribed by a human from `SCENARIO.md` Sec 5b's
prose description. None of the six source systems (AM/APM/DCS/CMMS/ERP/
Historian) structurally encode "what feeds what" anywhere -- that
information only exists in unstructured text (a scenario doc here; a P&ID
description, engineering narrative, or maintenance log in a real
deployment).

This module removes that human transcription step: it uses the configured
LLM (see `llm_provider.get_text_generation_provider`) to read raw prose and
propose FEEDS/COOLS/SUPPLIES_UTILITY relationships directly, with NO human
translating text to code in between.

SAFETY DESIGN (this is the part that must stay deterministic even though
the *extraction* isn't): an LLM reading prose can mis-parse, hallucinate an
equipment name, or invent a relationship that isn't really there. So the
extracted relationships are NEVER trusted directly:
  1. The model is grounded with the exact list of known canonical names +
     per-system aliases already resolved by Stage 1 -- it's instructed to
     use ONLY those names, never invent one.
  2. Every proposed relationship is re-validated in code afterward: both
     equipment references must resolve (exact match, or a high-confidence
     fuzzy match) to a real unified entity, or the edge is dropped and
     logged -- exactly like a hallucinated/misspelled name in `graph.py`'s
     original hardcoded list would fail the `if a_id and b_id` check.
  3. Every accepted edge is tagged `extracted_by="llm"` (vs. the old
     `doc_source="SCENARIO.md Sec 5b"` for human-curated edges) so a
     reasoning/reporting layer can treat LLM-derived topology as needing
     review rather than presenting it with the same confidence as a
     verified fact -- same pattern as `resolution.py`'s `low_signal` flag.
"""
from __future__ import annotations

import difflib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from .llm_provider import TextGenerationProvider

_NAME_RESOLUTION_FLOOR = 0.80  # same bar as resolution.py's NAME_SIMILARITY_HIGH_CONFIDENCE

_EXTRACTION_SYSTEM_PROMPT = (
    "You extract physical process-flow relationships between plant equipment "
    "from prose engineering text (e.g. a process description, P&ID narrative, "
    "or scenario write-up).\n"
    "Rules:\n"
    "1. You will be given a list of KNOWN EQUIPMENT NAMES. You must refer to "
    "equipment ONLY using one of those exact strings -- never invent, "
    "abbreviate, or paraphrase a name that isn't in the list. If the text "
    "mentions equipment not in the list (e.g. a piece of equipment not part "
    "of this dataset), omit any relationship involving it rather than "
    "guessing which known name it might be.\n"
    "2. Only extract relationships that describe physical material/utility "
    "flow between two pieces of equipment -- classify each as one of: "
    "FEEDS (process material flows from A into B), COOLS (A provides cooling "
    "to B), SUPPLIES_UTILITY (A supplies a utility such as steam/BFW to B). "
    "Do not invent a fourth type.\n"
    "3. Output ONLY a JSON array, no prose before or after, in this exact "
    "shape: "
    '[{"from": "<known equipment name>", "to": "<known equipment name>", '
    '"rel_type": "FEEDS|COOLS|SUPPLIES_UTILITY", "note": "<short justification '
    'quoting or paraphrasing the source text>"}]\n'
    "4. If you are not confident two known equipment names are directly "
    "connected, omit that relationship rather than guessing."
)


@dataclass
class ExtractedTopologyEdge:
    from_unified_id: str
    to_unified_id: str
    rel_type: str
    note: str
    from_name_raw: str  # exactly what the LLM output, before resolution
    to_name_raw: str
    extracted_by: str  # the model name, for provenance


@dataclass
class TopologyExtractionResult:
    edges: list[ExtractedTopologyEdge] = field(default_factory=list)
    dropped: list[dict[str, Any]] = field(default_factory=list)  # unresolved/rejected proposals


def _known_names(entities: list[dict[str, Any]]) -> dict[str, str]:
    """Every name variant an LLM might see in prose -> unified_id. This is
    what grounds the extraction prompt and what proposed names get
    validated against -- an extracted relationship can only become a real
    edge if both sides resolve through this dict.

    Includes: each entity's canonical_name, every member's own `name`, and
    -- critically -- the short human equipment tags ("P-101", "E-101",
    "K-101", "CV-400", ...) already curated in `agent.py`'s `_ALIASES` for
    the chat agent. Those tags are exactly the vocabulary a process
    description actually uses, and reusing them (instead of re-deriving
    short codes from scratch here) keeps one single source of truth for
    "what a human calls this equipment".

    Deliberately does NOT index raw `local_id` values as bare aliases.
    `local_id` is an internal per-system identifier, not a guaranteed
    globally-unique human-meaningful code -- this dataset has a planted
    trap proving that directly: AM's `local_id` for the Recycle Gas
    COMPRESSOR is literally the string "C-101", the exact same string DCS/
    Historian use for the distillation COLUMN. Indexing raw local_ids
    caused exactly this collision in testing (a "C-101" reference meant to
    be the column resolved to the compressor instead via an exact-match
    hit on AM's local_id). `_ALIASES` already disambiguates this correctly
    ("K-101" -> compressor, "Column C-101" -> column) -- trust that curated
    mapping, not raw system-internal identifiers.
    """
    lookup: dict[str, str] = {}
    for entity in entities:
        lookup[entity["canonical_name"]] = entity["unified_id"]
        for member in entity["members"]:
            if member.get("name"):
                lookup[member["name"]] = entity["unified_id"]

    try:
        from .agent import _ALIASES, _anchor_to_unified_id  # local import avoids a module cycle

        for alias in _ALIASES:
            unified_id = _anchor_to_unified_id(entities, alias["anchor"])
            if unified_id:
                lookup[alias["name"]] = unified_id
    except ImportError:
        pass  # agent.py's aliases are a nice-to-have, not a hard dependency

    return lookup


_SHORT_CODE_RE = re.compile(r"^[A-Za-z]{1,4}-?\d{2,4}$")


def _resolve_name(name: str, lookup: dict[str, str]) -> Optional[str]:
    """Exact match first. Fuzzy fallback ONLY for longer, prose-style names
    -- never for short equipment-tag-shaped strings like "C-101"/"K-101".

    This distinction is load-bearing, not stylistic: measured directly
    against this dataset's real aliases, difflib.SequenceMatcher gives
    "C-101" a similarity of exactly 0.8 against EVERY OTHER "<letter>-101"
    style code (P-101, K-101, H-101, E-101 all tie at 0.8), because 4 of
    5 characters ("-101") are identical regardless of which letter
    differs -- the one character that actually distinguishes the
    equipment contributes the LEAST to the score. No similarity floor can
    fix this: any threshold that accepts a genuine near-miss also accepts
    every other same-shaped code as an equally-valid match, so fuzzy
    matching on short codes is fundamentally unreliable, not just
    mistunable. Long prose names ("Crude Charge Pump 101" vs "Crude Charge
    Pump A" = 0.90) don't have this problem -- there the fuzzy floor is
    safe and useful, so it stays for those.
    """
    if name in lookup:
        return lookup[name]
    if _SHORT_CODE_RE.match(name.strip()):
        return None  # exact match only for tag-shaped strings; never guess
    best_score, best_id = 0.0, None
    for known_name, unified_id in lookup.items():
        score = difflib.SequenceMatcher(None, name.lower(), known_name.lower()).ratio()
        if score > best_score:
            best_score, best_id = score, unified_id
    if best_score >= _NAME_RESOLUTION_FLOOR:
        return best_id
    return None


def _parse_json_array(text: str) -> list[dict[str, Any]]:
    """Extract the JSON array of proposed relationships from the model's
    raw text response.

    Deliberately takes the LAST top-level `[...]` block, trying each
    candidate from last to first until one parses. Models occasionally
    self-correct mid-response (e.g. draft a list, then write "let me
    correct that" and emit a second, revised list) -- a single greedy
    regex spanning the whole response would swallow the prose commentary
    between the two arrays as one invalid blob and silently parse to
    nothing. The LAST array a model writes is its final, corrected answer.
    """
    candidates = re.findall(r"\[.*?\]", text, re.DOTALL)
    for candidate in reversed(candidates):
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, list):
            return parsed
    return []


_VALID_REL_TYPES = {"FEEDS", "COOLS", "SUPPLIES_UTILITY"}


def extract_topology_with_llm(
    prose_text: str,
    entities: list[dict[str, Any]],
    provider: TextGenerationProvider,
) -> TopologyExtractionResult:
    """Autonomously derive FEEDS/COOLS/SUPPLIES_UTILITY relationships from
    unstructured prose -- no human transcribes anything. Returns only
    edges where BOTH equipment references resolved to a real, already-
    unified entity; everything else is reported in `.dropped` for
    visibility rather than silently discarded or silently trusted.
    """
    lookup = _known_names(entities)
    equipment_list = "\n".join(f"- {name}" for name in sorted(lookup))

    user_prompt = (
        f"KNOWN EQUIPMENT NAMES:\n{equipment_list}\n\n"
        f"PROCESS DESCRIPTION TEXT:\n{prose_text}\n\n"
        "Extract the relationships as instructed."
    )

    raw = provider.generate(_EXTRACTION_SYSTEM_PROMPT, user_prompt, max_tokens=1200)
    proposals = _parse_json_array(raw)

    result = TopologyExtractionResult()
    model_name = getattr(provider, "model", type(provider).__name__)

    for proposal in proposals:
        from_raw = str(proposal.get("from", "")).strip()
        to_raw = str(proposal.get("to", "")).strip()
        rel_type = str(proposal.get("rel_type", "")).strip().upper()
        note = str(proposal.get("note", "")).strip()

        if rel_type not in _VALID_REL_TYPES:
            result.dropped.append({**proposal, "reason": f"invalid rel_type {rel_type!r}"})
            continue

        from_id = _resolve_name(from_raw, lookup)
        to_id = _resolve_name(to_raw, lookup)

        if not from_id or not to_id:
            result.dropped.append(
                {
                    **proposal,
                    "reason": (
                        f"could not resolve {'from' if not from_id else 'to'} "
                        f"name {(from_raw if not from_id else to_raw)!r} to a known equipment"
                    ),
                }
            )
            continue

        if from_id == to_id:
            result.dropped.append({**proposal, "reason": "from/to resolved to the same asset"})
            continue

        result.edges.append(
            ExtractedTopologyEdge(
                from_unified_id=from_id,
                to_unified_id=to_id,
                rel_type=rel_type,
                note=note,
                from_name_raw=from_raw,
                to_name_raw=to_raw,
                extracted_by=model_name,
            )
        )

    return result
