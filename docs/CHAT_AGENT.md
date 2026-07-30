# Chat / Reasoning Agent (Stage 3 "ask" + Stage 4 "reason")

Plain-language explainer of the chat agent stack — how a natural-language question turns into a
grounded, evidence-cited answer in the UI. Counterpart to [ENTITY_RESOLUTION.md](ENTITY_RESOLUTION.md)
(Stage 1) and [ONTOLOGY.md](ONTOLOGY.md) (Stage 2/3 graph) — this doc covers everything downstream of
the graph: the reasoning agent, the LLM providers, the HTTP API, and the frontend that renders it.

> **Maintenance rule (same discipline as the other two docs):** any change to `ontology_builder/agent.py`,
> the chat/tool-calling parts of `ontology_builder/llm_provider.py`, `server.py`'s `/api/chat` handling, or
> `frontend/app.js`'s answer-rendering logic must be reflected here in the same turn as the code edit — new
> quirks/bugs found go in "Quirks & gotchas", new fields/behavior go in the relevant section above it, not
> as an afterthought.

---

## 1. Two answer modes, chosen automatically

`ontology_builder/agent.py`'s `answer_question(question, entities, kg)` is the single entry point. It picks
one of two modes, entirely transparently to the caller (`server.py`):

```mermaid
flowchart TD
    Q[question] --> P{LLM provider configured?}
    P -- no --> D[_dispatch: deterministic rule-based routing]
    P -- yes --> A[_run_agentic: LLM tool-calling loop]
    A -- exception of any kind --> D
    D --> R[AgentAnswer]
    A -- success --> R
```

- **No LLM configured** (`get_text_generation_provider()` returns `NullTextGenerationProvider`) → always
  uses `_dispatch`, the deterministic path.
- **LLM configured** → tries `_run_agentic` first. **Any** exception (network error, malformed response,
  tool-calling unsupported by the model, timeout, etc.) is caught in `answer_question` and silently falls
  back to `_dispatch` — the chat must never hard-fail from an LLM hiccup.

This means the demo/system always answers something, and the UI never shows a raw stack trace.

**Streaming counterpart:** `stream_answer(question, entities, kg)` is the live-events version `/api/chat`
actually calls (see §3) -- same fallback discipline (agentic first, deterministic on any failure), but
yields `_run_agentic_events()`'s plan/tool_call/tool_result events as they happen instead of only returning
once at the end. `answer_question()` above still exists as the non-streaming form (drains the same generator
and returns just the final answer) for any caller that doesn't need live events.

### 1a. Deterministic fallback (`_dispatch`)

Rule-based, **not** an LLM — built to answer the exact scenarios in `docs/TEAM_HANDBOOK.md` Sec 7, but also
serves as the "no AI configured" safety net for arbitrary questions about the 8 known aliased assets.

1. **Resolve the asset(s).** `_match_aliases(question)` does keyword matching against `_ALIASES`, a small
   hardcoded list of `{name, anchor, keywords}` dicts. `anchor` is a `(system, local_id)` pair from Stage 1
   (e.g. `("AM", "PMP-100-101")`), **not** a hardcoded `unified_id` — because cluster numbering
   (`ASSET-001`, `ASSET-002`, ...) can shift between pipeline runs, but a system's own local ID for a piece
   of equipment never does. `_anchor_to_unified_id()` resolves the anchor to whatever `unified_id` that
   asset currently has in the just-loaded `entities` list.
2. **Route by keyword** in `_dispatch`: checks for `"same problem"`/`"same as"`, `"everything"`/`"show me"`,
   `"maintenance"` + `"last week"`/`"operations"`, `"vibrat"`, `"fuel"`, `"differential pressure"`/`"flooding"`/
   `" dp"`, `"alarm"` — first match wins, in that order. Falls through to `_answer_full_context` if nothing
   matches (i.e. any recognized asset with an unrecognized question still gets *something* useful back).
3. **Gather evidence** per handler via `KnowledgeGraph.context_for_asset(unified_id)` (all directly-connected
   alarms/work orders/operator actions/health events/cost postings) plus targeted `historian_series()`/
   `_trend()` calls for specific tags — **never** the whole ~518k-row historian file.
4. **Apply a small ranking/causal heuristic** (hand-written per scenario, see `_answer_vibration`,
   `_answer_fuel_rising`, `_answer_high_dp`, `_answer_same_problem`, `_answer_alarm_flood`,
   `_answer_full_context`, `_answer_maintenance_ops_join`) and return an `AgentAnswer` — natural-language
   `answer`, a Markdown `recommendation`, a short `headline`, a `confidence` tier string
   (`"high"`/`"medium-high"`/`"medium"`/`"low"`/`"n/a"`), and a structured `evidence` list so the UI can cite
   sources.

Key per-handler details worth knowing:

