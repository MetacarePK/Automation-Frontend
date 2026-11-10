"""The dashboard cards, the upload -> map -> run flow, and the automation's Office Ally login."""

import json
import re
import threading
from pathlib import Path

import openpyxl
from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field

from ..db import utcnow
from ..deps import Context, get_ctx, require_user, user_automation_slugs
from ..services import credentials as creds
from ..services.audit import client_ip, log_activity
from ..services.jobs import HOLD, OPEN_STATES, run_paths, sheet_fingerprint, staging_dir

router = APIRouter(prefix="/api/automations", tags=["automations"])

_BAD_CHARS = re.compile(r"[^\w .()\-]")
_login_test_locks: dict[str, threading.Lock] = {}


def safe_filename(name: str) -> str:
    base = Path((name or "").replace("\\", "/")).name
    base = _BAD_CHARS.sub("_", base).strip(" .")
    return base[:120]


def fingerprint(headers) -> str:
    """Order-independent id for a set of sheet headers."""
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
    """The automation if it exists AND this user may use it, else a 404."""
    automation = ctx.registry.get(slug)
    if automation is None:
        raise HTTPException(404, "Automation not found.")
    if user["role"] != "admin":
        with ctx.db.session() as conn:
            if slug not in user_automation_slugs(conn, user):
                raise HTTPException(404, "Automation not found.")
    return automation


def can_manage_credentials(ctx: Context, user, slug: str) -> bool:
    if user["role"] == "admin":
        return True
    with ctx.db.session() as conn:
        row = conn.execute("SELECT can_manage_credentials FROM user_automations WHERE user_id=? AND automation_slug=?",
                           (user["id"], slug)).fetchone()
    return bool(row and row["can_manage_credentials"])


# ------------------------------------------------------------------ cards

