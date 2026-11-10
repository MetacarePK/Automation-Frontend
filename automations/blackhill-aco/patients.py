"""Read patient names from the Excel sheet and look each one up in Practice Mate."""

import csv
import re
from datetime import date, datetime, timedelta
from pathlib import Path

import openpyxl
from playwright.sync_api import Error as PlaywrightError

# Trailing tokens that are generational/credential suffixes, not surnames.
SUFFIXES = {"JR", "SR", "I", "II", "III", "IV", "V", "MD", "DO", "PHD", "DDS", "RN"}

GRID = "#ctl00_phFolderContent_myCustomGrid_myGrid"
SEARCH_BY = "select#ctl00_phFolderContent_ucSearch_lstSearchBy"
SEARCH_COND = "select#ctl00_phFolderContent_ucSearch_lstSearchCondition"
SEARCH_INPUT = "input#ctl00_phFolderContent_ucSearch_txtSearch"
SEARCH_BTN = "input#ctl00_phFolderContent_ucSearch_btnSearch"

# Selecting "Date of Birth" hides txtSearch and swaps in month/day/year boxes
DOB_MONTH = "input#srcFM_0"
DOB_DAY = "input#srcFD_0"
DOB_YEAR = "input#srcFY_0"


def last_name_of(full_name):
    """Take the surname from a patient name, skipping any trailing suffix.

    This sheet's Patient column is 'Last, First [Middle]' - a comma means
    everything before it is the surname:
        'Cruz, Ricardo'        -> 'Cruz'
        'Karadanis, Joyce C'   -> 'Karadanis'
    No comma falls back to reading it as 'First [Middle] Last [Suffix]':
        'ADAM GARTNER'         -> 'GARTNER'
        'Neil B Sinclair II'   -> 'Sinclair'   (II is a suffix, not a surname)
    """
    text = str(full_name).strip()
    if "," in text:
        parts = [p for p in text.split(",", 1)[0].split() if p]
    else:
        parts = [p for p in re.split(r"[\s,]+", text) if p]
    while len(parts) > 1 and parts[-1].upper().strip(".") in SUFFIXES:
        parts.pop()
    return parts[-1] if parts else ""


def first_name_of(full_name):
    """First given name, used to sanity-check a last-name-only match.

    'Last, First [Middle]' -> the first word after the comma; no comma ->
    the first word overall, as if the name were 'First [Middle] Last'.
    """
    text = str(full_name).strip()
    if "," in text:
        parts = [p for p in text.split(",", 1)[1].split() if p]
    else:
        parts = [p for p in re.split(r"[\s,]+", text) if p]
    return parts[0] if parts else ""


# Excel stores a date as a count of days. Serial 1 is 1900-01-01, but Excel
# also believes 1900 was a leap year, so from serial 61 (1900-03-01) onward
# every date sits one day further out than a naive count from the epoch.
EXCEL_EPOCH = datetime(1899, 12, 30)
EXCEL_EPOCH_PRE_LEAP_BUG = datetime(1899, 12, 31)
EXCEL_LEAP_BUG_SERIAL = 60  # Excel's fictitious 1900-02-29
# A DOB or DOS outside this window is a stray number, not a date.
MAX_SERIAL = (datetime(2100, 12, 31) - EXCEL_EPOCH).days


def from_excel_serial(value):
    """Turn an Excel day-count into a datetime, or None if it is not a date.

    Columns formatted 'General' come back from openpyxl as raw numbers rather
    than datetimes, so a DOB arrives as 18283 instead of 1950-01-20. Left
    unconverted it fails BOTH lookup stages: the grid comparison rejects the
    correct patient, and the DOB search has no '/' to split on.
    """
    days = int(value)  # a time-of-day fraction is not part of a date
    if not 1 <= days <= MAX_SERIAL or days == EXCEL_LEAP_BUG_SERIAL:
        return None
    epoch = EXCEL_EPOCH if days > EXCEL_LEAP_BUG_SERIAL else EXCEL_EPOCH_PRE_LEAP_BUG
    return epoch + timedelta(days=days)


def format_dob(value):
    """Render a Patient DOB / DOS cell as MM/DD/YYYY, matching what the grid shows.

    The grid zero-pads (01/20/1950) and the match against it is plain string
    equality, so pad here too - '1/20/1950' must not read as a mismatch.
    """
    # bool is an int subclass; a TRUE cell is not a date
    if value is None or isinstance(value, bool):
        return ""

    # datetime subclasses date, so this covers date-only cells too
    if isinstance(value, date):
        return f"{value.month:02d}/{value.day:02d}/{value.year}"

    if isinstance(value, (int, float)):
        parsed = from_excel_serial(value)
        return f"{parsed.month:02d}/{parsed.day:02d}/{parsed.year}" if parsed else ""

    text = str(value).strip()
    if not text:
        return ""

    match = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})", text)
    if match:
        year, month, day = match.groups()
        return f"{int(month):02d}/{int(day):02d}/{year}"

    match = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})$", text)
    if match:
        month, day, year = match.groups()
        return f"{int(month):02d}/{int(day):02d}/{year}"

    return text


def format_pos(value):
    """The Place of Service code exactly as the sheet gives it.

    No remapping: whatever code the cell holds is the code that gets billed
    (32 -> "32", 11 -> "11", "32 - Nursing Facility" -> "32"). Single digits
    are zero-padded to the two-digit form POS codes use (2 -> "02").
    Returns "" when the cell holds nothing usable; the visit is then NOT
    created (see process_group) rather than billed with the CPT library's
    default place of service.
    """
    if value is None or isinstance(value, bool):
        return ""
    # 32.0 -> 32, so a numeric cell never arrives with a decimal tail
    if isinstance(value, float) and value.is_integer():
        value = int(value)

    text = str(value).strip()
    if not text:
        return ""

    # Tolerate a labelled cell such as "32 - Nursing Facility"
    match = re.match(r"^(\d+)", text)
    if not match:
        return ""

    code = int(match.group(1))
    if code == 0:  # not a place of service
        return ""
    return f"{code:02d}"


# A service line's ICD-10 Pointer can name at most four diagnoses (A..D), but
# every code on the sheet is still entered in the diagnosis boxes - the form
# renders 12 ICD-10 slots, A(1)..L(12), and all of them are used.
MAX_POINTER_DIAGNOSES = 4
MAX_DIAGNOSIS_BOXES = 12
MAX_DIAGNOSIS_CODES = MAX_DIAGNOSIS_BOXES


def split_icd10(value, limit=MAX_DIAGNOSIS_CODES):
    """Split the sheet's ICD10 cell into the codes for Diagnosis A(1)..L(12).

    'F06.34, G47.00, F32.A, F41.9, R41.82'
        -> all five, in order

    Repeats are dropped - the same diagnosis twice on one claim is a data
    error, not two diagnoses. At most `limit` codes are returned (the form's
    12 slots by default; pass None for every code).
    """
    if value is None or isinstance(value, bool):
        return []

    codes = []
    for part in re.split(r"[,;\n]", str(value)):
        code = part.strip().upper()
        if code and code not in codes:
            codes.append(code)
    return codes[:limit]


# CMS-HCC V28 mapping. Its "Diagnosis Code" column holds ICD-10 codes with
# the dots stripped (R60.1 -> R601), so sheet codes are compared the same way.
HCC_CSV = Path(__file__).resolve().parent / "HCC" / "HCC.csv"
HCC_CODE_COLUMN = "Diagnosis Code"
_hcc_codes = None


