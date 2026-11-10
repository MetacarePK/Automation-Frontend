# Automation Dashboard: How It Works

A plain-language guide to the system: what it does, how it works, what it stores, and what it does not do yet.
Written on 2026-11-10. Technical details are at the end of each section and in the reference tables.

---

## 1. What it is, in one minute

Medical billing at Meta X Care involves a lot of repetitive typing in a web portal called **Office Ally Practice Mate**:
for every patient visit on a billing sheet, someone has to find the patient, create a visit, and type in the codes.

We built **automations**: programs that open a real browser, log in to Office Ally, and do that typing by themselves,
row by row. They have been running for about three months on one Windows PC.

This project, the **Automation Dashboard**, puts those automations on a website so that:

- several people can use them, each with their own login;
- an administrator controls who is allowed in and which automations each person can use;
- everyone can start, watch, and stop runs from a browser, instead of sitting at one PC;
- everything is recorded: who signed in, who ran what, what happened to every row.

The automations themselves were **not rewritten**. The dashboard wraps them, so the Office Ally logic that has been
tested for three months is exactly the same.

```
  Person (browser)
        |  signs in, uploads a sheet, presses Start
        v
  Dashboard website  ----->  Database (who, what, results)
        |
        |  one run at a time per automation
        v
  Runner (a separate process per run)
        |  opens a browser
        v
  Office Ally Practice Mate  (creates the visits)
```

---

## 2. The two automations today

| | Blackhill ACO | Blackhill Non ACO |
|---|---|---|
| Does | Creates ACO visits in Practice Mate from a billing sheet | Creates Non ACO visits in Practice Mate from a billing sheet |
| Office Ally login | Its own account | Its own account |
| Modifier column | "Hospice Modifier" | "Modifier" |
| Entity Type field | Left as it is | Set by the automation |
| Insurance ID shown as | iNS ID | Beneficiary ID |
| Login behaviour | Logs in fresh on every run | Reuses the saved login while it is still valid |

Each automation appears on the dashboard as a **card**: a colored header (green for ACO, blue for Non ACO), a status
label, and a **Start** button (or **Stop** and **View** while a run is active).

More automations can be added later. Each one is just a folder with its program and a small description file
(`manifest.json`). Nothing else in the dashboard changes.

---

## 3. Who uses it

### Roles

| Role | Can do |
|---|---|
| **User** | Sign in, see only the automations the admin gave them, upload sheets, start, stop and retry their own runs, see their own history, files and activity, change their password. An admin can also allow a person to **update an automation's Office Ally login** (username and password, for sometimes they change); admins always can |
| **Admin** | Everything a user can, plus: approve or disable accounts, choose which automations each person can use, reset passwords, unlock accounts, make other admins, see every run and stop anyone's run, see sign-in monitoring and the full activity log, end sessions |

### Getting an account

1. A person opens the site and chooses **Request an account** (name, email, password).
2. The account is created as **Pending**. They cannot sign in yet.
3. An admin sees it under **Admin, Awaiting approval**, reviews it, ticks which automations the person may use,
   and approves it.
4. The person can now sign in and sees a card for each automation they were given.

The very first admin is never created through the website. It is created from the server's settings file or a
command (see section 10).

If someone requests an account with an email that already exists, the site gives the same answer as for a new one,
so nobody can use the form to find out who has an account.

---

## 4. A run, from start to finish

A **run** is one pass of an automation over one billing sheet.

1. **Start.** On the dashboard the person clicks **Start** on a card.
2. **Choose a sheet.** They pick an `.xlsx` file. It uploads immediately.
3. **Columns.** The dashboard matches the sheet's column names to what the automation needs. A layout used before is remembered.
   When all required columns are matched it shows "Columns matched"; otherwise it opens so the person can pick the missing ones.
4. **Options.** "Save to Office Ally" (on by default) and "Windows" (browser windows working at once, default 4, maximum 4).
   Under "More options": only the first N rows, only names containing some text, allow duplicates, start over.
