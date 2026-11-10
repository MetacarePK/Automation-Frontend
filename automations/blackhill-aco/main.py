"""Log in to Office Ally Practice Mate and work through the billing sheet.

Every run starts with a fresh login: the old auth_state.json is deleted first,
then a real login writes a new one (cookies + localStorage). That new session
is only shared within the same run - by the --workers windows and the retry
window - and is thrown away again at the start of the next run.

Usage:
    python main.py            # delete the old session, log in fresh, run
"""

import csv
import sys
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright

from patients import (
    MANAGE_PATIENTS_TAB,
    visit_on_dos,
    group_records,
    add_extra_cpt_lines,
    read_line_charges,
    back_to_patient_list,
    choose_row,
    rows_matching_dob,
    first_name_of,
    last_name_of,
    open_patient,
    open_visit_history,
    open_template_tab,
    create_new_visit,
    ensure_visit_info_tab,
    ensure_provider,
    open_billing_info_tab,
    open_billing_options_tab,
    click_update,
    select_facility,
    fill_line_dates,
    select_cpt,
    set_place_of_service,
    set_modifier_a,
    fill_diagnosis_codes,
    fill_diagnosis_pointer,
    clear_extra_diagnoses,
    read_patients,
    split_icd10,
    MAX_DIAGNOSIS_CODES,
    MAX_DIAGNOSIS_BOXES,
    is_hcc,
    search_dob,
    search_last_name,
    open_status_workbook,
    write_status_rows,
    save_status_workbook,
    read_previous_status,
    read_previous_captured,
    open_visit_by_id,
    read_subscriber_id,
    resolve_column_map,
)

HOME_URL = "https://cms.officeally.com/"
PM_URL = "https://pm.officeally.com/pm/"

# This automation logs into a DIFFERENT Office Ally account than the other
# automations in this project (see .env), so its .env and its browser
# session must stay local to this folder - never fall back to a shared
# auth_state.json one level up, which would silently reuse the wrong login.
BASE_DIR = Path(__file__).resolve().parent

ENV_PATH = BASE_DIR / ".env"
STATE_PATH = BASE_DIR / "auth_state.json"
EXCEL_PATH = BASE_DIR / "data.xlsx"
# Web-uploaded sheets land here, NOT the project root - a sheet named the
# same as a real file (main.py, .env, auth_state.json) must never be able to
# overwrite it. This is separate from EXCEL_PATH above, which is the CLI's
# existing default and stays exactly where it already was.
DATA_DIR = BASE_DIR / "data"
OUTPUT_DIR = BASE_DIR / "output"


# Output paths are derived from the SHEET being run, not fixed - two
# different uploaded sheets must never share one status file. Sharing one
# would mean a second sheet's resume check reads the first sheet's leftover
# Status column and could wrongly skip a row that just happens to share a
# row number with an unrelated sheet.
def status_xlsx_path_for(excel_path):
    return OUTPUT_DIR / f"{Path(excel_path).stem}_with_status.xlsx"


def results_path_for(excel_path):
    return OUTPUT_DIR / f"{Path(excel_path).stem}_results.csv"


def visits_path_for(excel_path):
    return OUTPUT_DIR / f"{Path(excel_path).stem}_visits.csv"

# The Login item in the top nav that opens the dropdown (Webflow dropdown toggle)
LOGIN_TOGGLE = "div.main-menu-dropdown-item.login"
# The dropdown panel that holds the three product login links
LOGIN_MENU = "div.menu-login-wrapper"
# Practice Mate carries a stable id, so prefer it over matching on text
PRACTICE_MATE = "a#nav_practice"



def load_credentials():
    """Read the .env file. Accepts both `key=value` and `key: value` lines."""
    if not ENV_PATH.exists():
        sys.exit(f"Missing {ENV_PATH}. Add your username and password to it.")

    values = {}
    for line in ENV_PATH.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        for sep in ("=", ":"):
            if sep in line:
                key, value = line.split(sep, 1)
                values[key.strip().lower()] = value.strip().strip("'\"")
                break

    username = values.get("username") or values.get("user") or values.get("oa_username")
    password = values.get("pass") or values.get("password") or values.get("oa_password")
    if not username or not password:
        sys.exit("Could not find a username and password in .env")
    return username, password


def on_app(url):
    """True once we're on the Practice Mate app itself, past the auth redirects."""
    return "/pm/" in url and "auth0bridge" not in url


def open_login_dropdown(page):
    """Click the Login nav item and wait for its dropdown to expand."""
    toggle = page.locator(LOGIN_TOGGLE).first
    toggle.wait_for(state="visible", timeout=30000)
    toggle.click()

    # Wait for the Practice Mate link to become visible, not merely present -
    # the links exist in the DOM while the dropdown is still collapsed.
    page.locator(PRACTICE_MATE).first.wait_for(state="visible", timeout=15000)


def show_dropdown_options(page):
    """Print every product login link revealed by the dropdown."""
    links = page.locator(f"{LOGIN_MENU} a")
    print("\nLogin dropdown options:")
    for i in range(links.count()):
        link = links.nth(i)
        label = " ".join(link.inner_text().split())
        print(f"  {i + 1}. {label:<22} -> {link.get_attribute('href')}")
    print()


def submit_login(login_page, username, password):
    """Fill in the Auth0 universal-login form and submit it."""
    login_page.wait_for_selector("input#username", timeout=30000)
    login_page.fill("input#username", username)
    login_page.fill("input#password", password)

    print(f"Signing in as {username}...")
    with login_page.expect_navigation(wait_until="domcontentloaded", timeout=60000):
        login_page.get_by_role("button", name="Continue").click()


def wait_for_login_result(page, timeout_ms=60000):
    """Poll until login settles: either the app loads or an error appears.

    Auth0 bounces through several redirects (auth.officeally.com -> auth0bridge
    -> the app), so a single check right after submit races the redirect chain
    and misreports. Poll until one side actually wins.
    """
    deadline = timeout_ms
    while deadline > 0:
        if on_app(page.url):
            page.wait_for_load_state("load")
            return True

        if "auth.officeally.com" in page.url:
            error = page.locator("[role='alert'], .ulp-input-error-message").first
            if error.count() and error.is_visible():
                print("Login failed -", error.inner_text().strip())
                return False

        page.wait_for_timeout(1000)
        deadline -= 1000

    print("Timed out. Last URL:", page.url.split("?")[0])
    return False


