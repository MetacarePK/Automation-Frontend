"""Sign up, sign in, sign out, who am I."""

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from ..db import utcnow
from ..deps import (SESSION_COOKIE, Context, get_ctx, optional_user, require_user,
                    user_automation_slugs)
from ..security import (hash_password, hash_token, new_session_token, normalize_email,
                        password_problem, valid_email, verify_password)
from ..services.audit import client_ip, log_activity, log_login, user_agent

router = APIRouter(prefix="/api/auth", tags=["auth"])

SIGNUP_MESSAGE = "Request sent. You can sign in once an administrator approves it."


class SignupBody(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=254)
    password: str = Field(max_length=200)


class LoginBody(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(max_length=200)


def public_user(conn, user) -> dict:
    return {
        "id": user["id"], "email": user["email"], "name": user["name"],
        "role": user["role"], "status": user["status"],
        "automations": user_automation_slugs(conn, user),
    }


def _iso(delta: timedelta) -> str:
    return (datetime.now(timezone.utc) + delta).strftime("%Y-%m-%dT%H:%M:%SZ")


@router.post("/signup", status_code=201)
def signup(body: SignupBody, request: Request, ctx: Context = Depends(get_ctx)):
    email = normalize_email(body.email)
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "Please enter your name.")
    if not valid_email(email):
        raise HTTPException(400, "Please enter a valid email address.")
    problem = password_problem(body.password)
    if problem:
        raise HTTPException(400, problem)

    ip = client_ip(request, ctx.settings.trust_proxy)
    with ctx.db.transaction() as conn:
        existing = conn.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
        if existing:
            # Same answer as a fresh signup, so the form can't be used to find
            # out who already has an account.
            log_activity(conn, action="signup_duplicate", actor_email=email, ip=ip)
            return {"message": SIGNUP_MESSAGE}
        status = "active" if ctx.settings.auto_approve_signups else "pending"
        cur = conn.execute(
            "INSERT INTO users (email, name, password_hash, role, status, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (email, name, hash_password(body.password), "user", status, utcnow()))
        new_id = cur.lastrowid
        if status == "active":
            for slug in ctx.registry.slugs():
                conn.execute("INSERT INTO user_automations VALUES (?,?,?,NULL)", (new_id, slug, utcnow()))
        log_activity(conn, action="signup", actor_email=email, target_type="user",
                     target_id=new_id, details={"status": status}, ip=ip)
    return {"message": SIGNUP_MESSAGE}


@router.post("/login")
def login(body: LoginBody, request: Request, response: Response, ctx: Context = Depends(get_ctx)):
    email = normalize_email(body.email)
    ip = client_ip(request, ctx.settings.trust_proxy)
    agent = user_agent(request)
    s = ctx.settings

    with ctx.db.session() as conn:
        # Spraying one IP across many accounts: cap failures per IP too.
        recent_ip_fails = conn.execute(
            "SELECT COUNT(*) AS n FROM login_events WHERE ip=? AND success=0 AND created_at>?",
            (ip, _iso(-timedelta(minutes=s.lockout_minutes)))).fetchone()["n"]
        if recent_ip_fails >= s.max_failed_logins * 6:
            log_login(conn, email=email, success=False, reason="ip_blocked", ip=ip, agent=agent)
            raise HTTPException(429, "Too many failed attempts. Try again later.")

        user = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()

        if user and user["locked_until"] and user["locked_until"] > utcnow():
            log_login(conn, email=email, success=False, reason="locked", user_id=user["id"], ip=ip, agent=agent)
            log_activity(conn, user=user, action="login_blocked_locked", ip=ip)
            raise HTTPException(429, "Too many failed attempts. Try again later.")

        # Always run the password check so unknown emails cost the same time.
        ok = verify_password(body.password, user["password_hash"] if user else None)

        if not ok:
            reason = "bad_password" if user else "unknown_email"
            if user:
                attempts = user["failed_attempts"] + 1
                locked_until = None
                if attempts >= s.max_failed_logins:
                    locked_until = _iso(timedelta(minutes=s.lockout_minutes))
                    attempts = 0
                    log_activity(conn, user=user, action="account_locked", ip=ip,
                                 details={"minutes": s.lockout_minutes})
                conn.execute("UPDATE users SET failed_attempts=?, locked_until=? WHERE id=?",
                             (attempts, locked_until, user["id"]))
            log_login(conn, email=email, success=False, reason=reason,
                      user_id=user["id"] if user else None, ip=ip, agent=agent)
            log_activity(conn, user=user, action="login_failed", actor_email=email, ip=ip,
                         details={"reason": reason})
            raise HTTPException(401, "Incorrect email or password.")

        # Correct password from here on, so it is safe to say why we refuse.
        if user["status"] == "pending":
            log_login(conn, email=email, success=False, reason="pending", user_id=user["id"], ip=ip, agent=agent)
            raise HTTPException(403, "Your account is waiting for administrator approval.")
        if user["status"] == "disabled":
            log_login(conn, email=email, success=False, reason="disabled", user_id=user["id"], ip=ip, agent=agent)
            raise HTTPException(403, "This account has been disabled. Contact an administrator.")

        token = new_session_token()
        conn.execute(
            "INSERT INTO sessions (id, user_id, created_at, last_seen_at, expires_at, ip, user_agent)"
            " VALUES (?,?,?,?,?,?,?)",
            (hash_token(token), user["id"], utcnow(), utcnow(),
             _iso(timedelta(hours=s.session_hours)), ip, agent))
        conn.execute("UPDATE users SET failed_attempts=0, locked_until=NULL, last_login_at=? WHERE id=?",
                     (utcnow(), user["id"]))
        log_login(conn, email=email, success=True, user_id=user["id"], ip=ip, agent=agent)
        log_activity(conn, user=user, action="login", ip=ip)
        payload = public_user(conn, user)

    response.set_cookie(
        SESSION_COOKIE, token, max_age=s.session_hours * 3600, httponly=True,
        samesite="lax", secure=s.cookie_secure, path="/")
    return payload


@router.post("/logout")
def logout(request: Request, response: Response, user=Depends(optional_user),
           ctx: Context = Depends(get_ctx)):
    sid = getattr(request.state, "session_id", None)
    if user and sid:
        with ctx.db.session() as conn:
            conn.execute("UPDATE sessions SET revoked_at=? WHERE id=?", (utcnow(), sid))
            log_activity(conn, user=user, action="logout",
                         ip=client_ip(request, ctx.settings.trust_proxy))
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@router.get("/me")
def me(user=Depends(require_user), ctx: Context = Depends(get_ctx)):
    with ctx.db.session() as conn:
        return public_user(conn, user)
