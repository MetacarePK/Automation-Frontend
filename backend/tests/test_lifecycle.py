"""Job lifecycle: states, verification, retry, scheduling, isolation, cleanup, retention."""
import json
import os
import time
from datetime import datetime, timedelta, timezone

import psutil
import pytest

from conftest import add_user, wait_for, xlsx_bytes

MAP = {"name": "Patient", "dob": "DOB", "status": "", "visit_id": "", "beneficiary_id": "", "line_charge": ""}


def upload(c, rows, name="sheet.xlsx", slug="fake-auto"):
    r = c.post(f"/api/automations/{slug}/upload", files={"file": (name, xlsx_bytes(rows), "application/octet-stream")})
    assert r.status_code == 200, r.text
    return r


def start(c, name="sheet.xlsx", slug="fake-auto", **kw):
    return c.post(f"/api/automations/{slug}/run", json={"filename": name, "column_map": MAP, "save": True, "workers": 1, **kw})


def run_of(c, rows, name="sheet.xlsx", slug="fake-auto", **kw):
    upload(c, rows, name, slug)
    r = start(c, name, slug, **kw)
    assert r.status_code == 201, r.text
    return r.json()["run_id"]


def events(c, run_id):
    return [e["kind"] for e in c.get(f"/api/runs/{run_id}/events").json()["events"]]


# ----------------------------------------------------------------------------- states and verification

def test_a_clean_run_goes_through_validating_and_running_to_completed(make_client):
    c = make_client("u@example.com")
    run = wait_for(c, run_of(c, [("Ann", "1"), ("Bob", "2")]))
    assert run["status"] == "completed" and run["failure_kind"] is None
    kinds = events(c, run["id"])
    assert kinds.index("queued") < kinds.index("validating") < kinds.index("started") < kinds.index("completed")


def test_completed_is_never_reported_when_the_result_sheet_disagrees(make_client):
    c = make_client("u@example.com")
    run = wait_for(c, run_of(c, [("Ann", "1"), ("BADOUT Bo", "2")]))
    assert run["status"] == "failed" and run["failure_kind"] == "verification_failed"
    assert "could not be verified" in run["error"]


def test_login_failure_is_permanent_and_never_auto_retried(make_client):
    c = make_client("u@example.com")
    run = wait_for(c, run_of(c, [("NOLOGIN x", "1")]))
    assert run["status"] == "failed" and run["failure_kind"] == "login_failed"
    time.sleep(2.5)                                           # longer than the (1 s) test backoff
    assert c.get("/api/runs").json()["total"] == 1            # no retry was queued


def test_engine_error_is_permanent_too(make_client):
    c = make_client("u@example.com")
    run = wait_for(c, run_of(c, [("CRASH me", "1")]))
    assert run["failure_kind"] == "engine_error" and "engine blew up" in run["error"]
    time.sleep(2.5)
    assert c.get("/api/runs").json()["total"] == 1


def test_missing_login_is_caught_before_any_browser_starts(make_client, settings):
    (settings.automations_dir / "fake-auto" / ".env").write_text("")
    c = make_client("u@example.com")
    run = wait_for(c, run_of(c, [("Ann", "1")]))
    assert run["status"] == "failed" and run["failure_kind"] == "config_error"
    assert "no Office Ally login" in run["error"]
    assert "started" not in events(c, run["id"])


# ----------------------------------------------------------------------------- retry

def test_a_crash_is_retried_once_automatically_and_finished_rows_are_not_redone(make_client):
    c = make_client("u@example.com")
    first = run_of(c, [("Ann", "1"), ("KILLME Bo", "2")])
    run1 = wait_for(c, first)
    assert run1["status"] == "failed" and run1["failure_kind"] == "crash"
    deadline = time.time() + 30
    while c.get("/api/runs").json()["total"] < 2 and time.time() < deadline:
        time.sleep(0.3)
    runs = c.get("/api/runs").json()["runs"]
    second = next(r for r in runs if r["id"] != first)
    assert second["retry_of"] == first and second["attempt"] == 2
    run2 = wait_for(c, second["id"])
    assert run2["status"] == "completed"
    assert run2["carried_over"] == 1                          # Ann was already Done in the first attempt
    assert run2["counts"]["seen"] == 1                        # only the interrupted row was processed again
    assert [x["attempt"] for x in run2["chain"]] == [1, 2]
    assert "auto_retry" in events(c, first)


