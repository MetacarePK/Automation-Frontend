"""Settings, read once from environment variables.

Everything has a safe default so `uvicorn app.main:app` works on a laptop;
on the server set these in the service environment (see deploy/README.md).
"""

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[2]
ENV_FILE = PROJECT_DIR / "backend" / ".env"


def load_env_file(path: Path = ENV_FILE) -> None:
    """Read KEY=VALUE lines from backend/.env into the environment, so the
    server picks up its settings (including the first admin account) no matter
    how it is launched - start.bat, a Windows service, systemd or a terminal.
    Real environment variables always win over the file."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if value[:1] in "\"'" and len(value) >= 2 and value[0] in value[1:]:
            value = value[1:value.index(value[0], 1)]               # quoted: everything inside, '#' included
        else:
            for marker in (" #", "\t#"):                            # unquoted: " # comment" ends the value
                if marker in value:
                    value = value.split(marker, 1)[0].rstrip()
        os.environ.setdefault(key.strip(), value)


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        raise SystemExit(f"{name} must be a whole number, but it is {raw!r}. Fix it in backend/.env")


@dataclass
class Settings:
    # Where the database, uploaded sheets, outputs and run logs live.
    storage_dir: Path = field(default_factory=lambda: Path(
        os.environ.get("AUTOMATION_STORAGE_DIR", PROJECT_DIR / "storage")))
    # One folder per automation: engine code + manifest.json.
    automations_dir: Path = field(default_factory=lambda: Path(
        os.environ.get("AUTOMATION_AUTOMATIONS_DIR", PROJECT_DIR / "automations")))
    frontend_dir: Path = field(default_factory=lambda: Path(
        os.environ.get("AUTOMATION_FRONTEND_DIR", PROJECT_DIR / "frontend")))

    # Cookies: set to true once the site is served over HTTPS (the domain).
    cookie_secure: bool = field(default_factory=lambda: _bool("AUTOMATION_COOKIE_SECURE", False))
    session_hours: int = field(default_factory=lambda: _int("AUTOMATION_SESSION_HOURS", 12))
    # Extra origins allowed to make state-changing requests (comma separated),
    # e.g. "https://automation.example.com". The request's own host is always allowed.
    allowed_origins: tuple = field(default_factory=lambda: tuple(
        o.strip().rstrip("/") for o in os.environ.get("AUTOMATION_ALLOWED_ORIGINS", "").split(",") if o.strip()))

    # Brute-force protection.
    max_failed_logins: int = field(default_factory=lambda: _int("AUTOMATION_MAX_FAILED_LOGINS", 5))
    lockout_minutes: int = field(default_factory=lambda: _int("AUTOMATION_LOCKOUT_MINUTES", 15))
    # Signups wait for an admin unless this is turned on.
    auto_approve_signups: bool = field(default_factory=lambda: _bool("AUTOMATION_AUTO_APPROVE", False))
    # Trust X-Forwarded-For from the reverse proxy (Caddy/nginx) for client IPs.
    trust_proxy: bool = field(default_factory=lambda: _bool("AUTOMATION_TRUST_PROXY", False))

    # Scheduling: how much the server may do at once, and how retries behave.
    max_concurrent_runs: int = field(default_factory=lambda: _int("AUTOMATION_MAX_CONCURRENT_RUNS", 2))
    max_total_workers: int = field(default_factory=lambda: _int("AUTOMATION_MAX_TOTAL_WORKERS", 8))
    max_active_per_user: int = field(default_factory=lambda: _int("AUTOMATION_MAX_ACTIVE_PER_USER", 5))
    run_timeout_hours: float = field(default_factory=lambda: float(os.environ.get("AUTOMATION_RUN_TIMEOUT_HOURS", "12")))
    # Automatic re-run after a crash (never after a login/input error). 0 turns it off.
    auto_retries: int = field(default_factory=lambda: _int("AUTOMATION_AUTO_RETRIES", 1))
    retry_backoff_seconds: int = field(default_factory=lambda: _int("AUTOMATION_RETRY_BACKOFF_SECONDS", 60))

    # Logs and retention (0 = keep forever).
    engine_log: str = field(default_factory=lambda: os.environ.get("AUTOMATION_ENGINE_LOG", "redacted"))  # redacted | off
    log_max_mb: int = field(default_factory=lambda: _int("AUTOMATION_LOG_MAX_MB", 20))
    retention_files_days: int = field(default_factory=lambda: _int("AUTOMATION_RETENTION_FILES_DAYS", 30))
    retention_rows_days: int = field(default_factory=lambda: _int("AUTOMATION_RETENTION_ROWS_DAYS", 180))

    # Running automations.
    python_bin: str = field(default_factory=lambda: os.environ.get("AUTOMATION_PYTHON", sys.executable))
    max_upload_mb: int = field(default_factory=lambda: _int("AUTOMATION_MAX_UPLOAD_MB", 25))
    max_workers_per_run: int = field(default_factory=lambda: _int("AUTOMATION_MAX_WORKERS", 4))
    # Browser used by the engines. Defaults match the Windows PC they were built on.
    browser_channel: str = field(default_factory=lambda: os.environ.get("AUTOMATION_BROWSER_CHANNEL", "msedge"))
    browser_headless: bool = field(default_factory=lambda: _bool("AUTOMATION_HEADLESS", False))

    # First admin, created at startup only if no admin exists yet.
    bootstrap_admin_email: str = field(default_factory=lambda: os.environ.get("AUTOMATION_ADMIN_EMAIL", ""))
    bootstrap_admin_password: str = field(default_factory=lambda: os.environ.get("AUTOMATION_ADMIN_PASSWORD", ""))

    @property
    def db_path(self) -> Path:
        return self.storage_dir / "automation.db"

    @property
    def uploads_dir(self) -> Path:
        return self.storage_dir / "workspaces"

    @property
    def runs_dir(self) -> Path:
        return self.storage_dir / "runs"


def get_settings() -> Settings:
    load_env_file()
    return Settings()
