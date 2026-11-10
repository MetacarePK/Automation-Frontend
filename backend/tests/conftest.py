import io
import shutil
import sys
import time
from pathlib import Path

import openpyxl
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings          # noqa: E402
from app.main import create_app          # noqa: E402
from app.security import hash_password   # noqa: E402
from app.db import utcnow                # noqa: E402

FAKES = Path(__file__).parent / "fake_automations"
PASSWORD = "Str0ngPassw0rd"


@pytest.fixture
def settings(tmp_path):
    automations = tmp_path / "automations"
    shutil.copytree(FAKES, automations)
    s = Settings(storage_dir=tmp_path / "storage", automations_dir=automations,
                 frontend_dir=Path(__file__).resolve().parents[2] / "frontend")
    s.max_failed_logins, s.lockout_minutes = 5, 15
    return s


@pytest.fixture
def app(settings):
    return create_app(settings)


@pytest.fixture
def client(app):
    with TestClient(app, base_url="http://testserver") as c:
        yield c


@pytest.fixture
def ctx(app):
    return app.state.ctx


def add_user(ctx, email, role="user", status="active", name="Test User", slugs=("fake-auto",)):
    with ctx.db.transaction() as conn:
        uid = conn.execute(
            "INSERT INTO users (email,name,password_hash,role,status,created_at) VALUES (?,?,?,?,?,?)",
            (email, name, hash_password(PASSWORD), role, status, utcnow())).lastrowid
        if status == "active":
            for slug in slugs:
                conn.execute("INSERT INTO user_automations VALUES (?,?,?,NULL)", (uid, slug, utcnow()))
    return uid


def login(app, email, password=PASSWORD):
    """A separate client per person, so cookies don't mix."""
    c = TestClient(app, base_url="http://testserver")
    c.__enter__()
    r = c.post("/api/auth/login", json={"email": email, "password": password})
    return c, r


@pytest.fixture
def make_client(app, ctx):
    opened = []

    def factory(email, **kw):
        add_user(ctx, email, **kw)
        c, r = login(app, email)
        assert r.status_code == 200, r.text
        opened.append(c)
        return c
    yield factory
    for c in opened:
        c.__exit__(None, None, None)


def xlsx_bytes(rows, header=("Patient", "DOB")):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(list(header))
    for r in rows:
        ws.append(list(r))
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def wait_for(client, run_id, states=("succeeded", "failed", "cancelled"), timeout=40):
    end = time.time() + timeout
    while time.time() < end:
        data = client.get(f"/api/runs/{run_id}").json()
        if data["status"] in states:
            return data
        time.sleep(0.3)
    raise AssertionError(f"run {run_id} did not reach {states}: {data}")
