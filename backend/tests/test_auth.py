from datetime import datetime, timedelta, timezone

from conftest import PASSWORD, add_user, login


def test_signup_creates_pending_account_that_cannot_sign_in(client, app):
    r = client.post("/api/auth/signup", json={"name": "Ann", "email": "Ann@Example.com", "password": PASSWORD})
    assert r.status_code == 201
    r = client.post("/api/auth/login", json={"email": "ann@example.com", "password": PASSWORD})
    assert r.status_code == 403 and "approval" in r.json()["detail"]
    assert client.get("/api/auth/me").status_code == 401


def test_duplicate_signup_looks_identical_so_emails_cannot_be_probed(client):
    body = {"name": "Ann", "email": "ann@example.com", "password": PASSWORD}
    first = client.post("/api/auth/signup", json=body)
    second = client.post("/api/auth/signup", json=body)
    assert first.status_code == second.status_code == 201 and first.json() == second.json()


def test_weak_passwords_and_bad_emails_rejected(client):
    for pw in ("short1A", "alllowercase123", "ALLUPPERCASE123", "NoNumbersHere"):
        assert client.post("/api/auth/signup", json={"name": "A", "email": "a@b.co", "password": pw}).status_code == 400
    assert client.post("/api/auth/signup", json={"name": "A", "email": "not-an-email", "password": PASSWORD}).status_code == 400


def test_login_sets_httponly_cookie_and_me_works(client, ctx):
    add_user(ctx, "u@example.com")
    r = client.post("/api/auth/login", json={"email": "u@example.com", "password": PASSWORD})
    assert r.status_code == 200
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie
    me = client.get("/api/auth/me").json()
    assert me["email"] == "u@example.com" and me["automations"] == ["fake-auto"]


def test_session_token_is_stored_hashed(client, ctx):
    add_user(ctx, "u@example.com")
    client.post("/api/auth/login", json={"email": "u@example.com", "password": PASSWORD})
    token = client.cookies.get("session")
    with ctx.db.session() as conn:
        ids = [r["id"] for r in conn.execute("SELECT id FROM sessions")]
    assert token and token not in ids and len(ids[0]) == 64


def test_wrong_password_and_unknown_email_get_same_answer(client, ctx):
    add_user(ctx, "u@example.com")
    a = client.post("/api/auth/login", json={"email": "u@example.com", "password": "Wrong-Password1"})
    b = client.post("/api/auth/login", json={"email": "nobody@example.com", "password": "Wrong-Password1"})
    assert a.status_code == b.status_code == 401 and a.json() == b.json()


def test_account_locks_after_repeated_failures_even_with_right_password(client, ctx):
    add_user(ctx, "u@example.com")
    for _ in range(5):
        assert client.post("/api/auth/login", json={"email": "u@example.com", "password": "Wrong-Password1"}).status_code == 401
    r = client.post("/api/auth/login", json={"email": "u@example.com", "password": PASSWORD})
    assert r.status_code == 429


def test_logout_kills_the_session(client, ctx):
    add_user(ctx, "u@example.com")
    client.post("/api/auth/login", json={"email": "u@example.com", "password": PASSWORD})
    token = client.cookies.get("session")
    client.post("/api/auth/logout")
    client.cookies.set("session", token)          # replay the old cookie
    assert client.get("/api/auth/me").status_code == 401


def test_expired_session_is_rejected(client, ctx):
    add_user(ctx, "u@example.com")
    client.post("/api/auth/login", json={"email": "u@example.com", "password": PASSWORD})
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with ctx.db.session() as conn:
        conn.execute("UPDATE sessions SET expires_at=?", (past,))
    assert client.get("/api/auth/me").status_code == 401


def test_disabled_user_cannot_sign_in_and_login_is_recorded(client, ctx):
    add_user(ctx, "u@example.com", status="disabled")
    assert client.post("/api/auth/login", json={"email": "u@example.com", "password": PASSWORD}).status_code == 403
    with ctx.db.session() as conn:
        ev = conn.execute("SELECT success, reason FROM login_events").fetchone()
    assert (ev["success"], ev["reason"]) == (0, "disabled")