def test_a_second_crash_is_not_retried_again(make_client, settings):
    settings.auto_retries = 0
    c = make_client("u@example.com")
    run = wait_for(c, run_of(c, [("KILLME x", "1")]))
    assert run["failure_kind"] == "crash"
    time.sleep(2.5)
    assert c.get("/api/runs").json()["total"] == 1


def test_manual_retry_resumes_and_forces_the_safe_options(make_client, ctx):
    c = make_client("u@example.com")
    first = run_of(c, [("Ann", "1"), ("MISS Cy", "2"), ("Dee", "3")], allow_duplicate=True, no_resume=True)
    run1 = wait_for(c, first)
    assert run1["status"] == "completed_with_errors" and run1["can_retry"] is True
    retry = c.post(f"/api/runs/{first}/retry")
    assert retry.status_code == 201
    new_id = retry.json()["run_id"]
    run2 = wait_for(c, new_id)
    assert run2["carried_over"] == 2 and run2["counts"]["seen"] == 1       # Ann and Dee are not processed again
    with ctx.db.session() as conn:
        options = json.loads(conn.execute("SELECT options FROM runs WHERE id=?", (new_id,)).fetchone()["options"])
    assert options["allow_duplicate"] is False and options["no_resume"] is False
    assert run2["attempt"] == 2 and run2["retry_of"] == first


def test_retry_is_refused_for_finished_good_runs_and_for_runs_still_going(make_client):
    c = make_client("u@example.com")
    done = wait_for(c, run_of(c, [("Ann", "1")]))
    assert c.post(f"/api/runs/{done['id']}/retry").status_code == 409
    slow = run_of(c, [("SLOW x", "1")], name="slow.xlsx")
    wait_for(c, slow, states=("running",))
    assert c.post(f"/api/runs/{slow}/retry").status_code == 409
    c.post(f"/api/runs/{slow}/cancel")
    wait_for(c, slow)


def test_the_same_sheet_cannot_be_started_twice_at_once(make_client):
    c = make_client("u@example.com")
    first = run_of(c, [("SLOW x", "1")])
    wait_for(c, first, states=("running",))
    again = start(c)
    assert again.status_code == 409 and "already queued or running" in again.json()["detail"]
    c.post(f"/api/runs/{first}/cancel")
    wait_for(c, first)


# ----------------------------------------------------------------------------- scheduling

def test_both_automations_run_at_the_same_time(make_client):
    c = make_client("u@example.com", slugs=("fake-auto", "fake-auto-2"))
    a = run_of(c, [("SLOW a", "1")], name="a.xlsx", slug="fake-auto")
    b = run_of(c, [("SLOW b", "1")], name="b.xlsx", slug="fake-auto-2")
    wait_for(c, a, states=("running",)); wait_for(c, b, states=("running",))
    assert c.get(f"/api/runs/{a}").json()["status"] == "running" == c.get(f"/api/runs/{b}").json()["status"]
    for r in (a, b):
        c.post(f"/api/runs/{r}/cancel")
    wait_for(c, a); wait_for(c, b)


def test_the_global_run_cap_holds_back_the_next_run_until_there_is_room(make_client, settings):
    settings.max_concurrent_runs = 1
    c = make_client("u@example.com", slugs=("fake-auto", "fake-auto-2"))
    a = run_of(c, [("SLOW a", "1")], name="a.xlsx", slug="fake-auto")
    wait_for(c, a, states=("running",))
    b = run_of(c, [("Bee", "1")], name="b.xlsx", slug="fake-auto-2")
    time.sleep(2.5)
    assert c.get(f"/api/runs/{b}").json()["status"] == "queued"
    c.post(f"/api/runs/{a}/cancel"); wait_for(c, a)
    assert wait_for(c, b)["status"] == "completed"            # starts by itself once there is room


