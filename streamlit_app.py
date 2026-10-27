"""
streamlit_app.py — Quilgo Automation Suite (Web UI)

Architecture:
  - Results and reviewer decisions live on disk (app/results_store.py), not in
    st.session_state, so a refresh, a closed tab, a dropped connection or a
    container restart loses nothing. Every edit is saved immediately; text that
    is typed but not yet saved is kept as a draft.
  - Tasks (Part 1, Part 2, push) run in one background runner owned by the
    server process (app/task_runner.py). Every page and every tab sees the same
    status and log, and only one task can run at a time.
  - The live log refreshes inside a fragment, so only the log panel re-renders
    while a task runs — not the whole page.
  - st.session_state only holds per-tab view state (current page, filters).
"""

import streamlit as st
import configparser
import subprocess
import sys
import os
import html as _html
import logging
import json
import time
import copy
import datetime
import traceback
import pandas as pd
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from app import config
from app import results_store as store
from app.task_runner import runner, LOG_RETENTION_DAYS
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
# Per-tab view state
# Only things that are fine to lose on refresh live here (and even the current
# page survives a refresh through the URL). Everything that matters is on disk.
# ══════════════════════════════════════════════════════════════════════════════
PAGES = ("settings", "control", "final_review")

# Widgets whose values should survive switching pages. Streamlit deletes a
# widget's state when the widget isn't rendered (i.e. you're on another page);
# re-assigning the key to itself on every run keeps it.
_PERSISTENT_WIDGET_DEFAULTS = {
    "w_use_dates": False, "w_start": None, "w_end": None, "w_auto": True,
    "fr_f_status": "All", "fr_f_roles": [], "fr_f_search": "",
    "fr_f_sort": "Pending first", "fr_f_empty": False, "fr_f_collapse_happy": True,
}

def _init():
    for k, v in _PERSISTENT_WIDGET_DEFAULTS.items():
        if k not in st.session_state:
            st.session_state[k] = v
        else:
            st.session_state[k] = st.session_state[k]
    # Role/quiz selections: defaults are set by the Control Panel, just keep them.
    for k in ("w_roles", "w_quizzes"):
        if k in st.session_state:
            st.session_state[k] = st.session_state[k]
    if "ready" in st.session_state:
        return
    st.session_state.ready          = True
    st.session_state.page           = st.query_params.get("page", "control")
    if st.session_state.page not in PAGES:
        st.session_state.page = "control"
    st.session_state.final_results  = []
    st.session_state.results_run_id = None
    st.session_state.results_version = None
    st.session_state.stale_notice    = False  # "a new run replaced your results" until "Got it"
    st.session_state.awaiting_run_id = None   # run this tab started and will follow to Final Review

_init()


def _go(page):
    """Switch page and keep it in the URL, so a refresh lands on the same page."""
    st.session_state.page = page
    st.query_params["page"] = page
    st.rerun()


def _toast(msg, icon=None):
    """
    Small corner notice that fades on its own — for quick confirmations.
    Queued and shown on the next redraw, because most actions end with
    st.rerun(), which would otherwise wipe a toast before anyone sees it.
    """
    st.session_state.setdefault("_toasts", []).append((msg, icon))


def _show_toasts():
    # Short duration on purpose: Streamlit doesn't show a new toast while an
    # older one is still on screen, so a long-lived toast would swallow the
    # next action's confirmation. The page itself always shows the real state.
    for msg, icon in st.session_state.pop("_toasts", []):
        st.toast(msg, icon=icon, duration=2)


def _sync_results():
    """
    Load results from disk when the file has changed since this tab last
    read it — after Part 2 finishes, after a refresh, or after another tab
    saved an edit.
    """
    v = store.version()
    if v == st.session_state.results_version:
        return
    run = store.load()
    st.session_state.final_results  = run.get("candidates", [])
    st.session_state.results_run_id = run.get("run_id")
    st.session_state.results_version = v


def _save_candidate(i, cand):
    """Persist one edited candidate and update this tab's copy."""
    store.update_candidate(cand)
    st.session_state.final_results[i] = cand
    st.session_state.results_version = store.version()


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
    """Return (test_mode: bool, emails: list[str], freeze: bool) from ini, falling back to defaults."""
    p = configparser.ConfigParser(interpolation=None)
    p.read(config.CONFIG_FILE)
    if not p.has_section("push_settings"):
        return True, sorted(DEFAULT_TEST_EMAILS), False
    s = p["push_settings"]
    mode = s.getboolean("test_mode", True)
    raw = s.get("test_candidate_emails", "").strip()
    if raw:
        emails = [e.strip() for e in raw.split(",") if e.strip()]
    else:
        emails = sorted(DEFAULT_TEST_EMAILS)
    freeze = s.getboolean("freeze_stage_transitions", False)
    return mode, emails, freeze


def _save_push_settings(test_mode: bool, emails: list, freeze: bool = None):
    p = configparser.ConfigParser(interpolation=None)
    p.read(config.CONFIG_FILE)   # preserve credentials and other sections
    # If freeze not supplied, read current value so it is never accidentally overwritten.
    if freeze is None:
        if p.has_section("push_settings"):
            freeze = p["push_settings"].getboolean("freeze_stage_transitions", False)
        else:
            freeze = False
    p["push_settings"] = {
        "test_mode": str(test_mode).lower(),
        "test_candidate_emails": ",".join(e.strip() for e in emails if e.strip()),
        "freeze_stage_transitions": str(freeze).lower(),
    }
    with open(config.CONFIG_FILE, "w") as f:
        p.write(f)


# ══════════════════════════════════════════════════════════════════════════════
# HTML helpers
# ══════════════════════════════════════════════════════════════════════════════
def _html_escape(s: str) -> str:
    """Escape for safe embedding inside a <div>…</div> block."""
    return _html.escape(s or "", quote=False)

def _copyable_email_html(email: str) -> str:
    """
    Render an email as clickable text that copies itself to the clipboard,
    with a brief '✓ copied' confirmation. Falls back to plain escaped text
    for missing/placeholder values (nothing useful to copy).
    """
    email = email or "N/A"
    if email in ("N/A", ""):
        return _html_escape(email)
    # json.dumps gives a JS-safe double-quoted string; escape any stray single
    # quote too since the HTML attribute itself is single-quoted below.
    js_literal = json.dumps(email).replace("'", "\\u0027")
    label = _html_escape(email)
    return (
        "<span style='display:inline-flex;align-items:center;gap:6px;'>"
        f"<span onclick='navigator.clipboard.writeText({js_literal}).then(()=>{{"
        "const c=this.nextElementSibling; c.style.opacity=1; clearTimeout(c._t); "
        "c._t=setTimeout(()=>c.style.opacity=0,1200);})' "
        "style='cursor:pointer;text-decoration:underline dotted;' "
        f"title='Click to copy'>{label}</span>"
        "<span style='opacity:0;transition:opacity .3s;color:#059669;font-size:11px;'>✓ copied</span>"
        "</span>"
    )

