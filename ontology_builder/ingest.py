"""Stage 0 - Ingest: load each application's config into its own list of
`AssetProfile` objects (one independent view per system).
"""
from __future__ import annotations

import csv
import json
from typing import Any

from . import config
from .classify import (
    classify_asset,
    classify_from_location_suffix,
    extract_mentioned_equipment_number,
    extract_trailing_number,
    extract_unit,
    normalize_criticality,
)
from .models import AssetProfile


def _load_json(path) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _load_cmms_notes_by_asset() -> dict[str, list[str]]:
    """Group work-order notes by asset_code, e.g. for mining equipment-code
    mentions like 'E-101' or 'CV-400' that CMMS doesn't expose structurally."""
    notes_by_asset: dict[str, list[str]] = {}
    with open(config.CMMS_DIR / "cmms_workorders.csv", newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("notes"):
                notes_by_asset.setdefault(row["asset_code"], []).append(row["notes"])
    return notes_by_asset


def load_am() -> list[AssetProfile]:
    """Alarm Management: one profile per distinct equipment_ref in the alarm config."""
    data = _load_json(config.AM_DIR / "am_config.json")
    seen: dict[str, AssetProfile] = {}
    for alarm in data.get("alarms", []):
        local_id = alarm["equipment_ref"]
        if local_id in seen:
            continue
        name = alarm.get("equipment_name", local_id)
        unit = extract_unit(alarm.get("console"))
        asset_class = classify_asset(name)
        equipment_number = extract_trailing_number(local_id) or extract_trailing_number(name)
        seen[local_id] = AssetProfile(
            system="AM",
            local_id=local_id,
            name=name,
            unit=unit,
            asset_class=asset_class,
            equipment_number=equipment_number,
            attributes={
                "alarm_point": alarm.get("alarm_point"),
                "priority": alarm.get("priority"),
                "console": alarm.get("console"),
            },
        )
    return list(seen.values())


def load_apm() -> list[AssetProfile]:
    """Asset Performance Monitoring: `assets` list, class given explicitly."""
    data = _load_json(config.APM_DIR / "apm_config.json")
    profiles = []
    for asset in data.get("assets", []):
        display_name = asset.get("display_name", "")
        unit = extract_unit(display_name)
        equipment_number = extract_trailing_number(display_name)
        asset_class = classify_asset(asset.get("asset_class"), display_name)
        profiles.append(
            AssetProfile(
                system="APM",
                local_id=asset["apm_id"],
                name=display_name,
                unit=unit,
                asset_class=asset_class,
                equipment_number=equipment_number,
                criticality=normalize_criticality(asset.get("criticality")),
                attributes={
                    "manufacturer": asset.get("manufacturer"),
                    "monitored_params": asset.get("monitored_params"),
                    "raw_asset_class": asset.get("asset_class"),
                },
            )
        )
    return profiles


def load_dcs() -> list[AssetProfile]:
    """DCS / control loops: one profile per loop_id."""
    data = _load_json(config.DCS_DIR / "dcs_config.json")
    profiles = []
    for loop in data.get("loops", []):
        loop_id = loop["loop_id"]
        loop_name = loop.get("loop_name", "")
        unit = extract_unit(loop_id) or extract_unit(data.get("unit"))
        equipment_number = extract_trailing_number(loop_id)
        asset_class = classify_asset(loop_name)
        profiles.append(
            AssetProfile(
                system="DCS",
                local_id=loop_id,
                name=loop_name,
                unit=unit,
                asset_class=asset_class,
                equipment_number=equipment_number,
                attributes={
                    "pv_tag": loop.get("pv_tag"),
                    "sp_tag": loop.get("sp_tag"),
                    "op_tag": loop.get("op_tag"),
                    "loop_type": loop.get("loop_type"),
                },
            )
        )
    return profiles


def load_historian() -> list[AssetProfile]:
    """Historian: group tags sharing the same `FAC1.UNITxxx.EQUIPMENT_TOKEN`
    prefix into one profile per physical equipment token."""
    data = _load_json(config.HIST_DIR / "historian_config.json")
    groups: dict[str, dict[str, Any]] = {}
    for tag in data.get("tags", []):
        parts = tag["tag"].split(".")
        if len(parts) < 4:
            continue
        equipment_key = ".".join(parts[:3])  # e.g. FAC1.UNIT100.CENTRIFUGAL_PUMP_101
        equipment_token = parts[2]  # e.g. CENTRIFUGAL_PUMP_101
        group = groups.setdefault(
            equipment_key,
            {"equipment_token": equipment_token, "unit_token": parts[1], "descriptions": [], "tags": []},
        )
        group["descriptions"].append(tag.get("description", ""))
        group["tags"].append(tag["tag"])

    profiles = []
    for equipment_key, group in groups.items():
        equipment_text = group["equipment_token"].replace("_", " ")
        name = group["descriptions"][0] if group["descriptions"] else equipment_text
        unit = extract_unit(group["unit_token"])
        equipment_number = extract_trailing_number(group["equipment_token"])
        asset_class = classify_asset(equipment_text, name)
        profiles.append(
            AssetProfile(
                system="Historian",
                local_id=equipment_key,
                name=name,
                unit=unit,
                asset_class=asset_class,
                equipment_number=equipment_number,
                attributes={"tags": group["tags"], "descriptions": group["descriptions"]},
            )
        )
    return profiles


def load_cmms() -> list[AssetProfile]:
    """CMMS / MaintWorks: `equipment` list -- the hardest system to match
    (no shared numeric convention with the others).

    CMMS asset codes carry no equipment number, but maintenance notes
    sometimes mention a real equipment code (e.g. "Exchanger E-101",
    "valve CV-400") -- mine those as a fallback equipment_number signal.
    """
    data = _load_json(config.CMMS_DIR / "cmms_config.json")
    notes_by_asset = _load_cmms_notes_by_asset()
    profiles = []
    for eq in data.get("equipment", []):
        description = eq.get("description", "")
        functional_location = eq.get("functional_location", "")
        unit = extract_unit(functional_location)
        asset_class = classify_asset(description) or classify_from_location_suffix(
            functional_location
        )
        notes = notes_by_asset.get(eq["asset_code"], [])
        mentioned_number = extract_mentioned_equipment_number(notes, own_unit=unit)
        profiles.append(
            AssetProfile(
                system="CMMS",
                local_id=eq["asset_code"],
                name=description,
                unit=unit,
                asset_class=asset_class,
                equipment_number=mentioned_number,  # None unless found in notes
                criticality=normalize_criticality(eq.get("criticality_class")),
                attributes={
                    "functional_location": functional_location,
                    "bom": eq.get("bom"),
                    "pm_schedule": eq.get("pm_schedule"),
                    "work_order_notes": notes,
                    "equipment_number_source": "work_order_notes" if mentioned_number else None,
                },
            )
        )
    return profiles


def load_erp() -> list[AssetProfile]:
    """ERP: `assets` list."""
    data = _load_json(config.ERP_DIR / "erp_config.json")
    profiles = []
    for asset in data.get("assets", []):
        description = asset.get("description", "")
        functional_location = asset.get("functional_location", "")
        unit = extract_unit(functional_location)
        equipment_number = extract_trailing_number(asset["erp_asset_id"])
        asset_class = classify_asset(description) or classify_from_location_suffix(
            functional_location
        )
        profiles.append(
            AssetProfile(
                system="ERP",
                local_id=asset["erp_asset_id"],
                name=description,
                unit=unit,
                asset_class=asset_class,
                equipment_number=equipment_number,
                criticality=normalize_criticality(asset.get("criticality_class")),
                attributes={
                    "functional_location": functional_location,
                    "cost_center": asset.get("cost_center"),
                },
            )
        )
    return profiles


def load_all() -> dict[str, list[AssetProfile]]:
    """Stage 0: return each system's independent asset list."""
    return {
        "AM": load_am(),
        "APM": load_apm(),
        "DCS": load_dcs(),
        "Historian": load_historian(),
        "CMMS": load_cmms(),
        "ERP": load_erp(),
    }
