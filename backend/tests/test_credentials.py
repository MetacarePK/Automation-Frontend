"""Updating an automation's Office Ally login from the dashboard."""
import os
import stat

from conftest import add_user, login, wait_for, xlsx_bytes

NEW = {"values": {"username": "newuser", "password": "N3w:pass=word#1"}}


def env_text(settings, slug="fake-auto"):
    return (settings.automations_dir / slug / ".env").read_text()


def test_a_person_without_the_right_can_neither_see_nor_change_the_login(make_client, settings):
    c = make_client("u@example.com")
    assert c.get("/api/automations/fake-auto/credentials").json() == {"can_edit": False}
    assert c.put("/api/automations/fake-auto/credentials", json=NEW).status_code == 403
    assert c.post("/api/automations/fake-auto/credentials/test").status_code == 403
    assert "fakeuser" in env_text(settings) and "newuser" not in env_text(settings)


def test_admins_and_people_who_were_granted_the_right_can_update_it(make_client, settings):
    admin = make_client("admin@example.com", role="admin")
    granted = make_client("g@example.com", creds=("fake-auto",))
    for who in (admin, granted):
        status = who.get("/api/automations/fake-auto/credentials").json()
        assert status["can_edit"] is True
        assert {f["key"] for f in status["fields"]} == {"username", "password"}
    assert granted.put("/api/automations/fake-auto/credentials", json=NEW).status_code == 200
    assert "username=newuser" in env_text(settings) and "password=N3w:pass=word#1" in env_text(settings)


def test_the_right_is_per_automation(make_client):
    c = make_client("g@example.com", slugs=("fake-auto", "fake-auto-2"), creds=("fake-auto",))
    assert c.get("/api/automations/fake-auto/credentials").json()["can_edit"] is True
    assert c.get("/api/automations/fake-auto-2/credentials").json()["can_edit"] is False
    assert c.put("/api/automations/fake-auto-2/credentials", json=NEW).status_code == 403


def test_an_automation_you_cannot_use_looks_like_it_does_not_exist(make_client):
    c = make_client("u@example.com", slugs=("fake-auto",))
    assert c.get("/api/automations/fake-auto-2/credentials").status_code == 404
    assert c.put("/api/automations/fake-auto-2/credentials", json=NEW).status_code == 404


def test_the_password_is_write_only_never_returned_and_never_logged(make_client, settings):
    admin = make_client("admin@example.com", role="admin")
    r = admin.put("/api/automations/fake-auto/credentials", json=NEW)
    assert r.status_code == 200 and r.json()["changed"] == ["username", "password"]
    body = admin.get("/api/automations/fake-auto/credentials").text + r.text
    assert "N3w:pass=word#1" not in body and "Sup3rSecret" not in body
    password_field = next(f for f in r.json()["fields"] if f["key"] == "password")
    assert password_field["is_set"] is True and "value" not in password_field
    username_field = next(f for f in r.json()["fields"] if f["key"] == "username")
    assert username_field["value"] == "newuser"
    activity = admin.get("/api/admin/activity?action=credentials_updated").json()["events"]
    assert activity and "N3w:pass" not in json_dump(activity) and "newuser" not in json_dump(activity)
    assert '"fields": ["username", "password"]' in activity[0]["details"]


def json_dump(value):
    import json
    return json.dumps(value)


def test_a_blank_password_keeps_the_current_one_and_the_file_stays_private(make_client, settings):
    admin = make_client("admin@example.com", role="admin")
    admin.put("/api/automations/fake-auto/credentials", json={"values": {"username": "onlyname", "password": ""}})
    text = env_text(settings)
    assert "username=onlyname" in text and "Sup3rSecret!" in text
    mode = stat.S_IMODE(os.stat(settings.automations_dir / "fake-auto" / ".env").st_mode)
    assert mode == 0o600


