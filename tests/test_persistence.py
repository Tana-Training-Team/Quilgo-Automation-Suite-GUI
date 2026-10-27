"""
Tests that dashboard data survives leaving the page, refreshing, or a restart.

Run from the project root:
    python -m unittest discover -s tests -p "test_*.py" -v

Each `AppTest` below is a separate browser session (a refresh or a new tab
starts a fresh one), so anything that only lived in st.session_state is gone
in the next one. The store is pointed at a temporary folder.
"""

import contextlib
import io
import logging
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

import pandas as pd
from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
logging.getLogger("streamlit").setLevel(logging.ERROR)

from app import results_store as store
from app.task_runner import runner
from core.processing.candidate_evaluator import evaluate_and_triage_candidates

APP = str(ROOT / "streamlit_app.py")


def _evaluate():
    """Real evaluator output: one tech fellow pending review, one auto-qualified."""
    m = pd.DataFrame([
        {"id": 1, "match_pk": 11, "email": "pending@x", "full_name": "Pat Pending", "job_category": "tech"},
        {"id": 2, "match_pk": 22, "email": "ok@x", "full_name": "Ola Okay", "job_category": "tech"},
    ])
    q = pd.DataFrame([("pending@x", "SQL", 8), ("pending@x", "Excel", 9),
                      ("ok@x", "SQL", 8), ("ok@x", "Excel", 9)],
                     columns=["email", "test_name", "score"])
    integ = pd.DataFrame([{"email": "pending@x", "test_name": "SQL",
                           "Issue_Types": "Low trust score", "flag_auto_fail_switch": False}])
    with contextlib.redirect_stdout(io.StringIO()):
        res, _, _ = evaluate_and_triage_candidates(m, q, integ, lambda *a: ("skip", ""))
    return res, integ


def _session(page):
    at = AppTest.from_file(APP, default_timeout=30)
    at.query_params["page"] = page
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    return at


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self._orig = {k: getattr(store, k) for k in
                      ("STATE_DIR", "RESULTS_FILE", "PREV_RESULTS_FILE", "INTEGRITY_FILE", "DRAFTS_FILE")}
        store.STATE_DIR = d
        store.RESULTS_FILE = d / "results.json"
        store.PREV_RESULTS_FILE = d / "results_prev.json"
        store.INTEGRITY_FILE = d / "integrity.json"
        store.DRAFTS_FILE = d / "drafts.json"
        results, integ = _evaluate()
        store.save_new_run(results, integ, "Part 2")

    def tearDown(self):
        for k, v in self._orig.items():
            setattr(store, k, v)
        self.tmp.cleanup()

    def _pending_expander_text(self, at):
        return [t for t in at.text_area if t.key and t.key.startswith("pj_")]

    def test_results_show_up_in_a_new_session(self):
        at = _session("final_review")
        labels = " ".join(e.label for e in at.expander)
        self.assertIn("Pat Pending", labels)
        self.assertIn("PENDING", labels)

    def test_approval_survives_a_refresh(self):
        at = _session("final_review")
        just = self._pending_expander_text(at)[0]
        just.input("Flag reviewed, looks fine").run()
        approve = next(b for b in at.button if b.key and b.key.startswith("pa_"))
        approve.click().run()
        self.assertFalse(at.exception)

        # "Refresh": a brand-new session reading from disk.
        at2 = _session("final_review")
        pat = next(e for e in at2.expander if "Pat Pending" in e.label)
        self.assertIn("APPROVED", pat.label)
        saved = next(c for c in store.load()["candidates"] if c["email"] == "pending@x")
        dec = next(d for d in saved["manual_decisions"] if d["role"] == "Data Analyst")
        self.assertEqual((dec["decision"], dec["justification"]),
                         ("Approved", "Flag reviewed, looks fine"))

    def test_unsaved_justification_survives_leaving_the_page(self):
        at = _session("final_review")
        self._pending_expander_text(at)[0].input("half-written note")
        # Leave for the Control Panel before clicking Approve/Reject.
        next(b for b in at.button if b.key == "nav_control").click().run()

        at2 = _session("final_review")
        self.assertEqual(self._pending_expander_text(at2)[0].value, "half-written note")
        pat = next(e for e in at2.expander if "Pat Pending" in e.label)
        self.assertIn("unsaved draft", pat.label)

    def test_reevaluate_after_restart_keeps_integrity_flags(self):
        # Simulate a restart: processor's in-memory copy is empty, only disk remains.
        from core import processor
        processor._cached_data_for_api_push["integrity_df"] = None
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            import streamlit_app as app
        cand = next(c for c in store.load()["candidates"] if c["email"] == "pending@x")
        app._reevaluate(cand)
        self.assertEqual(cand["roles"]["Data Analyst"]["status"], "MANUAL REVIEW (Pending)")
        self.assertIn("Low trust score", cand["original_row"]["summary_note_md"])

    def test_click_on_old_results_is_not_applied_to_a_new_run(self):
        at = _session("final_review")                  # reviewer has run 1 open
        self._pending_expander_text(at)[0].input("approve on run 1 data")
        results, integ = _evaluate()
        store.save_new_run(results, integ, "Part 2")    # a new run finishes meanwhile
        next(b for b in at.button if b.key and b.key.startswith("pa_")).click().run()
        self.assertFalse(at.exception)

        cand = next(c for c in store.load()["candidates"] if c["email"] == "pending@x")
        dec = next(d for d in cand["manual_decisions"] if d["role"] == "Data Analyst")
        self.assertEqual(dec["decision"], "Pending", "the stale click must not approve new data")
        self.assertTrue(any("A new run replaced the results" in w.value for w in at.warning))

    def test_page_survives_a_refresh(self):
        at = _session("final_review")
        self.assertEqual(at.session_state["page"], "final_review")

    def test_running_task_is_visible_from_another_tab(self):
        release = threading.Event()
        runner.start("Part 2", lambda emit: (emit("working…"), release.wait(10))[1])
        try:
            at = _session("control")  # a different tab from whoever started it
            sidebar = " ".join(e.value for e in at.sidebar.info)
            self.assertIn("Part 2", sidebar)
            self.assertTrue(at.button(key="btn_p1").disabled)
            self.assertIsNone(runner.start("Part 1", lambda emit: True),
                              "a second task must not start while one is running")
        finally:
            release.set()
            for _ in range(50):
                if not runner.snapshot()["running"]:
                    break
                time.sleep(0.05)

    def test_new_run_asks_before_replacing_unpushed_edits(self):
        cand = store.load()["candidates"][0]
        store.update_candidate(cand)            # an edit that hasn't been pushed
        self.assertTrue(store.has_unpushed_edits())
        at = _session("control")
        # The buttons need downloaded data to be enabled, so set the request
        # the way a click on "Run Processor" would.
        at.session_state["confirm_replace"] = "Part 2"
        at.run()
        self.assertTrue(any("will replace the current Final Review results" in w.value
                            for w in at.warning))
        at.button(key="cr_no").click().run()
        self.assertIsNone(at.session_state["confirm_replace"])

        store.mark_pushed()
        self.assertFalse(store.has_unpushed_edits())


if __name__ == "__main__":
    unittest.main()
