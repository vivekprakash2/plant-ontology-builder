#!/usr/bin/env python3
"""CLI entry point: build the knowledge graph on top of the resolved
entities and print the cross-app context for Crude Charge Pump 101 (the
S1 finale scenario) as a smoke test.

Usage:
    python run_graph.py
"""
from ontology_builder.graph import historian_series
from ontology_builder.pipeline import run_with_graph


def main() -> None:
    entities, kg = run_with_graph()

    print(f"Graph built: {len(kg.nodes)} nodes, {len(kg.edges)} edges.")
    print("Full graph written to output/graph.json\n")

    pump = next(e for e in entities if e["canonical_name"] == "Crude Charge Pump 101")
    print(f"--- Context for {pump['unified_id']} ({pump['canonical_name']}) ---")
    context = kg.context_for_asset(pump["unified_id"])
    for label, records in context.items():
        print(f"\n{label} ({len(records)}):")
        for r in records:
            print(f"  {r}")

    vib_tags = [r["id"] for r in context.get("HistorianTag", []) if r["id"].endswith(".VIB_01")]
    if vib_tags:
        print(f"\nVibration trend sample ({vib_tags[0]}), last 5 readings before 2026-07-28:")
        series = historian_series(vib_tags[0], end="2026-07-28T00:00:00Z")
        for row in series[-5:]:
            print(f"  {row['timestamp']}  {row['value']} ({row['quality']})")


if __name__ == "__main__":
    main()