# ══════════════════════════════════════════════════════════════════════════════
# stdout → task log (used inside the worker thread)
# ══════════════════════════════════════════════════════════════════════════════
class _LogWriter:
    """
    Stream-like object that forwards every write to the task log line-by-line.
    Treats BOTH '\\n' and '\\r' as line terminators, so tqdm/progress bars
    (which use carriage returns to rewrite the same line) show up live.
    """
    def __init__(self, emit):
        self.emit, self.buf = emit, ""
    def write(self, t):
        if not t:
            return
        self.buf += t.replace("\r\n", "\n").replace("\r", "\n")
        while "\n" in self.buf:
            line, self.buf = self.buf.split("\n", 1)
            self.emit(line)
    def flush(self):
        if self.buf:
            self.emit(self.buf); self.buf = ""
    def isatty(self): return False


class _capture_output:
    """Send print(), stderr and the logging module to the task log for the duration."""
    def __init__(self, emit):
        self.writer = _LogWriter(emit)
    def __enter__(self):
        self.orig = (sys.stdout, sys.stderr)
        sys.stdout = sys.stderr = self.writer
        self.handler = logging.StreamHandler(self.writer)
        self.handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        root = logging.getLogger()
        self.prev_level = root.level
        root.addHandler(self.handler)
        if self.prev_level > logging.INFO or self.prev_level == logging.NOTSET:
            root.setLevel(logging.INFO)
        return self
    def __exit__(self, *exc):
        try: self.writer.flush()
        except Exception: pass
        sys.stdout, sys.stderr = self.orig
        root = logging.getLogger()
        root.removeHandler(self.handler)
        root.setLevel(self.prev_level)
        return False

# ══════════════════════════════════════════════════════════════════════════════
# Background tasks
# Run by app.task_runner in a server-owned thread. Each receives `emit` (append
# a line to the shared log) and plain arguments captured at launch, returns
# True on success, and NEVER touches st.session_state — the tab that started
# it may be long gone by the time it finishes.
# ══════════════════════════════════════════════════════════════════════════════

def _task_playwright(emit, email, password, quizzes, auto_continue, sd, ed, api_key):
    emit("━"*50); emit("▶ Part 1 started"); emit("━"*50)
    env_file = config.PROJECT_ROOT / ".env"
    try:
        if not email or not password:
            emit("❌ ERROR: Quilgo credentials not found. Go to Settings first.")
            return False
        emit(f"✔ Credentials found for: {email}")

        with open(env_file, "w") as f:
            f.write(f'QUILGO_EMAIL="{email}"\nQUILGO_PASSWORD="{password}"\n')
        emit("✔ .env file written")

        # Clear master so Playwright writes into a clean directory
        prepare_fresh_master(config.PROJECT_ROOT)
        emit("✔ Prepared fresh master/ for this run.")

        # Write quiz selection — translate internal config keys to actual Quilgo sidebar names
        sidebar_names = [INTERNAL_TO_QUILGO_SIDEBAR_NAME.get(qz, qz) for qz in quizzes]
        with open(config.SELECTED_QUIZZES_FILE, "w") as f:
            json.dump(sidebar_names, f)
        emit(f"✔ Quiz filter: {'all quizzes' if not quizzes else str(len(quizzes))+' quiz(es)'}")

        emit("▶ Running: npx playwright test --browser chromium")
        emit(f"  Working dir: {config.PROJECT_ROOT}")

        # Unbuffered, uncoloured output so log lines stream live and readable.
        child_env = os.environ.copy()
        child_env.update(PYTHONUNBUFFERED="1", FORCE_COLOR="0", NO_COLOR="1", CI="1")
        npx_cmd = "npx.cmd" if sys.platform == "win32" else "npx"
        proc = subprocess.Popen(
            [npx_cmd, "playwright", "test", "--browser", "chromium"],
            cwd=str(config.PROJECT_ROOT),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,   # merged keeps order
            text=True, encoding="utf-8", errors="replace", bufsize=1, env=child_env,
        )
        # Read char-by-char so both '\n' and '\r' end a line — Playwright
        # rewrites progress lines with '\r', which readline() would hide.
        buf = []
        while True:
            ch = proc.stdout.read(1)
            if ch == "":
                break
            if ch in ("\n", "\r"):
                line = "".join(buf).rstrip(); buf.clear()
                if line: emit(line)
            else:
                buf.append(ch)
        if buf and "".join(buf).strip():
            emit("".join(buf).rstrip())
        proc.wait()
        emit(f"[Playwright exit code: {proc.returncode}]")

        if proc.returncode != 0:
            emit("❌ Part 1 FAILED — see output above for details.")
            return False

        with _capture_output(emit):
            new_rows = upsert_master_into_backup(config.PROJECT_ROOT)
            write_manifest(config.PROJECT_ROOT, stats_by_file=new_rows)
        emit("✔✔✔ Part 1 COMPLETED successfully!")
        if not auto_continue:
            return True
        emit("━"*50); emit("▶ Auto-continuing to Part 2…"); emit("━"*50)
        return _task_processor(emit, False, sd, ed, api_key)
    except FileNotFoundError as e:
        emit(f"❌ Command not found: {e}")
        emit("  Make sure Node.js and npx are installed in the container.")
        emit("  Try: Go to Settings → Run System Setup")
        return False
    finally:
        try:
            if env_file.exists(): env_file.unlink()
            if config.SELECTED_QUIZZES_FILE.exists(): config.SELECTED_QUIZZES_FILE.unlink()
        except Exception as ex:
            emit(f"[cleanup error] {ex}")


