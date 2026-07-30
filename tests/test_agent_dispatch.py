"""Regression tests for the reasoning agent -- both answer paths.

**Deterministic fallback (`_dispatch`).** The 7 demo questions from
docs/TEAM_HANDBOOK.md §7 (scenarios S1-S5 + unification + cross-app join) AND
the variant phrasings that previously produced confidently wrong answers --
the handlers must derive conclusions from graph evidence, never from a
per-scenario script:

  - "Why are there so many alarms on P-101?" must NOT get S5's nuisance
    verdict (P-101's 3 alarms are real vibration excursions).
  - "Is P-102 experiencing the same problem as P-101?" must NOT get S4's
    hardcoded lube-oil/CV-400 narrative.
  - "Why is the compressor vibrating?" must find K-101's actual cause
    (upstream cooling problem overheating the lube oil), not "no clear cause".

**Agentic path.** Live retests kept finding the same class of failure -- the
agent concluding about an asset it hadn't properly examined -- so the guards
against it are tested here too, via pure functions and a scripted fake
provider. They cover three distinct stages of that failure:

  - `TestHistorianTrendTool` -- never let the model see only the misleading
    window (K-101's lube oil is "stable" over 48h, "+32%" over 336h).
  - `TestCompletionGate` -- refuse an answer that claims something about a
    related asset the agent never queried at all.
  - `TestDismissedTrendWarningDetection` -- catch the answer that DID query it,
    was handed the long-window warning, and dismissed it anyway.

Everything here runs entirely offline: `_dispatch` is the no-LLM path, and the
agentic tests drive pure functions or a scripted provider. Note the corollary
recorded in docs/CHAT_AGENT.md §5 -- a green suite still is not evidence that a
live model behaves, only that these mechanisms do.

Run with:
    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import unittest

from ontology_builder.agent import (
    _MAX_HISTORY_CHARS,
    _MAX_HISTORY_TURNS,
    _asset_mention_terms,
    _dispatch,
    _find_dismissed_trend_warnings,
    _history_messages,
    _run_agentic_events,
    _tag_owner_aliases,
    _tool_get_historian_trend,
    _trend_result_for_llm,
    build_ui_panel,
)
from ontology_builder.pipeline import load_or_build


class TestDispatch(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.entities, cls.kg = load_or_build()

    def ask(self, question: str):
        return _dispatch(question, self.entities, self.kg)

    # --- The 7 canonical demo questions (TEAM_HANDBOOK.md §7) ---

    def test_q1_p101_vibration_finale(self) -> None:
        a = self.ask("Why is Crude Charge Pump P-101 vibrating?")
        self.assertEqual(a.scenario, "vibration")
        self.assertEqual(a.confidence, "medium-high")
        self.assertIn("Setpoint", a.headline)
        self.assertIn("seal", a.answer.lower())
        self.assertIn("WO-4471", a.answer)  # cites the seal work order
        self.assertIn("DCS-A-0450", a.answer)  # cites the setpoint change

    def test_q2_h101_fuel_rising(self) -> None:
        a = self.ask("Why is fired heater H-101's fuel consumption rising?")
        self.assertEqual(a.scenario, "fuel_rising")
        self.assertIn("fouling", a.answer.lower())
        self.assertIn("WO-4502", a.answer)  # cites the deferred cleaning WO

    def test_q3_c101_differential_pressure_multihop(self) -> None:
        a = self.ask("Why is column C-101's differential pressure high?")
        self.assertEqual(a.scenario, "high_dp")
        self.assertIn("cold feed", a.answer.lower())
        self.assertIn("cavitation", a.answer.lower())

    def test_q4_k101_distractor_different_cause(self) -> None:
        a = self.ask("Is the Recycle Gas Compressor K-101 experiencing the same problem as P-101?")
        self.assertEqual(a.scenario, "same_problem_comparison")
        self.assertTrue(a.headline.startswith("No"))
        self.assertIn("cooling", a.answer.lower())  # K-101's real cause
        self.assertIn("seal", a.answer.lower())  # P-101's real cause

    def test_q5_tk201_alarm_flood_config(self) -> None:
        a = self.ask("Why are there so many alarms on tank TK-201?")
        self.assertEqual(a.scenario, "alarm_flood_config")
        self.assertIn("mis-set", a.answer.lower())
        self.assertIn("78.5", a.answer)  # cites the actual configured H limit

    def test_q6_everything_about_p101(self) -> None:
        a = self.ask("Show me everything known about P-101 across all systems.")
        self.assertEqual(a.scenario, "full_context")
        for system in ("AM=", "APM=", "CMMS=", "DCS=", "ERP=", "Historian="):
            self.assertIn(system, a.answer)
        # A pure lookup gets no invented advice.
        self.assertIsNone(a.recommendation)

    def test_q6_panel_enumerates_records_from_every_system(self) -> None:
        """The unification question's whole point is showing the records it
        unified -- they were previously all dropped from the panel/walk
        because evidence was tagged with raw node labels ("AlarmEvent")
        instead of _describe_record's type keys ("alarm_event")."""
        a = self.ask("Show me everything known about P-101 across all systems.")
        panel = build_ui_panel(self.entities, self.kg, a)
        self.assertGreaterEqual(len(panel["evidence"]), 10)
        # Dated events must span all five transactional systems.
        self.assertEqual(
            {e["source"] for e in panel["timeline"]}, {"AM", "APM", "CMMS", "DCS", "ERP"}
        )
        # ...and the graph walk should light up that whole neighbourhood.
        self.assertGreaterEqual(len(panel["walk"]), 10)

    def test_diagnostic_answer_stays_lean(self) -> None:
        """Guard the other side of the same change: a vibration answer must
        NOT start listing every historian tag as an evidence card -- its
        relevant trends already render as charts."""
        a = self.ask("Why is Crude Charge Pump P-101 vibrating?")
        panel = build_ui_panel(self.entities, self.kg, a)
        self.assertLessEqual(len(panel["evidence"]), 6)
        self.assertGreaterEqual(len(panel["charts"]), 1)

    def test_q7_maintenance_ops_join(self) -> None:
        a = self.ask(
            "What maintenance happened on the crude charge pump in the last week, "
            "and did anything change in operations around the same time?"
        )
        self.assertEqual(a.scenario, "maintenance_ops_join")
        self.assertIn("WO-4471", a.answer)
        self.assertIn("Yes", a.answer)

    # --- Variant phrasings that previously produced wrong answers ---

    def test_variant_alarms_on_p101_not_called_nuisance(self) -> None:
        """P-101's 3 alarms are REAL vibration excursions (S1), not S5's
        config chatter -- the nuisance verdict must be evidence-gated."""
        a = self.ask("Why are there so many alarms on P-101?")
        self.assertNotEqual(a.scenario, "alarm_flood_config")
        self.assertNotIn("nuisance", (a.headline or "").lower())
        self.assertNotIn("mis-set", (a.headline or "").lower())
        # Hands off to the real root-cause analysis (S1: setpoint + seal).
        self.assertEqual(a.scenario, "vibration")
        self.assertIn("not an alarm flood", a.answer)
        self.assertIn("seal", a.answer.lower())

    def test_variant_p102_vs_p101_no_fabricated_lube_oil(self) -> None:
        """Comparing P-102 to P-101 must not replay S4's K-101 narrative."""
        a = self.ask("Is P-102 experiencing the same problem as P-101?")
        self.assertEqual(a.scenario, "same_problem_comparison")
        self.assertNotIn("lube", a.answer.lower())
        self.assertNotIn("cooling valve", a.answer.lower())
        # The evidence-derived causes: P-101 setpoint+seal, P-102 cavitation.
        self.assertIn("cavitation", a.answer.lower())
        self.assertIn("seal", a.answer.lower())

    def test_variant_k101_vibration_finds_real_cause(self) -> None:
        """'Why is the compressor vibrating?' must reach S4's actual root
        cause (upstream cooling problem -> lube-oil overheating), not
        'no clear cause found'."""
        a = self.ask("Why is the compressor vibrating?")
        self.assertEqual(a.scenario, "vibration")
        self.assertEqual(a.confidence, "medium-high")
        self.assertIn("lube-oil", (a.headline or "").lower())
        self.assertIn("WO-4530", a.answer)  # cites CV-400's open work order

    def test_variant_tk201_short_phrasing_still_flood(self) -> None:
        a = self.ask("Why does TK-201 keep alarming?")
        self.assertEqual(a.scenario, "alarm_flood_config")

    # --- Graceful non-answers ---

    def test_unresolved_open_ended_question(self) -> None:
        a = self.ask("What is alarming right now?")
        self.assertEqual(a.scenario, "unresolved")

    def test_unresolved_ambiguous_bare_c101(self) -> None:
        # "C-101" alone is the handbook's deliberate trap (compressor alias vs
        # column code) -- the fallback declines rather than guessing.
        a = self.ask("What is wrong with C-101?")
        self.assertEqual(a.scenario, "unresolved")


