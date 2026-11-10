# Architecture (read this first if you are an AI or a new developer)

This file is the precise technical map of the project. For a plain-language tour read `OVERVIEW.md`.
Everything below describes what exists in the code today; paths are relative to `app/`.

## 1. The core idea in five sentences

1. Billing "automations" (Playwright bots that create visits in Office Ally Practice Mate from an Excel sheet) already exist
   as independent Python programs ("engines") that have run on a Windows PC for months. **They are never modified.**
2. This project is a **multi-user web dashboard** (FastAPI + SQLite + plain JS) that wraps those engines.
3. A user uploads a sheet, the server **queues a run**, and a **separate OS process (the runner)** imports the engine,
   points its paths at that user's workspace, runs it, and streams per-row progress back as text lines.
4. The server stores users, sessions, runs, per-row results and an audit trail in SQLite; sheets and result files live on disk.
5. Adding an automation means adding a folder (`engine + manifest.json`); the dashboard discovers it at startup.

## 2. Component map

```
Browser (static HTML/ES-module JS, polling)
   |  JSON over HTTPS, session cookie
   v
FastAPI app  backend/app/main.py  (create_app factory; app.state.ctx = Context)
   |-- routers/        auth, account, automations, runs, admin      (HTTP layer, thin)
   |-- deps.py         Context, current-user dependencies, CSRF/origin check
   |-- services/jobs.py      JobManager: queue + supervises runner processes   <- the heart
   |-- services/registry.py  loads automations/*/manifest.json
   |-- services/audit.py     writes activity_logs and login_events
   |-- db.py / security.py / config.py / cli.py
   |
   |  subprocess.Popen  (one process per run, stdout = event stream)
   v
backend/runner/runner.py  (cwd = engine folder; imports the engine as `main`, `patients`, `parallel`)
   |  patches engine globals, launches the engine's run_automation()
   v
automations/<slug>/main.py, patients.py, parallel.py  (unchanged engine)  --> Playwright --> Office Ally
```

| Path | Responsibility |
|---|---|
| `backend/app/main.py` | `create_app(settings)` factory. Security-header + origin (CSRF) middleware, router registration, HTML page routes with server-side auth gating, `/static`, `/healthz`, `/favicon.ico` |
| `backend/app/config.py` | `Settings` dataclass read from env vars. `load_env_file()` reads `backend/.env` (called by `get_settings()` only; real env vars win) |
| `backend/app/db.py` | `MIGRATIONS` (append-only SQL list), `init_db`, `connect`, `Database.session()` / `.transaction()`, `utcnow()` |
| `backend/app/deps.py` | `Context` dataclass, `load_session_user`, `optional_user` / `require_user` / `require_admin`, `check_origin` |
| `backend/app/security.py` | bcrypt hashing, password policy, session-token generation and hashing, email validation |
| `backend/app/cli.py` | `create_admin`, `ensure_bootstrap_admin` (from `.env` at startup), `python -m app.cli create-admin|list-users` |
| `backend/app/routers/*.py` | HTTP endpoints (section 6). Business rules live here and in `jobs.py` |
| `backend/app/services/jobs.py` | `JobManager`: per-automation worker threads, run lifecycle, process supervision, one-shot `describe`/`validate` calls |
| `backend/app/services/registry.py` | Manifest loading and validation, `Automation` dataclass |
| `backend/app/services/audit.py` | `log_activity`, `log_login`, `client_ip` |
| `backend/runner/runner.py` | Adapter executed once per job/inspection. The ONLY code that imports an engine |
| `automations/<slug>/` | `manifest.json` + engine files + that automation's own `.env` (Office Ally credentials) |
| `frontend/*.html`, `frontend/static/*.js` | Pages and scripts (no build step); `app.js` is the shared library, `styles.css` the only stylesheet |
| `storage/` | Runtime data (gitignored): DB, workspaces, run logs |
| `deploy/` | Caddyfile, systemd unit, server instructions |
| `backend/tests/` | 38 pytest tests with a fake engine in `tests/fake_automations/fake-auto/` |

## 3. Core concepts (the vocabulary used in code)

