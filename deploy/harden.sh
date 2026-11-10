#!/bin/bash
# Run once on the server as root, after copying the app to /opt/automation/app.
# Restricts who can read the secrets (.env files with logins) and the patient data (storage/).
set -euo pipefail
APP=/opt/automation/app
id automation >/dev/null 2>&1 || useradd --system --home /opt/automation --shell /usr/sbin/nologin automation
chown -R automation:automation "$APP"
chmod 700 "$APP/storage" 2>/dev/null || mkdir -p "$APP/storage" && chmod 700 "$APP/storage" && chown automation:automation "$APP/storage"
chmod 600 "$APP/backend/.env" "$APP"/automations/*/.env
chmod 750 "$APP"
echo "Secrets are now readable only by the 'automation' account. Verify with: ls -l $APP/backend/.env $APP/automations/*/.env"
