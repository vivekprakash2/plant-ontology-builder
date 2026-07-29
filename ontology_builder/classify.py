"""Lexical helpers used to normalize free-text attributes into comparable fields.

These stand in for an AI/embeddings classifier so Stage 0/1 can run fully
offline. `llm_provider.py` exposes the swap point for a real model later —
this module should stay simple keyword/regex logic only.
"""
from __future__ import annotations

import re
from typing import Optional

# Ordered so more specific phrases are checked before generic ones.
_CLASS_KEYWORDS: list[tuple[str, str]] = [
    ("centrifugalpump", "Pump"),
    ("reflux pump", "Pump"),
    ("charge pump", "Pump"),
    ("pump", "Pump"),
    ("compressor", "Compressor"),
    ("preheat bundle", "HeatExchanger"),
    ("preheat exchanger", "HeatExchanger"),
    ("heat exchanger", "HeatExchanger"),
    ("exchanger", "HeatExchanger"),
    ("fired heater", "FiredHeater"),
    ("heater", "FiredHeater"),
    ("column", "Column"),
    ("drum", "Drum"),
    ("control valve", "Valve"),
    ("valve", "Valve"),
    ("tank", "Tank"),
]

# Fallback when only a functional-location style suffix is available
# (e.g. CMMS "FAC1-U100-PUMPS", ERP "FAC1/U100/ROT").
_LOCATION_SUFFIX_CLASS: dict[str, str] = {
    "PUMPS": "Pump",
    "HX": "HeatExchanger",
    "VLV": "Valve",
    "TANKS": "Tank",
    # "ROT" (rotating equipment) is ambiguous between Pump/Compressor on its
    # own -- intentionally NOT mapped here so callers fall back to
    # description-keyword classification instead of guessing.
}

_CRITICALITY_NORMALIZE = {
    "high": "High",
    "medium": "Medium",
    "low": "Low",
    "a": "High",
    "b": "Medium",
    "c": "Low",
}

# Matches "Unit 100", "UNIT100", as well as the shorthand forms actually used
# in this dataset: "U100" (DCS loop ids), "CON-U100" (AM console),
# "FAC1-U100-PUMPS" (CMMS functional_location), "FAC1/U400/VLV" (ERP).
_UNIT_RE = re.compile(r"\bu(?:nit)?[_\-\s]?0*(\d{2,4})\b", re.IGNORECASE)
_TRAILING_NUMBER_RE = re.compile(r"(\d{3})(?!.*\d{3})")


def classify_asset(*texts: Optional[str]) -> Optional[str]:
    """Return a normalized asset class by keyword-matching over given texts.

    Checks each text in order and returns the first keyword match found.
    """
    for text in texts:
        if not text:
            continue
        lowered = text.lower()
        for keyword, cls in _CLASS_KEYWORDS:
            if keyword in lowered:
                return cls
    return None


def classify_from_location_suffix(location: Optional[str]) -> Optional[str]:
    if not location:
        return None
    suffix = location.rstrip("/").split("/")[-1].split("-")[-1].upper()
    return _LOCATION_SUFFIX_CLASS.get(suffix)


def normalize_criticality(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    return _CRITICALITY_NORMALIZE.get(value.strip().lower())


def extract_unit(*texts: Optional[str]) -> Optional[str]:
    """Find a unit number like '100' from strings such as 'Unit 100',
    'U100', 'CON-U100', 'FAC1-U100-PUMPS', 'FAC1/U100/ROT'."""
    for text in texts:
        if not text:
            continue
        match = _UNIT_RE.search(text.replace("_", " ").replace("-", " "))
        if match:
            return match.group(1)
    return None


def extract_trailing_number(text: Optional[str]) -> Optional[str]:
    """Return the last standalone 3-digit run in `text`, e.g.
    'Crude Charge Pump 101' -> '101', 'PMP-100-101' -> '101'."""
    if not text:
        return None
    match = _TRAILING_NUMBER_RE.search(text)
    return match.group(1) if match else None


# Matches equipment-style codes embedded in free text, e.g. "E-101", "CV-400",
# "P-101", "H-101". Used to mine CMMS work-order notes for equipment
# identifiers CMMS itself doesn't expose in structured fields.
_CODE_MENTION_RE = re.compile(r"\b[A-Z]{1,3}-(\d{2,4})\b")


def extract_mentioned_equipment_number(
    texts: list[str], own_unit: Optional[str] = None
) -> Optional[str]:
    """Scan free text (e.g. maintenance notes) for an equipment code like
    'E-101' or 'CV-400' and return its numeric suffix.

    If `own_unit` is given and a candidate number equals it, the match is
    skipped -- that number is almost certainly a *unit* reference (e.g.
    "CV-400" = a valve in Unit 400) rather than a distinct equipment number.
    """
    for text in texts:
        if not text:
            continue
        for match in _CODE_MENTION_RE.finditer(text):
            number = match.group(1)
            if own_unit and number == own_unit:
                continue
            return number
    return None