def hcc_codes():
    """Every ICD-10 code in HCC.csv, dot-less and upper-case. Loaded once."""
    global _hcc_codes
    if _hcc_codes is None:
        if not HCC_CSV.exists():
            raise SystemExit(f"HCC file not found: {HCC_CSV}")
        with open(HCC_CSV, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            if HCC_CODE_COLUMN not in (reader.fieldnames or []):
                raise SystemExit(f"Column {HCC_CODE_COLUMN!r} not found in "
                                 f"{HCC_CSV}. Columns are: {reader.fieldnames}")
            # A code mapping to several HCC categories appears on several
            # rows; a set keeps just the code.
            _hcc_codes = {normalise_code(row[HCC_CODE_COLUMN])
                          for row in reader if row[HCC_CODE_COLUMN]}
    return _hcc_codes


def is_hcc(code):
    """True when the ICD-10 code (dots or not) is in HCC.csv."""
    return normalise_code(code) in hcc_codes()


def is_z_code(code):
    """True when the ICD-10 code starts with Z (status / encounter codes)."""
    return normalise_code(code).startswith("Z")


def prioritise_codes(codes):
    """Order the sheet's codes for Diagnosis A(1)..L(12).

    1. The FIRST code on the sheet, exactly as written, always goes in A(1).
    2. Then every HCC code (found in HCC.csv), in sheet order.
    3. Then every remaining code starting with Z, in sheet order.
    4. Then everything else, in sheet order.

    'R60.1, F03.911, Z79.4, E83.42, Z87.891, N18.9'  (only F03.911 is HCC)
        -> ['R60.1', 'F03.911', 'Z79.4', 'Z87.891', 'E83.42', 'N18.9']

    A Z code that is also an HCC code (Z21, Z79.4 ...) is placed by rule 2.
    The whole list is returned (not capped to 12) so fill_diagnosis_codes can
    still fall back to the next code when Office Ally rejects one. Calling it
    again on its own output changes nothing, which is what lets merged sheet
    rows be re-ordered together without losing the first row's first code.
    """
    if not codes:
        return []
    first, rest = codes[0], codes[1:]
    hcc = [c for c in rest if is_hcc(c)]
    z = [c for c in rest if not is_hcc(c) and is_z_code(c)]
    other = [c for c in rest if not is_hcc(c) and not is_z_code(c)]
    return [first] + hcc + z + other


# Which field goes to which of the sheet's own column headers. A sheet
# doesn't have to use these exact names - read_patients / open_status_workbook
# / read_previous_status / read_previous_captured all accept a column_map
# that overrides any subset of these, e.g. for a sheet whose CPT column is
# actually titled "Procedure Code". Unlisted fields keep this default.
DEFAULT_COLUMN_MAP = {
    "name": "Patient",
    "dob": "Birthdate",
    "provider": "Billing Provider",
    "dos": "Treatment Date",
    "cpt": "CPT",
    "pos": "Place of Service",
    "modifier": "Hospice Modifier",
    "icd10": "ICD",
    "facility": "Ivy Hill",
    "beneficiary_id": "iNS ID",
    "visit_id": "Visit ID ",
    "line_charge": "Line Charges",
    "status": "Status",
}


# Columns the run WRITES to. These must always have a real header name: a
# blank one (the web UI's "-- not in this sheet --") would make every one of
# them land in the same unnamed column, the Visit ID overwriting the Status.
OUTPUT_FIELDS = ("status", "visit_id", "beneficiary_id", "line_charge")


# Other header names the same field has appeared under. A sheet is not
# rewritten to suit this tool, so when the default name is not on the sheet
# an alias is looked for before the field is given up as "not in this
# sheet" - a Facility column silently skipped means claims saved with no
# facility at all.
COLUMN_ALIASES = {
    "facility": ("Ivy Hill", "Facility"),
    "beneficiary_id": ("iNS ID", "Medicare ID", "Beneficiary ID"),
    "visit_id": ("Visit ID",),
    "line_charge": ("Line Charges", "Line Charge", "Charge"),
    "dos": ("Treatment Date", "DOS", "Date of Service"),
    "pos": ("Place of Service", "POS"),
    "icd10": ("ICD", "ICD10", "ICD-10"),
    "modifier": ("Hospice Modifier", "Modifier"),
    "provider": ("Billing Provider", "Provider"),
    "dob": ("Birthdate", "DOB", "Date of Birth"),
}


def match_header(name, headers):
    """The sheet header equal to `name` ignoring case and spacing, or None."""
    target = str(name).strip().lower()
    for header in headers:
        if header is not None and str(header).strip().lower() == target:
            return header
    return None


def resolve_against_headers(fields, headers):
    """Point every field at the header this sheet actually uses for it.

    The mapped name wins whenever the sheet has it; otherwise the field's
    aliases are tried, so 'Facility' is found on a sheet that dropped the
    older 'Ivy Hill' name (and vice versa). A field with no match is left
    as-is - read_patients then treats it as not present.
    """
    resolved = dict(fields)
    for field, name in fields.items():
        if name and match_header(name, headers):
            continue
        for alias in COLUMN_ALIASES.get(field, ()):
            found = match_header(alias, headers)
            if found is not None:
                resolved[field] = found
                break
    return resolved


def resolve_column_map(column_map):
    """DEFAULT_COLUMN_MAP with any of the caller's overrides applied.

    A blank override means "not in this sheet". For an input field that
    turns it off; for an output field it falls back to the default name, so
    the column gets created instead.
    """
    overrides = {k: v for k, v in (column_map or {}).items()
                 if v or k not in OUTPUT_FIELDS}
    return {**DEFAULT_COLUMN_MAP, **overrides}


def read_patients(xlsx_path, column_map=None):
    """Return one record per sheet row: {'name': ..., 'dob': 'MM/DD/YYYY'}."""
    workbook = openpyxl.load_workbook(xlsx_path, data_only=True)
    sheet = workbook[workbook.sheetnames[0]]

    header = [cell.value for cell in sheet[1]]
    fields = resolve_against_headers(resolve_column_map(column_map), header)
    for key in ("name", "dob"):
        if fields[key] not in header:
            raise SystemExit(f"Column {fields[key]!r} not found. Columns are: {header}")
    name_index = header.index(fields["name"])
    dob_index = header.index(fields["dob"])
    provider_index = header.index(fields["provider"]) if fields["provider"] in header else None
    dos_index = header.index(fields["dos"]) if fields["dos"] in header else None
    cpt_index = header.index(fields["cpt"]) if fields["cpt"] in header else None
    facility_index = (header.index(fields["facility"])
                      if fields["facility"] in header else None)
    pos_index = header.index(fields["pos"]) if fields["pos"] in header else None
    icd_index = header.index(fields["icd10"]) if fields["icd10"] in header else None
    modifier_index = header.index(fields["modifier"]) if fields["modifier"] in header else None

    records = []
    for number, row in enumerate(sheet.iter_rows(min_row=2, values_only=True), start=2):
        name = row[name_index]
        if name and str(name).strip():
            raw_dob = row[dob_index]
            dob = format_dob(raw_dob)
            raw_pos = row[pos_index] if pos_index is not None else None
            pos = format_pos(raw_pos)
            records.append({
                "row": number,
                "name": str(name).strip(),
                "dob": dob,
                # A cell that held something but did not parse. Without this the
                # row looks like "no DOB on the sheet" and a surname-only match
                # gets accepted with nothing to verify it against.
                "dob_unparsed": ("" if dob or raw_dob is None
                                 else str(raw_dob).strip()),
                "provider": (str(row[provider_index]).strip()
                             if provider_index is not None
                             and row[provider_index] else ""),
                "dos": format_dob(row[dos_index]) if dos_index is not None else "",
                "cpt": (str(row[cpt_index]).strip()
                        if cpt_index is not None and row[cpt_index] else ""),
                "facility": (str(row[facility_index]).strip()
                             if facility_index is not None
                             and row[facility_index] else ""),
                "pos": pos,
                # What the cell literally held, so the run can show the
                # mapping it applied (sheet 2 -> POS 32) rather than just
                # the result
                "pos_raw": "" if raw_pos is None else str(raw_pos).strip(),
                # Full list, not capped to 4 here - fill_diagnosis_codes needs
                # the extras as fallbacks when an earlier code turns out to be
                # invalid/unrecognised, so it can try the next one instead of
                # failing the whole visit. The first sheet code stays first, then
                # HCC, then Z codes, then the rest (see prioritise_codes).
                "icd10": (prioritise_codes(split_icd10(row[icd_index], limit=None))
                          if icd_index is not None else []),
                "icd10_raw": (str(row[icd_index]).strip()
                              if icd_index is not None and row[icd_index] else ""),
                # Held something we could not read a number out of. Without
                # this the row looks like "no POS on the sheet" and the CPT
                # library's default gets billed unnoticed.
                "pos_unparsed": ("" if pos or raw_pos in (None, "")
                                 else str(raw_pos).strip()),
                "modifier": (str(row[modifier_index]).strip()
                             if modifier_index is not None
                             and row[modifier_index] else ""),
            })
    return records


def open_status_workbook(xlsx_path, column_map=None):
    """Load the sheet once so status can be written and saved after EACH
    visit as the run goes, rather than only once at the very end.

    Column names are matched ignoring surrounding whitespace, so this finds
    the sheet's 'Visit ID ' (trailing space) header correctly. Creates the
    Status column (or any captured column later) if the sheet doesn't have
    it yet. Returns a handle for write_status_rows / save_status_workbook.
    """
    workbook = openpyxl.load_workbook(xlsx_path)
    sheet = workbook[workbook.sheetnames[0]]
    header = [cell.value for cell in sheet[1]]
    fields = resolve_against_headers(resolve_column_map(column_map), header)

    # Which field each written column belongs to, so a name that is not on
    # the sheet can fall back to that field's aliases (Visit ID / iNS ID /
    # Medicare ID) instead of adding a second column beside the one the
    # sheet already has for it.
    # Every name a written column can be asked for - this sheet's own, the
    # tool's default, and the field's aliases - all point back at the field.
    field_of = {}
    for f in OUTPUT_FIELDS:
        for name in (fields[f], DEFAULT_COLUMN_MAP[f]) + COLUMN_ALIASES.get(f, ()):
            if name:
                field_of[str(name).strip().lower()] = f

    def column_for(name):
        target = str(name).strip().lower()
        candidates = [name] + list(
            COLUMN_ALIASES.get(field_of.get(target, ""), ()))
        for candidate in candidates:
            wanted = str(candidate).strip().lower()
            for i, v in enumerate(header):
                if v is not None and str(v).strip().lower() == wanted:
                    return i + 1
        # Sheets sometimes carry stray formatting far past the real data -
        # sheet.max_column can run into the tens of thousands. Anchor the
        # new column to the last header cell that actually has a value,
        # not the sheet's raw (often ghost-inflated) column count.
        used = [i for i, v in enumerate(header) if v not in (None, "")]
        col = (used[-1] + 2) if used else 1
        sheet.cell(row=1, column=col).value = name
        header.append(name)
        return col

    return {
        "workbook": workbook,
        "sheet": sheet,
        "column_for": column_for,
        "status_col": column_for(fields["status"]),
    }


def write_status_rows(handle, row_status, extra_columns_by_row=None):
    """Write a Done / Not Created status for the given rows into a workbook
    opened by open_status_workbook, keyed by sheet row number.

    extra_columns_by_row, if given, is {row_number: {column_name: value}} -
    used to also write back the Visit ID / iNS ID / Line Charges captured for
    that row.
    Does not save - call save_status_workbook to persist to disk.
    """
    sheet = handle["sheet"]
    status_col = handle["status_col"]
    for row_number, status in row_status.items():
        sheet.cell(row=row_number, column=status_col).value = status

    if extra_columns_by_row:
        column_for = handle["column_for"]
        for row_number, columns in extra_columns_by_row.items():
            for name, value in columns.items():
                if not value:
                    continue
                sheet.cell(row=row_number, column=column_for(name)).value = value


def save_status_workbook(handle, out_path):
    """Persist the workbook to out_path.

    Called after every visit (not just once at the end) so the sheet on disk
    reflects progress live as the run goes.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    handle["workbook"].save(out_path)


def read_previous_status(status_path, column_map=None):
    """Row -> Status from a prior run's output workbook, or {} if there isn't
    one yet (first run ever, or the file was removed).

    Lets a resumed run skip sheet rows a previous run already got to Done,
    instead of starting over from row 1 every time the browser gets closed
    partway through a sheet.
    """
    status_column = resolve_column_map(column_map)["status"]
    if not status_path.exists():
        return {}
    workbook = openpyxl.load_workbook(status_path, data_only=True)
    sheet = workbook[workbook.sheetnames[0]]
    # Matched ignoring surrounding whitespace and case, the same way
    # open_status_workbook finds the column when writing it.
    header = [str(v).strip().lower() if v is not None else ""
              for v in (cell.value for cell in sheet[1])]
    target = str(status_column).strip().lower()
    if target not in header:
        return {}
    status_col = header.index(target) + 1

    statuses = {}
    for row in range(2, sheet.max_row + 1):
        value = sheet.cell(row=row, column=status_col).value
        if value:
            statuses[row] = str(value).strip()
    return statuses


def read_previous_captured(status_path, column_map=None):
    """Visit ID / iNS ID / Line Charges already captured for each row in a
    prior run's output workbook, as {row: {column: value}} - only rows and columns that
    actually have a value are included.

    A --resume run never reopens a row it's skipping (it's already Done, and
    the whole point is to not re-touch Practice Mate for it), so there is no
    other way to get that row's captured IDs onto this run's fresh copy of
    the sheet - without this they would go blank again on every resume.
    """
    if not status_path.exists():
        return {}
    workbook = openpyxl.load_workbook(status_path, data_only=True)
    sheet = workbook[workbook.sheetnames[0]]
    header = [str(v).strip() if v is not None else "" for v in
              (cell.value for cell in sheet[1])]
    # Resolved against this sheet's own headers, so a Medicare ID column is
    # read back on a sheet that calls it that rather than "iNS ID".
    fields = resolve_against_headers(resolve_column_map(column_map), header)
    columns = (fields["visit_id"], fields["beneficiary_id"],
               fields["line_charge"])

    col_by_name = {name: header.index(name.strip()) + 1
                   for name in columns if name.strip() in header}
    if not col_by_name:
        return {}

    captured = {}
    for row in range(2, sheet.max_row + 1):
        row_values = {name: sheet.cell(row=row, column=col).value
                      for name, col in col_by_name.items()}
        row_values = {name: value for name, value in row_values.items() if value}
        if row_values:
            captured[row] = row_values
    return captured


def read_grid(page):
    """Pull the results grid as dicts.

    jqGrid renders hidden/duplicated <td>s, so reading cells positionally is
    fragile - getRowData returns them keyed by column name instead.
    """
    return page.evaluate(
        """(grid) => {
            const g = window.jQuery && window.jQuery(grid);
            if (!g || !g.length) return [];
            try { return g.jqGrid('getRowData') || []; } catch (e) { return []; }
        }""",
        GRID,
    )


def wait_for_grid(page, term, timeout_ms=30000):
    """Poll until the grid reflects `term`.

    The Search button does a partial postback with no navigation to await, so
    wait on the data itself: either every row starts with the term, or the grid
    stays empty long enough to count as a genuine no-match.
    """
    empty_ticks = 0
    waited = 0
    while waited < timeout_ms:
        rows = read_grid(page)
        if rows and all(
            str(r.get("LastName", "")).upper().startswith(term.upper()) for r in rows
        ):
            return rows

        empty_ticks = empty_ticks + 1 if not rows else 0
        if empty_ticks >= 6:  # ~3s consistently empty
            return []

        page.wait_for_timeout(500)
        waited += 500

    # Undetermined: the grid never settled into a state that answers the
    # question. Returning what's on screen would hand back the unfiltered
    # default list as if it were results, so signal failure instead.
    return None


def search_last_name(page, last_name):
    """Run one 'Patient Last Name / Starts With' search and return the rows."""
    # These default correctly, and re-selecting triggers an extra postback,
    # so only touch them if the page came back in some other state.
    if page.locator(SEARCH_BY).input_value() != "PatientLastName":
        page.select_option(SEARCH_BY, label="Patient Last Name")
        # Coming back from DOB mode, txtSearch is re-rendered by the postback
        page.wait_for_selector(SEARCH_INPUT, state="visible", timeout=30000)
    if page.locator(SEARCH_COND).input_value() != "StartsWith":
        page.select_option(SEARCH_COND, label="Starts With")

    page.fill(SEARCH_INPUT, last_name)
    page.click(SEARCH_BTN)

    rows = wait_for_grid(page, last_name)
    if rows is None:
        # The click landed before the grid was interactive. Reload and retry
        # once rather than trusting whatever is on screen.
        back_to_patient_list(page)
        page.fill(SEARCH_INPUT, last_name)
        page.click(SEARCH_BTN)
        rows = wait_for_grid(page, last_name)
    if rows is None:
        raise RuntimeError(f"last-name search for {last_name!r} never settled")
    return rows


def grid_signature(page):
    """Identity of what the grid currently shows, to detect a real refresh."""
    return tuple(str(r.get("PatientID", "")) for r in read_grid(page))


def wait_for_dob_grid(page, target_dob, before_signature, timeout_ms=30000):
    """Poll until a row carries `target_dob`, or the grid settles without one.

    A DOB with no match does NOT empty the grid - Practice Mate falls back to
    listing every patient. So emptiness is useless here; instead confirm a row
    actually holds the date, and otherwise wait for the grid to stop changing.
    """
    previous = None
    stable = 0
    waited = 0
    while waited < timeout_ms:
        rows = read_grid(page)
        hits = [r for r in rows
                if str(r.get("DateOfBirthSort", "")).strip() == target_dob]
        if hits:
            return hits

        signature = tuple(str(r.get("PatientID", "")) for r in rows)
        stable = stable + 1 if signature == previous else 0
        previous = signature

        if stable >= 6:  # ~3s unchanged and still no row with this DOB
            # Only a genuine no-match if the grid actually refreshed. If it
            # still shows exactly what it showed before the click, the search
            # never ran and "no match" would be a lie - unless it was empty
            # before and is still empty: an empty grid cannot "fail to
            # refresh", some accounts simply return no rows for a DOB that
            # nobody has, and that is a plain "not found", not an error.
            if signature != before_signature or not signature:
                return []
            return None

        page.wait_for_timeout(500)
        waited += 500

    return None


def search_dob(page, target_dob):
    """Run a 'Date of Birth / Date' search. `target_dob` is MM/DD/YYYY."""
    if not target_dob or "/" not in target_dob:
        return []
    month, day, year = target_dob.split("/")

    if page.locator(SEARCH_BY).input_value() != "PatientDOB":
        page.select_option(SEARCH_BY, label="Date of Birth")
        # The M/D/Y boxes are rendered by the postback that follows
        page.wait_for_selector(DOB_MONTH, state="visible", timeout=30000)

    page.fill(DOB_MONTH, month)
    page.fill(DOB_DAY, day)
    page.fill(DOB_YEAR, year)

    before = grid_signature(page)
    page.click(SEARCH_BTN)
    rows = wait_for_dob_grid(page, target_dob, before)
    if rows is None:
        raise RuntimeError(f"DOB search for {target_dob} never settled")
    return rows


MANAGE_PATIENTS_URL = (
    "https://pm.officeally.com/pm/ManagePatients/ManagePatients.aspx?Tab=P"
)
# Top-nav tab inside the Practice Mate app. Defined here, next to the URL it
# is the alternative to, because back_to_patient_list below is what uses it.
MANAGE_PATIENTS_TAB = "li#manage-patients_tab"


def rows_matching_dob(rows, dob):
    """Rows whose grid DOB equals the sheet DOB - the identity confirmation.

    A surname search can return several unrelated people, so the date of birth
    shown in the results is what tells us we have the right one.
    """
    if not dob:
        return []
    return [r for r in rows
            if str(r.get("DateOfBirthSort", "")).strip() == dob]


def choose_row(rows, first_name, dob=""):
    """Pick which result row to open when a search returns several.

    Date of birth first (it identifies the person), then first name, then
    whatever sorted first.
    """
    if not rows:
        return None

    by_dob = rows_matching_dob(rows, dob)
    if by_dob:
        return by_dob[0]

    target = str(first_name).strip().upper()
    for row in rows:
        if str(row.get("FirstName", "")).strip().upper() == target:
            return row
    return rows[0]


def open_patient(page, patient_id, timeout_ms=60000):
    """Click a result row's name link, opening that patient's chart.

    The name cells carry onclick="EditPatient(<id>)" and navigate in the same
    tab to EditPatient.aspx.
    """
    row = page.locator(f"{GRID} tr[id='{patient_id}']")
    row.wait_for(state="visible", timeout=15000)

    link = row.locator("a[onclick^='EditPatient']").first
    with page.expect_navigation(wait_until="domcontentloaded", timeout=timeout_ms):
        link.click()

    page.wait_for_load_state("load")
    return page.url


def back_to_patient_list(page, timeout_ms=90000):
    """Return to Manage Patients by clicking its nav tab.

    goto() on the ManagePatients URL kept timing out after a save; clicking the
    tab the way a person would is reliable.
    """
    try:
        # Inside the try: anything wrong with the tab must fall through to the
        # URL below, not kill a run whose visits have already been saved.
        tab = page.locator(MANAGE_PATIENTS_TAB)
        tab.wait_for(state="visible", timeout=20000)
        with page.expect_navigation(wait_until="domcontentloaded", timeout=timeout_ms):
            tab.click()
    except Exception:
        page.goto(MANAGE_PATIENTS_URL, wait_until="domcontentloaded", timeout=timeout_ms)

    page.wait_for_selector(SEARCH_BY, state="visible", timeout=30000)
    page.wait_for_selector(f"{GRID} tr.jqgrow", timeout=30000)
    page.wait_for_timeout(2000)


# The patient chart's tabs switch client-side; Visit History is tab 4 and
# reveals tblTab4 / VisitHistoryGrid without changing the URL.
VISIT_TAB_PANEL = "#tblTab4"
VISIT_GRID = "#VisitHistoryGrid"


def read_visits(page):
    """Visit History rows as dicts, keyed by column name."""
    return page.evaluate(
        """(grid) => {
            const g = window.jQuery && window.jQuery(grid);
            if (!g || !g.length) return [];
            try { return g.jqGrid('getRowData') || []; } catch (e) { return []; }
        }""",
        VISIT_GRID,
    )


def visit_on_dos(visits, dos, provider=""):
    """This patient's existing visit on that date of service, or None.

    EVERY row is checked, not just the newest. The grid is sorted newest-first,
    so a row-1-only check misses an older visit on the DOS that a later visit
    now sits above - which is exactly the duplicate worth catching.

    A DOS match alone is not enough: the same patient can be seen by two
    different doctors on the same day, and that is two billable visits, not
    one already billed. When `provider` is given, a DOS match only counts as
    the duplicate if that row's ProviderName also matches (compared the same
    tolerant way as the Visit Info provider fields - last/first name, case
    insensitive); a DOS match under a different provider is left alone so a
    new visit gets created for it instead of being skipped.
    """
    target = str(dos or "").strip()
    if not target:
        return None                      # no DOS to compare - not a duplicate
    provider_last = last_name_of(provider) if provider else ""
    provider_first = first_name_of(provider) if provider else ""
    for visit in visits or []:
        if str(visit.get("DateVisited", "")).strip() != target:
            continue
        if not provider_last:
            return visit                 # no provider on the sheet to check against
        visit_provider = str(visit.get("ProviderName", "")).strip()
        if (last_name_of(visit_provider).upper() == provider_last.upper()
                and first_name_of(visit_provider).upper() == provider_first.upper()):
            return visit
    return None


def open_visit_history(page, timeout_ms=30000):
    """Open the Visit History tab on an already-open patient chart.

    Returns the visit rows. An empty list is a real answer here - a patient can
    legitimately have no visits - so wait for the grid to settle before saying so.
    """
    link = page.get_by_role("link", name="Visit History").first
    link.wait_for(state="visible", timeout=timeout_ms)
    link.click()

    page.wait_for_selector(VISIT_TAB_PANEL, state="visible", timeout=timeout_ms)

    empty_ticks = 0
    waited = 0
    while waited < timeout_ms:
        visits = read_visits(page)
        if visits:
            return visits

        empty_ticks += 1
        if empty_ticks >= 8:  # ~4s with nothing loaded -> genuinely no visits
            return []

        page.wait_for_timeout(500)
        waited += 500

    return read_visits(page)


# Patient chart tab 5. Its "Create New Visit" link leaves the chart entirely
# and lands on EditVisit.aspx in the SAME tab (no popup, despite appearances).
TEMPLATE_TAB_PANEL = "#tblTab5"
# On EditVisit.aspx the tabs are Visit Info (0) / Billing Info (1) / Billing Options (2)
VISIT_INFO_PANEL = "#tblTab0"


def open_template_tab(page, timeout_ms=30000):
    """Open the Template tab on an already-open patient chart."""
    link = page.get_by_role("link", name="Template").first
    link.wait_for(state="visible", timeout=timeout_ms)
    link.click()
    page.wait_for_selector(TEMPLATE_TAB_PANEL, state="visible", timeout=timeout_ms)


def create_new_visit(page, timeout_ms=60000):
    """Click 'Create New Visit' on the Template tab.

    Opens a blank EditVisit form (PageAction=add). Nothing is written to the
    patient's record unless that form is explicitly saved.
    """
    link = page.get_by_text("Create New Visit", exact=True).first
    link.wait_for(state="visible", timeout=30000)

    with page.expect_navigation(wait_until="domcontentloaded", timeout=timeout_ms):
        link.click()

    page.wait_for_load_state("load")
    return page.url


# Primary insurance's Subscriber ID on the Visit Info tab of an (already
# saved) visit. A Secondary insurance section carries the same label with a
# "Secondary" prefix on the control name - not read here.
SUBSCRIBER_ID_FIELD = "#ctl00_phFolderContent_InsuranceSubscriberID"


def open_visit_by_id(page, patient_id, visit_id, timeout_ms=60000):
    """Open an already-saved visit directly by ID, landing on Visit Info.

    Used right after a save to read back what the visit ended up carrying
    (e.g. the Subscriber ID) - going by URL is what EditVisit's own Visit
    History grid links do, so it is reliable even when the page Update left
    us on is not the patient chart itself.
    """
    url = (
        "https://pm.officeally.com/pm/PatientVisits/EditVisit.aspx"
        f"?Tab=V&PageAction=edit&ID={visit_id}&From=EditPatient&PID={patient_id}"
    )
    page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    page.wait_for_load_state("load")
    page.wait_for_selector(VISIT_INFO_PANEL, state="visible", timeout=timeout_ms)
    return page.url


def read_subscriber_id(page, timeout_ms=15000):
    """The primary insurance Subscriber ID on the currently-open visit."""
    field = page.locator(SUBSCRIBER_ID_FIELD)
    field.wait_for(state="visible", timeout=timeout_ms)
    return field.input_value().strip()


def ensure_visit_info_tab(page, timeout_ms=30000):
    """Make sure the Visit Info tab is the one showing on the new-visit form.

    It is the default, but click it if the page came back on another tab.
    """
    panel = page.locator(VISIT_INFO_PANEL)
    for _ in range(int(timeout_ms / 500)):
        if panel.count() and panel.is_visible():
            return True
        page.wait_for_timeout(500)

    page.get_by_role("link", name="Visit Info").first.click()
    panel.wait_for(state="visible", timeout=timeout_ms)
    return True


# Visit Info -> "Provider ID:" -> the "..." button (title="Provider List").
# It opens a real popup WINDOW, not an in-page dialog.
PROVIDER_BROWSE_BTN = "#ctl00_phFolderContent_Button2"
PROVIDER_ID_FIELD = "#ctl00_phFolderContent_ProviderID"
PROVIDER_FIRST_FIELD = "#ctl00_phFolderContent_ProviderFirstName"
PROVIDER_LAST_FIELD = "#ctl00_phFolderContent_ProviderLastName"

POPUP_SEARCH_BY = "select#ctl04_popupBase_ddlSearch"
POPUP_CONDITION = "select#ctl04_popupBase_ddlCondition"
POPUP_INPUT = "input#ctl04_popupBase_txtSearch"
POPUP_SEARCH_BTN = "input#ctl04_popupBase_btnSearch"
POPUP_GRID = "#ctl04_popupBase_grvPopup"

BILLING_INFO_PANEL = "#tblTab1"


def read_provider_rows(popup):
    """Provider List rows as dicts. Columns: Select | Last | First | Specialty | ..."""
    rows = []
    trs = popup.locator(f"{POPUP_GRID} tr")
    for i in range(trs.count()):
        cells = [c.strip() for c in trs.nth(i).locator("td").all_inner_texts()]
        if len(cells) >= 3 and cells[0].lower() == "select":
            rows.append({
                "index": i,
                "last": cells[1],
                "first": cells[2],
                "specialty": cells[3] if len(cells) > 3 else "",
                "npi": cells[-1] if len(cells) > 4 else "",
            })
    return rows


def choose_provider(rows, last, first):
    """Pick the provider row, disambiguating by first name when needed.

    The list genuinely contains repeated surnames (SHAFER AMANDA vs SHAFER
    DANIELLE, PTACEK MARK vs PTACEK TRAVIS), so a lone surname match is only
    safe when it is the *only* one.
    """
    same_last = [r for r in rows if r["last"].upper() == last.upper()]
    if not same_last:
        raise RuntimeError(f"no provider with last name {last!r} in the Provider List")

    if len(same_last) == 1:
        return same_last[0]

    exact = [r for r in same_last if r["first"].upper() == first.upper()]
    if len(exact) == 1:
        return exact[0]
    if not exact:
        names = ", ".join(f"{r['first']} {r['last']}" for r in same_last)
        raise RuntimeError(
            f"{len(same_last)} providers named {last!r} ({names}) and none "
            f"has first name {first!r}"
        )
    raise RuntimeError(f"{len(exact)} providers match {first} {last} - ambiguous")


def select_provider(page, context, provider_name, timeout_ms=30000, attempts=2):
    """Open the Provider List popup, find the provider, and select them.

    Returns the chosen row plus what the form actually ended up holding.

    The popup's "Select" link has occasionally closed the popup out from
    under the click (Playwright reports "Target page, context or browser
    has been closed" mid-click, even though the element was visible and
    stable a moment before) - a transient timing race, not a real failure,
    since retrying the whole popup from scratch reliably works. Retried
    here rather than left to fail the whole visit; a genuine "no such
    provider" (RuntimeError from choose_provider) is a different exception
    type and is NOT retried - that is a real data problem, not a glitch.
    """
    last = last_name_of(provider_name)
    first = first_name_of(provider_name)

    for attempt in range(attempts):
        try:
            with context.expect_page(timeout=timeout_ms) as popup_info:
                page.click(PROVIDER_BROWSE_BTN)
            popup = popup_info.value
            popup.wait_for_load_state("load")
            popup.wait_for_selector(POPUP_INPUT, state="visible", timeout=timeout_ms)

            if popup.locator(POPUP_SEARCH_BY).input_value() != "s.LastName":
                popup.select_option(POPUP_SEARCH_BY, label="Last Name")

            popup.fill(POPUP_INPUT, last)
            popup.click(POPUP_SEARCH_BTN)
            popup.wait_for_load_state("load")
            popup.wait_for_timeout(1500)

            rows = read_provider_rows(popup)
            try:
                chosen = choose_provider(rows, last, first)
            except RuntimeError:
                popup.close()
                raise

            popup.locator(f"{POPUP_GRID} tr").nth(chosen["index"]) \
                 .locator("a", has_text="Select").first.click()

            # Selecting writes back to the opener and closes the popup
            for _ in range(int(timeout_ms / 500)):
                if popup.is_closed():
                    break
                page.wait_for_timeout(500)

            page.wait_for_timeout(1500)
            return chosen, {
                "id": page.locator(PROVIDER_ID_FIELD).input_value(),
                "first": page.locator(PROVIDER_FIRST_FIELD).input_value(),
                "last": page.locator(PROVIDER_LAST_FIELD).input_value(),
            }
        except PlaywrightError as exc:
            if attempt + 1 >= attempts:
                raise
            print(f"           (Provider List popup closed unexpectedly "
                  f"mid-click - retrying: {str(exc).splitlines()[0][:100]})")


def open_billing_info_tab(page, timeout_ms=30000):
    """Switch the visit form to the Billing Info tab."""
    page.get_by_role("link", name="Billing Info").first.click()
    page.wait_for_selector(BILLING_INFO_PANEL, state="visible", timeout=timeout_ms)


def provider_on_form(page):
    """Whatever provider the visit form is currently carrying."""
    return {
        "id": page.locator(PROVIDER_ID_FIELD).input_value().strip(),
        "first": page.locator(PROVIDER_FIRST_FIELD).input_value().strip(),
        "last": page.locator(PROVIDER_LAST_FIELD).input_value().strip(),
    }


def ensure_provider(page, context, provider_name, timeout_ms=30000):
    """Make the visit form carry `provider_name`, opening the popup only if needed.

    A new visit arrives pre-filled from the patient's template, which is often
    already the right provider. Checking the First/Last fields first avoids the
    popup round-trip entirely in that case.

    Returns (how, provider_dict) where `how` is "already on form" or "popup".
    """
    last = last_name_of(provider_name)
    first = first_name_of(provider_name)

    current = provider_on_form(page)
    if (current["last"].upper() == last.upper()
            and current["first"].upper() == first.upper()):
        return "already on form", current

    _, updated = select_provider(page, context, provider_name, timeout_ms)
    return "popup", updated


# Billable Line Items live under ucBillingCPT (ucNonBillingCPT is a separate,
# non-billable section - do not confuse the two).
LINE = "#ctl00_phFolderContent_ucVisitLineItem_ucBillingCPT_"

# The field suffixes are 0-BASED while the on-screen "Line No" column is
# 1-based: DOS0 is the first row, DOS1 is the second. Passing line=1 writes to
# the row labelled 2 and leaves row 1 blank.
FIRST_LINE = 0


def selected_label(select_locator):
    """Visible text of a <select>'s current option."""
    return select_locator.evaluate(
        "e => e.selectedIndex >= 0 ? e.options[e.selectedIndex].text.trim() : ''"
    )


