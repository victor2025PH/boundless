#!/usr/bin/env bash
# 每日频道营销帖自动发布 (cron 11:00)
# 调 /api/admin/daily 直发频道；命中「防编造闸」的稿子服务端自动降级为草稿，不上频道。
export HOME=/home/ubuntu
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

APP_DIR="${APP_DIR:-/home/ubuntu/yuntech}"
PORT="${PORT:-3000}"
LOG=/home/ubuntu/daily-post.log

# tr -d '\r'：容忍 Windows 上传的 CRLF env 文件
KEY=$(grep -E '^TELEGRAM_SETUP_KEY=' "$APP_DIR/.env.local" 2>/dev/null | sed -E 's/^[^=]+=//; s/^"//; s/"$//' | tr -d '\r')
[ -n "$KEY" ] || exit 0

ts=$(date '+%F %T')
out=$(curl -s -m 120 -X POST \
  -H "x-setup-key: $KEY" -H "Content-Type: application/json" \
  -d '{"publish":"channel","notify":false}' \
  "http://127.0.0.1:$PORT/api/admin/daily")
echo "[$ts] $out" >> "$LOG"
