# v2/app/results_store.py
#
# On-disk store for the Final Review dashboard.
#
# Streamlit's st.session_state lives only as long as one browser tab's
# connection: a refresh, a closed tab, a dropped connection or a container
# restart throws away the results and every reviewer decision in it. This
# module keeps that state on disk instead (under downloads/state/, which the
# Docker volume already persists) so any tab can pick it back up.
#
# Files:
#   results.json       current run: candidates + metadata. Rewritten after
#                      Part 2 and after every Approve / Reject / Save.
#   results_prev.json  the previous run, kept as a safety net when a new
#                      Part 2 run replaces the current one.
#   integrity.json     integrity flags from the run that produced results.json,
#                      needed to re-evaluate a candidate after a restart.
#   drafts.json        text typed into the dashboard but not saved yet
#                      (justifications, score edits, decision changes).

import json
import math
import os
import shutil
import threading
import uuid
from datetime import datetime, timezone

import pandas as pd

from . import config

STATE_DIR = config.DOWNLOADS_DIR / "state"
RESULTS_FILE = STATE_DIR / "results.json"
PREV_RESULTS_FILE = STATE_DIR / "results_prev.json"
INTEGRITY_FILE = STATE_DIR / "integrity.json"
DRAFTS_FILE = STATE_DIR / "drafts.json"

# One lock for every read-modify-write: background tasks and every browser tab
# run in the same Streamlit server process.
_lock = threading.RLock()


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _json_default(o):
    """Make pandas/numpy values JSON-safe without turning numbers into strings."""
    if isinstance(o, (pd.Timestamp, datetime)):
        return o.isoformat()
    if hasattr(o, "item"):            # numpy scalars (int64, float64, bool_)
        return o.item()
    if hasattr(o, "tolist"):          # numpy arrays
        return o.tolist()
    return str(o)


def _write_json(path, data):
    """Atomic write: a crash mid-write never leaves a half-written file."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, default=_json_default)
    os.replace(tmp, path)


def _read_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def candidate_key(cand):
    """Stable identity for a candidate within a run (a fellow can sit in two jobs)."""
    row = cand.get("original_row") or {}
    cid, match = row.get("id"), row.get("match_pk")
    if cid is not None and not (isinstance(cid, float) and math.isnan(cid)):
        return f"{cid}-{match}"
    return str(cand.get("email", ""))


# ── Results ──────────────────────────────────────────────────────────────────

def version():
    """Changes whenever results.json is rewritten; cheap enough to call every rerun."""
    try:
        return RESULTS_FILE.stat().st_mtime_ns
    except FileNotFoundError:
        return None


def load():
    """Return the stored run as a dict ({} when nothing has been saved yet)."""
    with _lock:
        return _read_json(RESULTS_FILE, {})


def save_new_run(candidates, integrity_df, task_name):
    """
    Store the output of a fresh Part 2 run. The previous run is kept in
    results_prev.json, and drafts from it are dropped (they belong to
    candidates that no longer exist in this form).
    """
    with _lock:
        if RESULTS_FILE.exists():
            shutil.copyfile(RESULTS_FILE, PREV_RESULTS_FILE)
        records = (integrity_df.to_dict(orient="records")
                   if integrity_df is not None and not integrity_df.empty else [])
        _write_json(INTEGRITY_FILE, records)
        run = {
            "run_id": uuid.uuid4().hex[:12],
            "task": task_name,
            "created_at": _now(),
            "edited_at": None,
            "pushed_at": None,
            "candidates": candidates,
        }
        _write_json(RESULTS_FILE, run)
        _write_json(DRAFTS_FILE, {"run_id": run["run_id"], "values": {}})
        return run["run_id"]


def update_candidate(cand):
    """
    Save one edited candidate. Re-reads the file under the lock and replaces
    only this candidate, so two reviewers working on different candidates in
    different tabs don't overwrite each other.
    """
    key = candidate_key(cand)
    with _lock:
        run = _read_json(RESULTS_FILE, {})
        cands = run.get("candidates", [])
        for i, c in enumerate(cands):
            if candidate_key(c) == key:
                cands[i] = cand
                break
        else:
            raise KeyError(f"candidate {key} is not in the stored results")
        run["edited_at"] = _now()
        _write_json(RESULTS_FILE, run)


def mark_pushed():
    with _lock:
        run = _read_json(RESULTS_FILE, {})
        if run:
            run["pushed_at"] = _now()
            _write_json(RESULTS_FILE, run)


def has_unpushed_edits():
    run = load()
    edited, pushed = run.get("edited_at"), run.get("pushed_at")
    return bool(edited) and (not pushed or edited > pushed)


def load_integrity_df():
    with _lock:
        return pd.DataFrame(_read_json(INTEGRITY_FILE, []))


# ── Drafts (typed but not yet saved) ────────────────────────────────────────

def load_drafts(run_id):
    with _lock:
        d = _read_json(DRAFTS_FILE, {})
    return d.get("values", {}) if d.get("run_id") == run_id else {}


def set_draft(run_id, key, value):
    with _lock:
        d = _read_json(DRAFTS_FILE, {})
        if d.get("run_id") != run_id:
            d = {"run_id": run_id, "values": {}}
        d["values"][key] = value
        _write_json(DRAFTS_FILE, d)


def clear_drafts(run_id, keys):
    with _lock:
        d = _read_json(DRAFTS_FILE, {})
        if d.get("run_id") != run_id:
            return
        for k in keys:
            d["values"].pop(k, None)
        _write_json(DRAFTS_FILE, d)
