"""
streamlit_app.py — Quilgo Automation Suite (Web UI)

Architecture:
  - st.session_state holds ALL persistent state (survives reruns).
  - Background threads receive the queue/event as plain Python objects
    passed at launch — they never touch st.session_state.
  - The UI polls every 1 s via st.rerun() while a task is running.
"""

import streamlit as st
import configparser
import subprocess
import threading
import sys
import os
import html as _html
import logging
import json
import queue
import time
import copy
import datetime
import traceback
import pandas as pd
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from app import config
from core import processor
from core.processing.quilgo_parser import (
    MASTER_TEST_CONFIG,
    ROLE_TO_TEST_MAPPING,
    ROLE_TO_CATEGORY_MAPPING,
    SLUG_MAPPING,
    ROLE_TO_DROPDOWN_OPTION_MAP,
    INTERNAL_TO_QUILGO_SIDEBAR_NAME,
)
from core.processing.candidate_evaluator import _generate_summary_notes
from core.processing.file_helpers import prepare_fresh_master, upsert_master_into_backup, write_manifest
from core.processing.api_pusher import DEFAULT_TEST_EMAILS

# ── Page config (must be first Streamlit call) ─────────────────────────────────
st.set_page_config(
    page_title="Quilgo Automation Suite",
    page_icon="🎯",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ══════════════════════════════════════════════════════════════════════════════
# Session state initialisation
# All mutable state lives here — persists across reruns for this browser tab.
# ══════════════════════════════════════════════════════════════════════════════
def _init():
    if "ready" in st.session_state:
        return
    st.session_state.ready          = True
    st.session_state.page           = "settings"
    st.session_state.log_lines      = []
    st.session_state.log_queue      = queue.Queue()   # thread → UI
    st.session_state.task_running   = False
    st.session_state.task_name      = ""
    st.session_state.task_start     = 0.0
    st.session_state.final_results  = []
    st.session_state.confirm_push   = False
    # manual review handshake objects
    st.session_state.review_event   = threading.Event()
    st.session_state.review_pending = None   # dict | None
    st.session_state.review_answer  = None   # tuple | None
    # filters
    st.session_state.start_date     = None
    st.session_state.end_date       = None
    # path to the current run's log file on disk (None when no run has started yet)
    st.session_state.current_log_file = None

_init()

# ══════════════════════════════════════════════════════════════════════════════
# Credentials
# ══════════════════════════════════════════════════════════════════════════════
def _load_creds():
    p = configparser.ConfigParser(interpolation=None)
    p.read(config.CONFIG_FILE)
    c = p["credentials"] if p.has_section("credentials") else {}
    return c.get("quilgo_email",""), c.get("quilgo_password",""), c.get("manatal_api_key","")

def _save_creds(email, password, api_key):
    p = configparser.ConfigParser(interpolation=None)
    p.read(config.CONFIG_FILE)   # preserve other sections (e.g. push_settings)
    p["credentials"] = {"quilgo_email": email,
                        "quilgo_password": password,
                        "manatal_api_key": api_key}
    with open(config.CONFIG_FILE, "w") as f:
        p.write(f)

# ══════════════════════════════════════════════════════════════════════════════
# Push / Test-Mode settings (stored in [push_settings] of gui_config.ini)
# ══════════════════════════════════════════════════════════════════════════════
def _load_push_settings():
    """Return (test_mode: bool, emails: list[str]) from ini, falling back to defaults."""
    p = configparser.ConfigParser(interpolation=None)
    p.read(config.CONFIG_FILE)
    if not p.has_section("push_settings"):
        return True, sorted(DEFAULT_TEST_EMAILS)
    s = p["push_settings"]
    mode = s.getboolean("test_mode", True)
    raw = s.get("test_candidate_emails", "").strip()
    if raw:
        emails = [e.strip() for e in raw.split(",") if e.strip()]
    else:
        emails = sorted(DEFAULT_TEST_EMAILS)
    return mode, emails


def _save_push_settings(test_mode: bool, emails: list):
    p = configparser.ConfigParser(interpolation=None)
    p.read(config.CONFIG_FILE)   # preserve credentials and other sections
    p["push_settings"] = {
        "test_mode": str(test_mode).lower(),
        "test_candidate_emails": ",".join(e.strip() for e in emails if e.strip()),
    }
    with open(config.CONFIG_FILE, "w") as f:
        p.write(f)


# ══════════════════════════════════════════════════════════════════════════════
# File-based session logs
#   • Each task run gets its own log file under downloads/logs/
#   • Log lines are appended as they arrive (streaming from the start of the run)
#   • Files older than LOG_RETENTION_DAYS are pruned at launch
# ══════════════════════════════════════════════════════════════════════════════
LOGS_DIR = config.DOWNLOADS_DIR / "logs"
LOG_RETENTION_DAYS = 3

def _html_escape(s: str) -> str:
    """Escape for safe embedding inside a <div>…</div> block."""
    return _html.escape(s or "", quote=False)

def _prune_old_logs():
    """Delete session log files older than LOG_RETENTION_DAYS. Best-effort — never raises."""
    try:
        if not LOGS_DIR.exists():
            return
        cutoff = time.time() - LOG_RETENTION_DAYS * 86400
        for f in LOGS_DIR.glob("session_*.log"):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
            except Exception:
                pass
    except Exception:
        pass

def _new_log_file(task_name: str) -> Path | None:
    """Create a fresh log file for a run; returns its path (or None on failure)."""
    try:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        safe = "".join(c if c.isalnum() else "_" for c in (task_name or "task"))
        path = LOGS_DIR / f"session_{ts}_{safe}.log"
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"# Quilgo Automation Suite — session log\n")
            f.write(f"# Task: {task_name}\n")
            f.write(f"# Started: {datetime.datetime.now().isoformat(timespec='seconds')}\n")
            f.write("#" + "─" * 60 + "\n")
        return path
    except Exception:
        return None

def _append_log_file(line: str):
    """Append one line to the active session log file. Best-effort — never raises."""
    path = st.session_state.get("current_log_file")
    if not path:
        return
    try:
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {line}\n")
    except Exception:
        pass

# ══════════════════════════════════════════════════════════════════════════════
# stdout → queue writer  (used inside threads as sys.stdout replacement)
# ══════════════════════════════════════════════════════════════════════════════
class _QWriter:
    """
    Stream-like object that forwards every write to a queue line-by-line.
    Treats BOTH '\\n' and '\\r' as line terminators, so tqdm/progress bars
    (which use carriage returns to rewrite the same line) are visible in the
    Streamlit log in real time instead of being buffered forever.
    """
    def __init__(self, q):
        self.q, self.buf = q, ""
    def write(self, t):
        if not t:
            return
        # Normalise CR → LF so progress-bar updates appear as fresh lines.
        # (We intentionally keep each CR-segment as its own log line.)
        self.buf += t.replace("\r\n", "\n").replace("\r", "\n")
        while "\n" in self.buf:
            line, self.buf = self.buf.split("\n", 1)
            self.q.put(line)
    def flush(self):
        if self.buf:
            self.q.put(self.buf); self.buf = ""
    # Make isatty/fileno failures non-fatal for libs that probe the stream
    def isatty(self): return False

# ══════════════════════════════════════════════════════════════════════════════
# Background task functions
# All arguments are plain Python objects captured at thread-launch time.
# NEVER read st.session_state inside these functions.
# ══════════════════════════════════════════════════════════════════════════════

