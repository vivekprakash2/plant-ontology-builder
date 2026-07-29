#!/usr/bin/env python3
"""CLI entry point: run Stage 0 (ingest) + Stage 1 (entity resolution) and
print a human-readable summary of the unified entities.

Usage:
    python run_pipeline.py
"""
from ontology_builder.pipeline import run


def main() -> None:
    entities = run()

    print(f"Resolved {len(entities)} unified physical assets.\n")
    for entity in entities:
        conf = f"{entity['confidence']:.2f}" if entity["confidence"] is not None else "n/a"
        print(f"[{entity['unified_id']}] {entity['canonical_name']}  "
              f"(confidence={conf}, systems={entity['system_count']})")
        for member in entity["members"]:
            print(f"    - {member['system']:<10} {member['local_id']:<28} \"{member['name']}\"")
        if entity["evidence"]:
            print("    evidence:")
            for reason in entity["evidence"]:
                print(f"      * {reason}")
        print()

    print("Full detail written to output/unified_entities.json")


if __name__ == "__main__":
    main()