| Concept | Definition |
|---|---|
| **Automation** | A folder in `automations/` with `manifest.json`, `main.py`, `patients.py` (and usually `parallel.py`). Identified by `slug` (equals the folder name) |
| **Engine** | The automation's Python code. Treated as a black box with a fixed contract (section 4) |
| **Manifest** | Dashboard-facing description: name, description, accent color, sheet field lists, required fields, insurance-ID label |
| **Run** | One execution of one automation on one sheet by one user. Row in `runs`, status `queued -> running -> succeeded/failed/cancelled` |
| **Runner** | `runner.py` process that executes a run (or a quick `describe`/`validate`) against an engine |
| **Workspace** | Per user, per automation folder holding that user's uploaded sheets and engine outputs |
| **Column map** | Dict `{field_key: sheet_header}` chosen by the user; remembered per sheet layout in `column_mappings` |
| **Context** | `Context(settings, db, registry, jobs)` stored at `app.state.ctx`; reached in routes via `Depends(get_ctx)` |
| **Status** | Row result string from the engine: `Done...`, `Not Created`; "duplicate" = `Done` with `duplicate_of` set |

## 4. The engine contract (what an automation must provide)

`runner.py` relies on exactly this; the two Blackhill engines satisfy it unchanged.

**`patients.py`**
- `DEFAULT_COLUMN_MAP: dict` mapping field keys to default sheet headers. Keys used: `name, dob, provider, dos, cpt, pos,
  modifier, icd10, facility, beneficiary_id, visit_id, line_charge, status`.
