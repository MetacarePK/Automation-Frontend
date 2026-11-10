#!/bin/sh
# Runs as the unprivileged 'automation' user before the server starts.
# Each automation's Office Ally login lives in the persistent volume (/data/secrets/<slug>.env) and is linked into
# the engine's folder, so a rebuilt or recreated container keeps the logins an admin saved from the dashboard.
set -eu

APP_DIR="${APP_DIR:-/app}"
DATA_DIR="${DATA_DIR:-/data}"

mkdir -p "$DATA_DIR/storage" "$DATA_DIR/secrets"
chmod 700 "$DATA_DIR/storage" "$DATA_DIR/secrets" 2>/dev/null || true

for dir in "$APP_DIR"/automations/*/; do
  [ -f "${dir}manifest.json" ] || continue
  slug="$(basename "$dir")"
  secret="$DATA_DIR/secrets/$slug.env"
  if [ ! -f "$secret" ]; then
    : > "$secret"                 # empty until an admin saves the login in the dashboard
    chmod 600 "$secret"
  fi
  ln -sfn "$secret" "${dir}.env"
done

exec "$@"
