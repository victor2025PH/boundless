# smart-reply 分段耗时周批（2026-08-01）——每周六 07:20 计划任务 SmartReplyReportWeekly。
# 跑 7 天窗口分段报表 + 追加趋势行 logs/eval/smart_reply_trend.jsonl（周审看走势：
# gen/xlate 各占多少、p90 是否回落、fallback 路径占比是否异常）。
# 与 proactive_review_weekly.ps1 同模式：UTF-8 显式（PS5 *>> 混写 UTF-16 坑）。
$ErrorActionPreference = "Continue"
$engine = Split-Path -Parent $PSScriptRoot
Set-Location $engine
$env:PYTHONIOENCODING = "utf-8"
$logDir = Join-Path $engine "logs\eval"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir "smart_reply_report_weekly.log"
$stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
"=== smart-reply report weekly @ $stamp ===" | Out-File -Append -Encoding utf8 $log
python -m scripts.smart_reply_report --days 7 --out-jsonl "logs\eval\smart_reply_trend.jsonl" 2>&1 |
    Out-File -Append -Encoding utf8 $log
# 日志超 5MB 轮转一份（保留一代，与轻量周批日志体量匹配）
if ((Get-Item $log -ErrorAction SilentlyContinue).Length -gt 5MB) {
    Move-Item -Force $log "$log.1"
}