def _task_processor(emit, use_cache, sd, ed, api_key):
    if not api_key:
        emit("❌ ERROR: Manatal API Key not found. Go to Settings first.")
        return False
    emit("="*60); emit("Starting Part 2: Data Processor…"); emit("="*60)

    # Every manual-review case is resolved on the Final Review dashboard, so
    # the evaluator defers each one as "Pending".
    def get_decision(candidate_data, role_name, review_num, total_reviews):
        emit(f"  ⏳ Deferring '{role_name}' for {candidate_data.get('full_name','?')} "
             f"to the Final Review dashboard.")
        return ("skip", "")

    with _capture_output(emit):
        results, success = processor.run_or_rerun_processing(
            use_cache=use_cache, api_key=api_key,
            project_root=config.PROJECT_ROOT,
            get_manual_review_decision=get_decision,
            start_date=sd, end_date=ed,
        )
    if not success:
        emit("❌ Part 2 FAILED.")
        return False
    # Saved by the task itself, so the results survive even if nobody has the
    # page open when Part 2 finishes.
    store.save_new_run(results, processor.get_cached_integrity_df(), "Part 2")
    emit(f"✔ {len(results)} candidate(s) saved for Final Review.")
    emit("✔✔✔ Part 2 COMPLETED successfully!")
    return True


def _task_api_push(emit, api_key, final_results_snapshot):
    """
    Push the final results to Manatal. `final_results_snapshot` is the
    dashboard's (edited) candidate list captured at launch; the push cache is
    rebuilt from it so Manatal gets the edited data, not the raw Part 2 output.
    """
    if not api_key:
        emit("❌ ERROR: Manatal API Key not found.")
        return False
    emit("="*60); emit("Starting API Push…"); emit("="*60)
    with _capture_output(emit):
        try:
            n = processor.refresh_push_cache_from_results(final_results_snapshot)
            print(f"✔ Push cache synced with {n} dashboard candidate(s)." if n
                  else "ℹ No dashboard results to sync — using Part 2 output as-is.")
        except Exception as sync_err:
            # Don't block the push — fall back to whatever is already cached.
            print(f"⚠ Could not sync dashboard edits into push cache: {sync_err}")
            traceback.print_exc()
        ok = processor.trigger_api_push(api_key)
    if ok:
        store.mark_pushed()
        emit("✔✔✔ API Push COMPLETED.")
    else:
        emit("❌ API Push FAILED.")
    return ok


def _start_task(name, target, *args, follow_to_final=False):
    """Start a task on the shared runner; tell the user if one is already running."""
    run_id = runner.start(name, target, *args)
    if run_id is None:
        _toast(f"{runner.snapshot()['name']} is already running — wait for it to finish.", "⚠️")
    else:
        st.session_state.awaiting_run_id = run_id if follow_to_final else None
        _toast(f"{name} started — you can leave this page, it keeps running.", "▶️")
    return run_id

# ══════════════════════════════════════════════════════════════════════════════
# Re-evaluate candidate after score edit
# ══════════════════════════════════════════════════════════════════════════════
def _reevaluate(candidate):
    # No Submission candidates have nothing to score. Leave them exactly as the
    # evaluator produced them, otherwise re-evaluating would replace the
    # "No Submission" note and push payload with a generic failed evaluation.
    if candidate.get("roles", {}).get("No Submission"):
        return candidate

    # From disk, not processor memory: after a restart the in-memory copy is
    # empty, and re-evaluating without the flags would turn flagged roles into
    # QUALIFIED.
    integrity_df = store.load_integrity_df()

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

    # Refresh 'automatic_outcome' on every non-pending, auto-decided role to track
    # the status just recomputed above — but never touch the reviewer's own
    # 'decision'/'justification'. A role with no entry yet (shouldn't normally
    # happen, but defensive for older cached data) gets a fresh agreeing default,
    # the same way the evaluator seeds it on the first pass.
    decisions_by_role = {d["role"]: d for d in candidate.get("manual_decisions", [])}
    for role, role_data in candidate["roles"].items():
        if role == "No Submission" or role in pending_roles:
            continue
        status = role_data.get("status", "FAIL")
        if "MANUAL REVIEW" in status:
            continue  # unresolved manual-review role — no automatic outcome to track
        auto_outcome = "Qualified" if "QUALIFIED" in status else "Not Qualified"
        entry = decisions_by_role.get(role)
        if entry is None:
            entry = {
                "role": role, "automatic_outcome": auto_outcome,
                "decision": "Approved" if auto_outcome == "Qualified" else "Rejected",
                "justification": "",
            }
            candidate.setdefault("manual_decisions", []).append(entry)
            decisions_by_role[role] = entry
        elif entry.get("automatic_outcome") is not None:
            # Only auto-decided roles track a moving automatic_outcome — a
            # genuinely manual-reviewed role (automatic_outcome=None) stays None.
            entry["automatic_outcome"] = auto_outcome

    # Re-stamp the role status with "(Manually Approved/Rejected)" ONLY when the
    # reviewer's decision is notable — either a genuine manual-review resolution
    # (automatic_outcome is None) or an override of an auto-decided role. A role
    # where the reviewer simply agrees with the automatic outcome keeps its plain
    # QUALIFIED/FAIL status, with no decision to record.
    for dec in candidate.get("manual_decisions",[]):
        rn, final = dec["role"], dec.get("decision")
        if rn not in candidate["roles"]: continue
        if final == "Pending":
            # Skip — the role status is already "MANUAL REVIEW (Pending)",
            # and there is no such thing as "QUALIFIED (Manually Pending)".
            continue
        auto = dec.get("automatic_outcome")
        agrees = (
            (auto == "Qualified" and final == "Approved") or
            (auto == "Not Qualified" and final == "Rejected")
        )
        if agrees:
            continue
        if final in ("Approved", "Rejected"):
            candidate["roles"][rn]["status"] = (
                "QUALIFIED (Manually Approved)" if final == "Approved"
                else "FAIL (Manually Rejected)"
            )
    md, html = _generate_summary_notes(candidate, integrity_df, candidate.get("manual_decisions"))
    candidate["original_row"]["summary_note_md"]   = md
    candidate["original_row"]["summary_note_html"] = html
    # Qualification is read straight from the decision record so an override in
    # either direction (auto-qualified rejected, or auto-failed accepted) flows
    # through to what gets pushed to Manatal.
    qr = [r for r in candidate["roles"] if decisions_by_role.get(r, {}).get("decision") == "Approved"]
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
        _toast("Credentials saved", "✅"); st.rerun()
    if go:
        _save_creds(ne, np_, nk)
        _go("control")

    st.divider()
    st.subheader("🛡️ Test Mode Guard")
    st.caption(
        "When **Test Mode** is ON, only whitelisted emails are pushed to Manatal. "
        "All other candidates are skipped and logged. "
        "Turn it OFF only when you are ready to go live."
    )

    tm, te, _freeze_cur = _load_push_settings()

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
            _toast(f"Saved — {len(parsed)} email(s) whitelisted", "✅"); st.rerun()

        if reset_push:
            _save_push_settings(True, sorted(DEFAULT_TEST_EMAILS))
            _toast("Test emails reset to defaults", "↺")
            st.rerun()

    st.divider()

    # ── Stage Transition Freeze ───────────────────────────────────────────────
    st.subheader("⏸ Stage Transition Freeze")
    st.caption(
        "When **Freeze** is ON, fellows stay in the stage they were pulled from — "
        "no stage movement happens in either pipeline (tech or non-tech). "
        "All other data (notes, scores, custom fields) is still sent to Manatal normally. "
        "Use this to verify what is being sent before committing to stage moves, "
        "then flip it OFF when you are ready."
    )

    def _on_freeze_change():
        tm_cur, te_cur, _ = _load_push_settings()
        _save_push_settings(tm_cur, te_cur, st.session_state["f_freeze_toggle"])

    _, _, freeze_val = _load_push_settings()
    new_freeze = st.toggle(
        "Freeze stage transitions",
        value=freeze_val,
        key="f_freeze_toggle",
        on_change=_on_freeze_change,
    )
    if new_freeze:
        st.warning(
            "⏸ **Freeze ON** — stage transitions are PAUSED. "
            "Fellows will NOT be moved between stages on the next push. "
            "Notes, scores, and custom fields will still be sent normally."
        )
    else:
        st.success("▶ **Freeze OFF** — stage transitions will run normally on the next push.")

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
def _fmt_elapsed(seconds):
    s = max(0, int(seconds))
    return f"{s//60:02d}:{s%60:02d}"