def open_manage_patients(page):
    """Click the Manage Patients tab in the Practice Mate top nav."""
    tab = page.locator(MANAGE_PATIENTS_TAB)
    tab.wait_for(state="visible", timeout=60000)

    print("\nClicking 'Manage Patients'...")
    with page.expect_navigation(wait_until="domcontentloaded", timeout=60000):
        tab.click()

    page.wait_for_load_state("load")
    print("  URL:  ", page.url.split("?")[0])
    print("  Title:", page.title())


def log_in(browser):
    """Run the full CMS -> dropdown -> Practice Mate -> Auth0 login flow."""
    username, password = load_credentials()
    context = browser.new_context(no_viewport=True)
    page = context.new_page()

    page.goto(HOME_URL, wait_until="domcontentloaded")
    print("Opened:", page.url)

    open_login_dropdown(page)
    show_dropdown_options(page)

    print("Clicking 'Practice Mate'...")
    # The link is target="_blank", so the click opens a NEW TAB. Capture it
    # with expect_page() - clicking without this leaves `page` on the old tab.
    with context.expect_page(timeout=30000) as popup_info:
        page.locator(PRACTICE_MATE).first.click()

    login_page = popup_info.value
    login_page.wait_for_load_state("load")
    login_page.bring_to_front()

    submit_login(login_page, username, password)
    if not wait_for_login_result(login_page):
        return context, login_page, False

    # Persist cookies + localStorage so the next run can skip all of the above
    context.storage_state(path=STATE_PATH)
    print(f"Session saved to {STATE_PATH.name}")
    return context, login_page, True


