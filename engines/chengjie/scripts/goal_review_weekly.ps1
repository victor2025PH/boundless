# 营销目标验收周批（P27，2026-08-05）——建议每周六 07:20 计划任务 GoalReviewWeekly
# （与 ProactiveReviewWeekly 07:10 错峰）。跑 7 天窗口报告 + 追加趋势行
# logs/eval/goal_trend.jsonl（周审看走势：模板成功率 / 拍漏斗 / 摸底采集速度 /
# suggest vs auto 档分布——「要不要开 bridge」的决策读数）。
# 与 proactive_review_weekly.ps1 同模式：UTF-8 显式（PS5 *>> 混写 UTF-16 坑）。
$ErrorActionPreference = "Continue"
$engine = Split-Path -Parent $PSScriptRoot
Set-Location $engine
$env:PYTHONIOENCODING = "utf-8"
$logDir = Join-Path $engine "logs\eval"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir "goal_review_weekly.log"
$stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
"=== goal review weekly @ $stamp ===" | Out-File -Append -Encoding utf8 $log
python -m scripts.goal_review --days 7 --out-jsonl "logs\eval\goal_trend.jsonl" 2>&1 |
    Out-File -Append -Encoding utf8 $log
# 日志超 5MB 轮转一份（保留一代，与轻量周批日志体量匹配）
if ((Get-Item $log -ErrorAction SilentlyContinue).Length -gt 5MB) {
    Move-Item -Force $log "$log.1"
}
