# Token 钱包影子读数周批（实施50 P4，2026-08-21）——建议每周六 07:20 计划任务
# WalletShadowWeekly。跑 7 天窗口读数 + 追加趋势行 logs/eval/wallet_trend.jsonl
# （enforce 切换决策看走势：燃烧率/跑道/would_degrade/付费路径占比）。
# 与 proactive_review_weekly.ps1 同模式：UTF-8 显式（PS5 *>> 混写 UTF-16 坑）。
# 注册（未自动建，需人工决定）：
#   schtasks /Create /TN WalletShadowWeekly /SC WEEKLY /D SAT /ST 07:20 /F ^
#     /TR "powershell -ExecutionPolicy Bypass -File D:\boundless\engines\chengjie\scripts\wallet_report_weekly.ps1"
$ErrorActionPreference = "Continue"
$engine = Split-Path -Parent $PSScriptRoot
Set-Location $engine
$env:PYTHONIOENCODING = "utf-8"
$logDir = Join-Path $engine "logs\eval"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir "wallet_report_weekly.log"
$stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
"=== wallet shadow weekly @ $stamp ===" | Out-File -Append -Encoding utf8 $log
python tools\wallet_shadow_report.py --days 7 --out-jsonl "logs\eval\wallet_trend.jsonl" 2>&1 |
    Out-File -Append -Encoding utf8 $log
if ((Get-Item $log -ErrorAction SilentlyContinue).Length -gt 5MB) {
    Move-Item -Force $log "$log.1"
}
