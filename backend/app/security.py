"""Passwords, session tokens, and input checks."""

import hashlib
import re
import secrets

import bcrypt

MIN_PASSWORD = 10
MAX_PASSWORD = 72          # bcrypt only looks at the first 72 bytes
_EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s]{2,}$")

# A real hash to compare against when the email is unknown, so a login for a
# missing account takes as long as one for a real account (no user probing).
_DUMMY_HASH = bcrypt.hashpw(b"not-a-real-password", bcrypt.gensalt(rounds=12))


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def valid_email(email: str) -> bool:
    return bool(_EMAIL_RE.match(email or "")) and len(email) <= 254


def password_problem(password: str) -> str | None:
    """None when fine, otherwise the sentence to show the person."""
    if not isinstance(password, str):
        return "Password is required."
    if len(password.encode("utf-8")) > MAX_PASSWORD:
        return f"Password must be at most {MAX_PASSWORD} bytes."
    if len(password) < MIN_PASSWORD:
        return f"Password must be at least {MIN_PASSWORD} characters."
    if password.lower() == password or password.upper() == password:
        return "Password needs both upper and lower case letters."
    if not any(c.isdigit() for c in password):
        return "Password needs at least one number."
    return None


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("ascii")


def verify_password(password: str, stored_hash: str | None) -> bool:
    """Constant-ish time whether or not the account exists."""
    try:
        target = stored_hash.encode("ascii") if stored_hash else _DUMMY_HASH
        ok = bcrypt.checkpw((password or "").encode("utf-8")[:MAX_PASSWORD], target)
    except ValueError:
        return False
    return ok and stored_hash is not None


def new_session_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """Only the hash is stored, so a copy of the database can't be replayed
    as a live session."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
