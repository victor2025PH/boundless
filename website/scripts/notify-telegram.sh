#!/usr/bin/env bash
# 复用型 Telegram 运维通知器（从 health-watchdog.sh 的 notify() 抽出，供各 cron 复用）。
# 用法: notify-telegram.sh "单行纯文本消息"
# 收件人 = .env.local 的 ALERT_CHAT_ID(逗号分隔可多个) ∪ hualing-leads/admin_chats.json 里绑定的 chat。
# token/chat 缺失时静默退出 0（不阻断调用方）；tr -d '\r' 容忍 Windows 上传的 CRLF env。
export HOME="${HOME:-/home/ubuntu}"
APP_DIR="${APP_DIR:-/home/ubuntu/yuntech}"
MSG="${1:-}"
[ -n "$MSG" ] || exit 0

T=$(grep -E '^TELEGRAM_BOT_TOKEN=' "$APP_DIR/.env.local" 2>/dev/null | sed -E 's/^[^=]+=//; s/^"//; s/"$//' | tr -d '\r')
[ -n "$T" ] || exit 0

ids=$(grep -E '^ALERT_CHAT_ID=' "$APP_DIR/.env.local" 2>/dev/null | sed -E 's/^[^=]+=//; s/^"//; s/"$//' | tr -d '\r' | tr ',' '\n')
if [ -f "$HOME/hualing-leads/admin_chats.json" ]; then
  ids="$ids
$(grep -oE '[0-9-]{6,}' "$HOME/hualing-leads/admin_chats.json" 2>/dev/null)"
fi
ids=$(echo "$ids" | grep -E '^-?[0-9]+$' | sort -u)
[ -n "$ids" ] || exit 0

for c in $ids; do
  curl -s "https://api.telegram.org/bot$T/sendMessage" \
    --data-urlencode "chat_id=$c" \
    --data-urlencode "text=$MSG" >/dev/null 2>&1
done
exit 0
