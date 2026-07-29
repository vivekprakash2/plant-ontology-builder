"""Data model for a single per-system view of a physical asset (Stage 0 output)."""
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class AssetProfile:
    """One application's view of one physical asset.

    Six of these (at most, one per system) should eventually resolve to a
    single unified/physical asset in Stage 1.
    """

    system: str  # "AM" | "APM" | "DCS" | "Historian" | "CMMS" | "ERP"
    local_id: str  # the identifier used inside that system
    name: str  # human-readable name/description as used by that system
    unit: Optional[str] = None  # normalized unit number, e.g. "100"
    asset_class: Optional[str] = None  # normalized class, e.g. "Pump", "Compressor"
    equipment_number: Optional[str] = None  # e.g. "101", "102" if parseable
    criticality: Optional[str] = None  # normalized: "High" | "Medium" | "Low"
    attributes: dict[str, Any] = field(default_factory=dict)  # raw provenance data

    @property
    def key(self) -> str:
        """Globally unique key across all systems."""
        return f"{self.system}:{self.local_id}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "system": self.system,
            "local_id": self.local_id,
            "name": self.name,
            "unit": self.unit,
            "asset_class": self.asset_class,
            "equipment_number": self.equipment_number,
            "criticality": self.criticality,
            "attributes": self.attributes,
        }
