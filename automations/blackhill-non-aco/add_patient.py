"""Log in to Practice Mate, reusing a saved session when possible, and open
the blank Add New Patient form under Manage Patients.

This is the navigation step only - it opens the form and stops there
without filling or saving anything.

Usage:
    python add_patient.py            # reuse the saved session if it is still valid
    python add_patient.py --fresh    # ignore the saved session and log in again
"""

import sys

from playwright.sync_api import sync_playwright

from main import STATE_PATH, has_valid_session, log_in, open_manage_patients
from patients import open_add_new_patient


def main():
    force_fresh = "--fresh" in sys.argv

    with sync_playwright() as p:
        browser = p.chromium.launch(
            channel="msedge",
            headless=False,
            args=["--start-maximized"],
        )

        context = page = None
        logged_in = False

        if STATE_PATH.exists() and not force_fresh:
            print(f"Loading saved session from {STATE_PATH.name}...")
            context = browser.new_context(storage_state=STATE_PATH, no_viewport=True)
            page = context.new_page()

            if has_valid_session(page):
                print("Reused saved session - no login needed.")
                logged_in = True
            else:
                print("Saved session expired. Logging in again...")
                context.close()
                context = None

        if not logged_in:
            context, page, logged_in = log_in(browser)

        if not logged_in:
            browser.close()
            sys.exit("Login failed.")

        open_manage_patients(page)
        open_add_new_patient(page)

        print("\nAdd New Patient form is open.")
        print("  URL:  ", page.url.split("?")[0])
        print("  Title:", page.title())

        if sys.stdin.isatty():
            input("\nPress Enter to close the browser...")
        browser.close()


if __name__ == "__main__":
    main()