def test_the_browser_worker_budget_is_respected(make_client, settings):
    settings.max_total_workers = 4
    c = make_client("u@example.com", slugs=("fake-auto", "fake-auto-2"))
    a = run_of(c, [("SLOW a", "1")], name="a.xlsx", slug="fake-auto", workers=4)
    wait_for(c, a, states=("running",))
    b = run_of(c, [("Bee", "1")], name="b.xlsx", slug="fake-auto-2", workers=2)
    time.sleep(2.5)
    assert c.get(f"/api/runs/{b}").json()["status"] == "queued"          # 4 + 2 would exceed the budget
    c.post(f"/api/runs/{a}/cancel"); wait_for(c, a)
    assert wait_for(c, b)["status"] == "completed"


def test_a_waiting_run_does_not_block_a_different_automation(make_client):
    c = make_client("u@example.com", slugs=("fake-auto", "fake-auto-2"))
    slow = run_of(c, [("SLOW a", "1")], name="a.xlsx", slug="fake-auto")
    wait_for(c, slow, states=("running",))
    blocked = run_of(c, [("Bee", "1")], name="b.xlsx", slug="fake-auto")        # same automation: must wait
    other = run_of(c, [("Cee", "1")], name="c.xlsx", slug="fake-auto-2")        # different automation: goes now
    assert wait_for(c, other)["status"] == "completed"
    assert c.get(f"/api/runs/{blocked}").json()["status"] == "queued"
    c.post(f"/api/runs/{slow}/cancel"); wait_for(c, slow); wait_for(c, blocked)


def test_the_queue_is_fair_between_people(make_client, settings):
    settings.max_concurrent_runs = 1
    a = make_client("a@example.com")
    b = make_client("b@example.com")
    a1 = run_of(a, [("SLOW a", "1")], name="a1.xlsx")
    wait_for(a, a1, states=("running",))
    a2 = run_of(a, [("Ann", "1")], name="a2.xlsx")             # A queues a second run first...
    b1 = run_of(b, [("Bob", "1")], name="b1.xlsx")             # ...then B queues one
    a.post(f"/api/runs/{a1}/cancel"); wait_for(a, a1)
    run_b = wait_for(b, b1)
    run_a2 = wait_for(a, a2)
    assert run_b["started_at"] <= run_a2["started_at"]         # B goes first although A queued earlier


def test_a_crashing_run_does_not_disturb_another_running_one(make_client, settings):
    settings.auto_retries = 0
    c = make_client("u@example.com", slugs=("fake-auto", "fake-auto-2"))
    survivor = run_of(c, [("SLOW s", "1")], name="s.xlsx", slug="fake-auto-2")
    wait_for(c, survivor, states=("running",))
    crashed = wait_for(c, run_of(c, [("KILLME k", "1")], name="k.xlsx", slug="fake-auto"))
    assert crashed["status"] == "failed"
    assert c.get(f"/api/runs/{survivor}").json()["status"] == "running"
    assert c.get("/healthz").status_code == 200
    c.post(f"/api/runs/{survivor}/cancel"); wait_for(c, survivor)


# ----------------------------------------------------------------------------- cleanup, isolation, restart

def test_cancelling_stops_every_process_the_run_started(make_client, settings):
    c = make_client("u@example.com")
    run_id = run_of(c, [("SPAWN browser", "1")])
    wait_for(c, run_id, states=("running",))
    pid_file = settings.runs_dir / str(run_id) / "output" / "child.pid"
    deadline = time.time() + 20
    while not pid_file.exists() and time.time() < deadline:
        time.sleep(0.2)
    child = int(pid_file.read_text())
    assert psutil.pid_exists(child)
    assert c.post(f"/api/runs/{run_id}/cancel").status_code == 200
    run = wait_for(c, run_id)
    assert run["status"] == "cancelled"
    time.sleep(0.5)
    assert not psutil.pid_exists(child) or psutil.Process(child).status() == psutil.STATUS_ZOMBIE