def test_bad_values_are_refused_with_a_clear_reason(make_client, settings):
    admin = make_client("admin@example.com", role="admin")
    for values, word in (({"username": ""}, "empty"), ({"username": " x"}, "start or end"),
                         ({"password": "a\nb"}, "line breaks"), ({"password": "'quoted'"}, "quote"),
                         ({"nope": "x"}, "Unknown"), ({"username": "x" * 300}, "too long")):
        r = admin.put("/api/automations/fake-auto/credentials", json={"values": values})
        assert r.status_code == 400 and word in r.json()["detail"], (values, r.text)
    assert "fakeuser" in env_text(settings)                   # nothing was written by the failed attempts


def test_the_login_cannot_be_changed_or_tested_while_a_run_is_active(make_client):
    admin = make_client("admin@example.com", role="admin")
    admin.post("/api/automations/fake-auto/upload", files={"file": ("s.xlsx", xlsx_bytes([("SLOW x", "1")]), "x")})
    run_id = admin.post("/api/automations/fake-auto/run", json={
        "filename": "s.xlsx", "save": True, "workers": 1,
        "column_map": {"name": "Patient", "dob": "DOB"}}).json()["run_id"]
    wait_for(admin, run_id, states=("running",))
    assert admin.get("/api/automations/fake-auto/credentials").json()["busy"] is True
    r = admin.put("/api/automations/fake-auto/credentials", json=NEW)
    assert r.status_code == 409 and "run is in progress" in r.json()["detail"]
    assert admin.post("/api/automations/fake-auto/credentials/test").status_code == 409
    admin.post(f"/api/runs/{run_id}/cancel"); wait_for(admin, run_id)
    assert admin.put("/api/automations/fake-auto/credentials", json=NEW).status_code == 200   # fine once it ended


def test_the_login_test_reports_success_and_failure_and_is_audited(make_client, ctx, monkeypatch):
    admin = make_client("admin@example.com", role="admin")
    monkeypatch.setattr(ctx.jobs, "check_login", lambda automation: {"type": "login_check", "ok": True, "url": "https://x"})
    assert admin.post("/api/automations/fake-auto/credentials/test").json() == {"ok": True, "message": "Signed in to Office Ally successfully."}
    monkeypatch.setattr(ctx.jobs, "check_login", lambda automation: {"type": "login_check", "ok": False, "url": "https://x"})
    bad = admin.post("/api/automations/fake-auto/credentials/test").json()
    assert bad["ok"] is False and "username and password" in bad["message"]
    monkeypatch.setattr(ctx.jobs, "check_login", lambda automation: {"type": "error", "error": "boom"})
    assert "could not run" in admin.post("/api/automations/fake-auto/credentials/test").json()["message"]
    events = admin.get("/api/admin/activity?action=credentials_tested").json()["events"]
    assert len(events) == 3


def test_admin_can_grant_the_right_when_approving_and_later_change_it(make_client, app, ctx):
    admin = make_client("admin@example.com", role="admin")
    uid = add_user(ctx, "new@example.com", status="pending")
    r = admin.post(f"/api/admin/users/{uid}/approve", json={"automations": ["fake-auto", "fake-auto-2"], "credentials": ["fake-auto-2"]})
    assert r.status_code == 200
    c, _ = login(app, "new@example.com")
    assert c.get("/api/auth/me").json()["credential_automations"] == ["fake-auto-2"]
    row = next(u for u in admin.get("/api/admin/users").json()["users"] if u["id"] == uid)
    assert row["credential_automations"] == ["fake-auto-2"]
    admin.put(f"/api/admin/users/{uid}/access", json={"automations": ["fake-auto"], "credentials": ["fake-auto"]})
    assert c.get("/api/auth/me").json()["credential_automations"] == ["fake-auto"]
    bad = admin.put(f"/api/admin/users/{uid}/access", json={"automations": ["fake-auto"], "credentials": ["fake-auto-2"]})
    assert bad.status_code == 400 and "can use" in bad.json()["detail"]
    c.__exit__(None, None, None)