class TestHistorianTrendTool(unittest.TestCase):
    """`get_historian_trend` must always hand the agent BOTH a short and a long
    view of a tag.

    This exists because of a measured failure: K-101's lube-oil temperature is
    `stable -0.3%` over 48h but `rising +32.1%` over 336h (it climbed
    54 -> 72 degC, then plateaued). Every live agentic run that happened to ask
    for 48h concluded the cooling valve CV-400 was "not implicated"; every run
    that asked for 336h named it correctly. The tool now removes that choice.

    Note this is one of the few tests that exercises an AGENTIC-path code path
    rather than the deterministic fallback -- see docs/CHAT_AGENT.md Sec 5.
    """

    LUBE = "FAC1.UNIT100.GAS_COMPRESSOR_101.LUBE_01"
    VIB = "FAC1.UNIT100.CENTRIFUGAL_PUMP_101.VIB_01"

    @classmethod
    def setUpClass(cls) -> None:
        cls.entities, cls.kg = load_or_build()

    def call(self, tag: str, window_hours: int):
        return _tool_get_historian_trend(self.entities, self.kg, {"tag": tag, "window_hours": window_hours})

    def test_short_window_still_surfaces_the_long_trend(self) -> None:
        r = self.call(self.LUBE, 48)
        self.assertEqual(r["direction"], "stable")  # the misleading short view
        longer = [o for o in r["other_windows"] if o["window_hours"] == 336]
        self.assertEqual(len(longer), 1)
        self.assertEqual(longer[0]["direction"], "rising")
        self.assertGreater(longer[0]["pct_change"], 30)
        # ...and it must say so explicitly, not just include the number.
        self.assertIn("WINDOW MATTERS", r["trend_note"])

    def test_long_window_request_also_gets_the_short_view(self) -> None:
        r = self.call(self.LUBE, 336)
        self.assertEqual(r["direction"], "rising")
        self.assertEqual([o["window_hours"] for o in r["other_windows"]], [48])

    def test_no_note_when_both_windows_agree(self) -> None:
        r = self.call(self.VIB, 48)
        self.assertEqual(r["direction"], "rising")
        self.assertTrue(all(o["direction"] == "rising" for o in r["other_windows"]))
        self.assertNotIn("trend_note", r)  # don't cry wolf on agreeing windows

    def test_chart_pipeline_shape_preserved(self) -> None:
        """The requested window's fields must stay at the TOP level, and the
        chart-only `points` array must be stripped before the result is sent to
        the model."""
        r = self.call(self.VIB, 48)
        for key in ("tag", "direction", "pct_change", "n_readings", "start_ts", "end_ts", "points"):
            self.assertIn(key, r)
        self.assertNotIn("points", _trend_result_for_llm(r))