def process_group(page, context, i, total, group, name_cache, skip_click,
                  make_visit, save_visit, allow_duplicate, stop_after_billing):
    """Look up and (optionally) create the one visit `group` represents.

    Everything for a single group/visit lives here so it can be driven either
    by the sequential loop in run_lookups (one shared page/name_cache for the
    whole sheet) or by a parallel worker (its own page/name_cache, pulling
    groups off a shared queue) - the Practice Mate interaction is identical
    either way, only how groups are handed out differs.

    Returns (result, group_visits, stop_run):
      result       one row for results.csv / the sheet's Status column
      group_visits Visit History rows collected while this group was open,
                   for output/visits.csv
      stop_run     True when a filled-but-unsaved form should be left on
                   screen and this page/worker should stop taking more groups
    """
    group_visits = []
    record = group["record"]
    group_cpts = group["cpts"]
    group_modifiers = group.get("modifiers", [])
    full_name = record["name"]
    dob = record["dob"]
    bad_dob = record.get("dob_unparsed", "")
    bad_pos = record.get("pos_unparsed", "")
    last = last_name_of(full_name)
    first = first_name_of(full_name)
    key = last.upper()

    # --- stage 1: last name ---
    error = ""
    unverified = ""
    seen_names = ""
    try:
        # Whatever the last record left on screen, get back to the list
        # before searching. Doing it here rather than after a save means a
        # slow navigation cannot throw away work that already succeeded.
        # ManagePatients.aspx, not just "ManagePatients": the patient
        # chart lives at /pm/ManagePatients/EditPatient.aspx, so matching
        # the folder name made this skip the navigation after a save.
        if make_visit and "ManagePatients.aspx" not in page.url:
            back_to_patient_list(page)
            name_cache.clear()
        if key in name_cache:
            rows = name_cache[key]
        else:
            rows = search_last_name(page, last)
            name_cache[key] = rows

        # A surname hit is only the right person if the DOB agrees - the
        # same last name can belong to several patients.
        confirmed = rows_matching_dob(rows, dob)
        seen_names = "; ".join(
            f"{r.get('FirstName','').strip()} {r.get('LastName','').strip()}"
            f" ({r.get('DateOfBirthSort','').strip()})" for r in rows[:4]
        )

        if confirmed:
            rows, found, method = confirmed, True, "last name + DOB"
        elif rows and bad_dob:
            # The sheet HAS a DOB, we just could not read it. Accepting a
            # surname-only match here would bill whoever sorted first.
            rows, found, method = [], False, ""
            error = f"unreadable Patient DOB {bad_dob!r} - cannot verify"
        elif rows and not dob:
            # No DOB in the sheet to verify against - surname is all we have
            found, method = True, "last name only (no DOB to verify)"
        else:
            # Either nothing matched the name, or the names that matched
            # belong to other people. Try the DOB search directly.
            if rows:
                unverified = seen_names
            dob_rows = search_dob(page, dob) if dob else []
            # A DOB match alone is not enough - two unrelated patients can
            # share a birthdate. Require the first name OR the last name to
            # also agree, otherwise this is the wrong person even though the
            # DOB lined up.
            rows = [
                r for r in dob_rows
                if str(r.get("FirstName", "")).strip().upper() == first.upper()
                or str(r.get("LastName", "")).strip().upper() == last.upper()
            ]
            found = bool(rows)
            method = "DOB" if found else ""
    except Exception as exc:
        # Better to flag a row than to record a verdict we cannot trust
        rows, found, method, error = [], False, "", str(exc)[:120]

    first_match = any(
        str(r.get("FirstName", "")).strip().upper() == first.upper() for r in rows
    )
    matches = "; ".join(
        f"{r.get('FirstName','').strip()} {r.get('LastName','').strip()}"
        f" ({r.get('DateOfBirthSort','').strip()})" for r in rows[:4]
    )

    # --- open the matched patient's chart, then its Visit History ---
    opened_id = opened_url = visit_url = provider_on_form = ""
    provider_source = ""
    # Two different things, kept apart on purpose. Conflating them is why
    # a --save run used to quit after the first patient.
    skip_nav_back = False   # leave the tab where it is; next record re-navigates
    stop_run = False        # end the whole run here
    line_dates = {}
    line_cpt = pos_from_cpt = line_pos = pos_source = ""
    line_modifier = modifier_source = ""
    captured_visit_id = captured_subscriber_id = ""
    capture_warning = ""
    # Reset every per-visit field here, not inside the try: a failure
    # part-way through must still be able to report what did get written.
    pointer = facility_set = saved = ""
    dropped_dx = []
    skipped_dx = []
    on_billing_options = False
    extra_lines = []
    line_charges = []
    diagnoses = []
    visits = []
    duplicate_of = ""   # existing visit id on this DOS, if there is one
    if found and not skip_click:
        chosen = choose_row(rows, first, dob)
        opened_id = str(chosen.get("PatientID", "")).strip()
        try:
            opened_url = open_patient(page, opened_id)
            visits = open_visit_history(page)
            for v in visits:
                group_visits.append({
                    "patient_name": full_name,
                    "patient_id": opened_id,
                    "visit_id": v.get("Visit ID", ""),
                    "date_visited": v.get("DateVisited", ""),
                    "provider": v.get("ProviderName", ""),
                    "charges": v.get("Charges", ""),
                    "balance": v.get("Balances", ""),
                    "status": v.get("Status", ""),
                    "claim_no": v.get("ClaimNo", ""),
                })

            # Already billed? A visit already on this DOS means this
            # sheet row has been entered before, so creating another one
            # would duplicate it. Checked before any form is opened, so a
            # skip costs nothing and writes nothing.
            existing = (None if allow_duplicate
                        else visit_on_dos(visits, record.get("dos", ""),
                                          record.get("provider", "")))
            if existing:
                duplicate_of = str(existing.get("Visit ID", "")).strip()

                # A claim already exists for this DOS - capture ITS Visit
                # ID / Subscriber ID for the sheet too, the same way a
                # freshly-saved visit's are captured below. Without this,
                # a skipped row wrote nothing back and the sheet's Visit
                # ID / iNS ID only ever came from visits created THIS run.
                if duplicate_of:
                    try:
                        open_visit_by_id(page, opened_id, duplicate_of)
                        captured_visit_id = duplicate_of
                        captured_subscriber_id = read_subscriber_id(page)
                    except Exception as exc:
                        capture_warning = str(exc).splitlines()[0][:110]
                        print(f"           (could not capture Visit ID / "
                              f"Subscriber ID for existing visit: {capture_warning})")
                        capture_warning = (f"could not read Visit ID / "
                                           f"Subscriber ID: {capture_warning}")

            # Template tab -> Create New Visit -> Visit Info (form only,
            # nothing is saved here)
            if make_visit and not duplicate_of:
                # Only the sheet's own Place of Service is ever billed. With
                # none to use, stop BEFORE the form is opened - picking a CPT
                # would otherwise leave the CPT library's default POS on the
                # line, and a line with a charge can already be written to
                # Practice Mate even without Update.
                if record.get("cpt") and not record.get("pos"):
                    raise RuntimeError(
                        f"Place of Service {bad_pos!r} on the sheet is not a "
                        f"POS code - visit not created" if bad_pos else
                        "no Place of Service on the sheet - visit not created")
                open_template_tab(page)
                visit_url = create_new_visit(page)
                ensure_visit_info_tab(page)

                # Provider ID "..." -> Provider List popup -> pick by last
                # name, disambiguated by first name -> then Billing Info
                if record.get("provider"):
                    how, provider_set = ensure_provider(
                        page, context, record["provider"])
                    provider_source = how
                    provider_on_form = (
                        f"{provider_set['first']} {provider_set['last']} "
                        f"(ID {provider_set['id']})")
                open_billing_info_tab(page)

                # Billable line 1: DOS into both From and To, then pick the
                # CPT from the User CPT popup
                if record.get("dos"):
                    line_dates = fill_line_dates(page, record["dos"])
                if record.get("cpt"):
                    _, cpt_set = select_cpt(page, context, record["cpt"])
                    line_cpt = (f"{cpt_set['cpt']} POS {cpt_set['pos']} "
                                f"charge {cpt_set['charge']} qty {cpt_set['quantity']}")
                    pos_from_cpt = cpt_set["pos"]

                    # Line 1 POS from the sheet. This has to happen here,
                    # after the CPT, for two reasons: the field is not
                    # editable until a CPT is on the line, and selecting
                    # a CPT writes the CPT library's own POS into it.
                    if record.get("pos"):
                        pos_source, line_pos = set_place_of_service(
                            page, record["pos"])

                    # Line 1 Modifier A from the sheet's Hospice Modifier
                    # column - a plain text box next to CPT/POS.
                    if record.get("modifier"):
                        modifier_source, line_modifier = set_modifier_a(
                            page, record["modifier"])
                elif record.get("pos"):
                    pos_source = "skipped - no CPT, POS field not editable"

                # Diagnosis A(1)..L(12) from the sheet's ICD10 cell. Each
                # box is an autocomplete, so the code is typed and then
                # committed by clicking the suggestion it offers.
                if record.get("icd10"):
                    diagnoses, skipped_dx = fill_diagnosis_codes(
                        page, record["icd10"],
                        limit=group.get("dx_limit", MAX_DIAGNOSIS_CODES))
                    # Point the service line at the diagnoses just filled
                    # The form arrives pre-filled from the patient's
                    # Template; drop any diagnosis the sheet did not supply
                    dropped_dx = clear_extra_diagnoses(page, len(diagnoses))
                    pointer = fill_diagnosis_pointer(page, len(diagnoses))

                # Duplicate sheet rows: add their CPTs as extra lines -
                # no diagnosis boxes of their own, but DOS, POS and the
                # ICD-10 Pointer are reapplied to match line 1 (see
                # add_extra_cpt_lines: Office Ally's own popup does not
                # carry any of those over to the new line). Modifier is
                # different: each extra line gets ITS OWN sheet row's
                # Hospice Modifier cell, not line 1's.
                if len(group_cpts) > 1:
                    extra_lines = add_extra_cpt_lines(
                        page, context, group_cpts,
                        pos=record.get("pos"),
                        dos=record.get("dos"),
                        modifiers=group_modifiers,
                        diagnosis_count=len(diagnoses))

                # Read each billable line's charge now that every line is
                # filled (still on the Billing Info tab) - best-effort, a
                # failed read must not fail the visit.
                try:
                    line_charges = read_line_charges(
                        page, max(1, len(group_cpts)))
                except Exception as exc:
                    print(f"           (could not read Line Charges: "
                          f"{str(exc).splitlines()[0][:110]})")

                open_billing_options_tab(page)
                on_billing_options = True
                # Entity Type is left as whatever the visit already
                # carries - unlike Create Visit, this flow must not
                # change it.

                if record.get("facility"):
                    fac_row, fac_fields = select_facility(
                        page, context, record["facility"])
                    if fac_row:
                        facility_set = (
                            f"{fac_row['name']} "
                            f"(ID {fac_fields.get('FacilityProviderID', '') or '-'}, "
                            f"NPI {fac_row['npi']})")

                # --save commits the visit; without it the form is only
                # filled and left on screen.
                if save_visit:
                    save_state, save_detail = click_update(page)
                    saved = f"{save_state}: {save_detail}"[:150]
                    if save_state != "saved":
                        raise RuntimeError(f"Update {saved}")
                    # Done with this visit - leave the tab where Update
                    # put it. Navigating back here is what kept timing
                    # out, and the next patient re-navigates anyway.
                    # This does NOT end the run: carry on to the next.
                    skip_nav_back = True

                    # Read back the Visit ID and (primary) Subscriber ID
                    # for the visit just saved, to write into the sheet's
                    # Visit ID / iNS ID columns. Best-effort: the visit is
                    # already committed regardless of whether this
                    # capture succeeds, so a failure here is reported but
                    # must not be treated as the save having failed.
                    try:
                        visits_after = open_visit_history(page)
                        new_visit = visit_on_dos(
                            visits_after, record.get("dos", ""),
                            record.get("provider", ""))
                        if new_visit:
                            captured_visit_id = str(
                                new_visit.get("Visit ID", "")).strip()
                            open_visit_by_id(page, opened_id, captured_visit_id)
                            captured_subscriber_id = read_subscriber_id(page)
                    except Exception as exc:
                        capture_warning = str(exc).splitlines()[0][:110]
                        print(f"           (could not capture Visit ID / "
                              f"Subscriber ID: {capture_warning})")
                        capture_warning = (f"saved, but could not read Visit "
                                           f"ID / Subscriber ID: {capture_warning}")
                else:
                    # Nothing was saved, so hold the filled form on screen
                    # to be checked by eye, and go no further.
                    skip_nav_back = stop_after_billing
                    stop_run = stop_after_billing
        except Exception as exc:
            opened_url = f"ERROR: {exc}"
            # Also the row's error, so it shows as ERROR in the console and
            # results.csv and as the Note in the web UI - it used to live
            # only in opened_url, leaving the Note blank for a failed visit.
            error = str(exc).splitlines()[0][:200]
        finally:
            # Back to the list so the next patient can be searched.
            # Held back while stopping for verification: this reloads
            # Manage Patients and would wipe the visit form off screen.
            if not skip_nav_back:
                try:
                    back_to_patient_list(page)
                except Exception as exc:
                    # Getting back is only needed for the NEXT patient.
                    # Letting this kill the run would throw away results
                    # for work that already succeeded, including a save.
                    nav_warning = str(exc).splitlines()[0][:110]
                    print(f"           (could not return to the patient "
                          f"list: {nav_warning})")
                name_cache.clear()  # grid was reset by the reload

    if error:
        verdict = "ERROR"
    elif duplicate_of:
        verdict = "SKIP"
    elif found:
        verdict = "YES"
    else:
        verdict = "NO"
    note = f"  {error}" if error else ""
    if duplicate_of and not error:
        note = (f"  already has visit {duplicate_of} on "
                f"{record.get('dos', '')} - nothing entered")
    elif found and not error:
        note = f"  via {method}"
        if not first_match:
            note += "  [different first name]"
    elif unverified and not error:
        note = f"  [surname matched {unverified} - different DOB]"

    # One plain-language line per visit for the web UI's Note column.
    if error:
        row_note = error
    elif duplicate_of:
        row_note = f"already existed (visit {duplicate_of}) - nothing entered"
    elif saved:
        row_note = "created"
    elif verdict == "NO":
        row_note = ("patient not found in Office Ally"
                    + (f" (surname matched {unverified} - different DOB)"
                       if unverified else ""))
    elif visit_url:
        row_note = "filled, not saved (run without Save)"
    else:
        row_note = "patient found"
    if capture_warning:
        row_note += f" - {capture_warning}"

    print(f"[{i}/{total}] {full_name:<30} last={last:<15} "
          f"dob={dob:<11} {verdict}{note}")
    if found and matches:
        print(f"           matched: {matches}")
    if opened_id:
        print(f"           opened PID {opened_id} -> "
              f"{'ok' if 'EditPatient' in opened_url else opened_url}"
              f" | visit history: {len(visits)} visit(s)")
        for v in visits[:3]:
            print(f"             {v.get('DateVisited','')}  "
                  f"{v.get('ProviderName','')[:22]:<22} "
                  f"${v.get('Charges',''):<9} {v.get('Status','')}")
        if visit_url:
            print(f"           new visit form open on Visit Info tab")
            print(f"             {visit_url.split('&From=')[0]}")
        if line_dates:
            print(f"           line 1 DOS: from {line_dates['from']} "
                  f"to {line_dates['to']}")
        if line_cpt:
            print(f"           line 1 CPT: {line_cpt}")
        if line_pos:
            sheet_pos = record.get("pos_raw", "")
            mapped = ("" if sheet_pos == line_pos
                      else f"sheet {sheet_pos} -> ")
            changed = ("" if pos_from_cpt == line_pos
                       else f"  [CPT had {pos_from_cpt or 'blank'}]")
            print(f"           line 1 POS: {mapped}{line_pos} "
                  f"[{pos_source}]{changed}")
        elif pos_source:
            print(f"           line 1 POS: {pos_source}")
        if line_modifier:
            print(f"           line 1 Modifier A: {line_modifier} [{modifier_source}]")
        elif modifier_source:
            print(f"           line 1 Modifier A: {modifier_source}")
        if diagnoses:
            dropped = (len(split_icd10(record.get("icd10_raw", ""), limit=None))
                       - len(diagnoses) - len(skipped_dx))
            tail = f"  [{dropped} more in sheet, past the form's {MAX_DIAGNOSIS_BOXES} boxes]" if dropped > 0 else ""
            print(f"           diagnoses: "
                  + ", ".join(f"{d['box']}={d['value']}"
                              + (" [HCC]" if is_hcc(d["code"]) else "")
                              for d in diagnoses)
                  + tail)
            for d in diagnoses:
                if d["how"] != "picked from list":
                    print(f"             {d['box']} {d['code']}: {d['how']}")
        if skipped_dx:
            print(f"           skipped {len(skipped_dx)} diagnos"
                  f"{'is' if len(skipped_dx) == 1 else 'es'} not recognised "
                  f"by Office Ally: " + ", ".join(skipped_dx))
        if dropped_dx:
            print(f"           removed {len(dropped_dx)} template diagnos"
                  f"{'is' if len(dropped_dx) == 1 else 'es'}: "
                  + ", ".join(dropped_dx))
        if pointer:
            print(f"           ICD-10 Pointer: {pointer} "
                  f"({len(pointer)} diagnos{'is' if len(pointer) == 1 else 'es'})")
        for x in extra_lines:
            print(f"           line {x['line']} DOS: {x['dos_from']} to {x['dos_to']}  "
                  f"CPT: {x['cpt']} "
                  f"POS {x['pos']} [{x['pos_source']}] "
                  f"Modifier A {x['modifier'] or '(none)'} "
                  f"charge {x['charge']} qty {x['quantity']} "
                  f"Pointer {x['pointer'] or '(none)'}"
                  f"   (extra CPT from duplicate sheet row)")
        for n, (charge, source) in enumerate(line_charges, 1):
            print(f"           line {n} charge: {charge or '(blank)'} [{source}]")
        if on_billing_options:
            print("           now on Billing Options tab")
        if facility_set:
            print(f"           Facility: {record.get('facility')} -> {facility_set}")
        if saved:
            print(f"           UPDATE clicked -> {saved}")
        if captured_visit_id or captured_subscriber_id:
            print(f"           captured for sheet: Visit ID="
                  f"{captured_visit_id or '?'} iNS ID="
                  f"{captured_subscriber_id or '?'}")
        if bad_pos:
            print(f"           WARNING: POS cell {bad_pos!r} unreadable - "
                  f"no visit is created for this row")
        if provider_on_form:
            print(f"           provider: {record.get('provider')} -> "
                  f"{provider_on_form} [{provider_source}]"
                  f", now on Billing Info tab")

    result = {
        "row": i,
        "patient_name": full_name,
        "searched_last_name": last,
        "dob": dob,
        "found": verdict,
        "found_via": method if found else "",
        "first_name_match": "yes" if first_match else "no",
        "match_count": len(rows),
        "matches": matches,
        "error": error,
        "surname_hits_rejected_by_dob": unverified,
        "opened_patient_id": opened_id,
        "opened_url": opened_url,
        "visit_count": len(visits),
        "duplicate_skipped": duplicate_of,
        "new_visit_url": visit_url,
        "sheet_provider": record.get("provider", ""),
        "provider_selected": provider_on_form,
        "provider_source": provider_source,
        "sheet_dos": record.get("dos", ""),
        "line_dos_from": line_dates.get("from", ""),
        "line_dos_to": line_dates.get("to", ""),
        "sheet_cpt": record.get("cpt", ""),
        "line_cpt": line_cpt,
        "sheet_pos_raw": record.get("pos_raw", ""),
        "sheet_pos": record.get("pos", ""),
        "sheet_pos_unreadable": bad_pos,
        "pos_from_cpt": pos_from_cpt,
        "line_pos": line_pos,
        "pos_source": pos_source,
        "sheet_modifier": record.get("modifier", ""),
        "line_modifier": line_modifier,
        "modifier_source": modifier_source,
        "sheet_icd10": record.get("icd10_raw", ""),
        "icd10_filled": "; ".join(f"{d['box']}={d['value']}" for d in diagnoses),
        "icd10_count": len(diagnoses),
        "icd10_hcc": "; ".join(d["code"] for d in diagnoses if is_hcc(d["code"])),
        "icd10_pointer": pointer,
        "icd10_skipped_invalid": "; ".join(skipped_dx),
        "template_diagnoses_removed": "; ".join(dropped_dx),
        "on_billing_options": "yes" if on_billing_options else "",
        "sheet_facility": record.get("facility", ""),
        "facility_selected": facility_set,
        "saved": saved,
        "captured_visit_id": captured_visit_id,
        "captured_subscriber_id": captured_subscriber_id,
        "line_charges": "; ".join(c for c, _ in line_charges),
        "_line_charges": [c for c, _ in line_charges],
        "capture_warning": capture_warning,
        "note": row_note,
        "sheet_rows_merged": len(group["rows"]),
        "all_cpts": ", ".join(group_cpts),
        "extra_cpt_lines": "; ".join(
            f"line {x['line']}={x['cpt']} dos={x['dos_from']} "
            f"modifier={x['modifier'] or '(none)'} "
            f"pointer={x['pointer'] or '(none)'}"
            for x in extra_lines),
    }

    return result, group_visits, stop_run


