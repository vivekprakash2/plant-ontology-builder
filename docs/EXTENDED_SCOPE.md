# Extended Scope / Backlog

A running log of ideas, gaps, and future-work items surfaced while discussing and building the ontology
builder — things we've talked about adding but haven't (yet), plus a short record of alternatives we
considered and deliberately decided *against*, so they aren't re-litigated from scratch later.

> **Maintenance rule:** update this file every time we (a) discuss a piece of future work, a design gap, or
> an improvement idea without immediately implementing it, or (b) actually implement something that was
> previously listed here as open (move it out of "Open items" and into "Implemented" with a one-line pointer
> to where/how). Treat a conversation that surfaces a new gap or idea as incomplete until it's logged here.

---

## Open items

### Ingestion & onboarding extensibility

- **Generic, config-driven ingestion adapter.** Today each system has its own hand-written `load_X()`
  parser in `ontology_builder/ingest.py`, and `load_all()` is a hardcoded 6-entry dict — resolution itself
  (`resolution.py`) is fully system-agnostic (operates on `AssetProfile` fields regardless of source), but
  *onboarding a genuinely new 7th system* still requires writing a new parser function by hand, not just
  adding configuration. A declarative mapping (JSON-path/CSV-column → `AssetProfile` field, per system) would
  let a new system be onboarded via config instead of new Python.
- **Generalize the hard-FK pass beyond ERP↔CMMS.** `resolution.py`'s one hardcoded shortcut
  (`erp_cost_postings.linked_wo` → `cmms_workorders.wo_id` → `asset_code`) only knows about those two exact
  files/columns. A new system with its own genuine foreign key into an existing system wouldn't be picked up
  automatically — would need its own bespoke block today. Could be made declarative (list of
  `(source_file, source_col, target_file, target_col)` FK descriptors) instead.
- **Generalize transactional-record graph attachment.** `graph.py`'s `build_graph()` has one hardcoded
  loop per system's transactional CSV (alarms, work orders, operator actions, APM events, ERP postings) to
  turn rows into nodes/edges. A new system's transactional data won't appear in the graph until an equivalent
  loop is added by hand. Same declarative-mapping idea as ingestion would cover this too.
- **Rule-based chat fallback (`_ALIASES`/`_dispatch` in `agent.py`) needs manual updates for new
  equipment/systems.** Low priority — the agentic LLM tool-calling path (`list_assets` + friends) already
  handles new assets/systems with zero changes once they're in the graph; this only matters when no LLM
  provider is configured.

### Graph-RAG / query-system hardening

- **Content/text search over node properties.** Today retrieval is purely name/ID based (`list_assets` +
  exact/keyword matching) — there's no way to retrieve by *content* (e.g. "which work orders mention a
  shim kit issue") without already knowing which asset to look at. Works today only because the whole asset
  catalog is small enough to dump into context; won't scale. Would need a text/embedding index over node
  properties (`notes`, `symptom`, alarm descriptions, etc.).
- **A more generic traversal/filter tool for the agent.** The agentic tool set now has 6 tools (`list_assets`,
  `get_asset_context`, `get_historian_trend`, `get_related_assets`, `search_evidence`,
  `get_plant_status_summary`); `get_related_assets` still only exposes `FEEDS`/`COOLS`/`SUPPLIES_UTILITY`
  edges and there's still no generic "traverse N hops of any relationship type" tool — a generic N-hop
  traversal tool would remove the need for the hardcoded 2-hop logic currently hand-written in `agent.py`'s
  `_answer_high_dp`.
- **`get_entity_resolution_provenance(unified_id)` — surface merge confidence/review flags to the agent.**
  `needs_review`/`review_notes`/`confidence` already exist on every entity in `unified_entities.json` (from
  the low-signal-match safeguard, see `docs/ENTITY_RESOLUTION.md`) but the agent has no tool to read them —
  it can't currently hedge on "how sure are you these are the same physical asset" for a thin-evidence merge
  like CV-400. `list_assets` could be extended to include these fields, or a dedicated tool added.
- **`get_historian_value_at(tag, timestamp)` — point-in-time lookup.** `get_historian_trend` only reports
  first/last/direction over a window; there's no way to pull the exact reading at one specific moment (e.g.
  "what was vibration reading exactly when WO-4502 was opened") for tighter cross-system correlation.
- **`get_cost_impact(unified_id, since=...)` — financial-impact rollup.** ERP cost postings are ingested and
  linked to assets, but nothing surfaces "what has this problem cost so far" — a natural reliability-
  engineering follow-up question that the data already supports but no tool currently answers.
