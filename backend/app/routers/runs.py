"""Run history, live progress, logs, downloads, cancel."""

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse

from ..deps import Context, get_ctx, require_user
from ..services.audit import client_ip, log_activity

router = APIRouter(prefix="/api/runs", tags=["runs"])

COUNT_SQL = """
    SELECT
      COALESCE(SUM(CASE WHEN duplicate_of <> '' THEN 1 ELSE 0 END), 0) AS duplicate,
      COALESCE(SUM(CASE WHEN duplicate_of = '' AND status LIKE 'Done%' THEN 1 ELSE 0 END), 0) AS created,
      COALESCE(SUM(CASE WHEN status = 'Not Created' THEN 1 ELSE 0 END), 0) AS not_created,
      COALESCE(SUM(CASE WHEN status LIKE 'Done%' THEN 1 ELSE 0 END), 0) AS done,
      COUNT(*) AS seen
    FROM run_rows WHERE run_id = ?
"""

RUN_COLUMNS = ("r.id, r.automation_slug, r.user_id, u.email AS user_email, u.name AS user_name, r.status,"
               " r.sheet_name, r.total, r.summary, r.error, r.queued_at, r.started_at, r.finished_at,"
               " r.options")


def _run_dict(row, counts=None):
    out = {k: row[k] for k in row.keys() if k != "options"}
    if counts is not None:
        out["counts"] = dict(counts)
    return out


def _get_run(conn, run_id: int, user):
    row = conn.execute(f"SELECT {RUN_COLUMNS} FROM runs r JOIN users u ON u.id=r.user_id WHERE r.id=?",
                       (run_id,)).fetchone()
    # Someone else's run looks like a missing one.
    if not row or (user["role"] != "admin" and row["user_id"] != user["id"]):
        raise HTTPException(404, "Run not found.")
    return row


@router.get("")
def list_runs(user=Depends(require_user), ctx: Context = Depends(get_ctx),
              automation: str | None = None, user_id: int | None = None,
              status: str | None = None,
              limit: int = Query(25, ge=1, le=200), offset: int = Query(0, ge=0)):
    where, args = [], []
    if user["role"] != "admin":
        where.append("r.user_id = ?"); args.append(user["id"])
    elif user_id:
        where.append("r.user_id = ?"); args.append(user_id)
    if automation:
        where.append("r.automation_slug = ?"); args.append(automation)
    if status:
        where.append("r.status = ?"); args.append(status)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    with ctx.db.session() as conn:
        total = conn.execute(f"SELECT COUNT(*) AS n FROM runs r {clause}", args).fetchone()["n"]
        rows = conn.execute(
            f"SELECT {RUN_COLUMNS} FROM runs r JOIN users u ON u.id=r.user_id {clause}"
            " ORDER BY r.id DESC LIMIT ? OFFSET ?", (*args, limit, offset)).fetchall()
        items = [_run_dict(r, conn.execute(COUNT_SQL, (r["id"],)).fetchone()) for r in rows]
    return {"total": total, "runs": items}


@router.get("/{run_id}")
def run_detail(run_id: int, user=Depends(require_user), ctx: Context = Depends(get_ctx),
               rows: int = Query(10, ge=0, le=5000)):
    """Header, counts and the most recently updated `rows` rows (0 = none)."""
    with ctx.db.session() as conn:
        run = _get_run(conn, run_id, user)
        counts = conn.execute(COUNT_SQL, (run_id,)).fetchone()
        row_items = conn.execute(
            "SELECT row_no AS row, patient_name, dos, status, visit_id, beneficiary_id, duplicate_of,"
            " error, line_charges, note FROM run_rows WHERE run_id=?"
            " ORDER BY updated_at DESC, row_no DESC LIMIT ?", (run_id, rows)).fetchall()
    out = _run_dict(run, counts)
    out["rows"] = [dict(r) for r in row_items]
    out["queue_position"] = (ctx.jobs.queue_position(run_id, run["automation_slug"])
                             if run["status"] == "queued" else None)
    return out


@router.get("/{run_id}/log")
def run_log(run_id: int, user=Depends(require_user), ctx: Context = Depends(get_ctx),
            tail: int = Query(300, ge=10, le=5000)):
    with ctx.db.session() as conn:
        _get_run(conn, run_id, user)
    path = ctx.settings.runs_dir / str(run_id) / "run.log"
    if not path.exists():
        return {"lines": []}
    # Read only the end of a possibly large file.
    with path.open("rb") as handle:
        handle.seek(0, 2)
        size = handle.tell()
        handle.seek(max(0, size - 400_000))
        text = handle.read().decode("utf-8", errors="replace")
    return {"lines": text.splitlines()[-tail:]}


@router.get("/{run_id}/download")
def download(run_id: int, request: Request, user=Depends(require_user),
             ctx: Context = Depends(get_ctx)):
    with ctx.db.session() as conn:
        _get_run(conn, run_id, user)
        path = conn.execute("SELECT output_path FROM runs WHERE id=?", (run_id,)).fetchone()["output_path"]
        automation_slug = conn.execute("SELECT automation_slug FROM runs WHERE id=?", (run_id,)).fetchone()[0]
        if not path or not Path(path).exists():
            # A run still in progress: the engine keeps this file current, so
            # serve it once it exists even though the run has not finished.
            from ..services.jobs import workspace_for
            owner = conn.execute("SELECT user_id, sheet_name FROM runs WHERE id=?", (run_id,)).fetchone()
            guess = (workspace_for(ctx.settings, automation_slug, owner["user_id"])["output_dir"]
                     / f"{Path(owner['sheet_name']).stem}_with_status.xlsx")
            path = str(guess) if guess.exists() else None
        if not path:
            return JSONResponse({"detail": "The result sheet is not written yet."}, status_code=404)
        log_activity(conn, user=user, action="download", target_type="run", target_id=run_id,
                     ip=client_ip(request, ctx.settings.trust_proxy))
    return FileResponse(
        path, filename=Path(path).name,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@router.post("/{run_id}/cancel")
def cancel(run_id: int, request: Request, user=Depends(require_user),
           ctx: Context = Depends(get_ctx)):
    with ctx.db.session() as conn:
        run = _get_run(conn, run_id, user)
        if run["status"] not in ("queued", "running"):
            raise HTTPException(409, "This run has already finished.")
        log_activity(conn, user=user, action="run_cancel_requested", target_type="run", target_id=run_id,
                     ip=client_ip(request, ctx.settings.trust_proxy))
    return {"status": ctx.jobs.cancel(run_id)}