def run_lookups(page, context, records, skip_click=False, make_visit=False,
                save_visit=False, allow_duplicate=False, on_progress=None):
    """Look each patient up by last name, then fall back to date of birth.

    One page, one browser tab, the whole sheet in order - see parallel.py for
    the multi-window version that runs several of these side by side.
    """
    # Without --save a filled visit is only worth looking at, so stop after the
    # first one and leave it on screen. With --save, run the whole sheet.
    stop_after_billing = make_visit and not save_visit
    results = []
    all_visits = []
    name_cache = {}  # one search per distinct last name, reused across duplicate rows

    # Rows identical apart from CPT are one visit with several billable lines
    groups = group_records(records)

    for i, group in enumerate(groups, 1):
        result, group_visits, stop_run = process_group(
            page, context, i, len(groups), group, name_cache, skip_click,
            make_visit, save_visit, allow_duplicate, stop_after_billing)
        all_visits.extend(group_visits)
        results.append(result)

        # Write this visit's status (and any captured Visit ID / iNS ID) back
        # to the sheet right now, not after the whole run finishes - so the
        # output file shows live progress while the browser keeps working.
        if on_progress:
            on_progress(group, result)

        if stop_run:
            remaining = len(groups) - i
            print("\n  " + "=" * 62)
            print("  STOPPED after Billing Options - the filled form is still on")
            print("  screen so it can be checked by eye. Nothing was saved.")
            print("  Check: provider, DOS from/to, CPT, POS, charge, units, diagnoses.")
            if remaining:
                print(f"  {remaining} further visit(s) skipped.")
            print("  Add --save to save each visit and run the whole sheet.")
            print("  " + "=" * 62)
            break

    return results, all_visits


