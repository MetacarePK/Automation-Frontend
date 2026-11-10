"""Runs ONE automation job in its own process, against an UNCHANGED engine.

    python runner.py <job.json>        (started with cwd = the engine folder)

Why a separate process per job:
  * Both Blackhill engines have modules called main / patients / parallel, so
    they cannot be imported into the same process.
  * run_automation() replaces sys.stdout for the whole process while it runs.
  * A crashed or killed run can never take the web server down with it.

What this file does that the engine's own CLI does not:
  * points the engine's data / output / saved-login paths at this user's
    workspace (the engines keep those in module globals, patched below),
  * forces the browser channel / headless setting from the environment, so a
    Linux server can run without a screen without editing the engine,
  * reports progress as `@@EVENT {json}` lines on the real stdout, which the
    web server turns into database rows.

The engine files themselves stay byte-identical to the versions that have been
running on the Windows PC.
"""

import json
import os
import sys
import threading
import traceback
from pathlib import Path

EVENT_PREFIX = "@@EVENT "
_emit_lock = threading.Lock()


def emit(payload: dict) -> None:
    # sys.__stdout__ is the real pipe: the engine swaps sys.stdout for a Tee
    # that mirrors into logs.txt, and events must not end up in the log.
    with _emit_lock:
        sys.__stdout__.write(EVENT_PREFIX + json.dumps(payload, default=str) + "\n")
        sys.__stdout__.flush()


def patch_browser(channel: str | None, headless: bool | None) -> None:
    """Override how every engine launch() call starts its browser."""
    from playwright.sync_api import BrowserType

    original = BrowserType.launch

    def launch(self, *args, **kwargs):
        if channel:
            if channel == "chromium":
                kwargs.pop("channel", None)      # Playwright's bundled Chromium
            else:
                kwargs["channel"] = channel
        if headless is not None:
            kwargs["headless"] = headless
        return original(self, *args, **kwargs)

    BrowserType.launch = launch


