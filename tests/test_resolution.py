"""Regression tests for Stage 1 entity resolution.

Encodes the ground truth from SCENARIO.md / TEAM_HANDBOOK.md: which
(system, local_id) pairs must resolve to the same physical asset, and
which known traps must NOT be merged (P-101 vs P-102, the "C-101" code
used by both a compressor alias and the actual column).

Run with:
    python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import unittest

from ontology_builder.ingest import load_all
from ontology_builder.resolution import resolve


class TestEntityResolution(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        all_profiles = load_all()
        cls.result = resolve(all_profiles)
        cls.cluster_of: dict[str, frozenset] = {}
        for cluster in cls.result.clusters:
            cluster_set = frozenset(cluster)
            for key in cluster:
                cls.cluster_of[key] = cluster_set

    def assert_same_cluster(self, *keys: str) -> None:
        clusters = {self.cluster_of[k] for k in keys}
        self.assertEqual(len(clusters), 1, f"Expected one cluster for {keys}, got {clusters}")

    def assert_different_clusters(self, key_a: str, key_b: str) -> None:
        self.assertNotEqual(
            self.cluster_of[key_a],
            self.cluster_of[key_b],
            f"{key_a} and {key_b} should NOT resolve to the same asset",
        )

    # --- S1 finale asset: P-101 ---
    def test_p101_resolved_across_six_systems(self) -> None:
        self.assert_same_cluster(
            "AM:PMP-100-101",
            "APM:Pump_001",
            "CMMS:EQ-1042",
            "DCS:U100_P101",
            "ERP:ERP-FAC1-PMP-100-101",
            "Historian:FAC1.UNIT100.CENTRIFUGAL_PUMP_101",
        )

    def test_erp_cmms_link_for_p101_backed_by_hard_fk(self) -> None:
        forced_pairs = [{e.a, e.b} for e in self.result.edges if e.forced]
        self.assertIn({"ERP:ERP-FAC1-PMP-100-101", "CMMS:EQ-1042"}, forced_pairs)

    # --- Trap: P-101 vs P-102 (two similar centrifugal pumps in Unit 100) ---
    def test_p101_and_p102_not_merged(self) -> None:
        self.assert_different_clusters("APM:Pump_001", "APM:Pump_002")
        self.assert_different_clusters("CMMS:EQ-1042", "APM:Pump_002")
        self.assert_different_clusters("CMMS:EQ-1042", "Historian:FAC1.UNIT100.PUMP_102")

    # --- K-101 recycle gas compressor ---
    def test_k101_compressor_resolved(self) -> None:
        self.assert_same_cluster(
            "AM:C-101",
            "APM:Compressor_005",
            "CMMS:EQ-6001",
            "DCS:U100_K101",
            "ERP:ERP-FAC1-CMP-100-101",
            "Historian:FAC1.UNIT100.GAS_COMPRESSOR_101",
        )

    # --- Trap: the string "C-101" means compressor in AM but the column in DCS/Historian ---
    def test_c101_code_ambiguity_not_merged(self) -> None:
        self.assert_different_clusters("AM:C-101", "DCS:U100_C101")

    def test_column_c101_resolved(self) -> None:
        self.assert_same_cluster("DCS:U100_C101", "Historian:FAC1.UNIT100.COLUMN_101")

    # --- E-101 preheat exchanger ---
    def test_heat_exchanger_e101_resolved(self) -> None:
        self.assert_same_cluster(
            "APM:Exchanger_014",
            "CMMS:EQ-2210",
            "ERP:ERP-FAC1-HEX-100-101",
            "Historian:FAC1.UNIT100.HEAT_EXCHANGER_101",
        )

    # --- CV-400 cooling-water control valve ---
    def test_valve_cv400_resolved(self) -> None:
        self.assert_same_cluster(
            "CMMS:EQ-8001",
            "DCS:U400_V101",
            "ERP:ERP-FAC1-VLV-400-101",
            "Historian:FAC1.UNIT400.CONTROL_VALVE_101",
        )

    def test_erp_cmms_link_for_valve_backed_by_hard_fk(self) -> None:
        forced_pairs = [{e.a, e.b} for e in self.result.edges if e.forced]
        self.assertIn({"ERP:ERP-FAC1-VLV-400-101", "CMMS:EQ-8001"}, forced_pairs)

    # --- TK-201 tank ---
    def test_tank_tk201_resolved(self) -> None:
        self.assert_same_cluster("AM:TNK-200-101", "Historian:FAC1.UNIT200.STORAGE_TANK_101")

    # --- Sanity: no cluster should ever mix two different asset classes ---
    def test_no_cluster_mixes_asset_classes(self) -> None:
        for cluster in self.result.clusters:
            classes = {
                self.result.profiles_by_key[k].asset_class
                for k in cluster
                if self.result.profiles_by_key[k].asset_class
            }
            self.assertLessEqual(len(classes), 1, f"Cluster {cluster} mixes classes {classes}")

    # --- low_signal / needs-review flag ---
    def test_valve_cv400_flagged_low_signal(self) -> None:
        # DCS:U400_V101's own loop name ("Cooling Water Flow Control") doesn't
        # classify as Valve, so its edges to ERP/Historian rely on
        # unit+equipment_number without a confirmed class match -- exactly
        # the shape that a coincidental cross-system naming collision could
        # also produce (see repo memory: the "Aerator"/"SCADA" test). This
        # must be surfaced for review, not silently trusted.
        cluster = self.cluster_of["CMMS:EQ-8001"]
        low_signal = self.result.low_signal_edges(list(cluster))
        self.assertTrue(low_signal, "CV-400 cluster should have at least one low_signal edge")

    def test_p101_not_flagged_low_signal(self) -> None:
        # P-101 is backed by confirmed class matches (Pump=Pump) across every
        # system plus a hard FK -- it should never be flagged for review.
        cluster = self.cluster_of["AM:PMP-100-101"]
        low_signal = self.result.low_signal_edges(list(cluster))
        self.assertFalse(low_signal, f"P-101 cluster should have no low_signal edges, got {low_signal}")


if __name__ == "__main__":
    unittest.main()
