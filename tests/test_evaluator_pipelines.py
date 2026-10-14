"""
Tests for tech vs non-tech evaluation rules.

Run from the project root:
    python -m unittest discover -s tests -p "test_*.py" -v

Rules:
  - Tech: a candidate with no quiz submissions is "No Submission" (stays in stage).
  - Non-tech: quizzes are optional. No quizzes → passes (Passed Tests). Only an
    integrity flag can fail a non-tech candidate or send them to review.
"""

import contextlib
import io
import json
import sys
import unittest
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.processing.candidate_evaluator import evaluate_and_triage_candidates


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


def _summary(cand):
    row = cand["original_row"]
    return (
        {r: d["status"] for r, d in cand["roles"].items()},
        json.loads(row["scores_to_update"])["techtestspassed"],
        row["attempt_outcome"],
    )


class PipelineRuleTests(unittest.TestCase):
    def test_tech_with_no_quizzes_is_no_submission(self):
        c = _evaluate([("t@x", "tech")], [("other@x", "SQL", 8)])
        roles, passed, outcome = _summary(c["t@x"])
        self.assertEqual(roles, {"No Submission": "NO SUBMISSION"})
        self.assertEqual(passed, ["FAIL: No Submission"])
        self.assertEqual(outcome, "not_attempted")

    def test_non_tech_with_no_quizzes_passes(self):
        c = _evaluate([("n@x", "non-tech")], [("other@x", "SQL", 8)])
        roles, passed, outcome = _summary(c["n@x"])
        self.assertEqual(roles, {"None-Tech": "QUALIFIED"})
        self.assertEqual(passed, ["Non-Tech"])
        self.assertEqual(outcome, "passed")

    def test_non_tech_passes_even_when_no_non_tech_quiz_was_downloaded(self):
        # Only a tech-only quiz in this run (e.g. quiz filter) — used to leave the
        # non-tech candidate with no role and push them as attempted_failed.
        c = _evaluate([("n@x", "non-tech")], [("other@x", "Java", 8)])
        roles, passed, outcome = _summary(c["n@x"])
        self.assertEqual(roles, {"None-Tech": "QUALIFIED"})
        self.assertEqual(outcome, "passed")

    def test_non_tech_low_scores_still_pass(self):
        c = _evaluate([("n@x", "non-tech")], [("n@x", "SQL", 2), ("n@x", "Excel", 3)])
        self.assertEqual(_summary(c["n@x"])[2], "passed")

    def test_non_tech_tab_switch_auto_fail_still_fails(self):
        c = _evaluate([("n@x", "non-tech")], [("n@x", "SQL", 8)], flags=[("n@x", "SQL", True)])
        roles, passed, outcome = _summary(c["n@x"])
        self.assertEqual(roles, {"None-Tech": "FAIL"})
        self.assertEqual(outcome, "attempted_failed")

    def test_non_tech_integrity_flag_goes_to_review(self):
        c = _evaluate([("n@x", "non-tech")], [("n@x", "SQL", 8)], flags=[("n@x", "SQL", False)])
        self.assertEqual(_summary(c["n@x"])[0], {"None-Tech": "MANUAL REVIEW (Pending)"})


if __name__ == "__main__":
    unittest.main()