def _task_playwright(q, email, password, quizzes, auto_continue,
                     sd, ed, api_key, review_event, review_answer_box):
    q.put("━"*50)
    q.put("▶ Part 1 thread started")
    q.put("━"*50)
    env_file  = config.PROJECT_ROOT / ".env"
    log_file  = config.PROJECT_ROOT / "playwright_live.log"
    try:
        # Credentials check
        if not email or not password:
            q.put("❌ ERROR: Quilgo credentials not found. Go to Settings first.")
            return

        q.put(f"✔ Credentials found for: {email}")

        # Write .env
        with open(env_file, "w") as f:
            f.write(f'QUILGO_EMAIL="{email}"\nQUILGO_PASSWORD="{password}"\n')
        q.put("✔ .env file written")

        # Clear master so Playwright writes into a clean directory
        prepare_fresh_master(config.PROJECT_ROOT)
        q.put("✔ Prepared fresh master/ for this run.")

        # Write quiz selection — translate internal config keys to actual Quilgo sidebar names
        sidebar_names = [INTERNAL_TO_QUILGO_SIDEBAR_NAME.get(q, q) for q in quizzes]
        with open(config.SELECTED_QUIZZES_FILE, "w") as f:
            json.dump(sidebar_names, f)
        q.put(f"✔ Quiz filter: {'all quizzes' if not quizzes else str(len(quizzes))+' quiz(es)'}")

        # Run Playwright — redirect output to a log file AND read it line by line
        cmd = f"npx playwright test --browser chromium 2>&1 | tee {log_file}"
        q.put(f"▶ Running: npx playwright test --browser chromium")
        q.put(f"  Working dir: {config.PROJECT_ROOT}")

        # Force unbuffered output from child processes so log lines stream live
        # instead of arriving in big chunks. Merging stderr into stdout preserves
        # chronological order (which is how a real terminal shows them).
        child_env = os.environ.copy()
        child_env["PYTHONUNBUFFERED"] = "1"
        child_env["FORCE_COLOR"] = "0"      # strip ANSI colour escapes
        child_env["NO_COLOR"] = "1"
        child_env["CI"] = "1"               # Playwright prints plain progress under CI

        npx_cmd = "npx.cmd" if sys.platform == "win32" else "npx"
        proc = subprocess.Popen(
            [npx_cmd, "playwright", "test", "--browser", "chromium"],
            cwd=str(config.PROJECT_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,       # merge so order is preserved
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,                      # line-buffered
            env=child_env,
        )

        # Read char-by-char so both '\n' and '\r' terminate a line.
        # Playwright (and tqdm-style tools) use '\r' to rewrite a progress line;
        # readline() would block until the final '\n', hiding live progress.
        def _pipe(stream):
            buf = []
            try:
                while True:
                    ch = stream.read(1)
                    if ch == "":
                        break
                    if ch in ("\n", "\r"):
                        line = "".join(buf).rstrip()
                        buf.clear()
                        if line:
                            q.put(line)
                    else:
                        buf.append(ch)
                if buf:
                    tail = "".join(buf).rstrip()
                    if tail:
                        q.put(tail)
            finally:
                try: stream.close()
                except Exception: pass

        t_out = threading.Thread(target=_pipe, args=(proc.stdout,), daemon=True)
        t_out.start()
        t_out.join()
        proc.wait()

        q.put(f"[Playwright exit code: {proc.returncode}]")

        if proc.returncode == 0:
            new_rows = upsert_master_into_backup(config.PROJECT_ROOT)
            write_manifest(config.PROJECT_ROOT, stats_by_file=new_rows)
            q.put("✔✔✔ Part 1 COMPLETED successfully!")
            if auto_continue:
                q.put("━"*50)
                q.put("▶ Auto-continuing to Part 2…")
                q.put("━"*50)
                _task_processor_inner(q, False, sd, ed, api_key, review_event, review_answer_box)
        else:
            q.put("❌ Part 1 FAILED — see output above for details.")
    except FileNotFoundError as e:
        q.put(f"❌ Command not found: {e}")
        q.put("  Make sure Node.js and npx are installed in the container.")
        q.put("  Try: Go to Settings → Run System Setup")
    except Exception as e:
        q.put(f"❌ EXCEPTION in Part 1: {e}")
        q.put(traceback.format_exc())
    finally:
        try:
            if env_file.exists(): env_file.unlink()
            if config.SELECTED_QUIZZES_FILE.exists(): config.SELECTED_QUIZZES_FILE.unlink()
            if log_file.exists(): log_file.unlink()
        except Exception as ex:
            q.put(f"[cleanup error] {ex}")
        q.put("__TASK_RUNNING_FALSE__")


def _task_processor_inner(q, use_cache, sd, ed, api_key, review_event, review_answer_box):
    q.put("[thread] Part 2 processor started")
    # Capture EVERYTHING the processor emits — print(), sys.stderr.write(),
    # and the `logging` module — so the Task Log mirrors the terminal exactly.
    orig_out, orig_err = sys.stdout, sys.stderr
    writer = _QWriter(q)
    sys.stdout = writer
    sys.stderr = writer
    log_handler = logging.StreamHandler(writer)
    log_handler.setLevel(logging.DEBUG)
    log_handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    root_logger = logging.getLogger()
    prev_level = root_logger.level
    root_logger.addHandler(log_handler)
    if prev_level > logging.INFO or prev_level == logging.NOTSET:
        root_logger.setLevel(logging.INFO)
    try:
        if not api_key:
            q.put("ERROR: Manatal API Key not found. Go to Settings first."); return
        q.put("="*60); q.put("Starting Part 2: Data Processor…"); q.put("="*60)

        # ── Manual review handling ────────────────────────────────────────
        # All manual-review cases are now resolved on the Final Review
        # dashboard instead of via the mid-run overlay. This callback
        # unconditionally returns "skip", which the evaluator now treats as
        # "defer this role to the dashboard as Pending" (it used to DROP the
        # candidate entirely — that was a real bug).
        #
        # The original synchronous overlay code (commented below) is kept
        # for reference so we can flip back with a one-line change if
        # needed. `_render_review` in the UI is also left in place.
        def get_decision(candidate_data, role_name, review_num, total_reviews):
            q.put(f"  ⏳ Deferring '{role_name}' for {candidate_data.get('full_name','?')} "
                  f"to the Final Review dashboard.")
            return ("skip", "")

        # --- Legacy synchronous overlay (disabled) ---
        # def get_decision(candidate_data, role_name, review_num, total_reviews):
        #     review_answer_box["answer"] = None
        #     review_answer_box["pending"] = {
        #         "candidate_data": candidate_data,
        #         "role_name": role_name,
        #         "review_num": review_num,
        #         "total_reviews": total_reviews,
        #     }
        #     q.put("__REVIEW_NEEDED__")
        #     review_event.clear()
        #     review_event.wait()
        #     result = review_answer_box["answer"]
        #     review_answer_box["pending"] = None
        #     return result

        results, success = processor.run_or_rerun_processing(
            use_cache=use_cache, api_key=api_key,
            project_root=config.PROJECT_ROOT,
            get_manual_review_decision=get_decision,
            start_date=sd, end_date=ed,
        )
        if success:
            review_answer_box["final_results"] = results
            q.put("✔✔✔ Part 2 COMPLETED successfully!"); q.put("__SHOW_FINAL__")
        else:
            q.put("❌ Part 2 FAILED."); q.put("__DONE__")
    except Exception as e:
        q.put(f"ERROR in Part 2: {e}"); q.put(traceback.format_exc()); q.put("__DONE__")
    finally:
        try: writer.flush()
        except Exception: pass
        sys.stdout = orig_out
        sys.stderr = orig_err
        try:
            root_logger.removeHandler(log_handler)
            root_logger.setLevel(prev_level)
        except Exception:
            pass


def _task_processor(q, use_cache, sd, ed, api_key, review_event, review_answer_box):
    try:
        _task_processor_inner(q, use_cache, sd, ed, api_key, review_event, review_answer_box)
    except Exception as e:
        q.put(f"EXCEPTION in Part 2 wrapper: {e}")
        q.put(traceback.format_exc())
    finally:
        q.put("__TASK_RUNNING_FALSE__")


def _task_api_push(q, api_key, final_results_snapshot):
    """
    Push the final results to Manatal.

    `final_results_snapshot` is a plain-Python list of candidate dicts,
    captured at launch time from `st.session_state.final_results`. We rebuild
    the push cache from this snapshot BEFORE calling `trigger_api_push` so
    the API ships the dashboard-edited data, not the stale Part 2 output.
    (See `processor.refresh_push_cache_from_results` for details.)
    """
    orig_out, orig_err = sys.stdout, sys.stderr
    writer = _QWriter(q)
    sys.stdout = writer
    sys.stderr = writer
    log_handler = logging.StreamHandler(writer)
    log_handler.setLevel(logging.DEBUG)
    log_handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    root_logger = logging.getLogger()
    prev_level = root_logger.level
    root_logger.addHandler(log_handler)
    if prev_level > logging.INFO or prev_level == logging.NOTSET:
        root_logger.setLevel(logging.INFO)
    try:
        if not api_key:
            q.put("ERROR: Manatal API Key not found."); q.put("__DONE__"); return
        q.put("="*60); q.put("Starting API Push…"); q.put("="*60)

        # Sync dashboard edits into the push cache. If the snapshot is empty
        # (nothing edited, no final_results in session), the existing cache
        # from Part 2 is preserved — same behaviour as before.
        try:
            n = processor.refresh_push_cache_from_results(final_results_snapshot)
            if n:
                q.put(f"✔ Push cache synced with {n} dashboard-edited candidate(s).")
            else:
                q.put("ℹ No dashboard edits to sync — using Part 2 output as-is.")
        except Exception as sync_err:
            # Don't let a sync failure block the push — log and continue with
            # whatever is already in the cache. That preserves the previous
            # behaviour if something goes wrong with reassembly.
            q.put(f"⚠ Could not sync dashboard edits into push cache: {sync_err}")
            q.put(traceback.format_exc())

        ok = processor.trigger_api_push(api_key)
        q.put("✔✔✔ API Push COMPLETED." if ok else "❌ API Push FAILED.")
        q.put("__DONE__")
    except Exception as e:
        q.put(f"ERROR in API Push: {e}"); q.put("__DONE__")
    finally:
        try: writer.flush()
        except Exception: pass
        sys.stdout = orig_out
        sys.stderr = orig_err
        try:
            root_logger.removeHandler(log_handler)
            root_logger.setLevel(prev_level)
        except Exception:
            pass
        q.put("__TASK_RUNNING_FALSE__")


def _launch(target, args):
    """Mark task as running, clear log, start daemon thread."""
    st.session_state.task_running  = True
    st.session_state.task_start    = time.time()
    st.session_state.log_lines     = []
    # drain any stale items from a previous run
    while not st.session_state.log_queue.empty():
        try: st.session_state.log_queue.get_nowait()
        except: pass
    # Prune >3-day-old session logs, then open a fresh log file for this run
    _prune_old_logs()
    st.session_state.current_log_file = _new_log_file(st.session_state.get("task_name", "task"))
    threading.Thread(target=target, args=args, daemon=True).start()


# ══════════════════════════════════════════════════════════════════════════════
# Drain queue + handle control signals
# ══════════════════════════════════════════════════════════════════════════════
def _drain():
    """Pull everything from the queue into session_state. Returns True if page should change."""
    q = st.session_state.log_queue
    navigate = None
    while True:
        try:
            line = q.get_nowait()
        except queue.Empty:
            break
        if line == "__TASK_RUNNING_FALSE__":
            st.session_state.task_running = False
        elif line == "__SHOW_FINAL__":
            box = st.session_state.get("review_answer_box", {})
            st.session_state.final_results = box.get("final_results", [])
            navigate = "final_review"
        elif line == "__REVIEW_NEEDED__":
            box = st.session_state.get("review_answer_box", {})
            st.session_state.review_pending = box.get("pending")
        else:
            st.session_state.log_lines.append(line)
            _append_log_file(line)
    return navigate

# ══════════════════════════════════════════════════════════════════════════════
# Re-evaluate candidate after score edit
# ══════════════════════════════════════════════════════════════════════════════
def _reevaluate(candidate):
    integrity_df = processor.get_cached_integrity_df()

    # Roles that currently have a Pending manual decision MUST keep their
    # "MANUAL REVIEW (Pending)" status through re-evaluation. If we let the
    # score-based recompute below overwrite it, and then the decision loop
    # re-stamps it as "FAIL (Manually Pending)", the role is silently marked
    # as a hard fail — and `scores_to_update` would push that to Manatal
    # even though the reviewer never decided.
    pending_roles = {
        d["role"] for d in candidate.get("manual_decisions", [])
        if d.get("decision") == "Pending" and d.get("role") in candidate.get("roles", {})
    }

    for role in list(candidate["roles"].keys()):
        if role not in ROLE_TO_TEST_MAPPING: continue
        if role in pending_roles:
            # Preserve the pending state — reviewer hasn't decided yet.
            candidate["roles"][role]["status"] = "MANUAL REVIEW (Pending)"
            continue
        role_category = ROLE_TO_CATEGORY_MAPPING.get(role, 'tech')
        passing = sum(1 for t in candidate["roles"][role].get("tests", []) if t.get("score", 0) >= 7)
        # Tech roles: hard-fail on score threshold before integrity check
        if role_category == 'tech' and passing < 2:
            candidate["roles"][role]["status"] = "FAIL"
            continue
        # Integrity check — applies to both tech and non-tech
        is_auto_failed = False
        is_flagged = False
        if not integrity_df.empty:
            for t in candidate["roles"][role].get("tests", []):
                issues = integrity_df[
                    (integrity_df['email'] == candidate['email']) &
                    (integrity_df['test_name'] == t['name'])
                ]
                if not issues.empty:
                    if bool(issues.iloc[0].get('flag_auto_fail_switch', False)):
                        is_auto_failed = True
                        break
                    else:
                        is_flagged = True
        if is_auto_failed:
            candidate["roles"][role]["status"] = "FAIL"
        elif is_flagged:
            candidate["roles"][role]["status"] = "MANUAL REVIEW"
        else:
            candidate["roles"][role]["status"] = "QUALIFIED"

    for dec in candidate.get("manual_decisions",[]):
        rn, final = dec["role"], dec["decision"]
        if rn not in candidate["roles"]: continue
        if final == "Pending":
            # Skip — the role status is already "MANUAL REVIEW (Pending)",
            # and there is no such thing as "QUALIFIED (Manually Pending)".
            continue
        if final in ("Approved", "Rejected"):
            candidate["roles"][rn]["status"] = (
                f"QUALIFIED (Manually {final})" if final == "Approved"
                else f"FAIL (Manually {final})"
            )
    md, html = _generate_summary_notes(candidate, integrity_df, candidate.get("manual_decisions"))
    candidate["original_row"]["summary_note_md"]   = md
    candidate["original_row"]["summary_note_html"] = html
    qr = [r for r,d in candidate["roles"].items() if "QUALIFIED" in d.get("status","FAIL")]
    row = candidate["original_row"]
    sp = {slug: row.get(test) for test,slug in SLUG_MAPPING.items() if pd.notna(row.get(test))}
    sp["techtestspassed"] = ([ROLE_TO_DROPDOWN_OPTION_MAP.get(r,r) for r in qr]
                              if qr else ["FAIL: Did not meet minimum requirements"])
    candidate["original_row"]["scores_to_update"] = json.dumps(sp)
    # Sync the stage-transition flag with the outcome determined above.
    # Guard against overwriting 'not_attempted' on No Submission candidates.
    if not candidate["roles"].get("No Submission"):
        candidate["original_row"]["attempt_outcome"] = "passed" if qr else "attempted_failed"
    return candidate


def _apply_score_edits(candidate, score_inp):
    """
    Apply score-textbox edits from the dashboard to the candidate's tests.
    Shared by the bottom "💾 Save & Re-evaluate" button AND the per-Pending
    Approve/Reject buttons, so score edits are never lost when the reviewer
    tweaks a number and clicks a decision button instead of Save.

    Silently skips empty / "N/A" entries (those are "not taken" placeholders)
    and raises ValueError only when a non-empty, non-N/A value isn't numeric.
    """
    for uk, (role, tname, raw) in score_inp.items():
        if raw is None: continue
        s = str(raw).strip()
        if not s or s.lower() == "n/a": continue
        ns = float(s)  # may raise ValueError — caught by the caller
        for t in candidate["roles"][role]["tests"]:
            if t.get("name") == tname:
                t["score"] = ns
                break

# ══════════════════════════════════════════════════════════════════════════════
# PAGE: Settings
# ══════════════════════════════════════════════════════════════════════════════
def page_settings():
    st.title("⚙️ Settings & System Setup")
    email, password, api_key = _load_creds()

    with st.form("creds_form"):
        ne  = st.text_input("Quilgo Email",    value=email,    key="f_email")
        np_ = st.text_input("Quilgo Password", value=password, type="password", key="f_pass")
        nk  = st.text_input("Manatal API Key", value=api_key,  type="password", key="f_key")
        c1, c2 = st.columns([1,3])
        with c1: save = st.form_submit_button("💾 Save", type="primary")
        with c2: go   = st.form_submit_button("→ Go to Automation")

    if save:
        _save_creds(ne, np_, nk)
        st.success("✅ Credentials saved!")
    if go:
        _save_creds(ne, np_, nk)
        st.session_state.page = "control"; st.rerun()

    st.divider()
    st.subheader("🛡️ Test Mode Guard")
    st.caption(
        "When **Test Mode** is ON, only whitelisted emails are pushed to Manatal. "
        "All other candidates are skipped and logged. "
        "Turn it OFF only when you are ready to go live."
    )

    tm, te = _load_push_settings()

    # Toggle lives outside any form so its value drives conditional rendering.
    # Saving happens immediately on change so the push guard reflects the new
    # state without needing a separate "Save" click.
    def _on_test_mode_change():
        _save_push_settings(
            st.session_state["f_test_mode_toggle"],
            _load_push_settings()[1],
        )

    new_tm = st.toggle(
        "Test Mode active",
        value=tm,
        key="f_test_mode_toggle",
        on_change=_on_test_mode_change,
    )

    if new_tm:
        st.markdown(
            "**Test candidate emails** "
            "<span style='color:#6b7280;font-size:12px;'>(one per line)</span>",
            unsafe_allow_html=True,
        )
        st.caption(
            "Defaults (hardcoded in api_pusher.py): "
            + ", ".join(sorted(DEFAULT_TEST_EMAILS))
        )

        with st.form("push_emails_form"):
            email_text = st.text_area(
                "Emails",
                value="\n".join(te),
                height=130,
                key="f_test_emails",
                label_visibility="collapsed",
                placeholder="one email per line",
                help="Add, edit, or remove emails. The hardcoded defaults are shown above.",
            )
            pc1, pc2, _ = st.columns([1, 1, 3])
            with pc1:
                save_push = st.form_submit_button("💾 Save emails", type="primary")
            with pc2:
                reset_push = st.form_submit_button("↺ Reset to defaults")

        if save_push:
            parsed = [e.strip().lower() for e in email_text.splitlines() if e.strip()]
            _save_push_settings(True, parsed)
            st.success(f"✅ Saved — {len(parsed)} email(s) whitelisted.")

        if reset_push:
            _save_push_settings(True, sorted(DEFAULT_TEST_EMAILS))
            st.success("↺ Reset to defaults.")
            st.rerun()

    st.divider()
    st.subheader("System Setup")
    st.info("Inside Docker this is done automatically. Only needed for local runs.")
    if st.button("🔧 Run npm install + playwright install", key="s_setup"):
        with st.spinner("Running… this may take a few minutes."):
            r = subprocess.run("npm install && npx playwright install chromium --with-deps",
                               cwd=str(config.PROJECT_ROOT), shell=True,
                               capture_output=True, text=True)
        st.success("✅ Done!") if r.returncode == 0 else st.error("❌ Failed.")
        st.code(r.stdout + r.stderr)

# ══════════════════════════════════════════════════════════════════════════════
# PAGE: Control Panel
# ══════════════════════════════════════════════════════════════════════════════
def page_control():
    all_roles  = sorted(ROLE_TO_TEST_MAPPING.keys())
    all_quizzes = sorted(MASTER_TEST_CONFIG.keys())

    # Re-initialise when the available role/quiz list changes (config updates, new roles added).
    # This ensures roles like Non-Tech are never silently absent from the selector
    # due to a stale session that was started before the role existed.
    _roles_fp   = ",".join(all_roles)
    _quizzes_fp = ",".join(all_quizzes)
    if st.session_state.get("_roles_fp") != _roles_fp:
        st.session_state._roles_fp = _roles_fp
        st.session_state.w_roles   = all_roles[:]
    elif "w_roles" not in st.session_state:
        st.session_state.w_roles = all_roles[:]
    if st.session_state.get("_quizzes_fp") != _quizzes_fp:
        st.session_state._quizzes_fp = _quizzes_fp
        st.session_state.w_quizzes   = all_quizzes[:]
    elif "w_quizzes" not in st.session_state:
        st.session_state.w_quizzes = all_quizzes[:]

    # review_answer_box: plain dict passed to thread, written by thread, read by UI via drain
    if "review_answer_box" not in st.session_state:
        st.session_state.review_answer_box = {}

    # ── on_change: roles → quizzes sync ──────────────────────────────────────
    def _roles_changed():
        roles = st.session_state.w_roles
        derived = []
        for r in roles:
            for qz in ROLE_TO_TEST_MAPPING.get(r, []):
                if qz not in derived: derived.append(qz)
        st.session_state.w_quizzes = derived if derived else all_quizzes[:]

    # ── Sidebar ───────────────────────────────────────────────────────────────
    with st.sidebar:
        st.header("🔧 Filters")

        st.subheader("📅 Date Range")
        use_dates = st.checkbox("Filter by submission date", key="w_use_dates")
        if use_dates:
            sv = st.session_state.start_date if isinstance(st.session_state.start_date, datetime.date) else None
            ev = st.session_state.end_date   if isinstance(st.session_state.end_date,   datetime.date) else None
            st.session_state.start_date = st.date_input("From", value=sv, key="w_start")
            st.session_state.end_date   = st.date_input("To",   value=ev, key="w_end")
        else:
            st.session_state.start_date = None
            st.session_state.end_date   = None

        st.multiselect(
            "Roles",
            options=all_roles,
            format_func=lambda r: "Non-Tech" if r == "None-Tech" else r,
            key="w_roles",
            on_change=_roles_changed,
        )

        qd = {q: f"{'●' if MASTER_TEST_CONFIG[q]['type']=='Required' else '○'} {q}" for q in all_quizzes}
        st.multiselect("Quizzes  (● Required  ○ Optional)",
                       options=all_quizzes, format_func=lambda q: qd[q],
                       key="w_quizzes")

        active = st.session_state.w_quizzes
        if active and set(active) != set(all_quizzes):
            st.success(f"Filter: {len(active)} quiz(es)")
            if st.button("✕ Clear filter", key="w_clear"):
                st.session_state.w_roles   = all_roles[:]
                st.session_state.w_quizzes = all_quizzes[:]
                st.rerun()
        else:
            st.caption("No filter — all quizzes will be downloaded.")

        st.divider()
        # Test Mode status badge
        _tm, _te = _load_push_settings()
        if _tm:
            st.warning(
                f"🛡️ **Test Mode ON** — only {len(_te)} whitelisted email(s) "
                f"will be pushed.",
                icon=None,
            )
        else:
            st.success("🚀 **Test Mode OFF** — all candidates will be pushed.")

        st.divider()
        if st.button("⚙️ Settings", use_container_width=True, key="nav_s"):
            st.session_state.page = "settings"; st.rerun()
        if st.session_state.final_results:
            if st.button("📊 Final Review", use_container_width=True, key="nav_f"):
                st.session_state.page = "final_review"; st.rerun()

    # ── Manual review overlay ─────────────────────────────────────────────────
    if st.session_state.review_pending:
        _render_review(); return

    # Drain queue FIRST — before reading task_running so signals update state
    navigate = _drain()
    if navigate:
        st.session_state.page = navigate
        st.rerun()

    # ── Header ────────────────────────────────────────────────────────────────
    running = st.session_state.task_running
    elapsed = ""
    if running:
        s = int(time.time() - st.session_state.task_start)
        elapsed = f"  ⏱ {s//60:02d}:{s%60:02d}"
    st.title(f"🎯 Control Panel{elapsed}")

    # ── Status banner ─────────────────────────────────────────────────────────
    if running:
        st.info(f"⏳ **{st.session_state.task_name} is running…** Log updates every second.")
    elif st.session_state.log_lines:
        last = st.session_state.log_lines[-1]
        if "✔✔✔" in last:
            st.success(last)
        elif "❌" in last or "ERROR" in last or "EXCEPTION" in last:
            st.error(last)
        else:
            st.success("✅ Task finished.")

    auto_cont = st.checkbox("Run Part 2 automatically after successful Part 1",
                            value=True, key="w_auto")

    # ── Buttons ───────────────────────────────────────────────────────────────
    c1, c2, c3 = st.columns(3)

    quilgo_ready = config.QUILGO_RUNS_DIR.exists() and any(config.QUILGO_RUNS_DIR.iterdir())
    cache_ready  = config.MANATAL_CACHE_FILE.exists()

    with c1:
        if st.button("▶ Run Downloader (Part 1)",
                     disabled=running, type="primary",
                     use_container_width=True, key="btn_p1"):
            email, password, api_key = _load_creds()
            quizzes = st.session_state.w_quizzes
            if set(quizzes) == set(all_quizzes): quizzes = []
            sd = pd.Timestamp(st.session_state.start_date, tz="UTC") if st.session_state.start_date else None
            ed = pd.Timestamp(st.session_state.end_date,   tz="UTC") if st.session_state.end_date   else None
            st.session_state.task_name = "Part 1"
            _launch(_task_playwright, args=(
                st.session_state.log_queue, email, password, quizzes,
                auto_cont, sd, ed, api_key,
                st.session_state.review_event,
                st.session_state.review_answer_box,
            ))
            st.rerun()

    with c2:
        if st.button("▶ Run Processor (Part 2)",
                     disabled=(running or not quilgo_ready),
                     type="primary", use_container_width=True, key="btn_p2",
                     help="Run Part 1 first to download Quilgo data"):
            _, _, api_key = _load_creds()
            sd = pd.Timestamp(st.session_state.start_date, tz="UTC") if st.session_state.start_date else None
            ed = pd.Timestamp(st.session_state.end_date,   tz="UTC") if st.session_state.end_date   else None
            st.session_state.task_name = "Part 2"
            _launch(_task_processor, args=(
                st.session_state.log_queue, False, sd, ed, api_key,
                st.session_state.review_event,
                st.session_state.review_answer_box,
            ))
            st.rerun()

    with c3:
        if st.button("🔄 Re-run Processor (Cache)",
                     disabled=(running or not cache_ready),
                     use_container_width=True, key="btn_p2c",
                     help="Skips Manatal fetch, uses cached profiles"):
            _, _, api_key = _load_creds()
            sd = pd.Timestamp(st.session_state.start_date, tz="UTC") if st.session_state.start_date else None
            ed = pd.Timestamp(st.session_state.end_date,   tz="UTC") if st.session_state.end_date   else None
            st.session_state.task_name = "Part 2"
            _launch(_task_processor, args=(
                st.session_state.log_queue, True, sd, ed, api_key,
                st.session_state.review_event,
                st.session_state.review_answer_box,
            ))
            st.rerun()

    if st.session_state.final_results and not running:
        if st.button("📊 Go to Final QA Page →", key="btn_qa"):
            st.session_state.page = "final_review"; st.rerun()

    # ── Log ───────────────────────────────────────────────────────────────────
    lc, bc = st.columns([9,1])
    with lc: st.subheader("📋 Task Log")
    with bc:
        if st.button("🗑", key="btn_clear", disabled=running, help="Clear log"):
            st.session_state.log_lines = []; st.rerun()

    # Render the log as a scrollable code block that auto-scrolls to the newest
    # line every rerun — this is what gives the "live terminal" feel. We keep
    # the full in-memory buffer (nothing is truncated) but only render the tail
    # in the box so the DOM stays responsive on very long runs.
    log_lines = st.session_state.log_lines
    TAIL = 800   # lines visible in the scrollable box; full log is on disk
    visible = log_lines[-TAIL:] if len(log_lines) > TAIL else log_lines
    log_text = "\n".join(visible) if visible else "(waiting for output…)"
    # Use a monospaced code block inside a fixed-height scroll container so the
    # browser naturally keeps the newest line in view.
    st.markdown(
        f"""<div id="quilgo-log-box" style="
                height:420px; overflow-y:auto; padding:10px;
                background:#0e1117; color:#e6edf3;
                border:1px solid #30363d; border-radius:6px;
                font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
                font-size:12px; white-space:pre-wrap; word-break:break-word;">
{_html_escape(log_text)}
            </div>
            <script>
                const el = window.parent.document.getElementById('quilgo-log-box')
                          || document.getElementById('quilgo-log-box');
                if (el) {{ el.scrollTop = el.scrollHeight; }}
            </script>""",
        unsafe_allow_html=True,
    )
    if len(log_lines) > TAIL:
        st.caption(f"Showing last {TAIL} of {len(log_lines)} lines · full log in the download below")

    # Offer a download of the current run's full on-disk log (streamed from start).
    # Cache the bytes in session_state so the media ID stays stable across the
    # sub-second polling reruns, avoiding MediaFileStorageError noise in the logs.
    lf = st.session_state.get("current_log_file")
    if lf and Path(lf).exists():
        try:
            cache_key = f"_log_bytes_{lf}"
            if not running:
                # Task finished — read once and cache so the final log is stable.
                if cache_key not in st.session_state:
                    with open(lf, "rb") as _fh:
                        st.session_state[cache_key] = _fh.read()
                log_bytes = st.session_state[cache_key]
            else:
                # Task still running — read fresh each time but don't cache yet.
                with open(lf, "rb") as _fh:
                    log_bytes = _fh.read()
            st.download_button(
                "⬇ Download full run log",
                data=log_bytes,
                file_name=Path(lf).name,
                mime="text/plain",
                key="btn_dl_log",
                help=f"Full log file for this run (auto-deleted after {LOG_RETENTION_DAYS} days)",
            )
            st.caption(f"📄 Log file: `{Path(lf).name}` · kept for {LOG_RETENTION_DAYS} days")
        except Exception:
            pass

    # ── Folder info ───────────────────────────────────────────────────────────
    fc1, fc2, _ = st.columns([1,1,4])
    with fc1:
        if quilgo_ready and st.button("📂 Quilgo folder", key="btn_f1"):
            try:
                from core.processing.file_helpers import find_latest_run_folder
                f = find_latest_run_folder(config.PROJECT_ROOT)
                st.info(str(f))
            except Exception as e: st.error(str(e))
    with fc2:
        if cache_ready and st.button("📂 Backups folder", key="btn_f2"):
            st.info(str(config.DOWNLOADS_DIR))

    # ── Auto-refresh while running ────────────────────────────────────────────
    if st.session_state.task_running:
        time.sleep(0.4)   # tighter cadence → log feels like a live terminal
        st.rerun()
    elif not st.session_state.task_running and st.session_state.log_queue.qsize() > 0:
        # Task just finished but queue has remaining lines — drain once more
        st.rerun()


# ══════════════════════════════════════════════════════════════════════════════
# Manual review overlay
# ══════════════════════════════════════════════════════════════════════════════
def _render_review():
    pr = st.session_state.review_pending
    c  = pr["candidate_data"]

    st.warning(f"⚠️ Manual Review {pr['review_num']} of {pr['total_reviews']}")
    st.subheader(f"👤 {c.get('full_name','N/A')}")
    st.caption(f"📧 {c.get('email','N/A')}")

    role_data = c.get("roles",{}).get(pr["role_name"],{})
    reasons   = role_data.get("manual_review_reasons", [])
    st.markdown(f"**Role:** `{_rl(pr['role_name'])}`")
    if reasons:
        st.error("Integrity flags:\n" + "\n".join(f"- {r}" for r in reasons))

    with st.expander("Full breakdown", expanded=True):
        for rname, rdata in c.get("roles",{}).items():
            if not rdata.get("tests"): continue
            st.markdown(f"**{'🟡' if 'MANUAL' in rdata.get('status','') else ('🟢' if 'QUALIFIED' in rdata.get('status','') else '🔴')} {_rl(rname)}** — {rdata.get('status','')}")
            _reasons = rdata.get("manual_review_reasons", [])
            for t in rdata.get("tests", []):
                tname = t.get("name", "?")
                detail = next((r[len(tname) + 2:] for r in _reasons if r.startswith(tname + ": ")), "")
                detail_str = f" | ⚠ {detail}" if detail else ""
                st.markdown(f"&nbsp;&nbsp;• {tname}: **{t.get('score','N/A')}** | {t.get('status','')}{detail_str}", unsafe_allow_html=True)

    just = st.text_area("Justification (required for Approve/Reject)", key="rv_just", height=80)

    r1,r2,r3,r4 = st.columns(4)
    with r1:
        if st.button("✅ Approve", type="primary", key="rv_approve"):
            if not just.strip(): st.error("Enter a justification.")
            else:
                st.session_state.review_answer_box["answer"] = ("approve", just.strip())
                st.session_state.review_event.set()
                st.session_state.review_pending = None; st.rerun()
    with r2:
        if st.button("❌ Reject", key="rv_reject"):
            if not just.strip(): st.error("Enter a justification.")
            else:
                st.session_state.review_answer_box["answer"] = ("reject", just.strip())
                st.session_state.review_event.set()
                st.session_state.review_pending = None; st.rerun()
    with r3:
        if st.button("⏭ Skip candidate", key="rv_skip"):
            st.session_state.review_answer_box["answer"] = ("skip", "")
            st.session_state.review_event.set()
            st.session_state.review_pending = None; st.rerun()
    with r4:
        if st.button("⏩ Skip ALL reviews", key="rv_skip_all"):
            st.session_state.review_answer_box["answer"] = ("skip_all", "")
            st.session_state.review_event.set()
            st.session_state.review_pending = None; st.rerun()

    time.sleep(0.5); st.rerun()

# ══════════════════════════════════════════════════════════════════════════════
# PAGE: Final Review
# ══════════════════════════════════════════════════════════════════════════════
def _pending_decisions(cand):
    """List of indices into candidate['manual_decisions'] that are still Pending."""
    return [j for j, d in enumerate(cand.get("manual_decisions", []))
            if d.get("decision") == "Pending"]

def _has_pending(cand):
    return len(_pending_decisions(cand)) > 0

def _normalise_pending(cand):
    """
    Forward-compat shim for Concern 3.

    Old evaluator output used role status = "MANUAL REVIEW" with NO entry in
    `manual_decisions`. The new dashboard UI keys off `manual_decisions`
    entries with decision == "Pending". If we ever load stale data (e.g.
    from an on-disk snapshot written by an older build), those roles would
    stay unreviewed but invisible — and the push guard wouldn't block them,
    because it only counts `decision == "Pending"` entries.

    This normaliser synthesises a Pending record for any role whose status
    starts with "MANUAL REVIEW" and has no matching decision, so the push
    guard + dashboard UI both see it. Idempotent — safe to call repeatedly.
    """
    roles = cand.get("roles", {}) or {}
    decs = cand.get("manual_decisions", []) or []
    decided_roles = {d.get("role") for d in decs}
    added = 0
    for rname, rdata in roles.items():
        status = str(rdata.get("status", ""))
        if status.startswith("MANUAL REVIEW") and rname not in decided_roles:
            decs.append({"role": rname, "decision": "Pending", "justification": ""})
            # Standardise the role status so _reevaluate treats it correctly.
            rdata["status"] = "MANUAL REVIEW (Pending)"
            added += 1
    if added:
        cand["manual_decisions"] = decs
    return added

def _rl(role: str) -> str:
    """Return the user-facing display label for a role name."""
    return 'Non-Tech' if role == 'None-Tech' else role


def _final_status(cand):
    # A candidate with any Pending decision is neither APPROVED nor REJECTED
    # yet — they're PENDING and can't be pushed to Manatal until resolved.
    if _has_pending(cand):
        return "PENDING", []
    q = {r for r,d in cand["roles"].items() if "QUALIFIED" in d.get("status","FAIL")}
    a = {d["role"] for d in cand.get("manual_decisions",[]) if d["decision"]=="Approved"}
    all_q = sorted(q|a)
    return ("APPROVED" if all_q else "REJECTED"), all_q

# Scoring rule used on the dashboard. Keep in sync with candidate_evaluator.py:
#   A role qualifies when the candidate has >= MIN_PASSING_TESTS test scores
#   that are >= PASS_THRESHOLD.
PASS_THRESHOLD = 7
MIN_PASSING_TESTS = 2

def _score_is_empty(raw) -> bool:
    """True when the score should be shown as `—` instead of a number."""
    if raw is None: return True
    s = str(raw).strip().lower()
    return s in ("", "n/a", "nan", "none")

def _has_empty_score(cand) -> bool:
    for rd in cand.get("roles", {}).values():
        for t in rd.get("tests", []):
            if _score_is_empty(t.get("score")):
                return True
    return False

def _candidate_matches_filters(cand, status, qroles, f):
    """Return True if the candidate should be shown under the current filters."""
    if f["status"] == "Approved" and status != "APPROVED": return False
    if f["status"] == "Rejected" and status != "REJECTED": return False
    if f["status"] == "Pending review" and status != "PENDING": return False
    if f["status"] == "Has manual decision" and not cand.get("manual_decisions"): return False

    # Role filter — match against roles the candidate was EVALUATED for, not
    # only the ones they qualified for; otherwise rejected candidates vanish
    # when you filter by role.
    if f["roles"]:
        cand_roles = set(cand.get("roles", {}).keys())
        if not cand_roles.intersection(f["roles"]):
            return False

    q = f["search"].strip().lower()
    if q:
        hay = f"{cand.get('full_name','')} {cand.get('email','')}".lower()
        if q not in hay: return False

    if f["only_empty_scores"] and not _has_empty_score(cand):
        return False

    return True

def page_final_review():
    results = st.session_state.final_results or []
    # Forward-compat: normalise any MANUAL REVIEW roles without a matching
    # Pending entry (see _normalise_pending for the full rationale). Safe
    # no-op on already-normalised candidates.
    for _c in results:
        _normalise_pending(_c)
    # Pre-compute status for every candidate — drives summary strip, filters,
    # AND the push guard, so this has to happen before the action bar.
    status_cache = [_final_status(c) for c in results] if results else []
    pending_count = sum(1 for s, _ in status_cache if s == "PENDING")

    # ── Top action bar ────────────────────────────────────────────────────────
    cb, cp = st.columns([2, 1])
    with cb:
        if st.button("← Back to Control Panel", key="fr_back"):
            st.session_state.page = "control"; st.rerun()
    with cp:
        # Block push while ANY candidate has a Pending manual decision —
        # otherwise un-reviewed cases would silently ship to Manatal.
        if pending_count > 0:
            st.button(
                f"🚀 Push blocked — {pending_count} pending",
                key="fr_push_blocked", disabled=True,
                help="Resolve all Pending manual decisions before pushing.",
            )
        else:
            lbl = "⚠️ Confirm — click again!" if st.session_state.confirm_push else "🚀 Push All to Manatal"
            if st.button(lbl, type="primary", key="fr_push"):
                if st.session_state.confirm_push:
                    _, _, ak = _load_creds()
                    if "review_answer_box" not in st.session_state:
                        st.session_state.review_answer_box = {}
                    # Snapshot the current edited results as a plain list so
                    # the background thread sees dashboard edits, not stale
                    # Part 2 output. `copy.deepcopy` prevents the thread from
                    # mutating session state while the UI is still rendering.
                    snapshot = copy.deepcopy(st.session_state.final_results or [])
                    _launch(_task_api_push,
                            args=(st.session_state.log_queue, ak, snapshot))
                    st.session_state.confirm_push = False
                    st.session_state.page = "control"; st.rerun()
                else:
                    st.session_state.confirm_push = True; st.rerun()

    if st.session_state.confirm_push and pending_count == 0:
        st.warning("About to push to Manatal — click again to confirm.")

    if not results:
        st.info("No results yet. Run Part 2 first."); return

    if pending_count > 0:
        st.error(
            f"⏳ **{pending_count} candidate(s) have pending manual decisions.** "
            f"Approve or Reject each role below — the push to Manatal is blocked "
            f"until every Pending is resolved."
        )

    # ── Summary strip ─────────────────────────────────────────────────────────
    total    = len(results)
    approved = sum(1 for s, _ in status_cache if s == "APPROVED")
    rejected = sum(1 for s, _ in status_cache if s == "REJECTED")
    # pending_count computed earlier (needed by the push guard)
    manual   = sum(1 for c in results if c.get("manual_decisions"))
    empty_sc = sum(1 for c in results if _has_empty_score(c))

    m1, m2, m3, m4, m5, m6 = st.columns(6)
    m1.metric("Total",               total)
    m2.metric("✅ Approved",          approved)
    m3.metric("❌ Rejected",          rejected)
    m4.metric("⏳ Pending",           pending_count)
    m5.metric("⚖ Manual decisions",  manual)
    m6.metric("⬚ Missing scores",    empty_sc)


    # ── Filters ───────────────────────────────────────────────────────────────
    all_roles_seen = sorted({r for c in results for r in c.get("roles", {}).keys()})

    with st.container(border=True):
        fc1, fc2, fc3 = st.columns([1.2, 1.5, 2])
        with fc1:
            f_status = st.selectbox(
                "Status",
                ["All", "Pending review", "Approved", "Rejected", "Has manual decision"],
                key="fr_f_status",
            )
        with fc2:
            f_roles = st.multiselect(
                "Role(s)", options=all_roles_seen, key="fr_f_roles",
                help="Show only candidates evaluated for these roles.",
            )
        with fc3:
            f_search = st.text_input(
                "Search (name or email)", key="fr_f_search",
                placeholder="e.g. jane or @gmail.com",
            )

        gc1, gc2, gc3 = st.columns([1.2, 1.2, 1])
        with gc1:
            f_sort = st.selectbox(
                "Sort by",
                ["Pending first", "Name (A→Z)",
                 "Status (Rejected first)", "Status (Approved first)",
                 "Most missing scores", "Most manual decisions"],
                key="fr_f_sort",
            )
        with gc2:
            f_empty = st.checkbox(
                "Only candidates with missing scores", key="fr_f_empty",
                help="Useful for spotting candidates who didn't complete all required tests.",
            )
        with gc3:
            f_collapse_happy = st.checkbox(
                "Collapse approved", value=True, key="fr_f_collapse_happy",
                help="Render approved + no-manual-decision candidates as a one-line row.",
            )

    filters = {
        "status": f_status,
        "roles": set(f_roles),
        "search": f_search or "",
        "only_empty_scores": f_empty,
    }

    # Keep original indices so Save writes back to the right slot
    indexed = list(enumerate(results))
    filtered = [
        (i, c) for (i, c) in indexed
        if _candidate_matches_filters(c, status_cache[i][0], status_cache[i][1], filters)
    ]

    def _missing_count(c):
        return sum(1 for rd in c.get("roles", {}).values()
                   for t in rd.get("tests", []) if _score_is_empty(t.get("score")))

    if f_sort == "Pending first":
        # Pending candidates bubble to the top — these need action.
        filtered.sort(key=lambda ic: (status_cache[ic[0]][0] != "PENDING",
                                      (ic[1].get("full_name") or "").lower()))
    elif f_sort == "Name (A→Z)":
        filtered.sort(key=lambda ic: (ic[1].get("full_name") or "").lower())
    elif f_sort == "Status (Rejected first)":
        filtered.sort(key=lambda ic: (status_cache[ic[0]][0] != "REJECTED",
                                      (ic[1].get("full_name") or "").lower()))
    elif f_sort == "Status (Approved first)":
        filtered.sort(key=lambda ic: (status_cache[ic[0]][0] != "APPROVED",
                                      (ic[1].get("full_name") or "").lower()))
    elif f_sort == "Most missing scores":
        filtered.sort(key=lambda ic: (-_missing_count(ic[1]),
                                      (ic[1].get("full_name") or "").lower()))
    elif f_sort == "Most manual decisions":
        filtered.sort(key=lambda ic: (-len(ic[1].get("manual_decisions", [])),
                                      (ic[1].get("full_name") or "").lower()))

    st.caption(f"Showing **{len(filtered)}** of {total} candidates")

    if not filtered:
        st.info("No candidates match the current filters.")
        return

    # ── Render ────────────────────────────────────────────────────────────────
    for (i, cand) in filtered:
        status, qroles = status_cache[i]
        if status == "APPROVED":  icon = "🟢"
        elif status == "PENDING": icon = "⏳"
        else:                     icon = "🔴"
        has_manual  = bool(cand.get("manual_decisions"))
        has_empty   = _has_empty_score(cand)
        has_pending = _has_pending(cand)

        # "Happy path": approved, no manual decisions, no missing scores, no
        # pending reviews → render as a compact one-line row. Pending ALWAYS
        # gets a full expander so the reviewer can act on it.
        compact = (f_collapse_happy and status == "APPROVED"
                   and not has_manual and not has_empty and not has_pending)

        if compact:
            roles_line = ", ".join(_rl(r) for r in qroles) if qroles else "—"
            st.markdown(
                f"<div style='padding:8px 12px;border:1px solid #e5e7eb;"
                f"border-radius:6px;margin-bottom:6px;'>"
                f"{icon} <b>{_html_escape(cand.get('full_name','N/A'))}</b> "
                f"<span style='color:#6b7280;'>· {_html_escape(cand.get('email','N/A'))}</span> "
                f"<span style='color:#059669;'>· Qualified for: "
                f"{_html_escape(roles_line)}</span>"
                f"</div>",
                unsafe_allow_html=True,
            )
            continue

        flags = []
        if has_pending: flags.append("⏳ pending review")
        if has_manual and not has_pending: flags.append("⚖ manual")
        if has_empty:  flags.append("⬚ missing scores")
        flag_suffix = f"  ·  {' · '.join(flags)}" if flags else ""

        # Auto-expand the ones that need your eyes — Pending always expands
        expand_default = (status in ("REJECTED", "PENDING")
                          or has_manual or has_empty)

        header = (f"{icon} **{cand.get('full_name','N/A')}** — {status}  |  "
                  f"{cand.get('email','N/A')}")

        with st.expander(header + flag_suffix, expanded=expand_default):
            # The goal of this block is to mirror the information shown by the
            # Part 2 manual-review overlay (`_render_review`) one-to-one, so
            # reviewers see the SAME context here that they would have seen had
            # they gone through the overlay during the run.
            #
            # Overlay content (in order):
            #   1. Qualified-for / Did-not-meet summary
            #   2. "Integrity flags:" red box — filtered to reasons containing
            #      the word "integrity"
            #   3. "Full breakdown" expander (open by default) — per-role
            #      status icon + status, then per-test `name: score | status`
            # Nothing else.

            if qroles: st.success("Qualified for: " + ", ".join(_rl(r) for r in qroles))
            else:      st.error("Did not meet requirements for any role.")

            # (2) Integrity flags — same filter the overlay uses:
            #     only reasons whose text contains "integrity".
            integrity_reasons = []
            for rname, rdata in cand.get("roles", {}).items():
                for reason in rdata.get("manual_review_reasons", []):
                    integrity_reasons.append((rname, reason))
            if integrity_reasons:
                # st.error can't render a role→reason list cleanly, so we
                # hand-roll a red box that matches the overlay's styling.
                items = "".join(
                    f"<li><b>{_html_escape(_rl(rn))}</b> — {_html_escape(r)}</li>"
                    for rn, r in integrity_reasons
                )
                st.markdown(
                    f"<div style='background:#fee2e2;border-left:4px solid #dc2626;"
                    f"padding:10px 14px;border-radius:4px;margin:8px 0;'>"
                    f"<div style='font-weight:600;color:#991b1b;margin-bottom:4px;'>"
                    f"Integrity flags:</div>"
                    f"<ul style='margin:0;padding-left:20px;color:#7f1d1d;'>"
                    f"{items}</ul></div>",
                    unsafe_allow_html=True,
                )

            # (3) Full breakdown — exactly the overlay's layout:
            #     🟡/🟢/🔴 role — status
            #       • test_name: score | test_status
            role_items = [(rn, rd) for rn, rd in cand.get("roles", {}).items()
                                    if rd.get("tests")]
            if role_items:
                with st.expander("Full breakdown", expanded=True):
                    for rname, rdata in role_items:
                        rstatus = rdata.get("status", "")
                        if "MANUAL" in rstatus:       icon = "🟡"
                        elif "QUALIFIED" in rstatus:  icon = "🟢"
                        else:                         icon = "🔴"
                        st.markdown(
                            f"**{icon} {_html_escape(_rl(rname))}** — "
                            f"{_html_escape(rstatus)}"
                        )
                        _reasons = rdata.get("manual_review_reasons", [])
                        for t in rdata.get("tests", []):
                            tname = t.get("name", "?")
                            tscore = t.get("score", "N/A")
                            tstatus = t.get("status", "")
                            detail = next((r[len(tname) + 2:] for r in _reasons if r.startswith(tname + ": ")), "")
                            detail_str = f" | ⚠ {_html_escape(detail)}" if detail else ""
                            st.markdown(
                                f"&nbsp;&nbsp;• {_html_escape(tname)}: "
                                f"**{_html_escape(str(tscore))}** | "
                                f"{_html_escape(tstatus)}{detail_str}",
                                unsafe_allow_html=True,
                            )

            st.markdown("---")
            edited = copy.deepcopy(cand)

            st.markdown(
                f"**Scores** <span style='color:#6b7280;font-size:12px;'>"
                f"(pass ≥ {PASS_THRESHOLD})</span>",
                unsafe_allow_html=True,
            )

            all_tests = [(r, t) for r, rd in edited.get("roles", {}).items()
                                for t in rd.get("tests", [])]
            seen, score_inp = set(), {}
            cols = st.columns(3); ci = 0
            for role, test in all_tests:
                uk = f"{role}|||{test.get('name')}"
                if uk in seen: continue
                seen.add(uk)
                raw = test.get("score")
                is_empty = _score_is_empty(raw)

                with cols[ci % 3]:
                    label = f"{test.get('name')} ({role})"
                    if is_empty:
                        # Missing score — clearly visually distinct from a 0/low score
                        st.markdown(
                            f"<div style='font-size:12px;color:#6b7280;"
                            f"margin-bottom:-4px;'>{_html_escape(label)}</div>"
                            f"<div style='padding:4px 0;'>"
                            f"<span style='background:#e5e7eb;color:#4b5563;"
                            f"padding:2px 10px;border-radius:10px;font-size:12px;'>"
                            f"— not taken</span></div>",
                            unsafe_allow_html=True,
                        )
                        v = "N/A"  # preserved as-is by Save (skipped below)
                    else:
                        v = st.text_input(label, value=str(raw), key=f"sc_{i}_{uk}")
                        try:
                            nf = float(v)
                            if nf >= PASS_THRESHOLD:
                                st.markdown(
                                    f"<div style='margin-top:-8px;color:#059669;"
                                    f"font-size:11px;'>✓ passes (≥ {PASS_THRESHOLD})</div>",
                                    unsafe_allow_html=True)
                            else:
                                st.markdown(
                                    f"<div style='margin-top:-8px;color:#dc2626;"
                                    f"font-size:11px;'>✗ below {PASS_THRESHOLD}</div>",
                                    unsafe_allow_html=True)
                        except Exception:
                            pass
                score_inp[uk] = (role, test.get("name"), v)
                ci += 1

            # Manual decisions split into two groups:
            #   • Pending — shown overlay-style with Approve / Reject buttons
            #     and a required justification. Clicking a button saves
            #     immediately (just like the Part 2 overlay did).
            #   • Already resolved (Approved / Rejected) — shown with a
            #     selectbox so the reviewer can change their mind; these are
            #     persisted by the "💾 Save & Re-evaluate" button at the
            #     bottom, same as before.
            pending_decs = [(j, d) for j, d in enumerate(edited.get("manual_decisions", []))
                                   if d.get("decision") == "Pending"]
            resolved_decs = [(j, d) for j, d in enumerate(edited.get("manual_decisions", []))
                                    if d.get("decision") != "Pending"]

            if pending_decs:
                st.markdown("### ⏳ Pending manual decisions")
                st.caption("Each role below was flagged by the evaluator. "
                           "Approve or Reject, with a justification, to resolve.")

                for (j, dec) in pending_decs:
                    rn = dec.get("role", "N/A")

                    # Section header — makes it obvious which role this
                    # decision block applies to.
                    st.markdown(f"**Role:** `{_html_escape(_rl(rn))}`")

                    just_key = f"pj_{i}_{j}"
                    nj = st.text_area(
                        "Justification (required for Approve/Reject)",
                        value=dec.get("justification", ""),
                        height=80, key=just_key,
                    )

                    err_key = f"pe_{i}_{j}"

                    b1, b2, _sp = st.columns([1, 1, 3])
                    with b1:
                        if st.button("✅ Approve", type="primary",
                                     key=f"pa_{i}_{j}"):
                            if not (nj or "").strip():
                                st.session_state[err_key] = "Enter a justification."
                                st.rerun()
                            else:
                                try:
                                    # Bug 2 fix: apply any score edits the
                                    # reviewer made in the Scores block above
                                    # BEFORE resolving the decision. Otherwise
                                    # a typed-in score would be lost because
                                    # Approve/Reject bypasses the Save button.
                                    _apply_score_edits(edited, score_inp)
                                except ValueError:
                                    st.session_state[err_key] = "Scores must be numbers."
                                    st.rerun()
                                edited["manual_decisions"][j]["decision"] = "Approved"
                                edited["manual_decisions"][j]["justification"] = nj.strip()
                                if rn in edited.get("roles", {}):
                                    edited["roles"][rn]["status"] = (
                                        "QUALIFIED (Manually Approved)")
                                refreshed = _reevaluate(edited)
                                st.session_state.final_results[i] = refreshed
                                st.session_state.pop(err_key, None)
                                st.rerun()
                    with b2:
                        if st.button("❌ Reject", key=f"pr_{i}_{j}"):
                            if not (nj or "").strip():
                                st.session_state[err_key] = "Enter a justification."
                                st.rerun()
                            else:
                                try:
                                    _apply_score_edits(edited, score_inp)
                                except ValueError:
                                    st.session_state[err_key] = "Scores must be numbers."
                                    st.rerun()
                                edited["manual_decisions"][j]["decision"] = "Rejected"
                                edited["manual_decisions"][j]["justification"] = nj.strip()
                                if rn in edited.get("roles", {}):
                                    edited["roles"][rn]["status"] = (
                                        "FAIL (Manually Rejected)")
                                refreshed = _reevaluate(edited)
                                st.session_state.final_results[i] = refreshed
                                st.session_state.pop(err_key, None)
                                st.rerun()

                    err = st.session_state.get(err_key)
                    if err:
                        st.error(err)

                    st.markdown("---")

            if resolved_decs:
                if pending_decs:
                    st.markdown("### Previously-resolved decisions")
                for (j, dec) in resolved_decs:
                    rn = dec.get("role", "N/A")
                    d1, d2 = st.columns([1, 2])
                    with d1:
                        nd = st.selectbox(
                            f"Decision '{rn}'", ["Approved", "Rejected"],
                            index=0 if dec.get("decision") == "Approved" else 1,
                            key=f"dec_{i}_{j}",
                        )
                    with d2:
                        nj2 = st.text_area(
                            f"Justification '{rn}'",
                            value=dec.get("justification", ""),
                            height=80, key=f"just_{i}_{j}",
                        )
                    edited["manual_decisions"][j]["decision"]      = nd
                    edited["manual_decisions"][j]["justification"] = nj2

            if st.button("💾 Save & Re-evaluate", key=f"save_{i}"):
                try:
                    _apply_score_edits(edited, score_inp)
                    edited = _reevaluate(edited)
                    st.session_state.final_results[i] = edited
                    st.success("✅ Saved."); st.rerun()
                except ValueError:
                    st.error("Scores must be numbers.")
                except Exception as e:
                    st.error(str(e))

# ══════════════════════════════════════════════════════════════════════════════
# Router
# ══════════════════════════════════════════════════════════════════════════════
def main():
    with st.sidebar:
        if (config.ASSETS_DIR / "logo.png").exists():
            st.image(str(config.ASSETS_DIR / "logo.png"), width=120)
        st.title("Navigation")
        if st.button("⚙️ Settings",      use_container_width=True, key="nav_settings"):
            st.session_state.page = "settings"; st.rerun()
        if st.button("🎯 Control Panel", use_container_width=True, key="nav_control"):
            st.session_state.page = "control";  st.rerun()
        if st.session_state.final_results:
            if st.button("📊 Final Review", use_container_width=True, key="nav_final"):
                st.session_state.page = "final_review"; st.rerun()

    p = st.session_state.page
    if p == "settings":       page_settings()
    elif p == "control":      page_control()
    elif p == "final_review": page_final_review()
    else: st.session_state.page = "settings"; st.rerun()

if __name__ == "__main__":
    main()