def _selected_dates():
    """(start, end) as UTC timestamps, or None when the date filter is off."""
    if not st.session_state.w_use_dates:
        return None, None
    sv, ev = st.session_state.w_start, st.session_state.w_end
    sd = pd.Timestamp(sv, tz="UTC") if isinstance(sv, datetime.date) else None
    ed = pd.Timestamp(ev, tz="UTC") if isinstance(ev, datetime.date) else None
    return sd, ed


def _launch_requested(name, all_quizzes):
    """Start the task the user clicked (after any confirmation)."""
    email, password, api_key = _load_creds()
    sd, ed = _selected_dates()
    if name == "Part 1":
        quizzes = st.session_state.w_quizzes
        if set(quizzes) == set(all_quizzes):
            quizzes = []
        auto = st.session_state.w_auto
        _start_task("Part 1", _task_playwright, email, password, quizzes, auto, sd, ed, api_key,
                    follow_to_final=auto)
    elif name == "Part 2":
        _start_task("Part 2", _task_processor, False, sd, ed, api_key, follow_to_final=True)
    elif name == "Part 2 (cache)":
        _start_task("Part 2", _task_processor, True, sd, ed, api_key, follow_to_final=True)


def _request_task(name, all_quizzes):
    """
    A new Part 2 replaces the current Final Review results. If reviewers have
    edited them since the last push, ask first instead of silently throwing
    that work away (the previous run is still kept in results_prev.json).
    """
    replaces_results = name != "Part 1" or st.session_state.w_auto
    if replaces_results and store.has_unpushed_edits():
        _confirm_replace_dialog(name, all_quizzes)
    else:
        _launch_requested(name, all_quizzes)
        st.rerun()


@st.dialog("Replace unpushed review work?")
def _confirm_replace_dialog(name, all_quizzes):
    run = store.load()
    st.warning(
        f"**{name} will replace the current Final Review results** "
        f"({len(run.get('candidates', []))} candidates). They have review edits "
        "that have **not been pushed to Manatal** yet."
    )
    st.caption("The current results are kept as a backup in "
               "downloads/state/results_prev.json, but they won't be shown on the dashboard.")
    k1, k2 = st.columns(2)
    if k1.button("Yes, replace them", type="primary", key="cr_yes", use_container_width=True):
        _launch_requested(name, all_quizzes)
        st.rerun()
    if k2.button("Cancel", key="cr_no", use_container_width=True):
        st.rerun()


@st.fragment(run_every=1.0)
def _live_log(rendered_run_id, rendered_running):
    """
    Live task log. Runs on its own every second, so only this panel
    re-renders while a task runs — the rest of the page (and anything being
    typed in it) is left alone.
    """
    snap = runner.snapshot()
    if (snap["run_id"], snap["running"]) != (rendered_run_id, rendered_running):
        # A task started or finished since the page was drawn: redraw the whole
        # page so buttons and status match (and follow to Final Review).
        st.rerun()

    if snap["running"]:
        st.info(f"⏳ **{snap['name']} is running…**  ⏱ "
                f"{_fmt_elapsed(time.time() - snap['started_at'])}  ·  "
                "You can leave this page or close the tab — the task keeps running "
                "and its results are saved.")
    elif snap["run_id"]:
        took = _fmt_elapsed(snap["finished_at"] - snap["started_at"])
        if snap["succeeded"]:
            st.success(f"✅ {snap['name']} finished successfully ({took}).")
        else:
            st.error(f"❌ {snap['name']} failed after {took} — see the log below.")

    TAIL = 800   # lines rendered; the full log is in the file on disk
    visible, total = runner.tail(TAIL)
    log_text = "\n".join(visible) if visible else "(no task has run yet)"
    # column-reverse keeps the scroll position pinned to the newest line
    # without JavaScript (scripts inside st.markdown don't run).
    st.markdown(
        f"""<div style="height:420px; overflow-y:auto; display:flex;
                flex-direction:column-reverse; padding:10px;
                background:#0e1117; color:#e6edf3;
                border:1px solid #30363d; border-radius:6px;
                font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
                font-size:12px;"><div style="white-space:pre-wrap; word-break:break-word;">{_html_escape(log_text)}</div></div>""",
        unsafe_allow_html=True,
    )
    if total > TAIL:
        st.caption(f"Showing last {TAIL} of {total} lines · full log in the download below")


