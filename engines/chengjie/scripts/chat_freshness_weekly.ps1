# 聊天新鲜度周批（2026-08-03）——计划任务 ChatFreshnessWeekly 每周日 07:20。
# 跑 7 天窗口新鲜度报告（分条采纳/语音履约/口头禅密度/复读榜/记忆回带）+
# 追加趋势行 logs/eval/chat_freshness_trend.jsonl（周审看 08-02 体检修复批的走势）。
# 与 proactive_review_weekly.ps1 同模式：UTF-8 显式（PS5 *>> 混写 UTF-16 坑）。
$ErrorActionPreference = "Continue"
$engine = Split-Path -Parent $PSScriptRoot
Set-Location $engine
$env:PYTHONIOENCODING = "utf-8"
$logDir = Join-Path $engine "logs\eval"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir "chat_freshness_weekly.log"
$stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
"=== chat freshness weekly @ $stamp ===" | Out-File -Append -Encoding utf8 $log
python tools\chat_freshness_report.py --days 7 --out-jsonl "logs\eval\chat_freshness_trend.jsonl" 2>&1 |
    Out-File -Append -Encoding utf8 $log
# 日志超 5MB 轮转一份（保留一代，与轻量周批日志体量匹配）
if ((Get-Item $log -ErrorAction SilentlyContinue).Length -gt 5MB) {
    Move-Item -Force $log "$log.1"
}
