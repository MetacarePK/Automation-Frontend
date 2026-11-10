# Project guide for AI assistants

Multi-user dashboard (FastAPI + SQLite + plain JS) that runs existing Office Ally billing automations ("engines") as isolated
subprocesses. **Read `ARCHITECTURE.md` before changing anything**; `OVERVIEW.md` is the plain-language version.

Non-negotiables:
- Do not modify files in `automations/<slug>/` (the tested engines). Adapt in `backend/runner/runner.py` or `manifest.json`.
- Never import an engine into the web server process; only `runner.py` does.
- One run at a time per automation (manifest `max_concurrent_runs`, default 1: one Office Ally login); each run uses only its own folder; users must never reach each other's runs/sheets/logs (404, not 403).
- A run is `completed` only after `verify_output` passes; retries never allow duplicates and always resume.
- Never store readable user passwords/tokens; the Office Ally password is write-only (never returned, logged or audited) and lives only in the engine's `.env`. Append-only DB migrations (table rebuilds go in `FK_OFF_MIGRATIONS`). Every security-relevant action writes an audit row. Logs go through `redact`.
- Frontend: no inline scripts/styles, no `innerHTML` with data, light theme only, no decorative dots/icons.

Commands: `cd backend && python -m pytest tests -q` (about 2 minutes). Start: `./start.sh` or `start.bat`.
Never run the real engines to test (they open Office Ally); use `backend/tests/fake_automations/fake-auto`.

Server checks: `python -m app.cli preflight`, `python -m app.cli check-login <slug>` (real, read-only sign-in; needs the user's go-ahead). The suite cannot prove Office Ally works from a server; see `deploy/README.md` Part 3.
