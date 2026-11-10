"""The dashboard cards, and the upload -> map -> run flow for one automation."""

import json
import re
from pathlib import Path

import openpyxl
from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field

from ..db import utcnow
from ..deps import Context, get_ctx, require_user, user_automation_slugs
from ..services.audit import client_ip, log_activity
from ..services.jobs import workspace_for

router = APIRouter(prefix="/api/automations", tags=["automations"])

MAX_QUEUED_PER_USER = 5
_BAD_CHARS = re.compile(r"[^\w .()\-]")


def safe_filename(name: str) -> str:
    base = Path((name or "").replace("\\", "/")).name
    base = _BAD_CHARS.sub("_", base).strip(" .")
    return base[:120]


def fingerprint(headers) -> str:
    """Order-independent id for a set of sheet headers (same idea as the old mappings.json)."""
    return "|".join(sorted(str(h).strip() for h in headers if h not in (None, "")))


def read_headers(xlsx_path: Path):
    workbook = openpyxl.load_workbook(xlsx_path, read_only=True)
    try:
        sheet = workbook[workbook.sheetnames[0]]
        header_row = next(sheet.iter_rows(min_row=1, max_row=1, values_only=True), ())
        headers = [str(h).strip() for h in header_row if h not in (None, "")]
        return headers, max((sheet.max_row or 1) - 1, 0)
    finally:
        workbook.close()


def allowed_automation(ctx: Context, user, slug: str):
    """The automation if it exists AND this user may use it, else a 404 (a
    forbidden automation looks the same as one that doesn't exist)."""
    automation = ctx.registry.get(slug)
    if automation is None:
        raise HTTPException(404, "Automation not found.")
    if user["role"] != "admin":
        with ctx.db.session() as conn:
            if slug not in user_automation_slugs(conn, user):
                raise HTTPException(404, "Automation not found.")
    return automation


@router.get("")
def list_automations(user=Depends(require_user), ctx: Context = Depends(get_ctx)):
    with ctx.db.session() as conn:
        granted = set(user_automation_slugs(conn, user))
        cards = []
        for automation in ctx.registry.all():
            if user["role"] != "admin" and automation.slug not in granted:
                continue
            counts = conn.execute(
                "SELECT SUM(status='running') AS running, SUM(status='queued') AS queued"
                " FROM runs WHERE automation_slug=?", (automation.slug,)).fetchone()
            # The run a Stop button on the card would stop: the person's own
            # active run, or for an admin any active run of this automation.
            active_sql = ("SELECT r.id, r.status, r.user_id, u.name AS user_name FROM runs r"
                          " JOIN users u ON u.id=r.user_id WHERE r.automation_slug=?"
                          " AND r.status IN ('running','queued')")
            active_args = [automation.slug]
            if user["role"] != "admin":
                active_sql += " AND r.user_id=?"
                active_args.append(user["id"])
            active = conn.execute(active_sql + " ORDER BY (r.status='running') DESC, r.id LIMIT 1",
                                  active_args).fetchone()
            last = conn.execute(
                "SELECT id, status, finished_at, started_at, sheet_name, total FROM runs"
                " WHERE automation_slug=? AND user_id=? ORDER BY id DESC LIMIT 1",
                (automation.slug, user["id"])).fetchone()
            card = automation.public()
            card.update({
                "running_now": counts["running"] or 0,
                "queued": counts["queued"] or 0,
                "last_run": dict(last) if last else None,
                "active_run": ({"id": active["id"], "status": active["status"],
                                "mine": active["user_id"] == user["id"], "user_name": active["user_name"]}
                               if active else None),
            })
            # The field lists are only needed on the run page, keep cards light.
            for heavy in ("input_fields", "output_fields", "required_fields"):
                card.pop(heavy)
            cards.append(card)
    return {"automations": cards}


@router.get("/{slug}")
def automation_detail(slug: str, user=Depends(require_user), ctx: Context = Depends(get_ctx)):
    automation = allowed_automation(ctx, user, slug)
    return {**automation.public(), "max_workers": ctx.settings.max_workers_per_run}


