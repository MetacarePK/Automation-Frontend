# Create Visit

Reads a billing sheet and, for each patient, opens Practice Mate, finds the
patient, fills a new visit form from the sheet, and saves it.

With `--save` the visit is committed and the run carries on to the next
patient. Without it, one visit is filled and left on screen to be checked.

A patient who already has a visit on the sheet's DOS is skipped — the row has
been billed before, so entering it again would duplicate it.

Every run resumes automatically: a sheet row already marked Done in its own
`output/<sheet>_with_status.xlsx` from a previous run is skipped without being searched
again — so closing the browser partway through a long sheet and running again
picks up where it left off instead of starting over from row 1. Pass
`--no-resume` to ignore that file and process every row again.

A row can also come out of a run marked Not Created for a reason that was
never really about the patient — a slow popup, a dropped connection, Office
Ally briefly not responding. Rather than requiring a second full run by hand
to pick those up, once the sheet has been gone through once, any row still
Not Created is automatically retried, up to `--retries` times (default 1)
before the run ends — same output/status file, so a row that clears on
retry just shows Done, same as if it had worked the first time. `--retries 0`
turns this off; `--retries N` tries more times. Doesn't apply to a
`--create-visit` run without `--save`, which deliberately stops after one
visit to be checked by eye.

A retry pass always runs one window at a time, even after a `--workers N`
run — only the first pass through the sheet is ever parallel. The leftover
batch is usually small, and retrying it with the same worker count would put
the same load back on Office Ally that may have caused the failures.

