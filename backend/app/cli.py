"""Command line helpers.

    python -m app.cli create-admin you@example.com        (asks for the password)
    python -m app.cli list-users
    python -m app.cli preflight [--skip-network]           check this machine before the first real run
    python -m app.cli check-login <slug> [--yes]           one REAL Office Ally sign-in (nothing is changed there)
"""

import getpass
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from .config import get_settings
from .db import Database, init_db, utcnow
from .deps import Context
from .security import hash_password, normalize_email, password_problem, valid_email
from .services.registry import Registry

log = logging.getLogger("automation.cli")


def _context() -> Context:
    settings = get_settings()
    init_db(settings.db_path)
    return Context(settings=settings, db=Database(settings.db_path), registry=Registry(settings.automations_dir))


def create_admin(ctx: Context, email: str, password: str, enforce_policy: bool = True) -> int:
    """`enforce_policy=False` is for the operator's own bootstrap password in
    the server's .env: the owner of the server decides it, and a weak one must
    never stop the site from starting."""
    email = normalize_email(email)
    if not valid_email(email):
        raise SystemExit("That is not a valid email address.")
    problem = password_problem(password) if enforce_policy else None
    if problem:
        raise SystemExit(problem)
    with ctx.db.transaction() as conn:
        existing = conn.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
        if existing:
            conn.execute("UPDATE users SET role='admin', status='active', password_hash=?,"
                         " failed_attempts=0, locked_until=NULL WHERE id=?",
                         (hash_password(password), existing["id"]))
            user_id = existing["id"]
        else:
            user_id = conn.execute(
                "INSERT INTO users (email, name, password_hash, role, status, created_at, approved_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (email, email.split("@")[0], hash_password(password), "admin", "active",
                 utcnow(), utcnow())).lastrowid
        conn.execute("INSERT INTO activity_logs (user_id, actor_email, action, target_type, target_id, created_at)"
                     " VALUES (?,?,?,?,?,?)", (user_id, email, "admin_created", "user", str(user_id), utcnow()))
    return user_id


def ensure_bootstrap_admin(ctx: Context) -> None:
    """If no admin exists yet and AUTOMATION_ADMIN_EMAIL/_PASSWORD are set, create one."""
    s = ctx.settings
    if not (s.bootstrap_admin_email and s.bootstrap_admin_password):
        return
    with ctx.db.session() as conn:
        has_admin = conn.execute("SELECT 1 FROM users WHERE role='admin' LIMIT 1").fetchone()
    if has_admin:
        return
    try:
        create_admin(ctx, s.bootstrap_admin_email, s.bootstrap_admin_password, enforce_policy=False)
        log.warning("First admin created: %s - remove AUTOMATION_ADMIN_PASSWORD from .env now.",
                    s.bootstrap_admin_email)
    except SystemExit as exc:                       # bad email in .env: report, keep the site up
        log.error("Could not create the first admin from .env: %s", exc)


# ------------------------------------------------------------------ server validation

RUNNER = Path(__file__).resolve().parents[1] / "runner" / "runner.py"
OFFICE_ALLY_URLS = ("https://cms.officeally.com/", "https://pm.officeally.com/pm/")


