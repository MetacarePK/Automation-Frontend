"""Parallel claim creation: N separate browser windows, one saved login shared
between them, each pulling the next patient off a shared queue.

Run: python main.py --create-visit --save --workers 3

Unlike a multi-TAB approach (several pages sharing one browser context, the
way provider/parallel.py speeds up its read-only lookups), each worker here
gets its own browser AND its own context - a full separate window, not a tab.
That matters because this flow, unlike provider's, opens popups (Provider
search, CPT search, Facility search) while filling a visit. Playwright's
popup-catching (expect_page) is scoped to a context: if two workers shared
one context and both opened a popup close together, one worker could catch
the OTHER worker's popup - meaning one patient's CPT or provider gets typed
onto a different patient's claim. Giving every worker its own browser makes
that impossible, at the cost of more memory than tabs-in-one-window would
use. For a flow that saves real claims, that trade is worth it.

Each worker is a plain Python thread running its own `sync_playwright()`
instance - Playwright requires a separate instance per thread, but otherwise
every function this calls (process_group, and everything under it in
patients.py) is the exact same code the sequential --create-visit path uses.
Nothing about the Practice Mate interaction itself changes; only how groups
are handed out does.

Start small. Test with --create-visit and NO --save first: every worker
fills exactly one visit and holds its window open (same "hold on screen,
nothing saved" behavior the sequential path has without --save, just one
window per worker instead of one for the whole sheet), so you can watch N
windows fill different patients at once and check them by eye before
trusting any of this to --save for real.
"""

import queue
import sys
import threading
import time

from playwright.sync_api import sync_playwright

from main import open_manage_patients, process_group, status_for_group, captured_for_group
from patients import (
    group_records,
    open_status_workbook,
    write_status_rows,
    save_status_workbook,
    last_name_of,
)


def partition_groups(groups, workers):
    """Split groups into `workers` buckets, keeping every group for the same
    patient in the SAME bucket - never split across two.

    All workers share one Office Ally login session (see module docstring).
    Two windows editing the SAME patient's chart at the same time under that
    one session has been observed to hang the Update click past its timeout
    (Office Ally never responds), even though the visit was filled out
    correctly - almost certainly the server serializing/locking concurrent
    requests against one patient's record within a single session. Forcing
    a repeat patient's visits onto one worker makes them run one after
    another instead of concurrently, which avoids that race entirely.
    Different patients still run fully in parallel, same as before.
    """
    patient_bucket = {}
    buckets = [[] for _ in range(workers)]
    for i, group in enumerate(groups, 1):
        record = group["record"]
        key = (last_name_of(record["name"]).strip().upper(),
               record.get("dob", ""))
        if key not in patient_bucket:
            patient_bucket[key] = len(patient_bucket) % workers
        buckets[patient_bucket[key]].append((i, group))
    return buckets


def open_manage_patients_from_pm(page):
    """Land a fresh window on Manage Patients, reusing the saved session.

    run_automation() has just logged in fresh and saved auth_state.json
    before any worker starts, so every worker only needs the happy path: go
    to Practice Mate with that session, then click into Manage Patients.
    """
    page.goto("https://pm.officeally.com/pm/", wait_until="domcontentloaded",
              timeout=90000)
    page.wait_for_load_state("load")
    if "auth.officeally.com" in page.url:
        raise RuntimeError(
            "this run's fresh login did not carry over to this window - "
            "run again to log in fresh")
    open_manage_patients(page)


def worker(worker_id, state_path, group_queue, results, results_lock,
          status_handle, status_lock, status_out_path, total,
          skip_click, make_visit, save_visit, allow_duplicate,
          stop_after_billing, wait_for_enter, column_map=None,
          progress_hook=None):
    """One browser window, working through the shared queue until it's empty
    or (without --save) it fills one visit and stops to be checked by eye.

    Every window waits for its own Enter press before closing (unless
    --no-wait), the same "browser stays open until Enter is pressed" promise
    the sequential path makes - just one prompt per window here instead of
    one for the whole run. With several windows finishing at different
    times, that means pressing Enter once per window as each one is done
    being checked, not once for everything at once.

    `progress_hook`, if given, is called after every visit from whichever
    thread happened to finish it - it must be thread-safe (a queue.put() is;
    the web server is the only caller that passes one).
    """
    name_cache = {}
    held_for_inspection = False

    with sync_playwright() as p:
        browser = p.chromium.launch(
            channel="msedge", headless=False, args=["--start-maximized"])
        try:
            context = browser.new_context(storage_state=str(state_path), no_viewport=True)
            page = context.new_page()
            print(f"[worker {worker_id}] opening its own window with the saved session...")
            open_manage_patients_from_pm(page)

            while True:
                try:
                    i, group = group_queue.get_nowait()
                except queue.Empty:
                    break

                result, group_visits, stop_run = process_group(
                    page, context, i, total, group, name_cache, skip_click,
                    make_visit, save_visit, allow_duplicate, stop_after_billing)

                with results_lock:
                    results.append((i, result, group_visits))

                group_status = status_for_group(group, result)
                group_captured = captured_for_group(group, result, column_map)
                with status_lock:
                    write_status_rows(status_handle, group_status,
                                      extra_columns_by_row=group_captured)
                    save_status_workbook(status_handle, status_out_path)
                if progress_hook:
                    progress_hook(group_status, group_captured, result)

                if stop_run:
                    held_for_inspection = True
                    print(f"[worker {worker_id}] filled one visit, holding it "
                          f"on screen - not saved, add --save to run for real")
                    break
        finally:
            if wait_for_enter:
                label = "the filled form" if held_for_inspection else "this window"
                input(f"\n[worker {worker_id}] Press Enter to close {label}...")
            browser.close()