def read_job(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def mode_describe(job: dict) -> int:
    import patients
    emit({"type": "describe", "default_map": patients.DEFAULT_COLUMN_MAP})
    return 0


def mode_validate(job: dict) -> int:
    """Read the sheet with the engine's own reader, so what is counted and
    rejected here is exactly what a real run would count and reject."""
    from patients import read_patients
    try:
        records = read_patients(Path(job["excel_path"]), column_map=job.get("column_map"))
    except SystemExit as exc:           # the engine reports sheet problems with sys.exit(msg)
        emit({"type": "validated", "ok": False, "error": str(exc)})
        return 0
    except Exception as exc:
        emit({"type": "validated", "ok": False, "error": f"Could not read that sheet: {exc}"})
        return 0
    emit({"type": "validated", "ok": True, "total": len(records)})
    return 0


def mode_run(job: dict) -> int:
    import main as engine
    from patients import resolve_column_map

    ws = job["workspace"]
    engine.DATA_DIR = Path(ws["data_dir"])
    engine.OUTPUT_DIR = Path(ws["output_dir"])
    engine.STATE_PATH = Path(ws["state_path"])
    engine.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    engine.STATE_PATH.parent.mkdir(parents=True, exist_ok=True)

    patch_browser(job.get("browser_channel") or None, job.get("browser_headless"))

    column_map = job.get("column_map") or {}
    resolved = resolve_column_map(column_map)

    # How many rows an earlier run already finished (the engine skips them). Reported so the
    # dashboard can show "already done" and keep a retry from looking like it lost work.
    carried = 0
    if not job.get("no_resume"):
        try:
            from patients import read_patients
            previous = engine.read_previous_status(engine.status_xlsx_path_for(Path(job["excel_path"])), column_map)
            rows_in_sheet = {r["row"] for r in read_patients(Path(job["excel_path"]), column_map=column_map)}
            carried = sum(1 for row, st in previous.items() if row in rows_in_sheet and str(st).startswith("Done"))
        except Exception:                      # resume info is informational; never block a run on it
            carried = 0
    emit({"type": "resume", "carried": carried})

    def progress_hook(group_status, group_captured, result):
        rows = []
        for row, row_status in group_status.items():
            captured = group_captured.get(row, {})
            rows.append({
                "row": row,
                "patient_name": result.get("patient_name", ""),
                "dos": result.get("sheet_dos", ""),
                "status": row_status,
                "visit_id": captured.get(resolved["visit_id"], ""),
                "beneficiary_id": captured.get(resolved["beneficiary_id"], ""),
                "duplicate_of": result.get("duplicate_skipped", ""),
                "error": result.get("error", ""),
                "line_charges": captured.get(resolved["line_charge"], ""),
                # ACO reports "note", Non ACO reports a beneficiary-ID mismatch
                "note": result.get("note", "") or result.get("beneficiary_id_mismatch", ""),
            })
        emit({"type": "rows", "rows": rows})

    outcome = engine.run_automation(
        excel_path=Path(job["excel_path"]),
        column_map=column_map,
        workers=max(1, int(job.get("workers") or 1)),
        limit=job.get("limit"),
        only=job.get("only"),
        no_resume=bool(job.get("no_resume")),
        make_visit=True,
        save_visit=bool(job.get("save")),
        allow_duplicate=bool(job.get("allow_duplicate")),
        no_wait=True,            # nobody is at a keyboard on a server
        interactive=False,
        progress_hook=progress_hook,
    )

    if not outcome.get("logged_in"):
        emit({"type": "finished", "ok": False,
              "error": "Could not log in to Office Ally - check the username and password in the automation's .env"})
        return 1

    emit({"type": "finished", "ok": True,
          "done": outcome["done"], "total": outcome["total"],
          "captured": len(outcome["captured_columns"]),
          "status_xlsx": str(outcome["status_xlsx_path"]),
          "status_column": resolved["status"],
          "summary": (f"{outcome['done']} of {outcome['total']} rows Done, "
                      f"{len(outcome['captured_columns'])} row(s) with Visit ID / insurance ID captured")})
    return 0


def mode_login_check(job: dict) -> int:
    """REAL Office Ally login with this automation's own credentials, then log out of nothing and
    change nothing: used by `app.cli check-login` to prove the server can sign in."""
    import main as engine
    from playwright.sync_api import sync_playwright

    state = Path(job["state_path"])
    state.parent.mkdir(parents=True, exist_ok=True)
    engine.STATE_PATH = state
    patch_browser(job.get("browser_channel") or None, job.get("browser_headless"))
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(channel="msedge", headless=False, args=["--start-maximized"])
            try:
                context, page, ok = engine.log_in(browser)
                emit({"type": "login_check", "ok": bool(ok), "url": page.url.split("?")[0]})
            finally:
                browser.close()
    finally:
        state.unlink(missing_ok=True)       # the saved session holds cookies - never leave it behind
    return 0


def mode_browser_check(job: dict) -> int:
    """Can this machine start the configured browser at all (no site visited)?"""
    from playwright.sync_api import sync_playwright
    patch_browser(job.get("browser_channel") or None, job.get("browser_headless"))
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=False)
        try:
            page = browser.new_page()
            page.goto("about:blank")
            emit({"type": "browser_check", "ok": True, "version": browser.version})
        finally:
            browser.close()
    return 0


MODES = {"describe": mode_describe, "validate": mode_validate, "run": mode_run,
         "login_check": mode_login_check, "browser_check": mode_browser_check}


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: runner.py <job.json>", file=sys.stderr)
        return 2
    job = read_job(sys.argv[1])
    if job.get("engine_dir"):
        engine_dir = str(Path(job["engine_dir"]).resolve())
        os.chdir(engine_dir)
        sys.path.insert(0, engine_dir)
    handler = MODES.get(job.get("mode"))
    if handler is None:
        print(f"unknown mode {job.get('mode')!r}", file=sys.stderr)
        return 2
    try:
        return handler(job)
    except SystemExit as exc:                 # engine sys.exit("message")
        message = str(exc.code) if exc.code not in (None, 0) else "Engine stopped"
        emit({"type": "finished", "ok": False, "error": message[:500]})
        return 1
    except Exception as exc:
        traceback.print_exc()
        emit({"type": "finished", "ok": False, "error": f"{type(exc).__name__}: {exc}"[:500]})
        return 1


if __name__ == "__main__":
    sys.exit(main())