def page_control():
    all_roles  = sorted(ROLE_TO_TEST_MAPPING.keys())
    all_quizzes = sorted(MASTER_TEST_CONFIG.keys())

    # Re-initialise when the available role/quiz list changes (config updates, new roles added).
    _roles_fp, _quizzes_fp = ",".join(all_roles), ",".join(all_quizzes)
    if st.session_state.get("_roles_fp") != _roles_fp or "w_roles" not in st.session_state:
        st.session_state._roles_fp = _roles_fp
        st.session_state.w_roles   = all_roles[:]
    if st.session_state.get("_quizzes_fp") != _quizzes_fp or "w_quizzes" not in st.session_state:
        st.session_state._quizzes_fp = _quizzes_fp
        st.session_state.w_quizzes   = all_quizzes[:]

    def _roles_changed():
        derived = []
        for r in st.session_state.w_roles:
            for qz in ROLE_TO_TEST_MAPPING.get(r, []):
                if qz not in derived: derived.append(qz)
        st.session_state.w_quizzes = derived if derived else all_quizzes[:]

    # ── Sidebar filters ───────────────────────────────────────────────────────
    with st.sidebar:
        st.header("🔧 Filters")
        st.subheader("📅 Date Range")
        if st.checkbox("Filter by submission date", key="w_use_dates"):
            st.date_input("From", key="w_start")
            st.date_input("To",   key="w_end")

        st.multiselect(
            "Roles", options=all_roles,
            format_func=lambda r: "Non-Tech" if r == "None-Tech" else r,
            key="w_roles", on_change=_roles_changed,
        )
        qd = {q: f"{'●' if MASTER_TEST_CONFIG[q]['type']=='Required' else '○'} {q}" for q in all_quizzes}
        st.multiselect("Quizzes  (● Required  ○ Optional)",
                       options=all_quizzes, format_func=lambda q: qd[q], key="w_quizzes")

        active = st.session_state.w_quizzes
        if active and set(active) != set(all_quizzes):
            st.success(f"Filter: {len(active)} quiz(es)")
            if st.button("✕ Clear filter", key="w_clear"):
                st.session_state.w_roles   = all_roles[:]
                st.session_state.w_quizzes = all_quizzes[:]
                st.rerun()
        else:
            st.caption("No filter — all quizzes will be downloaded.")

    # ── Header ────────────────────────────────────────────────────────────────
    snap = runner.snapshot()
    running = snap["running"]
    st.title("🎯 Control Panel")

    st.checkbox("Run Part 2 automatically after successful Part 1", key="w_auto")

    # ── Buttons ───────────────────────────────────────────────────────────────
    quilgo_ready = (config.QUILGO_MASTER_DIR.exists()
                    and any(config.QUILGO_MASTER_DIR.glob("*.csv")))
    cache_ready  = config.MANATAL_CACHE_FILE.exists()

    c1, c2, c3 = st.columns(3)
    with c1:
        if st.button("▶ Run Downloader (Part 1)", disabled=running, type="primary",
                     use_container_width=True, key="btn_p1"):
            _request_task("Part 1", all_quizzes)
    with c2:
        if st.button("▶ Run Processor (Part 2)", disabled=(running or not quilgo_ready),
                     type="primary", use_container_width=True, key="btn_p2",
                     help="Run Part 1 first to download Quilgo data"):
            _request_task("Part 2", all_quizzes)
    with c3:
        if st.button("🔄 Re-run Processor (Cache)", disabled=(running or not cache_ready),
                     use_container_width=True, key="btn_p2c",
                     help="Skips Manatal fetch, uses cached profiles"):
            _request_task("Part 2 (cache)", all_quizzes)

    if st.session_state.final_results:
        if st.button("📊 Go to Final Review →", key="btn_qa"):
            _go("final_review")

    # ── Log ───────────────────────────────────────────────────────────────────
    lc, bc = st.columns([9, 1])
    with lc: st.subheader("📋 Task Log")
    with bc:
        if st.button("🗑", key="btn_clear", disabled=running, help="Clear log"):
            runner.clear_log(); st.rerun()

    _live_log(snap["run_id"], running)

    # Download of the finished run's full log (outside the fragment so the
    # file isn't re-registered every second).
    lf = snap["log_file"]
    if lf and not running and Path(lf).exists():
        try:
            st.download_button(
                "⬇ Download full run log", data=Path(lf).read_bytes(),
                file_name=Path(lf).name, mime="text/plain", key="btn_dl_log",
                help=f"Full log file for this run (auto-deleted after {LOG_RETENTION_DAYS} days)",
            )
        except Exception:
            pass
    if lf:
        st.caption(f"📄 Log file: `{Path(lf).name}` · kept for {LOG_RETENTION_DAYS} days")

    # ── Folder info ───────────────────────────────────────────────────────────
    fc1, fc2, _ = st.columns([1, 1, 4])
    with fc1:
        if quilgo_ready and st.button("📂 Quilgo folder", key="btn_f1"):
            st.info(str(config.QUILGO_MASTER_DIR))
    with fc2:
        if cache_ready and st.button("📂 Backups folder", key="btn_f2"):
            st.info(str(config.DOWNLOADS_DIR))

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


def _is_notable_decision(d):
    """
    True when a role's decision record represents something a reviewer
    actually did — an unresolved Pending, a genuine manual-review resolution
    (automatic_outcome is None), or an override of an auto-decided role.

    False when the reviewer simply agrees with the automatic outcome — every
    auto-qualified/auto-failed role carries a decision record now, but a
    silent agreement shouldn't read as "manual activity" anywhere in the UI
    (summary counts, filters, or the collapse-approved view).
    """
    if d.get("decision") == "Pending":
        return True
    auto = d.get("automatic_outcome")
    if auto is None:
        return True  # resolved manual-review role
    agrees = (
        (auto == "Qualified" and d.get("decision") == "Approved") or
        (auto == "Not Qualified" and d.get("decision") == "Rejected")
    )
    return not agrees


def _final_status(cand):
    # A candidate with any Pending decision is neither APPROVED nor REJECTED
    # yet — they're PENDING and can't be pushed to Manatal until resolved.
    if _has_pending(cand):
        return "PENDING", []
    # Every role now carries a decision record (auto-decided or manually
    # reviewed), so it alone is the single source of truth for qualification —
    # this also means an override in either direction is respected here.
    all_q = sorted({
        d["role"] for d in cand.get("manual_decisions", [])
        if d.get("decision") == "Approved"
    })
    return ("APPROVED" if all_q else "REJECTED"), all_q


def _role_rejection_reason(rdata, dec):
    """Why a rejected role was rejected: 'manual', 'integrity' or 'score'."""
    auto = (dec or {}).get("automatic_outcome")
    if auto != "Not Qualified":
        # None → resolved manual-review role; "Qualified" → reviewer overrode a pass.
        return "manual"
    if any("Auto-Fail" in str(t.get("status", "")) for t in rdata.get("tests", [])):
        return "integrity"
    return "score"


def _rejection_reason_text(kind):
    return {
        "manual":    "rejected by reviewer",
        "integrity": "auto-failed on integrity (excessive tab switches)",
        "score":     f"fewer than {MIN_PASSING_TESTS} tests scored ≥ {PASS_THRESHOLD}",
    }[kind]


