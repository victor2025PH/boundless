#!/usr/bin/env bash
# AI 网关 24h 耗时报表 → Telegram 运维群（Q-14 #262 C）。每日一次，接进「📊 日报 · 成本」
# （ai_cost_report）同一类告警：花了多少（cost_recon 在实例侧发）+ 慢不慢 / 499 率（这里发）。
# 与 kpi-weekly-cron 同款：失败 down-flag 幂等 + 恢复补发。
# crontab: 40 9 * * * /usr/bin/bash /home/ubuntu/yuntech/scripts/ai-gateway-report-cron.sh
export HOME="${HOME:-/home/ubuntu}"
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
APP_DIR="${APP_DIR:-/home/ubuntu/yuntech}"
DOWNFLAG=/home/ubuntu/.ai-gateway-report.down
NOTIFY="$APP_DIR/scripts/notify-telegram.sh"
LOG=/home/ubuntu/ai-gateway-report.log
ts=$(date '+%F %T')
cd "$APP_DIR" || { bash "$NOTIFY" "[华灵运维] ⛔ AI 网关报表: cd $APP_DIR 失败 @ $ts" 2>/dev/null; exit 1; }

body=$(/usr/bin/node scripts/ai-gateway-report.mjs --md 2>>"$LOG")
code=$?
if [ "$code" -ne 0 ] || [ -z "$body" ]; then
  bash "$NOTIFY" "[华灵运维] ⛔ AI 网关报表生成失败 @ $ts · 退出码 $code（见 $LOG）" 2>/dev/null
  touch "$DOWNFLAG"
  exit "$code"
fi
echo "[$ts] $(/usr/bin/node scripts/ai-gateway-report.mjs 2>/dev/null)" >> "$LOG"
bash "$NOTIFY" "📊 日报 · 成本 · AI 网关耗时
$body
#ai_cost_report" 2>/dev/null
if [ -f "$DOWNFLAG" ]; then
  bash "$NOTIFY" "[华灵运维] ✅ AI 网关报表已恢复正常 @ $ts" 2>/dev/null
  rm -f "$DOWNFLAG"
fi
exit 0
