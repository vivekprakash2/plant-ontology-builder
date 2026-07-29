"""Open-vocabulary asset-class classification -- fallback for equipment
types not in `classify.py`'s fixed keyword list (e.g. "Turbine").

`classify_asset()` can only ever recognize the ~8 hand-authored classes in
`_CLASS_KEYWORDS`. An embeddings-based "nearest of these known classes"
fallback was considered and tested empirically (see repo memory) -- it
fails safe for genuinely new equipment types (all 8 known-class
similarities for "H2 Recycle Turbine" landed at 0.42-0.57, well below any
sane confidence floor), but that also means it provides ZERO benefit for
a brand-new category: it can only ever pick "closest of these 8," never
correctly say "this is something new."

This module solves that different problem: it asks the configured LLM to
propose a class label in its own words, NOT constrained to any fixed
list, so it can genuinely invent "Turbine" as a new category.

SAFETY DESIGN:
- Only runs for profiles where `classify_asset()` already returned None --
  never overrides a keyword match. The fast, deterministic path stays
  primary for anything already recognized.
- CANONICALIZATION (the hard part): the LLM might describe the same new
  equipment type differently across different profiles/systems ("Turbine"
  vs "Gas Turbine" vs "Turbine Driver"). If each got its own literal
  label, the same physical equipment type would silently split into
  several different "classes" -- and `resolution.py`'s class-hard-filter
  would then wrongly treat them as different equipment classes and reject
  matches between them. To prevent this, every newly-proposed label is
  compared (via the same `SimilarityProvider` used elsewhere, ideally
  real embeddings) against every canonical label already established
  earlier in this run; if similar enough, it's folded into that EXISTING
  canonical label instead of creating a near-duplicate. This is a greedy,
  order-dependent online clustering -- deterministic given a fixed
  profile processing order, which `pipeline.py` provides.
- Every LLM-assigned class is tagged in `attributes`:
  `asset_class_source="llm_open_vocab"` and `asset_class_raw_llm_label`
  (the literal, pre-canonicalization text the model returned) -- mirrors
  `ingest.py`'s existing `equipment_number_source` provenance pattern, so
  it's always auditable which classes came from the deterministic keyword
  path vs. this LLM path.
- Fails safe: if the LLM call errors, times out, or returns something
  unusable (empty, "Unknown", too long to plausibly be a class name), the
  profile's `asset_class` simply stays None -- never crashes the pipeline,
  never assigns a nonsense label.
"""
from __future__ import annotations

from typing import Any

from .llm_provider import SimilarityProvider, TextGenerationProvider
from .models import AssetProfile

_OPEN_VOCAB_SYSTEM_PROMPT = (
    "You classify industrial equipment into a general TYPE category, not a "
    "specific instance or tag number.\n"
    "You will be given a list of KNOWN CATEGORIES already used elsewhere in "
    "this dataset. If the equipment reasonably fits one of them, respond "
    "with that EXACT category name, copied verbatim -- do not invent a "
    "near-duplicate (e.g. if 'Valve' is already known, a flow control "
    "valve should be classified as 'Valve', not 'FlowControlValve'). Only "
    "invent a brand-new category, as a single PascalCase word or short "
    "phrase (e.g. 'Turbine', 'Blower', 'Reactor'), if this is genuinely a "
    "different kind of equipment not covered by any known category.\n"
    "Respond with ONLY the category name -- never a full sentence, never "
    "the specific instance name, never an explanation. If you genuinely "
    "cannot determine a sensible equipment type from the text, respond "
    "with exactly 'Unknown'."
)

# Canonicalization floor: how similar two LLM-proposed labels must be to be
# treated as the same new class. Deliberately high (same bar as
# resolution.py's NAME_SIMILARITY_HIGH_CONFIDENCE) -- merging two labels
# that are actually different equipment types would be worse than leaving
# them as separate (if redundant) classes, since resolution.py's hard
# filter would then wrongly allow cross-matching between genuinely
# different equipment.
_CANONICALIZATION_FLOOR = 0.85

_MAX_LABEL_LENGTH = 40


def _clean_label(raw: str) -> str | None:
    """Validate + normalize a raw LLM response into a usable class label,
    or None if it's unusable (empty, "Unknown", too long/sentence-like)."""
    label = raw.strip().strip(".").split("\n")[0].strip()
    if not label or len(label) > _MAX_LABEL_LENGTH:
        return None
    if label.lower() == "unknown":
        return None
    return label


def classify_open_vocabulary(
    profiles: list[AssetProfile],
    provider: TextGenerationProvider,
    similarity: SimilarityProvider,
) -> None:
    """Mutates `asset_class` in place for every profile where
    `classify_asset()` returned None, using the LLM to propose a class
    label -- grounded with the list of categories already known in this
    dataset (both the fixed keyword classes already assigned elsewhere,
    and any new categories this function has already introduced), so it
    can reuse an existing category ("Valve") instead of inventing a
    near-duplicate ("FlowControlValve") for equipment that genuinely fits
    one already. Only a description that doesn't fit any known category
    gets a real new label.

    A post-hoc embeddings canonicalization pass still runs as a second
    safety net (in case the model doesn't copy an existing name exactly),
    but grounding the prompt with known categories up front is the
    PRIMARY defense -- it was added after finding empirically that
    post-hoc-only canonicalization missed a real case ("FlowControlValve"
    only scored 0.67 similarity against "Valve", well under the 0.85
    high-confidence floor, so it would have silently created a
    near-duplicate class and split a real asset's cluster in two).

    Processes `profiles` in the given order; caller order affects exactly
    which literal string becomes canonical for a brand-new category, not
    correctness.
    """
    # Seed with every asset_class already assigned by classify_asset()'s
    # keyword path, across ALL profiles -- these are the dataset's
    # existing, curated categories, and the model should prefer reusing
    # them over inventing something new whenever the equipment fits.
    canonical_labels: list[str] = sorted(
        {p.asset_class for p in profiles if p.asset_class}
    )
    raw_label_cache: dict[str, str] = {}  # text -> raw LLM label, avoids duplicate calls

    for profile in profiles:
        if profile.asset_class is not None:
            continue
        text = (profile.name or "").strip()
        if not text:
            continue

        if text in raw_label_cache:
            raw = raw_label_cache[text]
        else:
            known_categories = "\n".join(f"- {c}" for c in canonical_labels) or "(none yet)"
            user_prompt = f"KNOWN CATEGORIES:\n{known_categories}\n\nEQUIPMENT DESCRIPTION:\n{text}"
            try:
                response = provider.generate(_OPEN_VOCAB_SYSTEM_PROMPT, user_prompt, max_tokens=20)
            except Exception:
                continue  # fail safe: leave asset_class as None, never crash the pipeline
            raw = _clean_label(response) or ""
            raw_label_cache[text] = raw

        if not raw:
            continue

        canonical = raw
        best_score = 1.0 if raw in canonical_labels else 0.0  # exact match short-circuit
        if best_score < 1.0:
            for existing in canonical_labels:
                try:
                    score = similarity.similarity(raw, existing)
                except Exception:
                    score = 0.0
                if score > best_score:
                    best_score, canonical = score, existing

        if best_score < _CANONICALIZATION_FLOOR:
            canonical_labels.append(raw)
            canonical = raw

        profile.asset_class = canonical
        profile.attributes["asset_class_source"] = "llm_open_vocab"
        profile.attributes["asset_class_raw_llm_label"] = raw
