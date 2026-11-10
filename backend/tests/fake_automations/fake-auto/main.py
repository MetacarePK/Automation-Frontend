"""Fake engine with the module globals the runner patches (DATA_DIR/OUTPUT_DIR/STATE_PATH).
Patient name tokens drive behaviour: SLOW sleeps, CRASH raises, DUP marks a duplicate."""
import time
from pathlib import Path

import openpyxl
from playwright.sync_api import sync_playwright  # noqa: F401  (real engines import it too)

from patients import read_patients, resolve_column_map

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
OUTPUT_DIR = BASE_DIR / "output"
STATE_PATH = BASE_DIR / "auth_state.json"


def run_automation(excel_path=None, column_map=None, workers=None, limit=None, only=None,
                   no_resume=False, make_visit=False, save_visit=False, allow_duplicate=False,
                   no_wait=False, interactive=None, progress_hook=None, retries=1):
    excel_path = Path(excel_path)
    resolved = resolve_column_map(column_map)
    records = read_patients(excel_path, column_map)
    if limit:
        records = records[:limit]
    STATE_PATH.write_text("{}")                      # proves the patched path is honoured
    row_status, captured = {}, {}
    wb = openpyxl.load_workbook(excel_path)
    ws = wb.active
    for rec in records:
        name = str(rec["name"])
        if "CRASH" in name:
            raise RuntimeError("engine blew up")
        if "SLOW" in name:
            time.sleep(60)
        dup = "DUP" in name
        status = "Done" if not name.startswith("MISS") else "Not Created"
        row_status[rec["row"]] = status
        cap = {resolved["visit_id"]: f"V{rec['row']}", resolved["beneficiary_id"]: "INS1",
               resolved["line_charge"]: "$10"} if status == "Done" else {}
        captured.update({rec["row"]: cap})
        if progress_hook:
            progress_hook({rec["row"]: status}, {rec["row"]: cap},
                          {"patient_name": name, "sheet_dos": "11/03/2026",
                           "duplicate_skipped": "V99" if dup else ""})
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUTPUT_DIR / f"{excel_path.stem}_with_status.xlsx"
    wb.save(out)
    done = sum(1 for s in row_status.values() if s.startswith("Done"))
    return {"logged_in": True, "done": done, "total": len(row_status), "captured_columns": captured,
            "status_xlsx_path": out, "results": [], "all_visits": [], "row_status": row_status}