@router.post("/{slug}/upload")
async def upload_sheet(slug: str, request: Request, file: UploadFile = File(...),
                       user=Depends(require_user), ctx: Context = Depends(get_ctx)):
    automation = allowed_automation(ctx, user, slug)
    filename = safe_filename(file.filename or "")
    if not filename.lower().endswith(".xlsx") or len(filename) < 6:
        raise HTTPException(400, "Please upload a .xlsx file.")

    limit = ctx.settings.max_upload_mb * 1024 * 1024
    content = await file.read(limit + 1)
    if len(content) > limit:
        raise HTTPException(413, f"File is larger than {ctx.settings.max_upload_mb} MB.")
    if not content.startswith(b"PK"):                  # .xlsx is a zip container
        raise HTTPException(400, "That does not look like a real .xlsx file.")

    data_dir = workspace_for(ctx.settings, slug, user["id"])["data_dir"]
    data_dir.mkdir(parents=True, exist_ok=True)
    dest = (data_dir / filename).resolve()
    if data_dir.resolve() not in dest.parents:
        raise HTTPException(400, "Invalid file name.")
    dest.write_bytes(content)

    try:
        headers, row_count = read_headers(dest)
    except Exception as exc:
        dest.unlink(missing_ok=True)
        raise HTTPException(400, f"Could not read that file: {exc}")
    if not headers:
        dest.unlink(missing_ok=True)
        raise HTTPException(400, "The first row of the sheet has no column headers.")

    try:
        default_map = ctx.jobs.default_map(automation)
    except RuntimeError as exc:
        raise HTTPException(500, f"Automation engine problem: {exc}")

    with ctx.db.session() as conn:
        saved = conn.execute("SELECT mapping FROM column_mappings WHERE automation_slug=? AND fingerprint=?",
                             (slug, fingerprint(headers))).fetchone()
        suggested = json.loads(saved["mapping"]) if saved else {}
        # Anything not remembered: pre-select a column whose header already
        # equals this engine's default name (compared stripped, as the engines do).
        for field_key, default_name in default_map.items():
            if field_key not in suggested and default_name.strip() in headers:
                suggested[field_key] = default_name.strip()
        log_activity(conn, user=user, action="sheet_uploaded", target_type="automation", target_id=slug,
                     details={"file": filename, "rows": row_count},
                     ip=client_ip(request, ctx.settings.trust_proxy))

    return {"filename": filename, "headers": headers, "row_count": row_count,
            "default_map": default_map, "suggested_map": suggested}


class RunBody(BaseModel):
    filename: str = Field(min_length=1, max_length=200)
    column_map: dict
    save: bool = False
    workers: int = 4
    limit: int | None = Field(default=None, ge=1, le=100000)
    only: str | None = Field(default=None, max_length=100)
    allow_duplicate: bool = False
    no_resume: bool = False


@router.post("/{slug}/run", status_code=201)
def start_run(slug: str, body: RunBody, request: Request,
              user=Depends(require_user), ctx: Context = Depends(get_ctx)):
    automation = allowed_automation(ctx, user, slug)

    column_map = {str(k): str(v) for k, v in body.column_map.items() if isinstance(v, (str, type(None)))}
    missing = [label for key, label in automation.required_fields.items() if not column_map.get(key)]
    if missing:
        raise HTTPException(400, f"Map a column for: {', '.join(missing)}")

    data_dir = workspace_for(ctx.settings, slug, user["id"])["data_dir"]
    sheet = (data_dir / safe_filename(body.filename)).resolve()
    if data_dir.resolve() not in sheet.parents or not sheet.exists():
        raise HTTPException(400, f"{body.filename} was not found - upload it again.")

    with ctx.db.session() as conn:
        waiting = conn.execute("SELECT COUNT(*) AS n FROM runs WHERE user_id=? AND status IN ('queued','running')",
                               (user["id"],)).fetchone()["n"]
    if waiting >= MAX_QUEUED_PER_USER:
        raise HTTPException(429, "You already have several runs waiting. Wait for one to finish.")

    check = ctx.jobs.validate_sheet(automation, sheet, column_map)
    if not check.get("ok"):
        raise HTTPException(400, check.get("error", "Could not read that sheet."))

    headers, _ = read_headers(sheet)
    workers = max(1, min(int(body.workers or 1), ctx.settings.max_workers_per_run))
    options = {"column_map": column_map, "save": body.save, "workers": workers, "limit": body.limit,
               "only": (body.only or "").strip() or None, "allow_duplicate": body.allow_duplicate,
               "no_resume": body.no_resume}

    with ctx.db.transaction() as conn:
        # Remembered for the next sheet with the same columns.
        conn.execute(
            "INSERT INTO column_mappings (automation_slug, fingerprint, mapping, updated_by, updated_at)"
            " VALUES (?,?,?,?,?) ON CONFLICT(automation_slug, fingerprint) DO UPDATE SET"
            " mapping=excluded.mapping, updated_by=excluded.updated_by, updated_at=excluded.updated_at",
            (slug, fingerprint(headers), json.dumps(column_map), user["id"], utcnow()))
        cur = conn.execute(
            "INSERT INTO runs (automation_slug, user_id, status, sheet_name, sheet_path, options, total, queued_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (slug, user["id"], "queued", sheet.name, str(sheet), json.dumps(options), check["total"], utcnow()))
        run_id = cur.lastrowid
        log_activity(conn, user=user, action="run_queued", target_type="run", target_id=run_id,
                     details={"automation": slug, "sheet": sheet.name, "rows": check["total"],
                              "save": body.save, "workers": workers},
                     ip=client_ip(request, ctx.settings.trust_proxy))

    ctx.jobs.submit(run_id, slug)
    return {"run_id": run_id, "total": check["total"], "workers": workers,
            "queue_position": ctx.jobs.queue_position(run_id, slug)}
