# plant-ontology-builder

**AskEllie** — an AI-powered plant ontology builder for the Hindenberg Refinery hackathon
(*"Automation to Autonomy"*). Six plant IT systems (Alarm Management, APM, DCS, Historian,
CMMS, ERP) each know one slice of the truth about the same physical equipment, under six
different IDs with no shared key. This project stitches them into one knowledge graph with
AI entity resolution, then puts a reasoning agent + chat UI on top that answers
*"Why is Crude Charge Pump P-101 vibrating?"* with a cited, cross-system causal chain.

The story, data, and judging criteria live in [docs/SCENARIO.md](docs/SCENARIO.md) and
[docs/TEAM_HANDBOOK.md](docs/TEAM_HANDBOOK.md).

## Quickstart

Python 3.10+, **zero third-party dependencies** — everything runs on the standard library.

```bash
python server.py
# then open http://127.0.0.1:8000/
```

That's it. On first run the server builds the unified entities + knowledge graph from
`data/` and caches them under `output/`; later runs start instantly from the cache. Ask a
question in the chat (or click one of the seven starter questions) and watch the Reasoning
Walk pane highlight the graph nodes the agent touches.

Other entry points:

```bash
python run_pipeline.py        # Stage 0+1 only: print the resolved unified entities
python run_graph.py           # ...plus build the graph + output/graph.html visualization
python -m unittest discover -s tests   # regression tests (entity resolution + agent dispatch)
FORCE_REBUILD=1 python server.py       # ignore the output/ cache and rebuild from data/
```

## Enabling the AI paths (optional but recommended for the demo)

With no configuration, everything runs fully offline and deterministically (difflib name
similarity, keyword classification, hardcoded process topology, rule-based answers). To make
the AI load-bearing, copy `.env.example` to `.env` and fill in your Databricks credentials:

| Variable | Enables |
|---|---|
| `DATABRICKS_HOST` + `DATABRICKS_TOKEN` + `LLM_MODEL` | The agentic tool-calling reasoning loop, open-vocabulary classification, and LLM topology extraction from prose |
| `EMBEDDING_MODEL` | Real embeddings-based name similarity for Stage 1 entity resolution |

The server prints which mode it's in at startup (`Language model ready: ...` vs
`No language model available`), and every chat answer is labeled in the UI
(`🤖 reasoned by <model>` vs `rule-based`). In agentic mode the chat also supports
follow-up questions ("what about its work orders?") — the UI sends the last few turns
as context; the rule-based fallback answers each question independently.

> **Before a demo:** if you rebuild the cache with the LLM configured
> (`FORCE_REBUILD=1`), sanity-check the printed `topology_extraction` line and the extracted
> `FEEDS/COOLS/SUPPLIES_UTILITY` edges — an incomplete extraction degrades the multi-hop
> answers (see docs/CHAT_AGENT.md §5).

## How it's organized

| Path | What it is |
|---|---|
| `data/` | The six systems' source data (JSON config + CSV transactional) |
| `ontology_builder/` | The pipeline: ingest → entity resolution → knowledge graph → reasoning agent |
| `server.py` | Zero-dependency local chat server (`/`, `/api/chat` SSE, `/api/graph`) |
| `frontend/` | The AskEllie chat + graph UI (vanilla HTML/CSS/JS, no build step) |
| `tests/` | Regression tests: resolution traps (P-101 vs P-102, the C-101 collision) + agent dispatch |
| `output/` | Cached pipeline artifacts (`unified_entities.json`, `graph.json`, `graph.html`) |
| `docs/` | Deep-dive docs — see below |

## Documentation

- [docs/ENTITY_RESOLUTION.md](docs/ENTITY_RESOLUTION.md) — how cross-system identity is decided (Stage 1)
- [docs/ONTOLOGY.md](docs/ONTOLOGY.md) — how the knowledge graph is built (Stages 2–3)
- [docs/CHAT_AGENT.md](docs/CHAT_AGENT.md) — the reasoning agent, API, and frontend (Stages 3–4)
- [docs/EXTENDED_SCOPE.md](docs/EXTENDED_SCOPE.md) — backlog and deliberate non-goals
