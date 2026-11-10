"""Admin dashboard: overview, users, login monitor, activity log, sessions."""

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from ..db import utcnow
from ..deps import Context, get_ctx, require_admin
from ..security import hash_password, password_problem
from ..services.audit import client_ip, log_activity

router = APIRouter(prefix="/api/admin", tags=["admin"])


def _days_ago(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _today_start() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00Z")


def _get_user(conn, user_id: int):
    row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    if not row:
        raise HTTPException(404, "User not found.")
    return row


def _revoke_sessions(conn, user_id: int):
    conn.execute("UPDATE sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL",
                 (utcnow(), user_id))


def _set_access(conn, user_id: int, slugs, granted_by: int):
    conn.execute("DELETE FROM user_automations WHERE user_id=?", (user_id,))
    for slug in dict.fromkeys(slugs):
        conn.execute("INSERT INTO user_automations (user_id, automation_slug, granted_at, granted_by)"
                     " VALUES (?,?,?,?)", (user_id, slug, utcnow(), granted_by))


def _check_slugs(ctx: Context, slugs):
    unknown = [s for s in slugs if ctx.registry.get(s) is None]
    if unknown:
        raise HTTPException(400, f"Unknown automation: {', '.join(unknown)}")


# ----------------------------------------------------------------------------- overview

@router.get("/overview")
def overview(admin=Depends(require_admin), ctx: Context = Depends(get_ctx)):
    now = utcnow()
    today = _today_start()
    with ctx.db.session() as conn:
        users = {r["status"]: r["n"] for r in conn.execute(
            "SELECT status, COUNT(*) AS n FROM users GROUP BY status")}
        logins_today = conn.execute(
            "SELECT COALESCE(SUM(success),0) AS ok, COALESCE(SUM(1-success),0) AS failed"
            " FROM login_events WHERE created_at>=?", (today,)).fetchone()
        unique_today = conn.execute(
            "SELECT COUNT(DISTINCT user_id) AS n FROM login_events WHERE success=1 AND created_at>=?",
            (today,)).fetchone()["n"]
        active_sessions = conn.execute(
            "SELECT COUNT(*) AS n FROM sessions WHERE revoked_at IS NULL AND expires_at>?", (now,)).fetchone()["n"]
        online_now = conn.execute(
            "SELECT COUNT(DISTINCT user_id) AS n FROM sessions WHERE revoked_at IS NULL AND expires_at>? AND last_seen_at>?",
            (now, (datetime.now(timezone.utc) - timedelta(minutes=15)).strftime("%Y-%m-%dT%H:%M:%SZ"))
        ).fetchone()["n"]
        series = conn.execute(
            "SELECT substr(created_at,1,10) AS day, SUM(success) AS ok, SUM(1-success) AS failed"
            " FROM login_events WHERE created_at>=? GROUP BY day ORDER BY day", (_days_ago(13),)).fetchall()
        runs_by_status = {r["status"]: r["n"] for r in conn.execute(
            "SELECT status, COUNT(*) AS n FROM runs WHERE queued_at>=? GROUP BY status", (today,))}
        live_runs = conn.execute(
            "SELECT r.id, r.automation_slug, r.status, r.sheet_name, r.total, r.started_at, u.email AS user_email"
            " FROM runs r JOIN users u ON u.id=r.user_id WHERE r.status IN ('running','queued')"
            " ORDER BY r.id").fetchall()
        recent_failed = conn.execute(
            "SELECT email, reason, ip, created_at FROM login_events WHERE success=0"
            " ORDER BY id DESC LIMIT 8").fetchall()
        pending = conn.execute(
            "SELECT id, name, email, created_at FROM users WHERE status='pending' ORDER BY id").fetchall()

    # fill days with no logins so the chart has no gaps
    by_day = {r["day"]: r for r in series}
    chart = []
    for offset in range(13, -1, -1):
        day = (datetime.now(timezone.utc) - timedelta(days=offset)).strftime("%Y-%m-%d")
        row = by_day.get(day)
        chart.append({"day": day, "ok": int(row["ok"]) if row else 0,
                      "failed": int(row["failed"]) if row else 0})
    return {
        "users": {"total": sum(users.values()), "pending": users.get("pending", 0),
                  "active": users.get("active", 0), "disabled": users.get("disabled", 0)},
        "logins_today": {"ok": int(logins_today["ok"]), "failed": int(logins_today["failed"]),
                         "unique_users": unique_today},
        "active_sessions": active_sessions, "online_now": online_now,
        "login_chart": chart, "runs_today": runs_by_status,
        "live_runs": [dict(r) for r in live_runs],
        "recent_failed_logins": [dict(r) for r in recent_failed],
        "pending_users": [dict(r) for r in pending],
    }


# ----------------------------------------------------------------------------- users

@router.get("/users")
def users(admin=Depends(require_admin), ctx: Context = Depends(get_ctx),
          status: str | None = None, q: str | None = None):
    where, args = [], []
    if status in ("pending", "active", "disabled"):
        where.append("u.status=?"); args.append(status)
    if q:
        where.append("(u.email LIKE ? OR u.name LIKE ?)"); args += [f"%{q}%", f"%{q}%"]
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    with ctx.db.session() as conn:
        rows = conn.execute(
            "SELECT u.id,u.email,u.name,u.role,u.status,u.created_at,u.approved_at,u.last_login_at,u.locked_until,"
            " (SELECT COUNT(*) FROM login_events l WHERE l.user_id=u.id AND l.success=1) AS login_count,"
            " (SELECT COUNT(*) FROM runs r WHERE r.user_id=u.id) AS run_count,"
            " (SELECT COUNT(*) FROM sessions s WHERE s.user_id=u.id AND s.revoked_at IS NULL AND s.expires_at>?) AS open_sessions"
            f" FROM users u {clause} ORDER BY (u.status='pending') DESC, u.created_at DESC", (utcnow(), *args)).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["automations"] = [r["automation_slug"] for r in conn.execute(
                "SELECT automation_slug FROM user_automations WHERE user_id=?", (row["id"],))]
            out.append(item)
    return {"users": out, "all_automations": [a.public() for a in ctx.registry.all()]}


class ApproveBody(BaseModel):
    automations: list[str] | None = None      # None = every automation


@router.post("/users/{user_id}/approve")
def approve(user_id: int, body: ApproveBody, request: Request,
            admin=Depends(require_admin), ctx: Context = Depends(get_ctx)):
    slugs = body.automations if body.automations is not None else ctx.registry.slugs()
    _check_slugs(ctx, slugs)
    with ctx.db.transaction() as conn:
        user = _get_user(conn, user_id)
        conn.execute("UPDATE users SET status='active', approved_at=?, approved_by=?, failed_attempts=0,"
                     " locked_until=NULL WHERE id=?", (utcnow(), admin["id"], user_id))
        _set_access(conn, user_id, slugs, admin["id"])
        log_activity(conn, user=admin, action="user_approved", target_type="user", target_id=user_id,
                     details={"email": user["email"], "automations": slugs},
                     ip=client_ip(request, ctx.settings.trust_proxy))
    return {"ok": True}


@router.post("/users/{user_id}/disable")
def disable(user_id: int, request: Request, admin=Depends(require_admin),
            ctx: Context = Depends(get_ctx)):
    if user_id == admin["id"]:
        raise HTTPException(400, "You can't disable your own account.")
    with ctx.db.transaction() as conn:
        user = _get_user(conn, user_id)
        conn.execute("UPDATE users SET status='disabled' WHERE id=?", (user_id,))
        _revoke_sessions(conn, user_id)
        log_activity(conn, user=admin, action="user_disabled", target_type="user", target_id=user_id,
                     details={"email": user["email"]}, ip=client_ip(request, ctx.settings.trust_proxy))
    return {"ok": True}


@router.post("/users/{user_id}/enable")
def enable(user_id: int, request: Request, admin=Depends(require_admin),
           ctx: Context = Depends(get_ctx)):
    with ctx.db.transaction() as conn:
        user = _get_user(conn, user_id)
        conn.execute("UPDATE users SET status='active', failed_attempts=0, locked_until=NULL,"
                     " approved_at=COALESCE(approved_at, ?), approved_by=COALESCE(approved_by, ?) WHERE id=?",
                     (utcnow(), admin["id"], user_id))
        log_activity(conn, user=admin, action="user_enabled", target_type="user", target_id=user_id,
                     details={"email": user["email"]}, ip=client_ip(request, ctx.settings.trust_proxy))
    return {"ok": True}


class AccessBody(BaseModel):
    automations: list[str]


@router.put("/users/{user_id}/access")
def set_access(user_id: int, body: AccessBody, request: Request,
               admin=Depends(require_admin), ctx: Context = Depends(get_ctx)):
    _check_slugs(ctx, body.automations)
    with ctx.db.transaction() as conn:
        user = _get_user(conn, user_id)
        _set_access(conn, user_id, body.automations, admin["id"])
        log_activity(conn, user=admin, action="access_changed", target_type="user", target_id=user_id,
                     details={"email": user["email"], "automations": body.automations},
                     ip=client_ip(request, ctx.settings.trust_proxy))
    return {"ok": True}


class RoleBody(BaseModel):
    role: str = Field(pattern="^(admin|user)$")


@router.put("/users/{user_id}/role")
def set_role(user_id: int, body: RoleBody, request: Request,
             admin=Depends(require_admin), ctx: Context = Depends(get_ctx)):
    with ctx.db.transaction() as conn:
        user = _get_user(conn, user_id)
        if user["role"] == "admin" and body.role == "user":
            admins = conn.execute("SELECT COUNT(*) AS n FROM users WHERE role='admin' AND status='active'").fetchone()["n"]
            if admins <= 1:
                raise HTTPException(400, "There must always be at least one active admin.")
        conn.execute("UPDATE users SET role=? WHERE id=?", (body.role, user_id))
        log_activity(conn, user=admin, action="role_changed", target_type="user", target_id=user_id,
                     details={"email": user["email"], "role": body.role},
                     ip=client_ip(request, ctx.settings.trust_proxy))
    return {"ok": True}


class ResetBody(BaseModel):
    password: str = Field(max_length=200)


@router.post("/users/{user_id}/reset-password")
def reset_password(user_id: int, body: ResetBody, request: Request,
                   admin=Depends(require_admin), ctx: Context = Depends(get_ctx)):
    problem = password_problem(body.password)
    if problem:
        raise HTTPException(400, problem)
    with ctx.db.transaction() as conn:
        user = _get_user(conn, user_id)
        conn.execute("UPDATE users SET password_hash=?, failed_attempts=0, locked_until=NULL WHERE id=?",
                     (hash_password(body.password), user_id))
        _revoke_sessions(conn, user_id)
        log_activity(conn, user=admin, action="password_reset", target_type="user", target_id=user_id,
                     details={"email": user["email"]}, ip=client_ip(request, ctx.settings.trust_proxy))
    return {"ok": True}


@router.post("/users/{user_id}/unlock")
def unlock(user_id: int, request: Request, admin=Depends(require_admin),
           ctx: Context = Depends(get_ctx)):
    with ctx.db.transaction() as conn:
        user = _get_user(conn, user_id)
        conn.execute("UPDATE users SET failed_attempts=0, locked_until=NULL WHERE id=?", (user_id,))
        log_activity(conn, user=admin, action="user_unlocked", target_type="user", target_id=user_id,
                     details={"email": user["email"]}, ip=client_ip(request, ctx.settings.trust_proxy))
    return {"ok": True}


# ----------------------------------------------------------------------------- logs

@router.get("/logins")
def logins(admin=Depends(require_admin), ctx: Context = Depends(get_ctx),
           user_id: int | None = None, success: bool | None = None, since_days: int | None = Query(None, ge=1, le=365),
           limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)):
    where, args = [], []
    if user_id:
        where.append("l.user_id=?"); args.append(user_id)
    if success is not None:
        where.append("l.success=?"); args.append(1 if success else 0)
    if since_days:
        where.append("l.created_at>=?"); args.append(_days_ago(since_days))
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    with ctx.db.session() as conn:
        total = conn.execute(f"SELECT COUNT(*) AS n FROM login_events l {clause}", args).fetchone()["n"]
        rows = conn.execute(
            "SELECT l.id,l.user_id,l.email,l.success,l.reason,l.ip,l.user_agent,l.created_at,u.name"
            f" FROM login_events l LEFT JOIN users u ON u.id=l.user_id {clause}"
            " ORDER BY l.id DESC LIMIT ? OFFSET ?", (*args, limit, offset)).fetchall()
    return {"total": total, "events": [dict(r) for r in rows]}