There are two ways to run this: the command line (below), or a local web
page - see [Web UI](#web-ui) near the bottom. Both drive the exact same
code; the web page is just a friendlier front end for someone who doesn't
want to touch the terminal or a `data/` folder by hand.

## Flow

```
[1]  open cms.officeally.com
      |
[2]  Login dropdown -> Practice Mate
      |
[3]  Auth0 login (username + pass from ../../.env)
      |
[4]  save session -> ../../auth_state.json   (later runs skip login)
      |
[5]  Manage Patients
      |
[6]  read data/Black.xlsx
      |    rows identical except CPT = ONE visit, many lines
      |
     ================ for each visit ================
      |
[7]  back to Manage Patients        (from patient 2 onward)
      |
[8]  search Patient Last Name  (last word of Patient Name)
      |
[9]  confirm with DOB from the results grid
      |    DOB matches -> found
      |    DOB differs -> [10]
      |    no rows     -> [10]
      |
[10] fallback: search by Date of Birth
      |    found -> ok
      |    none  -> NO, next patient
      |
[11] open the patient chart
      |    several matches -> pick by DOB, then first name
      |
[12] Visit History tab            -> read visits into output/<sheet>_visits.csv
      |
[13] already billed?  Date Visited == DOS AND Provider Name matches
      |    match -> SKIP, nothing entered, next patient
      |    none  -> carry on
      |    (every row is checked, not just the top one: the grid is
      |     newest-first, so an older visit on the DOS can sit below
      |     a newer one. A DOS match alone is not enough - the same
      |     patient can be seen by two different doctors on the same
      |     day, and that is two billable visits, not one already
      |     billed, so the provider on the row has to match too)
      |
[14] Template tab -> Create New Visit
      |
[15] Visit Info tab
      |    provider: keep if it already matches the sheet,
      |    otherwise "..." popup -> search last name
      |                          -> if >1 row, match first name
      |
[16] Billing Info tab
      |    line 1 DOS From + To   = DOS
      |    line 1 CPT             = CPT      ("..." popup, search by Code)
      |    line 1 POS             = POS      (after CPT: CPT writes its own POS)
      |    line 1 Modifier A      = Modifier (plain text box, e.g. "GW")
      |    Diagnosis A(1)..L(12)  = ICD10 split on commas, ALL codes (max 12), codes
      |                             order: 1st sheet code, then HCC (HCC/HCC.csv),
      |                             then Z codes, then the rest;
      |                             type code -> dropdown -> click suggestion
      |    leftover template dx   -> delete icon -> OK on "Confirm Clear"
      |                             (deleting compacts the list: same slot
      |                              is deleted again until it comes back empty)
      |    ICD-10 Pointer         = 1 / 12 / 123 / 1234 (a line points at 4 max)
      |    extra CPT rows         -> line 2.. DOS From/To + CPT + POS +
      |                              ICD-10 Pointer, all reapplied to match
      |                              line 1 (Office Ally's own "add line"
      |                              popup does not carry any of those over)
      |                              + that row's OWN Modifier - not line 1's,
      |                              since a second CPT can carry a different
      |                              modifier (or none)
      |
[17] Billing Options tab
      |    Entity Type            = Organization
      |    Facility               = Facility Name ("..." popup)
      |
[18] Update
      |    --save     -> visit saved, on to the next patient
      |    no --save  -> STOP, form left on screen, nothing saved
     ================================================
      |
[19] output/<sheet>_with_status.xlsx is updated after EVERY visit, not just
     once at the end - a copy of the input sheet with Status / Visit ID /
     Primary Beneficiary ID columns added (or reused, if the sheet already
     has them). `<sheet>` is the input file's own name - data/Black.xlsx
     writes to output/Black_with_status.xlsx, a different upload writes to
     its own file, so two different sheets never share one resume/status
     file and can't make each other skip rows that were never really done:
           Status                  Done for every row whose visit was saved
                                    this run or already existed for that
                                    DOS, Not Created for everything else -
                                    not found, an error, filled but not
                                    saved, or never reached because the run
                                    stopped early.
           Visit ID                the new (or already-existing) visit's ID
           Primary Beneficiary ID  the primary insurance Subscriber ID read
                                    back from that visit on Practice Mate -
                                    overwrites whatever was typed into the
                                    sheet, and a mismatch against the
                                    original sheet value is flagged in
                                    output/<sheet>_results.csv and printed
                                    to the console.
     Writing after every visit (not just at the end) means closing the
     browser partway through a long sheet loses nothing already created -
     the file on disk always reflects everything done so far.
      |
[20] write output/<sheet>_results.csv and output/<sheet>_visits.csv
```

## Sheet columns used

| Column | Goes to |
|---|---|
| `Patient Name` | last word = surname searched, first word verifies |
| `Patient DOB` | confirms identity, and the fallback search |
| `Provider Name` | Visit Info provider |
| `DOS` | line 1 Date(s) of Service, From and To |
| `CPT` | line 1 CPT |
| `POS` | line 1 Place of Service |
| `Modifier` | line 1 Modifier A |
| `ICD10` | Diagnosis A(1)..L(12): every code on the sheet is entered (up to the form's 12 boxes), order is: the sheet's first code exactly, then HCC codes (in `HCC/HCC.csv`, matched without dots), then remaining codes starting with Z, then the rest in sheet order. The line's ICD-10 Pointer names the first four (`1234`) |
| `Facility Name` | Billing Options facility |
| `Primary Beneficiary ID` | compared against Practice Mate's Subscriber ID for the visit, then overwritten with it |

`Patient Name`, `Patient DOB` and `Provider Name` must be present or the run
stops. Not automated: Units, Charge, Modifier B/C/D.

`Visit ID` and `Primary Beneficiary ID` are also written back into the
output sheet (see step [19] above) - `Visit ID` is filled in, and
`Primary Beneficiary ID` is verified against Practice Mate and overwritten
with whatever is actually on file there.

## Run

From inside this folder:

```bash
# FULL SHEET - fill and save every visit
.\venv\Scripts\activate
python main.py --create-visit --save

# fill ONE visit and hold it on screen to check, saving nothing
../venv/bin/python main.py --create-visit

# look up patients only, no visit form
../venv/bin/python main.py
```

Testing on a subset:

```bash
../venv/bin/python main.py --create-visit --save --limit 5
../venv/bin/python main.py --create-visit --save --only "Peterson"
../venv/bin/python main.py --no-click         # search only, never open a chart
../venv/bin/python main.py --fresh            # ignore saved session, log in again
../venv/bin/python main.py --excel other.xlsx # use a different sheet
../venv/bin/python main.py --no-wait          # do not wait for Enter at the end
../venv/bin/python main.py --allow-duplicate  # create even if the DOS already has a visit
../venv/bin/python main.py --no-resume        # ignore output/<sheet>_with_status.xlsx, process every row again
../venv/bin/python main.py --retries 2        # retry rows still Not Created up to 2 more times (default: 1)
../venv/bin/python main.py --retries 0        # disable the automatic retry pass

../venv/bin/python tests/test_dates.py             # 59 offline checks, no browser
../venv/bin/python tests/test_duplicate_guard.py   # 15 offline checks, no browser
../venv/bin/python tests/test_status_column.py     # status/Visit ID/Beneficiary ID write-back checks
../venv/bin/python tests/test_parallel_guard.py    # --workers safety-guard check
```

The browser stays open at the end until Enter is pressed, unless `--no-wait`
is passed or there is no terminal attached.

With `--create-visit` but no `--save`, Practice Mate still writes a visit
record once a billable line carries a charge — so an unsaved run is not
read-only. Only a bare `main.py` run is.

## Speed: --workers N

By default this runs one visit at a time in one browser window. `--workers N`
instead opens N **separate browser windows**, all sharing the one saved login,
each pulling the next patient off a shared queue - so N claims get created at
once instead of one after another.

```bash
# a saved session must already exist first (run this once if it doesn't)
../venv/bin/python main.py

# then the full sheet, 3 windows at once
../venv/bin/python main.py --create-visit --save --workers 3

# SAFE FIRST TEST: 3 windows, each fills ONE visit and holds it on screen -
# nothing is saved, so you can check all 3 by eye before trusting --save
../venv/bin/python main.py --create-visit --workers 3
```

Each worker gets its **own** browser, not just its own tab - this flow opens
popup windows while filling a visit (Provider search, CPT search, Facility
search), and Playwright only isolates popups correctly between fully separate
browser contexts. Sharing one context across workers (the way tab-based
parallelism would) risks one worker's popup being caught by another worker's
code - which would mean one patient's CPT or provider ending up on a
different patient's claim. Separate browsers make that impossible.

Every window waits for its own Enter press before closing (`--no-wait` skips
this), so with several windows finishing at different times, press Enter once
per window as each one is ready to close - not once for the whole run.

**Start at 3.** Office Ally has throttled this account's IP before for
traffic that looked automated; a claim-creation visit makes more requests per
patient (three popups plus the form fill) than a read-only lookup does, so
don't assume the same worker count that's safe for lookups is safe here too.
`--workers` requires a saved session to already exist (`auth_state.json`) -
run the plain sequential command once first if there isn't one yet.

## Web UI

A local page for running this without touching the terminal or `data/`
folder directly - upload a sheet, map its columns, click Run, watch progress
live. It drives the exact same `run_automation()` the CLI does; nothing about
how Practice Mate is used is different.

```bash
pip install -r requirements.txt

# double-click, or from a terminal:
./start.sh      # macOS / Linux
start.bat       # Windows
```

Either one starts the server and opens `http://127.0.0.1:8000` in your
browser automatically once it's ready. Both are thin wrappers around
`start.py` (`python start.py` works directly too, on any OS) - there's only
one copy of the actual start-up logic to keep in sync.

Without the launcher: `python -m uvicorn web.app:app`, then open
`http://127.0.0.1:8000` yourself.

1. **Upload** - pick your `.xlsx`. It gets saved into `data/` for you; there
   is no manual copy-the-file-into-the-folder step.
2. **Map the columns** - for each field (Patient Name, DOS, CPT, ...), pick
   which of your sheet's own columns it comes from. A sheet doesn't have to
   use this tool's exact header names. The mapping is remembered (in
   `web/mappings.json`) by the sheet's set of column headers, so uploading
   another sheet with the same layout later pre-fills it.
3. **Run** - "Create claims for real" is **on** by default: clicking Start
   run creates and saves real claims right away. Uncheck it first for a dry
   run (each visit gets filled but not saved) to check the mapping and
   matching against the results table before trusting a new sheet or a new
   mapping with real claims. Workers defaults to 4 separate browser windows
   (see Speed above for what that means and why not to push it much
   higher). Limit / Only / Allow-duplicate are under Advanced, same as the
   CLI flags.
4. **Progress** - updates live via WebSocket as each visit finishes: row,
   patient, DOS, Status, Visit ID, Beneficiary ID, and any error or
   duplicate/mismatch note. Shows the 10 most recently updated rows by
   default; "Show all rows" reveals the rest.

Notes:

- Single user, one run at a time, on this same machine - this is a local
  tool, not a hosted multi-user site, because it needs a real Edge browser
  logged into Office Ally right here.
- With "Create claims for real" unchecked, the browser windows close as soon
  as each one finishes its share of the queue rather than staying open to be
  eyeballed (there's no terminal for them to wait on a keypress from) - the
  progress table's detail is the review step in this mode, not the browser
  window itself. To actually watch a filled form on screen before it's
  saved, use the CLI's `--create-visit` (no `--save`).
- There's no cancel/stop button yet - closing the actual Edge window(s)
  works the same way it does from the CLI, and doesn't corrupt anything
  since status is written after every visit, not just at the end.
