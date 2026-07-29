# How the Ontology (Knowledge Graph) Is Built

This document explains, in plain language, how `output/unified_entities.json` (Stage 1's entity-resolution
output — see [`docs/ENTITY_RESOLUTION.md`](ENTITY_RESOLUTION.md)) becomes an actual **queryable knowledge
graph**: every alarm, work order, setpoint change, health event, cost posting, and historian tag attached to
the right physical asset, plus the plant's real process-flow topology layered on top. It also documents the
caching/storage design, every quirk, and the real bug/design decision behind each one.

> **Maintenance rule:** this file must be updated any time the ontology/graph logic changes —
> `ontology_builder/graph.py`, `ontology_builder/viz.py`, `ontology_builder/topology_extraction.py`, the
> `PROCESS_DESCRIPTION_PATH` config in `ontology_builder/config.py`, or the graph-building / caching parts of
> `ontology_builder/pipeline.py` (`run_with_graph`, `load_from_cache`, `load_or_build`). Treat a code change to
> any of these as incomplete until this doc reflects it. (Changes to *entity resolution itself* — `ingest.py`,
> `classify.py`, `resolution.py`, `open_vocab_classify.py`, `llm_provider.py`'s similarity/embedding code —
> belong in `docs/ENTITY_RESOLUTION.md` instead, not here.)


---

## The big picture

Entity resolution (Stage 0/1) answers "which records are the same physical asset?" and produces a flat list of
unified assets. That list alone can't answer "why is P-101 vibrating?" — you also need every *transactional*
record (alarms, work orders, setpoint changes, cost postings, sensor trends) attached to the right asset, and
you need the plant's actual physical flow (what feeds what) so a reasoning agent can walk cause-and-effect
across equipment, not just within one asset. That's what Stage 2/3 (`ontology_builder/graph.py`) builds:

1. **One `Asset` node per unified entity** from `unified_entities.json`.
2. **One node per transactional record**, read straight from the per-system CSVs, attached to its `Asset` via
   an edge — using the `(system, local_id)` join key that entity resolution already resolved (see
   `docs/ENTITY_RESOLUTION.md`'s "Where AI is actually used" — **none of this attachment step uses AI**; it's
   a deterministic dictionary lookup).
3. **Physical process-topology edges** (`FEEDS`/`COOLS`/`SUPPLIES_UTILITY`) between assets, representing the
   real plant flow line from `SCENARIO.md` Sec 5b — domain knowledge, not something entity resolution can or
   should infer from the data alone.

The result is a lightweight, dependency-free, **in-memory Python graph** (no Neo4j, no database server — see
"Why not a graph database?" below) that `ontology_builder/agent.py` (Stage 4) queries to answer causal
questions, and `ontology_builder/viz.py` renders as an interactive HTML page for humans to explore.

---

## The graph shape

```
(Asset) -[:HAS_ALARM]->            (AlarmEvent)
(Asset) -[:HAS_ALARM_CONFIG]->      (AlarmConfig)
(Asset) -[:HAS_WORK_ORDER]->       (WorkOrder)
(Asset) -[:HAS_SETPOINT_CHANGE]->  (OperatorAction)
(Asset) -[:HAS_HEALTH_EVENT]->     (HealthEvent)
(Asset) -[:HAS_COST_POSTING]->     (CostPosting)
(Asset) -[:HAS_HISTORIAN_TAG]->    (HistorianTag)
(CostPosting) -[:REFERENCES_WORK_ORDER]-> (WorkOrder)
(Asset) -[:FEEDS | :COOLS | :SUPPLIES_UTILITY]-> (Asset)
```

`KnowledgeGraph` (`graph.py`) is a plain dataclass-backed structure: `nodes: dict[str, Node]` and
`edges: list[Edge]`, where `Node = (id, label, properties)` and `Edge = (source, target, rel_type,
properties)`. No indexes, no query language — `neighbors(node_id, rel_type=None)` does a linear scan over
`edges`, which is fine at this dataset's scale (hundreds of nodes, not millions).

