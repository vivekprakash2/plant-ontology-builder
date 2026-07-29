"""Stage 1 - Entity Resolution.

Scores every cross-system pair of AssetProfiles, applies hard blocking
filters (never merge different units / different equipment numbers /
different asset classes), and clusters matches with union-find into
unified physical assets with a confidence score and evidence trail.

A second pass applies the one genuine hard foreign key in the dataset:
`erp_cost_postings.linked_wo` -> `cmms_workorders.wo_id` -> `asset_code`,
which directly confirms an ERP<->CMMS link (this is how the "hard" CMMS
system gets tied in with high confidence instead of guesswork alone).
"""
from __future__ import annotations

import csv
from dataclasses import dataclass, field
from typing import Optional

from . import config
from .llm_provider import SimilarityProvider, get_similarity_provider
from .models import AssetProfile

MATCH_THRESHOLD = 0.55

# weight given to each signal *when both sides have a known value for it*
#
# NOTE: `unit` + `equipment_number` alone (0.15 + 0.25 = 0.40) must stay
# below MATCH_THRESHOLD. A synthetic-data test (new equipment type using
# vocabulary outside `_CLASS_KEYWORDS`, e.g. "Turbine", reusing the common
# per-unit "-101" instance-number convention shared by real Pump-101 /
# Compressor-101 / Column-101 / Heater-101 / Exchanger-101 equipment) proved
# that when class is unknown on either side, the class hard-filter never
# fires -- so if unit+equipment_number alone could already clear the old
# 0.50 threshold, an unclassified new asset could transitively merge
# several *different*, previously-correctly-separated real assets into one
# false cluster. `class`'s weight was raised (now the single heaviest
# signal) so a genuine match still needs class support (or strong name
# similarity) to cross the threshold, not just a coincidental shared
# unit+number.
_WEIGHTS = {
    "unit": 0.15,
    "class": 0.30,
    "equipment_number": 0.25,
    "criticality": 0.05,
    "name_similarity": 0.25,
}
TOTAL_WEIGHT = sum(_WEIGHTS.values())

# A match is flagged `low_signal` (needs human review) when it clears
# MATCH_THRESHOLD WITHOUT a confirmed class match and WITHOUT a genuinely
# high-confidence name match -- i.e. it's relying solely on unit+equipment
# number (+ a merely moderate text overlap) rather than real corroborating
# evidence. Weight/threshold tuning alone can't eliminate this ambiguity
# (proven: a future source system sharing a naming template with an
# existing one can still coincidentally clear MATCH_THRESHOLD -- see
# repo memory), so instead of silently trusting or rejecting these edges,
# they're surfaced for review. 0.80 is deliberately well above the
# ~0.55-0.76 range seen from generic templated-name collisions (e.g.
# "Aerator Speed Control" ~ "Compressor Speed Control" = 0.76) and below
# genuine same-asset paraphrases (e.g. "Crude Charge Pump 101" ~ "Crude
# Charge Pump A" = 0.90).
NAME_SIMILARITY_HIGH_CONFIDENCE = 0.80


@dataclass
class MatchEdge:
    a: str  # AssetProfile.key
    b: str
    score: float
    reasons: list[str]
    forced: bool = False
    low_signal: bool = False


def compute_ambiguous_class_units(profiles: list[AssetProfile]) -> set[tuple[str, str]]:
    """(asset_class, unit) pairs where 2+ DISTINCT equipment_number values
    are observed anywhere in the dataset -- i.e. real siblings genuinely
    exist (P-101 AND P-102 are both 'Pump'/Unit 100), so a merge for this
    (class, unit) group cannot safely rely on class+unit alone.

    This exists because a single pairwise `score_pair()` call cannot tell
    "same class, same unit, no equipment_number on one side" apart from a
    genuine identity match vs. two different siblings -- that requires
    population-level context (does a second, different equipment_number
    exist anywhere for this class+unit?). Found via a real regression: an
    embeddings-based name-similarity swap let `CMMS:EQ-1042` (Crude Charge
    Pump A, no equipment_number -- CMMS's known weakness) merge with
    `Historian:...PUMP_102` (a DIFFERENT real pump) purely on unit+class
    (0.45) + moderate name similarity (0.55*0.25=0.14) = 0.59, crossing
    threshold -- "class match" is trivially true for ANY two pumps in the
    same unit and provides zero discrimination between siblings.
    """
    seen: dict[tuple[str, str], set[str]] = {}
    for p in profiles:
        if p.asset_class and p.unit and p.equipment_number:
            seen.setdefault((p.asset_class, p.unit), set()).add(p.equipment_number)
    return {key for key, numbers in seen.items() if len(numbers) > 1}


