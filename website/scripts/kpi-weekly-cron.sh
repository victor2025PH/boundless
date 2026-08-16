#!/usr/bin/env bash
# KPI 周报 cron 包装器（实施27）：跑 kpi-weekly-report.mjs 出报告文件，失败即 Telegram 告警。
# 与 ledger-backup-cron 同款：失败 down-flag 幂等 + 恢复补发；成功静默。
# crontab: 35 9 * * 1 /usr/bin/bash /home/ubuntu/yuntech/scripts/kpi-weekly-cron.sh
export HOME="${HOME:-/home/ubuntu}"
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
APP_DIR="${APP_DIR:-/home/ubuntu/yuntech}"
OUT_DIR="${KPI_REPORT_DIR:-/home/ubuntu/kpi-reports}"
DOWNFLAG=/home/ubuntu/.kpi-weekly.down
NOTIFY="$APP_DIR/scripts/notify-telegram.sh"
ts=$(date '+%F %T'); day=$(date '+%Y%m%d')
mkdir -p "$OUT_DIR"
cd "$APP_DIR" || { bash "$NOTIFY" "[华灵运维] ⛔ KPI 周报: cd $APP_DIR 失败 @ $ts" 2>/dev/null; exit 1; }

OUT_FILE="$OUT_DIR/kpi_$day.md"
/usr/bin/node scripts/kpi-weekly-report.mjs > "$OUT_FILE" 2>"$OUT_DIR/.kpi_$day.err"
code=$?

if [ "$code" -ne 0 ]; then
  err=$(tail -3 "$OUT_DIR/.kpi_$day.err" 2>/dev/null | tr '\n' ' ')
  bash "$NOTIFY" "[华灵运维] ⛔ KPI 周报生成失败 @ $ts · 退出码 $code · $err" 2>/dev/null
  touch "$DOWNFLAG"
  exit "$code"
fi
rm -f "$OUT_DIR/.kpi_$day.err"
if [ -f "$DOWNFLAG" ]; then
  bash "$NOTIFY" "[华灵运维] ✅ KPI 周报已恢复正常 @ $ts（$OUT_FILE）" 2>/dev/null
  rm -f "$DOWNFLAG"
fi
exit 0