class TestDismissedTrendWarningDetection(unittest.TestCase):
    """The next layer of defense after TestHistorianTrendTool above: even with
    `get_historian_trend` always handing back a `trend_note` warning (and
    _AGENT_SYSTEM_PROMPT rule 5 telling the model never to dismiss a cause on
    a short-window reading alone), a retest still measured a live run write a
    final answer clearing CV-400 as "not implicated" after fetching CWFL_01
    over 48h only, ignoring the note it was handed. `_find_dismissed_trend_warnings`
    is a deterministic check for exactly that pattern -- see its docstring and
    the corrective retry built around it in `_run_agentic_events`.

    Runs entirely offline: it only touches the pure functions, not the LLM.
    """

    CWFL = "FAC1.UNIT400.CONTROL_VALVE_101.CWFL_01"  # CV-400's cooling-water flow

    @classmethod
    def setUpClass(cls) -> None:
        cls.entities, cls.kg = load_or_build()

    def test_aliases_include_the_informal_equipment_code(self) -> None:
        # "CV-400" is what every real transcript calls this valve, but it
        # isn't a clean alias field anywhere -- it's embedded parenthetically
        # in the Historian profile's own name ("Cooling Water Flow (CV-400)").
        aliases = _tag_owner_aliases(self.kg, self.entities, self.CWFL)
        self.assertIn("CV-400", aliases)
        self.assertIn("Boiler Feed Flow Control Valve", aliases)

    def test_flags_a_dismissal_that_contradicts_the_trend_note(self) -> None:
        trend = _tool_get_historian_trend(self.entities, self.kg, {"tag": self.CWFL, "window_hours": 48})
        self.assertIn("trend_note", trend)  # sanity: this tag does disagree across windows
        trace = [{"tool": "get_historian_trend", "arguments": {"tag": self.CWFL}, "result": trend}]
        content = (
            "K-101's cooling-water supply from CV-400 is steady, and with cooling confirmed "
            "healthy this is not implicated in the vibration rise."
        )
        warnings = _find_dismissed_trend_warnings(trace, self.kg, self.entities, content)
        self.assertEqual(len(warnings), 1)
        self.assertIn("WINDOW MATTERS", warnings[0])

    def test_does_not_flag_an_answer_that_heeds_the_warning(self) -> None:
        trend = _tool_get_historian_trend(self.entities, self.kg, {"tag": self.CWFL, "window_hours": 48})
        trace = [{"tool": "get_historian_trend", "arguments": {"tag": self.CWFL}, "result": trend}]
        content = (
            "CV-400's cooling-water flow fell ~14% over the last 14 days even though it reads "
            "stable over the last 48h -- consistent with a sticking valve (WO-4530)."
        )
        self.assertEqual(_find_dismissed_trend_warnings(trace, self.kg, self.entities, content), [])

    def test_does_not_flag_when_asset_isnt_mentioned(self) -> None:
        trend = _tool_get_historian_trend(self.entities, self.kg, {"tag": self.CWFL, "window_hours": 48})
        trace = [{"tool": "get_historian_trend", "arguments": {"tag": self.CWFL}, "result": trend}]
        content = "Something else entirely is not implicated in this vibration rise."
        self.assertEqual(_find_dismissed_trend_warnings(trace, self.kg, self.entities, content), [])

    def test_does_not_flag_when_windows_agree(self) -> None:
        # VIB_01 rises consistently in both windows (no trend_note at all),
        # so even dismissive-sounding text about it should never be flagged.
        trend = _tool_get_historian_trend(
            self.entities, self.kg, {"tag": "FAC1.UNIT100.CENTRIFUGAL_PUMP_101.VIB_01", "window_hours": 48}
        )
        self.assertNotIn("trend_note", trend)
        trace = [{"tool": "get_historian_trend", "arguments": {"tag": trend["tag"]}, "result": trend}]
        content = "Crude Charge Pump 101's vibration is not implicated in anything else."
        self.assertEqual(_find_dismissed_trend_warnings(trace, self.kg, self.entities, content), [])


