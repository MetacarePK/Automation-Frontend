import time

import openpyxl

from conftest import add_user, wait_for, xlsx_bytes

MAP = {"name": "Patient", "dob": "DOB", "status": "", "visit_id": "", "beneficiary_id": "", "line_charge": ""}


def upload(c, rows, name="sheet.xlsx", header=("Patient", "DOB")):
    r = c.post("/api/automations/fake-auto/upload",
               files={"file": (name, xlsx_bytes(rows, header), "application/octet-stream")})
    return r


def start(c, filename="sheet.xlsx", **kw):
    body = {"filename": filename, "column_map": MAP, "save": True, "workers": 2, **kw}
    return c.post("/api/automations/fake-auto/run", json=body)


def test_upload_returns_headers_and_suggested_mapping(make_client):
    c = make_client("u@example.com")
    r = upload(c, [("Doe, Jane", "01/02/1950")])
    assert r.status_code == 200
    d = r.json()
    assert d["headers"] == ["Patient", "DOB"] and d["row_count"] == 1
    assert d["suggested_map"]["name"] == "Patient"      # matched against the engine's own defaults
    assert d["default_map"]["visit_id"] == "Visit ID"


def test_upload_rejects_non_xlsx_and_fake_xlsx(make_client):
    c = make_client("u@example.com")
    assert c.post("/api/automations/fake-auto/upload", files={"file": ("x.txt", b"hi")}).status_code == 400
    assert c.post("/api/automations/fake-auto/upload", files={"file": ("x.xlsx", b"not a zip")}).status_code == 400


def test_upload_filename_cannot_escape_the_workspace(make_client, settings):
    c = make_client("u@example.com")
    r = upload(c, [("A", "1")], name="../../../evil.xlsx")
    assert r.status_code == 200 and r.json()["filename"] == "evil.xlsx"
    assert not (settings.storage_dir.parent / "evil.xlsx").exists()


def test_full_run_records_rows_counts_log_download_and_audit(make_client, settings):
    c = make_client("u@example.com")
    upload(c, [("Ann", "1"), ("Bob DUP", "2"), ("MISS Cy", "3")])
    r = start(c)
    assert r.status_code == 201 and r.json()["total"] == 3
    run = wait_for(c, r.json()["run_id"])
    assert run["status"] == "succeeded", run
    assert run["counts"] == {"duplicate": 1, "created": 1, "not_created": 1, "done": 2, "seen": 3}
    assert {x["patient_name"] for x in run["rows"]} == {"Ann", "Bob DUP", "MISS Cy"}
    assert "Run" in "\n".join(c.get(f"/api/runs/{run['id']}/log").json()["lines"])
    dl = c.get(f"/api/runs/{run['id']}/download")
    assert dl.status_code == 200 and dl.content[:2] == b"PK"
    actions = [e["action"] for e in c.get("/api/account/activity").json()["events"]]
    assert {"run_queued", "run_succeeded", "download", "sheet_uploaded", "login"} <= set(actions)
    # the engine wrote its saved-login file to the PATCHED location, not into the engine folder
    assert (settings.uploads_dir / "fake-auto" / "auth_state.json").exists()
    assert not (settings.automations_dir / "fake-auto" / "auth_state.json").exists()


def test_bad_columns_are_refused_before_queueing(make_client):
    c = make_client("u@example.com")
    upload(c, [("Ann", "1")], header=("Name", "Birth"))
    r = start(c)
    assert r.status_code == 400 and "not found" in r.json()["detail"]
    r = c.post("/api/automations/fake-auto/run", json={"filename": "sheet.xlsx", "column_map": {"name": "Name"}})
    assert r.status_code == 400 and "DOB" in r.json()["detail"]      # required field unmapped


def test_engine_crash_marks_run_failed_with_reason(make_client):
    c = make_client("u@example.com")
    upload(c, [("CRASH me", "1")])
    run = wait_for(c, start(c).json()["run_id"])
    assert run["status"] == "failed" and "engine blew up" in run["error"]


