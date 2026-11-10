# ACO Automation

Reads the ACO billing sheet and, for each patient, opens Practice Mate, finds
the patient, fills a new visit form from the sheet, and saves it.

This is the same Practice Mate flow as `../Create Visit`, adapted for this
account's sheet columns, with two differences called out below: a Modifier A
field is filled from `Hospice Modifier`, and Entity Type is left untouched.

With `--save` the visit is committed and the run carries on to the next
patient. Without it, one visit is filled and left on screen to be checked.

A patient who already has a visit on the sheet's DOS is skipped — the row has
been billed before, so entering it again would duplicate it.

Every run resumes automatically: a sheet row `output/data_with_status.xlsx`
already marked Done from a previous run is skipped without being searched
again — so closing the browser partway through a long sheet and running again
picks up where it left off instead of starting over from row 1. Its Visit ID /
iNS ID from that previous run are carried over too. Pass `--no-resume` to
ignore that file and process every row again.

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

This account logs in separately from the other automations (see `.env` in
this folder) - its `auth_state.json` session is kept local to this folder on
purpose, so it never reuses a session saved by another automation's login.

## Flow

```
[1]  open cms.officeally.com
      |
[2]  Login dropdown -> Practice Mate
      |
[3]  Auth0 login (username + pass from ./.env)
      |
[4]  save session -> ./auth_state.json   (old one deleted first - every
      |                                         run logs in fresh; shared only by
      |                                         this run's windows)
      |
[5]  Manage Patients
      |
[6]  read data.xlsx
      |    rows identical except CPT = ONE visit, many lines
      |
     ================ for each visit ================
      |
[7]  back to Manage Patients        (from patient 2 onward)
      |
[8]  search Patient Last Name  (from "Patient", format "Last, First [Middle]")
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
[12] Visit History tab            -> read visits into output/visits.csv
      |
[13] already billed?  Date Visited == DOS AND Provider Name matches
      |    match -> open that existing visit, capture its Visit ID and
      |             (primary) Subscriber ID for the sheet, then SKIP -
      |             nothing entered, next patient
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
      |    (Billing Provider column)
      |
[16] Billing Info tab
      |    line 1 DOS From + To   = Treatment Date
      |    line 1 CPT             = CPT        ("..." popup, search by Code)
      |    line 1 POS             = Place of Service (after CPT: CPT writes its own POS)
      |                             exactly as on the sheet, never remapped; a blank or
      |                             unreadable cell -> visit NOT created (the CPT
      |                             library's default POS is never billed)
      |    line 1 Modifier A      = Hospice Modifier (plain text box, e.g. "GW")
      |    Diagnosis A(1)..L(12)  = ICD split on commas, ALL codes (max 12), codes
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
      |
[17] Billing Options tab
      |    Entity Type            = left as-is, NOT changed
      |    Facility               = Ivy Hill column ("..." popup)
      |
[18] Update
      |    --save     -> visit saved, on to the next patient
      |    no --save  -> STOP, form left on screen, nothing saved
     ================================================
      |
[19] write output/results.csv and output/visits.csv
      |
[20] output/data_with_status.xlsx
           a copy of the input sheet with a Status column added (or reused,
           if the sheet already has one). NOT written once at the end -
           seeded Not Created for every row before [7], then updated and
           saved to disk after EACH visit's [18], so it can be watched live
           while the run is still going: Done for every row whose visit was
           saved this run or already existed for that DOS, Not Created for
           everything else - not found, an error, filled but not saved, or
           never reached because the run stopped early.
```

## Sheet columns used

| Column | Goes to |
|---|---|
| `Patient` | "Last, First [Middle]" — surname before the comma is searched, first name after it verifies |
| `Birthdate` | confirms identity, and the fallback search |
| `Billing Provider` | Visit Info provider |
| `Treatment Date` | line 1 Date(s) of Service, From and To |
| `CPT` | line 1 CPT |
| `Place of Service` | Place of Service on every line, used exactly as written (blank/unreadable → row Not Created) |
| `Hospice Modifier` | line 1 Modifier A |
| `ICD` | Diagnosis A(1)..L(12): every code on the sheet is entered (up to the form's 12 boxes), order is: the sheet's first code exactly, then HCC codes (in `HCC/HCC.csv`, matched without dots), then remaining codes starting with Z, then the rest in sheet order. The line's ICD-10 Pointer names the first four (`1234`) |
| `Ivy Hill` | Billing Options facility |

`Patient`, `Birthdate` and `Billing Provider` must be present or the run
stops. Not automated: Units, Modifier B/C/D (the Charge itself is never typed -
it comes from the User CPT list, see Line Charges below). `Note id`, `Signatures`
and `Patient Flags` are on the sheet but not used. `Visit ID` and `iNS ID`
are not read from the sheet, but ARE written back to it (in
`output/data_with_status.xlsx`) - for a visit created and saved this run,
and for a visit that already existed on that DOS.

**Line Charges.** After every billable line is filled, the charge Office Ally
shows for each line (Line Charge column) is read and written to the sheet's
`Line Charges` column (a sheet that already has `Line Charge` / `Charge` is
reused instead of adding a column). One value per sheet row that has a CPT, in
order, so a visit with two CPT rows gets two charges. Written only for a visit
saved this run (`--save`); a failed read leaves the cell empty and does not
fail the visit.

**Several CPT rows, one visit.** Rows with the same patient, DOB, DOS, provider
and POS become one visit with one billable line per CPT row. Each extra line
gets the visit's DOS, POS and diagnosis pointer, plus its OWN row's Hospice
Modifier in Modifier A.

## Run

From inside this folder:

```bash
# FULL SHEET - fill and save every visit
python3 main.py --create-visit --save

# fill ONE visit and hold it on screen to check, saving nothing
python3 main.py --create-visit

# look up patients only, no visit form
python3 main.py
```

Testing on a subset:

```bash
python3 main.py --create-visit --save --limit 5
python3 main.py --create-visit --save --only "Cruz"
python3 main.py --no-click         # search only, never open a chart
python3 main.py --excel other.xlsx # use a different sheet
python3 main.py --no-wait          # do not wait for Enter at the end
python3 main.py --allow-duplicate  # create even if the DOS already has a visit
python3 main.py --no-resume        # ignore output/data_with_status.xlsx, process every row again
python3 main.py --retries 2        # retry rows still Not Created up to 2 more times (default: 1)
python3 main.py --retries 0        # disable the automatic retry pass
```

The browser stays open at the end until Enter is pressed, unless `--no-wait`
is passed or there is no terminal attached.

With `--create-visit` but no `--save`, Practice Mate still writes a visit
record once a billable line carries a charge — so an unsaved run is not
read-only. Only a bare `main.py` run is.
