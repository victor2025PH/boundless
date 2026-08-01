# 挽回效果周批（RH-P2，2026-08-01）——每周六 07:20 计划任务 WinbackReviewWeekly。
# 跑 30 天挽回漏斗 + 本周/上周环比 + 追加趋势行 logs/eval/winback_trend.jsonl
# （周审看走势：挽回率是否稳定 / 发送量与回话量的配比）。只读统计，随时可跑。
# 与 proactive_review_weekly.ps1 同模式：UTF-8 显式（PS5 *>> 混写 UTF-16 坑）。
$ErrorActionPreference = "Continue"
$engine = Split-Path -Parent $PSScriptRoot
Set-Location $engine
$env:PYTHONIOENCODING = "utf-8"
$logDir = Join-Path $engine "logs\eval"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir "winback_report_weekly.log"
$stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
"=== winback report weekly @ $stamp ===" | Out-File -Append -Encoding utf8 $log
python -m scripts.winback_report --days 30 --out-jsonl "logs\eval\winback_trend.jsonl" 2>&1 |
    Out-File -Append -Encoding utf8 $log
# 日志超 5MB 轮转一份（保留一代，与轻量周批日志体量匹配）
if ((Get-Item $log -ErrorAction SilentlyContinue).Length -gt 5MB) {
    Move-Item -Force $log "$log.1"
}
