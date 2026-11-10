"""Audit trail and login monitor writes. Every write is best-effort safe:
a logging failure must never break the action being logged."""

import json
import logging

from ..db import utcnow

log = logging.getLogger("automation.audit")


def client_ip(request, trust_proxy: bool) -> str:
    if trust_proxy:
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",")[0].strip()[:64]
    return (request.client.host if request.client else "")[:64]


def user_agent(request) -> str:
    return request.headers.get("user-agent", "")[:300]


def log_activity(conn, *, user=None, action, target_type=None, target_id=None,
                 details=None, ip=None, actor_email=None):
    """`user` is a row/dict with id and email, or None for anonymous events."""
    try:
        conn.execute(
            "INSERT INTO activity_logs (user_id, actor_email, action, target_type, target_id, details, ip, created_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (user["id"] if user else None,
             actor_email if actor_email is not None else (user["email"] if user else None),
             action, target_type, None if target_id is None else str(target_id),
             json.dumps(details, default=str) if details else None, ip, utcnow()))
    except Exception:  # pragma: no cover - logging must not break the request
        log.exception("could not write activity log for %s", action)


def log_login(conn, *, email, success, reason=None, user_id=None, ip=None, agent=None):
    try:
        conn.execute(
            "INSERT INTO login_events (user_id, email, success, reason, ip, user_agent, created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (user_id, email[:254], 1 if success else 0, reason, ip, agent, utcnow()))
    except Exception:  # pragma: no cover
        log.exception("could not write login event")
