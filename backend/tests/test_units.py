"""Small pure-logic tests: redaction, output verification, process tracking, migration 2."""
import pathlib
import subprocess
import sys
import time

import openpyxl
import psutil

from app.services.procs import ProcessTracker
from app.services.redact import redact
from app.services.verify import verify_output


def test_redaction_hides_secrets_but_not_ordinary_text():
    assert "Hunter2!" not in redact("login with Hunter2! now", ["Hunter2!"])
    assert redact("password: abc pass=def \"token\": \"x\"") == 'password: *** pass=*** "token": ***'
    assert redact("Cookie: a=b; c=d") == "Cookie: ***"
    assert redact("Authorization: Bearer abcdefghijkl") == "Authorization: ***"
    assert redact("passed: 5 tokens used 3 visit 4411 DOB 01/02/1950") == "passed: 5 tokens used 3 visit 4411 DOB 01/02/1950"


def _sheet(tmp_path, statuses, header=("Patient", "DOB", "Status")):
    wb = openpyxl.Workbook(); ws = wb.active; ws.append(list(header))
    for i, st in enumerate(statuses):
        ws.append([f"P{i}", "d", st])
    path = tmp_path / "out.xlsx"; wb.save(path); return path


def test_verification_accepts_a_matching_sheet_and_names_every_kind_of_mismatch(tmp_path):
    ok = _sheet(tmp_path, ["Done", "Not Created", "Done - x not exists"])
    assert verify_output(ok, "Status", [(2, "Done"), (3, "Not Created"), (4, "Done - x not exists")], 2) == []
    assert "row 3" in verify_output(ok, "Status", [(3, "Done")], None)[0]
    assert "engine reported 5" in verify_output(ok, "Status", [], 5)[0]
    assert "no 'Result' column" in verify_output(ok, "Result", [], None)[0]
    assert "cannot be opened" in verify_output(tmp_path / "missing.xlsx", "Status", [], None)[0]


def test_process_tracker_kills_a_child_that_outlives_its_parent():
    parent = subprocess.Popen([sys.executable, "-c",
                               "import subprocess,sys,time;"
                               "c=subprocess.Popen([sys.executable,'-c','import time;time.sleep(300)']);"
                               "print(c.pid,flush=True);time.sleep(300)"], stdout=subprocess.PIPE, text=True)
    child = int(parent.stdout.readline())
    tracker = ProcessTracker(parent.pid)
    parent.kill(); parent.wait()                        # the parent dies and the child is orphaned
    assert psutil.pid_exists(child)
    assert tracker.kill_all(grace=2) == []
    time.sleep(0.3)
    assert not psutil.pid_exists(child) or psutil.Process(child).status() == psutil.STATUS_ZOMBIE


def test_migration_2_upgrades_a_version_1_database_without_losing_rows(tmp_path):
    import sqlite3
    from app import db
    path = tmp_path / "t.db"
    real = db.MIGRATIONS
    try:
        db.MIGRATIONS = real[:1]; db.init_db(path)                      # exactly the schema that was first deployed
        c = sqlite3.connect(path); c.execute("PRAGMA foreign_keys=ON")
        c.execute("INSERT INTO users (email,name,password_hash,created_at) VALUES ('a@b.co','A','x','2026-01-01T00:00:00Z')")
        for i, st in enumerate(("succeeded", "failed", "running"), 1):
            c.execute("INSERT INTO runs (automation_slug,user_id,status,sheet_name,sheet_path,options,queued_at)"
                      " VALUES ('s',1,?,'a.xlsx','/p','{}','2026-01-01T00:00:00Z')", (st,))
            c.execute("INSERT INTO run_rows VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (i, 2, 'P', 'd', 'Done', 'V', 'I', '', '', '', '', 't'))
        c.commit(); c.close()
        db.MIGRATIONS = real; db.init_db(path)
        c = sqlite3.connect(path)
        assert [r[0] for r in c.execute("SELECT status FROM runs ORDER BY id")] == ["completed", "failed", "running"]
        assert c.execute("SELECT COUNT(*) FROM run_rows").fetchone()[0] == 3        # children were NOT cascade-deleted
        assert c.execute("PRAGMA foreign_key_check").fetchall() == []
        assert c.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == len(real)
    finally:
        db.MIGRATIONS = real


def test_sheet_fingerprint_ignores_file_bytes_but_not_content(tmp_path):
    from app.services.jobs import sheet_fingerprint

    def make(name, rows, extra_ghost=False):
        wb = openpyxl.Workbook(); ws = wb.active
        for r in rows:
            ws.append(r)
        if extra_ghost:
            ws.cell(row=500, column=40).value = None          # formatting far away from the data
        path = tmp_path / name; wb.save(path); return path
    a = make("a.xlsx", [["Patient", "DOB"], ["Ann", "1"]])
    time.sleep(1.1)                                             # a re-save changes the file's timestamps/bytes
    b = make("b.xlsx", [["Patient", "DOB"], ["Ann", "1"]], extra_ghost=True)
    assert a.read_bytes() != b.read_bytes()
    assert sheet_fingerprint(a) == sheet_fingerprint(b)
    assert sheet_fingerprint(a) != sheet_fingerprint(make("c.xlsx", [["Patient", "DOB"], ["Ann", "2"]]))
    assert sheet_fingerprint(a) != sheet_fingerprint(make("d.xlsx", [["Patient", "DOB"], [None, None], ["Ann", "1"]]))  # row numbers matter
