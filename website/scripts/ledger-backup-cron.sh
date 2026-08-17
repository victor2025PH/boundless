#!/usr/bin/env bash
# 集团库备份 cron 包装器（实施25）：跑 ledger-backup.mjs，失败即 Telegram 告警。
# 静默成功（不刷屏）；失败发告警 + 落 down-flag；下次成功补发「已恢复」并清 flag。
# crontab: 15 3 * * * /home/ubuntu/yuntech/scripts/ledger-backup-cron.sh
export HOME="${HOME:-/home/ubuntu}"
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
APP_DIR="${APP_DIR:-/home/ubuntu/yuntech}"
BACKUP_DIR="${LEDGER_BACKUP_DIR:-/home/ubuntu/ledger-backups}"
LOG="${LEDGER_BACKUP_LOG:-/home/ubuntu/ledger-backup.log}"
DOWNFLAG=/home/ubuntu/.ledger-backup.down
NOTIFY="$APP_DIR/scripts/notify-telegram.sh"
ts=$(date '+%F %T')

cd "$APP_DIR" || { echo "[$ts] FATAL: cd $APP_DIR 失败" >> "$LOG"; exit 1; }

# 跑备份，捕获输出与退出码（--out-summary 末行是 JSON，便于排障）
OUT=$(/usr/bin/node scripts/ledger-backup.mjs --dir "$BACKUP_DIR" --keep-days 30 --keep-min 10 --out-summary 2>&1)
code=$?
echo "[$ts] exit=$code" >> "$LOG"
echo "$OUT" >> "$LOG"

if [ "$code" -ne 0 ]; then
  # 失败：告警（截取末 3 行错误，控制消息长度），落 down-flag（避免每日重复告警靠 flag 幂等）
  tail3=$(echo "$OUT" | grep -iE 'failed|失败|error|integrity' | tail -3 | tr '\n' ' ')
  bash "$NOTIFY" "[华灵运维] ⛔ 集团库备份失败 @ $ts · 退出码 $code · $tail3 · 详情 $LOG" 2>/dev/null
  touch "$DOWNFLAG"
  exit "$code"
fi

# 成功：若此前失败过，补发恢复通知并清 flag（与 health-watchdog 同款语义）
if [ -f "$DOWNFLAG" ]; then
  bash "$NOTIFY" "[华灵运维] ✅ 集团库备份已恢复正常 @ $ts" 2>/dev/null
  rm -f "$DOWNFLAG"
fi
exit 0
