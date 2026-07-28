# 获客增长环周审：抓实例的就绪/校准/流失×成交，渲染快照 + 追加趋势 JSONL。
param([string[]]$Bases = @("http://127.0.0.1:18799"))   # tongyi 已退役；多实例时传数组
# 注册：schtasks /Create /TN GrowthReviewWeekly /SC WEEKLY /D MON /ST 09:00 /F ^
#   /TR "powershell -ExecutionPolicy Bypass -File d:\workspace\telegram-mtproto-ai\scripts\growth_review_weekly.ps1"
# 实例没起时 CLI 如实记「读取失败」，不编数字；趋势缺点即可见。
$ErrorActionPreference = "Continue"
Set-Location (Split-Path $PSScriptRoot -Parent)
$env:PYTHONIOENCODING = "utf-8"
$trend = "logs/goals/growth_trend.jsonl"
$log = "logs/goals/weekly_review.log"
New-Item -ItemType Directory -Force -Path "logs/goals" | Out-Null
if (-not $env:AITR_WEB_TOKEN) { $env:AITR_WEB_TOKEN = "admin" }

$ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
Add-Content -Path $log -Value "[$ts] growth review start"

foreach ($base in $Bases) {
    python -m scripts.growth_review --base $base --days 30 --out-jsonl $trend |
        Out-File -FilePath $log -Encoding utf8 -Append
}

$ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
Add-Content -Path $log -Value "[$ts] growth review done"
