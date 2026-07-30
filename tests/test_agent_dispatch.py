"""Regression tests for the deterministic reasoning fallback (`_dispatch`).

Covers the 7 demo questions from docs/TEAM_HANDBOOK.md §7 (scenarios S1-S5 +
unification + cross-app join) AND the variant phrasings that previously
produced confidently wrong answers -- the handlers must derive conclusions
from graph evidence, never from a per-scenario script:

  - "Why are there so many alarms on P-101?" must NOT get S5's nuisance
    verdict (P-101's 3 alarms are real vibration excursions).
  - "Is P-102 experiencing the same problem as P-101?" must NOT get S4's
    hardcoded lube-oil/CV-400 narrative.
  - "Why is the compressor vibrating?" must find K-101's actual cause
    (upstream cooling problem overheating the lube oil), not "no clear cause".

Runs entirely offline (no LLM needed -- `_dispatch` is the no-LLM path).

Run with:
    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import unittest

from ontology_builder.agent import (
    _MAX_HISTORY_CHARS,
    _MAX_HISTORY_TURNS,
    _dispatch,
    _history_messages,
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
