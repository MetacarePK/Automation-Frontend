"""The login check reports what the page showed when a sign-in fails. Uses a real headless Chrome and the fake engine."""
from pathlib import Path

import pytest

from conftest import add_user


def _chrome_works() -> bool:
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            p.chromium.launch(channel="chrome", headless=True).close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _chrome_works(), reason="needs Google Chrome installed")


def set_login(settings, username):
    (settings.automations_dir / "fake-auto" / ".env").write_text(f"username={username}\npassword=pw\n")


def test_a_timeout_reports_the_page_title_text_window_and_a_screenshot(settings, ctx):
    settings.browser_channel, settings.browser_headless = "chrome", True
    set_login(settings, "blocked")
    result = ctx.jobs.check_login(ctx.registry.get("fake-auto"))
    assert result["ok"] is False and result["error"].startswith("TimeoutError")
    page = result["pages"][0]
    assert page["title"] == "Just a moment..." and "Checking your browser" in page["text"]
    assert page["window"][0] == 1920                                    # the desktop window size reached the browser
    assert "HeadlessChrome" in page["user_agent"] or "Chrome" in page["user_agent"]
    assert "SHOULD-NOT-APPEAR" not in page["text"]                      # typed/input values are never part of the report
    assert Path(page["screenshot"]).exists() and Path(page["screenshot"]).is_relative_to(settings.storage_dir / "diagnostics")


def test_a_refused_login_and_a_good_login(settings, ctx):
    settings.browser_channel, settings.browser_headless = "chrome", True
    set_login(settings, "badpass")
    refused = ctx.jobs.check_login(ctx.registry.get("fake-auto"))
    assert refused["ok"] is False and "error" not in refused and refused["pages"][0]["title"] == "Just a moment..."
    set_login(settings, "fine")
    assert ctx.jobs.check_login(ctx.registry.get("fake-auto"))["ok"] is True


def test_the_dashboard_shows_what_the_page_showed_but_never_the_server_path(make_client, settings):
    settings.browser_channel, settings.browser_headless = "chrome", True
    admin = make_client("admin@example.com", role="admin")
    set_login(settings, "blocked")
    body = admin.post("/api/automations/fake-auto/credentials/test").json()
    assert body["ok"] is False and "did not complete" in body["message"] and body["error"].startswith("TimeoutError")
    assert body["pages"][0]["title"] == "Just a moment..." and "screenshot" not in body["pages"][0]
    set_login(settings, "badpass")
    assert "did not accept" in admin.post("/api/automations/fake-auto/credentials/test").json()["message"]
    set_login(settings, "fine")
    assert admin.post("/api/automations/fake-auto/credentials/test").json()["ok"] is True
    assert admin.get("/api/admin/activity?action=credentials_tested").json()["total"] == 3