5. **Start.** A confirmation asks if visits really should be created. The dashboard reads the sheet with the automation's own
   reader first, so an invalid file is refused with a clear message before anything is queued. The same sheet cannot be started
   twice at once.
6. **The run gets its own private folder** (its own copy of the sheet, its own output, its own log). Nothing is shared with other runs.
7. **Waiting.** A run starts when the server has room (see section 9). Until then it shows **Waiting**.
8. **Checking.** Just before starting, the server checks the sheet again and that the automation has a login saved.
9. **Running.** The page updates every two seconds: counts (Successful, Skipped, Failed, Pending), a colored bar, and a table of the rows.
   Filters show only Failed, Skipped or Successful rows. "Log" shows the run's log, "History" shows what happened to the run (queued, checking,
   started, retried, stopped...), "Settings" shows the options it was started with.
10. **Stop.** **Stop** (on the run page and on the dashboard card) ends the run and closes its browsers. Visits already saved stay saved.
11. **Finish.** The run ends in one of these states:

| State | Meaning |
|---|---|
| **Completed** | Every row was processed and the result sheet was read back and matches what the dashboard recorded |
| **Completed with errors** | It finished, but some rows failed (or reported nothing). Review them and retry |
| **Failed** | It stopped with a problem. The reason is shown (wrong login, invalid sheet, crash, result could not be verified...) |
| **Interrupted** | The server stopped while it was running. Retry it |
| **Stopped** | Someone pressed Stop |

A run is only called **Completed** after its result sheet has been checked against the database. If they disagree it is marked **Failed**.

12. **Review and download.** "Failed records (CSV)" lists every failed row with its reason. "Result sheet" is a copy of the sheet with
    Status, Visit ID, insurance ID and charges filled in.

Closing the browser tab does not affect a run. It lives on the server; reopening the card shows it again.

### Retry

