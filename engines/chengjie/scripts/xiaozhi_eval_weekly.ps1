# xiaozhi_eval_weekly.ps1 - weekly xiaozhi self-iteration batch (boss order 2026-09-01).
# Runs: (1) gap report from production qa_log (read-only) -> "what corpus to add" list;
#       (2) dialog regression eval (real corpus + real LLM + real route) -> trend jsonl.
# Output: UTF-8 log under logs\eval\xiaozhi_weekly\, keep newest 14.
# Exit: non-zero when the golden-set eval has misses (external alerting can hook this).
# ASCII-only on purpose: PS 5.1 decodes scripts as GBK; CJK comments break syntax.

$ErrorActionPreference = "Continue"
$engineRoot = Split-Path -Parent $PSScriptRoot
Set-Location $engineRoot

$logDir = Join-Path $engineRoot "logs\eval\xiaozhi_weekly"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$stamp = Get-Date -Format "yyyyMMdd-HHmm"
$logFile = Join-Path $logDir "xiaozhi_weekly_$stamp.log"

$env:PYTHONIOENCODING = "utf-8"

"=== xiaozhi weekly eval @ $stamp ===" | Out-File $logFile -Encoding utf8

"--- [1/2] corpus gap report (last 7 days, production qa_log, read-only) ---" |
    Out-File $logFile -Encoding utf8 -Append
python -X utf8 tools\xiaozhi_gap_report.py --days 7 --limit 20 2>&1 |
    Out-File $logFile -Encoding utf8 -Append

"--- [2/2] dialog regression eval (golden set, real LLM, trend appended) ---" |
    Out-File $logFile -Encoding utf8 -Append
python -X utf8 tools\xiaozhi_dialog_eval.py 2>&1 |
    Out-File $logFile -Encoding utf8 -Append
$evalExit = $LASTEXITCODE

# retention: keep newest 14 logs
Get-ChildItem $logDir -Filter "xiaozhi_weekly_*.log" |
    Sort-Object LastWriteTime -Descending |
    Select-Object -Skip 14 | Remove-Item -Force -ErrorAction SilentlyContinue

"=== done, eval exit=$evalExit ===" | Out-File $logFile -Encoding utf8 -Append
exit $evalExit