> **Evidence-gated since 2026-07-29:** the handlers originally asserted each planted scenario's scripted
> conclusion whenever their route matched — which produced confidently *wrong* answers on question
> variants ("Why are there so many alarms on P-101?" got S5's "nuisance alarm" verdict for 3 genuine
> vibration alarms; "Is P-102 the same problem as P-101?" replayed S4's lube-oil/CV-400 narrative for a
> pairing it doesn't apply to). Every conclusion below is now derived from records actually found in the
> graph, and `tests/test_agent_dispatch.py` locks in both the canonical questions and those variants.

- **`_causal_factors(entities, kg, unified_id)`** — the shared evidence collector behind the vibration and
  comparison handlers. Emits a factor **only when its supporting records exist**: a recent (<7d) operator
  setpoint *increase* on the asset's own loop; recent (<14d) seal work on the asset itself; a non-Closed
  work order on a `COOLS`/`SUPPLIES_UTILITY` upstream asset (corroborated by the asset's own rising
  lube-oil trend when it has a `LUBE_01` tag); or a falling own-suction-pressure trend (cavitation
  signature). Each factor carries its own sentence, evidence records, and recommendation line.
- `_answer_vibration`: reports the vibration trend, then composes causes from `_causal_factors`. The
  headline is picked from the factor *kinds* found: setpoint+seal → the S1 joint conclusion;
  `utility_cooling_problem` → "lube-oil overheating from upstream cooling problem" (this is how "why is
  the compressor vibrating?" reaches K-101's real cause); single factors get their own headlines; no
  factors → "no clear cause found" at low confidence.
- `_answer_fuel_rising`: walks one `FEEDS` hop upstream (e.g. heater ← exchanger) to check for exchanger
  fouling (falling outlet-temp trend + an open/closed cleaning work order).
- `_answer_high_dp`: needs a genuine **two-hop** traversal (Column ← H-101 ← E-101) to surface both
  contributing causes (upstream fouling cascading through the heater, *and* reflux-pump cavitation via
  falling suction pressure). The headline/recommendations only name the causes actually found — if the
  graph's topology is missing the reflux edge (see the cache warning below), it says "cold feed from
  upstream fouling" instead of claiming the full scripted chain.
- `_answer_same_problem`: fully generic comparison — runs `_causal_factors` for **both** assets
  independently and compares the factor-kind sets. Disjoint sets → "No — different root causes" (S4's
  expected answer, but equally correct for P-102-vs-P-101: seal+setpoint vs cavitation); overlapping →
  "Partly — they share …"; either side without evidence → "Inconclusive" at low confidence. No scripted
  verdict anywhere.
- `_answer_full_context` (the handbook's Q6 unification check): enumerates every record attached to the
  asset. It must tag each one with `_describe_record()`'s type key via `_CONTEXT_LABEL_TO_TYPE`, **not**
  the raw graph node label — passing `"AlarmEvent"` where `"alarm_event"` was expected silently dropped
  *every* record from the panel timeline/evidence and from the graph walk, so the one question whose point
  is "here's everything we unified across six systems" rendered as a bare sentence of counts with a 2-node
  walk (fixed 2026-07-29; `tests/test_agent_dispatch.py` now asserts the panel's dated events span all
  five transactional systems and the walk has ≥10 steps). `_CONTEXT_LABEL_TO_TYPE` is deliberately a
  *superset* of `_RECORD_LABEL_TO_TYPE` — it adds `HistorianTag` so "show me everything" lists an asset's
  tags, while diagnostic answers (which go through the base map) stay lean and show trends as charts
  instead. A companion test guards that lean side too.
- `_answer_alarm_flood`: **gated** on an actual flood signature (`_ALARM_FLOOD_MIN_TRANSITIONS = 15`; the
  planted TK-201 flood has 120 transitions, every genuine alarm in the data has ~3). Below the gate it
  reports the alarms as genuine — quantifying how far past the configured H limit the values actually went
  — and, for vibration alarms, hands off to `_answer_vibration` so the user gets the real root cause. Only
  above the gate does it make the nuisance/mis-set-limit case, citing the configured limit + deadband.
- `_trend(tag, window_hours=...)` **window choice matters**: 48h default works for fast-moving signals
  (vibration), but slow trends (fouling outlet-temp, lube-oil temp) need `24*14` (14 days) or they falsely
  look "stable". This was a real bug found earlier in the project (see repo memory).
- `NOW = 2026-07-28T06:00:00+00:00` is a fixed constant (not `datetime.now()`) — the scenario has a fictional
  "current" date baked into the sample data, and all "N hours/days ago" phrasing must be computed against
  it, not wall-clock time.

### 1b. Agentic mode (`_run_agentic` / `_run_agentic_events`)

The LLM is given **read-only tools** to query the knowledge graph itself, rather than being handed a
pre-baked answer to polish. This is the "expose the graph via tools, build an agent that reasons" pattern
(handbook Stage 4).

**Tools exposed** (`TOOL_SCHEMAS`, OpenAI tool-calling JSON schema shape):

| Tool | Purpose | Notes |
|---|---|---|
| `write_plan` | Write/update a step-by-step investigation plan (list of `{text, status}`), shown live to the user as a checklist | UI-only — doesn't query the graph. The system prompt instructs the model to call this FIRST, then again whenever a step's status changes. Handled inline in the loop (not via `_TOOL_EXECUTORS`) since it just updates `plan_steps`, not the KG |
| `list_assets` | Every physical asset, canonical name, per-system IDs | Model calls this first if it doesn't know the exact `unified_id` |
| `get_asset_context` | All alarms/alarm-point configuration/work orders/operator actions/health events/cost postings/historian tags connected to one asset | Wraps `KnowledgeGraph.context_for_asset`; adds a human `time_ago` string per record. Includes `AlarmConfig` records (configured HH/H/L/LL limits + deadband, see §6) alongside `AlarmEvent` |
| `get_historian_trend` | Direction + %change of one tag, **always for both a 48h and a 336h window** (never raw rows) | Wraps `_trend_with_comparison()`; the requested window is top-level, the other under `other_windows`, plus a `trend_note` when they disagree. One file pass. See §5 for why both are forced |
| `get_related_assets` | `FEEDS`/`COOLS`/`SUPPLIES_UTILITY` process-flow relationships | Enables multi-hop causal reasoning across equipment |
| `search_evidence` | Free-text substring search over alarm/work-order/operator-action/health-event/cost-posting fields (notes, symptoms, alarm points, technicians, etc.), optionally scoped by `systems` | Added to unblock open-ended, content-first questions not anchored to a known asset (e.g. "which work orders mention a shim kit"). Each match includes its resolved `asset_id`/`asset_name` via `_asset_for_record()`. Capped at `max_results` (default 20, max 50) |
| `get_plant_status_summary` | Plant-wide snapshot: every ACTIVE alarm, every non-`Closed` work order, recent APM health events, across ALL assets | Added for "what's going on right now" questions that aren't about one specific asset. Capped (30/30/15) — a snapshot, not an exhaustive dump |

`search_evidence`/`get_plant_status_summary` results feed into `_primary_asset_id_from_trace()` (as a lower-
priority fallback after `get_asset_context`/`get_related_assets`) and `_flatten_evidence()` (so their matches
show up as ordinary evidence cards/timeline entries) — see §3.

**Loop** (`_run_agentic_events`, a generator; `_run_agentic` just drains it for the non-streaming form; max
`_MAX_AGENT_TURNS = 12` turns — raised 6 → 7 when `write_plan` was added (a mandatory first plan call uses
part of the budget), then 7 → 12 after live-testing found a genuine 2-hop question (Column dP ← H-101 ←
E-101 fouling, the S3 cascade) hit "inconclusive within the tool-call budget" despite the model having
already gathered all the correct evidence — `write_plan` update calls each consume a full turn just like a
real tool call, and a multi-hop question needs genuinely sequential calls (can't look up H-101's own
upstream neighbor E-101 until `get_related_assets(H-101)` has already returned), which can't be batched into
fewer turns the way independent lookups can):
1. Seed `messages` with `_AGENT_SYSTEM_PROMPT` (rules: call `write_plan` first and keep it updated, always
   use tools before answering, never invent facts/IDs/numbers, cite specific evidence, consider
   upstream/downstream assets, use short/long historian windows appropriately, say so explicitly if evidence
   is inconclusive) + the user's question.
2. Call `provider.chat(messages, tools=TOOL_SCHEMAS, max_tokens=1600)`.
3. For each `tool_call` in the response: if it's `write_plan`, normalize+store the steps
   (`_normalize_plan_steps`) and `yield {"type": "plan", "steps": [...]}` — no KG query, no walk step.
   Otherwise, `yield {"type": "tool_call", "tool", "arguments", "label"}` (a human-readable "doing X" label
   via `_tool_call_label()`) **before** executing, so a live UI can show "in progress" the moment the model
   decides to look something up; then execute via `_TOOL_EXECUTORS`, append the result to `trace` +
   `messages` (via `_trend_result_for_llm()`, which **strips the `points` array** — the chart-only
   per-minute downsampled series — before sending the trend result back to the model; the model only needs
   summary stats, and the raw points would waste a large slice of the token budget for no reasoning benefit),
   and `yield {"type": "tool_result", "tool", "walk_step"}` (`walk_step` from `_walk_step_for_tool_call()` —
   see §3's graph-walk section — or `None` if this call touched no resolvable node).
4. If a turn's response has no `tool_calls`, it's the final answer: parsed by `_split_agent_response()` into
   `(headline, root_cause, recommendation)`, yielded as one final `{"type": "final", "answer": AgentAnswer,
   "plan": [...]}` event.
5. If the loop exhausts all turns without a final answer, yields a low-confidence "inconclusive within the
   tool-call budget" final event (still includes the full `trace` as evidence).

**Markdown output format, matched to the question type** (enforced only by prompt instruction, not by a
schema/grammar):
```
## Headline                     <- always
<= 12 words, no trailing period — shown as the big bold UI title

## Root Cause                   <- diagnostic questions ("why is X", "what caused Y")
   ...or...
## Summary                      <- informational/lookup questions ("show me everything about X")
3-5 sentences, cites specific evidence (work order IDs, action IDs, tag names)

## Recommended Actions          <- ONLY when diagnostic, or the evidence genuinely calls for action;
2-4 short bullet points            omitted entirely for pure lookup/status questions
```
> **Why the conditional sections** (changed 2026-07-29): the prompt previously demanded all three
> headings on *every* answer, so a pure lookup like "show me everything about P-101" was pushed into
> asserting a "Root Cause" and inventing "Recommended Actions" it had no basis for. The frontend already
> renders each section conditionally (`data.recommendation` / non-empty evidence / non-empty charts), so
> omitting a section server-side is all that was needed for the UI to drop it cleanly.

`_split_agent_response()` (regex on `^#{1,3}\s*<Heading>\s*$`, `re.MULTILINE`) parses this; the body
heading regex accepts `Root Cause`, `Summary`, or `Analysis`. **It never
raises** — any section it can't find just becomes `None` (recommendation) or falls back to "put everything
in root_cause" (if even the body heading is missing). This graceful degradation matters because prompt-only
formatting is not guaranteed; a model that ignores the format still produces a usable (if less structured)
answer instead of breaking the response.

`confidence` for agentic answers is always the literal string `"model-reasoned"` — a distinct tier from the
deterministic path's `"high"/"medium-high"/"medium"/"low"`, since there's no rule-based confidence score to
report. The frontend gives it its own purple badge (see §4).

`presented_by` is set to the model's name (e.g. `"databricks-claude-opus-4-8"`) for both modes when an LLM
is involved; `"rule-based"` when it isn't. The UI badge (`🤖 reasoned by <model>` vs `rule-based`) is the
only visible signal of which path actually answered a given question.

---

## 2. LLM providers (`ontology_builder/llm_provider.py`)

Two independent provider interfaces, each with the same "configured-but-broken should never crash the
pipeline" fallback philosophy:

- **`SimilarityProvider`** (`OfflineSimilarityProvider` difflib default, `EmbeddingSimilarityProvider` real
  embeddings) — used by **Stage 1 entity resolution**, not directly part of chat, but shares this module.
  See [ENTITY_RESOLUTION.md](ENTITY_RESOLUTION.md) for its details.
- **`TextGenerationProvider`** — used by chat/reasoning (this doc). Three implementations:
  - `NullTextGenerationProvider` — no model; `generate()`/`chat()` both raise `NotImplementedError`. This is
    what makes `answer_question` pick `_dispatch`.
  - `OpenAICompatibleTextGenerationProvider` — any OpenAI-compatible `/chat/completions` endpoint: OpenAI,
    Azure OpenAI, or (the one actually configured in this project) **Databricks Model Serving**
    (`https://<workspace-host>/serving-endpoints/chat/completions`). Implements both `generate()` (simple
    system+user prompt → text) and `chat()` (full messages + optional `tools`, returns the raw assistant
    message dict so the agentic loop can append it directly). Uses stdlib `urllib` only, no SDK dependency.
  - `MLXTextGenerationProvider` — local Apple Silicon model via `mlx-lm`. **Strictly opt-in** via
    `LOCAL_LLM_PROVIDER=mlx` env var — never attempted automatically, because a Metal initialization failure
    is a native `SIGABRT` that Python's `try/except` cannot catch and would take down the whole server
    process (see §5 "Quirks & gotchas").
- **Factory** `get_text_generation_provider()` — module-level cached singleton. Preference order:
  Databricks/OpenAI/Azure (if credentials present) → local mlx (only if explicitly opted in) → Null. Catches
  `RuntimeError` from a misconfigured OpenAI-compatible provider and `ImportError` from a missing mlx-lm
  install, falling through each time rather than raising.

### Environment variables that control the chat LLM

Read from real environment variables or a local, gitignored `.env` (auto-loaded by both `server.py` and
`ontology_builder/config.py` — see the "MAJOR METHODOLOGY BUG" entry in repo memory for why **both** load it):

| Variable | Purpose |
|---|---|
| `DATABRICKS_HOST` / `DATABRICKS_SERVER_HOSTNAME` | Workspace host (bare hostname or full URL, both accepted) |
| `DATABRICKS_TOKEN` | PAT — **never** print/log this; only report `len(value)` or presence/absence when debugging |
| `LLM_MODEL` / `DATABRICKS_MODEL` | Chat/tool-calling serving-endpoint name, e.g. `databricks-claude-opus-4-8` |
| `OPENAI_API_KEY` / `OPENAI_BASE_URL` / `OPENAI_MODEL` | Generic OpenAI-compatible alternative |
| `AZURE_OPENAI_API_KEY` / `AZURE_OPENAI_ENDPOINT` | Azure OpenAI alternative |
| `LOCAL_LLM_PROVIDER=mlx` | Opt-in to local mlx-lm (Apple Silicon only) |
| `LOCAL_LLM_MODEL` | mlx model repo id, default `mlx-community/Qwen2.5-3B-Instruct-4bit` |

Verified end-to-end and currently live: `DATABRICKS_HOST` + `LLM_MODEL=databricks-claude-opus-4-8` +
`DATABRICKS_TOKEN` in the real `.env`. Databricks-hosted Anthropic models reject `temperature`/
`response_format` on this endpoint shape, so `chat()` only sets `temperature` for non-`databricks-`-prefixed
model names.

---

## 3. HTTP layer (`server.py`)

Stdlib-only (`http.server.ThreadingHTTPServer`, no Flask/FastAPI) — binds `127.0.0.1:8000` only
(never `0.0.0.0`). Loads `unified_entities.json` + the graph once at startup via
`ontology_builder.pipeline.load_or_build()` (cache-first, see [ONTOLOGY.md](ONTOLOGY.md) §caching), and
warms up the LLM provider **on the main thread before serving requests** (mlx-lm's Metal init isn't safe to
trigger lazily from a request-handling thread).

**Routes:**
| Route | Method | Purpose |
|---|---|---|
| `/`, `/index.html` | GET | Chat UI HTML |
| `/app.js`, `/style.css`, `/logo.svg` | GET | Static frontend assets |
| `/api/suggestions` | GET | Returns the 7 demo questions from `docs/TEAM_HANDBOOK.md` Sec 7 (`SUGGESTED_QUESTIONS` constant) |
| `/api/graph` | GET | Whole plant ontology (`KnowledgeGraph.to_node_link_json()`) for the persistent explorer panel (§4b) |
| `/api/chat` | POST | `{"question": "...", "history": [{"question", "answer"}, ...]?}` → `text/event-stream` of live plan/tool_call/tool_result events, ending with one `final` event carrying the full answer + evidence + UI panel JSON (same shape `/api/chat` always returned, before this was streamed) |

**`/api/chat` request handling (input validation, OWASP-relevant):**
- `Content-Length` must be present, `> 0`, and `<= MAX_BODY_BYTES` (32768; raised from 4096 when the
  optional `history` array was added) — rejects oversized/missing bodies with 400 before reading the body.
- Body must parse as JSON (`json.JSONDecodeError`/`UnicodeDecodeError` → 400).
- `question` must be a non-empty string; trimmed and truncated to `MAX_QUESTION_LEN` (500 chars).
- `history` (optional, for agentic follow-ups): must be a list or it's discarded; hard-capped at 8 entries
  server-side, then `agent._history_messages()` keeps only the last 4 valid `{question, answer}` string
  pairs, truncating each side to 2000 chars — hostile/malformed history degrades to no history, never an
  error. **The deterministic fallback ignores history entirely** (stateless per question), so follow-ups
  that rely on pronouns only resolve in agentic mode; the system prompt (rule 6b) tells the model to treat
  prior answers as context for reference resolution, never as evidence — facts must be re-verified via
  tools before being cited.
- No SQL/shell/file-path use of the question anywhere in the call chain — it only ever feeds into Python
  string `.lower()`/`in` keyword checks (`_dispatch`) or gets sent as an LLM prompt message (`_run_agentic`),
  so there's no injection surface in the traditional sense; the main risk is prompt injection via question
  content (see §5).
- Responses set `X-Content-Type-Options: nosniff`. No secrets are ever included in a response — `evidence`/
  `panel`/`answer` are all derived from local CSV-sourced data or LLM output, never environment variables.

**Response shape** (the `final` SSE event's `data:` payload — see "Streaming (SSE)" below for the events
that precede it):
```json
{
  "type": "final",
  "asset": "P-101" ,
  "scenario": "vibration" ,
  "answer": "...",
  "headline": "...",
  "recommendation": "- ...",
  "raw_answer": null,
  "presented_by": "databricks-claude-opus-4-8",
  "confidence": "model-reasoned",
  "truncated": false,
  "evidence": [ ... ],
  "panel": { "entity": ..., "entity_id": "ASSET-002", "relationships": [...], "timeline": [...], "evidence": [...], "charts": [...], "walk": [...] },
  "plan": [ {"text": "...", "status": "done"}, ... ]
}
```

### Streaming (SSE) — `stream_answer()` → `/api/chat`

`/api/chat` is a `text/event-stream` response, not a single JSON blob: `server.py`'s `do_POST` iterates
`ontology_builder.agent.stream_answer(question, entities, kg)` and writes one `data: <json>\n\n` line per
yielded event, flushing after each so the browser receives them as they happen (not buffered until the
connection closes). Event shapes (see §1b for where each is yielded):

| `type` | When | Payload |
|---|---|---|
| `plan` | Agentic mode calls `write_plan` | `{"steps": [{"text", "status"}, ...]}` |
| `gate` | The completion gate rejected a final answer (§5) | `{"label", "unexamined": [...]}` — advisory/audit only; the frontend ignores unknown types, and the user sees the effect as the extra lookup the agent then performs |
| `tool_call` | Right before a (non-plan) tool executes | `{"tool", "arguments", "label"}` — `label` is a human-readable "doing X" string |
| `tool_result` | Right after a tool executes | `{"tool", "walk_step"}` — `walk_step` (`{"label", "node_ids"}` or `null`) is what the frontend uses for live graph-walk highlighting (§4b) |
| `final` | Always exactly one, last | The full response shape above |

Deterministic-fallback answers (no LLM configured, or the agentic loop failed) skip straight to a single
`final` event — there's nothing to stream for an instant, non-tool-calling answer. `stream_answer()` never
raises (same discipline as `answer_question()`); `server.py` sends response headers once up front (status
can't change mid-stream) and catches `BrokenPipeError`/`ConnectionResetError` if the client navigates away
mid-stream.

**Frontend consumption** (`frontend/app.js`'s `submitQuestion()`): reads `res.body` via a `ReadableStream`
reader (not `EventSource`, which doesn't support POST bodies), splits on `\n\n`, and dispatches each parsed
event — `plan` re-renders the checklist (`renderPlanChecklist()`), `tool_call` appends a trace chip
(`addTraceStep()`), `tool_result` marks it done and calls `liveWalkStep()` for live graph highlighting, and
`final` renders the completed assistant bubble (`renderAssistantBubble()`) and settles the explorer —
`focusAnswerInGraph()` if any live events occurred, or `animateGraphWalk()`'s post-hoc replay if the answer
came from the deterministic fallback (which has no live events to show).

**Conversation history** (`chatHistory` in `app.js`): after each `final` event the frontend records
`{question, answer}` (answer = headline + answer + recommendation, truncated to 2000 chars) and sends the
last 4 turns with every subsequent question — this is what lets agentic follow-ups like "what about its
work orders?" resolve "it". History lives only in the page's JS state: a refresh clears it, and the server
keeps no per-session state at all.

### `build_ui_panel()` — normalizing two different evidence shapes

Deterministic `_dispatch` evidence is a flat list of typed records (`{"type": "historian_trend", ...}`).
Agentic evidence is a list of `{"type": "tool_call", "tool": ..., "arguments": ..., "result": ...}` entries.
`_flatten_evidence()` normalizes both into one common flat-record list (unpacking `get_asset_context`,
`get_historian_trend`, `search_evidence`, and `get_plant_status_summary` tool results — `list_assets`/
`get_related_assets` results aren't temporal records, so those are skipped) so a single `_describe_record()`
+ `build_ui_panel()` pipeline can build:
- **`relationships`**: entity's per-system aliases + `FEEDS`/`COOLS`/`SUPPLIES_UTILITY` graph neighbors.
- **`timeline`**: every describable record with a timestamp, chronologically sorted, one line each (source
  tag: AM/CMMS/DCS/APM/ERP/HIST).
- **`evidence`** (cards): same records **except** `historian_trend` type — those are deliberately excluded
  here because they're shown richer in `charts` instead (see below); they're still included in `timeline`
  since a one-line chronological mention there is still useful alongside other cross-system events.
- **`charts`**: up to 3 distinct historian tags actually cited as evidence for *this specific answer* (no
  per-scenario hardcoding) — each with downsampled `points` (see `_downsample_points`, max 120, always keeps
  first+last), direction, %change, tag label/unit looked up from the tag node's properties.

Returns `None` entirely if no asset could be resolved for the answer (e.g. the model never called
`get_asset_context`/`get_related_assets`) — the frontend handles a `null` panel gracefully.

### `build_graph_walk()` — the ordered node-visit sequence for the animated explorer replay

A second, distinct pass over `answer.evidence` (not derived from the flattened/sorted `timeline`/`evidence`
above) that preserves **call-level granularity and order**, for the frontend's `animateGraphWalk()` (§4b) to
replay as a step-by-step highlight instead of one instant snapshot:
- Agentic mode: one step per tool call, in the exact order the LLM made them. `_walk_step_for_tool_call()`
  maps each tool's arguments/result to the real graph node ids it touched (e.g. `get_asset_context` → the
  asset plus every attached record id it returned; `get_historian_trend` → the tag; `get_related_assets` →
  the asset plus every related asset id; `search_evidence`/`get_plant_status_summary` → the matching record
  ids). `list_assets` is skipped (it touches every asset — not a meaningful single step). A call that
  touched no resolvable node (error/empty result) is also skipped.
- Deterministic mode: one step per evidence item `_dispatch`'s handler gathered, in gathering order —
  reuses `_describe_record()`'s `ref` extraction, same as `build_ui_panel()`'s `timeline`/`evidence` above.

Always starts with a `{"label": "Start at <entity>", "node_ids": [entity_id]}` step. Returns `[]` if no
asset was resolved (nothing to walk). Exposed as `panel.walk`.

---

## 4. Frontend (`frontend/`)

> **Status: redesigned twice on 2026-07-29.** First from a fixed single-result dashboard (modeled on
> `ui-prototype/`) to a conversation-style console + persistent whole-graph explorer; then — after user
> feedback that the visuals were ugly and the always-on whole-graph explorer wasn't useful — restyled to
> match `design_mockups/frontend_redesign_v3.html` and restructured so the graph pane is *scoped*: an
> always-bounded **Ontology Overview** (schema level) plus a **Reasoning Walk** that only ever shows the
> subgraph the current answer actually touched. The category-filter "browse everything" mode was removed
> deliberately. The `/api/chat` JSON contract in §3 is unchanged throughout (additive fields only:
> `panel.entity_id`, per-entry `ref` ids, `panel.walk`); `GET /api/graph` still backs the graph pane.

Vanilla HTML/CSS/JS, no build step, no framework. Layout: compact topbar (Ellie logo + "AskEllie" + theme
toggle), then a two-pane split — chat left (38%), graph right — separated by a drag-to-resize splitter
whose handle also collapses the graph pane entirely (it becomes a slim reopen tab on the right edge).

- **Left: `.chat-panel`** — a running conversation (`#chatThread`) with a "Chat with Ellie · N turns"
  header strip. Before the first question the thread shows a **starter panel** ("Hi, I'm Ellie 🐘" + the
  seven `/api/suggestions` demo questions as a single-column list of tappable cards); it's removed on the
  first submit. The composer is an underline-style input + red Ask button — Enter submits, Shift+Enter
  inserts a newline.
- **Right: `.explorer-panel`** — two tabs. **Ontology Overview** (the default): a bounded, type-level
  schema diagram. **Reasoning Walk**: empty ("No reasoning walk yet") until a question is asked, then
  shows only the answer's subgraph.

**Security posture (unchanged):** all dynamic content — chat bubbles, Markdown, timeline/evidence cards,
SVG trend charts, and every graph node/edge/inspector field — is built via `createElement`/`textContent`/
`createElementNS`+`setAttribute` only, **never** `innerHTML` with server/LLM-derived content, since answer
text ultimately comes from live LLM output.

### 4a. Chat thread

- **`renderMarkdown(container, text)`**, `extractHeadline()`, `CONFIDENCE_SCORE`/`confidenceClass()`, and
  the Sensor Trend `<svg>` builder (`buildTrendChartSvg()`/`buildTrendChartCard()`) carry over from the
  first redesign, re-homed into each assistant bubble.
- Each assistant card (`renderAssistantBubble()`): Ellie avatar (`/logo.png`) + name → headline → **one
  row of provenance pills** (`.assistant-meta`): the resolved asset (red dot + name), the outlined
  confidence pill, and a monospace model pill — `⚙ rule-based`, or `🤖 <model>` tinted violet
  (`.meta-model.is-ai`) when a language model actually reasoned the answer, so the agentic path is
  identifiable at a glance on stage. This replaced a muted run-on sentence (`asset: X · rule-based`) plus
  a separately-wrapping confidence badge, which stacked into three ragged rows → then **all sections
  inline under uppercase `.section-label` headings, no
  `<details>` dropdowns anywhere**, in the mockup's argument order: full analysis → "Evidence & timeline
  (N)" → "Recommended actions" → Sensor Trend charts → a dashed-underline "Focus `<entity>` in the
  explorer →" link.
- **Evidence & timeline** is one merged list (`buildEvidenceEntries()` combines `panel.timeline` with any
  `panel.evidence` records not already in it, e.g. undated alarm-config entries). Each entry: a dot
  colored by source system (`SOURCE_COLOR`, same palette as the graph legend), a monospace formatted
  time + bordered source chip + blue monospace record ref (`WO-4471`), then the record text. Only the
  first `TIMELINE_VISIBLE_LIMIT` (6) entries render visible; the rest sit behind a "Show all N records ▾"
  toggle — an alarm-flood answer cites 240+ records, which would otherwise swallow the thread.
- User bubbles are neutral gray with a small timestamp; the old red-gradient bubble is gone.
- **The AskEllie wordmark is a home button** (`#brandHome` → `resetToStart()`): aborts any in-flight
  answer via `activeChatAbort` (an `AbortController`, which also closes the server's SSE stream), clears
  the thread/history/trace/walk scope, re-renders the starter panel from the cached `starterQuestions`,
  and returns to the Ontology Overview. Done client-side rather than as a reload so it's instant and
  doesn't re-fetch + re-lay-out all 162 graph nodes. `AbortError` is swallowed in `submitQuestion`'s catch
  — a deliberate cancel is not an error.
- **Loading state**: a pending "Thinking..." bubble is appended immediately on submit and replaced in
  place — agentic answers can take 30–90+ seconds.
- If `panel.entity_id` is present, the walk view auto-focuses that entity's evidence as soon as the answer
  renders; the link re-focuses after browsing elsewhere.

### 4b. Graph pane: Ontology Overview + Reasoning Walk

**Ontology Overview** (`renderOntologyOverview()`, the default tab) — a schema/type-level diagram bounded
at one node per record type regardless of data volume: the `Asset` hub (count + name inside the circle)
with one satellite per record type, radius scaled gently by record count (126 AlarmEvents visibly outweigh
2 CostPostings), soft category-color fills. Every edge carries its relation name rotated along the line,
plus a wide invisible hit-path driving an instant cursor tooltip ("Asset —HAS_ALARM→ AlarmEvent · 126
links"). The type inspector uses the **same two-mode interaction as the walk view**: hovering a type node shows a
transient peek (source system, fields, relations, the relation it hangs off with a live link count — plus
a "Click to pin and list instances" hint), and *clicking* pins it, which adds the scrolling list of that
type's instance ids (`AME-000001` …, capped at 40 with a "+86 more" line) and rings the node. Backed by
`overviewTypeIndex`, built once per render from the `/api/graph` payload. Listing instances only on the
pinned panel is deliberate: a hover peek must stay short enough not to cover the diagram, and pinning is
the explicit "show me more" action. Pan/zoom works here too via a `translate+scale` transform on the
diagram's group in viewBox units.

**Reasoning Walk** (`#plantGraph`) — never shows the whole plant. `graphState.revealedIds` is the single
visibility gate: empty until a question is asked (empty-state hint; the trace panel/legend/zoom stack stay
hidden), then nodes are **progressively revealed** as the reasoning touches them:

- **Live** (agentic mode): each `tool_result` SSE event's `walk_step.node_ids` is revealed + pulsed blue
  as it arrives (`liveWalkStep()`), so the subgraph literally grows while the agent thinks.
- **Post-hoc** (deterministic mode, no live events): `animateGraphWalk(panel)` replays `panel.walk` step
  by step. Long walks fast-forward: >20 steps drop from 550ms to 110ms per step (`walkStepDelay()`), so
  TK-201's 122-step alarm-flood walk takes ~13s instead of ~67s (and renders as a striking radial burst).
- **The tidy layout applies DURING the walk, not only at settle** (fixed 2026-07-29 after a demo-run
  screenshot showed a mid-walk graph that was crowded, label-overlapped and half off-screen).
  `revealNodes()` re-runs `assignScopedLayout()` + `fitScopedView()` on every reveal, so each step shows a
  fully-framed subgraph; previously the animation played over the nodes' original *whole-plant* force
  positions and each step additionally `panToNode`'d onto a single node, pushing the rest out of view.
  Those per-step pans are gone — the fit keeps everything on screen instead.
  - `walkLayoutLocked` + `lockWalkLayoutFor(steps, panel)`: the two replay paths know all their steps up
    front, so they lay the **whole** union out once **and frame the camera on that full extent once**,
    then freeze both. The result is a static stage the walk lights up step by step — no nodes sliding
    around, no drifting zoom. (Re-fitting per reveal was what made the graph lurch on every play/step
    click; `revealNodes()` now only re-lays-out/re-fits when the layout is *un*locked.) The live agentic
    path stays unlocked — it learns nodes in batches over only a handful of steps — and re-fits as it
    grows. `focusAnswerInGraph()`/`resetWalkScope()` release the lock.
- Both paths settle into `focusAnswerInGraph(panel)`, which (redesigned 2026-07-29 after demo-run
  feedback that the settled view read as "random and overwhelming"):
  - **Lays the subgraph out deterministically** (`assignScopedLayout()`, replacing the old per-run force
    scatter): revealed Asset nodes sit on a left-to-right process backbone ordered by their revealed
    FEEDS/COOLS chain depth; each asset's records fan around it in sorted, type-grouped arcs (full circle
    for a standalone asset, above/below arcs when backbone neighbors exist), on concentric rings when one
    ring can't hold them. Record types with >12 instances hide their labels (`.label-hidden`; hover shows
    one) — an alarm flood becomes a tidy unlabeled radial burst. Same answer → same picture, every run.
  - **Applies a visual hierarchy** (`computeCitedIds()`): red `.highlighted` only for the asset backbone,
    the charted historian tags, and records the answer text names by id (e.g. "WO-4471"); everything else
    the reasoning merely touched gets quiet `.context` (45% opacity). Red edges only between cited nodes.
    **Fallback**: if the answer named *no* record ids at all — true of "show me everything about X", whose
    prose reports counts rather than ids — every revealed node counts as cited, since dimming the entire
    subgraph would wrongly signal "none of this matters".
  - **Fits the viewport to the whole subgraph** (`fitScopedView()`, also behind the ⤢ button) instead of
    a 100% crop, rings the entity as the anchor, and no longer auto-pins the inspector over the graph
    (hover/click still opens it).
  A new question (`resetWalkScope()`) clears the scope back to empty.
- The floating **Reasoning trace** panel (top-right) numbers each step's narration and has play/step/reset
  controls to re-watch the walk; the explorer header shows a monospace counter ("Showing N of 162 nodes
  (scoped to this answer)").
- Node circles are tinted with their category color; hover thickens the stroke and shows the record
  inspector; walk edges get the same wide-hit-path cursor tooltip ("P-101 —FEEDS→ E-101").
- **Label legibility**: captions are painted with a knockout halo in the canvas background color
  (`paint-order: stroke`), so they stay readable where edges/dots run behind them, and use `--text-main`
  rather than the muted gray. `fitScopedView()` may magnify up to **1.6x** (previously capped at 1.0,
  which rendered a 5-node answer tiny in a large empty pane). `nodeShortLabel()` captions historian tags
  by their distinguishing LAST segment (`VIB_01`) — truncating those 40-char ids from the end produced
  four identical `FAC1.UNIT100.CENTRIFU…` captions on one pump; the inspector/tooltips still show the
  full id.
- **Zoom/fit** (shared stack, both tabs): +/− buttons, cursor-anchored wheel zoom, click-drag pan, and a
  ⤢ fit button (overview → natural framing; walk → re-center on the answer's entity at 100%).
- **Light mode is the default theme** (switched from dark on 2026-07-29 — it reads better for the demo),
  same inline head-script/`applyTheme()` mechanism; a saved `localStorage.theme` still wins.
- **Node inspector modes** (both tabs): hovering a node is a *transient peek* — moving off it (or off the
  whole viewport, or switching tabs) always dismisses it, so the panel can't sit stale over the canvas.
  *Clicking* pins it: red border, a ✕ to dismiss, taller, and the content scrolls internally
  (`.inspector.pinned .inspector-body`) so an asset with 15 connections — or a schema type with 126
  instances — is readable rather than clipped. A pinned panel also dismisses on clicking empty canvas,
  suppressed after a drag-pan via `canvasDidDrag` (a pan ends in a click too, but isn't a dismiss
  gesture). The two views pin *different kinds of thing* — `graphState.selectedId` (a graph node id, walk
  view) vs `graphState.pinnedType` (`{label, count}`, schema view) — so `hideInspectorPreview()` is
  tab-aware; restoring the wrong one would strand an unrelated panel on screen. `.node.selected` /
  `.overview-node.selected` mean "pinned by click"; the answer entity's dashed ring is `.node.anchor`.
- **Edges stop at node boundaries** in both views rather than running center-to-center under the node
  fill: straight schema edges trim by each circle's radius + 2px, and the walk's quadratic curves trim
  along their end tangents (`trimToward()`, which points at the bezier control point).
- **The legend is contextual** (`updateLegend()`): hidden entirely on the Ontology Overview (every node
  there already shows its type as a caption, so it was pure redundancy), and on the walk it lists only
  the record types actually present in the current scoped subgraph, plus two rows that teach the color
  language ("examining now" = blue, "cited in answer" = red). Typically 4-7 rows instead of a fixed 9.


---

## 5. Quirks & gotchas (keep this section current)

- **`appendInlineMarkdown()`'s inline emphasis regex used to treat any bare `_..._` pair as italic, and had
  no code-span support at all** — a real, screenshot-caught bug: the model cites historian tags in backticks
  (e.g. `` `FAC1.UNIT100.CENTRIFUGAL_PUMP_101.VIB_01` ``), which are riddled with underscores used as a word
  separator, not emphasis. With no backtick handling, the backticks rendered as literal characters and the
  regex's `_[^_]+_` alternative matched the FIRST stray underscore in one tag name against the NEXT
  unrelated underscore later in the same paragraph (tag names almost always have an odd underscore count),
  wrapping everything in between — often most of the paragraph — in a single giant `<em>`, silently eating
  the underscores out of the tag name in the process. Fixed by (1) matching `` `code` `` spans FIRST so
  their content (including underscores) is never handed to the bold/italic pattern, rendered as a real
  `<code>` element (new `.markdown-body code` CSS, monospace + subtle pill background), and (2) dropping
  bare underscore-italic support entirely — the model is told to use backticks for identifiers and
  `**bold**` for emphasis, so `_italic_` wasn't providing real value against the risk. `*single-asterisk*`
  italic is unaffected/still supported.
- **The server is threaded (`ThreadingHTTPServer`), and that's load-bearing.** It used to be a
  single-threaded `HTTPServer` "on purpose" (mlx-lm's Metal init isn't thread-safe), but that meant an
  open `/api/chat` SSE stream blocked *every* other request: a mid-answer page refresh hung until the
  answer finished — measured **9.8s** on the fast rule-based path, and 30–90s on the agentic one, which
  reads to a user as the app having crashed. Now threaded by default; it drops back to single-threaded
  only when `LOCAL_LLM_PROVIDER=mlx` is explicitly set (that provider's native `SIGABRT` can't be caught,
  which is why it's still warmed up on the main thread at startup). Shared state is read-only
  (`_ENTITIES`/`_KG`), `historian_series()` opens its own file handle per call, and the text-generation
  provider is stateless per request, so no locking is needed. Verified: `GET /api/graph` returns in 20ms
  while a chat stream is open.
- **The graph cache can silently carry an incomplete LLM-extracted topology.** `output/graph.json` is
  trusted verbatim by `load_or_build()`. A cache committed on 2026-07-28 had been built by the LLM
  topology extractor and was missing the **P-102 → Column reflux `FEEDS` edge** (plus all V-201 edges),
  while containing a spurious "H-101 SUPPLIES_UTILITY Column" edge — so `_answer_high_dp` could never
  find S3's cavitation half, and the then-hardcoded headline claimed "reflux cavitation" anyway, masking
  the gap for a full day (caught by `tests/test_agent_dispatch.py`). Two lessons now encoded in code:
  headlines/recommendations only name causes actually found; and after any `FORCE_REBUILD=1` with the LLM
  configured, **eyeball the extracted `FEEDS/COOLS/SUPPLIES_UTILITY` edges** (the build prints a
  `topology_extraction: N accepted, M dropped` line — investigate a non-zero `dropped` count) before
  trusting the cache for a demo. The deterministic fallback topology (`graph.py`'s `_PROCESS_TOPOLOGY`)
  is complete per SCENARIO.md §5b.
- **Historian trend window length matters — and the agentic tool no longer lets the model get it wrong.**
  48h is right for vibration but falsely reads "stable" for slow signals (fouling, lube-oil temp). The
  deterministic handlers pass `window_hours=24*14` explicitly, and any new handler dealing with a
  slow-moving signal must too. For the **agentic** path, `get_historian_trend` now calls
  `_trend_with_comparison()`, which returns the requested window at the top level **plus** the
  complementary one under `other_windows`, from a **single pass** over the 518k-row file (widest window
  fetched once, narrower ones sliced in memory — no added latency, measured 1.9s either way). When the two
  windows disagree it adds an explicit `trend_note`.
  *Why this exists:* a retest measured the S4 answer flipping purely on the window the model happened to
  request. K-101's `LUBE_01` is `stable -0.3%` over 48h but `rising +32.1%` over 336h (54 → 72 °C, then
  plateaued) — **both true**. All runs that asked for 48h concluded cooling valve CV-400 was "not
  implicated" (one asserted it without ever checking CV-400); all runs that asked for 336h named CV-400
  correctly. Prompt rule 5 already asked for a long window on slow signals and was followed ~half the
  time, so the fix removes the choice rather than restating the instruction. The requested window's fields
  stay at the top level specifically so `build_ui_panel`'s charts, `_describe_record` and
  `_flatten_evidence` keep working untouched.
- **...and the model can still contradict its own `trend_note` warning, so there's a second, deterministic
  backstop.** A further retest re-ran the S4 question after the above fix and caught a live run whose final
  answer stated CV-400's cooling-water supply was "steady" and "confirmed healthy" — using only the 48h
  reading — despite the tool result it was handed containing the exact `trend_note` telling it not to do
  that. `_find_dismissed_trend_warnings()` scans the trace for any `get_historian_trend` result carrying an
  unresolved `trend_note` whose owning asset (matched against **all** its per-system aliases, including
  informal codes like "CV-400" that only ever appear parenthetically inside a system's own name string —
  see `_tag_owner_aliases()`) the final answer text dismisses with language like "not implicated"/"ruled
  out"/"confirmed healthy". If found, `_run_agentic_events` spends exactly one corrective turn quoting the
  warning back to the model verbatim and asking it to revise, mirroring the `finish_reason == "length"`
  retry's shape (bounded, deterministic, falls back to the original answer if the model doesn't produce a
  clean revision). Covered by `TestDismissedTrendWarningDetection` (offline, pure-function tests of the
  detector itself — the corrective retry's live effect still needs a real-LLM retest to confirm).
- **Almost every unit test exercises the DETERMINISTIC path, not the agentic one.** A green 41/41 is not
  evidence that the live LLM path behaves — e.g. `test_q3_c101_differential_pressure_multihop` asserts the
  word "cavitation" appears, which `_dispatch` guarantees but the live agent reliably does *not* say (it
  consistently frames P-102's falling suction as a symptom of the E-101→H-101 chain rather than S3's
  second cause, across every live run measured so far — this is still an open gap, not yet fixed the way
  the CV-400/window-length one was). `TestHistorianTrendTool` and `TestDismissedTrendWarningDetection` are
  the few agentic-path-relevant tests, and both only exercise pure helper functions, not the live model.
  Treat agentic behaviour as verified only by a live retest (`demo_run/`), never by the suite alone.
- **The Column/Compressor `C-101` code collision** (AM uses `C-101` as an alias for the compressor, DCS/
  Historian use it for the actual column) is a Stage 1 entity-resolution trap, but it's relevant here too:
  `_ALIASES`' `"anchor"` fields are deliberately `(system, local_id)` pairs, never a bare code string, so
  chat-level asset resolution can't fall into the same trap.
  `topology_extraction.py` had its own independent instance of this exact bug (fixed — exact-match-only for
  short codes) — see [ONTOLOGY.md](ONTOLOGY.md)/repo memory for that story if extending short-code matching
  anywhere in the chat path.
- **Output truncation is now detected instead of silent.** `chat()` returns the API's `finish_reason` on
  the message as `_finish_reason` (popped by `_run_agentic_events` before the message is appended back into
  the conversation, so it's never echoed to the API). `finish_reason == "length"` triggers **one retry** at
  `_ANSWER_RETRY_MAX_TOKENS` (3600); if that's still truncated the answer carries `truncated=True`, gets an
  italic "this answer was cut off" note appended to its text, and the UI shows a `⚠ cut off` chip. This
  closed a real defect found in a live retest: a Recommended Actions bullet ended mid-word
  ("…to protect cr") and nothing in the stack knew — `_split_agent_response()` happily parsed the
  half-written Markdown and the UI rendered it as a finished answer. `_ANSWER_MAX_TOKENS` also went
  1000 → 1600 → **2400**. If a 4th required section is ever added, re-check both budgets.
- **The completion gate makes rule 4a enforceable instead of advisory.** Before accepting a final answer,
  `_run_agentic_events` checks `_unexamined_neighbour_claim()`: if the answer *names* an asset that
  `get_related_assets` surfaced but no `get_asset_context` / `get_historian_trend` / `search_evidence` ever
  touched — or gives up (`"unconfirmed"`, `"not implicated"`, …) while such a lead is open — the answer is
  **rejected**, an instruction is appended to the conversation, and the loop continues. It fires **at most
  once** per question so a stubborn model can't burn the turn budget, and the rejection is recorded in the
  trace as a `completion_gate` entry for audit.
  Two design details that matter: (1) an answer that simply *doesn't discuss* a neighbour is **not** gated —
  an agent is allowed to judge a branch irrelevant, and gating that would fire on nearly every answer;
  (2) `_asset_mention_terms()` matches on the unified_id, canonical name, per-system local ids **and
  code-shaped `_ALIASES` keywords** (those containing a digit). That last part is load-bearing in both
  directions — the answer that motivated this said "CV-400", which is neither ASSET-004's canonical name
  ("Boiler Feed Flow Control Valve") nor any local id, while the plain alias words ("valve", "tank") would
  have matched almost any refinery answer. Idea borrowed from a sibling project's investigation checklist.
  Covered by `tests/test_agent_dispatch.py::TestCompletionGate` (7 tests, scripted fake provider, no LLM).
- **`get_related_assets` returning a neighbour is not evidence about that neighbour** — prompt rule **4a**
  exists because of measured run-to-run variance on exactly this: in one live run the agent asked
  "is K-101 the same problem as P-101?" (the handbook's S4 distractor), called `get_related_assets`, saw
  CV-400 listed, never checked CV-400's own records/trend, and reported K-101's cause as *"unconfirmed"* —
  while a different run of the same question named the stuck valve correctly. Rule 4a now requires pulling
  a plausible neighbour's `get_asset_context`/`get_historian_trend` before concluding, and forbids
  "unconfirmed" while a named related asset is unchecked. `_MAX_AGENT_TURNS` went 12 → 14 to pay for those
  extra sequential calls.
- **`_split_agent_response()` must never raise.** It's parsing free-form LLM text against a *requested but
  not enforced* format — a model that ignores the instructions should degrade to "everything in root_cause,
  headline/recommendation None", not crash the request. Any future change to the parsing regexes must
  preserve this always-degrade-gracefully property.
- **Prompt injection surface**: the user's raw `question` string is sent directly as a chat message to the
  configured LLM in agentic mode. The tools are read-only (no mutation, no filesystem/network access beyond
  the graph's in-memory data), and the system prompt instructs the model to ground every claim in tool
  results, but there is **no explicit prompt-injection defense** (e.g. no instruction-hierarchy tagging of
  user input, no output filtering). Low residual risk for a local single-user demo, but worth documenting:
  if this is ever exposed multi-user/externally, add clear delimiters around the user question and treat
  tool results (which include free-text `notes`/`symptom`/`recommendation` fields from the CSVs) as data,
  not instructions, more rigorously.
- **mlx-lm Metal crashes are native `SIGABRT`s, not Python exceptions** — this is *why* `LOCAL_LLM_PROVIDER=
  mlx` must stay strictly opt-in (see §2) and why the provider is warmed up on the main thread at server
  startup rather than lazily on first request. Do not change this to lazy/on-demand initialization without
  re-reading the mlx-lm session notes in repo memory.
- **`.env` edits made in the VS Code editor don't hit disk until saved** — a terminal-launched `python3`
  process reads the stale on-disk copy until the file is saved (Cmd+S). When debugging "why isn't my new env
  var taking effect", verify via a terminal read (`python3 -c "open('.env').read()"`), not the editor-aware
  file-reading tool, which sees the live (possibly unsaved) buffer instead.
- **Never print/log the raw `DATABRICKS_TOKEN` (or any secret) value** — when debugging provider config,
  only report `len(value)` or presence/absence.
- **"SLM" terminology was renamed to "LLM"/"language model"** everywhere (env vars, function names, UI copy)
  once the configured model became a large frontier model (Claude Opus) rather than a literal small local
  model — if you find any lingering "SLM" reference in code/UI, it's stale and should be renamed to match.
- **Two provider factories exist and are independent** (`get_similarity_provider()` for embeddings/Stage 1,
  `get_text_generation_provider()` for chat/Stage 4) — configuring one does not configure the other; e.g.
  `EMBEDDING_MODEL` (embeddings) and `LLM_MODEL` (chat) are separate env vars pointing at separate Databricks
  serving endpoints, easy to mix up when debugging "why isn't my LLM configured" if only one was set.
- **The "✨ polished by" badge/mode referenced in `app.js` and repo memory is currently dormant** — an
  earlier design (`_polish_with_llm`) rewrote a deterministic draft into more natural prose via a plain
  `generate()` call; the current code path only produces `presented_by != "rule-based"` via the agentic tool-
  calling loop (`scenario == "agentic"`), which the frontend labels "🤖 reasoned by". If a non-agentic
  polish-only mode is reintroduced, the frontend logic (`usedLlm`/badge text in `renderAssistantBubble()`)
  already supports it correctly — no frontend change needed, only backend wiring.

---

## 6. Coverage check against `docs/TEAM_HANDBOOK.md` §6/§7 (checked 2026-07-29)

The 6 agentic tools (§1b) + the deterministic `_dispatch` fallback (§1a) were checked directly against the 5
planted scenarios (§6) and 7 demo questions (§7) the handbook requires the system to handle. **Verdict:
sufficient for all 7**, with one minor, non-blocking gap (below).

| # | Question | Scenario | Needs | Verdict |
|---|---|---|---|---|
| 1 | Why is P-101 vibrating? | S1 (finale) | `get_asset_context` (seal WO + setpoint change) + `get_historian_trend` (VIB) | ✅ |
| 2 | Why is H-101's fuel use rising? | S2 | + `get_related_assets` (1-hop upstream to E-101) | ✅ |
| 3 | Why is C-101's dP high? | S3 (multi-hop) | genuine 2-hop reasoning (Column ← H-101 ← E-101) + reflux-pump cavitation | ✅ live-verified |
| 4 | Is K-101 the same problem as P-101? | S4 (distractor) | context+trend for both assets, `get_related_assets` (CV-400 `COOLS` edge) | ✅ |
| 5 | Why so many alarms on TK-201? | S5 (config, not process) | alarm-event pattern reasoning via `get_asset_context` | ✅ live-verified, see gap below |
| 6 | Show everything about P-101 | entity-resolution check | `list_assets` + `get_asset_context` | ✅ |
| 7 | Maintenance + ops changes last week | cross-app join | `get_asset_context` (its `reference_time` field drives "last week" math) | ✅ |

**Live-verified (real Databricks Claude Opus agentic path, not just reasoned about abstractly)**:
- **Q3**: tool trace was `list_assets → get_asset_context → get_related_assets → get_historian_trend →
  get_asset_context (×3) → get_historian_trend (×3) → search_evidence (×2)`. Reached the correct root cause
  (E-101 fouling cascading through H-101, +121% column dP over 14 days, plus reflux-pump cavitation risk).
  Notably, the model found E-101 (2 hops from the Column) via the new `search_evidence` tool (matched
  "fouling"/"bundle cleaning" text) rather than by calling `get_related_assets` twice — i.e. `search_evidence`
  gives the model an *alternative* route to multi-hop-shaped answers, not just direct graph traversal.
- **Q5**: tool trace was `list_assets → get_asset_context → get_related_assets → get_historian_trend`.
  Correctly concluded "chattering (nuisance) alarm... lack of adequate deadband/hysteresis" purely from the
  alarm events' own timestamps/values (78.6% ACTIVE ↔ 78.3% RTN, repeating every few minutes) — **without**
  ever reading the actual configured threshold/deadband values (see gap below).

**The one real gap found (now closed, see below)**: `data/am/am_config.json`'s per-alarm-point `limits`
(HH/H/L/LL) and `deadband` fields were read by `ingest.load_am()` only for the alarm's `alarm_point`/
`priority`/`console` attributes — the limit/deadband values themselves were never attached to the graph or
exposed by any tool. The agent could (and, per the Q5 test above, reliably did) infer a mis-set-limit
conclusion *behaviorally* from the alarm event value/timing pattern, but couldn't cite the actual configured
number (e.g. "H limit is 78.5%, deadband only 0.1%") as hard evidence the way it cites work order IDs or
setpoint values elsewhere.

**Closed the same day**: added `AlarmConfig` nodes to the graph (one per configured alarm point, attached to
its asset via a new `HAS_ALARM_CONFIG` edge — see `docs/ONTOLOGY.md` §3b) carrying `limit_hh`/`limit_h`/
`limit_l`/`limit_ll`/`deadband`/`priority`/`cause`/`consequence`/`recommended_action`. No new tool was needed
— `get_asset_context`/`context_for_asset()` picks up any node type attached to an asset automatically, so
both the agentic path and `search_evidence` got this evidence for free; only `_describe_record()`/
`_RECORD_LABEL_TO_TYPE`/`_SEARCH_FIELDS` needed new `"AlarmConfig"` entries, plus the deterministic
`_answer_alarm_flood()` handler was updated to actively cite the real limit/deadband instead of only
describing event behavior. **Live-verified after the fix**: the deterministic answer now reads "Configured
alarm limits: H=78.5%, deadband 0.1% — a deadband this tight relative to the observed value swing..."; the
agentic answer independently reached the same conclusion, additionally quoting the config's own
`recommended_action` text ("suspected mis-set"). Full 13/13 test suite still passes after the graph schema
change.

## 7. Known limitations / backlog (see also `docs/EXTENDED_SCOPE.md`)

Not blocking for the current rubric, but relevant if this session extends the chat surface further:

- **`search_evidence` is lexical substring search only** — it closes the old "no content search" gap, but
  it is not semantic retrieval/embeddings and has no ranking beyond first-match scan order. At larger scale,
  this should likely become indexed search (and possibly embeddings-backed retrieval) to improve recall and
  relevance for paraphrased queries.
- **Tool set is still intentionally narrow (7 tools, 6 of which query the KG):** `list_assets`,
  `get_asset_context`, `get_historian_trend`, `get_related_assets`, `search_evidence`,
  `get_plant_status_summary` (plus `write_plan`, which is UI-only and never touches the graph).
  `get_related_assets` only exposes `FEEDS`/`COOLS`/`SUPPLIES_UTILITY` edges, and there is still no generic
  "traverse N hops of any edge type" tool. New question patterns currently need either a new `_dispatch`
  branch (deterministic mode) or rely on the LLM composing the current tools (agentic mode) — the latter
  remains the preferred direction.
- **`_dispatch`'s `_ALIASES` table is hand-maintained** — a new physical asset needs a manual alias entry to
  be reachable via the deterministic fallback; the agentic path already handles new assets/systems with zero
  code changes (via `list_assets`) once they're in the graph. Low priority since the LLM path is preferred.
- **Live streaming exists now, but isn't atomic.** `stream_answer()` can yield several plan/tool_call/
  tool_result events to the client before a later turn in the same agentic loop fails — those already-sent
  events can't be un-sent, so a user could briefly see live tool-call activity followed by a deterministic-
  fallback final answer that doesn't match what was just shown. Rare (only on a mid-loop failure after
  earlier turns succeeded), and arguably honest behavior for a stream vs. a single atomic batch response,
  but worth knowing about.
- **No formal prompt-injection hardening** (see §5) — acceptable for a local single-user demo, flagged for
  any future multi-user/external exposure.
- **No MCP (Model Context Protocol) wrapping** — the tool-calling design already matches MCP's shape
  (schemas + read-only executors), but exposing it as an actual MCP server was judged explicitly optional
  per `docs/TEAM_HANDBOOK.md` and not pursued.
- **Residual LLM instruction-following variance is a known, not-fully-closeable limitation of the agentic
  path.** Two concrete, measured cases as of 2026-07-29:
  - S4 (K-101 vs P-101): even with the dual-window `trend_note` mechanism (§5) AND a deterministic
    contradiction-retry backstop (`_find_dismissed_trend_warnings`, §5) both in place, a model can in
    principle still write a final answer that ignores a corrective nudge on its retry turn too — the retry
    reduces but does not mathematically guarantee zero occurrences. Re-verify with a fresh live retest
    (multiple runs) before relying on this scenario's consistency for a demo.
  - S3 (C-101 dP): the live agentic path has never (across every run measured so far) framed reflux pump
    P-102's falling suction pressure as a genuine *contributing* cause (SCENARIO.md §5b's "cold feed **+**
    reflux cavitation" dual cause) — it's always described as a downstream symptom of the E-101→H-101
    chain, at most a forward-looking risk to "monitor for cavitation." This is unfixed; a similar
    prompt-plus-deterministic-backstop approach (as used for S4) would be the natural next step if it needs
    closing before a demo.