def write_visits(all_visits, visits_path):
    if not all_visits:
        print("No visits collected.")
        return
    visits_path.parent.mkdir(parents=True, exist_ok=True)
    with visits_path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(all_visits[0].keys()))
        writer.writeheader()
        writer.writerows(all_visits)
    print(f"Wrote {len(all_visits)} visits to {visits_path.name}")


def write_results(results, results_path):
    if not results:
        # Every row was already Done and skipped by resume, or the sheet
        # had no rows to begin with - not an error, just nothing new this
        # run. write_visits already guards the same way; results[0] below
        # would otherwise raise on an empty list.
        print("No new rows processed.")
        return
    results_path.parent.mkdir(parents=True, exist_ok=True)
    # Keys starting with "_" carry structured data for the status sheet, not csv
    fieldnames = [k for k in results[0].keys() if not k.startswith("_")]
    with results_path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames,
                                extrasaction="ignore")
        writer.writeheader()
        writer.writerows(results)
    print(f"\nWrote {len(results)} rows to {results_path.name}")


def _is_saved(result):
    # click_update() returns save_state "saved" only on a real commit; any
    # other outcome (validation error, timeout, ...) still leaves `saved`
    # non-empty, so the exact "saved:" prefix is what to check, not truthiness.
    return str(result.get("saved", "")).startswith("saved:")


