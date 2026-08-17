# AI 味验收周批（活人感 P1-9，2026-08-03）——每周六 07:40 计划任务 AiFlavorReviewWeekly。
# 跑 7 天窗口验收报告 + 追加趋势行 logs/eval/ai_flavor_trend.jsonl（周审看走势：
# 感叹词开场/反问率是否持续收敛、语音条长度是否达标、AI 质疑入站是否下降、
# 语音 vs 文字回复率）。与 proactive_review_weekly.ps1 同模式：UTF-8 显式。
$ErrorActionPreference = "Continue"
$engine = Split-Path -Parent $PSScriptRoot
Set-Location $engine
$env:PYTHONIOENCODING = "utf-8"
$logDir = Join-Path $engine "logs\eval"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir "ai_flavor_review_weekly.log"
$stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
"=== ai flavor review weekly @ $stamp ===" | Out-File -Append -Encoding utf8 $log
python -m scripts.ai_flavor_review --days 7 --out-jsonl "logs\eval\ai_flavor_trend.jsonl" 2>&1 |
    Out-File -Append -Encoding utf8 $log
# 日志超 5MB 轮转一份（保留一代，与轻量周批日志体量匹配）
if ((Get-Item $log -ErrorAction SilentlyContinue).Length -gt 5MB) {
    Move-Item -Force $log "$log.1"
}