def fill_line_dates(page, dos, line=FIRST_LINE):
    """Put the sheet's DOS into both From and To for a billable line."""
    page.fill(f"{LINE}DOS{line}", dos)
    page.fill(f"{LINE}ToDOS{line}", dos)
    # Blur so the page runs its own date validation/reformatting
    page.locator(f"{LINE}ToDOS{line}").press("Tab")
    page.wait_for_timeout(800)
    return {
        "from": page.locator(f"{LINE}DOS{line}").input_value(),
        "to": page.locator(f"{LINE}ToDOS{line}").input_value(),
    }


def read_cpt_rows(popup):
    """User CPT Code List rows. Columns: Select | CPT | Description | POS | Mods | Charge."""
    rows = []
    trs = popup.locator(f"{POPUP_GRID} tr")
    for i in range(trs.count()):
        cells = [c.strip() for c in trs.nth(i).locator("td").all_inner_texts()]
        if len(cells) >= 3 and cells[0].lower() == "select":
            rows.append({
                "index": i,
                "cpt": cells[1],
                "description": cells[2],
                "pos": cells[3] if len(cells) > 3 else "",
                "charge": cells[-1] if len(cells) > 4 else "",
            })
    return rows


def select_cpt(page, context, cpt_code, line=FIRST_LINE, timeout_ms=30000, attempts=2):
    """Open the CPT '...' popup for a line, search the code, and select it.

    Requires an exact code match - a 'Starts With' search for 99308 would also
    return 99308-adjacent codes, and billing the wrong CPT is not recoverable
    by eyeballing a list.

    The popup's "Select" link has occasionally closed the popup out from
    under the click (Playwright reports "Target page, context or browser
    has been closed" mid-click, even though the element was visible and
    stable a moment before) - a transient timing race, not a real failure,
    since retrying the whole popup from scratch reliably works. Retried
    here rather than failing the whole visit; a genuine "CPT not in the
    list" is a different exception type and is NOT retried - that is a
    real data problem, not a glitch.
    """
    cpt_code = str(cpt_code).strip()

    for attempt in range(attempts):
        try:
            with context.expect_page(timeout=timeout_ms) as popup_info:
                page.click(f"{LINE}btnUserCPT{line}")
            popup = popup_info.value
            popup.wait_for_load_state("load")
            popup.wait_for_selector(POPUP_INPUT, state="visible", timeout=timeout_ms)

            if selected_label(popup.locator(POPUP_SEARCH_BY)) != "Code":
                popup.select_option(POPUP_SEARCH_BY, label="Code")

            popup.fill(POPUP_INPUT, cpt_code)
            popup.click(POPUP_SEARCH_BTN)
            popup.wait_for_load_state("load")
            popup.wait_for_timeout(1500)

            rows = read_cpt_rows(popup)
            exact = [r for r in rows if r["cpt"] == cpt_code]
            if not exact:
                found = ", ".join(r["cpt"] for r in rows[:6]) or "nothing"
                popup.close()
                raise RuntimeError(f"CPT {cpt_code!r} not in the User CPT list (search returned {found})")

            chosen = exact[0]
            popup.locator(f"{POPUP_GRID} tr").nth(chosen["index"]) \
                 .locator("a", has_text="Select").first.click()

            for _ in range(int(timeout_ms / 500)):
                if popup.is_closed():
                    break
                page.wait_for_timeout(500)

            page.wait_for_timeout(1200)
            return chosen, {
                "cpt": page.locator(f"{LINE}CPT{line}").input_value(),
                "pos": page.locator(f"{LINE}PlaceOfService{line}").input_value(),
                "charge": page.locator(f"{LINE}Charge{line}").input_value(),
                "quantity": page.locator(f"{LINE}Quantity{line}").input_value(),
            }
        except PlaywrightError as exc:
            if attempt + 1 >= attempts:
                raise
            print(f"           (CPT popup closed unexpectedly mid-click - "
                  f"retrying: {str(exc).splitlines()[0][:100]})")


