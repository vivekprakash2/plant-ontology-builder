# The Story: Hindenberg Refinery — "Nobody Sees the Whole Elephant"

> A narrative scenario for the **AI-Powered Plant Ontology Builder** hackathon.
> This document sets the *world*. Data generation, schemas and stage definitions come later.
> It is intentionally story-first so that participants with no refinery background can
> picture what is happening and *why the problem is painful*.

---

## 1. The Plant

**Facility:** Hindenberg Refinery ("FAC1"), a mid-size crude oil refinery on the US Gulf Coast.
It turns crude oil into diesel, gasoline and other products, running 24×7, all year.

It is organized into **Process Units** — self-contained sections of the plant:

| Unit | Name | What it does | Star equipment |
|------|------|--------------|----------------|
| **Unit 100** | Crude Distillation Unit (CDU) | Heats crude oil and separates it into fractions | **Crude Charge Pump P-101**, Recycle Gas Compressor C-101 |
| **Unit 200** | Product Storage & Blending | Stores and blends finished products | Diesel Storage Tank TK-201 |
| **Unit 300** | Naphtha Treating | Cleans and cools naphtha overhead | Overhead Condenser (Heat Exchanger) E-301 |

Our story centers on **Unit 100** and one very important, very ordinary machine:

> ### ⭐ The star of the show: **Crude Charge Pump P-101**
> This centrifugal pump pushes crude oil from storage into the distillation furnace.
> If it slows or trips, the **entire Unit 100 loses feed** and the plant loses money by the minute.
> It is a "bad actor" — it has a history of vibration problems.

---

## 2. The Cast — the Applications Running the Plant

Here is the key twist the participants must internalize: **no single system knows everything about P-101.**
Each application was bought at a different time, by a different department, from a different vendor.
They each hold *one slice* of the truth about the same physical pump — and they all **name it differently.**

Think of the old parable: *six blind men each touch a different part of an elephant and describe six different animals.* That is exactly this plant.

| # | Application (persona who uses it) | What it knows about P-101 | What P-101 is called there |
|---|----------------------------------|---------------------------|----------------------------|
| 1 | **Alarm Management (AM)** — *Control Room Operator* | Alarms, alarm limits, priorities, who acknowledged what and when | `PMP-100-101` / tag `U100_P101` |
| 2 | **Asset Performance Monitoring (APM)** — *Reliability Engineer* | Health scores, vibration trends, predicted failures, criticality | `Pump_001` / "Centrifugal Pump 101" |
| 3 | **DCS / Control System** — *Process Engineer* | Live process values, setpoints, control loops, operator setpoint changes | Loop `FIC-100-101`, tag `FAC1.UNIT100...` |
| 4 | **Process Historian (PHD)** — *Everyone* | Time-series history of every sensor (flow, pressure, vibration, temperature) | `FAC1.UNIT100.CENTRIFUGAL_PUMP_101.*` |
| 5 | **CMMS / Maintenance (fictional: "MaintWorks")** — *Maintenance Planner* | Work orders, past repairs, spare parts, technician notes | Asset `EQ-1042`, "Crude Charge Pump A" |
| 6 | **ERP (SAP-like)** — *Plant Manager / Finance* | Cost center, purchase records, criticality class, warranty | `ERP-FAC1-PMP-100-101` |

> 🔑 **The core AI challenge in one sentence:**
> `PMP-100-101` (AM) = `Pump_001` (APM) = `EQ-1042` (MaintWorks) = `ERP-FAC1-PMP-100-101` (ERP)
> = the **same physical pump**, and *nobody has ever written that down.*

*(Applications 1–4 and 6 are from real system. **MaintWorks (CMMS)** is a new fictional application we introduce because the "why is it vibrating?" reasoning needs maintenance history — this is realistic; every real plant has a CMMS like SAP-PM, Maximo, or eMaint.)*

---

## 3. A Day in the Life of the Problem (the pain, dramatized)

**Tuesday, 02:14 AM. Control Room.**

Operator **Maria** is watching her screens. Suddenly the **Alarm Management** system lights up:

> 🔴 `PMP-100-101 VIBRATION HIGH — Priority 1`

Maria has *the Alarm Management view only*. She sees: high vibration, right now, on P-101. That's it.
She doesn't know *why*. She acknowledges the alarm and calls the reliability engineer.

