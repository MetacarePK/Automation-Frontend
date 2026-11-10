# Architecture (read this first if you are an AI or a new developer)

The precise technical map of the project. For a plain-language tour read `OVERVIEW.md`; for server steps `deploy/README.md`.
Everything below describes what exists in the code today; paths are relative to `app/`.

## 1. The core idea in six sentences

1. Billing "automations" (Playwright bots that create visits in Office Ally Practice Mate from an Excel sheet) already exist as
   independent Python programs ("engines") that ran on a Windows PC for months. **They are never modified.**
2. This project is a **multi-user web dashboard** (FastAPI + SQLite + plain JS) that wraps those engines.
3. A user uploads a sheet; the server **queues a run**; a **scheduler** starts it when capacity allows; a **separate OS process
   (the runner)** imports the engine, points its paths at that run's private folder, runs it, and streams per-row progress back.
4. Every run has its own folder, log, state and outputs. A run is only reported **completed after its result sheet was read back
   and matched against the database**. Crashes, cancels, restarts and partial failures end in an accurate state and can be retried safely.
5. The server stores users, sessions, runs, per-row results, run events and an audit trail in SQLite; sheets and result files live on disk
   and are removed by a retention policy.
6. Adding an automation means adding a folder (`engine + manifest.json`); the dashboard discovers it at startup.

## 2. Component map

```
Browser (static HTML / ES-module JS, polling)
   |  JSON over HTTPS, session cookie
   v
FastAPI app  backend/app/main.py        create_app(settings) -> app.state.ctx = Context(settings, db, registry, jobs)
   |-- routers/        auth, account, automations (+ Office Ally login), runs, admin     (HTTP layer)
   |-- deps.py         Context, current-user dependencies, CSRF/origin check
   |-- services/jobs.py        JobManager: scheduler + run supervisor                    <- the heart
   |-- services/procs.py       ProcessTracker: finds and kills every process a run started
   |-- services/verify.py      reads the result sheet back and compares it with the database
   |-- services/redact.py      secret redaction for logs
   |-- services/retention.py   removes old files / row details
   |-- services/credentials.py reads and writes an automation's Office Ally login (.env)
   |-- services/registry.py    loads automations/*/manifest.json
   |-- services/audit.py       activity_logs and login_events
   |-- db.py / security.py / config.py / cli.py
   |
   |  subprocess.Popen (one process per run; stdout = event stream)
   v
backend/runner/runner.py        cwd = engine folder; the ONLY code that imports an engine
   |  patches engine globals + browser launch, calls the engine's run_automation()
   v
automations/<slug>/main.py, patients.py, parallel.py   (unchanged engine)  --> Playwright --> Office Ally
```

| Path | Responsibility |
|---|---|
| `backend/app/main.py` | App factory, security-header + origin middleware, routers, HTML page routes (server-side auth gating), `/static`, `/healthz` |
| `backend/app/config.py` | `Settings` from env vars; `load_env_file()` reads `backend/.env` (used by `get_settings()` only; real env vars win) |
| `backend/app/db.py` | `MIGRATIONS` (append-only), `FK_OFF_MIGRATIONS` (table rebuilds), `init_db`, `Database.session()/transaction()`, `utcnow()` |
| `backend/app/services/jobs.py` | Scheduler, run lifecycle, paths (`run_paths`, `resume_store`, `staging_dir`), `sheet_fingerprint`, `RunLog`, retry |
| `backend/app/cli.py` | `create-admin`, `list-users`, `preflight`, `check-login`; `ensure_bootstrap_admin` at startup |
| `backend/runner/runner.py` | Modes `describe`, `validate`, `run`, `login_check`, `browser_check` |
| `automations/<slug>/` | `manifest.json` + engine + that automation's own `.env` (Office Ally login) |
| `frontend/` | HTML stubs + `static/*.js` (`app.js` shared: `h()`, `api()`, `shell()`, `runPanel()`), one `styles.css` |
| `backend/tests/` | pytest suite with a fake engine (`tests/fake_automations/fake-auto`, copied as `fake-auto-2`) |
| `deploy/` | Caddyfile, hardened systemd unit, `harden.sh`, server + verification instructions |

## 3. Core concepts