- **An externally-queryable API, not just the chat endpoint.** `/api/chat` (natural language) is the only
  public interface; there's no structured query endpoint for an analyst or another program to hit directly.
  `to_cypher()` is an export-only stub, not a live query engine.
- **Adjacency indexing in `KnowledgeGraph`.** `neighbors()` does a linear scan over all edges — fine at
  ~160 edges, would need real indexing (adjacency lists per node) if the graph grew to a genuinely larger
  multi-plant scale.
- **A formal, machine-readable ontology schema artifact.** Node/edge "classes" are currently just Python
  string labels (`"Asset"`, `"HAS_ALARM"`, etc.), documented in prose in `docs/ONTOLOGY.md` but not declared
  as an inspectable schema (JSON Schema/OWL/etc.) a judge or external tool could read without reading code.
  The hackathon rubric explicitly names "sensible classes, relationships" as a judged criterion, so this is
  more a presentation gap than a functional one.

### Storage / caching

- **Cache staleness check.** `pipeline.load_from_cache()`/`load_or_build()` currently trust the on-disk
  cache opportunistically with no check against `Data/*.csv`'s mtime/hash — explicit regeneration
  (`run_pipeline.py`/`run_graph.py`/`FORCE_REBUILD=1`) is required after any source-data or pipeline-code
  change, or the server will keep serving a stale cache indefinitely. Deliberately kept simple for now (see
  `docs/ONTOLOGY.md`'s caching section); worth revisiting if source data starts changing more frequently than
  by hand.
- **Optional real Neo4j import for a visual judge-facing explorer.** `to_cypher()` already exists as an
  export (`CREATE` statements). If a live, clickable graph explorer (Neo4j Browser/Bloom) is ever wanted for
  a demo, this is the path — not required for the pipeline to function, and nothing else depends on it.

### Frontend / UI

- **Redesigned once already (2026-07-29), but visuals/explorer usefulness are still open problems.**
  Original state: a fixed single-result dashboard (preset questions + summary card + timeline + evidence +
  relationship graph) modeled on `ui-prototype/`, felt too restrictive for open-ended questions — it
  implicitly steered toward the demo scenarios' shape rather than free exploration. First redesign
  (2026-07-29, see `docs/CHAT_AGENT.md` §4) replaced it with a two-pane layout: a real chat thread (left) +
  a persistent, always-browsable force-directed Plant Ontology Explorer (right, backed by a new
  `GET /api/graph` endpoint), with answers auto-highlighting their resolved entity/evidence in the graph.
  Verified functionally correct end-to-end (chat thread, category filters, pan/zoom, node inspector,
  auto-highlight-on-answer all work; both a scenario question and a genuine open-ended plant-wide question
  worked correctly).
  **However**, immediate follow-up feedback (same day): the visual design itself is considered
  "incredibly ugly", and the Plant Ontology Explorer specifically "doesn't look useful at all" — i.e. the
  *architecture* (chat thread + persistent graph, tied together via evidence-ref highlighting) may be sound,
  but the *visual design and the explorer's actual value to a user* are unresolved. Explicitly paused here
  per the user's request ("I want to work on this later") — no further redesign work was done past this
  point in this session. Whoever picks this up next should NOT treat the current implementation as a
  finished baseline to defend; treat it as a working skeleton whose visuals and explorer UX need a fresh
  pass. Possible angles worth considering next time (not yet explored): a fundamentally different visual
  style/design language (the current one is a direct carry-over of `ui-prototype/`'s Honeywell-red
  dashboard branding, never reconsidered for the new layout); whether a whole-plant force-directed node
  graph is even the right shape for "useful" ontology browsing versus something more structured (e.g. a
  hierarchical/tree view by unit, a table/list view with drill-down, or a simpler "asset cards" grid);
  and getting direct feedback on *why* the explorer doesn't feel useful (unclear value proposition? too
  cluttered? wrong information density? no clear task it helps with?) before iterating further blindly.
  Update `docs/CHAT_AGENT.md` §4 in the same turn as any future work here, per its standing maintenance rule.

### Other previously-noted ideas (carried over, still open)

(none currently -- the streaming/SSE idea below was implemented and moved to "Implemented".)

### Process-topology extraction: real-world prose sourcing

`topology_extraction.extract_topology_with_llm()` only accepts plain text (`prose_text: str`), sent straight
into a text-only prompt. `docs/SCENARIO.md` Sec 5b is a hackathon convenience — a real deployment is very
unlikely to have one clean paragraph like it describing the whole plant's flow. Two real gaps this exposes,
neither built:

