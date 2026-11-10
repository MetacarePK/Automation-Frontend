# Docker deployment: automation.metacare.one

For an Ubuntu server that already runs other containers (your `remiai-*` stack, with Caddy on ports 80 and 443).
This adds **one separate container** and one site block in your existing Caddy. It does not touch the remiai containers,
and it publishes **no ports** on the host.

> **Not yet tested by me:** I had no Docker on my machine, so the image has not been built or started. I checked what I could
> (compose file parses, entrypoint behaves, ignore rules keep secrets out and keep needed files in). Step 6 (`preflight`) is the first
> real test of the build and of Chromium inside the container. If anything there fails, send me the output.

## 0. Before you start

- Create the DNS record: `automation.metacare.one` **A** -> the server's IP. Check: `dig +short automation.metacare.one`.
  Do this first: Caddy asks Let's Encrypt for a certificate as soon as the site is added, and a wrong DNS wastes attempts.
- Use a **new folder** for this app, `/opt/automation`, separate from the remiai project.

## 1. Look around first (read-only, changes nothing)

```bash
free -h; nproc; df -h /
docker network ls
# which network is Caddy on? (this is CADDY_NETWORK below, probably remiai_default)
docker inspect remiai-caddy-1 --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}}{{"\n"}}{{end}}'
# where is its Caddyfile on the host, and what path does Caddy use inside the container?
docker inspect remiai-caddy-1 --format '{{range .Mounts}}{{.Source}}  ->  {{.Destination}}{{"\n"}}{{end}}'
docker inspect remiai-caddy-1 --format '{{.Path}} {{join .Args " "}}'
```

Memory matters: each Chromium window uses roughly 300 to 600 MB. The compose file limits this container to
`AUTOMATION_MEM_LIMIT` (default 6g). If `free -h` shows less than about 8 GB available alongside remiai, set it lower
and keep `AUTOMATION_MAX_TOTAL_WORKERS` at 2 to 4.

## 2. Copy the app to the server (from your Mac)

```bash
rsync -av --delete \
  --exclude '.git' --exclude 'storage' --exclude '.env' --exclude '__pycache__' --exclude '*.xlsx' \
  "/Users/alihassan/Documents/Freelancing/Meta X Care/Automation/app/" root@SERVER_IP:/opt/automation/app/
```

`.env` files are excluded on purpose: no password travels with the code, and none can end up inside the image.

## 3. Configure (on the server)

```bash
cd /opt/automation/app
cp deploy/docker/env.example .env
chmod 600 .env
nano .env          # set CADDY_NETWORK, the admin email and a LONG admin password, limits
```

- Do **not** put `$` in the admin password (Docker would treat it as a variable).
- Keep the values as written: no comments after a value on the same line.
- The first admin is created from `AUTOMATION_ADMIN_EMAIL` / `AUTOMATION_ADMIN_PASSWORD` on first start.

## 4. Build and start

```bash
docker compose build          # a few minutes: installs Chromium and its system libraries
docker compose up -d
docker compose ps             # STATE should become "healthy"
docker compose logs --tail 30 app    # look for: "First admin created"
```

## 5. Add the site to your existing Caddy

Do this only after step 4 shows the container healthy and DNS resolves.

```bash
# use the host path you saw in step 1 for CADDYFILE
CADDYFILE=/path/to/Caddyfile
cp "$CADDYFILE" "$CADDYFILE.bak-$(date +%F)"
cat deploy/docker/Caddyfile.snippet >> "$CADDYFILE"        # append in place (do not replace the file)
docker exec remiai-caddy-1 caddy validate --config /etc/caddy/Caddyfile
docker exec remiai-caddy-1 caddy reload   --config /etc/caddy/Caddyfile
```

- Use the container-side path from step 1 in the last two commands if it is not `/etc/caddy/Caddyfile`.
- `validate` first: a typo would otherwise break the remiai site too. If it complains, restore the `.bak` copy.
- Append with `cat >>` as shown. Editors that replace the file (some `nano`/`vim`/`sed -i` setups) can leave a single-file
  bind mount pointing at the old copy; if the reload shows no change, `docker restart remiai-caddy-1` (brief remiai downtime).

Open https://automation.metacare.one and sign in with the admin email and password from `.env`.

## 6. Verify (the first real test of the build)

```bash
docker compose exec app python -m app.cli preflight
```

Expected: every line PASS, including `browser 'chromium' headless=True started`, and both Office Ally sites answering.
No WARN or FAIL about packages, storage or the browser. (A FAIL on "login saved" is expected until step 7.)

## 7. Office Ally logins

In the dashboard open each automation (Blackhill ACO, Blackhill Non ACO), expand **Office Ally login**, enter the username and
password, **Save**, then **Test login**. The logins are stored in the persistent volume (`/data/secrets/<automation>.env`),
so they survive rebuilds. Then, from the server:

```bash
docker compose exec app python -m app.cli preflight
docker compose exec app python -m app.cli check-login blackhill-aco
docker compose exec app python -m app.cli check-login blackhill-non-aco
```

Now follow **Part 3** of `deploy/README.md` (small real sheet per automation, both at once, stop, retry, restart, and compare a headless
result with your Windows PC). Only after those pass should you rely on it. Until then, headless Chromium in Docker is unproven.

## 8. After the first start

Edit `.env`, delete the `AUTOMATION_ADMIN_PASSWORD` line, then `docker compose up -d` (recreates the container; the account stays).
Change the admin password in the dashboard (Account) to something strong.

## Everyday commands

```bash
docker compose logs -f app                  # server log
docker compose ps ; docker stats automation-app
docker compose restart app                  # runs in progress become "Interrupted"; Retry continues them
docker compose down                         # stops it; the data volume stays (never add -v)
```

**Update:** `rsync` again (step 2), then `docker compose build && docker compose up -d`. The database upgrades itself.

**Back up** (a consistent copy of the database, then the files):
```bash
docker compose exec app python -c "import sqlite3; s=sqlite3.connect('/data/storage/automation.db'); d=sqlite3.connect('/data/storage/backup.db'); s.backup(d)"
docker run --rm -v automation_data:/data -v "$PWD":/out alpine tar czf /out/automation-data-$(date +%F).tgz -C /data .
```
The backup holds patient data and logins: keep it private.

**Remove the site:** delete the block from the Caddyfile and reload; `docker compose down`.

## What this setup gives you

- A stopped or restarted container ends every browser it started (no orphaned processes).
- Memory, CPU and process limits, so the automations cannot starve the other apps on the server.
- Logins, database and run files live in one volume; the image holds no secrets.
- Nothing is published on the host. Only Caddy reaches the app, over the shared Docker network. (Other containers on that
  network could also reach it; every page and API still requires a signed-in account.)