**Retry** (on the run page) runs the same sheet again. It is built to be safe: rows that are already done are **not processed again**
(the run shows "Done earlier"), duplicates are never allowed, and the engine still checks Office Ally for an existing visit first.
Only failed, stopped, interrupted, or completed-with-errors runs can be retried. A run that **crashes** (browser or process died) is
retried once automatically after a short wait; a wrong login or an invalid sheet is never retried automatically, because it would only fail again.
A changed sheet starts from scratch (progress is tied to the sheet's exact contents).

### Row results

| Result | Meaning |
|---|---|
| **Successful** | The visit was created and saved |
| **Skipped** | A visit for that patient already existed, so nothing new was created. The existing visit's ID is recorded |
| **Failed** | Patient not found, bad data, or an Office Ally problem. The reason is in Note |
| **Pending** | Not processed yet |

Failed rows are retried once automatically at the end of a run (many failures are temporary, such as a slow popup).

---

## 5. What the automation does inside Office Ally

For each patient (group of rows) on the sheet:

1. Opens Practice Mate, goes to Manage Patients.
2. Searches by last name and confirms by date of birth; if that fails, searches by date of birth instead.
3. Opens the patient's chart and reads the Visit History.
4. **Duplicate check:** if a visit already exists for that date of service, it does not create another. It records the
   existing visit's ID. The same patient, date and CPT appearing twice in the sheet is also treated as a duplicate.
   (The ACO version also requires the same provider, since two doctors can legitimately see one patient on one day.)
5. Creates a new visit from the template:
   - provider (from the sheet),
   - date of service, CPT code, place of service, modifier,
   - diagnosis (ICD) codes, up to 12, ordered with the first sheet code, then HCC codes, then Z codes, then the rest,
   - ICD pointers, and extra lines when one visit has several CPT codes,
   - facility (and Entity Type for Non ACO).
6. Saves the visit (if "Save to Office Ally" is on) and writes back the Status, Visit ID, insurance ID and line charges.

A blank or unreadable place of service stops that visit from being created, rather than guessing.

---

## 6. What is stored, and where

Everything lives in one folder, `storage/`, plus two kinds of settings files.

### 6.1 The database (`storage/automation.db`, SQLite)

| Table | What it holds |
|---|---|
| `users` | Name, email, password **hash** (never the password), role, status (pending/active/disabled), failed-sign-in count and lock time, dates |
| `sessions` | One row per signed-in browser: a **hash** of the session token, who, times, IP address, browser name, whether it was ended |
| `user_automations` | Which person may use which automation, and whether they may also update that automation's Office Ally login |
| `login_events` | Every sign-in attempt, successful or not, with reason, IP and browser |
| `activity_logs` | The audit trail: who did what, to what, from which IP (sign-ups, approvals, access changes, uploads, runs, retries, downloads, login updates and tests). Login **values** are never recorded |
| `runs` | One row per run: automation, who started it, state, sheet name, the run's private copy of the sheet, the options chosen, total rows, rows carried over from earlier runs, summary, error and failure kind, times, retry link and attempt number, result file location |
| `run_events` | A timeline per run: queued, checking, started, retry queued, stop requested, finished, with details such as the exit code and the last lines of output |
| `run_rows` | One row per sheet row per run: patient name, date of service, status, Visit ID, insurance ID, skipped marker, error, line charges, note |
| `column_mappings` | The remembered column match per sheet layout and automation |
| `schema_version` | Which database version is installed, so updates upgrade it safely |

### 6.2 Files on disk (`storage/`)

| Location | What it holds |
|---|---|
| `runs/<run id>/input/` | The run's own copy of the uploaded sheet |
| `runs/<run id>/output/` | The automation's result files: the result sheet, results and visits CSVs, its own log |
| `runs/<run id>/run.log` | The run's log: timestamped lines, secrets hidden, size-capped |
| `runs/<run id>/auth_state.json` | The saved Office Ally browser session (cookies) **while the run is going; deleted when it ends** |
| `workspaces/<automation>/user-<id>/uploads/` | Sheets a person has uploaded and not yet started |
| `workspaces/<automation>/user-<id>/resume/` | A copy of the latest result sheet per exact sheet, so a retry skips finished rows |

### 6.3 Settings and secrets

| File | What it holds |
|---|---|
| `automations/<automation>/.env` | The **Office Ally username and password** for that automation. Updated from the dashboard by admins or people an admin allowed; the password is never shown again |
| `backend/.env` | Server settings (section 10), including the first admin's email and password for first start |

Both are excluded from version control and should be readable only by the account that runs the service.

### 6.4 What is deliberately not stored

User passwords in readable form (only a bcrypt hash), session tokens in readable form (only a hash), Office Ally passwords in the database or in
logs, browser screenshots or recordings.

### 6.5 Patient information and retention

Sheets, result sheets, `run_rows` and run logs contain **patient information** (names, dates of birth, insurance IDs, diagnosis codes).
Only the person who started a run, and admins, can see them. They are removed automatically:

- a run's files (sheet copy, outputs, log) are deleted **30 days** after the run finished;
- per-row details (patient name, date of service, IDs) are deleted **180 days** after;
- the run itself (who, when, counts, state) and the audit trail are kept.

Both periods are settings (`0` keeps everything). Engine output in logs can contain patient names; set `AUTOMATION_ENGINE_LOG=off` to store none.
Back up `storage/automation.db`.

---

## 7. Logs and monitoring

| Where | What you see | Who |
|---|---|---|
| Admin, **Overview** | Sign-ins today, failed sign-ins today, people signed in today, active now (last 15 min), open sessions, accounts awaiting approval, a 14-day chart of successful and failed sign-ins (with "View as table"), runs in progress, latest failed sign-ins | Admin |
| Admin, **Sign-ins** | Every sign-in attempt, filterable by person, result and period, with IP and browser | Admin |
| Admin, **Activity** | The full audit trail, filterable by person, action and text search | Admin |
| Admin, **Users** | Each person's status, automations, last sign-in, sign-in count, run count, open sessions, and buttons to manage them | Admin |
| Admin, **Sessions** | Everyone currently signed in, with a button to end a session | Admin |
| **History** | Every run: who, automation, sheet, rows, created, not created, status, start time. Click a run for its rows, counts and log | Users see their own; admins see all |
| **Account** | A person's own activity trail, and password change | Each person |
| Run page, **Log** | The run's own log lines | Run owner and admins |

---

## 8. Security

- **Passwords:** at least 10 characters with upper and lower case letters and a number; stored as bcrypt hashes.
- **Sessions:** a random token in a cookie that scripts cannot read (HttpOnly, SameSite). Only its hash is stored.
  Sessions last 12 hours and extend while the person is active. Admins can end any session. Changing a password signs
  out the person's other devices. Disabling a person signs them out immediately.
- **Lockout:** 5 wrong passwords lock that account for 15 minutes, even if the right password is then used. Too many
  failures from one IP address are also blocked for 15 minutes.
- **No guessing who has an account:** wrong password and unknown email give the identical answer and take similar time.
- **Approval:** a new account can do nothing until an admin approves it and chooses its automations.
- **Isolation:** a person cannot open, download, stop or even see another person's runs, sheets or results. An
  automation they were not given looks like it does not exist.
- **Cross-site protection:** requests from other websites are rejected; the site sends strict content-security headers
  and refuses to be embedded in other pages.
- **Uploads:** `.xlsx` only, maximum 25 MB, checked to be a real spreadsheet, file names cleaned so nothing can be
  written outside the person's own folder.
- **Safe display:** patient names and every other value from a sheet are shown as plain text, so a sheet cannot inject
  code into a page.
- **Office Ally login:** the password can be written but never read back through the dashboard or logs; the audit trail records that it changed, never the value. Only admins and people an admin allowed can change it, and not during a run.
- **Logs:** secrets (passwords, cookies, tokens) are removed from run logs before they are stored.
- **Last admin:** the system refuses to disable or demote the only remaining admin, and admins cannot disable themselves.

Passwords for Office Ally are protected only by file permissions on the server, because the automations need to read
them. Treat the server and the `.env` files accordingly.

---

## 9. Rules and limits

| Rule | Value |
|---|---|
| Runs of the **same automation** at once | 1 (each automation uses one Office Ally login and two windows on one login can hang). Others wait |
| Different automations at once | Yes (Blackhill ACO and Non ACO run side by side) |
| Runs at the same time, whole server | 2 (setting) |
| Browser windows at the same time, whole server | 8 (setting); 4 per run |
| Order when runs are waiting | Fair: the person with the fewest running jobs goes first, then the person who started one least recently, then the oldest. A waiting run never blocks a different automation |
| Active or waiting runs per person | 5 |
| Maximum upload | 25 MB, `.xlsx` only |
| Longest run | 12 hours, then it is stopped and marked failed |
| Automatic retry | Only after a crash, once, after about a minute (waits longer each time); never after a login or input error |
| If the server restarts | Waiting runs resume; a run that was in progress is marked **Interrupted**. Retry it: finished rows are skipped |
| If a run crashes | Only that run is affected. The website and other runs carry on, and its browsers are cleaned up |
| Login update | Not possible while that automation has an active run |

---

## 10. Running it on a server

**Where:** the Office Ally portals do not open from Pakistan (that is why the PC uses a VPN). Put the server in the
**United States** and no VPN is needed. First check by logging in to Office Ally by hand from the server.

**Operating system:** Windows is the closest to the PC the automations were built on (visible Edge window).
Linux works too, with a headless browser, but behaves slightly differently and needs a small real test first.

**Settings file `backend/.env`** (real environment variables override it; the full list with defaults is in `backend/.env.example`):

| Setting | Meaning |
|---|---|
| `AUTOMATION_ADMIN_EMAIL`, `AUTOMATION_ADMIN_PASSWORD` | The first admin, created on first start if no admin exists yet. Remove the password line afterwards |
| `AUTOMATION_COOKIE_SECURE` | `true` once the site is on HTTPS |
| `AUTOMATION_TRUST_PROXY` | `true` when a reverse proxy such as Caddy sits in front |
| `AUTOMATION_ALLOWED_ORIGINS` | Your domain, for example `https://automation.example.com` |
| `AUTOMATION_BROWSER_CHANNEL` | `msedge` (Windows) or `chromium` (Linux) |
| `AUTOMATION_HEADLESS` | `false` on Windows, `true` on Linux |
| `AUTOMATION_MAX_CONCURRENT_RUNS`, `AUTOMATION_MAX_TOTAL_WORKERS`, `AUTOMATION_MAX_WORKERS` | How many runs and browser windows the server may use at once (defaults 2, 8, 4) |
| `AUTOMATION_AUTO_RETRIES`, `AUTOMATION_RETRY_BACKOFF_SECONDS` | Automatic retry after a crash (default 1 retry after 60 s) |
| `AUTOMATION_RETENTION_FILES_DAYS`, `AUTOMATION_RETENTION_ROWS_DAYS` | When old files and patient row details are deleted (30 and 180 days; 0 keeps them) |
| `AUTOMATION_ENGINE_LOG` | `redacted` (default) or `off` to store no engine output |
| `AUTOMATION_SESSION_HOURS` | Sign-in length (default 12) |

**Start:** `start.bat` (Windows) or `./start.sh` (Linux/Mac). The site listens on `127.0.0.1:8000` only; put **Caddy**
in front for HTTPS on your domain (sample `deploy/Caddyfile`, and a Linux service file `deploy/automation.service`).
Step-by-step instructions for Windows and Linux are in `deploy/README.md`.

**Backups:** copy `storage/automation.db` regularly. It is the whole history.

---

## 11. What is not done yet

- It has **not been validated on a real server with real Office Ally logins**. The automated tests use a stand-in automation, so they prove the
  dashboard logic (states, queue, retry, cleanup, isolation, retention) but not that Office Ally works from your server. `deploy/README.md`,
  Part 3, lists the exact checks to run: `python -m app.cli preflight`, `python -m app.cli check-login <automation>`, then a small approved sheet per automation.
- Running **headless** on Linux is unverified against Office Ally.
- Only two of about nineteen automations have been moved.
- No email or message alerts when a run finishes or fails.
- No password reset by email: an admin resets it and gives the new password in person.
- One database file on one server.

---

## 12. Glossary

| Term | Meaning |
|---|---|
| **Office Ally / Practice Mate** | The web portal where visits and claims are created |
| **Visit** | One patient encounter recorded in Practice Mate |
| **DOS** | Date of service: the day the patient was seen |
| **CPT** | The code for the service that was performed |
| **ICD / ICD-10** | Diagnosis codes. HCC codes are a risk-adjustment subset of them |
| **POS** | Place of service code (for example 31 for a skilled nursing facility) |
| **Modifier** | A short code added to a CPT to adjust its meaning |
| **ACO / Non ACO** | Two billing groups with separate Office Ally accounts and sheet layouts |
| **Run** | One pass of an automation over one sheet |
| **Row status** | Done, Duplicate or Not created, per sheet row |
| **Pending** | An account request waiting for admin approval |
| **Session** | A signed-in browser. Ending it signs that browser out |
| **Hash** | A one-way scrambled form of a password or token that can be checked but not reversed |
| **Queue** | Runs waiting for room on the server or for their automation's Office Ally login to be free |
| **Retry** | Running the same sheet again; rows already done are skipped and duplicates are never allowed |
| **Interrupted** | A run that was in progress when the server stopped |
| **Retention** | Automatic deletion of old files and patient row details after a set number of days |
| **Headless** | A browser running without a visible window |

---

## Where things are in the project

```
app/
  OVERVIEW.md      this document
  ARCHITECTURE.md  the technical map (for developers and AI assistants)
  README.md        short technical readme
  backend/         the server (FastAPI + SQLite), the runner, and the tests (38)
  frontend/        the pages (plain HTML, CSS and JavaScript)
  automations/     one folder per automation: its program, manifest.json and its own .env
  deploy/          Caddy sample, Linux service file, server instructions
  storage/         created while running: database, sheets, results, run logs
```