@router.get("")
def list_automations(user=Depends(require_user), ctx: Context = Depends(get_ctx)):
    with ctx.db.session() as conn:
        granted = set(user_automation_slugs(conn, user))
        cards = []
        for automation in ctx.registry.all():
            if user["role"] != "admin" and automation.slug not in granted:
                continue
            counts = conn.execute(
                "SELECT COALESCE(SUM(status IN ('validating','running')),0) AS running,"
                " COALESCE(SUM(status='queued'),0) AS queued FROM runs WHERE automation_slug=?",
                (automation.slug,)).fetchone()
            # The run a Stop button on the card would stop: the person's own open run, or for an
            # admin any open run of this automation.
            active_sql = ("SELECT r.id, r.status, r.user_id, u.name AS user_name FROM runs r"
                          " JOIN users u ON u.id=r.user_id WHERE r.automation_slug=?"
                          " AND r.status IN ('queued','validating','running')")
            active_args = [automation.slug]
            if user["role"] != "admin":
                active_sql += " AND r.user_id=?"
                active_args.append(user["id"])
            active = conn.execute(active_sql + " ORDER BY (r.status IN ('validating','running')) DESC, r.id LIMIT 1",
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
            for heavy in ("input_fields", "output_fields", "required_fields"):
                card.pop(heavy)
            cards.append(card)
    return {"automations": cards}


@router.get("/{slug}")
def automation_detail(slug: str, user=Depends(require_user), ctx: Context = Depends(get_ctx)):
    automation = allowed_automation(ctx, user, slug)
    return {**automation.public(), "max_workers": ctx.settings.max_workers_per_run,
            "can_manage_credentials": can_manage_credentials(ctx, user, slug)}


# ------------------------------------------------------------------ upload and run

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

    stage = staging_dir(ctx.settings, slug, user["id"])
    stage.mkdir(parents=True, exist_ok=True)
    dest = (stage / filename).resolve()
    if stage.resolve() not in dest.parents:
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
    settings = ctx.settings

    column_map = {str(k): str(v) for k, v in body.column_map.items() if isinstance(v, (str, type(None)))}
    missing = [label for key, label in automation.required_fields.items() if not column_map.get(key)]
    if missing:
        raise HTTPException(400, f"Map a column for: {', '.join(missing)}")

    stage = staging_dir(settings, slug, user["id"])
    staged = (stage / safe_filename(body.filename)).resolve()
    if stage.resolve() not in staged.parents or not staged.exists():
        raise HTTPException(400, f"{body.filename} was not found - upload it again.")

    with ctx.db.session() as conn:
        open_runs = conn.execute(
            "SELECT automation_slug, sheet_name FROM runs WHERE user_id=? AND status IN ('queued','validating','running')",
            (user["id"],)).fetchall()
    if len(open_runs) >= settings.max_active_per_user:
        raise HTTPException(429, "You already have several runs waiting. Wait for one to finish.")
    if any(r["automation_slug"] == slug and r["sheet_name"] == staged.name for r in open_runs):
        # the same sheet twice at once could process the same patients twice
        raise HTTPException(409, f"{staged.name} is already queued or running. Wait for it, or stop it first.")

    check = ctx.jobs.validate_sheet(automation, staged, column_map)
    if not check.get("ok"):
        raise HTTPException(400, check.get("error", "Could not read that sheet."))

    headers, _ = read_headers(staged)
    workers = max(1, min(int(body.workers or 1), settings.max_workers_per_run))
    options = {"column_map": column_map, "save": body.save, "workers": workers, "limit": body.limit,
               "only": (body.only or "").strip() or None, "allow_duplicate": body.allow_duplicate,
               "no_resume": body.no_resume, "sheet_sha256": sheet_fingerprint(staged)}

    with ctx.db.transaction() as conn:
        conn.execute(
            "INSERT INTO column_mappings (automation_slug, fingerprint, mapping, updated_by, updated_at)"
            " VALUES (?,?,?,?,?) ON CONFLICT(automation_slug, fingerprint) DO UPDATE SET"
            " mapping=excluded.mapping, updated_by=excluded.updated_by, updated_at=excluded.updated_at",
            (slug, fingerprint(headers), json.dumps(column_map), user["id"], utcnow()))
        # HOLD keeps the scheduler away until the run's private copy of the sheet exists
        run_id = conn.execute(
            "INSERT INTO runs (automation_slug, user_id, status, sheet_name, sheet_path, options, total, queued_at, not_before)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (slug, user["id"], "queued", staged.name, "", json.dumps(options), check["total"], utcnow(), HOLD)).lastrowid
        conn.execute("INSERT INTO run_events (run_id, ts, level, kind, message) VALUES (?,?,?,?,?)",
                     (run_id, utcnow(), "info", "queued", f"Queued by {user['email']}: {staged.name}, {check['total']} rows"))
        log_activity(conn, user=user, action="run_queued", target_type="run", target_id=run_id,
                     details={"automation": slug, "sheet": staged.name, "rows": check["total"],
                              "save": body.save, "workers": workers},
                     ip=client_ip(request, settings.trust_proxy))

    # Each run works on its own copy: re-uploading the same file name can never change a queued run.
    paths = run_paths(settings, run_id)
    try:
        paths["input_dir"].mkdir(parents=True, exist_ok=True)
        copy = paths["input_dir"] / staged.name
        copy.write_bytes(staged.read_bytes())
        with ctx.db.session() as conn:
            conn.execute("UPDATE runs SET sheet_path=? WHERE id=?", (str(copy), run_id))
    except OSError:
        with ctx.db.session() as conn:
            conn.execute("UPDATE runs SET status='failed', failure_kind='engine_error', finished_at=?, error=? WHERE id=?",
                         (utcnow(), "Could not store the sheet for this run.", run_id))
        raise HTTPException(500, "Could not store the sheet for this run.")

    ctx.jobs.submit(run_id, slug)
    return {"run_id": run_id, "total": check["total"], "workers": workers,
            "queue_position": ctx.jobs.queue_position(run_id, slug)}


# ------------------------------------------------------------------ Office Ally login

class CredentialsBody(BaseModel):
    values: dict[str, str]


def _require_credential_rights(ctx, user, slug):
    automation = allowed_automation(ctx, user, slug)
    if not can_manage_credentials(ctx, user, slug):
        raise HTTPException(403, "You are not allowed to change this login. Ask an administrator.")
    return automation


@router.get("/{slug}/credentials")
def get_credentials(slug: str, user=Depends(require_user), ctx: Context = Depends(get_ctx)):
    automation = allowed_automation(ctx, user, slug)
    if not can_manage_credentials(ctx, user, slug):
        return {"can_edit": False}
    return {"can_edit": True, **creds.read_status(automation), "busy": ctx.jobs.slug_busy(slug)}


@router.put("/{slug}/credentials")
def update_credentials(slug: str, body: CredentialsBody, request: Request,
                       user=Depends(require_user), ctx: Context = Depends(get_ctx)):
    automation = _require_credential_rights(ctx, user, slug)
    if ctx.jobs.slug_busy(slug):
        raise HTTPException(409, "A run is in progress. Update the login after it finishes.")
    try:
        changed = creds.update(automation, body.values)
    except creds.CredentialError as exc:
        raise HTTPException(400, str(exc))
    with ctx.db.session() as conn:
        # which fields changed - never their values
        log_activity(conn, user=user, action="credentials_updated", target_type="automation", target_id=slug,
                     details={"fields": changed}, ip=client_ip(request, ctx.settings.trust_proxy))
    return {"ok": True, "changed": changed, **creds.read_status(automation)}


@router.post("/{slug}/credentials/test")
def test_credentials(slug: str, request: Request, user=Depends(require_user), ctx: Context = Depends(get_ctx)):
    """A real Office Ally sign-in with the saved login. Nothing is created or changed there."""
    automation = _require_credential_rights(ctx, user, slug)
    if ctx.jobs.slug_busy(slug):
        raise HTTPException(409, "A run is in progress. Test the login after it finishes.")
    lock = _login_test_locks.setdefault(slug, threading.Lock())
    if not lock.acquire(blocking=False):
        raise HTTPException(409, "A login test is already running.")
    try:
        result = ctx.jobs.check_login(automation)
    finally:
        lock.release()
    ok = result.get("type") == "login_check" and bool(result.get("ok"))
    with ctx.db.session() as conn:
        log_activity(conn, user=user, action="credentials_tested", target_type="automation", target_id=slug,
                     details={"ok": ok}, ip=client_ip(request, ctx.settings.trust_proxy))
    if ok:
        return {"ok": True, "message": "Signed in to Office Ally successfully."}
    if result.get("type") != "login_check":
        return {"ok": False, "message": "The test could not run on the server. See the server log."}
    # Reached the login page but did not get in: say what the page showed (no credentials are ever in this text).
    pages = [{k: v for k, v in p.items() if k != "screenshot"} for p in result.get("pages", [])]
    message = ("Office Ally did not accept the login. Check the username and password."
               if not result.get("error") else "The sign-in did not complete. The page showed:")
    return {"ok": False, "message": message, "error": result.get("error"), "pages": pages}
