"""SQLite access: one connection per call, WAL mode, versioned migrations.

Plain `sqlite3` on purpose - the schema is small and sqlite3 ships with
Python. Every connection turns on foreign keys and a busy timeout, because the
web threads and the job-runner threads write at the same time.
"""

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


def utcnow() -> str:
    """ISO-8601 UTC with seconds, e.g. 2026-11-10T14:03:22Z. Sorts as text."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


MIGRATIONS = [
    # 1 - initial schema
    """
    CREATE TABLE users (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        email           TEXT NOT NULL UNIQUE COLLATE NOCASE,
        name            TEXT NOT NULL,
        password_hash   TEXT NOT NULL,
        role            TEXT NOT NULL DEFAULT 'user' CHECK (role IN ('admin','user')),
        status          TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','active','disabled')),
        failed_attempts INTEGER NOT NULL DEFAULT 0,
        locked_until    TEXT,
        created_at      TEXT NOT NULL,
        approved_at     TEXT,
        approved_by     INTEGER REFERENCES users(id),
        last_login_at   TEXT
    );

    CREATE TABLE sessions (
        id           TEXT PRIMARY KEY,              -- sha256 of the cookie token
        user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        created_at   TEXT NOT NULL,
        last_seen_at TEXT NOT NULL,
        expires_at   TEXT NOT NULL,
        ip           TEXT,
        user_agent   TEXT,
        revoked_at   TEXT
    );
    CREATE INDEX idx_sessions_user ON sessions(user_id);

    CREATE TABLE user_automations (
        user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        automation_slug TEXT NOT NULL,
        granted_at      TEXT NOT NULL,
        granted_by      INTEGER REFERENCES users(id),
        PRIMARY KEY (user_id, automation_slug)
    );

    -- every sign-in attempt, successful or not (the admin "Logins" monitor)
    CREATE TABLE login_events (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id    INTEGER REFERENCES users(id) ON DELETE SET NULL,
        email      TEXT NOT NULL,
        success    INTEGER NOT NULL CHECK (success IN (0,1)),
        reason     TEXT,
        ip         TEXT,
        user_agent TEXT,
        created_at TEXT NOT NULL
    );
    CREATE INDEX idx_login_events_time ON login_events(created_at);
    CREATE INDEX idx_login_events_user ON login_events(user_id);

    -- audit trail: who did what, to what, from where
    CREATE TABLE activity_logs (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id     INTEGER REFERENCES users(id) ON DELETE SET NULL,
        actor_email TEXT,
        action      TEXT NOT NULL,
        target_type TEXT,
        target_id   TEXT,
        details     TEXT,                           -- JSON
        ip          TEXT,
        created_at  TEXT NOT NULL
    );
    CREATE INDEX idx_activity_time ON activity_logs(created_at);
    CREATE INDEX idx_activity_user ON activity_logs(user_id);
    CREATE INDEX idx_activity_action ON activity_logs(action);

    CREATE TABLE runs (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        automation_slug TEXT NOT NULL,
        user_id         INTEGER NOT NULL REFERENCES users(id),
        status          TEXT NOT NULL DEFAULT 'queued'
                        CHECK (status IN ('queued','running','succeeded','failed','cancelled')),
        sheet_name      TEXT NOT NULL,
        sheet_path      TEXT NOT NULL,
        options         TEXT NOT NULL,              -- JSON: column_map, save, workers, ...
        total           INTEGER NOT NULL DEFAULT 0,
        summary         TEXT,
        error           TEXT,
        queued_at       TEXT NOT NULL,
        started_at      TEXT,
        finished_at     TEXT,
        output_path     TEXT,
        cancel_requested INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX idx_runs_user ON runs(user_id, id DESC);
    CREATE INDEX idx_runs_auto ON runs(automation_slug, status);

    CREATE TABLE run_rows (
        run_id         INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
        row_no         INTEGER NOT NULL,
        patient_name   TEXT,
        dos            TEXT,
        status         TEXT,
        visit_id       TEXT,
        beneficiary_id TEXT,
        duplicate_of   TEXT,
        error          TEXT,
        line_charges   TEXT,
        note           TEXT,
        updated_at     TEXT NOT NULL,
        PRIMARY KEY (run_id, row_no)
    );

    -- remembered column mapping per sheet layout, per automation
    CREATE TABLE column_mappings (
        automation_slug TEXT NOT NULL,
        fingerprint     TEXT NOT NULL,
        mapping         TEXT NOT NULL,              -- JSON
        updated_by      INTEGER REFERENCES users(id),
        updated_at      TEXT NOT NULL,
        PRIMARY KEY (automation_slug, fingerprint)
    );
    """,
    # 2 - job lifecycle: richer states, retry chain, per-run bookkeeping, event timeline.
    # SQLite cannot alter a CHECK constraint, so `runs` is rebuilt (foreign keys are switched
    # off around this script by init_db, see FK_OFF_MIGRATIONS).
    """
    CREATE TABLE runs_new (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        automation_slug TEXT NOT NULL,
        user_id         INTEGER NOT NULL REFERENCES users(id),
        status          TEXT NOT NULL DEFAULT 'queued'
                        CHECK (status IN ('queued','validating','running','completed',
                                          'completed_with_errors','failed','interrupted','cancelled')),
        sheet_name      TEXT NOT NULL,
        sheet_path      TEXT NOT NULL,              -- the run's own private copy of the sheet
        options         TEXT NOT NULL,
        total           INTEGER NOT NULL DEFAULT 0,
        carried_over    INTEGER NOT NULL DEFAULT 0, -- rows already Done from an earlier run (resume)
        summary         TEXT,
        error           TEXT,
        failure_kind    TEXT,                       -- login_failed|invalid_input|crash|timeout|engine_error|verification_failed|interrupted
        queued_at       TEXT NOT NULL,
        started_at      TEXT,
        finished_at     TEXT,
        output_path     TEXT,
        cancel_requested INTEGER NOT NULL DEFAULT 0,
        retry_of        INTEGER,                    -- run this one retries (soft link)
        attempt         INTEGER NOT NULL DEFAULT 1,
        not_before      TEXT,                       -- backoff: do not start before this time
        files_purged_at TEXT,
        rows_purged_at  TEXT
    );
    INSERT INTO runs_new (id, automation_slug, user_id, status, sheet_name, sheet_path, options, total,
                          summary, error, queued_at, started_at, finished_at, output_path, cancel_requested)
    SELECT id, automation_slug, user_id,
           CASE status WHEN 'succeeded' THEN 'completed' ELSE status END,
           sheet_name, sheet_path, options, total, summary, error, queued_at, started_at, finished_at,
           output_path, cancel_requested
    FROM runs;
    DROP TABLE runs;
    ALTER TABLE runs_new RENAME TO runs;
    CREATE INDEX idx_runs_user ON runs(user_id, id DESC);
    CREATE INDEX idx_runs_auto ON runs(automation_slug, status);
    CREATE INDEX idx_runs_retry ON runs(retry_of);

    CREATE TABLE run_events (
        id      INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id  INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
        ts      TEXT NOT NULL,
        level   TEXT NOT NULL CHECK (level IN ('info','warn','error')),
        kind    TEXT NOT NULL,
        message TEXT NOT NULL,
        details TEXT
    );
    CREATE INDEX idx_run_events_run ON run_events(run_id, id);
    """,
    # 3 - per-person, per-automation permission to update the Office Ally login (admins always may)
    """
    ALTER TABLE user_automations ADD COLUMN can_manage_credentials INTEGER NOT NULL DEFAULT 0;
    """,
]


FK_OFF_MIGRATIONS = {2}      # migrations that rebuild a table other tables point at


def connect(db_path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=30, isolation_level=None,
                           check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init_db(db_path) -> None:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
        row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
        current = row["v"] or 0
        for number, script in enumerate(MIGRATIONS, start=1):
            if number <= current:
                continue
            rebuild = number in FK_OFF_MIGRATIONS
            if rebuild:   # table rebuilds must not cascade-delete child rows (run_rows) - the pragma is a no-op inside a transaction
                conn.execute("PRAGMA foreign_keys = OFF")
            try:
                conn.executescript("BEGIN;\n" + script + f"\nINSERT INTO schema_version(version) VALUES ({number});\nCOMMIT;")
                if rebuild and conn.execute("PRAGMA foreign_key_check").fetchall():
                    raise RuntimeError(f"migration {number} left broken foreign keys")
            finally:
                if rebuild:
                    conn.execute("PRAGMA foreign_keys = ON")
    finally:
        conn.close()


class Database:
    """Thin handle so tests can point the whole app at a temp file."""

    def __init__(self, path):
        self.path = Path(path)

    @contextmanager
    def session(self):
        conn = connect(self.path)
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def transaction(self):
        conn = connect(self.path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