def _runner(ctx: Context, job: dict, timeout: int = 90) -> dict:
    """Run one runner job (no engine import needed for browser_check) and return its first event."""
    with tempfile.TemporaryDirectory() as tmp:
        job_file = Path(tmp) / "job.json"
        job_file.write_text(json.dumps(job), encoding="utf-8")
        try:
            proc = subprocess.run([ctx.settings.python_bin, str(RUNNER), str(job_file)], capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=timeout, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            return {"type": "error", "error": f"timed out after {timeout}s"}
    for line in proc.stdout.splitlines():
        if line.startswith("@@EVENT "):
            return json.loads(line[8:])
    tail = [l for l in (proc.stderr or proc.stdout).strip().splitlines() if l.strip()][-3:]
    return {"type": "error", "error": " | ".join(tail)[:300] or f"exit code {proc.returncode}"}


def preflight(ctx: Context, check_network: bool = True) -> int:
    """Everything that can be checked WITHOUT touching patient data. Exit code 1 if anything FAILs."""
    s = ctx.settings
    rows: list[tuple[str, str, str]] = []

    def add(status, name, detail=""):
        rows.append((status, name, detail))

    add("PASS" if sys.version_info >= (3, 10) else "FAIL", "Python", sys.version.split()[0])
    for module in ("fastapi", "uvicorn", "openpyxl", "psutil", "bcrypt", "playwright"):
        try:
            __import__(module)
            add("PASS", f"package {module}")
        except ImportError:
            add("FAIL", f"package {module}", "not installed (pip install -r backend/requirements.txt)")

    try:
        s.storage_dir.mkdir(parents=True, exist_ok=True)
        probe = s.storage_dir / ".write-test"
        probe.write_text("ok"); probe.unlink()
        free_gb = shutil.disk_usage(s.storage_dir).free / 1e9
        add("PASS" if free_gb >= 5 else "WARN" if free_gb >= 0.5 else "FAIL", "storage",
            f"{s.storage_dir} writable, {free_gb:.1f} GB free")
    except OSError as exc:
        add("FAIL", "storage", f"{s.storage_dir} is not writable: {exc}")

    with ctx.db.session() as conn:
        has_admin = conn.execute("SELECT 1 FROM users WHERE role='admin' AND status='active' LIMIT 1").fetchone()
    add("PASS" if has_admin else "WARN", "admin account", "exists" if has_admin else "none yet (set AUTOMATION_ADMIN_EMAIL/PASSWORD or run create-admin)")

    add("PASS", "limits", f"{s.max_concurrent_runs} runs at once, {s.max_total_workers} browser windows total, "
        f"{s.max_workers_per_run} per run, timeout {s.run_timeout_hours:g}h, auto-retries {s.auto_retries}")
    add("PASS", "retention", f"files {s.retention_files_days or 'kept'} days, row detail {s.retention_rows_days or 'kept'} days")

    for problem in ctx.registry.errors:
        add("FAIL", "automation manifest", problem)
    for automation in ctx.registry.all():
        name = f"automation {automation.slug}"
        env = automation.engine_dir / ".env"
        from .services import credentials as creds
        missing = [f["key"] for f in creds.read_status(automation)["fields"] if not f["is_set"]]
        add("FAIL" if missing else "PASS", f"{name} login", f"missing: {', '.join(missing)}" if missing else "username and password are saved")
        if os.name != "nt" and env.exists() and (env.stat().st_mode & 0o077):
            add("WARN", f"{name} .env permissions", "readable by other accounts; run: chmod 600 " + str(env))
        event = _runner(ctx, {"mode": "describe", "engine_dir": str(automation.engine_dir)})
        add("PASS" if event.get("type") == "describe" else "FAIL", f"{name} engine loads", event.get("error", "ok"))

    if os.name != "nt" and not s.browser_headless and not os.environ.get("DISPLAY"):
        add("FAIL", "display", "a visible browser needs a display but DISPLAY is not set: use AUTOMATION_HEADLESS=true or a virtual display (xvfb)")
    event = _runner(ctx, {"mode": "browser_check", "browser_channel": s.browser_channel, "browser_headless": s.browser_headless,
                              "browser_window_size": s.window_size}, timeout=120)
    add("PASS" if event.get("ok") else "FAIL",
        f"browser '{s.browser_channel}' headless={s.browser_headless}",
        f"started, version {event.get('version')}" if event.get("ok") else event.get("error", "could not start") + "  (python -m playwright install " + s.browser_channel + ")")

    if check_network:
        for url in OFFICE_ALLY_URLS:
            started = time.time()
            try:
                with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "preflight"}), timeout=20) as response:
                    add("PASS", f"network {url}", f"HTTP {response.status} in {time.time() - started:.1f}s")
            except urllib.error.HTTPError as exc:
                add("PASS" if exc.code < 500 else "WARN", f"network {url}", f"HTTP {exc.code} (the site answered)")
            except Exception as exc:
                add("FAIL", f"network {url}", f"{type(exc).__name__}: {exc} - from a blocked region this fails; the server should be in the US")

    width = max(len(name) for _, name, _ in rows)
    for status, name, detail in rows:
        print(f"{status:4}  {name:<{width}}  {detail}")
    failures = sum(1 for status, _, _ in rows if status == "FAIL")
    print(f"\n{failures} failed, {sum(1 for r in rows if r[0] == 'WARN')} warnings, {sum(1 for r in rows if r[0] == 'PASS')} passed")
    print("This checks the machine only. It does not prove Office Ally processing works: run check-login, then a small real sheet.")
    return 1 if failures else 0


def check_login(ctx: Context, slug: str, assume_yes: bool) -> int:
    automation = ctx.registry.get(slug)
    if automation is None:
        print(f"Unknown automation {slug!r}. Known: {', '.join(ctx.registry.slugs())}")
        return 2
    if not assume_yes:
        answer = input(f"This signs in to Office Ally for real with the saved login of '{slug}' (nothing is created or changed). Continue? [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            print("Cancelled.")
            return 2
    from .services.jobs import JobManager
    result = JobManager(ctx).check_login(automation)
    if result.get("type") == "login_check" and result.get("ok"):
        print(f"PASS  signed in to Office Ally as the '{slug}' login ({result.get('url')})")
        return 0
    if result.get("type") != "login_check":
        print(f"FAIL  the test could not run: {result.get('error')}")
        return 1
    print("FAIL  the sign-in did not succeed." + (f" Error: {result['error']}" if result.get("error") else " Office Ally did not accept the login."))
    for page in result.get("pages", []):
        print("\n  page    :", page.get("url"))
        print("  title   :", page.get("title"))
        print("  window  :", page.get("window"), "  webdriver:", page.get("webdriver"))
        print("  browser :", page.get("user_agent"))
        print("  text    :", page.get("text"))
        if page.get("screenshot"):
            print("  picture :", page["screenshot"], " (copy out with: docker compose cp app:" + page["screenshot"] + " .)")
        if page.get("error"):
            print("  (could not inspect this page:", page["error"] + ")")
    return 1

def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) >= 2 and argv[0] == "create-admin":
        password = getpass.getpass("Password: ")
        if password != getpass.getpass("Repeat password: "):
            raise SystemExit("Passwords do not match.")
        user_id = create_admin(_context(), argv[1], password)
        print(f"Admin ready: {normalize_email(argv[1])} (id {user_id})")
        return 0
    if argv and argv[0] == "preflight":
        return preflight(_context(), check_network="--skip-network" not in argv)
    if len(argv) >= 2 and argv[0] == "check-login":
        return check_login(_context(), argv[1], assume_yes="--yes" in argv)
    if argv == ["list-users"]:
        ctx = _context()
        with ctx.db.session() as conn:
            for r in conn.execute("SELECT id,email,role,status,last_login_at FROM users ORDER BY id"):
                print(f"{r['id']:>3}  {r['email']:<34} {r['role']:<6} {r['status']:<9} {r['last_login_at'] or '-'}")
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main())