---

## Step 1: `Asset` nodes — one per unified entity

`build_graph(entities)` first creates one `Asset` node per entry in `unified_entities.json`, storing
`canonical_name`, `confidence`, and a `system_ids` map (`{"AM": "PMP-100-101", "APM": "Pump_001", ...}`) as
node properties. This is the graph's set of "hub" nodes everything else attaches to.

## Step 2: the reverse lookup — the one and only join key

`_build_reverse_lookup(entities)` scans every entity's `members` list and builds a single dict:
`(system, local_id) -> unified_id`. **This is the entire interface between entity resolution and the
graph** — every transactional CSV row carries a `(system, local_id)`-shaped reference (e.g. AM's
`equipment_ref`, CMMS's `asset_code`, DCS's `equipment_ref`, APM's `apm_id`, ERP's `erp_asset_id`), and that
reference is looked up in this dict to find which `Asset` node to attach the record to. **There is no
re-guessing of identity here — entity resolution has already done that work in Stage 1.** If a row's local ID
isn't in the lookup (e.g. it references equipment not in any unified entity), the record's node is still
created, but no `HAS_*` edge is added to it — it's just an orphan node (`add_edge` silently no-ops on a
missing source/target, see "Quirks" below).

## Step 3: attach every transactional CSV row

For each per-system transactional file — `am_alarm_events.csv`, `cmms_workorders.csv`,
`dcs_operator_actions.csv`, `apm_events.csv`, `erp_cost_postings.csv` — every row becomes one node (labeled
`AlarmEvent`/`WorkOrder`/`OperatorAction`/`HealthEvent`/`CostPosting` respectively) with its CSV columns as
properties, plus a `HAS_*` edge from the resolved `Asset`. ERP cost postings additionally get a
`REFERENCES_WORK_ORDER` edge to the CMMS work order named in `linked_wo`, if that work order node exists —
this is the same genuine hard FK that Stage 1 uses to force-link ERP↔CMMS (see
`docs/ENTITY_RESOLUTION.md` Sec 3d), now also expressed as a graph edge for traversal.

## Step 3b: `AlarmConfig` nodes — alarm-point CONFIGURATION, not events (added)

`am_config.json`'s per-alarm-point `limits` (HH/H/L/LL) and `deadband`/`rationalization` fields are
configuration, not transactional events, so they get their own node type/step, mirroring `HistorianTag`'s
"config metadata, not raw readings" pattern (Step 4 below) rather than being bundled into `AlarmEvent`. One
`AlarmConfig` node per configured alarm point (node id = the `alarm_point` string itself, e.g.
`"TNK-200-101.LVL"` — confirmed unique across the config file), attached to its asset via `HAS_ALARM_CONFIG`,
with `measurement`/`eng_unit`/`limit_hh`/`limit_h`/`limit_l`/`limit_ll`/`deadband`/`priority`/`cause`/
`consequence`/`recommended_action` properties.

**Why this was added (not in the original design):** without it, the reasoning agent (`agent.py`) could only
infer a mis-set-alarm-limit conclusion *behaviorally*, from `AlarmEvent` value/timing patterns alone (e.g.
"the value oscillates in a tight band right at some unknown threshold") — a correct qualitative answer, but
unable to cite the actual configured number as hard evidence the way it cites work order IDs or setpoint
values elsewhere. This was found and closed while checking tool coverage against
`docs/TEAM_HANDBOOK.md` §7 Q5 (TK-201 alarm flood) — see `docs/CHAT_RAG.md` §6 for the full story and
live-verified before/after answers. Confirmed real-world payoff: TK-201's actual configured deadband is 0.1%
against an observed level swing of ~0.3% (78.3%↔78.6%) around its 78.5% H limit — i.e. the deadband genuinely
is too tight relative to real process noise, not just "looks that way" from event timing alone.

