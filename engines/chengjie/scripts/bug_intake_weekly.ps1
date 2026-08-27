# -*- coding: utf-8 -*-
# 报障群值守周读（BugIntakeWeekly 计划任务入口，2026-08-18）。
# 只读报表：近 7 天工单/分类/回访读数 + KB 补货建议，趋势行追加
# logs/eval/bug_intake_trend.jsonl；日志落 logs/eval/，保留 14 份。
# 值守零数据时 CLI 自己打印空态并 exit 0，不制造假红。
$ErrorActionPreference = "Continue"
$engine = Split-Path -Parent $PSScriptRoot
Set-Location $engine
$logDir = Join-Path $engine "logs\eval"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$stamp = Get-Date -Format "yyyyMMdd-HHmm"
$log = Join-Path $logDir "bug_intake_weekly_$stamp.log"

$env:PYTHONIOENCODING = "utf-8"
python tools\bug_intake_report.py --days 7 `
    --out-jsonl (Join-Path $logDir "bug_intake_trend.jsonl") `
    *> $log
$code = $LASTEXITCODE

# 轮转：只留最近 14 份周读日志
Get-ChildItem $logDir -Filter "bug_intake_weekly_*.log" |
    Sort-Object LastWriteTime -Descending |
    Select-Object -Skip 14 | Remove-Item -Force -ErrorAction SilentlyContinue

exit $code
