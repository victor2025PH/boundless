# run_duty_notify_backlog.ps1 -- DutyNotifyBacklog scheduled-task entry (117).
# Every 6 h: fixed tickets whose fix-notify was never sent (notify_ts=0) piling
# up (>=3 and oldest >=24h) -> alert duty via tools/duty_alert.py (12h debounce).
# Read-only: never sends to the bug group, never writes the ledger. Why it
# exists (I-6 F1, 2026-09-04): 61 fixed tickets sat un-notified from 08-30 to
# 09-04 because "mark fixed" via CLI/set_ticket_status never triggers the
# notify path and nothing scheduled ever looked at the backlog. Logs under
# deploy\duty\logs\, keep 10 days. ASCII-only (PS 5.1 GBK). Register:
#   schtasks /Create /TN DutyNotifyBacklog /SC HOURLY /MO 6 /F ^
#     /TR "powershell -ExecutionPolicy Bypass -File D:\boundless\deploy\duty\run_duty_notify_backlog.ps1"
$ErrorActionPreference = "SilentlyContinue"
$py  = "C:\Users\Administrator\AppData\Local\Programs\Python\Python313\python.exe"
$wd  = "D:\boundless\engines\chengjie"
$logDir = "D:\boundless\deploy\duty\logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$stamp = Get-Date -Format "yyyyMMdd"
$logFile = Join-Path $logDir "duty_notify_backlog_$stamp.log"
$ts = Get-Date -Format "s"
try {
  Set-Location $wd
  $env:PYTHONIOENCODING = "utf-8"
  $out = & $py "tools\duty_notify_pending.py" --alert 2>&1 | Out-String
} catch {
  $out = "EXCEPTION: $_"
}
"[$ts]`n$out`n" | Out-File -FilePath $logFile -Append -Encoding UTF8
Get-ChildItem $logDir -Filter "duty_notify_backlog_*.log" | Sort-Object LastWriteTime -Desc |
  Select-Object -Skip 10 | Remove-Item -Force -ErrorAction SilentlyContinue
