"""Runs queued automation jobs, one at a time per automation.

Why one at a time per automation: each automation signs in to ONE Office
Ally account, and the engines already note that two windows working under the
same login can hang. ACO and Non ACO use different accounts, so they run side
by side; a second run of the same automation waits its turn in the queue.

Every job is a separate OS process (backend/runner/runner.py), so a crash,
hang or cancel never touches the web server. State lives in SQLite, so a
restart of the server loses nothing: queued runs resume, runs that were
mid-flight are marked failed (their sheet resumes from its status file).
"""

import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from ..db import utcnow
from .audit import log_activity

log = logging.getLogger("automation.jobs")

RUNNER = Path(__file__).resolve().parents[2] / "runner" / "runner.py"
EVENT_PREFIX = "@@EVENT "
ONESHOT_TIMEOUT = 90          # seconds for describe / validate
RUN_TIMEOUT_HOURS = float(os.environ.get("AUTOMATION_RUN_TIMEOUT_HOURS", "12"))


def workspace_for(settings, slug: str, user_id: int) -> dict:
    """Per user, per automation: the engine resumes a sheet from its own
    output file, so users must not share one folder. The saved Office Ally
    login is per automation (shared), because runs of an automation are
    serialised."""
    base = settings.uploads_dir / slug
    user_dir = base / f"user-{user_id}"
    return {
        "data_dir": user_dir / "data",
        "output_dir": user_dir / "output",
        "state_path": base / "auth_state.json",
    }


def _popen_kwargs() -> dict:
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def kill_tree(proc: subprocess.Popen) -> None:
    """Stop the runner and every browser it started."""
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           capture_output=True, timeout=20)
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except OSError:
            pass


