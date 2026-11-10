"""Run history, live progress, logs, files, failed records, retry and cancel."""

import csv
import io
import json
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response

from ..deps import Context, get_ctx, require_user
from ..services.audit import client_ip, log_activity
from ..services.jobs import OPEN_STATES, RETRYABLE, run_paths

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
               " r.sheet_name, r.total, r.carried_over, r.summary, r.error, r.failure_kind, r.queued_at,"
               " r.started_at, r.finished_at, r.attempt, r.retry_of, r.files_purged_at, r.rows_purged_at, r.options")

# kind -> file name pattern inside the run's output folder (never built from client input)
OUTPUT_FILES = {"status": "{stem}_with_status.xlsx", "results": "{stem}_results.csv", "visits": "{stem}_visits.csv"}
MIME = {".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".csv": "text/csv", ".log": "text/plain"}


def _counts(conn, run, counts_row):
    c = dict(counts_row)
    c["carried"] = run["carried_over"]
    c["pending"] = max(0, (run["total"] or 0) - c["seen"] - run["carried_over"]) \
        if run["status"] in OPEN_STATES else max(0, (run["total"] or 0) - c["seen"] - run["carried_over"])
    return c


def _run_dict(conn, row, with_counts=True):
    out = {k: row[k] for k in row.keys() if k != "options"}
    if with_counts:
        out["counts"] = _counts(conn, row, conn.execute(COUNT_SQL, (row["id"],)).fetchone())
    return out


def _get_run(conn, run_id: int, user):
    row = conn.execute(f"SELECT {RUN_COLUMNS} FROM runs r JOIN users u ON u.id=r.user_id WHERE r.id=?", (run_id,)).fetchone()
    if not row or (user["role"] != "admin" and row["user_id"] != user["id"]):
        raise HTTPException(404, "Run not found.")      # someone else's run looks like a missing one
    return row


def _chain(conn, run_id: int) -> list[dict]:
    """The run plus every retry before and after it, oldest first."""
    seen = {run_id}
    cursor = run_id
    while True:                                          # walk back to the first attempt
        parent = conn.execute("SELECT retry_of FROM runs WHERE id=?", (cursor,)).fetchone()
        if not parent or not parent["retry_of"] or parent["retry_of"] in seen:
            break
        cursor = parent["retry_of"]
        seen.add(cursor)
    ids, frontier = [cursor], [cursor]
    while frontier:                                      # then collect the retries after it
        marks = ",".join("?" * len(frontier))
        children = [r["id"] for r in conn.execute(f"SELECT id FROM runs WHERE retry_of IN ({marks}) ORDER BY id", frontier)]
        children = [c for c in children if c not in ids]
        ids += children
        frontier = children
    marks = ",".join("?" * len(ids))
    return [dict(r) for r in conn.execute(f"SELECT id, attempt, status FROM runs WHERE id IN ({marks}) ORDER BY id", ids)]


@router.get("")
def list_runs(user=Depends(require_user), ctx: Context = Depends(get_ctx),
              automation: str | None = None, user_id: int | None = None, status: str | None = None,
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
        items = [_run_dict(conn, r) for r in rows]
    return {"total": total, "runs": items}


@router.get("/{run_id}")
def run_detail(run_id: int, user=Depends(require_user), ctx: Context = Depends(get_ctx),
               rows: int = Query(10, ge=0, le=5000),
               view: str = Query("all", pattern="^(all|failed|skipped|created)$")):
    """Header, counts and the most recently updated rows (filtered by `view`; rows=0 for none)."""
    clause = {"all": "", "failed": " AND status='Not Created'", "skipped": " AND duplicate_of<>''",
              "created": " AND duplicate_of='' AND status LIKE 'Done%'"}[view]
    with ctx.db.session() as conn:
        run = _get_run(conn, run_id, user)
        out = _run_dict(conn, run)
        row_items = conn.execute(
            "SELECT row_no AS row, patient_name, dos, status, visit_id, beneficiary_id, duplicate_of,"
            f" error, line_charges, note FROM run_rows WHERE run_id=?{clause}"
            " ORDER BY updated_at DESC, row_no DESC LIMIT ?", (run_id, rows)).fetchall()
        chain = _chain(conn, run_id)
        has_open_same_sheet = conn.execute(
            "SELECT 1 FROM runs WHERE user_id=? AND automation_slug=? AND sheet_name=? AND status IN ('queued','validating','running') LIMIT 1",
            (run["user_id"], run["automation_slug"], run["sheet_name"])).fetchone()
    options = json.loads(run["options"])
    out["config"] = {k: options.get(k) for k in ("save", "workers", "limit", "only", "allow_duplicate", "no_resume", "column_map")}
    out["rows"] = [dict(r) for r in row_items]
    out["chain"] = chain
    out["can_cancel"] = run["status"] in OPEN_STATES
    out["can_retry"] = run["status"] in RETRYABLE and not run["files_purged_at"] and not has_open_same_sheet
    out["queue_position"] = (ctx.jobs.queue_position(run_id, run["automation_slug"]) if run["status"] == "queued" else None)
    return out


@router.get("/{run_id}/events")
def run_events(run_id: int, user=Depends(require_user), ctx: Context = Depends(get_ctx)):
    with ctx.db.session() as conn:
        _get_run(conn, run_id, user)
        rows = conn.execute("SELECT ts, level, kind, message, details FROM run_events WHERE run_id=? ORDER BY id", (run_id,)).fetchall()
    return {"events": [{**dict(r), "details": json.loads(r["details"]) if r["details"] else None} for r in rows]}


@router.get("/{run_id}/log")
def run_log(run_id: int, user=Depends(require_user), ctx: Context = Depends(get_ctx),
            tail: int = Query(300, ge=10, le=5000)):
    with ctx.db.session() as conn:
        run = _get_run(conn, run_id, user)
    path = run_paths(ctx.settings, run_id)["log_path"]
    if run["files_purged_at"] or not path.exists():
        return {"lines": [], "purged": bool(run["files_purged_at"])}
    with path.open("rb") as handle:                      # read only the end of a possibly large file
        handle.seek(0, 2)
        size = handle.tell()
        handle.seek(max(0, size - 400_000))
        text = handle.read().decode("utf-8", errors="replace")
    return {"lines": text.splitlines()[-tail:], "purged": False}


def _output_path(ctx, run, kind: str) -> Path | None:
    stem = Path(run["sheet_name"]).stem
    base = run_paths(ctx.settings, run["id"])["output_dir"]
    path = base / OUTPUT_FILES[kind].format(stem=stem)
    return path if path.exists() and not run["files_purged_at"] else None


@router.get("/{run_id}/files")
def run_files(run_id: int, user=Depends(require_user), ctx: Context = Depends(get_ctx)):
    """What can be downloaded for this run."""
    with ctx.db.session() as conn:
        run = _get_run(conn, run_id, user)
        failed = conn.execute("SELECT COUNT(*) AS n FROM run_rows WHERE run_id=? AND status='Not Created'", (run_id,)).fetchone()["n"]
    labels = {"status": "Result sheet", "results": "Results (CSV)", "visits": "Visits (CSV)"}
    files = []
    for kind, label in labels.items():
        path = _output_path(ctx, run, kind)
        if path:
            files.append({"kind": kind, "label": label, "name": path.name, "size": path.stat().st_size})
    if failed:
        files.append({"kind": "failed", "label": "Failed records (CSV)", "name": f"run-{run_id}-failed.csv", "size": None})
    log = run_paths(ctx.settings, run_id)["log_path"]
    if log.exists() and not run["files_purged_at"]:
        files.append({"kind": "log", "label": "Run log", "name": "run.log", "size": log.stat().st_size})
    return {"files": files, "purged": bool(run["files_purged_at"])}


def _csv_safe(value) -> str:
    text = "" if value is None else str(value)
    # spreadsheet formula injection: a cell that starts like a formula is neutralised
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


@router.get("/{run_id}/download")
def download(run_id: int, request: Request, file: str = Query("status", pattern="^(status|results|visits|failed|log)$"),
             user=Depends(require_user), ctx: Context = Depends(get_ctx)):
    with ctx.db.session() as conn:
        run = _get_run(conn, run_id, user)
        if file == "failed":
            rows = conn.execute(
                "SELECT row_no, patient_name, dos, status, error, note FROM run_rows WHERE run_id=? AND status='Not Created' ORDER BY row_no",
                (run_id,)).fetchall()
            buffer = io.StringIO()
            writer = csv.writer(buffer)
            writer.writerow(["Row", "Patient", "DOS", "Status", "Reason"])
            for r in rows:
                writer.writerow([r["row_no"], _csv_safe(r["patient_name"]), _csv_safe(r["dos"]), r["status"],
                                 _csv_safe(r["error"] or r["note"])])
            path, payload = None, buffer.getvalue()
        elif file == "log":
            path, payload = run_paths(ctx.settings, run_id)["log_path"], None
        else:
            path, payload = _output_path(ctx, run, file), None
        if payload is None and (path is None or not path.exists() or run["files_purged_at"]):
            return JSONResponse({"detail": "That file is not available (not written yet, or removed by the retention policy)."}, status_code=404)
        log_activity(conn, user=user, action="download", target_type="run", target_id=run_id,
                     details={"file": file}, ip=client_ip(request, ctx.settings.trust_proxy))
    if payload is not None:
        return Response(payload, media_type="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="run-{run_id}-failed.csv"'})
    return FileResponse(path, filename=path.name, media_type=MIME.get(path.suffix, "application/octet-stream"))


@router.post("/{run_id}/cancel")
def cancel(run_id: int, request: Request, user=Depends(require_user), ctx: Context = Depends(get_ctx)):
    with ctx.db.session() as conn:
        run = _get_run(conn, run_id, user)
        if run["status"] not in OPEN_STATES:
            raise HTTPException(409, "This run has already finished.")
        log_activity(conn, user=user, action="run_cancel_requested", target_type="run", target_id=run_id,
                     ip=client_ip(request, ctx.settings.trust_proxy))
    return {"status": ctx.jobs.cancel(run_id)}


@router.post("/{run_id}/retry", status_code=201)
def retry(run_id: int, user=Depends(require_user), ctx: Context = Depends(get_ctx)):
    """Run the same sheet again. Rows already Done are skipped and duplicates are never allowed."""
    with ctx.db.session() as conn:
        _get_run(conn, run_id, user)
    try:
        new_id = ctx.jobs.create_retry(run_id, user, manual=True)
    except LookupError:
        raise HTTPException(404, "Run not found.")
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    return {"run_id": new_id}