# Finds the "Line Charge" column by its header text: locate the line's own
# Charge input, walk up to its table row, find the header cell reading
# "Line Charge" in the same table, and read the cell at that column position
# in the line's row (its input's value, else its text).
_LINE_CHARGE_BY_HEADER_JS = """
charge => {
  const input = document.getElementById(charge);
  if (!input) return null;
  const row = input.closest('tr');
  const table = row && row.closest('table');
  if (!table) return null;
  const norm = t => (t || '').replace(/\\s+/g, ' ').trim().toLowerCase();
  let col = -1;
  for (const cell of table.querySelectorAll('th, td')) {
    if (norm(cell.innerText) === 'line charge' && !cell.querySelector('table')) {
      col = cell.cellIndex; break;
    }
  }
  if (col < 0 || col >= row.cells.length) return null;
  const cell = row.cells[col];
  const field = cell.querySelector('input, select');
  return (field ? field.value : cell.innerText || '').trim();
}
"""


def read_line_charge(page, line=FIRST_LINE):
    """The charge Billing Info shows for a billable line, as displayed.

    Tries, in order: an element whose id ends in LineCharge<n>; the column
    headed "Line Charge" in the line's own row; the line's Charge input.
    Returns (value, source) so a run shows which one was read.
    """
    by_id = page.locator(f"[id*='ucBillingCPT_'][id$='LineCharge{line}' i]")
    if by_id.count():
        value = by_id.first.evaluate(
            "e => ('value' in e && e.tagName !== 'BUTTON') ? e.value : e.innerText")
        return str(value or "").strip(), "Line Charge"

    charge_id = f"{LINE.lstrip('#')}Charge{line}"
    try:
        value = page.evaluate(_LINE_CHARGE_BY_HEADER_JS, charge_id)
    except Exception:
        value = None
    if value and re.search(r"\d", value):
        return value, "Line Charge column"

    el = page.locator(f"{LINE}Charge{line}")
    if el.count():
        return str(el.first.input_value() or "").strip(), "Charge field"
    return "", "not found"


