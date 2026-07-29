# How Entity Resolution Works

This document explains, in plain language, exactly how this project decides that records from six different plant systems (AM, APM, DCS, Historian, CMMS, ERP) describe the *same physical piece of equipment*. It also explains every quirky rule in the code, **why** each one exists, where AI is actually used, and how the confidence score is calculated.

> **Maintenance rule:** this file must be updated any time the entity resolution logic changes — `ontology_builder/ingest.py`, `classify.py`, `resolution.py`, `open_vocab_classify.py`, `llm_provider.py`'s similarity/embedding code, or `config.py`'s credential loading. Treat a code change to any of these as incomplete until this doc reflects it.

---

## The big picture

Six systems each describe the same equipment with completely different IDs and names, and there's no shared key. Entity resolution happens in three steps:

1. **Profiling** (`ingest.py` + `classify.py`) — turn every system's raw record into one common shape.
2. **Open-vocabulary classification** (`open_vocab_classify.py`) — an optional AI step that fills in equipment types the keyword list doesn't recognize.
3. **Resolution** (`resolution.py`) — compare records across systems and decide which ones are the same physical thing.

---

## Step 1: Profiling — six shapes become one shape

Every record, from every system, gets turned into the same 7-field shape: `system`, `local_id`, `name`, `unit`, `asset_class`, `equipment_number`, `criticality`.

- **`unit`** — pulled out with a regex that recognizes `U100`, `Unit 100`, `CON-U100`, `FAC1-U100-PUMPS`, however it's dressed up in the surrounding text. It just looks for "U"/"Unit" followed by digits.
- **`asset_class`** — found by scanning text for keywords ("pump", "compressor", "exchanger", "column"...). This is a fixed, hand-written list of ~8 classes. It can't recognize a word that was never added to it (a "turbine" comes back blank from this step alone) — that's exactly the gap Step 2 exists to fill.
- **`equipment_number`** — usually the last 3-digit number in the ID or name ("Crude Charge Pump 101" → "101"). **CMMS is the odd one out**: its codes (`EQ-1042`) have no number to extract at all, so the system instead scans that asset's maintenance-note free text for a mentioned code like "E-101" and borrows that number — but only if the mentioned number isn't secretly just the unit number in disguise (a guard added after realizing "CV-400" would otherwise look like equipment number 400, when 400 is actually the *unit*).
- **`criticality`** — different systems grade this differently (`A/B/C` vs `High/Medium/Low`); a small lookup table normalizes both onto the same three values.

None of this is AI — it's all regex and keyword matching, done once per record, before anything is compared across systems.

---

## Step 2: Open-vocabulary classification (this is where one AI call happens for classification)

**Why this exists:** the keyword list in Step 1 can only ever recognize equipment types someone thought to add. A synthetic test with a brand-new "Turbine" proved this concretely — its class came back blank everywhere, since "turbine" isn't a keyword.

**Why a simpler fix (embeddings nearest-match) doesn't work here:** we tested comparing "H2 Recycle Turbine" against all 8 known class labels using real embeddings — the best match scored only 0.566 (against "Compressor"), nowhere near confident, and the top three candidates were within 0.02 of each other (essentially a coin flip). An embeddings-only approach can only ever pick "closest of these 8 known ones" — it can never correctly say "this is a genuinely new type." So for a real new equipment category, it either guesses wrong or (with a strict-enough floor) just agrees with "unclassified," providing zero benefit over doing nothing.

**What actually solves it:** `classify_open_vocabulary()` asks the configured LLM to propose a class name in its own words — not limited to the fixed list, so it can genuinely invent "Turbine." Two safety mechanisms make this reliable:

1. **The prompt is grounded with every class already known in the dataset** (both the 8 keyword classes and any new ones already introduced earlier in the same run), and the model is explicitly told to reuse an existing category if the equipment reasonably fits one, rather than inventing a near-duplicate.
   - *This exists because of a real bug found during testing*: without this grounding, the LLM invented `"FlowControlValve"` for a valve that should have just been `"Valve"` (the same physical asset already had `"Valve"` assigned via keywords in other systems) — the two labels only scored 0.669 similarity via embeddings, well under any safe canonicalization floor, so post-hoc reconciliation alone silently failed and split one real asset into two.
2. **A second, backup canonicalization pass** still checks a newly-proposed label against every class label already assigned (via embeddings, if configured), in case the model doesn't reuse an existing name exactly.

**Safety rules:**
- Only ever runs for a profile whose `asset_class` is still `None` after Step 1 — it never overrides a keyword match.
- Only runs at all if a real LLM is configured; otherwise this step is skipped entirely and nothing changes.
- If the LLM call fails, returns something unusable, or says "Unknown," the class simply stays `None` — never crashes, never guesses.
- Every LLM-assigned class is tagged `asset_class_source="llm_open_vocab"` plus the original raw label, so it's always auditable which classes came from keywords vs. the AI step.

---

## Step 3: Resolution — deciding who's the same thing

Every pair of records from *different* systems gets compared (with a performance shortcut described below). For each pair:

### 3a. Hard filters — instant "no" if these clearly disagree

If both records have a `unit`, `asset_class`, `equipment_number`, or `criticality` and they *disagree*, the pair is rejected immediately, no matter how similar the names sound. This is the main safety net — it's why the compressor's alias `C-101` never gets confused with the column also called `C-101`: they have different `asset_class` values, so this filter blocks it outright.

### 3b. The ambiguous-sibling guard — a second, subtler filter

**Why this exists:** `asset_class` matching alone isn't strong evidence when real siblings of that class exist — *any* two pumps in the same unit both say "Pump," that doesn't mean they're the *same* pump. This was discovered as a live regression: when embeddings-based name similarity was turned on, `CMMS:EQ-1042` (which has no `equipment_number` — its known structural weakness) nearly merged with the *wrong* pump (P-102's Historian record) purely on unit+class agreement plus a moderate 0.55 name-similarity score, because nothing else was there to stop it.