- **Vision-based P&ID extraction.** The actual authoritative source of physical plant connectivity in real
  life is P&ID (Piping & Instrumentation Diagram) drawings, not prose — and those are diagrams (scanned
  PDF/CAD), not text. Reading equipment tags + tracing connecting lines from an actual diagram image would
  need a multimodal/vision-capable extraction path, not a drop-in change to the current text-only prompt.
- **Structured Line List ingestion (bypasses the LLM entirely).** Real engineering projects often produce a
  "line list" — a spreadsheet with `line_number, from_equipment, to_equipment, service` columns. This is
  *structured* data, not prose — if available, it should be ingested deterministically like any other
  system's CSV (more reliable than LLM-from-prose), not run through `extract_topology_with_llm()` at all.

---

## Implemented (moved out of "Open items")

- **On-disk cache for entities + graph** (`KnowledgeGraph.from_node_link_json()`, `pipeline.load_from_cache()`
  / `load_or_build()`, wired into `server.py` via `FORCE_REBUILD`) — was discussed as "how do we want to store
  the ontology output" / "should we cache it", implemented and verified (~0.03s cached load vs ~0.45s full
  rebuild). See `docs/ONTOLOGY.md`'s "Storage & caching design" section.
- **LLM-based process-topology extraction wired in as the primary path**, hardcoded `_PROCESS_TOPOLOGY`
  demoted to a fallback-only path (used when no LLM is configured or the prose file is missing) — was
  discussed as an extensibility gap against the rubric's "Ontology / graph design ... extensible" criterion,
  implemented via `topology_extraction.extract_topology_with_llm()` + `config.PROCESS_DESCRIPTION_PATH`, and
  verified end-to-end (7/8 edges extracted, all tagged `extracted_by="databricks-claude-opus-4-8"`). See
  `docs/ONTOLOGY.md`'s "Step 5" and "Autonomous process-topology extraction" sections.
- **`docs/ONTOLOGY.md`** itself — a plain-language explainer of the knowledge-graph/ontology stage (graph
  shape, caching, quirks), counterpart to `docs/ENTITY_RESOLUTION.md`.
- **`docs/CHAT_AGENT.md`** itself — a plain-language explainer of the chat-agent/reasoning stage, counterpart to
  `docs/ENTITY_RESOLUTION.md`/`docs/ONTOLOGY.md`.
- **`search_evidence(query, systems=[...])` — free-text search over record content**, added as a new agentic
  tool in `agent.py` (`_tool_search_evidence`, `_SEARCHABLE_NODE_LABELS`/`_SEARCH_FIELDS`). Substring-matches
  alarm/work-order/operator-action/health-event/cost-posting fields (notes, symptoms, alarm points,
  technicians, etc.), optionally scoped by system, capped at `max_results` (default 20, max 50). Verified:
  `search_evidence("shim kit")` correctly finds `WO-4471` (the only work order whose notes mention it).
- **`get_plant_status_summary()` — plant-wide (cross-asset) snapshot**, added as a new agentic tool in
  `agent.py` (`_tool_get_plant_status_summary`). Returns every ACTIVE alarm, every non-`Closed` work order,
  and recent APM health events across ALL assets (capped 30/30/15), each resolved to its owning asset via
  the new `_asset_for_record()` helper. Both tools' results are wired into `_primary_asset_id_from_trace()`
  (fallback asset resolution for the UI panel) and `_flatten_evidence()` (so their matches render as normal
  evidence cards/timeline entries) — see `docs/CHAT_AGENT.md` §1b/§3. Verified end-to-end against the real
  loaded graph (30 active alarms / 2 open work orders / 3 health events returned, correctly asset-attributed)
  and the full 13/13 test suite still passes.
