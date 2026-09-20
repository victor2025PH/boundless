#!/usr/bin/env bash
# 官网「关键功能存活」哨兵 —— 防跨环境部署互相静默覆盖（2026-08-07 事故后建）。
#
# 背景：website 是「工作树即部署源」，多环境各自整树部署；某环境的树若缺了另一环境
# 未提交的功能，一次部署就把它 rsync 抹掉（今日实锤：托管 delivery 字段被覆盖 →
# 托管单被装机守护误签授权码，2 小时后才在演练中发现）。真正根治要功能进 git；
# 在那之前，本哨兵把「再次被覆盖」的发现窗口从数小时压到 ≤cron 周期（15 分钟）。
#
# 放在 app dir 之外（/home/ubuntu/ops/）：deploy 的 rsync --delete 只作用于 app dir，
# 碰不到这里 —— 任何环境部署后哨兵仍在，环境无关。只读 grep 线上树，零副作用。
# 去抖：仅在「缺失集合」变化时告警（避免每 15 分钟刷屏）；恢复补一条。
# 用法：feature-guard.sh          正常巡检（cron 入口，缺失即经本机 relay 发 TG）
#       feature-guard.sh --dry    只报不发不写状态（自检/验证用）
set -u
APP_DIR="${APP_DIR:-/home/ubuntu/yuntech}"
OPS_DIR="$(cd "$(dirname "$0")" && pwd)"
MANIFEST="${MANIFEST:-$OPS_DIR/feature-markers.txt}"
STATE="${STATE:-$OPS_DIR/feature-guard.state}"
DRY=0; [ "${1:-}" = "--dry" ] && DRY=1

[ -f "$MANIFEST" ] || { echo "manifest not found: $MANIFEST" >&2; exit 2; }

missing=()
while IFS='|' read -r rel sub label; do
  rel="$(echo "${rel:-}" | tr -d '\r')"
  [ -z "${rel// }" ] && continue
  case "$rel" in \#*) continue;; esac
  sub="$(echo "${sub:-}" | tr -d '\r')"
  label="$(echo "${label:-$rel}" | tr -d '\r')"
  f="$APP_DIR/$rel"
  if [ ! -f "$f" ]; then missing+=("$label [文件缺失:$rel]"); continue; fi
  if [ -n "${sub// }" ] && ! grep -qF -- "$sub" "$f"; then
    missing+=("$label [标记丢失:$rel]")
  fi
done < "$MANIFEST"

if [ "$DRY" = "1" ]; then
  if [ "${#missing[@]}" -gt 0 ]; then
    echo "[dry] MISSING (${#missing[@]}):"; printf '  - %s\n' "${missing[@]}"
  else echo "[dry] all present"; fi
  exit 0
fi

cur=""
[ "${#missing[@]}" -gt 0 ] && cur="$(printf '%s\n' "${missing[@]}" | sort | md5sum | cut -d' ' -f1)"
prev=""; [ -f "$STATE" ] && prev="$(tr -d '\n' < "$STATE" 2>/dev/null)"

send_alert() {
  local text="$1" key
  key="$(grep -E '^EVENT_INGEST_KEY=' "$APP_DIR/.env.local" 2>/dev/null | sed -E 's/^[^=]+=//; s/^"//; s/"$//' | tr -d '\r')"
  [ -z "$key" ] && { echo "no EVENT_INGEST_KEY, skip alert" >&2; return; }
  local payload
  payload="$(python3 -c 'import json,sys;print(json.dumps({"text":sys.argv[1],"source":"feature-guard@vps"}))' "$text")"
  curl -s -m 15 -X POST "http://127.0.0.1:3000/api/ops/alert" \
    -H 'Content-Type: application/json' -H "Authorization: Bearer $key" \
    -d "$payload" >/dev/null && echo "alerted" || echo "alert POST failed" >&2
}

if [ "${#missing[@]}" -gt 0 ]; then
  echo "$(date -Is) MISSING: ${missing[*]}"
  if [ "$cur" != "$prev" ]; then
    send_alert "🧩 官网关键功能缺失（疑似被部署覆盖，需核对 deploy 源树 / 从 git 恢复）：$(printf '%s；' "${missing[@]}")"
  fi
  echo "$cur" > "$STATE"
else
  echo "$(date -Is) all present"
  if [ -n "$prev" ]; then
    send_alert "✅ 官网关键功能已全部恢复（feature-guard 巡检转绿）"
  fi
  : > "$STATE"
fi
