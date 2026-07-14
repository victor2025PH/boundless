#!/usr/bin/env bash
# Install a 10-minute cron that pings the order-SLA scan (stale-paid alerts + renewal reminders).
# Idempotent: skips if already present. Reads ADMIN_KEY from the app's .env.local.
set -euo pipefail

APP_DIR="/home/ubuntu/yuntech"
LOG="/home/ubuntu/order-sla.log"
ENDPOINT="http://127.0.0.1:3000/api/admin/order-sla"

KEY="$(grep -E '^ADMIN_KEY=' "$APP_DIR/.env.local" | cut -d= -f2- | tr -d '\r' || true)"
if [ -z "$KEY" ]; then
  KEY="$(grep -E '^TELEGRAM_SETUP_KEY=' "$APP_DIR/.env.local" | cut -d= -f2- | tr -d '\r' || true)"
fi
if [ -z "$KEY" ]; then
  echo "[error] no ADMIN_KEY / TELEGRAM_SETUP_KEY in $APP_DIR/.env.local"
  exit 1
fi

LINE="*/10 * * * * curl -fsS -m 30 \"${ENDPOINT}?key=${KEY}\" >> ${LOG} 2>&1"

if crontab -l 2>/dev/null | grep -q "order-sla"; then
  echo "[skip] order-sla cron already installed"
else
  ( crontab -l 2>/dev/null; echo "$LINE" ) | crontab -
  echo "[done] order-sla cron installed (every 10 min)"
fi

echo "--- current crontab ---"
crontab -l 2>/dev/null | sed 's/key=[^ \"]*/key=***/g'
