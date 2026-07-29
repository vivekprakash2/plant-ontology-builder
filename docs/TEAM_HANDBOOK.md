# Team Handbook — AI-Powered Plant Ontology Builder

**Hackathon:** Automation to Autonomy · **Duration:** 3 days · **Teams:** 4

Welcome. Over the next three days you will build an **AI system that unifies data from six independent
industrial applications into one connected "brain"** — and then let an agent reason across all of them
to answer questions a senior plant engineer would typically need hours to answer manually.

This is an **open-ended** challenge. You choose your stack, architecture, and tools. This handbook gives you
the problem, the data, the stages, and items that the judging team will be looking for pecific to this problem. 

It does **not** prescribe how to build it.

> **Read first, in this order:** `SCENARIO.md` (the story) → `refinery_pfd.html` (the plant, open in a browser)
> → this handbook → then explore `data/`.

---

## 1. The problem in one paragraph

The Hindenberg Refinery runs on six separate software systems (alarms, asset health, control, historian,
maintenance, ERP). Each one holds *part* of the truth about the same physical equipment, and **each names
the same equipment differently, with no shared key.** The crude charge pump **P-101** is `PMP-100-101` in
one system, `Pump_001` in another, `EQ-1042` in a third, and so on. Because nothing links them, when a pump
starts vibrating at 2 AM, an engineer must manually cross-reference four systems for three hours just to
form a hypothesis about *why*. **Your job: use AI to stitch these silos into one ontology / knowledge graph,
then build an agent that reasons across it to answer "why?" in seconds.**

---

## 2. What "done" looks like (the finale demo)

At judging, we will type plain-English questions and watch your agent answer them by reasoning across the
unified data. The headline question:

> **"Why is Crude Charge Pump P-101 vibrating?"**

A great answer identifies that the vibration correlates with an operator **setpoint change last shift**,
notes a recent **mechanical-seal replacement**, ranks the likely root cause, and cites the **evidence from
multiple applications** — all of which required first recognizing that `PMP-100-101`, `Pump_001`, `EQ-1042`,
etc. are the *same pump*.

You will be asked other questions too (see §7). Some are traps. A strong system handles them gracefully.

---

## 3. The six applications (your data sources)

Everything lives in `data/`. **Configuration = JSON, transactional = CSV.** Each app uses its own IDs.

| App | Folder | What it knows | Config (JSON) | Transactional (CSV) |
|-----|--------|---------------|---------------|---------------------|
| **Alarm Management (AM)** | `data/am/` | Alarms, limits, priorities, who acknowledged | `am_config.json` | `am_alarm_events.csv` |
| **Asset Performance (APM)** | `data/apm/` | Health scores, predicted failures | `apm_config.json` | `apm_health_history.csv`, `apm_events.csv` |
| **DCS / Control** | `data/dcs/` | Control loops, **operator setpoint changes**, PV/SP/OP | `dcs_config.json` | `dcs_operator_actions.csv`, `dcs_process_values.csv` |
| **Historian (PHD)** | `data/hist/` | **Time-series** of every sensor (30 days @ 1 min) | `historian_config.json` | `historian_timeseries.csv` |
| **CMMS / MaintWorks** | `data/cmms/` | Work orders, repair history, technician notes | `cmms_config.json` | `cmms_workorders.csv` |
| **ERP** | `data/erp/` | Cost center, criticality, cost postings | `erp_config.json` | `erp_cost_postings.csv` |

> **The core difficulty:** the same physical asset appears in all six with **different IDs and often
> different names**. There is **no shared key**. Linking them is the heart of the challenge.

**Hints you may exploit (but must discover):** names, descriptions, hierarchy/unit numbers, shared tag
references, and at least one genuine cross-app foreign key (ERP cost postings reference a CMMS work order).

---

## 4. Naming mismatch — worked example (Pump P-101)

The *same* pump, across systems:

| App | ID | Name | How hard to match |
|-----|----|----|----|
| AM | `PMP-100-101` | Crude Charge Pump 101 | easy |
| APM | `Pump_001` | Facility 1 > Unit 100 > Centrifugal Pump 101 | medium |
| DCS | `U100_P101` | Crude Charge Pump Flow Control | medium |
| Historian | `FAC1.UNIT100.CENTRIFUGAL_PUMP_101.*` | pump tags | medium |
| CMMS | `EQ-1042` | Crude Charge Pump A | hard |
| ERP | `ERP-FAC1-PMP-100-101` | Crude Charge Pump A (Main Building) | easy |

**Traps to avoid** (do NOT merge these):
- **P-101** (charge pump) vs **P-102** (reflux pump) — two similar centrifugal pumps in Unit 100.
- **E-101** (preheat exchanger) vs **E-301** (overhead condenser) — similar descriptions, different assets.
- The code **`C-101`** appears as a *compressor* alias in one system but is also the *column's* code —
  same string, different equipment class. Class matters.

---

## 5. Suggested staged approach

You may stop at any stage and still demo something real. Later stages score higher. **These are stages,
not requirements** — you own the design.

| Stage | Goal | You demonstrate |
|-------|------|-----------------|
| **0 — Ingest** | Load each app's data as its **own graph/model** | Six independent views of the plant |
| **1 — Link entities** | Use **AI** to prove same-asset across apps, with a **confidence score** | `Pump_001` = `PMP-100-101` = `EQ-1042` = … |
| **2 — Merge attributes** | Fuse overlapping attributes (criticality, location, description); resolve conflicts | One unified asset per physical thing |
| **3 — Query** | **RAG + knowledge-graph** layer answering cross-app questions | "Show alarms + open work orders for P-101" |
| **4 — Reason (agent)** | Expose the graph via **tools / MCP**, build an agent that answers **"why?"** with a causal chain | The finale demo |

