"""Command line helpers.

    python -m app.cli create-admin you@example.com        (asks for the password)
    python -m app.cli list-users
"""

import getpass
import logging
import sys

from .config import get_settings
from .db import Database, init_db, utcnow
from .deps import Context
from .security import hash_password, normalize_email, password_problem, valid_email
from .services.registry import Registry

log = logging.getLogger("automation.cli")


def _context() -> Context:
    settings = get_settings()
    init_db(settings.db_path)
    return Context(settings=settings, db=Database(settings.db_path), registry=Registry(settings.automations_dir))


def create_admin(ctx: Context, email: str, password: str, enforce_policy: bool = True) -> int:
    """`enforce_policy=False` is for the operator's own bootstrap password in
    the server's .env: the owner of the server decides it, and a weak one must
    never stop the site from starting."""
    email = normalize_email(email)
    if not valid_email(email):
        raise SystemExit("That is not a valid email address.")
    problem = password_problem(password) if enforce_policy else None
    if problem:
        raise SystemExit(problem)
    with ctx.db.transaction() as conn:
        existing = conn.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
        if existing:
            conn.execute("UPDATE users SET role='admin', status='active', password_hash=?,"
                         " failed_attempts=0, locked_until=NULL WHERE id=?",
                         (hash_password(password), existing["id"]))
            user_id = existing["id"]
        else:
            user_id = conn.execute(
                "INSERT INTO users (email, name, password_hash, role, status, created_at, approved_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (email, email.split("@")[0], hash_password(password), "admin", "active",
                 utcnow(), utcnow())).lastrowid
        conn.execute("INSERT INTO activity_logs (user_id, actor_email, action, target_type, target_id, created_at)"
                     " VALUES (?,?,?,?,?,?)", (user_id, email, "admin_created", "user", str(user_id), utcnow()))
    return user_id


def ensure_bootstrap_admin(ctx: Context) -> None:
    """If no admin exists yet and AUTOMATION_ADMIN_EMAIL/_PASSWORD are set, create one."""
    s = ctx.settings
    if not (s.bootstrap_admin_email and s.bootstrap_admin_password):
        return
    with ctx.db.session() as conn:
        has_admin = conn.execute("SELECT 1 FROM users WHERE role='admin' LIMIT 1").fetchone()
    if has_admin:
        return
    try:
        create_admin(ctx, s.bootstrap_admin_email, s.bootstrap_admin_password, enforce_policy=False)
        log.warning("First admin created: %s - remove AUTOMATION_ADMIN_PASSWORD from .env now.",
                    s.bootstrap_admin_email)
    except SystemExit as exc:                       # bad email in .env: report, keep the site up
        log.error("Could not create the first admin from .env: %s", exc)


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) >= 2 and argv[0] == "create-admin":
        password = getpass.getpass("Password: ")
        if password != getpass.getpass("Repeat password: "):
            raise SystemExit("Passwords do not match.")
        user_id = create_admin(_context(), argv[1], password)
        print(f"Admin ready: {normalize_email(argv[1])} (id {user_id})")
        return 0
    if argv == ["list-users"]:
        ctx = _context()
        with ctx.db.session() as conn:
            for r in conn.execute("SELECT id,email,role,status,last_login_at FROM users ORDER BY id"):
                print(f"{r['id']:>3}  {r['email']:<34} {r['role']:<6} {r['status']:<9} {r['last_login_at'] or '-'}")
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main())