def score_pair(
    p1: AssetProfile,
    p2: AssetProfile,
    similarity: SimilarityProvider,
    ambiguous_class_units: Optional[set[tuple[str, str]]] = None,
) -> Optional[tuple[float, list[str], bool]]:
    """Return (score, reasons, low_signal) or None if the pair is
    hard-rejected / has no comparable signal at all."""
    if p1.system == p2.system:
        return None

    # --- hard filters: never merge across these, even with high text similarity ---
    if p1.unit and p2.unit and p1.unit != p2.unit:
        return None
    if p1.asset_class and p2.asset_class and p1.asset_class != p2.asset_class:
        return None
    if p1.equipment_number and p2.equipment_number and p1.equipment_number != p2.equipment_number:
        return None
    # Criticality doubles as a disambiguator between similar-but-distinct
    # equipment of the same class/unit (e.g. P-101 = High/A vs P-102 =
    # Medium, both centrifugal pumps in Unit 100) -- treat a mismatch as a
    # hard reject rather than a soft penalty.
    if p1.criticality and p2.criticality and p1.criticality != p2.criticality:
        return None

    # --- ambiguous-sibling guard: class+unit alone is NOT enough evidence
    # when real siblings of this (class, unit) exist in the dataset and
    # this specific pair can't confirm equipment_number itself. Require a
    # near-exact name match instead of letting a moderate text-similarity
    # score combine with unit+class to cross MATCH_THRESHOLD. ---
    equipment_number_confirmed = bool(
        p1.equipment_number and p2.equipment_number and p1.equipment_number == p2.equipment_number
    )
    if (
        ambiguous_class_units
        and not equipment_number_confirmed
        and p1.unit
        and p2.unit
        and p1.unit == p2.unit
        and p1.asset_class
        and p2.asset_class
        and p1.asset_class == p2.asset_class
        and (p1.asset_class, p1.unit) in ambiguous_class_units
    ):
        name_sim_check = (
            similarity.similarity(p1.name, p2.name) if (p1.name and p2.name) else 0.0
        )
        if name_sim_check < NAME_SIMILARITY_HIGH_CONFIDENCE:
            return None

    numerator = 0.0
    reasons: list[str] = []
    class_confirmed = False

    # NOTE: the denominator is the *fixed* total possible weight (see
    # TOTAL_WEIGHT below), not the sum of only the "known" dimensions.
    # Renormalizing over just the known dimensions would let a pair with
    # sparse data (e.g. CMMS records with no equipment_number) look more
    # confident than one with full evidence -- missing information should
    # lower confidence, not be excluded from it.
    def add(weight: float, known: bool, matched: bool, label: str) -> None:
        nonlocal numerator
        if not known:
            return
        if matched:
            numerator += weight
            reasons.append(f"{label} match")

    add(_WEIGHTS["unit"], bool(p1.unit and p2.unit), p1.unit == p2.unit, "unit")
    class_known = bool(p1.asset_class and p2.asset_class)
    class_matched = p1.asset_class == p2.asset_class
    add(_WEIGHTS["class"], class_known, class_matched, "class")
    class_confirmed = class_known and class_matched
    add(
        _WEIGHTS["equipment_number"],
        bool(p1.equipment_number and p2.equipment_number),
        p1.equipment_number == p2.equipment_number,
        "equipment_number",
    )
    add(
        _WEIGHTS["criticality"],
        bool(p1.criticality and p2.criticality),
        p1.criticality == p2.criticality,
        "criticality",
    )

    name_sim = 0.0
    if p1.name and p2.name:
        name_sim = similarity.similarity(p1.name, p2.name)
        numerator += _WEIGHTS["name_similarity"] * name_sim
        if name_sim >= 0.5:
            reasons.append(f"name similarity {name_sim:.2f} ('{p1.name}' ~ '{p2.name}')")

    low_signal = not class_confirmed and name_sim < NAME_SIMILARITY_HIGH_CONFIDENCE
    return numerator / TOTAL_WEIGHT, reasons, low_signal


class UnionFind:
    def __init__(self, keys: list[str]) -> None:
        self.parent = {k: k for k in keys}

    def find(self, k: str) -> str:
        while self.parent[k] != k:
            self.parent[k] = self.parent[self.parent[k]]
            k = self.parent[k]
        return k

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb


def _find_erp_cmms_fk_links(profiles_by_key: dict[str, AssetProfile]) -> list[MatchEdge]:
    """Hard FK: ERP cost posting -> CMMS work order -> CMMS asset_code."""
    wo_to_asset: dict[str, str] = {}
    with open(config.CMMS_DIR / "cmms_workorders.csv", newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            wo_to_asset[row["wo_id"]] = row["asset_code"]

    edges: list[MatchEdge] = []
    with open(config.ERP_DIR / "erp_cost_postings.csv", newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            asset_code = wo_to_asset.get(row["linked_wo"])
            if not asset_code:
                continue
            erp_key = f"ERP:{row['erp_asset_id']}"
            cmms_key = f"CMMS:{asset_code}"
            if erp_key in profiles_by_key and cmms_key in profiles_by_key:
                edges.append(
                    MatchEdge(
                        a=erp_key,
                        b=cmms_key,
                        score=1.0,
                        reasons=[
                            f"hard FK: ERP posting {row['posting_id']} -> "
                            f"work order {row['linked_wo']} -> CMMS asset {asset_code}"
                        ],
                        forced=True,
                    )
                )
    return edges


@dataclass
class ResolutionResult:
    profiles_by_key: dict[str, AssetProfile]
    edges: list[MatchEdge]
    clusters: list[list[str]] = field(default_factory=list)

    def cluster_confidence(self, member_keys: list[str]) -> Optional[float]:
        members = set(member_keys)
        relevant = [e.score for e in self.edges if e.a in members and e.b in members]
        if not relevant:
            return None
        return sum(relevant) / len(relevant)

    def low_signal_edges(self, member_keys: list[str]) -> list[MatchEdge]:
        """Edges within this cluster that relied on unit+equipment_number
        alone (no confirmed class match, no high-confidence name match, not
        a hard FK) -- i.e. matches that should be surfaced for human review
        rather than silently trusted, since no threshold/weight tuning can
        reliably distinguish these from a coincidental false merge (see
        repo memory: the 'Aerator'/'SCADA' naming-template collision test)."""
        members = set(member_keys)
        return [
            e
            for e in self.edges
            if e.low_signal and e.a in members and e.b in members
        ]


def _candidate_pairs(flat: list[AssetProfile]) -> list[tuple[AssetProfile, AssetProfile]]:
    """Yield only pairs worth actually scoring, instead of the full O(n^2)
    cross product over every profile.

    This produces the EXACT SAME set of pairs that could ever possibly
    pass `score_pair()`'s hard filters -- it's a pure performance
    optimization, not a behavior change. The hard filters already reject
    any pair where unit or asset_class are both known and DIFFER, so most
    of a naive O(n^2) scan is guaranteed-rejected work: comparing a Unit
    400 valve against a Unit 100 pump, or a compressor against a tank,
    can never match. Blocking groups profiles by (unit, asset_class) and
    only compares within each group -- for a plant with hundreds or
    thousands of assets (not this ~30-profile demo dataset), that turns a
    quadratic scan into roughly one much smaller quadratic scan per group,
    which is dramatically cheaper in aggregate.

    Profiles missing EITHER unit or asset_class go in a separate
    "wildcard" bucket and are compared against EVERYTHING (including each
    other) -- because a hard filter only fires when BOTH sides have a
    known, differing value, a profile with an unknown unit/class could
    still legitimately match anything, so it can't be safely blocked away.
    This is exactly why CMMS records (which often lack a clean class) and
    any newly-onboarded system with unrecognized vocabulary still get
    fully compared -- blocking here only skips comparisons that were
    ALWAYS going to be rejected anyway, never a comparison that could have
    produced a real match.
    """
    blocks: dict[tuple[str, str], list[AssetProfile]] = {}
    wildcard: list[AssetProfile] = []
    for p in flat:
        if p.unit and p.asset_class:
            blocks.setdefault((p.unit, p.asset_class), []).append(p)
        else:
            wildcard.append(p)

    pairs: list[tuple[AssetProfile, AssetProfile]] = []
    for group in blocks.values():
        for i, p1 in enumerate(group):
            for p2 in group[i + 1 :]:
                pairs.append((p1, p2))
    for i, p1 in enumerate(wildcard):
        for p2 in wildcard[i + 1 :]:
            pairs.append((p1, p2))
        for group in blocks.values():
            for p2 in group:
                pairs.append((p1, p2))
    return pairs


def resolve(all_profiles: dict[str, list[AssetProfile]]) -> ResolutionResult:
    flat: list[AssetProfile] = [p for plist in all_profiles.values() for p in plist]
    profiles_by_key = {p.key: p for p in flat}
    similarity = get_similarity_provider()
    ambiguous_class_units = compute_ambiguous_class_units(flat)

    edges: list[MatchEdge] = []
    for p1, p2 in _candidate_pairs(flat):
        if p1.system == p2.system:
            continue
        result = score_pair(p1, p2, similarity, ambiguous_class_units)
        if result is None:
            continue
        score, reasons, low_signal = result
        if score >= MATCH_THRESHOLD:
            edges.append(
                MatchEdge(
                    a=p1.key, b=p2.key, score=score, reasons=reasons, low_signal=low_signal
                )
            )

    edges.extend(_find_erp_cmms_fk_links(profiles_by_key))

    uf = UnionFind(list(profiles_by_key.keys()))
    for edge in edges:
        uf.union(edge.a, edge.b)

    groups: dict[str, list[str]] = {}
    for key in profiles_by_key:
        groups.setdefault(uf.find(key), []).append(key)

    result = ResolutionResult(profiles_by_key=profiles_by_key, edges=edges)
    result.clusters = list(groups.values())
    return result
