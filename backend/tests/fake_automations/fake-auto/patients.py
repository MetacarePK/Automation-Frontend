"""Fake engine: same public surface the runner uses from a real engine."""
import openpyxl

DEFAULT_COLUMN_MAP = {"name": "Patient", "dob": "DOB", "visit_id": "Visit ID",
                      "beneficiary_id": "Ins ID", "line_charge": "Charges", "status": "Status"}


def resolve_column_map(column_map):
    return {**DEFAULT_COLUMN_MAP, **{k: v for k, v in (column_map or {}).items() if v}}


def read_patients(xlsx_path, column_map=None):
    cols = resolve_column_map(column_map)
    sheet = openpyxl.load_workbook(xlsx_path, data_only=True).active
    header = [c.value for c in sheet[1]]
    for key in ("name", "dob"):
        if cols[key] not in header:
            raise SystemExit(f"Column {cols[key]!r} not found. Columns are: {header}")
    n, d = header.index(cols["name"]), header.index(cols["dob"])
    return [{"row": i, "name": r[n], "dob": r[d]}
            for i, r in enumerate(sheet.iter_rows(min_row=2, values_only=True), start=2) if r[n]]