def test_every_run_has_its_own_files_and_nothing_is_shared_between_people(make_client, settings):
    a = make_client("a@example.com")
    b = make_client("b@example.com")
    ra = wait_for(a, run_of(a, [("Ann", "1")]))
    rb = wait_for(b, run_of(b, [("Bob", "1")]))
    pa, pb = settings.runs_dir / str(ra["id"]), settings.runs_dir / str(rb["id"])
    assert (pa / "input" / "sheet.xlsx").exists() and (pb / "input" / "sheet.xlsx").exists() and pa != pb
    assert (pa / "output" / "sheet_with_status.xlsx").exists() and (pb / "output" / "sheet_with_status.xlsx").exists()
    assert not (pa / "auth_state.json").exists() and not (pb / "auth_state.json").exists()   # cookies are deleted after the run


def test_progress_is_only_reused_for_the_very_same_sheet_contents(make_client):
    c = make_client("u@example.com")
    wait_for(c, run_of(c, [("Ann", "1"), ("Bob", "2")]))
    # same name, DIFFERENT contents: must not inherit the earlier rows' progress
    other = wait_for(c, run_of(c, [("Cy", "9"), ("Di", "8")]))
    assert other["carried_over"] == 0 and other["counts"]["seen"] == 2
    # same name and same contents as the first: resumes
    again = wait_for(c, run_of(c, [("Ann", "1"), ("Bob", "2")]))
    assert again["carried_over"] == 2 and again["counts"]["seen"] == 0


def test_server_restart_interrupts_inflight_runs_and_keeps_queued_ones(make_client, settings, ctx):
    c = make_client("u@example.com")
    uid = c.get("/api/auth/me").json()["id"]
    now = "2026-01-01T00:00:00Z"
    with ctx.db.session() as conn:
        for status in ("validating", "running"):
            conn.execute("INSERT INTO runs (automation_slug,user_id,status,sheet_name,sheet_path,options,queued_at)"
                         " VALUES ('fake-auto',?,?,'x.xlsx','/none','{}',?)", (uid, status, now))
    ctx.jobs._recover()
    rows = {r["id"]: r for r in c.get("/api/runs").json()["runs"]}
    assert all(r["status"] == "interrupted" for r in rows.values())
    assert all(r["failure_kind"] == "interrupted" for r in rows.values())


def test_logs_are_timestamped_structured_and_free_of_secrets(make_client, settings):
    c = make_client("u@example.com")
    run = wait_for(c, run_of(c, [("LEAK Lee", "1")]))
    text = "\n".join(c.get(f"/api/runs/{run['id']}/log").json()["lines"])
    assert "Sup3rSecret" not in text and "abc123" not in text and "***" in text
    assert "engine" in text and text.splitlines()[0][:4] == "2026" or text.splitlines()[0][:2] == "20"
    assert " INFO " in text


def test_engine_output_can_be_switched_off_entirely(make_client, settings):
    settings.engine_log = "off"
    c = make_client("u@example.com")
    run = wait_for(c, run_of(c, [("LEAK Lee", "1")]))
    text = "\n".join(c.get(f"/api/runs/{run['id']}/log").json()["lines"])
    assert "password" not in text.lower() and "server" in text


# ----------------------------------------------------------------------------- results, files, access

def test_failed_records_and_files_can_be_reviewed_and_downloaded(make_client):
    c = make_client("u@example.com")
    run = wait_for(c, run_of(c, [("Ann", "1"), ("MISS Cy", "2")]))
    failed = c.get(f"/api/runs/{run['id']}?rows=50&view=failed").json()["rows"]
    assert [r["patient_name"] for r in failed] == ["MISS Cy"]
    kinds = {f["kind"] for f in c.get(f"/api/runs/{run['id']}/files").json()["files"]}
    assert {"status", "failed", "log"} <= kinds
    csv_body = c.get(f"/api/runs/{run['id']}/download?file=failed")
    assert csv_body.status_code == 200 and "MISS Cy" in csv_body.text and csv_body.text.startswith("Row,Patient")
    assert c.get(f"/api/runs/{run['id']}/download?file=../../etc/passwd").status_code == 422   # only known file kinds