- `resolve_column_map(column_map) -> dict` (defaults overlaid with the caller's overrides).
- `read_patients(xlsx_path, column_map) -> list[dict]`; on a bad sheet it **raises `SystemExit(message)`** (the runner converts
  that to a clean error).

**`main.py`**
- Module globals the runner **overwrites before the run**: `DATA_DIR`, `OUTPUT_DIR`, `STATE_PATH` (saved Office Ally login).
  The engine reads these globals at call time, which is why patching them works. It derives result files from `OUTPUT_DIR`
  (`<sheet>_with_status.xlsx`, `_results.csv`, `_visits.csv`, `logs.txt`) and resumes from the status file.
- `run_automation(excel_path, column_map, workers, limit, only, no_resume, make_visit, save_visit, allow_duplicate,
  no_wait, interactive, progress_hook, retries)` returning a dict with `logged_in`, `done`, `total`, `captured_columns`,
  `status_xlsx_path`, ...
- `progress_hook(group_status, group_captured, result)` is called after each visit, possibly from worker **threads**.
- Credentials are read from the engine's own `.env` (`ENV_PATH` in its folder): `user:`/`password:` or `key=value`.

**Engine facts that shape the design**
- Both engines name their modules `main`, `patients`, `parallel` -> they cannot coexist in one Python process.
- `run_automation()` replaces `sys.stdout`/`sys.stderr` process-wide while running.
- They launch Playwright with `channel="msedge"`, `headless=False`, `--start-maximized` (visible window).
- Blackhill ACO deletes `auth_state.json` and logs in fresh every run; Non ACO reuses it. Each automation logs in to its own
  Office Ally account; two parallel windows on one login can hang -> **one run at a time per automation**.

## 5. The runner protocol

Command: `python backend/runner/runner.py <job.json>` with `cwd = engine folder`. `job.json` always has `mode` and `engine_dir`.

| Mode | Input | Output event (stdout line `@@EVENT {json}`) |
|---|---|---|
| `describe` | - | `{"type":"describe","default_map":{...}}` |
| `validate` | `excel_path`, `column_map` | `{"type":"validated","ok":true,"total":N}` or `{"ok":false,"error":"..."}` |
| `run` | `excel_path`, `column_map`, `save`, `workers`, `limit`, `only`, `allow_duplicate`, `no_resume`, `workspace{data_dir,output_dir,state_path}`, `browser_channel`, `browser_headless` | many `{"type":"rows","rows":[{row,patient_name,dos,status,visit_id,beneficiary_id,duplicate_of,error,line_charges,note}]}` then one `{"type":"finished","ok":bool,"error"?,"summary"?,"done","total","status_xlsx"}` |

- Events are written to `sys.__stdout__` (the real pipe) because the engine replaces `sys.stdout`. All other stdout/stderr
  lines are the engine's normal output; the server appends them to `storage/runs/<id>/run.log`.
- `run` mode patches `main.DATA_DIR/OUTPUT_DIR/STATE_PATH` to the user's workspace and monkey-patches
  `playwright.sync_api.BrowserType.launch` to force `channel` (`chromium` removes it) and `headless` from settings.
- Exit code 0 only when `finished.ok` is true. stdin is `/dev/null`, so any stray `input()` fails instead of hanging.

## 6. HTTP surface

All JSON under `/api`; all state-changing requests pass the origin check; errors are `{"detail": "..."}`.

| Area | Endpoints |
|---|---|
| Auth | `POST /api/auth/signup` `login` `logout`, `GET /api/auth/me` |
| Account | `GET /api/account/activity`, `POST /api/account/password` |
| Automations | `GET /api/automations` (cards incl. `running_now`, `queued`, `last_run`, `active_run`), `GET /api/automations/{slug}` (manifest + `max_workers`), `POST /{slug}/upload` (multipart), `POST /{slug}/run` |
| Runs | `GET /api/runs` (filters: automation, status, user_id[admin], limit, offset), `GET /{id}?rows=N` (header + counts + latest rows), `GET /{id}/log`, `GET /{id}/download`, `POST /{id}/cancel` |
| Admin | `GET /api/admin/overview`, `users`, `logins`, `activity`, `sessions`; `POST /users/{id}/approve|disable|enable|reset-password|unlock`; `PUT /users/{id}/access|role`; `POST /sessions/{id}/revoke` |
| Pages | `GET /` `/login` `/signup` `/runs` `/account` `/admin` `/a/{slug}` (HTML; server redirects by auth/role), `/static/*`, `/healthz` |

Access rules: `require_user` for everything except signup/login/pages; `require_admin` for `/api/admin/*`. A resource the caller
may not see returns **404** (not 403) so existence is not leaked. Admins may see/stop any run; users only their own.

## 7. Data model (SQLite, `storage/automation.db`, WAL, foreign keys on)

Timestamps are UTC ISO-8601 text (`2026-11-10T14:03:22Z`) so string comparison equals time comparison.

| Table | Key columns / notes |
|---|---|
| `users` | `email` (unique, NOCASE), `password_hash` (bcrypt), `role` admin/user, `status` pending/active/disabled, `failed_attempts`, `locked_until`, `approved_by`, `last_login_at` |
| `sessions` | `id` = sha256(cookie token), `user_id`, `expires_at` (sliding), `last_seen_at`, `ip`, `user_agent`, `revoked_at` |
| `user_automations` | PK (`user_id`, `automation_slug`); admins bypass this table |
| `login_events` | every attempt: `email`, `success`, `reason` (bad_password, unknown_email, pending, disabled, locked, ip_blocked), `ip`, `user_agent` |
| `activity_logs` | `user_id`, `actor_email`, `action`, `target_type`, `target_id`, `details` (JSON), `ip` |
| `runs` | `automation_slug`, `user_id`, `status`, `sheet_name`, `sheet_path`, `options` (JSON), `total`, `summary`, `error`, `queued_at/started_at/finished_at`, `output_path`, `cancel_requested` |
| `run_rows` | PK (`run_id`,`row_no`); latest state per sheet row (upserted by the job manager) |
| `column_mappings` | PK (`automation_slug`,`fingerprint`); fingerprint = sorted sheet headers joined by `|` |
| `schema_version` | integer; drives migrations |

Derived counts (in `routers/runs.py` `COUNT_SQL`): *created* = `Done%` and no `duplicate_of`; *duplicate* = `duplicate_of <> ''`;
*not_created* = `status = 'Not Created'`.

**Migrations:** `MIGRATIONS` in `db.py` is a list of SQL scripts applied in order and recorded in `schema_version`.
Migration 1 was edited in place before any deployment; **from now on only append new entries, never edit an applied one.**

## 8. Key flows

**Sign up and approval.** `signup` -> row in `users` with status `pending` (identical response for existing emails) ->
admin `approve` sets `active`, writes `user_automations` -> `login` verifies bcrypt (always runs a hash check), enforces lockout
(5 failures/15 min, plus a per-IP cap), creates a `sessions` row (only the token's hash stored), sets an HttpOnly SameSite cookie,
writes `login_events` + `activity_logs`.

**A run.**
1. `POST /upload`: save to `storage/workspaces/<slug>/user-<id>/data/<safe name>`, read headers with openpyxl, ask the runner for
   `describe` (cached) and suggest a column map (remembered mapping first, then exact default-name matches).
2. `POST /run`: check access and required fields, resolve the filename *inside the user's data dir* (never trust a client path),
   enforce per-user cap (5 active), run the runner's `validate` (the engine's own reader), save the mapping, insert `runs`
   (status `queued`), call `jobs.submit`.
3. `JobManager` has **one worker thread per automation slug**; it picks the oldest `queued` run, atomically flips it to
   `running`, writes `storage/runs/<id>/job.json`, starts the runner (new process group), and reads stdout line by line:
   `rows` events are upserted into `run_rows`, `finished` is remembered, other lines go to `run.log`. A watchdog kills the run after
   12 hours.
4. On process exit the status becomes `succeeded` (finished.ok and exit 0), `cancelled` (cancel requested), or `failed`;
   `runs` gets `finished_at`, `error`/`summary`, `output_path`; `activity_logs` gets `run_<status>`.
5. The browser polls `GET /api/runs/{id}` every 2 s while active and `GET /api/automations` every 5 s on the dashboard.

**Cancel.** `POST /cancel`: queued -> marked `cancelled` immediately; running -> `cancel_requested=1` then the process tree is
killed (`killpg` SIGTERM then SIGKILL on POSIX, `taskkill /T /F` on Windows), which also closes the browsers.

**Restart.** `JobManager.start()` marks every `running` run `failed` ("server restarted") and resumes `queued` ones. Re-running the
same sheet skips rows the engine already marked Done (it resumes from its own status file in the user's workspace).

## 9. Filesystem layout at runtime

```
storage/
  automation.db (+ -wal, -shm)
  workspaces/<slug>/auth_state.json                saved Office Ally session, shared per automation (runs are serialised)
  workspaces/<slug>/user-<id>/data/<sheet>.xlsx     uploaded sheets
  workspaces/<slug>/user-<id>/output/               engine outputs: <sheet>_with_status.xlsx, _results.csv, _visits.csv, logs.txt
  runs/<run id>/run.log, job.json                   per-run log and runner instructions (no secrets)
  runs/_oneshot/                                    short-lived describe/validate job files
automations/<slug>/.env                             Office Ally credentials for that automation
backend/.env                                        server settings + first-admin bootstrap
```

## 10. Configuration (env vars, optionally in `backend/.env`)

`AUTOMATION_STORAGE_DIR`, `AUTOMATION_AUTOMATIONS_DIR`, `AUTOMATION_FRONTEND_DIR`, `AUTOMATION_COOKIE_SECURE`,
`AUTOMATION_SESSION_HOURS` (12), `AUTOMATION_ALLOWED_ORIGINS`, `AUTOMATION_TRUST_PROXY`, `AUTOMATION_MAX_FAILED_LOGINS` (5),
`AUTOMATION_LOCKOUT_MINUTES` (15), `AUTOMATION_AUTO_APPROVE` (false), `AUTOMATION_PYTHON`, `AUTOMATION_MAX_UPLOAD_MB` (25),
`AUTOMATION_MAX_WORKERS` (4), `AUTOMATION_BROWSER_CHANNEL` (msedge), `AUTOMATION_HEADLESS` (false),
`AUTOMATION_ADMIN_EMAIL` / `AUTOMATION_ADMIN_PASSWORD` (first admin, created only if no admin exists; the operator's password is
exempt from the password policy), `AUTOMATION_RUN_TIMEOUT_HOURS` (12, read in `jobs.py`).

Start: `cd backend && python -m uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000` (or `start.sh` / `start.bat`).
`create_app` is a factory; there is deliberately no module-level `app`.

## 11. Frontend conventions

- Plain ES modules, no build tools. `static/app.js` exports `h()` (DOM builder), `api()`, `shell()` (top bar + page frame),
  `runPanel()` (progress UI shared by the run page and History), `stopRun()`, `pill()`, formatting helpers.
- **No `innerHTML` with data and no inline scripts/styles.** Values go through `textContent`; the CSP is
  `default-src 'self'` (+ `img-src data:`), so styles are classes and dynamic sizes use `el.style.x = ...` (CSSOM is allowed).
- One page = one HTML stub + one JS file (`login`, `signup`, `dashboard`, `automation`, `runs`, `account`, `admin`).
- Design: light theme only, flat surfaces, system fonts, a single shared side gutter (`--gutter`, `--max`) for top bar and page,
  no decorative dots/icons. Colors are CSS variables at the top of `styles.css`.

## 12. Invariants (do not break these)

1. **Never edit engine files for dashboard reasons.** Adapt in `runner.py` or `manifest.json`.
2. **Never import an engine into the web server process.** Only `runner.py` imports engines.
3. One active run per automation at a time (shared Office Ally login). Enforced by the per-slug worker thread.
4. A user must never reach another user's run, sheet or result: use `_get_run` / `allowed_automation`, return 404.
5. Never trust client-supplied paths: resolve filenames inside the user's `data_dir` (`safe_filename` + parent check).
6. Never store passwords or session tokens in readable form; only hashes.
7. Every security-relevant action writes `activity_logs` (and sign-ins write `login_events`) via `services/audit.py`.
8. The login path must take similar time and give identical messages for unknown email vs wrong password.
9. Keep at least one active admin (role/disable endpoints enforce it).
10. Append-only migrations.

## 13. How to extend

**Add an automation.** Create `automations/<slug>/` containing the engine (satisfying section 4), its `.env`, and a `manifest.json`
(keys: `slug` = folder name, `name`, `description`, `accent`, `insurance_id_label`, `required_fields` {key: label},
`input_fields` / `output_fields` as `[key, label, required]`). Restart. It appears as a card; grant access in Admin > Users.
Test it with `describe` and `validate` before a real run.

**Add an endpoint.** Put it in the matching router, depend on `require_user`/`require_admin`, use `ctx.db.session()` or
`.transaction()`, write an audit row for state changes, add a test.

**Change the schema.** Append a script to `MIGRATIONS`; never modify earlier ones.

**Add a page.** HTML stub in `frontend/`, JS in `frontend/static/`, route entry in `PAGES` in `main.py` with its audience
(`anonymous`, `user`, `admin`).

## 14. Testing

`cd backend && python -m pytest tests -q` (about 35 seconds). `conftest.py` builds an app per test over a temp storage dir and a
copy of `tests/fake_automations/`. The **fake engine** implements the real contract and reacts to patient names:
`SLOW` sleeps 60 s (for cancel/queue tests), `CRASH` raises, `DUP` reports a duplicate, `MISS` reports Not Created.
Tests spawn real runner subprocesses, so they exercise the actual protocol. They cover auth, approval, lockout, CSRF, session
expiry/revocation, role checks, per-user isolation, queue serialisation, cancel, crash handling, restart recovery, uploads,
column validation, activity and login logging, and the `.env` loader/bootstrap admin.
Browser checks were done by driving the real UI with Playwright against a throwaway server using the fake engine
(never against the real engines, which would open Office Ally).

## 15. Known gaps and gotchas

- Not yet deployed or run against real Office Ally through the dashboard. Headless mode on Linux is unverified (the engines use
  `no_viewport=True`, so window size may differ).
- No notifications, no email password reset, no automatic cleanup of old sheets/results/logs.
- Uploaded sheets, outputs, `run_rows` and run logs contain patient data.
- `Settings()` created directly (as in tests) does not read `backend/.env`; only `get_settings()` does.
- On macOS the project folder `automation/` was renamed to `app/`; the Python package inside is also called `app` (`backend/app`).
- The engines' own tests (`automations/*/tests/`) are the originals and are not part of the dashboard test suite.
- Office Ally credentials are one set per automation, readable by the service account; treat the server accordingly.
