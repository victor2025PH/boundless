#!/bin/bash
# ai_triage_cron.sh — VPS 侧每日崩溃归因（AI 闭环自动化，2026-07-13 P3）。
# 直读 ingest 的 agg.sqlite，产当日归因报告 + 追加 tasks.jsonl（去重后派发给研发/agent）。
# 部署：放 /opt/avatarhub/，crontab 每日一条：
#   30 9 * * * /opt/avatarhub/ai_triage_cron.sh >> /var/log/ai_triage.log 2>&1
set -e
APP=/opt/avatarhub
DB="${AH_INGEST_DATA:-/home/ubuntu/avatarhub-telemetry}/agg.sqlite"
OUT="${AH_INGEST_DATA:-/home/ubuntu/avatarhub-telemetry}/triage"
mkdir -p "$OUT"
DAY=$(date +%Y%m%d)

# 用 ai_triage.py 直读 sqlite 出报告 + 任务流（HERE=项目根，源码定位需要项目树；VPS 上放 app 副本或跳过定位）
python3 "$APP/ai_triage.py" --sqlite "$DB" --emit-tasks || true

# 任务去重：把新任务（按 sig 去重）追加到 dispatched.jsonl，供研发/agent 消费；已派发的不重复推。
# ai_triage.py 的 OUT_DIR=<项目根>/logs/ai_triage；VPS 上 HERE=/opt（ai_triage.py 在 /opt/avatarhub/）。
TASKS="/opt/logs/ai_triage/tasks.jsonl"
[ -f "$TASKS" ] || TASKS="$APP/logs/ai_triage/tasks.jsonl"
DISP="$OUT/dispatched.jsonl"
touch "$DISP"
if [ -f "$TASKS" ]; then
  python3 - "$TASKS" "$DISP" <<'PY'
import json, sys
tasks, disp = sys.argv[1], sys.argv[2]
seen = set()
try:
    for ln in open(disp, encoding="utf-8"):
        try: seen.add(json.loads(ln).get("sig"))
        except Exception: pass
except FileNotFoundError:
    pass
new = 0
with open(disp, "a", encoding="utf-8") as out:
    for ln in open(tasks, encoding="utf-8"):
        try: t = json.loads(ln)
        except Exception: continue
        if t.get("sig") and t["sig"] not in seen:
            seen.add(t["sig"]); out.write(json.dumps(t, ensure_ascii=False) + "\n"); new += 1
print(f"[ai_triage_cron] {new} new tasks dispatched")
PY
fi
echo "[ai_triage_cron] done $DAY"