This mirrors the **Standard → Meaning → Reality** continuum in `Ontology-CDM-KG.jpg`:
CDM/ingest → Ontology/linking → Knowledge Graph/query & reason.

---

## 6. The five scenarios hidden in the data

The transactional data contains **five planted situations**. You don't need to "solve" all five to win —
they exist so the agent has real cross-application causal chains to reason over, and so judges can probe it.
See `refinery_pfd.html` (colored dots) for where they live on the plant.

| # | Scenario | What's really going on |
|---|----------|------------------------|
| **S1** ⭐ | **P-101 vibration (finale)** | Vibration rises after an operator raised the flow setpoint +12% last shift; a seal was replaced 6 days ago. |
| **S2** | **E-101 exchanger fouling** | Outlet temp slowly drops → heater burns more fuel; a cleaning work order was deferred twice. |
| **S3** | **C-101 column flooding (multi-hop)** | Column differential pressure rises — caused partly by the cold feed from **S2** plus reflux-pump cavitation. |
| **S4** | **K-101 compressor (distractor)** | Also shows high vibration and a P1 alarm — *looks like P-101* — but the real cause is lube-oil overheating from a **stuck cooling-water valve (CV-400)**. Different root cause. |
| **S5** | **TK-201 alarm flood (config, not process)** | Hundreds of nuisance high-level alarms caused by a **mis-set alarm limit** in AM — the tank level is actually normal. |

> **Why these matter for judging:** S3 needs genuine multi-hop reasoning. S4 tests that your agent does not
> conflate two vibrating machines with different causes. S5 tests whether it can tell a **configuration
> mistake** from a real process problem.

---

## 7. Demo questions your system should try to handle

Design for these (and expect variations at judging):

1. **Why is Crude Charge Pump P-101 vibrating?** *(the finale — S1)*
2. Why is fired heater H-101's fuel consumption rising? *(S2)*
3. Why is column C-101's differential pressure high? *(S3, multi-hop)*
4. Is the Recycle Gas Compressor K-101 experiencing the same problem as P-101? *(S4 — expected answer: no, different root cause)*
5. Why are there so many alarms on tank TK-201? *(S5 — expected answer: config issue, not a real upset)*
6. Show me everything known about P-101 across all systems. *(entity-resolution / unification check)*
7. What maintenance happened on the crude charge pump in the last week, and did anything change in operations around the same time? *(cross-app join)*

---

## 8. Success metrics (how you're judged within general Hackathon rubric)

| Criterion | What we look for |
|-----------|------------------|
| **Entity resolution quality** | Correct cross-app links. Target **> 80%** accuracy; correct handling of traps; confidence scores. |
| **Reasoning / agent quality** | Correct root cause on the finale + other questions; cites evidence from multiple apps; handles the distractor (S4) and config case (S5). |
| **Ontology / graph design** | Sensible classes, relationships, and attribute merging; extensible. |
| **Use of AI** | AI is load-bearing (embeddings/LLM for matching & reasoning), not cosmetic. |
| **Query / RAG + interface** | Cross-app questions answered; usable interface (chat/API/UI). |
| **Demo & clarity** | Clear story, reproducible run. |

An **answer key exists** and is held by the mentors; entity-resolution accuracy is scored against it. Do not
expect to see it during the build.

---

## 9. Ground rules & tips

- **Stack is your choice.** Graph DB (Neo4j, etc.), vector store, any LLM/embeddings (Azure OpenAI, etc.),
  any language. MCP for tool exposure is encouraged but optional.  You can use the BMS system demoed on Monday.
- **AI must do the hard part.** Pure hard-coded ID mapping tables will score poorly on the AI criterion —
  the point is to *infer* the links.
- **Determinism where it matters.** Your **entity links** should be stable across runs (use low temperature /
  caching / thresholds). LLM prose can vary; the *facts* it cites should not.
- **Cite your evidence.** A root-cause answer that names the specific work order, setpoint change, and
  historian trend is far stronger than a vague guess.
- **Watch the historian size.** `historian_timeseries.csv` is ~36 MB / ~518k rows. Consider indexing,
  downsampling, or querying by tag/time-range rather than loading it all into a prompt.
- **Start with P-101.** Get the finale scenario working end-to-end first; then generalize.

---

## 10. What's in this folder

| File | Purpose |
|------|---------|
| `SCENARIO.md` | The story: plant, applications, the pain, the vision |
| `TEAM_HANDBOOK.md` | This document |
| `refinery_pfd.html` | Interactive plant diagram — hover equipment to see cross-app aliases |
| `data/` | All source data (JSON config + CSV transactional) |

---

### Your first hour, suggested

1. Open `refinery_pfd.html`; hover P-101 and note its six aliases.
2. Skim one config JSON and one transactional CSV from each of the six `data/` folders.
3. Manually trace S1 by hand: find the seal work order (CMMS), the setpoint change (DCS), and the vibration
   rise (Historian). Feel the pain — that manual trace is exactly what your agent will automate.
4. Decide your stack and sketch your ontology.

**Good luck. Build the brain that sees the whole elephant.**
