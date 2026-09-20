# 案例×学习融合裁决周批（融合 P4，2026-08-16）——每周六 07:20 计划任务 FusionReviewWeekly。
# 跑采纳度裁决报告 + 追加趋势行 logs\eval\fusion_adoption_trend.jsonl。
# 存在意义：效果数据的地基 kb_query_log 只滚动保留 7 天——不做周快照，裁决日想看
# 趋势时历史已蒸发；ctdo_ 埋点纪元 2026-08-16，满 14 天（约 8/30）后本报告判词
# 自动从「样本不足」翻成可执行结论（撤/留/加码），照单执行即可。
# 与 proactive_review_weekly.ps1 同模式：UTF-8 显式（PS5 *>> 混写 UTF-16 坑）。
$ErrorActionPreference = "Continue"
$engine = Split-Path -Parent $PSScriptRoot
Set-Location $engine
$env:PYTHONIOENCODING = "utf-8"
$logDir = Join-Path $engine "logs\eval"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir "fusion_review_weekly.log"
$stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
"=== fusion adoption review weekly @ $stamp ===" | Out-File -Append -Encoding utf8 $log
python tools\fusion_adoption_report.py --days 30 --out-jsonl "logs\eval\fusion_adoption_trend.jsonl" 2>&1 |
    Out-File -Append -Encoding utf8 $log
# 日志超 5MB 轮转一份（保留一代，与轻量周批日志体量匹配）
if ((Get-Item $log -ErrorAction SilentlyContinue).Length -gt 5MB) {
    Move-Item -Force $log "$log.1"
}