def read_line_charges(page, count):
    """Charge for each of the first `count` billable lines, in line order."""
    return [read_line_charge(page, line=i) for i in range(count)]


def set_place_of_service(page, pos, line=FIRST_LINE, timeout_ms=30000):
    """Put the sheet's POS on a billable line. Step [20].

    Must run AFTER select_cpt: choosing a CPT fills POS in from the CPT
    library, so writing the sheet value first would just be overwritten.

    The field is a plain input in some Practice Mate builds and a dropdown in
    others, so branch on the tag rather than assume. Returns (how, value_on_form).
    """
    pos = str(pos).strip()
    if not pos:
        return "no POS on sheet", ""

    field = page.locator(f"{LINE}PlaceOfService{line}")
    field.wait_for(state="visible", timeout=timeout_ms)

    current = field.input_value().strip()
    if current == pos:
        # The CPT default already agrees - skip the postback a write triggers
        return "already set by CPT", current

    tag = field.evaluate("e => e.tagName.toLowerCase()")
    if tag == "select":
        try:
            field.select_option(value=pos)
        except Exception:
            # Some builds label options "32 - Nursing Facility" and carry a
            # differently padded value, so fall back to matching the visible text
            field.select_option(label=re.compile(rf"^\s*0*{re.escape(pos.lstrip('0'))}\b"))
        how = "dropdown"
    else:
        field.fill(pos)
        field.press("Tab")  # blur so the page runs its own validation
        how = "typed"

    page.wait_for_timeout(800)
    written = field.input_value().strip()
    if written.lstrip("0") != pos.lstrip("0"):
        raise RuntimeError(
            f"POS {pos!r} did not stick on line {line + 1} - field now holds {written!r}"
        )
    return how, written


def set_modifier_a(page, modifier, line=FIRST_LINE, timeout_ms=15000):
    """Put the sheet's Hospice Modifier into the line's Modifier A box.

    The line item carries four plain-text modifier boxes (A-D); only A is
    used here. Like POS, it is a per-line field, so it takes the same
    line=FIRST_LINE convention as select_cpt/set_place_of_service.
    """
    modifier = str(modifier).strip()
    if not modifier:
        return "no modifier on sheet", ""

    field = page.locator(f"{LINE}ModifierA{line}")
    field.wait_for(state="visible", timeout=timeout_ms)
    field.fill(modifier)
    field.press("Tab")  # blur so the page runs its own validation
    page.wait_for_timeout(600)

    written = field.input_value().strip()
    if written.upper() != modifier.upper():
        raise RuntimeError(
            f"Modifier A {modifier!r} did not stick on line {line + 1} - "
            f"field now holds {written!r}"
        )
    return "typed", written