def _rejection_outcome(cand):
    """
    Split a REJECTED candidate into the message the dashboard shows:
      'not_attempted' — no submission for any assessed quiz
      'failed'        — attempted, and every role failed on score alone
      'not_met'       — anything else (integrity auto-fail, reviewer
                        rejection, or a mix of reasons)
    Returns (kind, [(role, reason_text), ...]).
    """
    roles = cand.get("roles", {}) or {}
    if "No Submission" in roles:
        return "not_attempted", []
    decs = {d.get("role"): d for d in cand.get("manual_decisions", [])}
    reasons = [(rn, _role_rejection_reason(rd, decs.get(rn)))
               for rn, rd in roles.items()
               if decs.get(rn, {}).get("decision") != "Approved"]
    kinds = {k for _, k in reasons}
    kind = "failed" if kinds == {"score"} else "not_met"
    return kind, [(rn, _rejection_reason_text(k)) for rn, k in reasons]

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
    if f["status"] == "Has manual decision" and not any(
        _is_notable_decision(d) for d in cand.get("manual_decisions", [])
    ): return False

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

def _same_value(a, b):
    """Equal as entered — "9" and "9.0" are the same score."""
    if a == b:
        return True
    try:
        return float(a) == float(b)
    except (TypeError, ValueError):
        return False


def _store_draft(run_id, key, saved):
    """
    on_change: keep typed-but-unsaved dashboard input on disk. Only real
    differences from the saved value count — Streamlit also fires on_change
    when a widget is redrawn after a save, and putting a value back the way
    it was should clear the "unsaved draft" marker.
    """
    if key not in st.session_state:   # widget already gone (e.g. decision just saved)
        return
    value = st.session_state[key]
    if _same_value(value, saved):
        store.clear_drafts(run_id, [key])
    else:
        store.set_draft(run_id, key, value)


def _commit_candidate(i, cand, ck, run_id):
    """Save a decided/edited candidate and drop its drafts."""
    _save_candidate(i, cand)
    marker = f"_{ck}_"
    # Widget state is deliberately left alone: deleting a widget's key while
    # Streamlit is still handling it crashes the next on_change (KeyError) when
    # reviewers click quickly. The widgets already hold the saved values, and a
    # resolved Pending decision's box disappears on the redraw anyway.
    store.clear_drafts(run_id, [k for k in store.load_drafts(run_id) if marker in k])


@st.dialog("Push to Manatal?")
def _confirm_push_dialog():
    """Show exactly what the push will do before anything is sent."""
    cands = store.load().get("candidates", [])
    test_mode, test_emails, freeze = _load_push_settings()
    allowed = {e.strip().lower() for e in test_emails}
    if test_mode:
        n_push = sum(1 for c in cands if str(c.get("email", "")).strip().lower() in allowed)
    else:
        n_push = len(cands)
    approved = sum(1 for c in cands if _final_status(c)[0] == "APPROVED")

    st.markdown(f"**{n_push} of {len(cands)} fellow(s) will be updated in Manatal** "
                f"({approved} approved, {len(cands) - approved} not).")
    if test_mode:
        st.warning(f"🛡️ **Test Mode ON** — only the {len(allowed)} whitelisted email(s) "
                   "are pushed; everyone else is skipped.")
    else:
        st.error("🚀 **Test Mode OFF** — this is a live push to every fellow listed.")
    if freeze:
        st.info("⏸ **Stage Freeze ON** — notes and scores are sent, but nobody is moved "
                "between stages.")
    else:
        st.info("▶ Fellows will be moved to their next stage.")
    st.caption("Each fellow gets a new note on their Manatal match. This can't be undone "
               "from here.")

    k1, k2 = st.columns(2)
    if k1.button("Push now", type="primary", key="pd_yes", use_container_width=True,
                 disabled=n_push == 0):
        _, _, ak = _load_creds()
        # Push exactly what is saved on disk (every dashboard edit is saved
        # there the moment it's made).
        if _start_task("API Push", _task_api_push, ak, cands):
            _go("control")
        st.rerun()
    if k2.button("Cancel", key="pd_no", use_container_width=True):
        st.rerun()