def status_for_group(group, result):
    """One Done/Not Created verdict per ORIGINAL sheet row in this group (a
    visit can cover several sheet rows via group_records). Done if the visit
    was saved this run, or a visit already existed for that DOS
    (duplicate_skipped). Anything else - not found, an error, filled but not
    saved, or never reached - is Not Created."""
    status = "Done" if (_is_saved(result) or result.get("duplicate_skipped")) else "Not Created"
    # A foot-care visit whose diagnoses lack B35.1 is still created - the
    # status just says so (only for a visit saved this run, not one that
    # already existed and was left untouched).
    if _is_saved(result) and group.get("missing_icd"):
        status = f"Done - {group['missing_icd']} not exists"
    return {row_record["row"]: status for row_record in group["rows"]}


def skip_done_rows(all_records, previous_status):
    """Every sheet row EXCEPT ones a previous run already marked Done.

    This is the resume check: a row counts as already handled if the last
    run's output workbook has "Done" in its Status column for that row -
    which status_for_group only ever writes when the visit was saved that
    run, or Practice Mate already had a visit on that DOS/provider when
    checked (see status_for_group / process_group's duplicate check). A row
    marked "Not Created" (not found, an error, filled but not saved, or the
    run stopped before reaching it) is NOT considered done - it gets
    retried, not skipped.

    Returns the records to actually process this run.
    """
    done_rows = {row for row, status in previous_status.items()
                if str(status).startswith("Done")}
    if not done_rows:
        return all_records
    return [r for r in all_records if r["row"] not in done_rows]


def captured_for_group(group, result, column_map=None):
    """Visit ID / iNS ID / Line Charges captured for this group's visit, one
    entry per ORIGINAL sheet row (mirrors status_for_group's per-row spread).
    Empty if nothing was captured for this group.

    Line Charges is per row: each sheet row that carries a CPT is one
    billable line, in order, so it gets that line's charge. Only written
    for a visit that was actually saved this run."""
    fields = resolve_column_map(column_map)
    visit_id = result.get("captured_visit_id", "")
    subscriber_id = result.get("captured_subscriber_id", "")
    charges = result.get("_line_charges") or [] if _is_saved(result) else []
    if not visit_id and not subscriber_id and not charges:
        return {}
    captured = {}
    line = 0
    for row_record in group["rows"]:
        entry = {fields["visit_id"]: visit_id,
                 fields["beneficiary_id"]: subscriber_id}
        if str(row_record.get("cpt", "")).strip():
            if line < len(charges):
                entry[fields["line_charge"]] = charges[line]
            line += 1
        captured[row_record["row"]] = entry
    return captured


class _Tee:
    """Duplicate every write to the original stream and a log file, so the
    terminal is completely unaffected but everything printed is also saved
    for after-the-fact debugging."""

    def __init__(self, stream, log_file):
        self._stream = stream
        self._log_file = log_file
        self._lock = threading.Lock()

    def write(self, data):
        with self._lock:
            self._stream.write(data)
            self._log_file.write(data)
            self._log_file.flush()
        return len(data)

    def flush(self):
        with self._lock:
            self._stream.flush()
            self._log_file.flush()

    def isatty(self):
        return self._stream.isatty()