# Diagnosis Codes A(1)..L(12) on the Billing Info tab. Every box carries the
# same placeholder, so locating by it gives them in document order without
# needing to know the generated control ids.
DIAGNOSIS_PLACEHOLDER = "Start typing code or description"

# Where the suggestion list turns up. Practice Mate is WebForms, so the AJAX
# Control Toolkit extender is the likely one, but jQuery UI is used elsewhere
# in the app - try each and take whichever actually has visible items.
# The page keeps ~49 ui-autocomplete <ul>s alive, one per input, and a used
# one keeps its stale <li> while hidden. Selectors MUST be scoped with
# :visible - matching items across all menus returns a hidden leftover first,
# and judging visibility from that first item hides the real open menu.
AUTOCOMPLETE_CANDIDATES = (
    "ul.ui-autocomplete:visible li.ui-menu-item",
    "ul.ui-menu:visible li.ui-menu-item",
    "ul.ui-autocomplete:visible li",
    "[id*='completionList']:visible li",
    "[id*='CompletionList']:visible li",
    "ul.ajax__autocomplete:visible li",
    "[role='listbox']:visible [role='option']",
    "ul.typeahead:visible li",
    ".autocomplete-suggestions:visible .autocomplete-suggestion",
)


# The form renders BOTH an ICD-9 and an ICD-10 set of 12 boxes with identical
# placeholders; the ICD-9 ones stay hidden. Matching on placeholder alone picks
# the hidden dd_09_1 first and every wait times out, so target ICD-10 by id.
DIAGNOSIS_BOX_ICD10 = "input[id*='ucDiagnosisCodes_dd_10_']"
# Picking a suggestion splits it in two: the code lands in dc_10_N and the
# description in dd_10_N. Verify against the code box, not the one typed into.
DIAGNOSIS_CODE_ICD10 = "input[id*='ucDiagnosisCodes_dc_10_']"


def diagnosis_code_boxes(page):
    """The read-back code fields that hold the committed ICD-10 code."""
    return page.locator(DIAGNOSIS_CODE_ICD10)


def diagnosis_boxes(page):
    """The visible ICD-10 Diagnosis inputs, in A(1)..L(12) order."""
    return page.locator(DIAGNOSIS_BOX_ICD10)


def normalise_code(text):
    """Compare ICD-10 codes ignoring dots, case and any trailing description."""
    return re.sub(r"[^A-Z0-9]", "", str(text).upper())


def visible_suggestions(page):
    """(selector, locator) for whichever suggestion menu is actually open.

    Every candidate is :visible-scoped, so a non-zero count means a real open
    menu - no need to inspect the first item, which may belong to a leftover.
    """
    for selector in AUTOCOMPLETE_CANDIDATES:
        items = page.locator(selector)
        try:
            if items.count():
                return selector, items
        except Exception:
            continue
    return None, None


def dump_diagnosis_dom(page, limit=4000):
    """Print what the page actually looks like around the diagnosis boxes.

    Only runs when the suggestion list could not be found. The generated ids
    and the widget markup are not knowable in advance, so capture them rather
    than guess again on the next run.
    """
    print("\n  --- diagnosis autocomplete not recognised, dumping DOM ---")
    try:
        boxes = diagnosis_boxes(page)
        print(f"  boxes found by placeholder: {boxes.count()}")
        if boxes.count():
            info = boxes.first.evaluate(
                "e => ({id: e.id, name: e.name, cls: e.className, "
                "parent: e.parentElement && e.parentElement.outerHTML.slice(0, 400)})")
            print(f"  first box id={info['id']!r} name={info['name']!r} class={info['cls']!r}")
            print(f"  parent: {info['parent']}")
    except Exception as exc:
        print(f"  could not inspect the boxes: {exc}")

    try:
        # Anything that appeared and looks like a popup list
        found = page.evaluate(
            """() => [...document.querySelectorAll('ul, [role=listbox], div')]
                 .filter(e => {
                     const r = e.getBoundingClientRect();
                     const s = getComputedStyle(e);
                     return r.height > 0 && r.width > 0 && s.display !== 'none'
                         && (s.position === 'absolute' || s.position === 'fixed')
                         && e.querySelectorAll('li, [role=option], div').length > 0
                         && e.innerText.trim().length > 0;
                 })
                 .slice(0, 5)
                 .map(e => ({tag: e.tagName, id: e.id, cls: e.className,
                             html: e.outerHTML.slice(0, 500)}))"""
        )
        if found:
            print(f"  {len(found)} floating list(s) on the page:")
            for f in found:
                print(f"    <{f['tag']} id={f['id']!r} class={f['cls']!r}>")
                print(f"      {f['html'][:limit]}")
        else:
            print("  no floating list elements visible")
    except Exception as exc:
        print(f"  could not scan for popups: {exc}")
    print("  --- end dump ---\n")


def close_suggestions(page, timeout_ms=3000):
    """Close whatever menu is open so it stops covering the next box.

    Never hide the <ul>s wholesale: an inline display:none sticks and every
    autocomplete on the page stays dead for the rest of the session.
    """
    waited = 0
    while waited < timeout_ms:
        _, items = visible_suggestions(page)
        if not items or not items.count():
            return True
        page.keyboard.press("Escape")
        page.wait_for_timeout(250)
        waited += 250
    return False


def pick_suggestion(page, code, timeout_ms=12000):
    """Wait for the suggestion list and click the row for `code`.

    Returns the text that was clicked, or None if no list ever appeared.
    """
    target = normalise_code(code)
    waited = 0
    while waited < timeout_ms:
        selector, items = visible_suggestions(page)
        if items:
            count = items.count()
            # Prefer the row that actually starts with this code; the list is
            # a code+description search, so a plain "contains" can land on a
            # different code that merely mentions it.
            best = None
            for i in range(count):
                text = items.nth(i).inner_text().strip()
                flat = normalise_code(text)
                if flat.startswith(target):
                    best = (i, text)
                    break
                if best is None and target in flat:
                    best = (i, text)
            if best:
                index, text = best
                items.nth(index).click()
                page.wait_for_timeout(600)
                close_suggestions(page)
                return text
        page.wait_for_timeout(300)
        waited += 300
    return None


def type_and_pick(page, index, code, timeout_ms):
    """One attempt at typing `code` into box `index` and clicking its suggestion."""
    close_suggestions(page)
    box = diagnosis_boxes(page).nth(index)
    box.wait_for(state="visible", timeout=timeout_ms)
    box.click()
    box.fill("")
    box.press_sequentially(code, delay=90)

    # Committing the previous code re-renders this section; if that landed
    # mid-typing the box is now empty and there is nothing to match on.
    if not box.input_value().strip():
        return None, ""

    picked = pick_suggestion(page, code, timeout_ms=6000)
    if picked is None:
        # Some builds only fire the lookup once a separator is typed
        box.press("Space")
        picked = pick_suggestion(page, code, timeout_ms=6000)
    return picked, box.input_value().strip()


def committed_code(page, index):
    """The ICD-10 code actually recorded in box `index`, if any."""
    return diagnosis_code_boxes(page).nth(index).input_value().strip()


def fill_diagnosis_code(page, index, code, timeout_ms=12000, attempts=3):
    """Type one ICD-10 code into diagnosis box `index` and commit it.

    The box is an autocomplete: typing alone sets no diagnosis, the suggestion
    has to be clicked. What proves it worked is the code landing in dc_10_N -
    not whether we saw ourselves click, since the click can register while the
    menu disappears too fast to observe.
    """
    label = f"{chr(65 + index)}({index + 1})"
    wanted = normalise_code(code)

    for attempt in range(attempts):
        if normalise_code(committed_code(page, index)).startswith(wanted):
            break  # already in place from a previous attempt

        type_and_pick(page, index, code, timeout_ms)

        if normalise_code(committed_code(page, index)).startswith(wanted):
            break
        # Let the postback from the previous commit finish, then try again
        page.wait_for_timeout(1500)

    written = committed_code(page, index)
    description = diagnosis_boxes(page).nth(index).input_value().strip()

    if not normalise_code(written).startswith(wanted):
        # Nothing was committed. Text left in the typing box is not a
        # diagnosis, so this must fail rather than look successful.
        dump_diagnosis_dom(page)
        raise RuntimeError(
            f"diagnosis {label}: {code!r} was never committed after "
            f"{attempts} attempts - code box holds {written!r}"
        )

    return "picked from list", f"{written} - {description}"[:70]


def fill_diagnosis_codes(page, codes, timeout_ms=12000, limit=MAX_DIAGNOSIS_CODES):
    """Fill Diagnosis A(1)..L(12) from the sheet's ICD10 codes. Step [21].

    `codes` may hold more than `limit` entries. If Office Ally's autocomplete
    never recognises one (an invalid or unsupported code), that code is
    skipped and the next one in the list is tried in its place instead of
    failing the whole visit - a sheet with 9 ICD10 codes and one bad one
    among the first 12 should still end up with 12 entered diagnoses.

    Returns (written, skipped):
      written  one {code, box, value, how} per diagnosis actually filled,
               at most `limit` entries
      skipped  codes that were tried and rejected by the autocomplete
    """
    written = []
    skipped = []
    for code in codes:
        if len(written) >= limit:
            break
        if written or skipped:
            # Each commit (or failed attempt) triggers a re-render; let it
            # finish before touching the box again.
            page.wait_for_timeout(1500)
        box_index = len(written)
        try:
            how, value = fill_diagnosis_code(page, box_index, code, timeout_ms)
        except RuntimeError:
            print(f"           (diagnosis {code!r} not recognised by "
                  f"Office Ally's autocomplete - skipping it, trying the "
                  f"next code)")
            skipped.append(code)
            continue
        written.append({
            "box": f"{chr(65 + box_index)}({box_index + 1})",
            "code": code,
            "value": value,
            "how": how,
        })
    return written, skipped