## Step 4: Historian — tag metadata only, never the raw rows

`historian_timeseries.csv` is ~518k rows. **It is never materialized as graph nodes or edges.** Instead, for
every tag in `historian_config.json`, one lightweight `HistorianTag` node is created (`description`,
`eng_unit`, `min`, `max` as properties) and attached to its asset via `HAS_HISTORIAN_TAG`. The tag's asset is
found by taking the first 3 dot-separated segments of the tag name as the equipment key (e.g.
`FAC1.UNIT100.CENTRIFUGAL_PUMP_101.VIB_01` → `FAC1.UNIT100.CENTRIFUGAL_PUMP_101`) and looking that up in the
same reverse-lookup dict under `("Historian", ...)`.

Actual time-series values are never loaded into the graph at all. `historian_series(tag, start=None,
end=None)` streams `historian_timeseries.csv` row-by-row, filtering by tag (+ optional ISO8601 bounds), and
returns only the matching rows — this is what Stage 4's reasoning agent calls on demand to pull just the
trend it needs for one specific question, not the whole file.

## Step 5: physical process topology — LLM-extracted from prose, not hardcoded

**Primary path (default whenever an LLM is configured):** `build_graph(entities, prose_text, text_provider)`
calls `topology_extraction.extract_topology_with_llm()`, which reads unstructured prose describing the plant's
physical flow (crude feed → P-101 → E-101 → H-101 → Column C-101 → reflux drum V-201 → P-102 back to the
column; CV-400 supplies cooling water to K-101's lube-oil cooler and BFW/steam utilities to H-101) and asks
the model to propose `FEEDS`/`COOLS`/`SUPPLIES_UTILITY` edges directly — no human transcribes anything. Every
accepted edge is tagged `extracted_by="<model name>"` for provenance.

**Why this replaced hand-transcription:** a hardcoded, dataset-specific edge list cannot generalize to a
different plant, a different P&ID, or new equipment without a human rewriting Python — directly contrary to
the hackathon rubric's "Ontology / graph design ... extensible" criterion and its "AI must do the hard part"
ground rule (`docs/TEAM_HANDBOOK.md` Sec 8-9). `config.PROCESS_DESCRIPTION_PATH` (defaults to
`docs/SCENARIO.md`, overridable via env var) is the prose source `run_with_graph()` reads and passes through —
pointing this at a different deployment's own process description requires zero code changes.

**Fallback (no LLM configured, or the prose file is missing):** `_PROCESS_TOPOLOGY`, a hardcoded list of
`((system, local_id), (system, local_id), rel_type, note)` tuples transcribing the same flow line from
`docs/SCENARIO.md` Sec 5b, tagged `doc_source="SCENARIO.md Sec 5b"` instead of `extracted_by`. This keeps the
pipeline fully functional with zero installs/credentials — the same "never let a missing AI provider break
the deterministic path" philosophy used everywhere else in this codebase (open-vocab classification,
name-similarity scoring, LLM answer polishing). `build_graph()` picks exactly one source per run (LLM if a
real provider + prose are both available, hardcoded list otherwise) — it never merges both.

**Why extraction can't be inferred from the transactional data instead:** nothing in AM/APM/DCS/CMMS/ERP/
Historian records *what feeds what* — that's a fact about the physical plant, not about any system's records.
Entity resolution can prove two records describe the same asset; it cannot derive pipe connectivity from
attribute similarity. The prose (a scenario doc, P&ID narrative, or maintenance-log excerpt) is the only place
this fact exists in either the hardcoded or LLM-based approach.

Three non-obvious safety rules make the LLM path reliable (each was found via a real bug during testing, not
designed upfront):