class TestCompletionGate(unittest.TestCase):
    """The agentic loop must refuse a final answer that makes a claim about a
    related asset it never queried.

    Driven by a scripted fake provider, so this tests real agentic-path code
    with no LLM. The failure it guards against was measured live: the agent
    called `get_related_assets(K-101)`, saw cooling valve CV-400 listed, never
    queried it, then asserted "its cooling-water supplier CV-400 is not
    implicated" -- contradicting CV-400's OPEN corrective work order WO-4530.
    """

    BAD = (
        "## Headline\nNo -- different problems\n\n## Root Cause\nK-101 vibration rose ~125% but "
        "its lube-oil temp is stable and its cooling-water supplier CV-400 is not implicated.\n\n"
        "## Recommended Actions\n- Inspect K-101"
    )
    GOOD = (
        "## Headline\nNo -- K-101 is a cooling problem\n\n## Root Cause\nCV-400 has an open work "
        "order WO-4530 and cooling-water flow is down 14%.\n\n## Recommended Actions\n- Expedite WO-4530"
    )
    NO_MENTION = (
        "## Headline\nNo -- different problems\n\n## Root Cause\nP-101 is seal-driven; K-101 shows "
        "a bearing signature.\n\n## Recommended Actions\n- Inspect K-101 bearings"
    )

    @classmethod
    def setUpClass(cls) -> None:
        cls.entities, cls.kg = load_or_build()

    @staticmethod
    def _call(name, args):
        return {"id": "1", "function": {"name": name, "arguments": json.dumps(args)}}

    def _run(self, script):
        """Returns (gate_event_count, final_answer)."""

        class Scripted:
            model = "fake-model"

            def __init__(self, steps):
                self.steps = list(steps)

            def chat(self, messages, tools=None, max_tokens=600):
                return self.steps.pop(0) if self.steps else {"role": "assistant", "content": "## Headline\nx"}

        events = list(_run_agentic_events("is K-101 the same as P-101?", self.entities, self.kg, Scripted(script)))
        gates = [e for e in events if e["type"] == "gate"]
        final = next(e for e in events if e["type"] == "final")["answer"]
        return len(gates), final

    def _saw_related(self):
        return {"role": "assistant", "tool_calls": [self._call("get_related_assets", {"unified_id": "ASSET-001"})]}

    def test_claim_about_unexamined_neighbour_is_rejected_then_retried(self) -> None:
        gates, final = self._run([self._saw_related(),
                                  {"role": "assistant", "content": self.BAD},
                                  {"role": "assistant", "content": self.GOOD}])
        self.assertEqual(gates, 1)
        self.assertIn("cooling", (final.headline or "").lower())  # the corrected answer won

    def test_no_gate_when_the_neighbour_was_examined(self) -> None:
        script = [
            {"role": "assistant", "tool_calls": [
                self._call("get_related_assets", {"unified_id": "ASSET-001"}),
                self._call("get_asset_context", {"unified_id": "ASSET-004"}),
            ]},
            {"role": "assistant", "content": self.GOOD},
        ]
        self.assertEqual(self._run(script)[0], 0)

    def test_no_gate_when_the_answer_ignores_the_neighbour(self) -> None:
        """An agent may judge a branch irrelevant and simply not discuss it --
        that must not be blocked, or the gate would fire on every answer."""
        gates, _ = self._run([self._saw_related(), {"role": "assistant", "content": self.NO_MENTION}])
        self.assertEqual(gates, 0)

    def test_gate_on_giving_up_while_a_lead_is_open(self) -> None:
        inconclusive = "## Headline\nInconclusive\n\n## Root Cause\nThe cause remains unclear."
        gates, _ = self._run([self._saw_related(),
                              {"role": "assistant", "content": inconclusive},
                              {"role": "assistant", "content": self.GOOD}])
        self.assertEqual(gates, 1)

    def test_gate_fires_at_most_once(self) -> None:
        """A stubborn model must not be able to burn the turn budget here."""
        gates, final = self._run([self._saw_related(),
                                  {"role": "assistant", "content": self.BAD},
                                  {"role": "assistant", "content": self.BAD}])
        self.assertEqual(gates, 1)
        self.assertIsNotNone(final.headline)  # still returns an answer

    def test_no_gate_without_any_discovered_neighbour(self) -> None:
        script = [
            {"role": "assistant", "tool_calls": [self._call("get_asset_context", {"unified_id": "ASSET-001"})]},
            {"role": "assistant", "content": self.BAD},
        ]
        self.assertEqual(self._run(script)[0], 0)

    def test_mention_terms_include_the_domain_code_not_just_canonical_name(self) -> None:
        """CV-400 is neither ASSET-004's canonical name ("Boiler Feed Flow
        Control Valve") nor any of its local ids, so matching on those alone
        would miss the exact answer this gate exists to catch."""
        terms = _asset_mention_terms(self.entities, "ASSET-004")
        self.assertIn("cv-400", terms)
        self.assertIn("boiler feed flow control valve", terms)
        # ...and generic words must NOT be terms, or the gate would fire always.
        self.assertNotIn("valve", terms)
        self.assertNotIn("control valve", terms)


