# v2/app/task_runner.py
#
# Runs Part 1 / Part 2 / the API push in a background thread that belongs to
# the Streamlit server process, not to a browser tab.
#
# Previously each task reported through a queue held in one tab's
# st.session_state: only that tab could see the log, the "running" flag was
# lost on refresh while the thread kept going, and another tab could start a
# second task on top of it. Now there is one runner for the whole app; every
# page and every tab reads the same status and log, and only one task can run
# at a time.

import datetime
import threading
import time
import traceback

from . import config

LOGS_DIR = config.DOWNLOADS_DIR / "logs"
LOG_RETENTION_DAYS = 3


def _prune_old_logs():
    """Delete session log files older than LOG_RETENTION_DAYS. Best-effort — never raises."""
    try:
        cutoff = time.time() - LOG_RETENTION_DAYS * 86400
        for f in LOGS_DIR.glob("session_*.log"):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
            except Exception:
                pass
    except Exception:
        pass


class TaskRunner:
    def __init__(self):
        self._lock = threading.Lock()
        self.run_id = 0          # increments on every start; lets a tab spot "my run finished"
        self.name = ""
        self.running = False
        self.started_at = 0.0
        self.finished_at = 0.0
        self.succeeded = None    # None while running / before any run
        self.lines = []
        self.log_file = None

    # ── called from the UI ───────────────────────────────────────────────────
    def start(self, name, target, *args):
        """
        Start `target(emit, *args)` in a background thread. `target` returns
        True on success. Returns the new run_id, or None if a task is already
        running (only one task at a time — they share processor state and
        redirect stdout).
        """
        with self._lock:
            if self.running:
                return None
            self.run_id += 1
            self.name = name
            self.running = True
            self.started_at = time.time()
            self.finished_at = 0.0
            self.succeeded = None
            self.lines = []
            self.log_file = self._new_log_file(name)
            run_id = self.run_id
        threading.Thread(target=self._run, args=(run_id, target, args), daemon=True).start()
        return run_id

    def clear_log(self):
        with self._lock:
            if not self.running:
                self.lines = []

    def snapshot(self):
        """A consistent copy of the status for rendering."""
        with self._lock:
            return {
                "run_id": self.run_id, "name": self.name, "running": self.running,
                "started_at": self.started_at, "finished_at": self.finished_at,
                "succeeded": self.succeeded, "n_lines": len(self.lines),
                "log_file": self.log_file,
            }

    def tail(self, n):
        with self._lock:
            return list(self.lines[-n:]), len(self.lines)

    # ── called from the worker thread ────────────────────────────────────────
    def emit(self, line):
        line = str(line)
        with self._lock:
            self.lines.append(line)
            path = self.log_file
        if path:
            try:
                ts = datetime.datetime.now().strftime("%H:%M:%S")
                with open(path, "a", encoding="utf-8") as f:
                    f.write(f"[{ts}] {line}\n")
            except Exception:
                pass

    def _run(self, run_id, target, args):
        ok = False
        try:
            ok = bool(target(self.emit, *args))
        except Exception as e:
            self.emit(f"❌ EXCEPTION in {self.name}: {e}")
            self.emit(traceback.format_exc())
        finally:
            with self._lock:
                if self.run_id == run_id:
                    self.running = False
                    self.succeeded = ok
                    self.finished_at = time.time()

    @staticmethod
    def _new_log_file(task_name):
        try:
            _prune_old_logs()
            LOGS_DIR.mkdir(parents=True, exist_ok=True)
            ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            safe = "".join(c if c.isalnum() else "_" for c in (task_name or "task"))
            path = LOGS_DIR / f"session_{ts}_{safe}.log"
            with open(path, "w", encoding="utf-8") as f:
                f.write("# Quilgo Automation Suite — session log\n")
                f.write(f"# Task: {task_name}\n")
                f.write(f"# Started: {datetime.datetime.now().isoformat(timespec='seconds')}\n")
                f.write("#" + "─" * 60 + "\n")
            return path
        except Exception:
            return None


# One runner per server process. Streamlit re-executes streamlit_app.py on
# every interaction, but imported modules stay loaded, so this is shared by
# every tab.
runner = TaskRunner()