@contextmanager
def _tee_stdout_to_file(log_path, excel_path):
    """Mirror everything printed during a run into output/logs.txt too, so
    the full console output (including --workers threads, which share this
    same process) survives after the terminal/browser window is closed.
    Appends, with a header per run, rather than overwriting - so one run's
    logs don't erase the previous run's.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = log_path.open("a", encoding="utf-8")
    log_file.write(f"\n{'=' * 70}\n"
                   f"Run started {datetime.now():%Y-%m-%d %H:%M:%S}"
                   f"  (excel: {excel_path.name})\n{'=' * 70}\n")
    log_file.flush()

    original_stdout, original_stderr = sys.stdout, sys.stderr
    sys.stdout = _Tee(original_stdout, log_file)
    sys.stderr = _Tee(original_stderr, log_file)
    try:
        yield
    finally:
        sys.stdout, sys.stderr = original_stdout, original_stderr
        log_file.close()


def run_automation(excel_path=None, column_map=None,
                   workers=None, limit=None, only=None, no_resume=False,
                   skip_click=False, make_visit=False, save_visit=False,
                   allow_duplicate=False, no_wait=False, interactive=None,
                   progress_hook=None, retries=1):
    """Log in fresh (the old auth_state.json is deleted first), read `excel_path`,
    resume from its last output file, then run it - one window here, or
    N parallel windows with `workers`. This IS the tool; main() below is
    just an argv-parsing wrapper around it, and the web server (web/app.py)
    calls it directly the same way.

    `column_map` lets the sheet use its own column headers instead of this
    tool's defaults (patients.DEFAULT_COLUMN_MAP) - only the fields you
    want to override need to be in it.

    `progress_hook(group_status, group_captured, result)`, if given, fires
    after every visit finishes, on top of the incremental Status/Visit ID/
    iNS ID xlsx write that always happens regardless. It may be called from
    a worker thread when `workers` is set, so it must be thread-safe - the
    web server's hook is a queue.put(), which is.

    `retries` controls automatic re-attempts, within this same run, of
    whatever rows are still "Not Created" once the sheet has been gone
    through once - a row can land there for a reason that will not go
    away (patient not found), but also for one that often will (a slow
    popup, a dropped connection, Office Ally momentarily not responding).
    Rather than making someone notice that and re-run the whole sheet by
    hand, the leftover rows are tried again automatically, up to `retries`
    times, before the run is considered finished. 0 disables this. Does
    not apply to a --create-visit run without --save, which deliberately
    stops after one visit to be checked by eye. A retry pass always runs
    one window at a time regardless of `workers` - only attempt 1 is ever
    parallel - since the leftover batch is usually small and retrying it
    with the same worker count would put the same load back on Office
    Ally that may have caused the failures.

    Returns a dict summarizing the run, or {"logged_in": False} if login
    failed.
    """
    excel_path = Path(excel_path) if excel_path else EXCEL_PATH
    if interactive is None:
        interactive = sys.stdin.isatty()

    status_xlsx_path = status_xlsx_path_for(excel_path)
    results_path = results_path_for(excel_path)
    visits_path = visits_path_for(excel_path)

    with _tee_stdout_to_file(OUTPUT_DIR / "logs.txt", excel_path), sync_playwright() as p:
        browser = p.chromium.launch(
            channel="msedge",
            headless=False,
            args=["--start-maximized"],
        )

        # Every run starts from a fresh login: the old auth_state.json is
        # deleted first, never reused. log_in() writes a new one, which the
        # parallel worker windows and the retry window then share for the
        # rest of THIS run only.
        if STATE_PATH.exists():
            STATE_PATH.unlink()
            print(f"Deleted old {STATE_PATH.name} - logging in fresh...")

        context, page, logged_in = log_in(browser)

        if not logged_in:
            browser.close()
            return {"logged_in": False}

        # Parallel mode (workers) opens its own separate window per worker,
        # each reusing the session this run just logged in with - this
        # window only existed to make sure auth_state.json is good, so
        # close it now rather than leave an extra unused browser open.
        browser_closed = False
        if workers:
            browser.close()
            browser_closed = True
        else:
            print("\nPractice Mate is open.")
            print("  URL:  ", page.url.split("?")[0])
            print("  Title:", page.title())
            open_manage_patients(page)

        all_records = read_patients(excel_path, column_map=column_map)
        if only:
            all_records = [r for r in all_records
                           if only.lower() in r["name"].lower()]

        # Resume: a row a previous run already got to Done (saved, or a
        # visit already existed for that DOS) is skipped rather than
        # searched and re-checked - the sheet's own output/*_with_status
        # file from last time is the source of truth for that, since the
        # browser can get closed partway through a long sheet and the
        # next run would otherwise start over from row 1.
        previous_status = {}
        previous_captured = {}
        records = all_records
        if not no_resume:
            previous_status = read_previous_status(status_xlsx_path, column_map)
            previous_captured = read_previous_captured(status_xlsx_path, column_map)
            records = skip_done_rows(all_records, previous_status)
            if len(records) < len(all_records):
                print(f"Resuming from {status_xlsx_path.name}: "
                      f"{len(all_records) - len(records)} row(s) already "
                      f"Done, skipping them")

        if limit:
            records = records[:limit]
        visits_planned = len(group_records(records))
        print(f"\nLooking up {visits_planned} visit(s) "
              f"from {len(records)} sheet row(s)\n")

        # make_visit without --save deliberately stops after ONE visit so it
        # can be checked by eye before trusting a sheet/mapping - retrying
        # would just refill another single visit and stop again, so the
        # leftover-row retry pass below only makes sense for a full run.
        stop_after_billing = make_visit and not save_visit
        max_attempts = 1 if stop_after_billing else 1 + max(0, retries)

        results = []
        all_visits = []
        row_status = {}
        captured_columns = {}
        status_handle = None
        pass_records = records
        pass_previous_status = previous_status
        pass_previous_captured = previous_captured

        # A retry pass (attempt 2+) always runs one browser window at a
        # time, even when the original run used --workers N - the leftover
        # batch is usually small, and retrying it in parallel would put the
        # same worker-count load back on Office Ally that the failures may
        # have come from in the first place. --workers only ever governs
        # attempt 1. If attempt 1 WAS parallel, its single check-in window
        # is already closed by the time we'd need one for a retry, so one
        # is opened here on first use and reused for every later attempt.
        retry_browser = retry_context = retry_page = None

        def sequential_session():
            nonlocal retry_browser, retry_context, retry_page
            if not workers:
                return page, context
            if retry_context is None:
                retry_browser = p.chromium.launch(
                    channel="msedge", headless=False, args=["--start-maximized"])
                retry_context = retry_browser.new_context(
                    storage_state=str(STATE_PATH), no_viewport=True)
                retry_page = retry_context.new_page()
                retry_page.goto(PM_URL, wait_until="domcontentloaded", timeout=90000)
                retry_page.wait_for_load_state("load")
                # Same check parallel.py's open_manage_patients_from_pm makes
                # before trusting a shared session in a fresh window - the
                # attempt-1 parallel pass already confirmed the session was
                # good, but a long-running sheet could still outlive it by
                # the time a retry window opens.
                if "auth.officeally.com" in retry_page.url:
                    raise RuntimeError(
                        "saved session did not carry over to the retry "
                        "window - auth_state.json may have expired mid-run")
                open_manage_patients(retry_page)
            return retry_page, retry_context

        for attempt in range(1, max_attempts + 1):
            if workers and attempt == 1:
                from parallel import run_parallel
                pass_results, pass_visits, row_status, captured_columns = run_parallel(
                    pass_records, all_records, STATE_PATH, excel_path, status_xlsx_path,
                    workers=workers, skip_click=skip_click, make_visit=make_visit,
                    save_visit=save_visit, allow_duplicate=allow_duplicate,
                    previous_status=pass_previous_status,
                    previous_captured=pass_previous_captured,
                    no_wait=no_wait, column_map=column_map, progress_hook=progress_hook)
            else:
                cur_page, cur_context = sequential_session()
                if status_handle is None:
                    # Open the status workbook and seed every row BEFORE the
                    # run starts, so the output file exists and is readable
                    # from the first visit onward - not just once the whole
                    # sheet is done. A row skipped this run because it's
                    # already Done is seeded from previous_status/
                    # previous_captured (its Visit ID / iNS ID are carried
                    # over too - this run never reopens it, so that's the
                    # only place those values can still come from), not
                    # defaulted to Not Created.
                    status_handle = open_status_workbook(excel_path, column_map)
                    row_status = {r["row"]: pass_previous_status.get(r["row"], "Not Created")
                                  for r in all_records}
                    captured_columns = dict(pass_previous_captured)
                    write_status_rows(status_handle, row_status,
                                      extra_columns_by_row=pass_previous_captured)
                    save_status_workbook(status_handle, status_xlsx_path)

                def on_progress(group, result):
                    group_status = status_for_group(group, result)
                    group_captured = captured_for_group(group, result, column_map)
                    row_status.update(group_status)
                    captured_columns.update(group_captured)
                    write_status_rows(status_handle, group_status,
                                       extra_columns_by_row=group_captured)
                    save_status_workbook(status_handle, status_xlsx_path)
                    if progress_hook:
                        progress_hook(group_status, group_captured, result)

                pass_results, pass_visits = run_lookups(
                    cur_page, cur_context, pass_records,
                    skip_click=skip_click, make_visit=make_visit,
                    save_visit=save_visit, allow_duplicate=allow_duplicate,
                    on_progress=on_progress)

            results.extend(pass_results)
            all_visits.extend(pass_visits)

            if attempt == max_attempts:
                break

            # Retry only the rows THIS run left Not Created - a row that
            # failed for a real reason (patient not on file, bad data)
            # fails the same way again, but one stuck on a slow popup or a
            # dropped connection often clears on a second try. Rows already
            # Done, or never touched this run (--limit/--only/resume), are
            # never revisited here.
            pass_records = [r for r in pass_records
                            if row_status.get(r["row"]) == "Not Created"]
            if not pass_records:
                break
            pass_previous_status = row_status
            pass_previous_captured = captured_columns
            print(f"\n{len(pass_records)} row(s) still Not Created after "
                  f"attempt {attempt} - retrying one at a time "
                  f"(attempt {attempt + 1}/{max_attempts})\n")

        # This is a separate window from the one --workers used for attempt
        # 1 (if any) - close it here rather than leaving it for the
        # end-of-run wait-for-Enter below, which only ever knew about the
        # main `browser`.
        if retry_browser is not None:
            retry_browser.close()

        write_results(results, results_path)
        write_visits(all_visits, visits_path)

        done = sum(1 for s in row_status.values() if str(s).startswith("Done"))
        print(f"Status column in {status_xlsx_path.name} (output/) was "
              f"updated live as each visit finished "
              f"({done} of {len(row_status)} rows Done"
              f", {len(captured_columns)} row(s) with Visit ID/iNS ID captured)")

        # Parallel mode's browser was already closed above, and each of its
        # worker windows waits for its own Enter press and closes itself.
        if not browser_closed:
            if not no_wait and interactive:
                input("\nPress Enter to close the browser...")
            browser.close()

    return {
        "logged_in": True,
        "results": results,
        "all_visits": all_visits,
        "row_status": row_status,
        "captured_columns": captured_columns,
        "done": done,
        "total": len(row_status),
        "results_path": results_path,
        "visits_path": visits_path,
        "status_xlsx_path": status_xlsx_path,
    }


def main():
    workers = None
    if "--workers" in sys.argv:
        workers = int(sys.argv[sys.argv.index("--workers") + 1])
        if workers < 1:
            sys.exit("--workers must be at least 1")

    excel_path = EXCEL_PATH
    if "--excel" in sys.argv:
        excel_path = Path(sys.argv[sys.argv.index("--excel") + 1])

    run_automation(
        excel_path=excel_path,
        workers=workers,
        limit=(int(sys.argv[sys.argv.index("--limit") + 1])
              if "--limit" in sys.argv else None),
        only=(sys.argv[sys.argv.index("--only") + 1]
             if "--only" in sys.argv else None),
        no_resume="--no-resume" in sys.argv,
        skip_click="--no-click" in sys.argv,
        make_visit="--create-visit" in sys.argv,
        save_visit="--save" in sys.argv,
        allow_duplicate="--allow-duplicate" in sys.argv,
        no_wait="--no-wait" in sys.argv,
        retries=(int(sys.argv[sys.argv.index("--retries") + 1])
                if "--retries" in sys.argv else 1),
    )


if __name__ == "__main__":
    main()
