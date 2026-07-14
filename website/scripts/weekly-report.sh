#!/usr/bin/env bash
# 运营周报 (cron 周一 09:30) — 汇总近 7 天数据推送给绑定的管理员
export HOME=/home/ubuntu
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

APP_DIR="${APP_DIR:-/home/ubuntu/yuntech}"
PORT="${PORT:-3000}"
LOG=/home/ubuntu/weekly-report.log

# tr -d '\r'：容忍 Windows 上传的 CRLF env 文件
KEY=$(grep -E '^TELEGRAM_SETUP_KEY=' "$APP_DIR/.env.local" 2>/dev/null | sed -E 's/^[^=]+=//; s/^"//; s/"$//' | tr -d '\r')
[ -n "$KEY" ] || exit 0

ts=$(date '+%F %T')
out=$(curl -s -m 60 -X POST -H "x-setup-key: $KEY" -H "Content-Type: application/json" -d '{}' \
  "http://127.0.0.1:$PORT/api/admin/weekly-report")
echo "[$ts] $out" >> "$LOG"
