# Deploying and verifying on the server

Two Blackhill automations first (ACO and Non ACO). Nothing here is "done" until the checks in **Part 3** pass on
the real server with real Office Ally logins. The automated tests prove the dashboard logic; they cannot prove
Office Ally works from your server.

## Part 1: install

The server must be in the **United States** (the Office Ally portals do not open from Pakistan; that is why the PC
needs a VPN, and a US server does not).

**Linux (recommended for a server)**
1. `apt install python3-venv caddy xvfb`. Copy the `app` folder to `/opt/automation/app`.
2. `python3 -m venv /opt/automation/venv && /opt/automation/venv/bin/pip install -r /opt/automation/app/backend/requirements.txt`
3. `/opt/automation/venv/bin/python -m playwright install --with-deps chromium`
4. Copy `backend/.env.example` to `backend/.env` and fill it in. For Linux set
   `AUTOMATION_BROWSER_CHANNEL=chromium` and `AUTOMATION_HEADLESS=true`.
5. Put each automation's Office Ally login in its own `automations/<slug>/.env`, or save it later from the dashboard
   (run page, "Office Ally login").
6. `sudo bash /opt/automation/app/deploy/harden.sh` (secrets readable only by the service account).
7. Install `deploy/automation.service` to `/etc/systemd/system/`, then `systemctl enable --now automation`.
8. Put `deploy/Caddyfile` at `/etc/caddy/Caddyfile` with your domain (DNS A record pointing at the server first),
   `systemctl reload caddy`. Then set `AUTOMATION_COOKIE_SECURE=true`, `AUTOMATION_TRUST_PROXY=true`,
   `AUTOMATION_ALLOWED_ORIGINS=https://your-domain` and restart.

**Windows (closest to your PC)**
Install Python 3.11+ and Microsoft Edge, copy `app` to `C:\automation\app`, fill `backend\.env`
(`msedge`, `AUTOMATION_HEADLESS=false`), `python -m pip install -r requirements.txt`, then run `start.bat`.
Edge windows need a signed-in desktop session (Remote Desktop; do not sign the user out). A Windows *service* has no
desktop, so use Task Scheduler "At log on" running `start.bat` if you need visible windows. Put Caddy for Windows in front.

## Part 2: check the machine (no patient data, no Office Ally login)

```
cd /opt/automation/app/backend
sudo -u automation /opt/automation/venv/bin/python -m app.cli preflight
```
It checks Python and packages, storage and free disk, that each automation has a login saved and its engine loads,
that the configured browser actually starts (`about:blank` only), that a visible browser has a display, and that
`cms.officeally.com` and `pm.officeally.com` answer from this server. Every line must say PASS (WARN needs a look).
Use `--skip-network` to leave out the two website checks.

## Part 3: verify with the real portal (you must do these, they need real logins and approved test data)

1. **Real sign-in, nothing changed.** For each automation:
   `python -m app.cli check-login blackhill-aco` and `python -m app.cli check-login blackhill-non-aco`
   (or the "Test login" button on the run page). Expected: `PASS signed in to Office Ally`.
2. **One small real run per automation, from the dashboard**, using a sheet of 3 to 5 rows that your team approves for
   testing: include one row you expect to fail (a patient not in Office Ally) and one that already has a visit.
   Start it with "Save to Office Ally" ON only if those test visits may really be created, otherwise use rows that
   are safe to create.
3. **Check the results against Office Ally itself** (open the patients in Practice Mate), then against the dashboard:
   - The run ends as Completed (or Completed with errors if you included a failing row), never stuck on Running.
   - Successful / Skipped / Failed counts equal what you see in Practice Mate.
   - "Result sheet" opens and its Status column agrees with the rows shown; "Failed records (CSV)" lists the failing row.
4. **Run both automations at the same time** with two small sheets. Both must show Running together.
5. **Stop test.** Start a larger sheet, press Stop, then confirm no browser is left:
   `pgrep -fa 'chrom|msedge'` shows nothing from that run.
6. **Retry test.** Retry the run that had a failed row: rows already done must not be processed again
   (the run shows "Done earlier") and no duplicate visit appears in Practice Mate.
7. **Restart test.** Start a run, `systemctl restart automation`: the run must show Interrupted (not Running),
   History is still there, and Retry continues without redoing finished rows.
8. **Headless check (Linux).** Compare one headless run's result sheet with the same sheet run on your Windows PC.
   Headless browsers can behave differently; do not rely on it until the outputs match.

Only after steps 1 to 8 pass should the remaining automations be moved.

## Operating it

- **Logs:** `journalctl -u automation -f` for the server; each run's own log is on the run page ("Log", "History").
- **Limits:** `AUTOMATION_MAX_CONCURRENT_RUNS`, `AUTOMATION_MAX_TOTAL_WORKERS`, `AUTOMATION_MAX_WORKERS`; the systemd file adds memory
  and task limits. Watch memory with 2 runs of 4 windows each and lower the limits if the server swaps.
- **Two runs of one automation** never overlap (one Office Ally login). To allow it later, add
  `"max_concurrent_runs": 2` to that automation's `manifest.json` only after verifying separate sessions are safe.
- **Retention:** files and row details are removed automatically after `AUTOMATION_RETENTION_FILES_DAYS` and
  `AUTOMATION_RETENTION_ROWS_DAYS`; the run record and audit trail stay.
- **Backups:** copy `storage/automation.db`. Remove `AUTOMATION_ADMIN_PASSWORD` from `.env` after the first start.
- **Changing the Office Ally login:** run page, "Office Ally login" (admins, or people an admin allowed). The password
  is never shown again. It cannot be changed while that automation has an active run.
- **If the server dies:** runs that were in progress show Interrupted. systemd's control-group stop also ends their
  browsers. Retry them; finished rows are skipped.
- **Update:** copy new code over, `systemctl restart automation`. The database upgrades itself (migrations only append).
