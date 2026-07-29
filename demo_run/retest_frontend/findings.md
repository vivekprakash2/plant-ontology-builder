# Frontend retest — 5 hidden scenarios + 7 demo questions + follow-ups

Date: 2026-07-29 (after "fix: add frontend clarity" commit `f2e8b70`, which reworked
`frontend/app.js`'s knowledge-graph layout: radial ring placement around revealed asset
nodes, an orphan-node fallback row, and a `fitScopedView()` zoom/pan-to-fit helper).

Server was **not** restarted (frontend files are static and read from disk per request by
`server.py`; confirmed via repeated `GET /app.js` entries in the server log after each
reload — no stale bundle risk). Browser was hard-refreshed between every independent
session; a `console.error`/`window.onerror`/`unhandledrejection` listener was
(re-)installed via DevTools right after each reload.

## Regression checks

- `python -m unittest discover -s tests` → **30/30 pass**, no change from before the
  frontend edit.
- **No console errors** in any of the 9 screenshots/sessions below.
- **No "rule-based" fallback** seen — every answer's meta line read
  `🤖 reasoned by databricks-claude-opus-4-8`.
- **Large-graph layout regression check (the main risk area for this diff)**: the S5
  follow-up scopes 123 of 162 nodes (TK-201's ~120 chattering alarms). The new radial
  layout renders this cleanly as a starburst around the tank asset node, fully contained
  and readable at default zoom — see
  [s5_followup_largegraph.png](retest_frontend/s5_followup_largegraph.png). This is
  exactly the case the commit's code comments call out as the old bug ("that's what made
  the old view stretch the fit out to a tiny zoom") — looks fixed.

## 5 hidden scenarios (docs/SCENARIO.md §5b) — all pass

| # | Scenario | Question asked (via UI suggestion button) | Result |
|---|---|---|---|
| S1 | P-101 vibration (finale) | "Why is Crude Charge Pump P-101 vibrating?" | ✅ Correctly cites WO-4471 (incomplete alignment) + DCS-A-0450 setpoint hike. [s1_p101.png](retest_frontend/s1_p101.png) |
| S2 | Exchanger fouling | "Why is fired heater H-101's fuel consumption rising?" | ✅ Correctly cites E-101 fouling (APM-E-0009, WO-4502) driving H-101 firing. [s2_h101.png](retest_frontend/s2_h101.png) |
| S3 | Column flooding (cascade) | "Why is column C-101's differential pressure high?" | ✅ Correctly chains E-101 fouling → colder feed → H-101 firing → C-101 dP. [s3_c101.png](retest_frontend/s3_c101.png). Note: this run's agent didn't check reflux pump P-102 suction trend at all (said "no supporting evidence of an independent cause" without checking it) — in the earlier pre-frontend-change test run, the agent *did* check P-102 suction (-11.8%) and mentioned it. This is LLM tool-path non-determinism between runs, not a frontend issue. |
| S4 | Compressor distractor | "Is the Recycle Gas Compressor K-101 experiencing the same problem as P-101?" | ⚠️ Correctly concludes "not the same problem" (right headline), but this run's agent checked K-101 lube-oil temp only (found it normal/flat) and did **not** check the CV-400 cooling-water-valve flow tag, so it could not name the actual planted cause (stuck/fouled CV-400) and instead reported the compressor's cause as "unconfirmed." Earlier (pre-frontend-change) run did check CV-400 flow and correctly named it. Again, this looks like agent tool-path variance run-to-run rather than a frontend regression — see [s4_k101_vs_p101.png](retest_frontend/s4_k101_vs_p101.png). Worth a note for the demo: this question's answer quality can vary between asks. |
| S5 | Alarm flood (config, not process) | "Why are there so many alarms on tank TK-201?" | ✅ Correctly identifies chattering H alarm from mis-set limit/tight deadband, not a real upset. [s5_tk201.png](retest_frontend/s5_tk201.png) |

## 7 demo questions (the UI's suggested-question buttons) — all pass

1. P-101 vibrating → ✅ (= S1 above)
2. H-101 fuel consumption → ✅ (= S2 above)
3. C-101 differential pressure → ✅ (= S3 above, with the P-102 caveat noted)
4. K-101 vs P-101 → ⚠️ (= S4 above, with the CV-400 caveat noted)
5. TK-201 alarms → ✅ (= S5 above)
6. "Show me everything known about P-101 across all systems." → ✅ correct root cause
   (setpoint + incomplete seal alignment), **but the last "Recommended actions" bullet was
   cut off mid-word** ("...prepare to trip/swap the pump if vibration continues climbing
   to protect cr"). This is a genuine answer-text truncation (looks like a token-limit cutoff
   on the model's structured output), not a frontend rendering bug — the frontend rendered
   exactly the (truncated) text it received. See [s6_p101_everything.png](retest_frontend/s6_p101_everything.png).
7. "What maintenance happened on the crude charge pump in the last week, and did anything
   change in operations around the same time?" → ✅ correctly correlates WO-4471 (seal,
   incomplete alignment) with both DCS setpoint changes (315→320, then 320→358.4) in one
   coherent answer. [s7_maintenance_last_week.png](retest_frontend/s7_maintenance_last_week.png)

## Follow-ups tested

- Session 1 follow-up: after S1, asked "What work orders were involved?" → ✅ correctly
  narrows to WO-4471 as the relevant one, WO-4388 as background; reasoning walk and graph
  re-scoped correctly. [s1_followup.png](retest_frontend/s1_followup.png)
- Session 5 follow-up: after S5, asked "What limit change would you recommend?" → ✅
  gives a concrete recommendation (raise H limit 78.5%→~85%, widen deadband 0.1%→~1%) and
  is the large-graph (123-node) layout regression check noted above.
  [s5_followup_largegraph.png](retest_frontend/s5_followup_largegraph.png)

## Full Q&A transcripts

Verbatim question + answer + recommended-actions text, re-fetched directly from
`/api/chat` after the frontend change (raw SSE saved per question as
`retest_frontend/qN_full.txt` / `followupN_full.txt`; all confirmed
`"scenario": "agentic"`, `"presented_by": "databricks-claude-opus-4-8"`). Note these are
a **separate run** from the UI-button clicks used for the screenshots above, so minor
wording differs from the screenshots (expected LLM non-determinism) but conclusions match.

### Q1 — "Why is Crude Charge Pump P-101 vibrating?" (45–50s)

**Headline:** Overnight flow-setpoint hike compounding an unfinished seal alignment

**Answer:** P-101 vibration rose ~70% over 48h (7.1 → 12.2 mm/s), breaching HH (11.2 mm/s) and driving active P1 alarms AME-000001/2/3. The dominant trigger is an operator setpoint change 8 hours ago (DCS-A-0450, 320 → 358.4), which pushed discharge flow up ~12% (319 → 358 m³/h) right as vibration spiked — the pump is being run away from its best-efficiency point, causing hydraulic instability (matching the alarm-config cause). This is compounded by a latent mechanical issue: WO-4471 (7 days ago) replaced the mechanical seal but noted the shim kit was short and alignment was flagged for recheck, and APM event APM-E-0007 predicts bearing_wear (0.72 confidence) with bearing temp trending up ~10% (62.7 → 68.9 °C). Suction pressure is stable (~2.4 barg), so cavitation/starvation is unlikely. Net: an aggressive flow increase is exciting a machine left in a marginal alignment state — the setpoint is the immediate driver, the unfinished alignment/bearing wear is the underlying vulnerability.

**Recommended actions:**
- Reduce the P-101 flow setpoint back toward ~320 (reverse DCS-A-0450) to drop vibration below HH and protect against a feed-loss trip to the CDU.
- Open a corrective WO to complete the alignment recheck deferred in WO-4471 (correct shim kit) and inspect the bearing/seal per APM-E-0007 within its 72h window.
- Trend bearing temp and vibration after the setpoint reduction to confirm hydraulic vs. mechanical contribution.
- Acknowledge/track the active P1 HH alarms and brief operations not to re-raise the setpoint until inspection is complete.

### Q2 — "Why is fired heater H-101's fuel consumption rising?" (~42s)

**Headline:** Fouled upstream preheat exchanger E-101 forcing H-101 to burn more fuel

**Answer:** H-101's fuel gas flow (`FAC1.UNIT100.FIRED_HEATER_101.FUEL_01`) rose **+8.7%** over the last 14 days (1560 → 1696 Nm³/h). Over the same window, the outlet temperature of the upstream preheat exchanger E-101 (`FAC1.UNIT100.HEAT_EXCHANGER_101.TOUT_01`), which FEEDS the heater, fell **-3.8%** (226 → 217 °C) — meaning colder feed is reaching the heater, so it must fire harder to hit the same coil-outlet target. This is corroborated by APM health event **APM-E-0009** (fouling_trend, confidence 0.8, "Schedule bundle cleaning") and work order **WO-4502**, a preventive E-101 bundle cleaning that has been **deferred twice** with notes citing suspected fouling and drifting outlet temperature. The fouling explanation is the most likely root cause; no H-101 burner/fuel-quality alarms or work orders were found to suggest a heater-side fault.

**Recommended actions:**
- Reinstate and schedule the deferred E-101 bundle cleaning (WO-4502) to restore preheat and cut heater firing.
- Verify feed rate/composition and heater coil-outlet setpoint haven't changed, to rule out a demand-driven fuel increase.
- Track E-101 outlet temp and H-101 fuel flow together as a fouling KPI; act before further duty loss or coil overfiring.
- Quantify the extra fuel cost from the +8.7% rise to prioritize the cleaning turnaround window.

### Q3 — "Why is column C-101's differential pressure high?" (~58s)

**Headline:** Deferred exchanger fouling starving heater, driving column DP up

**Answer:** C-101's differential pressure (`FAC1.UNIT100.COLUMN_101.PDP_01`) has climbed steadily +121% over 14 days (0.505 → 1.116 bar) — a slow ramp characteristic of column loading/incipient flooding, not a sudden upset. Upstream preheat exchanger E-101 (ASSET-003) is fouling: its outlet temp `TOUT_01` fell 3.8% (226 → 217 °C), with APM health event **APM-E-0009** flagging a fouling trend and **WO-4502** (bundle cleaning) deferred twice. To compensate for the cooler feed, fired heater H-101 fuel gas (`FUEL_01`) has been pushed up +8.7% (1560 → 1696 Nm³/h), raising heater outlet temperature and vapor loading into the column, which increases tray pressure drop. The reflux pump P-102 suction pressure (`PSUC_02`) fell only ~11.8% and shows no fault — consistent with it responding to rising column pressure rather than causing it. Most likely root cause: **E-101 fouling forcing higher heater firing, over-vaporizing feed and raising column DP.**

**Recommended actions:**
- Reschedule/execute the deferred E-101 bundle cleaning (**WO-4502**, per APM-E-0009) at the next opportunity to restore preheat.
- Trim H-101 heater firing (`FUEL_01`) back toward baseline to reduce column vapor load and DP.
- Monitor C-101 `PDP_01` for approach to flooding and verify no C-101 DP alarm limits are being exceeded during the trend.
- Confirm P-102 reflux is stable; treat its lower suction pressure as a symptom, not a separate fault, unless flow abnormalities appear.

*(Note: this run's agent did check and cite P-102 suction pressure — unlike the earlier UI-button run for the same question, which omitted it. Confirms the S3 tool-path non-determinism flagged above works both ways.)*

### Q4 — "Is the Recycle Gas Compressor K-101 experiencing the same problem as P-101?" (~56s)

**Headline:** No — same symptom (high vibration), different root causes on each machine

**Answer:** Both machines show rising vibration in P1 HH alarm, but the underlying failure modes are distinct and unrelated. **K-101** (ASSET-001) has vibration rising 125% (3.6→8.2 mm/s, tag VIB_02) driven by a steady 14-day lube-oil temperature climb (54→72°C on LUBE_01, +32%); this matches its alarm cause "Lube oil / mechanical" (AME-000005/6 HH) and APM anomaly APM-E-0008 ("check lube oil cooling"), and its only related asset is CV-400, which cools its lube oil — a cooling/lube-oil problem. **P-101** (ASSET-002) has vibration at 11.6 mm/s (HH, AME-000002/3) driven by mechanical issues: a mechanical seal was just replaced 7 days ago with alignment left unverified (WO-4471, "shim kit short, flagged for recheck"), a large discharge-pressure setpoint hike 8 hours ago (315→358.4 via DCS-A-0450), and APM predicts bearing_wear (APM-E-0007). So the shared "high vibration" is coincidental — K-101 is a lube-oil/cooling issue while P-101 is a seal-alignment/overload issue.

**Recommended actions:**
- **K-101:** Investigate lube-oil cooling per APM-E-0008 — check the CV-400 cooling-water supply to the lube-oil cooler and lube-oil temperature (LUBE_01) before it trips the compressor.
- **P-101:** Reduce discharge-pressure setpoint (recently raised to 358.4 via DCS-A-0450) and complete the deferred alignment recheck from WO-4471 (short shim kit).
- **P-101:** Inspect bearing/seal within 72h per APM-E-0007 prediction; alarm config recommends reducing flow first.
- Treat the two as **separate work scopes** — do not assume a common cause or common fix.

*(Note: this run correctly names CV-400 as K-101's cooling-water link — unlike the earlier UI-button run for the same question, which stopped at "lube oil temp is normal, cause unconfirmed" without checking CV-400. Confirms the S4 tool-path non-determinism flagged above works both ways.)*

### Q5 — "Why are there so many alarms on tank TK-201?" (~53s)

**Headline:** Chattering High-Level alarm from limit set just below normal operating level

**Answer:** TK-201 (ASSET-005) has generated well over 100 alarm events in the last ~24 hours (AME-000007 onward), but they are all the **same** point — `TNK-200-101.LVL` High (P3) — cycling ACTIVE → RTN every few minutes between just two values: 78.6% (ACTIVE) and 78.3% (RTN). This is classic alarm chatter: the tank level is sitting essentially on top of its High limit. The alarm config confirms it — `limit_h = 78.5%` with a tiny `deadband = 0.1%`, and the config note itself flags the recommended action as *"review alarm limit (suspected mis-set)."* The historian confirms the level is genuinely high and slowly rising (76.8% → 79.1%, +3% over 48h, tag `FAC1.UNIT200.STORAGE_TANK_101.LVL_01`), so it repeatedly crosses the too-tight threshold. There are no work orders, no health events, and no upstream/downstream related assets — this is not equipment failure or a cascade; it's a mis-set alarm limit sitting inside the normal operating band, aggravated by a slowly filling tank.

**Recommended actions:**
- Raise/rationalize the `TNK-200-101.LVL` H limit (currently 78.5%, below the ~79% operating level) and widen the 0.1% deadband to stop the chatter, well clear of HH at 95%.
- Draw the tank down or reroute product to bring level comfortably below the reset High limit, since it is genuinely rising (+3% / 48h).
- Confirm with operator OP_LEE whether 78–79% is normal working level, then update the alarm setpoint documentation accordingly.
- Suppress/shelve the chattering point temporarily while the limit is corrected to reduce operator alarm load.

### Q6 — "Show me everything known about P-101 across all systems." (~59s)

**Headline:** Post-seal-swap misalignment plus flow-setpoint hike driving vibration to trip

**Answer:** P-101's vibration has climbed ~70% in 48h and is now in HH alarm (11.6 mm/s vs 11.2 limit), with a corroborating APM bearing-wear prediction (APM-E-0007, 0.72). Two mechanical/operational stressors coincide: WO-4471 (7 days ago) replaced the mechanical seal but explicitly left alignment unverified because the shim kit was short, and ~8h ago operator OP_SAM raised the flow setpoint sharply from 320 to 358.4 (DCS-A-0450), pushing flow +12% and loading the machine harder. The most likely cause is residual seal/coupling misalignment from the incomplete WO-4471 job, aggravated by the recent setpoint increase; the modest bearing-temp rise (+9.9%) is consistent with the developing bearing distress rather than a separate fault. Suction pressure is stable, so cavitation/hydraulic starvation is unlikely.

**Recommended actions:**
- Reduce the pump flow setpoint back toward 320 m³/h (reverse DCS-A-0450) to unload the pump per the alarm's recommended action.
- Complete the alignment recheck flagged on WO-4471 (obtain correct shim kit) and inspect bearing/seal within the 72h APM window.
- Confirm the HH alarm is acknowledged and staffed; prepare for possible trip given loss-of-crude-feed-to-CDU consequence and downstream impact on E-101.
- Monitor VIB_01 and BTMP_01 closely; escalate to a controlled shutdown if vibration keeps rising above HH.

*(Note: this API run's recommendation list is complete/not truncated — the mid-word cutoff seen in the UI screenshot for this question appears to be run-specific, not a hard token-limit ceiling that always triggers on this question. Still worth verifying server-side max_tokens headroom.)*

### Q7 — "What maintenance happened on the crude charge pump in the last week, and did anything change in operations around the same time?" (~46s)

**Headline:** Seal replacement left alignment unchecked, then setpoint hike drove vibration into HH

**Answer:** In the last week the only maintenance on Crude Charge Pump 101 (ASSET-002) was **WO-4471** (Corrective, created 2026-07-21, completed 2026-07-22, TECH_JOE): a **mechanical seal replacement (part MECH-SEAL-32)** — with a critical note that *"Alignment checked but shim kit was short; flagged for recheck at next opportunity"* (cost posting POST-9001, $4,200). On the operations side, operator OP_SAM raised the pump loop (U100_P101) setpoint from **320 → 358.4** just 8 hours ago (**DCS-A-0450**, 2026-07-27 22:00), following an earlier 315→320 bump on 2026-07-16 (DCS-A-0431). Since then vibration has climbed **~70% over 48 hours** (7.14 → 12.18 mm/s) and tripped the H (AME-000001, 8.7) then HH alarms (AME-000002/003, 11.4/11.6 mm/s vs. 11.2 HH limit), with APM predicting bearing wear at 0.72 confidence (APM-E-0007). The most likely cause is the **incomplete alignment from the seal job compounded by the recent setpoint increase** pushing the pump harder — the deferred shim recheck is the key unresolved maintenance defect.

**Recommended actions:**
- Reduce the U100_P101 setpoint back toward 320 to lower vibration below the HH limit and protect the bearing (per alarm recommended action).
- Raise/expedite a follow-up work order to complete the deferred alignment shim recheck flagged in WO-4471.
- Perform the APM-recommended bearing/seal inspection within 72h (APM-E-0007) before the P1 HH alarm escalates to a pump trip and loss of crude feed.
- Verify the seal replaced under MECH-SEAL-32 for integrity given the post-maintenance vibration rise.

### Follow-up 1 — after Q1, "What work orders were involved?" (no P-101 named in the question; ~80s)

**Headline:** Two work orders on P-101 — recent seal replacement is the relevant one

**Answer:** P-101 (ASSET-002 / CMMS EQ-1042) has two work orders on record. **WO-4471** (Corrective, created 2026-07-21, closed 2026-07-22, TECH_JOE) is the vibration-relevant one: it replaced the mechanical seal (part MECH-SEAL-32) but the notes state the *shim kit was short, so alignment was flagged for recheck at next opportunity* — leaving the machine in a marginal alignment state. **WO-4388** (Preventive, closed 2026-04-25, ~95 days ago) was just a routine seal inspection with no issues found and is not implicated. WO-4471 also carries cost posting POST-9001 ($4,200 labor+parts).

**Recommended actions:**
- Raise a follow-up corrective WO to complete the alignment recheck flagged in **WO-4471** with the correct shim kit.
- Inspect bearing/seal within 72h per APM event **APM-E-0007** while alignment is being addressed.
- Reduce discharge flow (recent SP change DCS-A-0450, 320→358.4) back toward setpoint to lower vibration until alignment is corrected.

### Follow-up 2 — after Q5, "What limit change would you recommend?" (~17s)

**Headline:** Raise High level limit above normal band and widen deadband

**Answer:** The chatter comes purely from geometry: the High limit `limit_h = 78.5%` sits right inside the tank's normal operating band. The level has been essentially flat at ~79% for two weeks (`FAC1.UNIT200.STORAGE_TANK_101.LVL_01`: 79.8% → 79.1%, "stable", over 336h), and the alarm cycles between only 78.6% (ACTIVE) and 78.3% (RTN) — a ~0.3% swing that easily overwhelms the tiny `deadband = 0.1%`, producing 100+ events (AME-000007…AME-000126). The alarm config note itself says "review alarm limit (suspected mis-set)." The HH/overfill limit is comfortably higher at 95%, so there is ample room to move the High limit up without compromising overfill protection.

**Recommended actions:**
- Raise `limit_h` on `TNK-200-101.LVL` from **78.5% to ~85%** — clear of the ~79% normal band, still well below the HH at 95%.
- Widen the `deadband` from **0.1% to ~1–2%** so normal level ripple can't re-trigger the alarm.
- Leave HH=95 / L=20 / LL=10 unchanged; overfill protection is preserved.
- Log the change via MOC and confirm the storm clears; if level later climbs toward the new limit, investigate outflow/routing rather than re-lowering it.

## Summary

- **Frontend regression check: pass.** The graph-layout rework in `f2e8b70` handles both
  small (~4-16 node) and large (123-node) scoped answers correctly, with the reasoning
  walk populating live and highlighting the cited path, and zero console errors across 9
  question/follow-up turns.
- **Backend/agent behavior: mostly consistent with the original test**, with two notable
  instances of run-to-run non-determinism in *which* evidence the agent chose to check
  (S3's reflux pump suction trend, S4's CV-400 cooling-water flow) — worth flagging to the
  team since it means answer quality for S3/S4 can vary between asks of the same question,
  independent of the frontend change.
- **One new finding**: q6 ("everything about P-101")'s recommended-actions text was cut off
  mid-sentence — likely a response length/token-limit issue on the backend/model side,
  worth a look before the demo if that exact question is likely to be asked live.