1. **Grounding must include short human tags** (`P-101`, `K-101`, `CV-400`, "Column C-101", etc. — taken from
   `agent.py`'s `_ALIASES`), not just verbose `canonical_name`/member names. Prose refers to equipment by its
   short tag; grounding only on verbose names measured only 1/8 edges extracted correctly.
2. **Never index raw `local_id` values as bare aliases.** This dataset's `C-101` trap (AM's `local_id` for the
   *compressor* is literally `C-101`, the exact same string DCS uses for the *column*) caused an exact-match
   collision that resolved a column reference to the compressor. Only `agent.py`'s already-disambiguated
   `_ALIASES` are trusted for short-code grounding.
3. **Fuzzy matching is unsound for short `<Letter>-101`-style codes** — measured directly: `"C-101"` scores
   exactly 0.8 similarity against `"P-101"`, `"K-101"`, `"H-101"`, *and* `"E-101"` simultaneously (4 of 5
   characters identical regardless of which letter differs — the one character that matters most contributes
   least to the score). No threshold can break this 4-way tie. Fix: short-code-shaped strings are resolved by
   **exact match only**; fuzzy fallback is only used for longer prose-style names, where it's safe (e.g.
   "Crude Charge Pump 101" ~ "Crude Charge Pump A" = 0.90 with no other close competitor).

Verified end-to-end through `build_graph()`/`run_with_graph()` (not just the module in isolation) against the
real `docs/SCENARIO.md` prose: correctly extracts 6-7 of the 8 ground-truth edges per run (recall varies with
LLM non-determinism — one verification run got 7/8, tagged `extracted_by="databricks-claude-opus-4-8"`) with
**zero wrong-asset resolutions** across multiple runs — `V-201`/Reflux Drum is correctly never resolved (it
has no `_ALIASES` entry) rather than guessed. Full 13/13 regression suite still passes (those tests exercise
Stage 1 resolution only, unaffected by this Stage 2/3 change).

---

## Storage & caching design

### Why not a graph database (Neo4j, etc.)?

The actual query patterns this project needs — `context_for_asset()` (1-hop: everything directly attached to
one asset) and occasional fixed 2-hop chains (e.g. Column ← H-101 ← E-101 for the column-flooding scenario) —
don't need arbitrary variable-length pattern matching over millions of nodes with concurrent writers, which is
what a real graph database earns its keep on. This dataset is dozens of asset nodes plus a few hundred
transactional nodes (historian, deliberately, is never materialized as nodes at all — see Step 4 above).
Running Neo4j would add a server process to install/manage, a driver dependency, network round-trips replacing
in-process dict lookups, and credentials to secure — for zero query capability this project doesn't already
have via a plain Python dict/list structure.

`to_cypher()` exists as an **optional export** (`CREATE` statements for a quick Neo4j import) in case a real
graph database or visual explorer (Neo4j Browser/Bloom) is ever wanted for a demo — it's not required for the
pipeline to function, and nothing else in the codebase depends on it.

### The two JSON artifacts, and what each one is for