- **Alarm limit/deadband configuration (`am_config.json`'s `limits`/`deadband` fields) is now ingested and
  exposed.** Found 2026-07-29 while checking tool coverage against `docs/TEAM_HANDBOOK.md` §7 Q5 (TK-201
  alarm flood) — previously dropped on the floor entirely (not in Stage 1 profiling, not attached to any
  graph node, not returned by any tool), so the agent could only infer a mis-set-limit conclusion
  *behaviorally* from alarm event timing, not cite the actual configured number. Closed the same day:
  `graph.py`'s `build_graph()` now creates one `AlarmConfig` node per configured alarm point (attached via a
  new `HAS_ALARM_CONFIG` edge, id = the `alarm_point` string), and `agent.py`'s deterministic
  `_answer_alarm_flood()` plus the agentic `get_asset_context`/`search_evidence` tools (automatically, via
  the existing generic graph-context plumbing — no new tool needed) now cite the actual configured
  limit/deadband as hard evidence. Live-verified after the fix: TK-201's answer now cites "H=78.5%, deadband
  0.1%" against an observed ~0.3% swing, both deterministically and agentically (the agentic answer also
  quoted the config's own `recommended_action` text, "suspected mis-set"). See `docs/ONTOLOGY.md` §3b and
  `docs/CHAT_AGENT.md` §6 for the full story. Full 13/13 test suite still passes.
- **Animated graph walk** (2026-07-29): `agent.py`'s `build_graph_walk()` (+ `_walk_step_for_tool_call()`)
  turns the answer's already-collected evidence/tool-call trace into an ordered `[{label, node_ids}, ...]`
  sequence (one step per tool call in agentic mode, exact call order; one step per gathered evidence item in
  deterministic mode), exposed as `panel.walk`. `frontend/app.js`'s `animateGraphWalk()` replays it as a
  step-by-step highlight across the Plant Ontology Explorer (each step's nodes pulse blue and the view pans
  to them, then fade to a trail) before settling into the existing `focusAnswerInGraph()` end state — this is
  what answers "where is the reasoning currently focused", raised while discussing whether a Neo4j/vis.js-
  style graph walk was feasible on this project's custom in-memory `KnowledgeGraph`. Verified end-to-end
  against the live server: a real question returned 6 correctly-ordered steps mapped to real graph node ids.
  **Explicitly not live/streaming** at the time — see the next entry, which closed that gap the same day.
  See `docs/CHAT_AGENT.md` §3/§4b.
- **Live streaming chat (plan + tool-call feed + live graph walk)** (2026-07-29): closes the "Streaming/SSE
  responses in the chat UI" idea above for real, not just the graph-walk replay. `agent.py`'s
  `_run_agentic_events()` (a generator; `_run_agentic` now just drains it for non-streaming callers) yields
  `plan`/`tool_call`/`tool_result` events live as the agentic loop runs, plus a new `write_plan` tool (model
  calls it first with a 3-6 step investigation plan, updates it as steps complete) so the UI can render a
  real checklist, not just an inferred one. `server.py`'s `/api/chat` is now a `text/event-stream` response
  (`stream_answer()`) instead of a single blocking JSON POST -- still single-threaded stdlib `HTTPServer`,
  but SSE only needs a kept-open connection with flushed writes, no async framework required.
  `frontend/app.js` reads the stream via `ReadableStream` (not `EventSource`, which can't send a POST body),
  rendering a live plan checklist + tool-call trace chips, and a new `liveWalkStep()`/`liveWalkFinish()` pair
  highlights graph nodes **as each tool call actually happens** rather than replaying afterward --
  `animateGraphWalk()`'s replay is now only a fallback for the deterministic (no-LLM) path, which has no
  live events to show. Verified end-to-end via both a raw SSE curl trace and in-browser screenshots: plan
  steps update live (pending → in_progress → done), trace chips accumulate correctly, and the explorer pans/
  highlights to the exact nodes each tool call touched, in real time. See `docs/CHAT_AGENT.md` §1b/§3/§4b/§7
  (a known caveat: streaming isn't atomic -- a mid-loop failure after some events were already sent can't be
  un-sent, so the client could rarely see a partial live trace followed by a mismatched deterministic-
  fallback final answer).

---

## Decisions made (considered, and deliberately NOT changed) — for reference, not re-litigated

- **No Neo4j (or other graph database) as the primary store.** Considered explicitly when discussing output
  storage. Decision: the actual query patterns here (1-2 hop lookups over a few hundred nodes) don't need a
  real graph database's capabilities, and running one would add a server process, driver dependency, network
  round-trips, and credentials to secure for zero query capability gained. Kept the lightweight in-memory
  `KnowledgeGraph` + JSON-file storage instead; `to_cypher()` remains available as an optional export if ever
  needed (see "Open items" above).
- **`server.py` auto-builds from source when the cache is missing, rather than refusing to start.**
  Considered requiring an explicit separate build step (fail fast with a clear error if no cache exists)
  versus auto-rebuilding inline. Decision: keep auto-build-on-missing-cache for a single-command demo
  experience, accepting that first-run/cache-miss startup time is unpredictable when a live LLM/embeddings
  provider is configured.
