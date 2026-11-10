"""Scheduler and supervisor for automation runs.

Every run is a separate OS process (backend/runner/runner.py). This module decides WHEN a
queued run may start and supervises it until it ends.

Scheduling rules
  * Global caps: at most `max_concurrent_runs` runs and `max_total_workers` browser windows at once.
  * Per automation: `max_concurrent_runs` from the manifest (default 1). Each automation signs in
    to ONE Office Ally account and two windows on one login can hang, so this stays 1 until
    separate accounts/sessions have been verified.
  * Fair queue: among runs that may start, the person with the fewest active runs goes first, then
    the person who started a run least recently, then the oldest run. A run that cannot start
    (its automation is busy, no capacity) never blocks a different automation behind it.

Life of a run
  queued -> validating -> running -> completed | completed_with_errors | failed | cancelled
  (interrupted = the server stopped while it was in progress)
  A run only becomes `completed` after its result sheet has been read back and matched against
  the rows the database recorded.

Isolation: each run has its own folder (private copy of the sheet, output, saved login, logs).
Only the per-user "resume store" (a copy of the latest status sheet, keyed by the sheet's content
hash) carries progress from one run to the next, so a retry skips rows that are already Done.
"""

import hashlib
import json
import logging
import os
import shutil
import subprocess
import threading
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..db import utcnow
from .audit import log_activity
from .procs import ProcessTracker
from .redact import read_env_secrets, redact
from .verify import verify_output

log = logging.getLogger("automation.jobs")

RUNNER = Path(__file__).resolve().parents[2] / "runner" / "runner.py"
EVENT_PREFIX = "@@EVENT "
ONESHOT_TIMEOUT = 90                      # seconds for describe / validate
HOLD = "9999-12-31T00:00:00Z"             # not_before value while a run is still being prepared
ACTIVE_STATES = ("validating", "running")
OPEN_STATES = ("queued", "validating", "running")
RETRYABLE = ("failed", "interrupted", "completed_with_errors", "cancelled")


# ------------------------------------------------------------------ paths

def user_area(settings, slug: str, user_id: int) -> Path:
    return settings.uploads_dir / slug / f"user-{user_id}"


def staging_dir(settings, slug: str, user_id: int) -> Path:
    """Where an uploaded sheet waits until a run takes its own copy."""
    return user_area(settings, slug, user_id) / "uploads"


def resume_store(settings, slug: str, user_id: int, sheet_sha: str, sheet_name: str) -> Path:
    """Latest status sheet for THIS sheet's content (hash), so a different file that happens to
    share a name can never inherit another sheet's progress."""
    stem = Path(sheet_name).stem
    return user_area(settings, slug, user_id) / "resume" / sheet_sha[:16] / f"{stem}_with_status.xlsx"


def run_dir(settings, run_id: int) -> Path:
    return settings.runs_dir / str(run_id)


def run_paths(settings, run_id: int) -> dict:
    base = run_dir(settings, run_id)
    return {"base": base, "input_dir": base / "input", "output_dir": base / "output",
            "state_path": base / "auth_state.json", "log_path": base / "run.log", "job_file": base / "job.json"}


def sheet_fingerprint(path: Path) -> str:
    """Identity of a sheet's CONTENT (cell values and their row numbers), not its bytes: saving the same sheet
    again in Excel changes the file but not this, while any different sheet gets a different value."""
    import openpyxl
    digest = hashlib.sha256()
    workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        sheet = workbook[workbook.sheetnames[0]]
        for number, row in enumerate(sheet.iter_rows(values_only=True), start=1):
            values = list(row)
            while values and values[-1] in (None, ""):
                values.pop()                         # trailing empty cells and ghost rows do not count
            if values:
                digest.update(f"{number}:{values!r}\n".encode("utf-8"))
    finally:
        workbook.close()
    return digest.hexdigest()


