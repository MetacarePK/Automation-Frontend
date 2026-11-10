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

1. Upload: the sheet is saved in that user's own workspace (`storage/workspaces/<automation>/user-<id>/`).
2. Start: the server checks the sheet with the engine's own reader, then queues the run.
3. One run at a time **per automation** (each uses one Office Ally login; two windows on one login can hang).
   ACO and Non ACO run side by side. Others wait in the queue and start by themselves.
4. Each run is its own process (`backend/runner/runner.py`) that points the engine at that user's folders,
   applies the browser settings from the environment, and reports each row back. The page polls the database,
   so a refresh, a second tab, or a server restart loses nothing.
5. Stopping a run kills the runner and every browser it started.
6. Re-running the same sheet name resumes: rows already Done are skipped, exactly as on the PC.

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
cd backend && python -m pytest tests -q        # 38 tests: auth, approval, isolation, queue, cancel, crash, restart
```

## Putting it on the server

See `deploy/README.md`.
