"""Check that a finished run's result sheet really matches what the database recorded.

A run is only reported as completed after this passes. The engine writes a Status
column into the sheet after every visit; here that column is read back and compared
with the rows the runner reported.
"""

import openpyxl


def verify_output(path, status_column: str, db_rows, finished_done: int | None) -> list[str]:
    """Returns a list of problems; an empty list means the output is verified.

    db_rows: iterable of (row_no, status) the runner reported for this run.
    finished_done: how many rows the engine says are Done in total (includes rows carried over).
    """
    problems: list[str] = []
    try:
        workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    except Exception as exc:
        return [f"result sheet cannot be opened ({type(exc).__name__})"]
    try:
        sheet = workbook[workbook.sheetnames[0]]
        rows = sheet.iter_rows(values_only=True)
        header = next(rows, None)
        if header is None:
            return ["result sheet is empty"]
        wanted = str(status_column).strip().lower()
        names = [str(v).strip().lower() if v is not None else "" for v in header]
        if wanted not in names:
            return [f"result sheet has no '{status_column}' column"]
        col = names.index(wanted)
        in_file = {}
        for row_no, values in enumerate(rows, start=2):
            if col < len(values) and values[col] not in (None, ""):
                in_file[row_no] = str(values[col]).strip()
    finally:
        workbook.close()

    for row_no, status in db_rows:
        got = in_file.get(row_no)
        if got != str(status).strip():
            problems.append(f"row {row_no}: run says '{status}', sheet says '{got or 'empty'}'")
            if len(problems) >= 5:
                problems.append("...")
                break
    if finished_done is not None:
        file_done = sum(1 for v in in_file.values() if v.startswith("Done"))
        if file_done != finished_done:
            problems.append(f"{file_done} rows are Done in the sheet but the engine reported {finished_done}")
    return problems
