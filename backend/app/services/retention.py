"""Retention: patient data does not live forever.

  * Files (a run's sheet copy, outputs and logs, per-user staging uploads and progress
    copies) are removed `retention_files_days` after the run finished.
  * Row-level results (patient name, DOS, IDs) are removed `retention_rows_days` after.
  * The run itself (who, when, counts, status) and the audit trail are kept.
0 disables a rule. Runs that are still open are never touched.
"""

import shutil
import time
from datetime import datetime, timedelta, timezone

from ..db import utcnow
from .jobs import run_dir


def _cutoff(days: int, now: datetime) -> str:
    return (now - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def purge(ctx, now: datetime | None = None) -> dict:
    s = ctx.settings
    now = now or datetime.now(timezone.utc)
    stats = {"run_files": 0, "run_rows": 0, "stale_files": 0}

    if s.retention_files_days:
        cutoff = _cutoff(s.retention_files_days, now)
        with ctx.db.session() as conn:
            old = conn.execute(
                "SELECT id FROM runs WHERE finished_at IS NOT NULL AND finished_at<? AND files_purged_at IS NULL"
                " AND status NOT IN ('queued','validating','running')", (cutoff,)).fetchall()
            for row in old:
                shutil.rmtree(run_dir(s, row["id"]), ignore_errors=True)
                conn.execute("UPDATE runs SET files_purged_at=?, output_path=NULL WHERE id=?", (utcnow(), row["id"]))
                conn.execute("UPDATE run_events SET details=NULL WHERE run_id=?", (row["id"],))
                stats["run_files"] += 1
        # per-user staging uploads and progress copies that nothing has touched for the whole period
        limit = now.timestamp() - s.retention_files_days * 86400
        if s.uploads_dir.exists():
            for area in s.uploads_dir.glob("*/user-*"):
                for sub in ("uploads", "resume"):
                    for path in (area / sub).glob("**/*"):
                        try:
                            if path.is_file() and path.stat().st_mtime < limit:
                                path.unlink()
                                stats["stale_files"] += 1
                        except OSError:
                            pass
        diagnostics = s.storage_dir / "diagnostics"
        if diagnostics.exists():
            for path in diagnostics.glob("**/*.png"):
                try:
                    if path.stat().st_mtime < limit:
                        path.unlink()
                        stats["stale_files"] += 1
                except OSError:
                    pass
        oneshot = s.runs_dir / "_oneshot"
        if oneshot.exists():
            for path in oneshot.glob("*"):
                try:
                    if time.time() - path.stat().st_mtime > 86400:
                        path.unlink()
                except OSError:
                    pass

    if s.retention_rows_days:
        cutoff = _cutoff(s.retention_rows_days, now)
        with ctx.db.session() as conn:
            old = conn.execute(
                "SELECT id FROM runs WHERE finished_at IS NOT NULL AND finished_at<? AND rows_purged_at IS NULL"
                " AND status NOT IN ('queued','validating','running')", (cutoff,)).fetchall()
            for row in old:
                conn.execute("DELETE FROM run_rows WHERE run_id=?", (row["id"],))
                conn.execute("UPDATE runs SET rows_purged_at=? WHERE id=?", (utcnow(), row["id"]))
                stats["run_rows"] += 1
    return stats