def run_parallel(records, all_records, state_path, excel_path, status_out_path,
                 workers=3, skip_click=False, make_visit=False,
                 save_visit=False, allow_duplicate=False,
                 previous_status=None, previous_captured=None,
                 no_wait=False, column_map=None, progress_hook=None):
    """Group rows by visit, then hand the groups to N browser windows.

    `records` is what this run will actually process (after --limit/--only/
    resume filtering); `all_records` is every row on the sheet, needed so
    every row - including ones this run never touches - gets a Status value
    seeded up front, matching the sequential path.

    Returns (results, all_visits, row_status, captured_columns) - the same
    shape main() already builds for the sequential path, so main() can print
    the same summary line regardless of which path ran.
    """
    if not state_path.exists():
        raise SystemExit(
            f"No saved session at {state_path.name} yet - run the sequential "
            f"command once first (python main.py) so there is a session for "
            f"every window to share, then re-run with --workers.")

    stop_after_billing = make_visit and not save_visit
    groups = group_records(records)
    total = len(groups)

    # Seed the status workbook up front, same as the sequential path: EVERY
    # sheet row gets a Status value immediately, defaulting to Not Created,
    # so a row this run never touches (--limit/--only, or already Done from
    # a previous run) still shows a value instead of a blank cell.
    previous_status = previous_status or {}
    status_handle = open_status_workbook(excel_path, column_map)
    row_status = {r["row"]: previous_status.get(r["row"], "Not Created")
                  for r in all_records}
    write_status_rows(status_handle, row_status,
                      extra_columns_by_row=previous_captured)
    save_status_workbook(status_handle, status_out_path)

    if total == 0:
        # Nothing to do (everything was already Done and resumed past, or
        # the sheet/filter matched no rows) - the status file above is
        # still worth writing, but there is no reason to open N browser
        # windows, log each into Office Ally, and immediately close them
        # again for zero work. That's wasted traffic against an account
        # that has been IP-throttled before for looking automated.
        print("Nothing to process - every row was already resumed as Done.")
        return [], [], row_status, dict(previous_captured or {})

    results = []
    results_lock = threading.Lock()
    status_lock = threading.Lock()  # only one worker touches the workbook at a time
    wait_for_enter = not no_wait and sys.stdin.isatty()

    # One queue per worker, not one shared queue - partition_groups keeps
    # every group for the same patient in the same bucket, so two workers
    # can never end up editing that patient's chart at the same time.
    buckets = partition_groups(groups, workers)
    queues = []
    for w, bucket in enumerate(buckets):
        q = queue.Queue()
        for item in bucket:
            q.put(item)
        queues.append(q)

    started = time.time()
    print(f"{total} visit(s) across {workers} browser window(s)")
    for w, bucket in enumerate(buckets):
        print(f"  worker {w + 1}: {len(bucket)} visit(s)")
    print()

    threads = [
        threading.Thread(
            target=worker,
            args=(w + 1, state_path, queues[w], results, results_lock,
                  status_handle, status_lock, status_out_path, total,
                  skip_click, make_visit, save_visit, allow_duplicate,
                  stop_after_billing, wait_for_enter, column_map, progress_hook))
        for w in range(workers)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    elapsed = time.time() - started
    print(f"\n{total} visit(s) in {elapsed / 60:.1f} min "
          f"({elapsed / max(total, 1):.1f}s each, {workers} window(s))")

    results.sort(key=lambda r: r[0])
    all_results = [r for _, r, _ in results]
    all_visits = [v for _, _, vs in results for v in vs]

    # row_status/captured_columns are rebuilt here (rather than updated live
    # via a callback like the sequential path's on_progress) purely so
    # main() gets the same return shape to print its summary from - the
    # actual disk writes already happened inside each worker, under
    # status_lock, as every visit finished.
    captured_columns = dict(previous_captured or {})
    for i, result, _ in results:
        group = groups[i - 1]
        row_status.update(status_for_group(group, result))
        captured_columns.update(captured_for_group(group, result, column_map))

    return all_results, all_visits, row_status, captured_columns