class TestHistoryMessages(unittest.TestCase):
    """`_history_messages` turns client-supplied {question, answer} turns into
    chat messages for the agentic loop -- it must survive hostile/malformed
    input (it comes straight from a POST body) and enforce the size bounds."""

    def test_valid_history_becomes_alternating_messages(self) -> None:
        msgs = _history_messages(
            [
                {"question": "Why is P-101 vibrating?", "answer": "Setpoint hike + seal work."},
                {"question": "What about K-101?", "answer": "Lube-oil overheating."},
            ]
        )
        self.assertEqual([m["role"] for m in msgs], ["user", "assistant", "user", "assistant"])
        self.assertEqual(msgs[0]["content"], "Why is P-101 vibrating?")
        self.assertEqual(msgs[3]["content"], "Lube-oil overheating.")

    def test_malformed_input_degrades_to_empty(self) -> None:
        for bad in (None, "a string", 42, {"question": "q"}, [None, 42, "x", {}, {"question": "q"}]):
            self.assertEqual(_history_messages(bad), [], f"expected [] for {bad!r}")

    def test_only_last_n_turns_kept(self) -> None:
        turns = [{"question": f"q{i}", "answer": f"a{i}"} for i in range(10)]
        msgs = _history_messages(turns)
        self.assertEqual(len(msgs), _MAX_HISTORY_TURNS * 2)
        self.assertEqual(msgs[0]["content"], f"q{10 - _MAX_HISTORY_TURNS}")

    def test_long_content_truncated(self) -> None:
        msgs = _history_messages([{"question": "q" * 99_999, "answer": "a" * 99_999}])
        self.assertEqual(len(msgs[0]["content"]), _MAX_HISTORY_CHARS)
        self.assertEqual(len(msgs[1]["content"]), _MAX_HISTORY_CHARS)


if __name__ == "__main__":
    unittest.main()
