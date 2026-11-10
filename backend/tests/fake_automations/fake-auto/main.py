"""Fake engine with the real engine contract (module globals DATA_DIR/OUTPUT_DIR/STATE_PATH, resume from
its own status sheet, Status column written after every row). Patient-name tokens drive behaviour:
  SLOW    sleeps 60 s              CRASH   raises (permanent engine error)
  DUP     reports a duplicate      MISS    reports Not Created
  NOLOGIN login fails              KILLME  the process dies without a verdict, once per engine folder
  SPAWN   starts a child process and then sleeps (stands in for a browser)
  LEAK    prints a secret (log redaction must hide it)
  BADOUT  reports Done but writes a different status into the sheet (output verification must catch it)
"""
import os
import subprocess
import sys
import time
from pathlib import Path

import openpyxl
from playwright.sync_api import sync_playwright  # noqa: F401  (real engines import it too)
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from patients import read_patients, resolve_column_map

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
OUTPUT_DIR = BASE_DIR / "output"
STATE_PATH = BASE_DIR / "auth_state.json"


def status_xlsx_path_for(excel_path):
    return OUTPUT_DIR / f"{Path(excel_path).stem}_with_status.xlsx"


def read_previous_status(status_path, column_map=None):
    if not Path(status_path).exists():
        return {}
    sheet = openpyxl.load_workbook(status_path, data_only=True).active
    header = [str(c.value).strip().lower() if c.value is not None else "" for c in sheet[1]]
    if "status" not in header:
        return {}
    col = header.index("status") + 1
    return {r: str(sheet.cell(row=r, column=col).value).strip() for r in range(2, sheet.max_row + 1)
            if sheet.cell(row=r, column=col).value}


def run_automation(excel_path=None, column_map=None, workers=None, limit=None, only=None,
                   no_resume=False, make_visit=False, save_visit=False, allow_duplicate=False,
                   no_wait=False, interactive=None, progress_hook=None, retries=1):
    excel_path = Path(excel_path)
    resolved = resolve_column_map(column_map)
    all_records = read_patients(excel_path, column_map)
    if any("NOLOGIN" in str(r["name"]) for r in all_records):
        return {"logged_in": False}
    STATE_PATH.write_text("{\"cookies\": [\"secret\"]}")          # proves the patched path is honoured and is cleaned up
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out = status_xlsx_path_for(excel_path)

    previous = {} if no_resume else read_previous_status(out, column_map)
    done_before = {row for row, st in previous.items() if st.startswith("Done")}
    records = [r for r in all_records if r["row"] not in done_before]
    if limit:
        records = records[:limit]

    wb = openpyxl.load_workbook(excel_path)
    ws = wb.active
    header = [c.value for c in ws[1]]
    status_col = header.index("Status") + 1 if "Status" in header else len(header) + 1
    ws.cell(row=1, column=status_col).value = "Status"
    row_status = {r["row"]: previous.get(r["row"], "Not Created") for r in all_records}
    for row, st in row_status.items():
        ws.cell(row=row, column=status_col).value = st
    wb.save(out)

    captured = {}
    for rec in records:
        name = str(rec["name"])
        if "KILLME" in name:
            marker = BASE_DIR / "killme.marker"
            if not marker.exists():
                marker.write_text("x")
                os._exit(9)                                      # no verdict, like a browser/driver crash
        if "CRASH" in name:
            raise RuntimeError("engine blew up")
        if "LEAK" in name:
            print("debug: login with password: Sup3rSecret! cookie: sid=abc123; theme=dark")
        if "SPAWN" in name:
            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
            (OUTPUT_DIR / "child.pid").write_text(str(child.pid))
            time.sleep(60)
        if "SLOW" in name:
            time.sleep(60)
        dup = "DUP" in name
        status = "Not Created" if name.startswith("MISS") else "Done"
        row_status[rec["row"]] = status
        cap = {resolved["visit_id"]: f"V{rec['row']}", resolved["beneficiary_id"]: "INS1",
               resolved["line_charge"]: "$10"} if status == "Done" else {}
        captured[rec["row"]] = cap
        ws.cell(row=rec["row"], column=status_col).value = "Not Created" if "BADOUT" in name else status
        wb.save(out)                                              # live, after every row, like the real engine
        if progress_hook:
            progress_hook({rec["row"]: status}, {rec["row"]: cap},
                          {"patient_name": name, "sheet_dos": "11/03/2026", "duplicate_skipped": "V99" if dup else ""})
    done = sum(1 for s in row_status.values() if s.startswith("Done"))
    return {"logged_in": True, "done": done, "total": len(row_status), "captured_columns": captured,
            "status_xlsx_path": out, "results": [], "all_visits": [], "row_status": row_status}


def log_in(browser):
    """Like the real engines' log_in(browser) -> (context, page, logged_in). The saved username picks the scenario:
    'blocked' hangs on a challenge page and times out, 'badpass' is refused, anything else signs in."""
    username = ""
    for line in (BASE_DIR / ".env").read_text().splitlines():
        if line.startswith("username="):
            username = line.split("=", 1)[1].strip()
    context = browser.new_context(no_viewport=True)
    page = context.new_page()
    page.goto("data:text/html,<title>Just a moment...</title><body><h1>Checking your browser before accessing</h1>"
              "<input id='other' value='SHOULD-NOT-APPEAR'></body>")
    if username == "blocked":
        raise PlaywrightTimeout("Page.wait_for_selector: Timeout 30000ms exceeded.\nCall log:\n  - waiting for locator(\"input#username\")")
    return context, page, username != "badpass"