**Reliability engineer Raj** logs into **APM**. His view says `Pump_001` health score dropped from 92 to 61 over three days, with a rising vibration trend. APM *suspects* a mechanical issue — but APM has **no idea** that:

- Last **Thursday**, a maintenance crew replaced the pump's mechanical seal (that fact lives in **MaintWorks**, which Raj doesn't check), **and**
- Last **shift**, an operator increased the pump's flow setpoint by 12% to catch up on production (that fact lives in the **DCS**, in yet another system).

Any *one* of those could explain the vibration. Maybe the new seal was installed slightly misaligned. Maybe the higher flow is pushing the pump past its sweet spot. Maybe both. **But the answer is scattered across four applications that don't talk to each other.**

So what happens today, in real life?
Raj spends **three hours** manually cross-referencing four systems, phoning the maintenance planner, and scrolling historian trends — *just to form a hypothesis.* Meanwhile the pump keeps degrading.

> **This 3-hour manual detective process is exactly what the hackathon teams will automate.**

---

## 4. Why This Is Hard (and why a chatbot alone can't do it)

1. **The same thing has different names everywhere.** No shared ID. Matching requires reasoning over *names, descriptions, hierarchy position, and attributes* — not a simple lookup. → needs **AI entity resolution**.
2. **The truth is a graph, not a table.** Pump → seal → work order → operator → setpoint → historian tag → alarm. Answering "why" means **traversing relationships across systems**. → needs a **knowledge graph**.
3. **The answer requires multi-step reasoning over time.** "The seal changed *last week*" + "setpoint raised *last shift*" + "vibration rose *after*" → a causal chain. → needs an **agent that plans, retrieves, and reasons**, not a single prompt.

This is precisely the *"Why Agentic AI?"* justification: heuristic entity mapping, auto-traversal of the graph, and causal reasoning across sources.

---

## 5. The Vision — What Success Looks Like (the finale demo)

By the end of 3 days, a judge should be able to type a plain-English question and watch an agent do in **30 seconds** what Raj does in 3 hours:

> **Judge:** *"Why is Crude Charge Pump P-101 vibrating?"*
>
> **Agent:** *"P-101's vibration began rising ~18 hours ago. I found two contributing events across systems:*
> *1. A **mechanical seal replacement** was completed 6 days ago (MaintWorks WO-4471). Post-repair vibration baseline shifted up — possible seal misalignment.*
> *2. The **flow setpoint was raised 12%** last shift (DCS, by operator on console U100), pushing the pump above its efficiency curve.*
> *The vibration trend (Historian tag …VIB_01) correlates most strongly with the setpoint change.*
> ***Most likely root cause:** operating point change compounding a recent seal job. **Recommendation:** reduce setpoint to prior value and inspect seal alignment. Confidence: medium-high."*

To answer that, under the hood the agent **must** have:
- ✅ Recognized that AM's `PMP-100-101`, APM's `Pump_001`, and MaintWorks' `EQ-1042` are the **same pump** (entity resolution),
- ✅ Walked the **unified graph** from pump → work order → setpoint → historian trend,
- ✅ **Reasoned** over the timeline to rank causes.

Every capability the teams build ladders up to this one moment.

---

## 5b. The Process Flow & the Hidden Scenarios

The elaborated Unit 100 Crude Distillation flow is drawn in **`refinery_pfd.html`** (open in any browser — hover equipment to see its per-application aliases; colored dots mark the planted scenarios).

**Process line:** Crude Feed Tank → **Charge Pump P-101** → **Preheat Exchanger E-101** → **Fired Heater H-101** → **Distillation Column C-101**. The column separates crude into products by boiling point: **naphtha** overhead → **Condenser E-301** → **Reflux Drum V-201** → **Reflux Pump P-102** (reflux back to column) + naphtha to Unit 300; **kerosene** and **diesel** side draws; and **atmospheric residue** as bottoms. Heat is supplied by fired heater H-101 plus **stripping steam** at the column bottom (a crude atmospheric tower has *no reboiler* — that is factually correct). Side loop: **Recycle Gas Compressor K-101**. Utilities: **CV-400** (BFW/steam + cooling water), feeding H-101 and the K-101 lube-oil cooler.

**Five hidden scenarios planted in the (future) transactional data** — the agent should discover these by reasoning across applications:

| # | Scenario | Equipment | Planted causal chain | Demo question |
|---|----------|-----------|----------------------|---------------|
| **S1** ⭐ | Pump vibration (**FINALE**) | P-101 | Seal replaced 6 days ago (CMMS) **+** flow setpoint +12% last shift (DCS) → vibration rise (Historian) | *"Why is Crude Charge Pump P-101 vibrating?"* |
| **S2** | Exchanger fouling | E-101, H-101 | Outlet temp slowly declining (Historian) → heater fuel-gas up (KPI); cleaning WO deferred twice (CMMS) | *"Why is heater H-101 fuel consumption rising?"* |
| **S3** | Column flooding (**cascade / multi-hop**) | C-101, P-102 | High column dP caused by cold feed (from **S2**) **+** reflux pump P-102 cavitation | *"Why is column C-101 differential pressure high?"* |
| **S4** | Compressor (**DISTRACTOR**) | K-101, CV-400 | High vibration *looks like* P-101, but root cause is lube-oil overtemp from stuck cooling valve CV-400 — a **different** cause | *"Is K-101's problem the same as P-101's?"* |
| **S5** | Alarm flood (**CONFIG, not process**) | TK-201 | Nuisance high-level alarms from a mis-set alarm limit in AM — configuration issue, not a real upset | *"Why are there so many alarms on TK-201?"* |

> **Why these five:** S1 is the money demo. S2→S3 tests genuine **multi-hop causal reasoning** (one problem feeds another). S4 is a **trap** — a good agent must not conflate two vibrating machines with different causes (this also stresses entity resolution). S5 tests whether the agent can tell a **config mistake** apart from a real process event. Together they give judges a rich set of questions to probe live.

---

## 6. The Journey the Teams Will Take (staged, high level)

The problem is deliberately layered so a team can stop at any stage and still have shown something real. (Detailed acceptance criteria and data come later — this is just the arc.)

| Stage | Name | The "aha" a team demonstrates |
|-------|------|-------------------------------|
| **0** | *Meet the elephant* | Load each application's data as its **own independent graph** — six blind men, six views. |
| **1** | *Same elephant* | Use **AI to link entities** across systems: prove `Pump_001` = `PMP-100-101` = `EQ-1042`, with a **confidence score**. |
| **2** | *One elephant* | Merge overlapping **attributes** (criticality, location, description) into one unified entity; resolve conflicts. |
| **3** | *Ask the elephant* | Put a **RAG + knowledge-graph query layer** on top; answer simple cross-app questions ("show all alarms + open work orders for P-101"). |
| **4** | *The elephant reasons* | Expose the graph via an **MCP server / tools**, and build the **agent** that answers *"why is P-101 vibrating?"* with a causal chain. |

> The **"From Standard to Meaning to Reality"** diagram already in this folder maps almost 1:1:
> **CDM (format)** → Stage 0 ingest, **Ontology (meaning)** → Stages 1–2 linking, **Knowledge Graph (reality)** → Stages 3–4 query & reason.

---

## 7. Cast of Personas (for the teams to design *for*)

- **Maria — Control Room Operator.** Lives in Alarm Management. Wants to know *what to do right now*.
- **Raj — Reliability Engineer.** Lives in APM. Wants to know *why an asset is degrading and what caused it*. **This is our hero user for the finale.**
- **Sam — Process Engineer.** Lives in the DCS. Owns setpoints and control loops.
- **Priya — Maintenance Planner.** Lives in MaintWorks. Knows the repair history.
- **The Plant Manager.** Lives in dashboards/ERP. Wants root causes and downtime avoided.

---

## 8. What We Deliberately Kept Fictional (and why it's fair game)

- **"MaintWorks" CMMS** — invented, but mirrors real tools (SAP-PM, IBM Maximo, eMaint). Needed so the agent has *maintenance history* to reason over.
- **Specific events** (seal replacement 6 days ago, +12% setpoint last shift, vibration rise) — invented storyline that the sample data will later encode. These are the "planted clues" the finale agent must find.
- **Health scores, alarm timestamps, technician notes** — will be synthesized to be internally consistent with the storyline above.

Everything is grounded in the real assets and units, so the fictional layer sits naturally on top of the provided data.

---

### One-line pitch to read to the participants on Day 1

> *"Six systems each know one-sixth of the truth about a single vibrating pump. Your job: use AI to stitch them into one brain that can answer 'why?' — the way a senior reliability engineer would, but in 30 seconds instead of 3 hours."*