def _iso_in(seconds: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _popen_kwargs() -> dict:
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def _atomic_copy(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".tmp")
    shutil.copyfile(src, tmp)
    os.replace(tmp, dst)


# ------------------------------------------------------------------ per-run log

class RunLog:
    """Timestamped, redacted, size-capped log file for one run."""

    def __init__(self, path: Path, max_mb: int, secrets, engine_output: bool):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._file = path.open("a", encoding="utf-8")
        self._cap = max_mb * 1024 * 1024 if max_mb else 0
        self._written = path.stat().st_size if path.exists() else 0
        self._secrets = secrets
        self._engine_output = engine_output
        self._truncated = False
        self._lock = threading.Lock()
        self.tail: list[str] = []

    def write(self, level: str, source: str, message: str) -> None:
        message = redact(message.rstrip("\n"), self._secrets)[:2000]
        line = f"{utcnow()} {level.upper():5} {source:6} | {message}\n"
        with self._lock:
            if self._cap and self._written + len(line) > self._cap:
                if not self._truncated:
                    self._truncated = True
                    self._file.write(f"{utcnow()} WARN  server | log size limit reached, further output is not stored\n")
                    self._file.flush()
                return
            self._file.write(line)
            self._file.flush()
            self._written += len(line)

    def engine(self, line: str) -> None:
        if not self._engine_output:
            return
        text = line.rstrip("\n")
        if not text.strip():
            return
        lowered = text.lower()
        level = "error" if ("traceback" in lowered or "error" in lowered or "exception" in lowered) else "info"
        self.write(level, "engine", text)
        self.tail = (self.tail + [redact(text, self._secrets)[:300]])[-15:]

    def close(self) -> None:
        with self._lock:
            self._file.close()


# ------------------------------------------------------------------ manager

class JobManager:
    def __init__(self, ctx):
        self.ctx = ctx
        self._cv = threading.Condition()
        self._active: dict[int, dict] = {}            # run_id -> {slug,user_id,workers,tracker}
        self._last_start: dict[int, float] = {}       # user_id -> monotonic time of their last start
        self._scheduler: threading.Thread | None = None
        self._retention: threading.Thread | None = None
        self._stopping = False
        self._describe_cache: dict[str, dict] = {}

    # -------------------------------------------------------------- lifecycle

    def start(self) -> None:
        self._recover()
        self._stopping = False
        self._scheduler = threading.Thread(target=self._scheduler_loop, name="scheduler", daemon=True)
        self._scheduler.start()
        s = self.ctx.settings
        if s.retention_files_days or s.retention_rows_days:
            self._retention = threading.Thread(target=self._retention_loop, name="retention", daemon=True)
            self._retention.start()

    def stop(self) -> None:
        with self._cv:
            self._stopping = True
            self._cv.notify_all()
            trackers = [a["tracker"] for a in self._active.values() if a.get("tracker")]
        for tracker in trackers:
            tracker.kill_all(grace=3)

    def _recover(self) -> None:
        """After a restart: runs that were in progress did not finish - say so accurately - and
        runs still waiting simply carry on."""
        now = utcnow()
        with self.ctx.db.transaction() as conn:
            stuck = conn.execute("SELECT id FROM runs WHERE status IN ('validating','running')").fetchall()
            for row in stuck:
                conn.execute(
                    "UPDATE runs SET status='interrupted', failure_kind='interrupted', finished_at=?, error=? WHERE id=?",
                    (now, "The server stopped while this run was in progress. Retry it: rows already done are skipped.",
                     row["id"]))
                conn.execute("INSERT INTO run_events (run_id, ts, level, kind, message) VALUES (?,?,?,?,?)",
                             (row["id"], now, "warn", "interrupted", "Server restarted while the run was in progress"))
            # a run that was still being prepared when the server went down can never start
            conn.execute("UPDATE runs SET status='failed', failure_kind='interrupted', finished_at=?, error=?"
                         " WHERE status='queued' AND not_before=?",
                         (now, "The server stopped while this run was being prepared. Start it again.", HOLD))

    # -------------------------------------------------------------- public API

    def submit(self, run_id: int, slug: str = "") -> None:
        """Called once a run is fully prepared (its sheet copy exists)."""
        with self.ctx.db.session() as conn:
            conn.execute("UPDATE runs SET not_before=NULL WHERE id=? AND not_before=?", (run_id, HOLD))
        with self._cv:
            self._cv.notify_all()

    def cancel(self, run_id: int) -> str:
        with self.ctx.db.session() as conn:
            row = conn.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()
            if not row:
                return "missing"
            if row["status"] == "queued":
                changed = conn.execute(
                    "UPDATE runs SET status='cancelled', finished_at=?, cancel_requested=1, error=? WHERE id=? AND status='queued'",
                    (utcnow(), "Cancelled before it started", run_id)).rowcount
                if changed:
                    self._event(run_id, "info", "cancelled", "Cancelled while waiting in the queue", conn=conn)
                    return "cancelled"
            conn.execute("UPDATE runs SET cancel_requested=1 WHERE id=? AND status IN ('validating','running')", (run_id,))
        self._event(run_id, "info", "cancel_requested", "Stop requested")
        with self._cv:
            tracker = (self._active.get(run_id) or {}).get("tracker")
        if tracker is not None:
            survivors = tracker.kill_all(grace=5)
            if survivors:
                self._event(run_id, "error", "cleanup", f"{len(survivors)} process(es) did not stop", {"pids": survivors})
        return "cancelling"

    def queue_position(self, run_id: int, slug: str) -> int:
        with self.ctx.db.session() as conn:
            return conn.execute(
                "SELECT COUNT(*) AS n FROM runs WHERE automation_slug=? AND status IN ('queued','validating','running') AND id<?",
                (slug, run_id)).fetchone()["n"]

    def slug_busy(self, slug: str) -> bool:
        with self.ctx.db.session() as conn:
            return bool(conn.execute(
                "SELECT 1 FROM runs WHERE automation_slug=? AND status IN ('validating','running') LIMIT 1", (slug,)).fetchone())

    def create_retry(self, run_id: int, user, manual: bool = True, delay: float = 0.0) -> int:
        """Queue a new run for the same sheet. Safe by construction: duplicates are never allowed and
        progress is always resumed, so rows that are already Done are skipped, and a visit that may
        already exist in Office Ally is found by the engine's own duplicate check."""
        s = self.ctx.settings
        with self.ctx.db.transaction() as conn:
            old = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if old is None:
                raise LookupError("Run not found.")
            if old["status"] not in RETRYABLE:
                raise ValueError("Only a failed, stopped, interrupted or incomplete run can be retried.")
            if old["files_purged_at"]:
                raise ValueError("This run's files were removed by the retention policy. Upload the sheet again.")
            busy = conn.execute(
                "SELECT id FROM runs WHERE user_id=? AND automation_slug=? AND sheet_name=? AND status IN ('queued','validating','running') LIMIT 1",
                (old["user_id"], old["automation_slug"], old["sheet_name"])).fetchone()
            if busy:
                raise ValueError(f"This sheet is already queued or running (run #{busy['id']}).")
            src_input = run_paths(s, run_id)["input_dir"]
            if not (src_input / old["sheet_name"]).exists():
                raise ValueError("The original sheet is no longer available. Upload it again.")
            options = json.loads(old["options"])
            options.update({"allow_duplicate": False, "no_resume": False})       # the safety rules of a retry
            cur = conn.execute(
                "INSERT INTO runs (automation_slug, user_id, status, sheet_name, sheet_path, options, total, queued_at,"
                " retry_of, attempt, not_before) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (old["automation_slug"], old["user_id"], "queued", old["sheet_name"], "", json.dumps(options),
                 old["total"], utcnow(), run_id, old["attempt"] + 1, HOLD))
            new_id = cur.lastrowid
            dst = run_paths(s, new_id)["input_dir"]
            dst.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src_input / old["sheet_name"], dst / old["sheet_name"])
            conn.execute("UPDATE runs SET sheet_path=?, not_before=? WHERE id=?",
                         (str(dst / old["sheet_name"]), _iso_in(delay) if delay else None, new_id))
            owner = conn.execute("SELECT id, email FROM users WHERE id=?", (old["user_id"],)).fetchone()
            who = user if (manual and user is not None) else owner
            log_activity(conn, user=who, action="run_retry" if manual else "run_auto_retry", target_type="run",
                         target_id=new_id, details={"retry_of": run_id, "attempt": old["attempt"] + 1})
            conn.execute("INSERT INTO run_events (run_id, ts, level, kind, message) VALUES (?,?,?,?,?)",
                         (new_id, utcnow(), "info", "queued",
                          f"Retry of run #{run_id} (attempt {old['attempt'] + 1}); finished rows are skipped"
                          + (f", starts in about {int(delay)}s" if delay else "")))
            conn.execute("INSERT INTO run_events (run_id, ts, level, kind, message) VALUES (?,?,?,?,?)",
                         (run_id, utcnow(), "info", "retry_queued", f"Retry queued as run #{new_id}"))
        with self._cv:
            self._cv.notify_all()
        return new_id

    # -------------------------------------------------------------- one-shot inspection calls

    def _oneshot(self, automation, job: dict, timeout: int = ONESHOT_TIMEOUT) -> dict:
        job_dir = self.ctx.settings.runs_dir / "_oneshot"
        job_dir.mkdir(parents=True, exist_ok=True)
        job_file = job_dir / f"{os.getpid()}-{threading.get_ident()}-{time.time_ns()}.json"
        job_file.write_text(json.dumps({**job, "engine_dir": str(automation.engine_dir)}), encoding="utf-8")
        try:
            proc = subprocess.run(
                [self.ctx.settings.python_bin, str(RUNNER), str(job_file)],
                cwd=str(automation.engine_dir), capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=timeout, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            return {"type": "error", "error": "Timed out."}
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
        return self._oneshot(automation, {"mode": "validate", "excel_path": str(excel_path), "column_map": column_map})

    def check_login(self, automation) -> dict:
        """A REAL Office Ally sign-in with the automation's saved credentials. Changes nothing."""
        s = self.ctx.settings
        state = s.runs_dir / "_oneshot" / f"login-{time.time_ns()}.json"
        return self._oneshot(automation, {"mode": "login_check", "state_path": str(state),
                                          "browser_channel": s.browser_channel, "browser_headless": s.browser_headless},
                             timeout=180)

    # -------------------------------------------------------------- scheduler

    def _pick_next(self):
        """The queued run that may start now, or None. Caller holds self._cv."""
        s = self.ctx.settings
        if len(self._active) >= max(1, s.max_concurrent_runs):
            return None
        with self.ctx.db.session() as conn:
            queued = conn.execute(
                "SELECT * FROM runs WHERE status='queued' AND (not_before IS NULL OR not_before<=?) ORDER BY id",
                (utcnow(),)).fetchall()
        if not queued:
            return None
        per_slug = Counter(a["slug"] for a in self._active.values())
        per_user = Counter(a["user_id"] for a in self._active.values())
        used_workers = sum(a["workers"] for a in self._active.values())
        eligible = []
        for run in queued:
            automation = self.ctx.registry.get(run["automation_slug"])
            limit = automation.max_concurrent_runs if automation else 1
            if per_slug[run["automation_slug"]] >= limit:
                continue                                   # that automation's login is busy; others may still go
            workers = int(json.loads(run["options"]).get("workers") or 1)
            if self._active and used_workers + workers > s.max_total_workers:
                continue                                   # not enough browser capacity right now
            eligible.append((run, workers))
        if not eligible:
            return None
        run, workers = min(eligible, key=lambda item: (per_user[item[0]["user_id"]],
                                                       self._last_start.get(item[0]["user_id"], 0.0), item[0]["id"]))
        self._active[run["id"]] = {"slug": run["automation_slug"], "user_id": run["user_id"],
                                   "workers": workers, "tracker": None}
        self._last_start[run["user_id"]] = time.monotonic()
        return run

    def _scheduler_loop(self) -> None:
        while True:
            with self._cv:
                if self._stopping:
                    return
                run = self._pick_next()
                if run is None:
                    self._cv.wait(timeout=1.0)
                    continue
            threading.Thread(target=self._run_thread, args=(run,), name=f"run-{run['id']}", daemon=True).start()

    def _run_thread(self, run) -> None:
        try:
            self._execute(run)
        except Exception:                                  # a bug here must never leave a run stuck
            log.exception("run %s crashed the job manager", run["id"])
            self._finish(run["id"], "failed", failure_kind="engine_error",
                         error="Internal error while running - see the server log.")
        finally:
            with self._cv:
                self._active.pop(run["id"], None)
                self._cv.notify_all()

    # -------------------------------------------------------------- events / state

    def _event(self, run_id, level, kind, message, details=None, conn=None) -> None:
        sql = "INSERT INTO run_events (run_id, ts, level, kind, message, details) VALUES (?,?,?,?,?,?)"
        args = (run_id, utcnow(), level, kind, message[:500], json.dumps(details, default=str) if details else None)
        try:
            if conn is not None:
                conn.execute(sql, args)
            else:
                with self.ctx.db.session() as c:
                    c.execute(sql, args)
        except Exception:                                  # a timeline write must not break a run
            log.exception("could not write run event")

    def _finish(self, run_id, status, *, error=None, summary=None, failure_kind=None, output_path=None,
                carried=None, details=None) -> None:
        with self.ctx.db.session() as conn:
            changed = conn.execute(
                "UPDATE runs SET status=?, finished_at=?, error=?, summary=?, failure_kind=?, output_path=COALESCE(?, output_path),"
                " carried_over=COALESCE(?, carried_over) WHERE id=? AND status IN ('queued','validating','running')",
                (status, utcnow(), error, summary, failure_kind, output_path, carried, run_id)).rowcount
            if not changed:
                return
            owner = conn.execute("SELECT u.id, u.email FROM users u JOIN runs r ON r.user_id=u.id WHERE r.id=?", (run_id,)).fetchone()
            self._event(run_id, "info" if status in ("completed", "cancelled") else "warn" if status == "completed_with_errors" else "error",
                        status, error or summary or status, details, conn=conn)
            log_activity(conn, user=owner, action=f"run_{status}", target_type="run", target_id=run_id,
                         details={"error": error, "summary": summary, "kind": failure_kind})

    def _cancel_requested(self, run_id) -> bool:
        with self.ctx.db.session() as conn:
            return bool(conn.execute("SELECT cancel_requested FROM runs WHERE id=?", (run_id,)).fetchone()["cancel_requested"])

    # -------------------------------------------------------------- the run itself

    def _preflight(self, automation, run, secrets) -> tuple[str | None, str | None]:
        """(failure_kind, message) when the run must not start, else (None, None)."""
        sheet = Path(run["sheet_path"])
        if not sheet.exists():
            return "invalid_input", "The sheet for this run is missing. Upload it again."
        if not secrets:
            return "config_error", "This automation has no Office Ally login saved. An administrator must set it."
        options = json.loads(run["options"])
        check = self.validate_sheet(automation, sheet, options.get("column_map", {}))
        if not check.get("ok"):
            return "invalid_input", check.get("error", "The sheet could not be read.")
        with self.ctx.db.session() as conn:
            conn.execute("UPDATE runs SET total=? WHERE id=?", (check["total"], run["id"]))
        return None, None

    def _execute(self, run) -> None:
        s = self.ctx.settings
        run_id = run["id"]
        automation = self.ctx.registry.get(run["automation_slug"])
        paths = run_paths(s, run_id)
        paths["base"].mkdir(parents=True, exist_ok=True)

        with self.ctx.db.session() as conn:
            claimed = conn.execute("UPDATE runs SET status='validating' WHERE id=? AND status='queued'", (run_id,)).rowcount
        if not claimed:
            return                                          # cancelled while waiting
        if automation is None:
            self._finish(run_id, "failed", failure_kind="config_error", error="This automation is no longer installed.")
            return
        self._event(run_id, "info", "validating", "Checking the sheet and the automation")

        secrets = read_env_secrets(automation.engine_dir / ".env")
        runlog = RunLog(paths["log_path"], s.log_max_mb, secrets, engine_output=(s.engine_log != "off"))
        try:
            options = json.loads(run["options"])
            runlog.write("info", "server", f"Run {run_id} for {automation.name}, sheet {run['sheet_name']}, attempt {run['attempt']}")

            kind, message = self._preflight(automation, run, secrets)
            if kind:
                runlog.write("error", "server", message)
                self._finish(run_id, "failed", failure_kind=kind, error=message)
                return
            if self._cancel_requested(run_id):
                self._finish(run_id, "cancelled", error="Stopped by request.")
                return

            # --- private output folder, seeded with earlier progress for this exact sheet
            paths["output_dir"].mkdir(parents=True, exist_ok=True)
            sheet_sha = options.get("sheet_sha256") or sheet_fingerprint(Path(run["sheet_path"]))
            resume_file = resume_store(s, run["automation_slug"], run["user_id"], sheet_sha, run["sheet_name"])
            if not options.get("no_resume") and resume_file.exists():
                shutil.copyfile(resume_file, paths["output_dir"] / resume_file.name)
                self._event(run_id, "info", "resume", "Continuing from earlier progress on this sheet")

            job = {
                "mode": "run", "engine_dir": str(automation.engine_dir), "excel_path": run["sheet_path"],
                "column_map": options.get("column_map", {}), "save": options.get("save", False),
                "workers": options.get("workers", 1), "limit": options.get("limit"), "only": options.get("only"),
                "allow_duplicate": options.get("allow_duplicate", False), "no_resume": options.get("no_resume", False),
                "workspace": {"data_dir": str(paths["input_dir"]), "output_dir": str(paths["output_dir"]),
                              "state_path": str(paths["state_path"])},
                "browser_channel": s.browser_channel, "browser_headless": s.browser_headless,
            }
            paths["job_file"].write_text(json.dumps(job), encoding="utf-8")

            with self.ctx.db.session() as conn:
                conn.execute("UPDATE runs SET status='running', started_at=? WHERE id=? AND status='validating'", (utcnow(), run_id))
            self._event(run_id, "info", "started", f"Started with {job['workers']} browser window(s)")
            self._supervise(run, automation, job, paths, runlog, options, resume_file)
        finally:
            runlog.close()
            paths["state_path"].unlink(missing_ok=True)     # the saved login holds cookies: never keep it

    def _supervise(self, run, automation, job, paths, runlog, options, resume_file) -> None:
        s = self.ctx.settings
        run_id = run["id"]
        env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
        try:
            proc = subprocess.Popen(
                [s.python_bin, str(RUNNER), str(paths["job_file"])], cwd=str(automation.engine_dir),
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                encoding="utf-8", errors="replace", bufsize=1, env=env, **_popen_kwargs())
        except OSError as exc:
            runlog.write("error", "server", f"Could not start the runner: {exc}")
            self._finish(run_id, "failed", failure_kind="engine_error", error=f"Could not start the runner: {exc}")
            return

        tracker = ProcessTracker(proc.pid)
        tracker.start_monitor()
        with self._cv:
            if run_id in self._active:
                self._active[run_id]["tracker"] = tracker
        if self._cancel_requested(run_id):                  # stop arrived while the process was starting
            tracker.kill_all(grace=3)

        timed_out = threading.Event()

        def on_timeout():
            timed_out.set()
            tracker.kill_all(grace=3)
        watchdog = threading.Timer(s.run_timeout_hours * 3600, on_timeout)
        watchdog.daemon = True
        watchdog.start()

        finished = None
        carried = 0
        try:
            for line in proc.stdout:
                if line.startswith(EVENT_PREFIX):
                    try:
                        event = json.loads(line[len(EVENT_PREFIX):])
                    except ValueError:
                        continue
                    kind = event.get("type")
                    if kind == "rows":
                        self._store_rows(run_id, event["rows"])
                    elif kind == "resume":
                        carried = int(event.get("carried") or 0)
                        with self.ctx.db.session() as conn:
                            conn.execute("UPDATE runs SET carried_over=? WHERE id=?", (carried, run_id))
                    elif kind == "finished":
                        finished = event
                else:
                    runlog.engine(line)
            rc = proc.wait()
        finally:
            watchdog.cancel()
            survivors = tracker.kill_all(grace=3)           # no browser may outlive its run
        if survivors:
            self._event(run_id, "error", "cleanup", f"{len(survivors)} process(es) could not be stopped", {"pids": survivors})
            runlog.write("error", "server", f"{len(survivors)} leftover process(es) could not be stopped: {survivors}")

        # --- decide the outcome
        cancelled = self._cancel_requested(run_id)
        status_file = Path(finished["status_xlsx"]) if finished and finished.get("status_xlsx") else None
        self._promote_progress(run_id, status_file or (paths["output_dir"] / resume_file.name), resume_file, runlog)

        details = {"exit_code": rc, "last_output": runlog.tail} if s.engine_log != "off" else {"exit_code": rc}
        if cancelled:
            self._finish(run_id, "cancelled", error="Stopped by request.", carried=carried,
                         output_path=str(status_file) if status_file else None)
            return
        if timed_out.is_set():
            self._finish(run_id, "failed", failure_kind="timeout", carried=carried, details=details,
                         error=f"Stopped after {s.run_timeout_hours:g} hours without finishing.")
            return
        if self._stopping:
            self._finish(run_id, "interrupted", failure_kind="interrupted", carried=carried,
                         error="The server was stopped while this run was in progress. Retry it: rows already done are skipped.")
            return
        if finished and finished.get("ok") and rc == 0:
            self._complete(run, finished, options, carried, runlog, status_file)
            return

        error = (finished or {}).get("error") or f"The runner stopped unexpectedly (exit code {rc})."
        failure_kind = self._classify(finished, error)
        runlog.write("error", "server", error)
        self._finish(run_id, "failed", failure_kind=failure_kind, error=error, carried=carried, details=details,
                     output_path=str(status_file) if status_file else None)
        self._maybe_auto_retry(run, failure_kind)

    @staticmethod
    def _classify(finished, error: str) -> str:
        """crash = the process died without a verdict (worth one automatic retry); everything else is
        a permanent problem that a retry would only repeat."""
        if finished is None:
            return "crash"
        text = error.lower()
        if "could not log in" in text or "login" in text and "password" in text:
            return "login_failed"
        if text.startswith(("column ", "could not read", "missing ")) or "not found. columns are" in text:
            return "invalid_input"
        return "engine_error"

    def _complete(self, run, finished, options, carried, runlog, status_file) -> None:
        """The runner says it finished. Believe it only after the result sheet agrees with the database."""
        run_id = run["id"]
        with self.ctx.db.session() as conn:
            rows = [(r["row_no"], r["status"]) for r in conn.execute("SELECT row_no, status FROM run_rows WHERE run_id=?", (run_id,))]
        problems = verify_output(status_file, finished.get("status_column", "Status"), rows, finished.get("done")) \
            if status_file else ["the runner reported no result sheet"]
        if problems:
            message = "Result could not be verified: " + "; ".join(problems)
            runlog.write("error", "server", message)
            self._finish(run_id, "failed", failure_kind="verification_failed", error=message, carried=carried,
                         output_path=str(status_file) if status_file else None)
            return

        not_created = sum(1 for _, st in rows if st == "Not Created")
        planned_known = not options.get("limit") and not options.get("only")
        unreported = max(0, finished.get("total", 0) - carried - len(rows)) if planned_known else 0
        summary = finished.get("summary") or f"{finished.get('done')} of {finished.get('total')} rows done"
        if not_created or unreported:
            bits = []
            if not_created:
                bits.append(f"{not_created} row(s) not created")
            if unreported:
                bits.append(f"{unreported} row(s) reported no result")
            self._finish(run_id, "completed_with_errors", summary=f"{summary}; " + ", ".join(bits), carried=carried,
                         output_path=str(status_file))
        else:
            self._finish(run_id, "completed", summary=summary, carried=carried, output_path=str(status_file))

    def _promote_progress(self, run_id, status_file: Path, resume_file: Path, runlog) -> None:
        """Keep the latest readable status sheet as this sheet's progress, even after a failure or a stop:
        those rows really were saved in Office Ally, and a retry must not process them again."""
        try:
            if not status_file.exists():
                return
            import openpyxl
            openpyxl.load_workbook(status_file, read_only=True).close()     # a half-written file must not replace good progress
            _atomic_copy(status_file, resume_file)
        except Exception as exc:
            runlog.write("warn", "server", f"Progress for this sheet was not saved: {type(exc).__name__}")
            self._event(run_id, "warn", "progress", "Could not save progress for resume (result sheet unreadable)")

    def _maybe_auto_retry(self, run, failure_kind: str) -> None:
        s = self.ctx.settings
        if failure_kind != "crash" or self._stopping or run["attempt"] > s.auto_retries:
            return
        delay = s.retry_backoff_seconds * (2 ** (run["attempt"] - 1))
        try:
            new_id = self.create_retry(run["id"], None, manual=False, delay=delay)
            self._event(run["id"], "info", "auto_retry", f"Crashed; retry queued as run #{new_id} in about {int(delay)}s")
        except (ValueError, LookupError) as exc:
            self._event(run["id"], "warn", "auto_retry", f"No automatic retry: {exc}")

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

    # -------------------------------------------------------------- retention

    def _retention_loop(self) -> None:
        from .retention import purge
        while not self._stopping:
            try:
                purge(self.ctx)
            except Exception:
                log.exception("retention purge failed")
            with self._cv:
                self._cv.wait(timeout=3600)