| Concept | Definition |
|---|---|
| **Automation** | Folder in `automations/` identified by `slug` (= folder name) |
| **Engine** | The automation's Python code, a black box with a fixed contract (section 4) |
| **Manifest** | Dashboard-facing description: name, accent colour, sheet fields, `required_fields`, `insurance_id_label`, optional `max_concurrent_runs` (default 1) and `credential_fields` |
| **Run** | One execution of one automation on one sheet by one user. Row in `runs` |
| **Run states** | `queued -> validating -> running -> completed \| completed_with_errors \| failed \| interrupted \| cancelled` |
| **Failure kind** | `login_failed`, `invalid_input`, `config_error` (permanent), `crash` (transient, retried once), `timeout`, `engine_error`, `verification_failed`, `interrupted` |
| **Run folder** | `storage/runs/<id>/`: `input/` (private copy of the sheet), `output/`, `auth_state.json` (deleted after the run), `run.log`, `job.json` |
| **Resume store** | `storage/workspaces/<slug>/user-<id>/resume/<sheet fingerprint>/<stem>_with_status.xlsx`: the latest readable status sheet for that exact sheet content; seeded into the next run's output folder so finished rows are skipped |
| **Sheet fingerprint** | Hash of the sheet's cell values + row numbers (not file bytes), so re-saving in Excel keeps progress but a different sheet with the same name never inherits it |
| **Retry chain** | Runs linked by `retry_of` / `attempt` |
| **Carried over** | Rows that were already Done from earlier runs (reported by the runner's `resume` event) |
| **Context** | `Context(settings, db, registry, jobs)` at `app.state.ctx`, reached by `Depends(get_ctx)` |

## 4. The engine contract (what an automation must provide)

**`patients.py`**: `DEFAULT_COLUMN_MAP` (keys `name, dob, provider, dos, cpt, pos, modifier, icd10, facility, beneficiary_id, visit_id,
line_charge, status`), `resolve_column_map(map)`, `read_patients(xlsx, column_map)` (raises `SystemExit(message)` on a bad sheet).

**`main.py`**
- Module globals the runner **overwrites per run**: `DATA_DIR`, `OUTPUT_DIR`, `STATE_PATH`. The engine reads them at call time.
- `run_automation(excel_path, column_map, workers, limit, only, no_resume, make_visit, save_visit, allow_duplicate, no_wait, interactive,
  progress_hook, retries)` -> dict with `logged_in`, `done`, `total`, `captured_columns`, `status_xlsx_path`.
- `progress_hook(group_status, group_captured, result)` after each visit, possibly from worker threads.
- `status_xlsx_path_for(excel)`, `read_previous_status(status_path, column_map)` (the resume mechanism: row -> Status, `Done...` rows are skipped).
- `log_in(browser)` -> `(context, page, logged_in)` (used by the `login_check` mode).
- Credentials come from `.env` in the engine's own folder (`username`/`user`, `password`/`pass`; `key=value` or `key: value`).
- The engine writes a **Status column** into a copy of the sheet after every row (`Done...` / `Not Created`); row numbers = Excel rows.

Facts that shape the design: both engines name their modules `main`/`patients`/`parallel` (cannot share a process); `run_automation()`
replaces `sys.stdout` process-wide; they launch Edge visibly (`channel="msedge"`, `headless=False`); ACO logs in fresh every run, Non ACO
reuses a saved session; each automation uses one Office Ally account and two windows on one login can hang.

## 5. The runner protocol

`python backend/runner/runner.py <job.json>`; `job.json` has `mode` (and `engine_dir` except for `browser_check`). Events are stdout lines
`@@EVENT {json}` written to `sys.__stdout__`; everything else is engine output.

| Mode | Output events |
|---|---|
| `describe` | `{"type":"describe","default_map":{...}}` |
| `validate` | `{"type":"validated","ok":true,"total":N}` or `ok:false,error` (uses the engine's own reader) |
| `run` | `{"type":"resume","carried":N}`, many `{"type":"rows","rows":[...]}`, then `{"type":"finished","ok","done","total","status_xlsx","status_column","summary"\|"error"}` |
| `login_check` | `{"type":"login_check","ok":bool,"url":...}`: a REAL sign-in, deletes its session file, changes nothing in Office Ally |
| `browser_check` | `{"type":"browser_check","ok":true,"version":...}`: starts the configured browser on `about:blank` |

`run` patches `main.DATA_DIR/OUTPUT_DIR/STATE_PATH` to the run folder and wraps `playwright BrowserType.launch` to force `channel` and `headless`.
Exit code 0 only when `finished.ok`. stdin is closed so a stray `input()` fails instead of hanging.

## 6. Scheduling and the life of a run (`services/jobs.py`)

**Submit** (`POST /automations/{slug}/run`): check access + required columns; resolve the staged upload inside the user's folder; refuse a
second open run of the same sheet (409) and more than `max_active_per_user`; validate with the runner; insert the `runs` row with
`not_before = HOLD` (invisible to the scheduler), copy the sheet into `runs/<id>/input/`, store the content fingerprint, then `submit()` clears HOLD.

**Scheduler** (one thread; `_pick_next`): a queued run may start when
1. `len(active) < max_concurrent_runs` (global cap);
2. its automation has fewer active runs than the manifest's `max_concurrent_runs` (default 1, the Office Ally account lock);
3. active browser workers + its workers <= `max_total_workers` (a run alone may exceed it);
4. `not_before` has passed (retry backoff).

A run that cannot start never blocks another automation. Among startable runs the order is **fair**: fewest active runs for that person,
then least recently started person, then oldest run.

**Execute** (one thread per run): atomically `queued -> validating` (a cancelled run is skipped), preflight (sheet exists, the automation has
a login saved, the engine's reader accepts the sheet), create the output folder and seed it from the resume store, `-> running`, start the
runner in its own process group, track every descendant process (`ProcessTracker` snapshots the tree every 2 s), read events (`rows` are
upserted into `run_rows`; `resume` sets `carried_over`), write a redacted timestamped `run.log`.

**Outcome** (after the process ends, then `ProcessTracker.kill_all()` so no browser can outlive the run):

| Situation | Final state |
|---|---|
| cancel requested | `cancelled` |
| watchdog (`run_timeout_hours`) | `failed` / `timeout` |
| server stopping | `interrupted` |
| runner reported `ok` and exit 0 | **verify** (below), then `completed` or `completed_with_errors` |
| runner reported an error | `failed` with kind `login_failed` / `invalid_input` / `engine_error` |
| process died without a verdict | `failed` / `crash` -> one automatic retry with backoff |

**Verification** (`services/verify.py`): the status sheet must open, contain the engine's status column, agree with every row the runner
reported, and its Done count must equal the engine's `done`. Otherwise the run is `failed` / `verification_failed`. `completed_with_errors` means
any `Not Created` row, or (when no limit/filter was used) rows that reported no result.

**After every outcome:** the latest readable status sheet is copied into the resume store (those rows are really saved in Office Ally and a retry
must not redo them); `auth_state.json` is deleted; `runs`/`run_events`/`activity_logs` are updated.

**Retry** (`create_retry`): only for `failed`, `interrupted`, `completed_with_errors`, `cancelled`; refused if the files were purged or the sheet is already open.
Always forces `allow_duplicate=False` and `no_resume=False`; copies the sheet; new run links via `retry_of`, `attempt+1`. **Automatic** retry happens only for
`crash`, at most `auto_retries` times, after `retry_backoff_seconds * 2^(attempt-1)`; never for login/input/config errors.

**Cancel** (`cancel`): queued -> `cancelled` at once; validating/running -> flag + `ProcessTracker.kill_all()` (terminate, grace, kill; verifies survivors).

**Restart** (`_recover`): `validating`/`running` -> `interrupted` (+ event); runs still being prepared (HOLD) -> `failed`; `queued` runs simply continue.

## 7. HTTP surface

JSON under `/api`; state-changing requests pass the origin check; errors are `{"detail": "..."}`. A resource the caller may not see returns **404**.

| Area | Endpoints |
|---|---|
| Auth | `POST /api/auth/signup`, `login`, `logout`; `GET /api/auth/me` (includes `credential_automations`) |
| Account | `GET /api/account/activity`, `POST /api/account/password` |
| Automations | `GET /api/automations` (cards: `running_now`, `queued`, `last_run`, `active_run`); `GET /{slug}`; `POST /{slug}/upload`; `POST /{slug}/run` |
| Office Ally login | `GET/PUT /api/automations/{slug}/credentials`, `POST .../credentials/test` (admin or granted person; PUT/test refused with 409 while a run is active) |
| Runs | `GET /api/runs`; `GET /{id}?rows=&view=all\|failed\|skipped\|created` (counts incl. `pending`, `carried`, `chain`, `config`, `can_retry`, `can_cancel`); `GET /{id}/events`; `GET /{id}/log`; `GET /{id}/files`; `GET /{id}/download?file=status\|results\|visits\|failed\|log`; `POST /{id}/cancel`; `POST /{id}/retry` |
| Admin | `GET /api/admin/overview`, `users`, `logins`, `activity`, `sessions`; `POST /users/{id}/approve\|disable\|enable\|reset-password\|unlock`; `PUT /users/{id}/access\|role`; `POST /sessions/{id}/revoke` |
| Pages | `/`, `/login`, `/signup`, `/runs`, `/account`, `/admin`, `/a/{slug}`, `/static/*`, `/healthz` |

Downloads only accept a fixed set of file kinds; paths are derived on the server (never from client input). CSV cells that start like formulas are neutralised.

## 8. Data model (SQLite `storage/automation.db`, WAL, foreign keys on; UTC ISO-8601 text timestamps)

| Table | Notes |
|---|---|
| `users`, `sessions`, `login_events`, `activity_logs` | accounts (bcrypt), hashed session tokens, every sign-in attempt, audit trail |
| `user_automations` | PK (`user_id`,`automation_slug`), `can_manage_credentials` (admins bypass) |
| `runs` | `status` (8 states, CHECK), `failure_kind`, `sheet_path` (the run's private copy), `options` JSON (incl. `sheet_sha256` = content fingerprint), `total`, `carried_over`, `retry_of`, `attempt`, `not_before`, `output_path`, `files_purged_at`, `rows_purged_at`, `cancel_requested`, times |
| `run_rows` | PK (`run_id`,`row_no`); latest state per sheet row for that run |
| `run_events` | per-run timeline: `ts`, `level`, `kind`, `message`, `details` JSON (exit code, last redacted output lines) |
| `column_mappings` | remembered column match per sheet layout and automation |
| `schema_version` | integer driving migrations |

Counts (`routers/runs.py` `COUNT_SQL`): *successful* = `Done%` without `duplicate_of`; *skipped* = `duplicate_of <> ''`; *failed* = `Not Created`;
*pending* = `total - seen - carried_over`.

**Migrations are append-only.** 1 = initial schema (edited once before any deployment; never again). 2 = rebuild `runs` with the new states/columns and add
`run_events` (listed in `FK_OFF_MIGRATIONS` so the rebuild cannot cascade-delete `run_rows`). 3 = `user_automations.can_manage_credentials`.

## 9. Filesystem layout at runtime

```
storage/automation.db (+ -wal, -shm)
storage/runs/<run id>/input/<sheet>.xlsx          private copy used by that run
storage/runs/<run id>/output/                     engine outputs: <sheet>_with_status.xlsx, _results.csv, _visits.csv, logs.txt
storage/runs/<run id>/run.log                     structured, timestamped, redacted
storage/runs/<run id>/job.json                    runner instructions (no secrets)
storage/runs/<run id>/auth_state.json             saved Office Ally session; deleted when the run ends
storage/workspaces/<slug>/user-<id>/uploads/      staged uploads waiting to become a run
storage/workspaces/<slug>/user-<id>/resume/<fp>/  latest status sheet per sheet content
automations/<slug>/.env                           Office Ally login (written by services/credentials.py, mode 600)
backend/.env                                      server settings + first-admin bootstrap
```

## 10. Configuration (env vars, optionally in `backend/.env`; see `backend/.env.example`)

Paths and web: `AUTOMATION_STORAGE_DIR`, `_AUTOMATIONS_DIR`, `_FRONTEND_DIR`, `_COOKIE_SECURE`, `_SESSION_HOURS`, `_ALLOWED_ORIGINS`, `_TRUST_PROXY`,
`_MAX_FAILED_LOGINS`, `_LOCKOUT_MINUTES`, `_AUTO_APPROVE`. Capacity: `_MAX_CONCURRENT_RUNS` (2), `_MAX_TOTAL_WORKERS` (8), `_MAX_WORKERS` (4),
`_MAX_ACTIVE_PER_USER` (5), `_RUN_TIMEOUT_HOURS` (12), `_MAX_UPLOAD_MB` (25). Retries: `_AUTO_RETRIES` (1), `_RETRY_BACKOFF_SECONDS` (60).
Logs/retention: `_ENGINE_LOG` (`redacted`|`off`), `_LOG_MAX_MB` (20), `_RETENTION_FILES_DAYS` (30), `_RETENTION_ROWS_DAYS` (180); 0 keeps forever.
Browser: `_BROWSER_CHANNEL` (msedge), `_HEADLESS` (false), `_PYTHON`. First admin: `_ADMIN_EMAIL`, `_ADMIN_PASSWORD` (operator's password is exempt from the policy).

Start: `cd backend && python -m uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000` (or `start.sh` / `start.bat`).
Check a server: `python -m app.cli preflight`; real sign-in test: `python -m app.cli check-login <slug>`.

## 11. Frontend conventions

Plain ES modules, no build. **No `innerHTML` with data and no inline scripts/styles** (CSP `default-src 'self'`); values go through `textContent`; dynamic
sizes use `el.style.x = ...`. One page = one HTML stub + one JS file. Light theme only, flat surfaces, system fonts, one shared side gutter for top bar and page,
no decorative dots or icons. The run panel polls `GET /api/runs/{id}` every 2 s while a run is open, so a refresh or reconnect always shows database state.

## 12. Invariants (do not break these)

1. Never edit engine files for dashboard reasons; adapt in `runner.py` or `manifest.json`.
2. Never import an engine into the web server process; only `runner.py` does.
3. Runs of one automation never overlap unless its manifest allows it (`max_concurrent_runs`, default 1) after the session safety was verified.
4. Every run uses only its own folder; shared mutable state is limited to the resume store (keyed by content) and the database.
5. A run is `completed` only after `verify_output` passes. A retry never allows duplicates and always resumes.
6. A user must never reach another user's run, sheet, result, log or events: use `_get_run` / `allowed_automation` and return 404; derive file paths on the server.
7. No readable user passwords or session tokens; the Office Ally password is write-only (never returned, logged or audited) and lives only in the engine's `.env`.
8. Logs pass through `redact`; engine output can be switched off.
9. Every security-relevant action writes `activity_logs`; sign-ins write `login_events`; run history goes to `run_events`.
10. Append-only migrations; table rebuilds go in `FK_OFF_MIGRATIONS`.
11. Keep at least one active admin.

## 13. How to extend

**Add an automation:** folder `automations/<slug>/` with the engine (section 4), its `.env`, and `manifest.json` (`slug`, `name`, `description`, `accent`,
`insurance_id_label`, `required_fields`, `input_fields`/`output_fields` as `[key, label, required]`; optional `max_concurrent_runs`, `credential_fields`).
Restart; grant access in Admin > Users. Run `preflight` and `check-login` before a real run. **Add an endpoint:** router + `require_user`/`require_admin` + audit row + test.
**Change the schema:** append to `MIGRATIONS`. **Add a page:** HTML stub + JS file + `PAGES` entry in `main.py`.

## 14. Testing

`cd backend && python -m pytest tests -q` (about two minutes; tests spawn real runner subprocesses). The fake engine implements the real contract and reacts to
patient-name tokens: `SLOW` sleeps, `CRASH` raises, `DUP` duplicate, `MISS` Not Created, `NOLOGIN` login fails, `KILLME` dies without a verdict once,
`SPAWN` starts a child process (stands in for a browser), `LEAK` prints secrets, `BADOUT` writes a mismatching result sheet.

Covered: auth/lockout/CSRF/sessions, approval and roles, user isolation (runs, files, events, retry, cancel), the state machine, output verification, failure
classification, automatic and manual retry (resume, no duplicates), the fair queue, global run and worker caps, one-run-per-automation, concurrent automations,
crash isolation, process cleanup on cancel, restart recovery, per-run folders and deleted sessions, content-keyed resume, structured and redacted logs,
file listing / failed-record CSV / whitelisted downloads, retention, Office Ally login updates (permissions, write-only password, block during runs, audit without values),
migration 2 on a version-1 database, process tracking, redaction, fingerprinting. **Browser checks** drive the real UI with Playwright against a throwaway server
using the fake engine (never the real engines).

**What the automated tests cannot prove:** Office Ally login and processing from the target server, headless behaviour on Linux, and real browser
memory use. Those are the manual checks in `deploy/README.md` Part 3.

## 15. Known gaps and limits

- Not yet validated on a real server or against real Office Ally (Part 3 of the deploy guide).
- Headless Linux is unverified (the engines use `no_viewport=True`, so window size may differ).
- Engine output can contain patient names (it is redacted for secrets only); set `AUTOMATION_ENGINE_LOG=off` to store none. It is visible only to the run's owner and admins and is purged with the run's files.
- If the whole server is killed without a clean stop, orphaned browsers are ended by systemd's control-group stop (not by the dashboard itself).
- Non ACO no longer reuses a saved Office Ally session between runs (each run signs in fresh in its own folder, as ACO always did).
- Changing a sheet's content starts it from scratch (by design); the engine's own duplicate check against Office Ally still prevents double visits.
- No notifications; no email password reset; one server, one database file.
