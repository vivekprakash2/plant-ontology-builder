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
