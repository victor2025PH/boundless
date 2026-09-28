#!/usr/bin/env bash
# Install a 15-minute cron that POSTs /api/admin/schedule/run (scheduled posts + dragon daily
# reminders + @ChatX_bot 24h follow-ups). Idempotent: skips if already present.
# Reads TELEGRAM_SETUP_KEY from the app's .env.local; runs on the VPS, not locally.
set -euo pipefail

APP_DIR="/home/ubuntu/yuntech"
LOG="/home/ubuntu/schedule-run.log"
ENDPOINT="http://127.0.0.1:3000/api/admin/schedule/run"

KEY="$(grep -E '^TELEGRAM_SETUP_KEY=' "$APP_DIR/.env.local" | cut -d= -f2- | tr -d '\r' || true)"
if [ -z "$KEY" ]; then
  echo "[error] no TELEGRAM_SETUP_KEY in $APP_DIR/.env.local"
  exit 1
fi

LINE="*/15 * * * * curl -fsS -m 60 -X POST -H \"x-setup-key: ${KEY}\" \"${ENDPOINT}\" >> ${LOG} 2>&1"

if crontab -l 2>/dev/null | grep -q "schedule/run"; then
  echo "[skip] schedule/run cron already installed"
else
  ( crontab -l 2>/dev/null; echo "$LINE" ) | crontab -
  echo "[done] schedule/run cron installed (every 15 min)"
fi

echo "--- current crontab ---"
crontab -l 2>/dev/null | sed -E 's/(x-setup-key: |key=)[^ \"]*/\1***/g'