class JobManager:
    def __init__(self, ctx):
        self.ctx = ctx
        self._cv = threading.Condition()
        self._threads: dict[str, threading.Thread] = {}
        self._procs: dict[int, subprocess.Popen] = {}
        self._stopping = False
        self._describe_cache: dict[str, dict] = {}

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> None:
        with self.ctx.db.session() as conn:
            conn.execute(
                "UPDATE runs SET status='failed', finished_at=?, error=? WHERE status='running'",
                (utcnow(), "The server restarted while this run was in progress. "
                           "Run the same sheet again - rows already Done are skipped."))
        for slug in self.ctx.registry.slugs():
            self._ensure_thread(slug)
        with self._cv:
            self._cv.notify_all()

    def stop(self) -> None:
        with self._cv:
            self._stopping = True
            self._cv.notify_all()
        for proc in list(self._procs.values()):
            kill_tree(proc)

    def _ensure_thread(self, slug: str) -> None:
        thread = self._threads.get(slug)
        if thread is None or not thread.is_alive():
            thread = threading.Thread(target=self._worker_loop, args=(slug,),
                                      name=f"jobs-{slug}", daemon=True)
            self._threads[slug] = thread
            thread.start()

    # ------------------------------------------------------------------ public API

    def submit(self, run_id: int, slug: str) -> None:
        self._ensure_thread(slug)
        with self._cv:
            self._cv.notify_all()

    def cancel(self, run_id: int) -> str:
        """Returns the run's new status."""
        with self.ctx.db.session() as conn:
            row = conn.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()
            if not row:
                return "missing"
            if row["status"] == "queued":
                conn.execute("UPDATE runs SET status='cancelled', finished_at=?, cancel_requested=1,"
                             " error='Cancelled before it started' WHERE id=? AND status='queued'",
                             (utcnow(), run_id))
                return "cancelled"
            if row["status"] == "running":
                conn.execute("UPDATE runs SET cancel_requested=1 WHERE id=?", (run_id,))
        proc = self._procs.get(run_id)
        if proc is not None:
            kill_tree(proc)
        return "cancelling"

    def queue_position(self, run_id: int, slug: str) -> int:
        """How many runs of this automation are ahead of this one (queued or
        running). 0 = it goes next / is going now, 1 = one run is in front."""
        with self.ctx.db.session() as conn:
            return conn.execute(
                "SELECT COUNT(*) AS n FROM runs WHERE automation_slug=? AND status IN ('queued','running') AND id<?",
                (slug, run_id)).fetchone()["n"]

    # ------------------------------------------------------------------ one-shot helpers

    def _oneshot(self, automation, job: dict) -> dict:
        job_dir = self.ctx.settings.runs_dir / "_oneshot"
        job_dir.mkdir(parents=True, exist_ok=True)
        job_file = job_dir / f"{os.getpid()}-{threading.get_ident()}-{time.time_ns()}.json"
        job_file.write_text(json.dumps({**job, "engine_dir": str(automation.engine_dir)}), encoding="utf-8")
        try:
            proc = subprocess.run(
                [self.ctx.settings.python_bin, str(RUNNER), str(job_file)],
                cwd=str(automation.engine_dir), capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=ONESHOT_TIMEOUT,
                stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            return {"type": "error", "error": "Timed out reading the sheet."}
        finally:
            job_file.unlink(missing_ok=True)
        for line in proc.stdout.splitlines():
            if line.startswith(EVENT_PREFIX):
                return json.loads(line[len(EVENT_PREFIX):])
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-3:]
        return {"type": "error", "error": "Could not read the sheet: " + " | ".join(tail)[:300]}

    def default_map(self, automation) -> dict:
        cached = self._describe_cache.get(automation.slug)
        if cached is None:
            event = self._oneshot(automation, {"mode": "describe"})
            if event.get("type") != "describe":
                raise RuntimeError(event.get("error", "engine did not respond"))
            cached = event["default_map"]
            self._describe_cache[automation.slug] = cached
        return cached

    def validate_sheet(self, automation, excel_path: Path, column_map: dict) -> dict:
        return self._oneshot(automation, {"mode": "validate", "excel_path": str(excel_path),
                                          "column_map": column_map})

    # ------------------------------------------------------------------ worker

    def _next_queued(self, slug: str):
        with self.ctx.db.session() as conn:
            return conn.execute(
                "SELECT * FROM runs WHERE automation_slug=? AND status='queued'"
                " ORDER BY id LIMIT 1", (slug,)).fetchone()

    def _worker_loop(self, slug: str) -> None:
        while True:
            with self._cv:
                while not self._stopping:
                    run = self._next_queued(slug)
                    if run is not None:
                        break
                    self._cv.wait(timeout=5)
                if self._stopping:
                    return
            try:
                self._execute(run)
            except Exception:                       # never let the loop die
                log.exception("run %s crashed the job manager", run["id"])
                with self.ctx.db.session() as conn:
                    conn.execute("UPDATE runs SET status='failed', finished_at=?, error=?"
                                 " WHERE id=? AND status IN ('queued','running')",
                                 (utcnow(), "Internal error while running - see server log.", run["id"]))

    # ------------------------------------------------------------------ executing a run

    def _execute(self, run) -> None:
        settings = self.ctx.settings
        automation = self.ctx.registry.get(run["automation_slug"])
        run_id = run["id"]
        run_dir = settings.runs_dir / str(run_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        log_path = run_dir / "run.log"

        with self.ctx.db.session() as conn:
            claimed = conn.execute(
                "UPDATE runs SET status='running', started_at=? WHERE id=? AND status='queued'",
                (utcnow(), run_id)).rowcount
            owner = conn.execute("SELECT id, email FROM users WHERE id=?", (run["user_id"],)).fetchone()
        if not claimed:
            return                                   # cancelled while waiting

        if automation is None:
            self._finish(run_id, owner, "failed", error="This automation is no longer installed.")
            return

        options = json.loads(run["options"])
        ws = workspace_for(settings, automation.slug, run["user_id"])
        job = {
            "mode": "run", "engine_dir": str(automation.engine_dir),
            "excel_path": run["sheet_path"], "column_map": options.get("column_map", {}),
            "save": options.get("save", False), "workers": options.get("workers", 1),
            "limit": options.get("limit"), "only": options.get("only"),
            "allow_duplicate": options.get("allow_duplicate", False),
            "no_resume": options.get("no_resume", False),
            "workspace": {k: str(v) for k, v in ws.items()},
            "browser_channel": settings.browser_channel,
            "browser_headless": settings.browser_headless,
        }
        job_file = run_dir / "job.json"
        job_file.write_text(json.dumps(job), encoding="utf-8")

        env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
        finished_event = None
        timed_out = threading.Event()

        with log_path.open("a", encoding="utf-8") as log_file:
            log_file.write(f"[{utcnow()}] Run {run_id} started by {owner['email']} "
                           f"({automation.name}, sheet {run['sheet_name']})\n")
            log_file.flush()
            try:
                proc = subprocess.Popen(
                    [settings.python_bin, str(RUNNER), str(job_file)],
                    cwd=str(automation.engine_dir), stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                    encoding="utf-8", errors="replace", bufsize=1, env=env, **_popen_kwargs())
            except OSError as exc:
                log_file.write(f"[{utcnow()}] Could not start the runner: {exc}\n")
                self._finish(run_id, owner, "failed", error=f"Could not start the runner: {exc}")
                return
            self._procs[run_id] = proc

            def on_timeout():
                timed_out.set()
                kill_tree(proc)
            watchdog = threading.Timer(RUN_TIMEOUT_HOURS * 3600, on_timeout)
            watchdog.daemon = True
            watchdog.start()

            try:
                for line in proc.stdout:
                    if line.startswith(EVENT_PREFIX):
                        try:
                            event = json.loads(line[len(EVENT_PREFIX):])
                        except ValueError:
                            continue
                        if event.get("type") == "rows":
                            self._store_rows(run_id, event["rows"])
                        elif event.get("type") == "finished":
                            finished_event = event
                    else:
                        log_file.write(line if line.endswith("\n") else line + "\n")
                        log_file.flush()
                rc = proc.wait()
            finally:
                watchdog.cancel()
                self._procs.pop(run_id, None)

            with self.ctx.db.session() as conn:
                cancelled = bool(conn.execute("SELECT cancel_requested FROM runs WHERE id=?",
                                              (run_id,)).fetchone()["cancel_requested"])

            if cancelled:
                status, error, summary = "cancelled", "Stopped by request.", None
            elif timed_out.is_set():
                status, error, summary = "failed", f"Stopped after {RUN_TIMEOUT_HOURS:g} hours with no finish.", None
            elif finished_event and finished_event.get("ok") and rc == 0:
                status, error, summary = "succeeded", None, finished_event.get("summary")
            else:
                status = "failed"
                error = (finished_event or {}).get("error") or f"The runner stopped unexpectedly (exit code {rc})."
                summary = None
            log_file.write(f"[{utcnow()}] Run {run_id} {status}"
                           f"{': ' + error if error else ''}\n")

        output = (finished_event or {}).get("status_xlsx")
        self._finish(run_id, owner, status, error=error, summary=summary, output_path=output)

    def _store_rows(self, run_id: int, rows: list) -> None:
        now = utcnow()
        with self.ctx.db.transaction() as conn:
            for r in rows:
                conn.execute(
                    "INSERT INTO run_rows (run_id,row_no,patient_name,dos,status,visit_id,beneficiary_id,"
                    "duplicate_of,error,line_charges,note,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)"
                    " ON CONFLICT(run_id,row_no) DO UPDATE SET patient_name=excluded.patient_name,"
                    " dos=excluded.dos,status=excluded.status,visit_id=excluded.visit_id,"
                    " beneficiary_id=excluded.beneficiary_id,duplicate_of=excluded.duplicate_of,"
                    " error=excluded.error,line_charges=excluded.line_charges,note=excluded.note,"
                    " updated_at=excluded.updated_at",
                    (run_id, r["row"], str(r.get("patient_name", "")), str(r.get("dos", "")),
                     str(r.get("status", "")), str(r.get("visit_id", "")),
                     str(r.get("beneficiary_id", "")), str(r.get("duplicate_of", "")),
                     str(r.get("error", "")), str(r.get("line_charges", "")),
                     str(r.get("note", "")), now))

    def _finish(self, run_id, owner, status, error=None, summary=None, output_path=None) -> None:
        with self.ctx.db.session() as conn:
            conn.execute(
                "UPDATE runs SET status=?, finished_at=?, error=?, summary=?, output_path=? WHERE id=?",
                (status, utcnow(), error, summary, output_path, run_id))
            log_activity(conn, user=owner, action=f"run_{status}", target_type="run",
                         target_id=run_id, details={"error": error, "summary": summary})
