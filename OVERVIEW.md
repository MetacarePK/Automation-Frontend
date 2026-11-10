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
| **User** | Sign in, see only the automations the admin gave them, upload sheets, start and stop their own runs, see their own history and activity, change their password |
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
3. **Columns.** The dashboard matches the sheet's column names to what the automation needs (Patient, Birthdate, CPT,
   and so on). If the layout was used before, the match is remembered. When all required columns are matched it
   shows "Columns matched" and stays out of the way; otherwise it opens so the person can pick the missing ones.
4. **Options.** "Save to Office Ally" (on by default) and "Windows" (how many browser windows work at once, default 4,
   maximum 4). Under "More options": only the first N rows, only names containing some text, allow duplicates,
   start over and ignore earlier progress.
5. **Start run.** A confirmation asks if visits really should be created. The dashboard first reads the sheet with the
   automation's own reader to check it is valid, then queues the run.
6. **Queue.** Only one run per automation goes at a time, because each automation uses one Office Ally login and two
   windows on one login can hang. If another run is going, the new one shows "Waiting" and starts by itself.
   ACO and Non ACO can run at the same time.
7. **Progress.** The page updates every two seconds: a colored bar, counts (Created, Duplicate, Not created, Rows), and
   a table of the latest rows. "All rows" shows everything; "Log" shows the run's log.
8. **Stop.** **Stop** is on the run page and on the dashboard card. It stops the run and closes its browsers. Visits
   already saved stay saved.
9. **Finish and download.** When the run ends, **Download sheet** gives a copy of the sheet with the results filled in.

Closing the browser tab does not affect a run. It lives on the server. Reopening the card shows it again.

### If the same sheet is run again

The automation remembers progress in its result file. Rows already **Done** are skipped, so a run that was stopped or
failed halfway continues where it left off. "Start over" turns that off.

### Row results

| Result | Meaning |
|---|---|
| **Done / Created** | The visit was created and saved |
| **Duplicate** | A visit for that patient already existed, so nothing new was created. The existing visit's ID is recorded |
| **Not created** | Patient not found, bad data, or Office Ally problem. The reason is shown in Note |

Rows that end as Not created are retried once automatically at the end of the run, one window at a time, because
many failures are temporary (slow popup, dropped connection).

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
| `users` | Name, email, password **hash** (never the password), role (admin/user), status (pending/active/disabled), failed-sign-in count and lock time, created/approved dates, last sign-in |
| `sessions` | One row per signed-in browser: a **hash** of the session token, who, when it started, last seen, when it expires, IP address, browser name, whether it was ended |
| `user_automations` | Which person may use which automation, who granted it and when |
| `login_events` | Every sign-in attempt, successful or not: email, result, reason (wrong password, unknown email, pending, disabled, locked), IP, browser, time |
| `activity_logs` | The audit trail: who did what, to what, from which IP, when (sign-ups, approvals, role and access changes, uploads, runs started/finished/stopped, downloads, password changes) |
| `runs` | One row per run: automation, who started it, status, sheet name and location, the options chosen (column match, save on/off, windows...), total rows, summary or error, times, result file location |
| `run_rows` | One row per sheet row per run: patient name, date of service, status, Visit ID, insurance ID, duplicate marker, error, line charges, note |
| `column_mappings` | The remembered column match for each sheet layout, per automation |
| `schema_version` | Which database version is installed, so updates upgrade it safely |

### 6.2 Files on disk (`storage/`)

| Location | What it holds |
|---|---|
| `workspaces/<automation>/user-<id>/data/` | Sheets that person uploaded, exactly as uploaded |
| `workspaces/<automation>/user-<id>/output/` | The automation's result files: the sheet with Status/Visit ID/insurance ID/charges filled in, plus results and visits CSV files and the engine's own log |
| `workspaces/<automation>/auth_state.json` | The saved Office Ally browser session (cookies) for that automation |
| `runs/<run id>/run.log` | The log of that run, shown under "Log" |
| `runs/<run id>/job.json` | The instructions given to the runner for that run (paths, column match, options). No passwords |

Each person's sheets and results are in their own folder. Other users cannot open them through the site.

### 6.3 Settings and secrets (outside `storage/`)