def page_final_review():
    results = st.session_state.final_results or []
    # Forward-compat: normalise any MANUAL REVIEW roles without a matching
    # Pending entry (see _normalise_pending for the full rationale). Safe
    # no-op on already-normalised candidates.
    for _c in results:
        if _normalise_pending(_c):
            store.update_candidate(_c)
    # Pre-compute status for every candidate — drives summary strip, filters,
    # AND the push guard, so this has to happen before the action bar.
    status_cache = [_final_status(c) for c in results] if results else []
    pending_count = sum(1 for s, _ in status_cache if s == "PENDING")

    # ── Top action bar ────────────────────────────────────────────────────────
    cb, cp = st.columns([2, 1])
    with cb:
        if st.button("← Back to Control Panel", key="fr_back"):
            _go("control")
    with cp:
        task = runner.snapshot()
        # Block push while ANY candidate has a Pending manual decision —
        # otherwise un-reviewed cases would silently ship to Manatal.
        if task["running"]:
            st.button(f"🚀 Push unavailable — {task['name']} is running",
                      key="fr_push_busy", disabled=True)
        elif pending_count > 0:
            st.button(
                f"🚀 Push blocked — {pending_count} pending",
                key="fr_push_blocked", disabled=True,
                help="Resolve all Pending manual decisions before pushing.",
            )
        else:
            if st.button("🚀 Push All to Manatal", type="primary", key="fr_push"):
                _confirm_push_dialog()

    if not results:
        st.info("No results yet. Run Part 2 first."); return

    # "A new run replaced your results" — at the top of the page, and it stays
    # until the reviewer dismisses it, so it can't vanish on the next click.
    run_id = st.session_state.results_run_id
    seen = st.session_state.get("_rendered_run_id")
    if seen and seen != run_id:
        st.session_state.stale_notice = True
    st.session_state._rendered_run_id = run_id
    if st.session_state.stale_notice:
        n1, n2 = st.columns([6, 1])
        n1.warning("🔄 **A new run replaced the results you were looking at.** "
                   "Anything you clicked on the old results was not applied — "
                   "please review the current list below.")
        if n2.button("Got it", key="stale_ok", use_container_width=True):
            st.session_state.stale_notice = False
            st.rerun()

    run_meta = store.load()
    def _when(ts):
        try:
            return datetime.datetime.fromisoformat(ts).astimezone().strftime("%d %b %H:%M")
        except Exception:
            return "—"
    st.caption(
        f"💾 Results from the {run_meta.get('task', 'Part 2')} run of "
        f"**{_when(run_meta.get('created_at'))}** · last edit {_when(run_meta.get('edited_at'))} "
        f"· last push {_when(run_meta.get('pushed_at'))} · "
        "every decision is saved as soon as you make it."
    )

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
    manual   = sum(1 for c in results
                   if any(_is_notable_decision(d) for d in c.get("manual_decisions", [])))
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
                "Collapse approved", key="fr_f_collapse_happy",
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
    run_id = st.session_state.results_run_id
    drafts = store.load_drafts(run_id)

    # Results were replaced by a new run since this tab last drew them.

    def _dkw(key, saved):
        """Widget kwargs: start from the draft if there is one, save edits as drafts."""
        return dict(value=drafts.get(key, saved), key=key,
                    on_change=_store_draft, args=(run_id, key, saved))

    for (i, cand) in filtered:
        # The run id is part of every widget key, so a click made on a page
        # still showing an older run can't land on the new run's candidate.
        ck = f"{run_id}_{store.candidate_key(cand)}"
        has_draft = any(f"_{ck}_" in k for k in drafts)
        status, qroles = status_cache[i]
        if status == "APPROVED":  icon = "🟢"
        elif status == "PENDING": icon = "⏳"
        else:                     icon = "🔴"
        has_manual  = any(_is_notable_decision(d) for d in cand.get("manual_decisions", []))
        has_empty   = _has_empty_score(cand)
        has_pending = _has_pending(cand)

        # "Happy path": approved, no manual decisions, no missing scores, no
        # pending reviews → render as a compact one-line row. Pending ALWAYS
        # gets a full expander so the reviewer can act on it.
        compact = (f_collapse_happy and status == "APPROVED"
                   and not has_manual and not has_empty and not has_pending
                   and not has_draft)

        if compact:
            roles_line = ", ".join(_rl(r) for r in qroles) if qroles else "—"
            st.markdown(
                f"<div style='padding:8px 12px;border:1px solid #e5e7eb;"
                f"border-radius:6px;margin-bottom:6px;'>"
                f"{icon} <b>{_html_escape(cand.get('full_name','N/A'))}</b> "
                f"<span style='color:#6b7280;'>· {_copyable_email_html(cand.get('email','N/A'))}</span> "
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

        # Auto-expand the ones that need your eyes — Pending always expands
        expand_default = (status in ("REJECTED", "PENDING")
                          or has_manual or has_empty or has_draft)
        if has_draft: flags.append("📝 unsaved draft")
        flag_suffix = f"  ·  {' · '.join(flags)}" if flags else ""

        # Rejected candidates get a more specific label so "did not attempt"
        # and "failed" aren't lumped together under one generic REJECTED.
        rej_kind, rej_reasons = (_rejection_outcome(cand) if status == "REJECTED"
                                 else (None, []))
        status_label = {"not_attempted": "DID NOT ATTEMPT",
                        "failed": "FAILED"}.get(rej_kind, status)
        header = (f"{icon} **{cand.get('full_name','N/A')}** — {status_label}  |  "
                  f"{cand.get('email','N/A')}")

        with st.expander(header + flag_suffix, expanded=expand_default):
            # Content (in order):
            #   1. Outcome summary (qualified / pending / did not attempt / failed)
            #   2. "Integrity flags:" red box — every integrity reason per role
            #   3. "Full breakdown" expander (open by default) — per-role
            #      status icon + status, then per-test `name: score | status`

            st.markdown(f"📧 {_copyable_email_html(cand.get('email','N/A'))}", unsafe_allow_html=True)

            if qroles:
                st.success("Qualified for: " + ", ".join(_rl(r) for r in qroles))
            elif status == "PENDING":
                pending_roles = [d.get("role") for d in cand.get("manual_decisions", [])
                                 if d.get("decision") == "Pending"]
                st.info("Awaiting review — decision pending for: "
                        + ", ".join(_rl(r) for r in pending_roles))
            elif rej_kind == "not_attempted":
                st.warning("Did not attempt — no submissions found for any of the "
                           "assessed quizzes.")
            else:
                if rej_kind == "failed":
                    st.error(f"Failed — did not reach the passing score "
                             f"({MIN_PASSING_TESTS} tests ≥ {PASS_THRESHOLD}) for any role.")
                else:
                    st.error("Did not meet requirements for any role.")
                if rej_reasons:
                    st.caption(" · ".join(f"{_rl(rn)}: {why}" for rn, why in rej_reasons))

            # (2) Integrity flags
            integrity_reasons = []
            for rname, rdata in cand.get("roles", {}).items():
                for reason in rdata.get("manual_review_reasons", []):
                    integrity_reasons.append((rname, reason))
            if integrity_reasons:
                # st.error can't render a role→reason list cleanly, so we
                # hand-roll a red box.
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

            # (3) Full breakdown:
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
                        v = st.text_input(label, **_dkw(f"sc_{ck}_{uk}", str(raw)))
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
            #   • Pending — shown with Approve / Reject buttons
            #     and a required justification. Clicking a button saves
            #     immediately.
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

                    nj = st.text_area(
                        "Justification (required for Approve/Reject)",
                        height=80, **_dkw(f"pj_{ck}_{j}", dec.get("justification", "")),
                    )

                    err_key = f"pe_{ck}_{j}"

                    b1, b2, _sp = st.columns([1, 1, 3])
                    with b1:
                        if st.button("✅ Approve", type="primary",
                                     key=f"pa_{ck}_{j}"):
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
                                _toast(f"Approved — {cand.get('full_name','')} · {_rl(rn)}", "✅")
                                edited["manual_decisions"][j]["decision"] = "Approved"
                                edited["manual_decisions"][j]["justification"] = nj.strip()
                                if rn in edited.get("roles", {}):
                                    edited["roles"][rn]["status"] = (
                                        "QUALIFIED (Manually Approved)")
                                refreshed = _reevaluate(edited)
                                st.session_state.pop(err_key, None)
                                _commit_candidate(i, refreshed, ck, run_id)
                                st.rerun()
                    with b2:
                        if st.button("❌ Reject", key=f"pr_{ck}_{j}"):
                            if not (nj or "").strip():
                                st.session_state[err_key] = "Enter a justification."
                                st.rerun()
                            else:
                                try:
                                    _apply_score_edits(edited, score_inp)
                                except ValueError:
                                    st.session_state[err_key] = "Scores must be numbers."
                                    st.rerun()
                                _toast(f"Rejected — {cand.get('full_name','')} · {_rl(rn)}", "❌")
                                edited["manual_decisions"][j]["decision"] = "Rejected"
                                edited["manual_decisions"][j]["justification"] = nj.strip()
                                if rn in edited.get("roles", {}):
                                    edited["roles"][rn]["status"] = (
                                        "FAIL (Manually Rejected)")
                                refreshed = _reevaluate(edited)
                                st.session_state.pop(err_key, None)
                                _commit_candidate(i, refreshed, ck, run_id)
                                st.rerun()

                    err = st.session_state.get(err_key)
                    if err:
                        st.error(err)

                    st.markdown("---")

            if resolved_decs:
                st.markdown("### Role Decisions")
                st.caption(
                    "Every role has a decision here. Auto-qualified/auto-failed roles "
                    "start pre-filled agreeing with the automatic result — change the "
                    "decision to override it. A justification is only required when "
                    "the decision disagrees with the automatic result (or for a "
                    "genuine manual-review role, same as before)."
                )
                for (j, dec) in resolved_decs:
                    rn = dec.get("role", "N/A")
                    auto = dec.get("automatic_outcome")
                    d1, d2 = st.columns([1, 2])
                    with d1:
                        dkey = f"dec_{ck}_{j}"
                        nd = st.selectbox(
                            f"Decision '{_rl(rn)}'", ["Approved", "Rejected"],
                            index=0 if drafts.get(dkey, dec.get("decision")) == "Approved" else 1,
                            key=dkey, on_change=_store_draft,
                            args=(run_id, dkey, dec.get("decision")),
                        )
                        if auto is not None:
                            st.caption(f"Automatic result: {auto}")
                    with d2:
                        overriding = auto is not None and not (
                            (auto == "Qualified" and nd == "Approved") or
                            (auto == "Not Qualified" and nd == "Rejected")
                        )
                        just_label = f"Justification '{_rl(rn)}'"
                        if auto is None or overriding:
                            just_label += "  (required)"
                        nj2 = st.text_area(
                            just_label, height=80,
                            **_dkw(f"just_{ck}_{j}", dec.get("justification", "")),
                        )
                        if overriding:
                            st.caption("⚠ Overrides the automatic result.")
                    edited["manual_decisions"][j]["decision"]      = nd
                    edited["manual_decisions"][j]["justification"] = nj2

            if has_draft:
                st.caption("📝 You have unsaved changes for this candidate (kept as a draft "
                           "— they'll still be here if you leave the page). Click Save to apply them.")
            if st.button("💾 Save & Re-evaluate", key=f"save_{ck}"):
                # Justification is required whenever a decision disagrees with the
                # automatic result, and always for a genuine manual-review role
                # (automatic_outcome is None) — same rule as the Pending flow above.
                # Roles still Pending are skipped here; they're resolved via their
                # own Approve/Reject buttons, not this Save action.
                missing_justification = []
                for dec in edited.get("manual_decisions", []):
                    if dec.get("decision") == "Pending":
                        continue
                    auto = dec.get("automatic_outcome")
                    agrees = (
                        (auto == "Qualified" and dec.get("decision") == "Approved") or
                        (auto == "Not Qualified" and dec.get("decision") == "Rejected")
                    )
                    requires_justification = auto is None or not agrees
                    if requires_justification and not (dec.get("justification") or "").strip():
                        missing_justification.append(dec.get("role", "?"))

                if missing_justification:
                    st.error(
                        "Justification required for: "
                        + ", ".join(_rl(r) for r in missing_justification)
                    )
                else:
                    try:
                        _apply_score_edits(edited, score_inp)
                        edited = _reevaluate(edited)
                        _commit_candidate(i, edited, ck, run_id)
                        _toast(f"Saved — {cand.get('full_name','')}", "💾")
                        st.rerun()
                    except ValueError:
                        st.error("Scores must be numbers.")
                    except Exception as e:
                        st.error(str(e))

# ══════════════════════════════════════════════════════════════════════════════
# Router
# ══════════════════════════════════════════════════════════════════════════════
@st.fragment(run_every=2.0)
def _sidebar_status():
    """Task status on every page, refreshed on its own without redrawing the page."""
    snap = runner.snapshot()
    if snap["running"]:
        st.info(f"⏳ **{snap['name']}** running · "
                f"{_fmt_elapsed(time.time() - snap['started_at'])}")
    elif snap["run_id"]:
        if snap["succeeded"]:
            st.success(f"✅ {snap['name']} finished")
        else:
            st.error(f"❌ {snap['name']} failed")
    if (st.session_state.page == "final_review"
            and store.version() != st.session_state.results_version):
        st.warning("New results were saved since this page loaded.")
        if st.button("↻ Load latest results", key="sb_reload", use_container_width=True):
            st.rerun(scope="app")


def main():
    # First visit with no saved credentials → start on Settings.
    if not st.session_state.get("_landed"):
        st.session_state._landed = True
        if "page" not in st.query_params and not _load_creds()[2]:
            st.session_state.page = "settings"

    _sync_results()
    _show_toasts()

    # Follow a Part 2 this tab started through to the Final Review once it's done.
    snap = runner.snapshot()
    aw = st.session_state.awaiting_run_id
    if aw and not snap["running"] and snap["run_id"] == aw:
        st.session_state.awaiting_run_id = None
        if snap["succeeded"] and st.session_state.page == "control" and st.session_state.final_results:
            _go("final_review")

    with st.sidebar:
        if (config.ASSETS_DIR / "logo.png").exists():
            st.image(str(config.ASSETS_DIR / "logo.png"), width=120)
        st.title("Navigation")
        if st.button("⚙️ Settings",      use_container_width=True, key="nav_settings"):
            _go("settings")
        if st.button("🎯 Control Panel", use_container_width=True, key="nav_control"):
            _go("control")
        if st.session_state.final_results:
            if st.button("📊 Final Review", use_container_width=True, key="nav_final"):
                _go("final_review")

        _sidebar_status()

        # Test Mode + Stage Freeze badges — visible on every page.
        _tm, _te, _freeze = _load_push_settings()
        if _tm:
            st.warning(f"🛡️ **Test Mode ON** — only {len(_te)} whitelisted email(s) will be pushed.")
        else:
            st.success("🚀 **Test Mode OFF** — all candidates will be pushed.")
        if _freeze:
            st.warning("⏸ **Stage Freeze ON** — fellows will NOT be moved between stages.")
        st.divider()

    p = st.session_state.page
    if p == "settings":       page_settings()
    elif p == "control":      page_control()
    elif p == "final_review": page_final_review()
    else: _go("control")

if __name__ == "__main__":
    main()
