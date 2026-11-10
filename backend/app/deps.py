"""Shared request plumbing: app context, current user, role checks, CSRF guard."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from fastapi import Depends, HTTPException, Request

from .config import Settings
from .db import Database, utcnow
from .security import hash_token
from .services.registry import Registry

SESSION_COOKIE = "session"
UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}


@dataclass
class Context:
    settings: Settings
    db: Database
    registry: Registry
    jobs: object = None     # JobManager, set by main.create_app


def get_ctx(request: Request) -> Context:
    return request.app.state.ctx


def _iso_plus(hours: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_session_user(ctx: Context, token: str | None):
    """(user_row, session_id) for a live session, else (None, None)."""
    if not token:
        return None, None
    sid = hash_token(token)
    with ctx.db.session() as conn:
        row = conn.execute(
            "SELECT s.id AS sid, s.expires_at, s.last_seen_at, u.* FROM sessions s"
            " JOIN users u ON u.id = s.user_id"
            " WHERE s.id = ? AND s.revoked_at IS NULL", (sid,)).fetchone()
        if not row or row["expires_at"] <= utcnow() or row["status"] != "active":
            return None, None
        # Sliding expiry, written at most every 5 minutes to keep writes rare.
        if row["last_seen_at"] < _iso_plus(-5 / 60):
            conn.execute("UPDATE sessions SET last_seen_at=?, expires_at=? WHERE id=?",
                         (utcnow(), _iso_plus(ctx.settings.session_hours), sid))
        return row, sid


def optional_user(request: Request, ctx: Context = Depends(get_ctx)):
    user, sid = load_session_user(ctx, request.cookies.get(SESSION_COOKIE))
    request.state.session_id = sid
    return user


def require_user(user=Depends(optional_user)):
    if user is None:
        raise HTTPException(status_code=401, detail="Please sign in.")
    return user


def require_admin(user=Depends(require_user)):
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Admins only.")
    return user


def user_automation_slugs(conn, user) -> list[str]:
    rows = conn.execute("SELECT automation_slug FROM user_automations WHERE user_id=?",
                        (user["id"],)).fetchall()
    return [r["automation_slug"] for r in rows]


def check_origin(request: Request, ctx: Context):
    """CSRF defence on top of SameSite=Lax cookies: a browser sends Origin on
    every cross-site POST, and it must be this site (or an allowed one)."""
    if request.method not in UNSAFE:
        return
    origin = request.headers.get("origin")
    if not origin:
        return    # same-origin fetches from some clients omit it; Lax cookies still cover those
    host = urlparse(origin).netloc
    allowed = {request.headers.get("host", "")}
    allowed |= {urlparse(o).netloc for o in ctx.settings.allowed_origins}
    # Behind a reverse proxy the Host header is the public domain; also accept forwarded host.
    fwd = request.headers.get("x-forwarded-host")
    if fwd and ctx.settings.trust_proxy:
        allowed.add(fwd.split(",")[0].strip())
    if host not in allowed:
        raise HTTPException(status_code=403, detail="Cross-site request blocked.")
