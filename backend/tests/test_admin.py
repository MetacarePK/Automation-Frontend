from conftest import PASSWORD, add_user, login


def test_non_admin_cannot_use_admin_api(make_client):
    c = make_client("u@example.com")
    for path in ("/api/admin/overview", "/api/admin/users", "/api/admin/logins", "/api/admin/activity", "/api/admin/sessions"):
        assert c.get(path).status_code == 403


def test_approval_flow_end_to_end(make_client, client, app, ctx):
    admin = make_client("admin@example.com", role="admin")
    client.post("/api/auth/signup", json={"name": "New", "email": "new@example.com", "password": PASSWORD})
    pending = admin.get("/api/admin/users?status=pending").json()["users"]
    assert [u["email"] for u in pending] == ["new@example.com"]
    uid = pending[0]["id"]
    assert admin.post(f"/api/admin/users/{uid}/approve", json={"automations": []}).status_code == 200
    c, r = login(app, "new@example.com")
    assert r.status_code == 200 and r.json()["automations"] == []
    assert c.get("/api/automations").json()["automations"] == []         # no cards yet
    admin.put(f"/api/admin/users/{uid}/access", json={"automations": ["fake-auto"]})
    cards = c.get("/api/automations").json()["automations"]
    assert [x["slug"] for x in cards] == ["fake-auto"]
    c.__exit__(None, None, None)


def test_user_without_access_gets_404_for_that_automation(make_client):
    c = make_client("u@example.com", slugs=())
    assert c.get("/api/automations/fake-auto").status_code == 404


def test_approve_rejects_unknown_automation(make_client, ctx):
    admin = make_client("admin@example.com", role="admin")
    uid = add_user(ctx, "p@example.com", status="pending")
    assert admin.post(f"/api/admin/users/{uid}/approve", json={"automations": ["nope"]}).status_code == 400


def test_disable_revokes_live_sessions(make_client, ctx):
    admin = make_client("admin@example.com", role="admin")
    user = make_client("u@example.com")
    uid = user.get("/api/auth/me").json()["id"]
    assert admin.post(f"/api/admin/users/{uid}/disable").status_code == 200
    assert user.get("/api/auth/me").status_code == 401


def test_cannot_disable_self_or_demote_last_admin(make_client):
    admin = make_client("admin@example.com", role="admin")
    me = admin.get("/api/auth/me").json()["id"]
    assert admin.post(f"/api/admin/users/{me}/disable").status_code == 400
    assert admin.put(f"/api/admin/users/{me}/role", json={"role": "user"}).status_code == 400


def test_password_reset_signs_user_out_and_new_password_works(make_client, app):
    admin = make_client("admin@example.com", role="admin")
    user = make_client("u@example.com")
    uid = user.get("/api/auth/me").json()["id"]
    assert admin.post(f"/api/admin/users/{uid}/reset-password", json={"password": "weak"}).status_code == 400
    assert admin.post(f"/api/admin/users/{uid}/reset-password", json={"password": "BrandNewPass99"}).status_code == 200
    assert user.get("/api/auth/me").status_code == 401
    c, r = login(app, "u@example.com", "BrandNewPass99")
    assert r.status_code == 200
    c.__exit__(None, None, None)


def test_overview_counts_logins_and_failures(make_client, client, ctx):
    admin = make_client("admin@example.com", role="admin")
    add_user(ctx, "u@example.com")
    client.post("/api/auth/login", json={"email": "u@example.com", "password": "Bad-Password-1"})
    client.post("/api/auth/login", json={"email": "u@example.com", "password": PASSWORD})
    o = admin.get("/api/admin/overview").json()
    assert o["logins_today"]["ok"] == 2 and o["logins_today"]["failed"] == 1
    assert o["logins_today"]["unique_users"] == 2
    assert o["users"]["active"] == 2 and o["active_sessions"] == 2
    assert len(o["login_chart"]) == 14 and o["login_chart"][-1]["ok"] == 2
    assert o["recent_failed_logins"][0]["email"] == "u@example.com"


def test_login_monitor_filters_per_user(make_client, client, ctx):
    admin = make_client("admin@example.com", role="admin")
    uid = add_user(ctx, "u@example.com")
    client.post("/api/auth/login", json={"email": "u@example.com", "password": "Bad-Password-1"})
    client.post("/api/auth/login", json={"email": "u@example.com", "password": PASSWORD})
    both = admin.get(f"/api/admin/logins?user_id={uid}").json()
    assert both["total"] == 2
    failed = admin.get(f"/api/admin/logins?user_id={uid}&success=false").json()
    assert failed["total"] == 1 and failed["events"][0]["reason"] == "bad_password"


def test_activity_log_is_per_user_and_user_sees_only_own(make_client, ctx):
    admin = make_client("admin@example.com", role="admin")
    a = make_client("a@example.com")
    b = make_client("b@example.com")
    a_id = a.get("/api/auth/me").json()["id"]
    mine = a.get("/api/account/activity").json()
    assert mine["total"] >= 1 and {e["action"] for e in mine["events"]} == {"login"}
    only_a = admin.get(f"/api/admin/activity?user_id={a_id}").json()
    assert only_a["total"] == mine["total"]
    assert all(e["user_id"] == a_id for e in only_a["events"])


def test_change_own_password_keeps_this_device_signs_out_others(make_client, app):
    user = make_client("u@example.com")
    other, _ = login(app, "u@example.com")
    r = user.post("/api/account/password", json={"current_password": PASSWORD, "new_password": "AnotherPass123"})
    assert r.status_code == 200
    assert user.get("/api/auth/me").status_code == 200
    assert other.get("/api/auth/me").status_code == 401
    assert user.post("/api/account/password", json={"current_password": "nope", "new_password": "AnotherPass1234"}).status_code == 400
    other.__exit__(None, None, None)