def test_cross_site_post_is_blocked(client, ctx):
    add_user(ctx, "u@example.com")
    r = client.post("/api/auth/login", json={"email": "u@example.com", "password": PASSWORD},
                    headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    ok = client.post("/api/auth/login", json={"email": "u@example.com", "password": PASSWORD},
                     headers={"Origin": "http://testserver"})
    assert ok.status_code == 200


def test_pages_are_gated_by_the_server(client, ctx):
    assert client.get("/", follow_redirects=False).headers["location"] == "/login"
    add_user(ctx, "u@example.com")
    client.post("/api/auth/login", json={"email": "u@example.com", "password": PASSWORD})
    assert client.get("/login", follow_redirects=False).headers["location"] == "/"
    assert client.get("/admin", follow_redirects=False).headers["location"] == "/"   # not an admin


def test_security_headers_present(client):
    r = client.get("/healthz")
    assert r.headers["x-frame-options"] == "DENY" and "default-src 'self'" in r.headers["content-security-policy"]


def test_env_file_is_loaded_and_real_environment_wins(tmp_path, isolated_env):
    from app.config import load_env_file
    f = tmp_path / ".env"
    f.write_text('# comment\nAUTOMATION_ADMIN_EMAIL=boss@example.com\nAUTOMATION_ADMIN_PASSWORD="quoted pass"\nAUTOMATION_HEADLESS=true\n')
    import os
    os.environ["AUTOMATION_HEADLESS"] = "false"
    load_env_file(f)
    assert os.environ["AUTOMATION_ADMIN_EMAIL"] == "boss@example.com"
    assert os.environ["AUTOMATION_ADMIN_PASSWORD"] == "quoted pass"
    assert os.environ["AUTOMATION_HEADLESS"] == "false"


def test_bootstrap_admin_works_with_the_operators_own_password_and_never_blocks_startup(settings):
    from fastapi.testclient import TestClient
    from app.main import create_app
    settings.bootstrap_admin_email, settings.bootstrap_admin_password = "boss@example.com", "metacare2026"
    with TestClient(create_app(settings), base_url="http://testserver") as c:
        r = c.post("/api/auth/login", json={"email": "boss@example.com", "password": "metacare2026"})
        assert r.status_code == 200 and r.json()["role"] == "admin"
    settings.bootstrap_admin_email = "not-an-email"          # a typo in .env must not stop the server
    settings.storage_dir = settings.storage_dir.parent / "other"
    with TestClient(create_app(settings), base_url="http://testserver") as c:
        assert c.get("/healthz").status_code == 200


def test_env_file_handles_comments_quotes_and_odd_values(tmp_path, isolated_env):
    import os
    from app.config import load_env_file, Settings
    f = tmp_path / ".env"
    f.write_text("\n".join([
        "AUTOMATION_MAX_CONCURRENT_RUNS=3      # runs at the same time",
        "AUTOMATION_ADMIN_EMAIL=boss@example.com   # the first admin",
        'AUTOMATION_ADMIN_PASSWORD="pa ss#word # x"',        # quoted: the # belongs to the password
        "AUTOMATION_ENGINE_LOG='off'",
        "AUTOMATION_ALLOWED_ORIGINS=",                         # empty value
        "AUTOMATION_RETENTION_FILES_DAYS=7",
        "NOT_A_SETTING_LINE",
    ]))
    load_env_file(f)
    s = Settings()
    assert s.max_concurrent_runs == 3 and s.bootstrap_admin_email == "boss@example.com"
    assert s.bootstrap_admin_password == "pa ss#word # x" and s.engine_log == "off" and s.retention_files_days == 7
    assert s.allowed_origins == ()


def test_the_shipped_env_example_loads_cleanly(isolated_env):
    from pathlib import Path
    from app.config import load_env_file, Settings
    example = Path(__file__).resolve().parents[1] / ".env.example"
    load_env_file(example)
    s = Settings()                                             # would raise on any value that is not a clean number
    assert s.max_concurrent_runs == 2 and s.max_total_workers == 8 and s.auto_retries == 1 and s.engine_log == "redacted"
    assert s.retention_files_days == 30 and s.retention_rows_days == 180


def test_a_bad_number_in_env_stops_startup_with_a_clear_message(isolated_env):
    import os
    import pytest
    from app.config import Settings
    os.environ["AUTOMATION_MAX_WORKERS"] = "four"
    with pytest.raises(SystemExit) as err:
        Settings()
    assert "AUTOMATION_MAX_WORKERS" in str(err.value) and "whole number" in str(err.value)


def test_shipped_env_files_have_no_inline_comments_because_systemd_and_start_bat_would_keep_them():
    import re
    from pathlib import Path
    for name in (".env.example",):
        for line in (Path(__file__).resolve().parents[1] / name).read_text().splitlines():
            if line.startswith("AUTOMATION_"):
                assert not re.search(r"\s#", line), f"{name}: inline comment would break systemd/start.bat: {line}"
