# 主动触达验收周批（P4，2026-07-29）——每周六 07:10 计划任务 ProactiveReviewWeekly。
# 跑 7 天窗口验收报告 + 追加趋势行 logs/eval/proactive_trend.jsonl（周审看走势：
# 事故模板句是否持续收敛 / life_share 占比是否上来 / 各形态回复率）。
# 与 translation_eval_weekly.ps1 同模式：UTF-8 显式（PS5 *>> 混写 UTF-16 坑）。
$ErrorActionPreference = "Continue"
$engine = Split-Path -Parent $PSScriptRoot
Set-Location $engine
$env:PYTHONIOENCODING = "utf-8"
$logDir = Join-Path $engine "logs\eval"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir "proactive_review_weekly.log"
$stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
"=== proactive review weekly @ $stamp ===" | Out-File -Append -Encoding utf8 $log
python -m scripts.proactive_review --days 7 --out-jsonl "logs\eval\proactive_trend.jsonl" 2>&1 |
    Out-File -Append -Encoding utf8 $log
# 日志超 5MB 轮转一份（保留一代，与轻量周批日志体量匹配）
if ((Get-Item $log -ErrorAction SilentlyContinue).Length -gt 5MB) {
    Move-Item -Force $log "$log.1"
}