def diagnosis_pointer(count):
    """Pointer digits for the diagnoses that were filled. Step [22].

    A line can point at four diagnoses at most, so 5 or more still give '1234'
    (the first four in priority order: first sheet code, then HCC).

    4 diagnoses -> '1234'   (A B C D)
    3 diagnoses -> '123'    (A B C)
    2 diagnoses -> '12'
    1 diagnosis -> '1'
    """
    return "".join(str(i + 1) for i in range(min(count, MAX_POINTER_DIAGNOSES)))


def fill_diagnosis_pointer(page, count, line=FIRST_LINE):
    """Write the ICD-10 Pointer for a billable line and read back what stuck."""
    value = diagnosis_pointer(count)
    if not value:
        return ""

    field = page.locator(f"{LINE}DiagnosisCode{line}")
    field.wait_for(state="visible", timeout=15000)
    field.fill("")
    field.fill(value)
    field.press("Tab")
    page.wait_for_timeout(800)

    written = field.input_value().strip()
    if written != value:
        raise RuntimeError(
            f"ICD-10 Pointer: wrote {value!r} but the field holds {written!r}"
        )
    return written


# Third tab on the visit form: Visit Info (0) / Billing Info (1) / Billing Options (2)
BILLING_OPTIONS_PANEL = "#tblTab2"


def open_billing_options_tab(page, timeout_ms=30000):
    """Switch the visit form to the Billing Options tab. Step [23]."""
    page.get_by_role("link", name="Billing Options").first.click()
    page.wait_for_selector(BILLING_OPTIONS_PANEL, state="visible", timeout=timeout_ms)


# Two Entity Type dropdowns exist on the visit form, on different tabs:
#   Visit Info      -> selRenderingProviderEntityType  (Rendering Provider)
#   Billing Options -> selBillingProviderEntityType    (Billing Provider)
# Both offer Individual (1) / Organization (2).
ENTITY_TYPE_BILLING = "select#ctl00_phFolderContent_selBillingProviderEntityType"
ENTITY_TYPE_RENDERING = "select#ctl00_phFolderContent_selRenderingProviderEntityType"
ENTITY_TYPE_VALUES = {"individual": "1", "organization": "2"}


def set_entity_type(page, choice="Organization", selector=ENTITY_TYPE_BILLING,
                    timeout_ms=15000):
    """Set an Entity Type dropdown. Step [24].

    It is a native <select>, so this picks the option directly rather than
    opening a menu, then reads it back to confirm.
    """
    wanted = ENTITY_TYPE_VALUES.get(str(choice).strip().lower())
    if wanted is None:
        raise RuntimeError(f"Entity Type {choice!r} must be Individual or Organization")

    field = page.locator(selector)
    field.wait_for(state="visible", timeout=timeout_ms)
    field.select_option(value=wanted)
    page.wait_for_timeout(600)

    written = field.input_value()
    if written != wanted:
        raise RuntimeError(
            f"Entity Type: selected {choice!r} but the field holds {written!r}"
        )
    return selected_label(field)


def group_key(record):
    """What makes two sheet rows the same visit: patient, DOS, provider, POS.

    The CPT and the ICD10 are deliberately NOT part of it. Rows that differ
    only by CPT and/or diagnoses are one visit with several billable lines;
    their diagnoses are combined by merge_row_diagnoses.
    """
    return (
        str(record.get("name", "")).strip().upper(),
        record.get("dob", ""),
        record.get("dos", ""),
        str(record.get("provider", "")).strip().upper(),
        record.get("pos_raw", ""),
    )


# Every diagnosis of EACH merged sheet row is entered, up to the form's 12
# boxes A(1)..L(12). Every line's ICD-10 Pointer is at most 1234.
MAX_MERGED_DIAGNOSES = MAX_DIAGNOSIS_BOXES


# Foot-care CPTs get a fixed diagnosis order instead of the generic one:
# B35.1 (tinea unguium) first, then I73.9 - or E11.9 when there is no I73.9 -
# second, then the usual HCC / Z / rest order for everything else. A visit
# carrying any of these CPTs follows the rule; if B35.1 is not among its
# codes the claim is still created and the sheet's Status says so.
FOOT_CPTS = frozenset({"11721", "11720", "11056", "11055", "11057"})
FOOT_FIRST_CODE = "B35.1"
FOOT_SECOND_CODES = ("I73.9", "E11.9")   # in priority order - I73.9 wins


def is_foot_visit(cpts):
    """True when any of the visit's CPTs is one of FOOT_CPTS."""
    return any(str(c).strip() in FOOT_CPTS for c in cpts)


def _find_code(codes, wanted):
    """The sheet's own spelling of `wanted` in `codes` (dots ignored), or None."""
    target = normalise_code(wanted)
    for code in codes:
        if normalise_code(code) == target:
            return code
    return None


def prioritise_foot_codes(codes):
    """Order for a foot-care visit: B35.1, then I73.9 (else E11.9), then the rest.

    The rest keeps the generic order (sheet's first remaining code, then
    HCC, then Z, then the rest). B35.1 missing does not stop the second code
    from moving up: I73.9 (else E11.9) simply takes A(1). The whole list is
    returned so a code Office Ally rejects can still fall back to the next.
    """
    front = []
    first = _find_code(codes, FOOT_FIRST_CODE)
    if first:
        front.append(first)
    for wanted in FOOT_SECOND_CODES:
        second = _find_code(codes, wanted)
        if second:
            front.append(second)
            break
    # Everything else follows the generic rule unchanged (so with no B35.1 /
    # I73.9 / E11.9 at all the result is exactly prioritise_codes' order)
    rest = [c for c in codes if c not in front]
    return front + prioritise_codes(rest)


def missing_foot_code(cpts, codes):
    """FOOT_FIRST_CODE when a foot-care visit's diagnoses lack it, else ''."""
    if is_foot_visit(cpts) and _find_code(codes, FOOT_FIRST_CODE) is None:
        return FOOT_FIRST_CODE
    return ""


def merge_row_diagnoses(rows):
    """Combine the diagnoses of the sheet rows that make up one visit.

    Takes every code of each row (already in priority order), in row order,
    dropping any code already taken so no diagnosis is entered twice. Codes
    past the form's 12 boxes are kept as fallbacks only - they are tried when
    Office Ally rejects an earlier one, never filled otherwise.

    A visit with a foot-care CPT (FOOT_CPTS) is ordered by
    prioritise_foot_codes instead.

    Returns (codes, limit): the candidate list and how many boxes to fill
    (always the form's 12; fewer get filled when there are fewer codes).
    """
    codes = []
    for row in rows:
        for code in row.get("icd10") or []:
            if code not in codes:
                codes.append(code)
    # Same rule across the whole visit: the first row's first code stays in
    # A(1), then HCC, then Z, then the rest (stable: row order otherwise kept)
    if is_foot_visit(r.get("cpt", "") for r in rows):
        return prioritise_foot_codes(codes), MAX_MERGED_DIAGNOSES
    return prioritise_codes(codes), MAX_MERGED_DIAGNOSES


def group_records(records):
    """Fold duplicate rows that differ only by CPT into one visit.

    Two sheet rows for the same patient, same DOS, same provider and same
    diagnoses are not two visits - they are one visit with two billable lines.
    The first row fills the whole form; each extra CPT only adds a line.

    `modifiers` is kept parallel to `cpts` (same length, same order) - each
    entry is the Hospice Modifier cell from the same sheet row as the CPT at
    that index, since a second CPT is not necessarily billed with the first
    CPT's modifier.

    The group's "record" is a copy of the first row whose icd10 / icd10_raw
    are the combined diagnoses of every row (see merge_row_diagnoses);
    "dx_limit" is how many diagnosis boxes to fill.

    Returns [{"record": <first row>, "cpts": [...], "modifiers": [...],
              "rows": [...], "dx_limit": n}, ...]
    """
    groups = []
    seen = {}
    for record in records:
        key = group_key(record)
        cpt = str(record.get("cpt", "")).strip()
        modifier = str(record.get("modifier", "")).strip()
        if key in seen:
            group = seen[key]
            group["rows"].append(record)
            # Keep repeats: one sheet row is one billable line, so the same
            # CPT twice means two lines, not a row to throw away.
            if cpt:
                group["cpts"].append(cpt)
                group["modifiers"].append(modifier)
        else:
            group = {
                "record": record,
                "cpts": [cpt] if cpt else [],
                "modifiers": [modifier] if cpt else [],
                "rows": [record],
            }
            seen[key] = group
            groups.append(group)

    for group in groups:
        codes, limit = merge_row_diagnoses(group["rows"])
        group["dx_limit"] = limit
        group["missing_icd"] = missing_foot_code(group["cpts"], codes)
        if len(group["rows"]) > 1:
            group["record"] = {
                **group["record"],
                "icd10": codes,
                "icd10_raw": ", ".join(codes),
            }
        elif is_foot_visit(group["cpts"]):
            # One row: keep its raw text, but enter the foot-care order
            group["record"] = {**group["record"], "icd10": codes}
    return groups


def add_extra_cpt_lines(page, context, cpts, pos=None, dos=None, modifiers=None,
                        diagnosis_count=0, timeout_ms=30000):
    """Add CPTs 2..n as extra billable lines. Step [25].

    `cpts` is the whole list; the first is already on line 1 (index FIRST_LINE),
    so this starts at the second. Picking from the popup writes that line's
    POS/charge/units from the CPT list, so the sheet's POS has to be reapplied
    afterwards exactly as it is on line 1 - otherwise the extra lines carry the
    CPT list's default place of service instead of the one being billed.

    The same applies to the Date(s) of Service and the ICD-10 Pointer: these
    extra lines are only ever added for sheet rows sharing line 1's DOS and
    diagnoses (group_key requires the same dos and icd10_raw for rows to
    merge into one visit), but Office Ally's own "add line" popup leaves the
    new line's DOS blank and does not carry line 1's pointer over - it
    defaults the new line to 1234 regardless of how many diagnoses the visit
    actually has. Left alone that submits a claim line with no DOS and a
    wrong pointer, so both are (re)written here to match line 1 exactly, the
    same way POS is.

    `modifiers` is different: it is NOT line 1's value reapplied, it is that
    row's OWN Hospice Modifier cell (kept parallel to `cpts` by
    group_records) - the second CPT on a visit is often billed with a
    different modifier, or none at all, so each extra line gets its own.
    """
    modifiers = modifiers or []
    added = []
    for offset, cpt in enumerate(cpts[1:], start=1):
        line = FIRST_LINE + offset

        dates_written = fill_line_dates(page, dos, line=line) if dos else {}

        _, actual = select_cpt(page, context, cpt, line=line, timeout_ms=timeout_ms)

        pos_written = actual["pos"]
        pos_source = "from CPT list"
        if pos:
            # set_place_of_service returns (how, value)
            pos_source, pos_written = set_place_of_service(
                page, pos, line=line, timeout_ms=timeout_ms)

        modifier = modifiers[offset] if offset < len(modifiers) else ""
        modifier_written = ""
        if modifier:
            _, modifier_written = set_modifier_a(
                page, modifier, line=line, timeout_ms=timeout_ms)

        pointer_written = fill_diagnosis_pointer(page, diagnosis_count, line=line)

        added.append({
            "line": line + 1,          # the number shown in the Line No column
            "dos_from": dates_written.get("from", ""),
            "dos_to": dates_written.get("to", ""),
            "cpt": actual["cpt"],
            "pos": pos_written,
            "pos_source": pos_source,
            "modifier": modifier_written,
            "charge": actual["charge"],
            "quantity": actual["quantity"],
            "pointer": pointer_written,
        })
    return added