def test_users_cannot_see_or_cancel_each_others_runs(make_client):
    a = make_client("a@example.com")
    b = make_client("b@example.com")
    upload(a, [("Ann", "1")])
    run_id = start(a).json()["run_id"]
    wait_for(a, run_id)
    assert b.get(f"/api/runs/{run_id}").status_code == 404
    assert b.get(f"/api/runs/{run_id}/log").status_code == 404
    assert b.get(f"/api/runs/{run_id}/download").status_code == 404
    assert b.post(f"/api/runs/{run_id}/cancel").status_code == 404
    assert b.get("/api/runs").json()["total"] == 0


def test_admin_sees_every_run_and_can_filter_by_user(make_client):
    admin = make_client("admin@example.com", role="admin")
    a = make_client("a@example.com")
    upload(a, [("Ann", "1")])
    run_id = start(a).json()["run_id"]
    wait_for(a, run_id)
    uid = a.get("/api/auth/me").json()["id"]
    assert admin.get("/api/runs").json()["total"] == 1
    assert admin.get(f"/api/runs?user_id={uid}").json()["runs"][0]["user_email"] == "a@example.com"
    assert admin.get(f"/api/runs/{run_id}").status_code == 200


def test_runs_of_one_automation_are_serialised_and_queued_run_can_be_cancelled(make_client):
    a = make_client("a@example.com")
    b = make_client("b@example.com")
    upload(a, [("SLOW one", "1")], name="a.xlsx")
    upload(b, [("Quick", "1")], name="b.xlsx")
    first = start(a, filename="a.xlsx").json()["run_id"]
    wait_for(a, first, states=("running",))
    second = start(b, filename="b.xlsx").json()
    assert second["queue_position"] == 1
    time.sleep(1.5)
    assert b.get(f"/api/runs/{second['run_id']}").json()["status"] == "queued"   # waits, does not overlap
    assert b.post(f"/api/runs/{second['run_id']}/cancel").json()["status"] == "cancelled"
    assert a.post(f"/api/runs/{first}/cancel").status_code == 200                  # kills the SLOW process
    assert wait_for(a, first)["status"] == "cancelled"


def test_restart_marks_inflight_runs_failed_and_resumes_queue(settings, ctx, make_client):
    c = make_client("u@example.com")
    uid = c.get("/api/auth/me").json()["id"]
    with ctx.db.session() as conn:
        for status in ("running", "queued"):
            conn.execute("INSERT INTO runs (automation_slug,user_id,status,sheet_name,sheet_path,options,queued_at)"
                         " VALUES ('fake-auto',?,?, 'gone.xlsx','/nonexistent/gone.xlsx','{}','2026-01-01T00:00:00Z')",
                         (uid, status))
    ctx.jobs.start()                    # what a server restart does
    runs = {r["id"]: r for r in c.get("/api/runs").json()["runs"]}
    assert runs[1]["status"] == "failed" and "restarted" in runs[1]["error"]
    run2 = wait_for(c, 2)               # queued run was picked up (and fails: its sheet is gone)
    assert run2["status"] == "failed"


def test_workers_are_capped_by_settings(make_client, ctx):
    c = make_client("u@example.com")
    upload(c, [("Ann", "1")])
    r = start(c, workers=99)
    assert r.json()["workers"] == ctx.settings.max_workers_per_run
    wait_for(c, r.json()["run_id"])


def test_card_reports_active_run_so_start_and_stop_work_from_the_dashboard(make_client):
    a = make_client("a@example.com")
    b = make_client("b@example.com")
    admin = make_client("admin@example.com", role="admin")
    card = lambda c: c.get("/api/automations").json()["automations"][0]
    assert card(a)["active_run"] is None
    upload(a, [("SLOW one", "1")], name="a.xlsx")
    run_id = start(a, filename="a.xlsx").json()["run_id"]
    wait_for(a, run_id, states=("running",))
    assert card(a)["active_run"]["id"] == run_id and card(a)["active_run"]["mine"] is True
    assert card(b)["active_run"] is None and card(b)["running_now"] == 1      # sees it is busy, can't stop it
    assert card(admin)["active_run"]["id"] == run_id and card(admin)["active_run"]["mine"] is False
    assert admin.post(f"/api/runs/{run_id}/cancel").status_code == 200       # an admin can stop anyone's run
    assert wait_for(a, run_id)["status"] == "cancelled"
    assert card(a)["active_run"] is None