**How it works:** the system pre-scans the *entire* dataset once and asks, for every `(class, unit)` combination: "are there actually two or more different equipment numbers here?" (Pump/Unit100 → yes, 101 and 102 both exist.) If yes, any pair in that group that *can't* confirm a matching equipment number must clear a much higher bar on name similarity (0.80+) before it's allowed to match — class+unit alone is no longer enough. If no real siblings exist for that class+unit (e.g. there's only ever one compressor in Unit 100), the normal, looser rules apply, since there's no real collision risk.

### 3c. Weighted scoring

If a pair survives both filters above, it's scored from five weighted pieces:

| Signal | Weight |
|---|---|
| unit matches | 0.15 |
| asset class matches | 0.30 |
| equipment number matches | 0.25 |
| criticality matches | 0.05 |
| name similarity | 0.25 |

**Important quirk:** the score is divided by the *fixed total* of all five weights (1.0), not just by however many signals happened to be known for that particular pair. A record missing three of the five signals doesn't get graded on a curve — those points are simply gone, dragging the score down. Missing information should lower confidence, not be excused from the test.

**This is where the second AI call happens — name similarity.** A `SimilarityProvider` compares the two records' text names and returns a 0–1 score:
- By default, this is `difflib` (character overlap) — no AI, zero network calls, works everywhere.
- If `EMBEDDING_MODEL` is configured in `.env`, it instead calls a real embeddings model (currently Databricks' `databricks-gte-large-en`), gets back vector representations of both names, and computes cosine similarity — genuinely understanding that "Crude Charge Pump 101" and "Crude Charge Pump A" describe the same thing semantically, not just by shared letters. Vectors are cached per unique text (not per pair), so this doesn't multiply into hundreds of network calls.
- If the embeddings call ever fails (bad config, network issue), it silently falls back to `difflib` rather than crashing the pipeline.

**A pair only becomes a real match if its total score clears 0.55** (`MATCH_THRESHOLD`).

### 3d. The one non-probabilistic shortcut — the hard foreign key

Separately from all scoring: if an ERP cost posting references a CMMS work order, and that work order's `asset_code` matches a real CMMS record, that ERP↔CMMS pair gets force-linked with a perfect score of `1.0` — no scoring, no guessing, just following an actual fact that exists in the data.

**Important nuance:** this hard link only ever adds *one specific, trusted edge*. It provides zero protection against a *different*, unrelated (and possibly wrong) edge forming on that same record through the normal scoring path — that's exactly how the P-101/P-102 near-miss happened: the correct FK edge and a separate, wrong scored edge both touched the same CMMS record, and grouping doesn't distinguish "certain" edges from "risky" ones once they're both accepted.

### 3e. Turning matches into unified assets, and the confidence score

Every accepted match (scored ≥ 0.55, or a forced FK link) becomes an edge. A union-find structure groups everything connected by *any* chain of edges into one cluster — if A matches B and B matches C, all three become one asset even if A and C never scored against each other directly.

**Confidence score for a unified asset = the plain average of every pairwise edge score *within* that cluster.** Not one number computed once — the average of however many individual links held that cluster together. A cluster held together by several strong matches (and any perfect 1.0 hard FK links) ends up with high confidence; one barely held together by thin evidence ends up lower.

### 3f. Two honesty layers on top

- **`low_signal` / `needs_review`**: flags any accepted match with *no* confirmed class match and *no* strong name match — i.e. it only got in on thin, coincidental evidence. Surfaced as `needs_review: true` in the final output so a human can double-check it, rather than presenting every match with equal confidence.
- **The ambiguous-sibling guard** (3b) actually *blocks* certain risky matches outright rather than just flagging them, because that specific failure mode is common and dangerous enough to prevent, not just warn about.

### 3g. Performance: blocking instead of comparing every possible pair

Comparing every record against every other record gets expensive fast (`n²`). Since the hard filters guarantee any pair with a different `unit` or `asset_class` can never match anyway, records are first grouped by `(unit, asset_class)`, and only records *within the same group* are ever compared. Records missing a unit or class (rare) go in a separate "wildcard" bucket compared against everything, since a hard filter can never reject a pair where one side is simply unknown. Measured on this dataset: 406 possible pairs → 83 actually compared (a 79.6% reduction), with byte-identical results to comparing everything — this is a pure speed optimization, not a behavior change.

---

## Display-only: cleaning up the `canonical_name`

`pipeline.py`'s `_canonical_name()` picks which member's `name` becomes the human-facing label for a unified asset — it walks a fixed system priority order (`AM > APM > ERP > DCS > CMMS > Historian`) and returns the first one present. **This choice is purely cosmetic** — it has zero effect on matching, scoring, or confidence; all matched records still appear in `members` regardless of which one's name gets displayed.

**Why APM names get trimmed:** every APM `display_name` follows the exact same structured breadcrumb template — `"Facility 1 > Unit 100 > <equipment name>"` (confirmed: every entry in `apm_config.json` follows this). Before display, `_strip_hierarchy_prefix()` takes just the text after the last `" > "`, turning `"Facility 1 > Unit 100 > Centrifugal Pump 102"` into `"Centrifugal Pump 102"`.

**This is a deterministic string split, not an LLM call — deliberately.** The ugly formatting comes from one specific system's known, consistent, structured template, not arbitrary unstructured text that would need real language understanding to clean up. A plain `rsplit(" > ", 1)` solves it perfectly, reproducibly, with zero latency/cost and zero risk of an LLM subtly altering the name's meaning — exactly the kind of problem that should stay deterministic rather than reaching for AI.

---

## Where AI is actually used — summary

| Step | AI used? | What for | Fallback if unavailable |
|---|---|---|---|
| Profiling (unit/class/number/criticality extraction) | No | — | — |
| Open-vocabulary classification | Yes (LLM) | Inventing/reusing an equipment class for unrecognized vocabulary | Skipped entirely; class stays `None` |
| Name similarity scoring | Yes (embeddings), optional | Judging how similar two equipment names/descriptions are | Falls back to `difflib` (character overlap) |
| Hard filters, ambiguous-sibling guard, weighted scoring, union-find clustering, blocking | No | The actual matching decision logic | — (always deterministic) |

**Enabling the AI paths:** set `DATABRICKS_TOKEN` + `DATABRICKS_HOST` + `LLM_MODEL` (for the open-vocab classifier) and `EMBEDDING_MODEL` (for real name-similarity) in a local `.env` file. Credentials are loaded automatically by `ontology_builder/config.py` and `ontology_builder/llm_provider.py` — every entry point (`run_pipeline.py`, the test suite, `server.py`) picks them up with no manual setup. If nothing is configured, the entire pipeline runs fully offline and deterministically.

---

## Every quirk, and the real bug that caused it

| Quirk | Why it exists |
|---|---|
| Unit regex accepts many formats (`U100`, `CON-U100`, `FAC1-U100-PUMPS`...) | Every system spells "unit" differently; a literal `"unit"`-only regex once silently set `unit=None` for most profiles and caused a false mega-merge. |
| Criticality mismatch is a *hard* reject, not a soft penalty | It's the actual signal that separates P-101 (High) from P-102 (Medium) — two real, similar pumps in the same unit. |
| CMMS equipment numbers are mined from work-order notes | CMMS's own asset codes (`EQ-1042`) carry no number at all — by design, the "hardest to match" system. |
| The "own unit" guard on note-mined numbers | Otherwise "CV-400" in a note would be misread as equipment number 400, when 400 is actually that asset's *unit*. |
| The `C-101` code is treated as ambiguous by class, not by string | AM uses `C-101` for the compressor; DCS/Historian use it for the actual distillation column — same string, different equipment. |
| `unit`+`equipment_number` alone can't cross the match threshold | A synthetic "Turbine" (unrecognized class) reusing the common "-101" numbering convention could otherwise transitively merge five different, previously-correct real assets into one. |
| The ambiguous-sibling guard (3b) | An embeddings-based name-similarity swap nearly merged P-101 and P-102 via a CMMS record with no equipment number — proving `asset_class` match alone is not enough evidence when real siblings exist. |
| Open-vocab classification prompt is grounded with known categories | Without this, the LLM invented a near-duplicate class ("FlowControlValve") instead of reusing "Valve," silently splitting one real asset in two. |
| `.env` is loaded in both `config.py` and `llm_provider.py` | A bare `python3 run_pipeline.py` or bare test-suite run was silently using no-AI fallbacks all along, because nothing in `ontology_builder` loaded `.env` except `server.py` — this was traced by noticing test runs finished suspiciously fast (milliseconds) for a step that should be making live network calls. |

---

## Files involved

- `ontology_builder/models.py` — the common `AssetProfile` shape.
- `ontology_builder/classify.py` — keyword/regex extraction helpers (Step 1).
- `ontology_builder/ingest.py` — per-system loaders that build `AssetProfile`s (Step 1).
- `ontology_builder/open_vocab_classify.py` — LLM-based class classification (Step 2).
- `ontology_builder/resolution.py` — hard filters, ambiguous-sibling guard, scoring, clustering, confidence (Step 3).
- `ontology_builder/llm_provider.py` — the pluggable similarity/text-generation providers (where AI calls actually happen, or don't).
- `ontology_builder/config.py` — shared paths + `.env` credential loading.
- `ontology_builder/pipeline.py` — orchestrates all three steps and writes `output/unified_entities.json`.
- `tests/test_resolution.py` — regression tests encoding the known traps (P-101 vs P-102, the `C-101` ambiguity, etc.).
