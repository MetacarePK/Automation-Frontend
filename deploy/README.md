# Deploying to the server

The portals do not open from Pakistan, so the server must be in the **US** (then no VPN is needed on it).
Run the dashboard **behind HTTPS on your domain**; never expose port 8000 directly.

## Before anything: test Office Ally from the server

Open https://cms.officeally.com in the server's browser and sign in to Practice Mate by hand once.
If that works, the automations will. (Datacenter IPs are sometimes challenged - better to learn that first.)

## Windows server (closest to your PC - recommended first)

1. Install Python 3.11+, Microsoft Edge, and copy the `app` folder to `C:\automation\app`.
2. In `app\backend` copy `.env.example` to `.env` and fill it in (keep `msedge` / `AUTOMATION_HEADLESS=false`:
   the engines were built and tested with a visible Edge window; a signed-in desktop session must stay open
   on the server, e.g. via Remote Desktop with the window minimised - do not log the user out).
3. `python -m pip install -r requirements.txt`, then `python -m app.cli create-admin you@yourcompany.com`.
4. Run it as a service so it survives reboots: install NSSM (`nssm install AutomationDashboard`), with
   Application = `python.exe`, Arguments = `-m uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000`,
   Startup directory = `C:\automation\app\backend`. Note: a Windows *service* has no desktop, so Edge windows
   would not show; if you need visible windows use Task Scheduler "At log on" with `start.bat` instead.
5. Install Caddy for Windows, put `Caddyfile` next to it with your domain, and run it as a service.

## Linux server

1. `apt install python3-venv caddy`, copy `app` to `/opt/automation/app`, create a venv at `/opt/automation/venv`,
   `pip install -r backend/requirements.txt`, then `python -m playwright install --with-deps chromium`.
2. In `backend/.env` set `AUTOMATION_BROWSER_CHANNEL=chromium` and `AUTOMATION_HEADLESS=true`.
   (Headless can behave differently from a visible window - run one small real sheet and compare before trusting it.)
3. Install `automation.service` to `/etc/systemd/system/`, then `systemctl enable --now automation`.
4. Put the `Caddyfile` at `/etc/caddy/Caddyfile` (your domain), `systemctl reload caddy`.

## Settings to flip once HTTPS works

`AUTOMATION_COOKIE_SECURE=true`, `AUTOMATION_TRUST_PROXY=true`, `AUTOMATION_ALLOWED_ORIGINS=https://your-domain`.

## Keep safe

- `automations/*/.env` hold the Office Ally logins and `storage/` holds the sheets: restrict both to the service
  account, and back up `storage/automation.db` (it is the whole history).
- Remove `AUTOMATION_ADMIN_PASSWORD` from `.env` after the first start.
- Update by copying new code over and restarting; the database upgrades itself.