@router.get("/activity")
def activity(admin=Depends(require_admin), ctx: Context = Depends(get_ctx),
             user_id: int | None = None, action: str | None = None, q: str | None = None,
             limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)):
    where, args = [], []
    if user_id:
        where.append("a.user_id=?"); args.append(user_id)
    if action:
        where.append("a.action=?"); args.append(action)
    if q:
        where.append("(a.actor_email LIKE ? OR a.details LIKE ? OR a.target_id LIKE ?)")
        args += [f"%{q}%"] * 3
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    with ctx.db.session() as conn:
        total = conn.execute(f"SELECT COUNT(*) AS n FROM activity_logs a {clause}", args).fetchone()["n"]
        rows = conn.execute(
            "SELECT a.id,a.user_id,a.actor_email,a.action,a.target_type,a.target_id,a.details,a.ip,a.created_at"
            f" FROM activity_logs a {clause} ORDER BY a.id DESC LIMIT ? OFFSET ?",
            (*args, limit, offset)).fetchall()
        actions = [r["action"] for r in conn.execute("SELECT DISTINCT action FROM activity_logs ORDER BY action")]
    return {"total": total, "events": [dict(r) for r in rows], "actions": actions}


@router.get("/sessions")
def sessions(admin=Depends(require_admin), ctx: Context = Depends(get_ctx)):
    with ctx.db.session() as conn:
        rows = conn.execute(
            "SELECT s.id, s.user_id, u.email, u.name, s.created_at, s.last_seen_at, s.expires_at, s.ip, s.user_agent"
            " FROM sessions s JOIN users u ON u.id=s.user_id"
            " WHERE s.revoked_at IS NULL AND s.expires_at>? ORDER BY s.last_seen_at DESC", (utcnow(),)).fetchall()
    return {"sessions": [dict(r) for r in rows]}


@router.post("/sessions/{session_id}/revoke")
def revoke_session(session_id: str, request: Request, admin=Depends(require_admin),
                   ctx: Context = Depends(get_ctx)):
    with ctx.db.transaction() as conn:
        row = conn.execute("SELECT user_id FROM sessions WHERE id=?", (session_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Session not found.")
        conn.execute("UPDATE sessions SET revoked_at=? WHERE id=?", (utcnow(), session_id))
        log_activity(conn, user=admin, action="session_revoked", target_type="user", target_id=row["user_id"],
                     ip=client_ip(request, ctx.settings.trust_proxy))
    return {"ok": True}
