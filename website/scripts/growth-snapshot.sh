#!/usr/bin/env bash
# 频道/群订阅数每日快照 (cron 03:20)
# 调 /api/admin/growth 记录 getChatMemberCount 到 growth.jsonl，供后台增长曲线使用。
export HOME=/home/ubuntu
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

APP_DIR="${APP_DIR:-/home/ubuntu/yuntech}"
PORT="${PORT:-3000}"
LOG=/home/ubuntu/growth-snapshot.log

# tr -d '\r'：容忍 Windows 上传的 CRLF env 文件
KEY=$(grep -E '^TELEGRAM_SETUP_KEY=' "$APP_DIR/.env.local" 2>/dev/null | sed -E 's/^[^=]+=//; s/^"//; s/"$//' | tr -d '\r')
[ -n "$KEY" ] || exit 0

ts=$(date '+%F %T')
out=$(curl -s -m 20 -X POST -H "x-setup-key: $KEY" "http://127.0.0.1:$PORT/api/admin/growth")
echo "[$ts] $out" >> "$LOG"
