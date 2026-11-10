"""A signed-in person's own log and password."""

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from ..db import utcnow
from ..deps import Context, get_ctx, require_user
from ..security import hash_password, password_problem, verify_password
from ..services.audit import client_ip, log_activity

router = APIRouter(prefix="/api/account", tags=["account"])


@router.get("/activity")
def my_activity(user=Depends(require_user), ctx: Context = Depends(get_ctx),
                limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)):
    with ctx.db.session() as conn:
        total = conn.execute("SELECT COUNT(*) AS n FROM activity_logs WHERE user_id=?", (user["id"],)).fetchone()["n"]
        rows = conn.execute(
            "SELECT id, action, target_type, target_id, details, ip, created_at FROM activity_logs"
            " WHERE user_id=? ORDER BY id DESC LIMIT ? OFFSET ?", (user["id"], limit, offset)).fetchall()
    return {"total": total, "events": [dict(r) for r in rows]}


class PasswordBody(BaseModel):
    current_password: str = Field(max_length=200)
    new_password: str = Field(max_length=200)


@router.post("/password")
def change_password(body: PasswordBody, request: Request, user=Depends(require_user),
                    ctx: Context = Depends(get_ctx)):
    if not verify_password(body.current_password, user["password_hash"]):
        raise HTTPException(400, "Your current password is not correct.")
    problem = password_problem(body.new_password)
    if problem:
        raise HTTPException(400, problem)
    keep = getattr(request.state, "session_id", None)
    with ctx.db.transaction() as conn:
        conn.execute("UPDATE users SET password_hash=? WHERE id=?", (hash_password(body.new_password), user["id"]))
        # Sign out every other device; this one stays signed in.
        conn.execute("UPDATE sessions SET revoked_at=? WHERE user_id=? AND id<>? AND revoked_at IS NULL",
                     (utcnow(), user["id"], keep or ""))
        log_activity(conn, user=user, action="password_changed", ip=client_ip(request, ctx.settings.trust_proxy))
    return {"ok": True}
