# Project guide for AI assistants

Multi-user dashboard (FastAPI + SQLite + plain JS) that runs existing Office Ally billing automations ("engines") as isolated
subprocesses. **Read `ARCHITECTURE.md` before changing anything**; `OVERVIEW.md` is the plain-language version.

Non-negotiables:
- Do not modify files in `automations/<slug>/` (the tested engines). Adapt in `backend/runner/runner.py` or `manifest.json`.
- Never import an engine into the web server process; only `runner.py` does.
- One run at a time per automation; users must never reach each other's runs/sheets (404, not 403).
- Never store readable passwords/tokens; append-only DB migrations; every security-relevant action writes an audit row.
- Frontend: no inline scripts/styles, no `innerHTML` with data, light theme only, no decorative dots/icons.

Commands: `cd backend && python -m pytest tests -q` (38 tests). Start: `./start.sh` or `start.bat`.
Never run the real engines to test (they open Office Ally); use `backend/tests/fake_automations/fake-auto`.