| File | What it holds |
|---|---|
| `automations/<automation>/.env` | The **Office Ally username and password** for that automation. Shared by everyone who runs it; they are never shown on screen |
| `backend/.env` | Server settings (see section 10), including the first admin's email and password for first start |

Both are excluded from version control (`.gitignore`) and should be readable only by the account that runs the service.

### 6.4 What is deliberately not stored

- User passwords in readable form (only a bcrypt hash).
- Session tokens in readable form (only a hash, so a copy of the database cannot be used to sign in).
- Office Ally passwords in the database.
- Browser screenshots or recordings.

### 6.5 Be aware

- Uploaded sheets, result sheets, `run_rows`, and run logs contain **patient information** (names, dates of birth,
  insurance IDs, diagnosis codes). Protect `storage/` accordingly and back up `storage/automation.db`.
- **Nothing is deleted automatically.** Sheets, results and logs stay until someone removes them.

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
- **Last admin:** the system refuses to disable or demote the only remaining admin, and admins cannot disable themselves.

Passwords for Office Ally are protected only by file permissions on the server, because the automations need to read
them. Treat the server and the `.env` files accordingly.

---

## 9. Rules and limits

| Rule | Value |
|---|---|
| Runs per automation at a time | 1 (others wait in a queue and start automatically) |
| ACO and Non ACO at the same time | Yes |
| Browser windows per run | Up to 4 (server setting) |
| Active or waiting runs per person | 5 |
| Maximum upload | 25 MB, `.xlsx` only |
| Longest run | 12 hours, then it is stopped and marked failed |
| Session length | 12 hours, extended while in use |
| Account lock | 5 wrong passwords, 15 minutes |
| If the server restarts | Waiting runs resume; a run that was in progress is marked failed ("server restarted"). Run the same sheet again and finished rows are skipped |
| If a run crashes | Only that run fails. The website and other runs are not affected |

---

## 10. Running it on a server

**Where:** the Office Ally portals do not open from Pakistan (that is why the PC uses a VPN). Put the server in the
**United States** and no VPN is needed. First check by logging in to Office Ally by hand from the server.

**Operating system:** Windows is the closest to the PC the automations were built on (visible Edge window).
Linux works too, with a headless browser, but behaves slightly differently and needs a small real test first.

**Settings file `backend/.env`** (real environment variables override it):

| Setting | Meaning |
|---|---|
| `AUTOMATION_ADMIN_EMAIL`, `AUTOMATION_ADMIN_PASSWORD` | The first admin, created on first start if no admin exists yet. Remove the password line afterwards |
| `AUTOMATION_COOKIE_SECURE` | `true` once the site is on HTTPS |
| `AUTOMATION_TRUST_PROXY` | `true` when a reverse proxy such as Caddy sits in front |
| `AUTOMATION_ALLOWED_ORIGINS` | Your domain, for example `https://automation.example.com` |
| `AUTOMATION_BROWSER_CHANNEL` | `msedge` (Windows) or `chromium` (Linux) |
| `AUTOMATION_HEADLESS` | `false` on Windows, `true` on Linux |
| `AUTOMATION_MAX_WORKERS` | Most browser windows per run (default 4) |
| `AUTOMATION_SESSION_HOURS` | Sign-in length (default 12) |

**Start:** `start.bat` (Windows) or `./start.sh` (Linux/Mac). The site listens on `127.0.0.1:8000` only; put **Caddy**
in front for HTTPS on your domain (sample `deploy/Caddyfile`, and a Linux service file `deploy/automation.service`).
Step-by-step instructions for Windows and Linux are in `deploy/README.md`.

**Backups:** copy `storage/automation.db` regularly. It is the whole history.

---

## 11. What is not done yet

- It has **not been deployed** to a real server, and a real Office Ally run through the dashboard has **not been
  tested** yet. Everything was tested with a stand-in automation and by checking that the real automations load and
  read sheets correctly.
- Running **headless** (no visible browser) on Linux is untested against Office Ally.
- Only two of about nineteen automations have been moved. The rest stay as they are.
- No email or message alerts when a run finishes or fails.
- No password reset by email: an admin resets it and gives the new password in person.
- No automatic cleanup of old sheets, results, or logs (see section 6.5).
- Office Ally logins are one per automation, not one per person.
- One database file on one server; there is no multi-server setup.

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
| **Queue** | Runs of one automation waiting for the current one to finish |
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
