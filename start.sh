#!/bin/bash
# Local/Linux launcher: installs what is missing, then serves the dashboard on 127.0.0.1:8000.
cd "$(dirname "$0")/backend"
python3 -m pip install -q -r requirements.txt
[ -f .env ] && set -a && . ./.env && set +a
python3 -m playwright install msedge >/dev/null 2>&1 || true
exec python3 -m uvicorn app.main:create_app --factory --host 127.0.0.1 --port "${PORT:-8000}"