| File | Written by | What it contains | Can anything load it back in? |
|---|---|---|---|
| `output/unified_entities.json` | `pipeline.build_unified_entities()` | Stage 1 output only — the unified assets list | Yes — `graph.build_graph(entities)` takes this shape directly |
| `output/graph.json` | `KnowledgeGraph.to_node_link_json()` | The fully-built graph (all nodes + edges from Steps 1-5 above) | Yes — `KnowledgeGraph.from_node_link_json()` |
| `output/graph.html` | `viz.write_html()` | Self-contained interactive vis-network visualization (nodes/edges embedded inline, no CORS/file:// issues) | No — display-only, not re-parsed by any code |

`to_node_link_json()`'s format flattens each node's properties alongside its `id`/`label` keys (and each
edge's properties alongside `source`/`target`/`type`) — `from_node_link_json()` pops those reserved keys back
out before treating the rest as properties. This round-trips cleanly as long as no real property is ever
named `id`/`label`/`source`/`target`/`type` (true today; worth checking if a new record type/property is
added).

### The cache-aware entry points (`pipeline.py`)

- **`run()`** — Stage 0/1 only. Always re-reads `Data/*.csv`, re-runs resolution, writes
  `unified_entities.json`. Used directly by `run_pipeline.py`.
- **`run_with_graph()`** — full rebuild: calls `run()`, then `build_graph()`, then writes both
  `graph.json` and `graph.html`. **Always does a full rebuild from source** — this is the explicit
  "regenerate everything" entry point, used by `run_graph.py` and whenever `Data/*.csv` or the
  resolution/graph logic changes.
- **`load_from_cache()`** — loads *both* `unified_entities.json` and `graph.json` from disk and rehydrates a
  `KnowledgeGraph` via `from_node_link_json()`, touching **no** source CSVs and making **no** network calls.
  Returns `None` (never raises) if either file is missing or fails to parse — callers must treat that as
  "no usable cache," not an error.
- **`load_or_build(force_rebuild=False)`** — the normal-startup entry point (used by `server.py`): tries
  `load_from_cache()` first; on a miss (or if `force_rebuild=True`), falls back to `run_with_graph()`
  (which also (re)writes the cache as a side effect, so the *next* startup can hit it).

`server.py` calls `load_or_build()`, gated by the `FORCE_REBUILD` environment variable (unset / `"0"` /
`"false"` → use the cache if present; anything else → always force a full rebuild). **This means normal
server restarts do not re-run entity resolution or re-read `Data/*.csv` at all** once a cache exists — only
the very first run (or an explicit `FORCE_REBUILD=1`, or `Data/*.csv`/pipeline code changing) pays the full
cost. Measured: cached load ~0.03s vs ~0.45s for a full rebuild.

**Design choice (deliberately kept simple):** the cache is currently trusted opportunistically — there is no
staleness check comparing the cache's age against `Data/*.csv`'s mtime. If you edit `Data/*.csv` or the
resolution/graph logic, you must explicitly regenerate (`python3 run_pipeline.py && python3 run_graph.py`, or
run with `FORCE_REBUILD=1`) — the server will otherwise happily keep serving a now-stale cache indefinitely.
This tradeoff was chosen deliberately over auto-invalidation (see `TEAM_HANDBOOK.md`'s "keep it simple, your
stack is your choice" philosophy) but is worth revisiting if source data starts changing more frequently than
by hand.

**Non-obvious finding from verifying this:** because `EMBEDDING_MODEL` is a live-configured provider (see
`docs/ENTITY_RESOLUTION.md`), a *fresh* rebuild's `unified_entities.json` is not always byte-identical
run-to-run — live embeddings API calls have small run-to-run variance that can nudge a confidence score or
(rarely) which edges cross `MATCH_THRESHOLD`. This means the cache is not purely a performance optimization —
**it also makes the server's answers reproducible across restarts**, instead of silently re-deriving slightly
different entity clusters via a fresh live API call on every single startup.

### Build (offline) vs. serve (online) — a firm separation of concerns

Building the ontology (Stage 0-3: ingest → resolve → graph) is a **backend data-preparation step**, never
something the frontend triggers, shows progress for, or needs to know exists. The chat UI only ever calls
`/api/chat` and `/api/suggestions`, both of which operate purely on the already-built `_ENTITIES`/`_KG` kept
as module-level globals in `server.py` — there is no code path by which a user's question can trigger a
rebuild. The current explicit choice (documented above) is **auto-build-on-missing-cache**: `server.py` is
convenient to start from a clean checkout (`python3 server.py` just works), at the cost of unpredictable
first-run startup time when a live LLM/embeddings provider is configured. The alternative — refusing to start
without a pre-existing cache and requiring an explicit build step first — was considered and explicitly not
chosen, to keep a single-command demo experience.

---

## Every quirk, and the real reason it exists

| Quirk | Why it exists |
|---|---|
| `add_edge` silently no-ops if source or target node doesn't exist | Guards against a dangling edge from an unresolved `(system, local_id)` reference (e.g. a transactional row referencing equipment that isn't in any unified entity) rather than crashing the whole graph build over one bad row. |
| Historian is never materialized as graph nodes/edges beyond tag metadata | `historian_timeseries.csv` is ~518k rows — loading it into the in-memory graph would be wasteful for a query pattern (`historian_series()`) that only ever wants one tag's trend for one specific time window at a time. |
| Historian tag → asset lookup takes only the first 3 dot-segments of the tag name | Tag names are `FACILITY.UNIT.EQUIPMENT.SIGNAL` (4 segments); the equipment identity that Stage 1 resolved is only the first 3 — the 4th segment (`VIB_01`, `LUBE_01`, etc.) is the specific signal, not part of the equipment's identity. |
| Process topology is LLM-extracted from prose by default, not hardcoded | No system's records state "what feeds what" -- that's a fact about plant physical layout (from the PFD / prose description), not something entity resolution or attribute matching can infer; hardcoding it is also dataset-specific and non-extensible (fails the rubric's "extensible" criterion), so an LLM reads the prose instead. |
| `_PROCESS_TOPOLOGY` still exists as a hardcoded fallback | Keeps the pipeline fully functional with zero installs/credentials when no LLM is configured or the prose file is missing -- same philosophy as every other optional AI step in this codebase. |
| `graph.json`'s node/edge properties are flattened alongside reserved keys (`id`/`label`/`source`/`target`/`type`) | Keeps the node-link JSON format simple/human-readable for the vis-network export; `from_node_link_json()` must pop those reserved keys back out before treating the rest as properties — would silently corrupt data if a real property were ever named one of those. |
| The on-disk cache has no staleness check against `Data/*.csv` | Deliberate simplicity tradeoff — explicit regeneration (`run_pipeline.py`/`run_graph.py`/`FORCE_REBUILD=1`) is required after any source-data or pipeline-code change; the cache will otherwise serve stale results indefinitely. |
| A fresh rebuild is not always byte-identical to a previous one | Live embeddings API calls (when `EMBEDDING_MODEL` is configured) have small run-to-run scoring variance — discovered while verifying the cache, not something the cache itself causes. |

---

## Files involved

- `ontology_builder/graph.py` — `KnowledgeGraph` (nodes/edges, `neighbors()`, `context_for_asset()`,
  `to_node_link_json()`/`from_node_link_json()`, `to_cypher()`), `build_graph(entities, prose_text,
  text_provider)`, `_PROCESS_TOPOLOGY` (hardcoded fallback only), `historian_series()`.
- `ontology_builder/topology_extraction.py` — primary, LLM-based topology extraction
  (`extract_topology_with_llm`), used by `build_graph()` whenever a real LLM + prose are available.
- `ontology_builder/config.py` — `PROCESS_DESCRIPTION_PATH` (defaults to `docs/SCENARIO.md`, overridable via
  env var), the prose source for topology extraction.
- `ontology_builder/viz.py` — renders a `KnowledgeGraph` as a self-contained interactive HTML page
  (vis-network via CDN).
- `ontology_builder/pipeline.py` — `run()` (Stage 0/1 only), `run_with_graph()` (full rebuild incl. graph +
  viz), `load_from_cache()` / `load_or_build()` (the caching layer).
- `run_pipeline.py` — CLI entry point, Stage 0/1 only, always a full rebuild.
- `run_graph.py` — CLI entry point, full rebuild incl. graph, prints a smoke-test context dump for P-101.
- `server.py` — the only consumer of `load_or_build()`; keeps `_ENTITIES`/`_KG` as module-level globals for
  the server process's lifetime.
- `ontology_builder/agent.py` — Stage 4, the actual consumer of the graph at query time (`context_for_asset`,
  `neighbors`, `historian_series`) — see that module's docstring for reasoning-layer details, out of scope
  for this doc.
