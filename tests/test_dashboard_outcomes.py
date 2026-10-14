"""
Tests for the Final Review dashboard's rejection messages.

Run from the project root:
    python -m unittest discover -s tests -p "test_*.py" -v

Candidates are built by the real evaluator, then classified the way the
dashboard does it, so the labels track the actual evaluation rules.
"""

import contextlib
import copy
import io
import logging
import sys
import unittest
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
logging.getLogger("streamlit").setLevel(logging.ERROR)

from core.processing.candidate_evaluator import evaluate_and_triage_candidates

with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
    import streamlit_app as app  # bare mode: page code only runs under `streamlit run`


def _evaluate(fellows, scores, flags=()):
    """fellows: [(email, job_category)], scores: [(email, test, score)],
    flags: [(email, test, auto_fail_bool)] → {email: candidate}"""
    m = pd.DataFrame([{"email": e, "full_name": e, "job_category": c} for e, c in fellows])
    q = pd.DataFrame(scores, columns=["email", "test_name", "score"])
    integ = pd.DataFrame([{"email": e, "test_name": t, "Issue_Types": "flag",
                           "flag_auto_fail_switch": a} for e, t, a in flags])
    with contextlib.redirect_stdout(io.StringIO()):
        res, _, _ = evaluate_and_triage_candidates(m, q, integ, lambda *a: ("skip", ""))
    return {c["email"]: c for c in res}


class RejectionOutcomeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.c = _evaluate(
            fellows=[("none@x", "tech"), ("low@x", "tech"), ("tabs@x", "tech"),
                     ("pass@x", "tech"), ("pending@x", "tech")],
            scores=[("low@x", "SQL", 5), ("low@x", "Excel", 6),
                    ("tabs@x", "SQL", 8), ("tabs@x", "Excel", 9),
                    ("pass@x", "SQL", 8), ("pass@x", "Excel", 9),
                    ("pending@x", "SQL", 8), ("pending@x", "Excel", 9)],
            flags=[("tabs@x", "SQL", True), ("pending@x", "SQL", False)],
        )

    def test_no_submission_is_did_not_attempt(self):
        cand = self.c["none@x"]
        self.assertEqual(app._final_status(cand)[0], "REJECTED")
        self.assertEqual(app._rejection_outcome(cand)[0], "not_attempted")

    def test_low_scores_is_failed(self):
        cand = self.c["low@x"]
        self.assertEqual(app._final_status(cand)[0], "REJECTED")
        kind, reasons = app._rejection_outcome(cand)
        self.assertEqual(kind, "failed")
        self.assertTrue(all("tests scored" in why for _, why in reasons))

    def test_integrity_auto_fail_is_did_not_meet_requirements(self):
        kind, reasons = app._rejection_outcome(self.c["tabs@x"])
        self.assertEqual(kind, "not_met")
        self.assertIn(("Data Analyst", "auto-failed on integrity (excessive tab switches)"), reasons)

    def test_reviewer_rejecting_a_pass_is_did_not_meet_requirements(self):
        cand = copy.deepcopy(self.c["pass@x"])
        for d in cand["manual_decisions"]:
            if d["role"] == "Data Analyst":
                d["decision"], d["justification"] = "Rejected", "test"
        self.assertEqual(app._final_status(cand)[0], "REJECTED")
        kind, reasons = app._rejection_outcome(cand)
        self.assertEqual(kind, "not_met")
        self.assertIn(("Data Analyst", "rejected by reviewer"), reasons)

    def test_pending_is_not_rejected(self):
        self.assertEqual(app._final_status(self.c["pending@x"])[0], "PENDING")

    def test_saving_a_no_submission_candidate_keeps_its_push_data(self):
        cand = copy.deepcopy(self.c["none@x"])
        before = (cand["original_row"]["summary_note_md"],
                  cand["original_row"]["scores_to_update"],
                  cand["original_row"]["attempt_outcome"])
        app._reevaluate(cand)
        after = (cand["original_row"]["summary_note_md"],
                 cand["original_row"]["scores_to_update"],
                 cand["original_row"]["attempt_outcome"])
        self.assertEqual(before, after)
        self.assertIn("FAIL: No Submission", after[1])


if __name__ == "__main__":
    unittest.main()