def test_csv_cells_that_look_like_formulas_are_neutralised():
    from app.routers.runs import _csv_safe
    assert _csv_safe("=cmd|' /C calc'!A0") == "'=cmd|' /C calc'!A0"
    assert _csv_safe("+1") == "'+1" and _csv_safe("@x") == "'@x" and _csv_safe("Doe, Jane") == "Doe, Jane"


def test_other_people_cannot_reach_a_runs_files_events_or_retry(make_client):
    a = make_client("a@example.com")
    b = make_client("b@example.com")
    rid = wait_for(a, run_of(a, [("MISS x", "1")]))["id"]
    for path in ("events", "files", "log", "download?file=failed", "download?file=status"):
        assert b.get(f"/api/runs/{rid}/{path}").status_code == 404, path
    assert b.post(f"/api/runs/{rid}/retry").status_code == 404
    assert b.post(f"/api/runs/{rid}/cancel").status_code == 404


# ----------------------------------------------------------------------------- retention

def test_retention_removes_old_files_and_row_detail_but_keeps_the_record(make_client, settings, ctx):
    from app.services.retention import purge
    c = make_client("u@example.com")
    run = wait_for(c, run_of(c, [("Ann", "1"), ("MISS Cy", "2")]))
    settings.retention_files_days, settings.retention_rows_days = 30, 90
    assert purge(ctx) == {"run_files": 0, "run_rows": 0, "stale_files": 0}          # too recent: nothing happens
    old = (datetime.now(timezone.utc) - timedelta(days=100)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with ctx.db.session() as conn:
        conn.execute("UPDATE runs SET finished_at=? WHERE id=?", (old, run["id"]))
    stats = purge(ctx)
    assert stats["run_files"] == 1 and stats["run_rows"] == 1
    assert not (settings.runs_dir / str(run["id"])).exists()
    after = c.get(f"/api/runs/{run['id']}").json()
    assert after["status"] == "completed_with_errors" and after["files_purged_at"] and after["rows_purged_at"]
    assert after["rows"] == [] and after["can_retry"] is False
    assert c.get(f"/api/runs/{run['id']}/download?file=status").status_code == 404
    assert c.get(f"/api/runs/{run['id']}/log").json()["purged"] is True
    assert c.post(f"/api/runs/{run['id']}/retry").status_code == 409


def test_retention_never_touches_runs_that_are_still_open(make_client, settings, ctx):
    from app.services.retention import purge
    c = make_client("u@example.com")
    rid = run_of(c, [("SLOW x", "1")])
    wait_for(c, rid, states=("running",))
    settings.retention_files_days = settings.retention_rows_days = 1
    with ctx.db.session() as conn:
        conn.execute("UPDATE runs SET finished_at='2020-01-01T00:00:00Z' WHERE id=?", (rid,))   # nonsense, but open
    assert purge(ctx)["run_files"] == 0
    c.post(f"/api/runs/{rid}/cancel"); wait_for(c, rid)


def test_run_details_show_the_selected_configuration_and_no_secrets(make_client):
    c = make_client("u@example.com")
    run = wait_for(c, run_of(c, [("Ann", "1"), ("Bob", "2")], limit=1, only="Ann", workers=1))
    config = run["config"]
    assert config["save"] is True and config["workers"] == 1 and config["limit"] == 1 and config["only"] == "Ann"
    assert config["column_map"]["name"] == "Patient"
    text = json.dumps(run)
    assert "Sup3rSecret" not in text and "sheet_sha256" not in text and "/runs/" not in run["sheet_name"]
