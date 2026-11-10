# Automation dashboard

One website for all the billing automations: people sign up, an admin approves them, each person sees a card
per automation they may use, opens one, uploads a sheet, and watches it run. Everything is recorded per user.

Docs: `OVERVIEW.md` (plain-language tour), `ARCHITECTURE.md` (technical map for developers and AI assistants), `deploy/README.md` (server).

Today it hosts **Blackhill ACO** and **Blackhill Non ACO**. Their engines in `automations/` are byte-for-byte
the files that have been running on the Windows PC.

```
app/
  backend/      FastAPI + SQLite (app/ = the server, runner/ = runs one job, tests/)
  frontend/     the pages (plain HTML/JS/CSS, no build step)
  automations/  one folder per automation: its engine + manifest.json (+ its own .env)
  deploy/       Caddyfile (HTTPS for your domain), systemd service, notes
  storage/      created at runtime: database, each user's sheets/outputs, run logs
```

## Run it

```
./start.sh            # Mac / Linux            start.bat  on Windows
python -m app.cli create-admin you@yourcompany.com     # once, from backend/ (asks for a password)
```

Open http://127.0.0.1:8000, sign in, and the admin area is at `/admin`.
Anyone else uses **Request an account**; the request waits in Admin > Users until approved.

## How a run works

1. Upload: the sheet is staged in that user's own area; starting a run copies it into the run's private folder (`storage/runs/<id>/`).
2. The server checks the sheet with the engine's own reader and queues the run (state `queued`).
3. A scheduler starts it when there is room: global caps on runs and browser windows, one run per automation at a time (one Office Ally
   login each), and a fair order between people. ACO and Non ACO run side by side.
4. `validating` (sheet and login re-checked) then `running`: a separate process (`backend/runner/runner.py`) runs the unchanged engine against
   the run's own folders and reports each row; the page polls the database, so refreshes, reconnects and restarts lose nothing.
5. When the process ends every browser it started is cleaned up, the result sheet is read back and compared with the database, and only then the
   run becomes `completed` / `completed_with_errors` (otherwise `failed`, with the reason). Stop gives `cancelled`; a server restart gives `interrupted`.
6. Retry (manual, or one automatic retry after a crash) skips rows already Done and never allows duplicates.
7. Old files and patient row details are removed by the retention policy.

## What is logged

| Where | What |
|---|---|
| Admin > Sign-ins | every sign-in, good or bad: who, result, reason, IP, browser |
| Admin > Activity log | sign-ups, approvals, role/access changes, uploads, runs started/finished/stopped, downloads |
| My account > My activity | the same trail, for that one person |
| Run history > a run | status per row, counts, and the full run log |
| Admin > Overview | sign-ins per day, who is active, what is running, failed sign-ins |

## Security built in

- Passwords hashed with bcrypt; 10+ characters with upper/lower case and a number.
- Sessions are random tokens in an HttpOnly, SameSite cookie; only a hash is stored, so a database copy can't be replayed. Admins can end any session.
- 5 wrong passwords lock the account for 15 minutes; failures per IP are capped too.
- New accounts can't do anything until an admin approves them and picks their automations.
- Cross-site POSTs are rejected; strict Content-Security-Policy; one person can never open another's run or sheet.
- The first admin is created from the command line, never through the website.

## Add the next automation

1. Copy its folder to `automations/<slug>/` (it needs `main.py` with `run_automation()` and `patients.py` with
   `DEFAULT_COLUMN_MAP`, `resolve_column_map`, `read_patients`, like the Blackhill engines).
2. Add `manifest.json` next to it (copy one from `blackhill-aco/` - name, description, accent colour, the sheet fields).
3. Restart. It appears as a new card; give people access in Admin > Users.

## Tests

```
cd backend && python -m pytest tests -q        # auth, approval, isolation, states, verification, retry, scheduling, cleanup, retention, logins
```

## Putting it on the server

See `deploy/README.md` (install, `preflight`, `check-login`, and the checks to run with the real portal).