# Billing Options -> Facility -> the "..." button
FACILITY_BROWSE_BTN = "#ctl00_phFolderContent_Button35"


def facility_search_name(value):
    """The name to actually search on.

    The sheet prefixes facilities with a short site code that the Facilities
    list does not carry:
        'empgrn - Fountain Springs Healthcare' -> 'Fountain Springs Healthcare'
    Searching the raw cell returns nothing.
    """
    text = str(value or "").strip()
    if " - " in text:
        text = text.split(" - ", 1)[1].strip()
    return text


def read_facility_rows(popup):
    """Facilities rows. Columns: Select | Name | Address | City | State | Zip | ID | NPI."""
    rows = []
    trs = popup.locator(f"{POPUP_GRID} tr")
    for i in range(trs.count()):
        cells = [c.strip() for c in trs.nth(i).locator("td").all_inner_texts()]
        if len(cells) >= 3 and cells[0].lower() == "select":
            rows.append({
                "index": i,
                "name": cells[1],
                "address": cells[2],
                "city": cells[3] if len(cells) > 3 else "",
                "facility_id": cells[6] if len(cells) > 6 else "",
                "npi": cells[-1],
            })
    return rows


def choose_facility(rows, wanted):
    """Prefer an exact name match; only fall back when there is just one row."""
    if not rows:
        raise RuntimeError(f"no facility matching {wanted!r} in the Facilities list")

    target = wanted.strip().upper()
    exact = [r for r in rows if r["name"].strip().upper() == target]
    if len(exact) == 1:
        return exact[0]
    if len(rows) == 1:
        return rows[0]
    names = ", ".join(r["name"] for r in rows[:5])
    raise RuntimeError(
        f"{len(rows)} facilities match {wanted!r} ({names}) - cannot choose"
    )


def select_facility(page, context, facility_name, timeout_ms=30000, attempts=2):
    """Open the Facilities popup, search by name, and select it. Step [26].

    The popup's "Select" link has occasionally closed the popup out from
    under the click (Playwright reports "Target page, context or browser
    has been closed" mid-click, even though the element was visible and
    stable a moment before) - a transient timing race, not a real failure,
    since retrying the whole popup from scratch reliably works. Retried
    here rather than failing the whole visit; a genuine "no such facility"
    or "form holds a different facility" is a different exception type
    (RuntimeError) and is NOT retried - that is a real data problem, not
    a glitch.
    """
    wanted = facility_search_name(facility_name)
    if not wanted:
        return None, {}

    for attempt in range(attempts):
        try:
            with context.expect_page(timeout=timeout_ms) as popup_info:
                page.click(FACILITY_BROWSE_BTN)
            popup = popup_info.value
            popup.wait_for_load_state("load")
            popup.wait_for_selector(POPUP_INPUT, state="visible", timeout=timeout_ms)

            if selected_label(popup.locator(POPUP_SEARCH_BY)) != "Facility Name":
                popup.select_option(POPUP_SEARCH_BY, label="Facility Name")
            if selected_label(popup.locator(POPUP_CONDITION)) != "Starts With":
                popup.select_option(POPUP_CONDITION, label="Starts With")

            popup.fill(POPUP_INPUT, wanted)
            popup.click(POPUP_SEARCH_BTN)
            popup.wait_for_load_state("load")
            popup.wait_for_timeout(1500)

            rows = read_facility_rows(popup)
            try:
                chosen = choose_facility(rows, wanted)
            except RuntimeError:
                popup.close()
                raise

            popup.locator(f"{POPUP_GRID} tr").nth(chosen["index"]) \
                 .locator("a", has_text="Select").first.click()

            for _ in range(int(timeout_ms / 500)):
                if popup.is_closed():
                    break
                page.wait_for_timeout(500)
            page.wait_for_timeout(1200)

            # The ID shown next to "Facility:" is FacilityProviderID. FacilityID is a
            # different field and is normally blank.
            written = page.evaluate("""() => {
                const out = {};
                ['FacilityProviderID', 'FacilityName', 'FacilityAddress', 'FacilityCity',
                 'FacilityState', 'FacilityZip', 'FacilityNPI'].forEach(k => {
                    const e = document.getElementById('ctl00_phFolderContent_' + k);
                    if (e) out[k] = e.value;
                });
                return out;
            }""")

            got = written.get("FacilityName", "").strip().upper()
            if got and got != chosen["name"].strip().upper():
                raise RuntimeError(
                    f"selected facility {chosen['name']!r} but the form holds "
                    f"{written.get('FacilityName')!r}"
                )
            return chosen, written
        except PlaywrightError as exc:
            if attempt + 1 >= attempts:
                raise
            print(f"           (Facility popup closed unexpectedly mid-click - "
                  f"retrying: {str(exc).splitlines()[0][:100]})")


# Both Update buttons on the visit form (top and bottom) call Advance('').
# btnUpdate is the named one; Button4 is the same handler with a generated id.
UPDATE_BTN = "#ctl00_phFolderContent_btnUpdate"


def form_validation_errors(page):
    """Any visible validation message the form is complaining about."""
    return page.evaluate(
        """() => [...document.querySelectorAll(
                  'span[id*="Valid"], .error, [style*="color:Red"], [style*="color: red"]')]
             .filter(e => e.getBoundingClientRect().height > 0
                          && (e.innerText || '').trim().length > 2)
             .slice(0, 5)
             .map(e => e.innerText.trim().slice(0, 120))"""
    )


def click_update(page, timeout_ms=60000):
    """Click Update, committing the visit. Step [26].

    This is the save. Everything before it only fills fields.

    Update runs Advance(''), which validates client-side first, so a click does
    not always navigate. Poll for whichever happens: we leave EditVisit, or the
    page shows what it is unhappy about.
    """
    button = page.locator(UPDATE_BTN)
    button.wait_for(state="visible", timeout=timeout_ms)

    before = page.url
    button.click()

    waited = 0
    while waited < timeout_ms:
        if page.url != before and "EditVisit" not in page.url:
            page.wait_for_load_state("load")
            page.wait_for_timeout(1500)
            return "saved", page.url

        try:
            errors = form_validation_errors(page)
        except Exception:
            # Execution context destroyed = the page is navigating, which is
            # what a successful save looks like. Keep polling for the URL.
            errors = []
        if errors:
            return "rejected", "; ".join(errors)

        page.wait_for_timeout(500)
        waited += 500

    # Still on EditVisit with nothing to show for it
    return "no response", page.url


def confirm_clear_dialog(page, timeout_ms=8000):
    """Accept the 'Confirm Clear' dialog raised by a diagnosis delete icon.

    It is a jQuery UI dialog in the page, not a browser confirm(), so there is
    no dialog event to handle - the OK button has to be clicked. Until it is,
    the diagnosis is NOT cleared.
    """
    ok = page.locator(".ui-dialog:visible button", has_text="OK").first
    waited = 0
    while waited < timeout_ms:
        if ok.count() and ok.is_visible():
            ok.click()
            page.wait_for_timeout(700)
            return True
        page.wait_for_timeout(250)
        waited += 250
    return False


def clear_extra_diagnoses(page, keep, timeout_ms=15000):
    """Remove template diagnoses past the ones the sheet supplied.

    A new visit is pre-filled from the patient's Template - typically five or
    six diagnoses. Writing only the sheet's codes overwrites the first N and
    leaves the rest attached to the claim, so anything past N is cleared.

    Deleting COMPACTS the list: clearing slot 4 shifts slot 5 up into it. So
    this deletes the same slot repeatedly until it comes back empty, rather
    than walking down the slots.

    Clearing goes through the row's trash icon and its confirm dialog. Emptying
    the inputs with JavaScript instead leaves the widget's own state behind and
    the page then drops the ICD-10 Pointer.

    Returns the codes removed.
    """
    slot = keep + 1                          # dc_10_/dd_10_ are 1-based
    code_box = page.locator(
        f"#ctl00_phFolderContent_ucDiagnosisCodes_dc_10_{slot}")
    if not code_box.count():
        return []

    removed = []
    # At most the whole grid, so a stuck row cannot spin forever
    for _ in range(MAX_DIAGNOSIS_BOXES):
        existing = code_box.input_value().strip()
        if not existing:
            break

        trash = page.locator(
            f"#ctl00_phFolderContent_ucDiagnosisCodes_dd_10_{slot}"
        ).locator("xpath=ancestor::tr[1]").locator(
            "span.fa-clear, span.fa-trash-o").first
        if not (trash.count() and trash.is_visible()):
            break

        close_suggestions(page)
        trash.click()
        if not confirm_clear_dialog(page):
            raise RuntimeError(
                f"diagnosis {slot}: delete confirm dialog never appeared")

        if code_box.input_value().strip() == existing:
            raise RuntimeError(
                f"diagnosis {slot}: still holds {existing!r} after confirming clear")

        removed.append(existing)

    return removed
