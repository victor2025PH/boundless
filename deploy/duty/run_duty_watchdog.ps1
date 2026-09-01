# run_duty_watchdog.ps1 -- DutyGroupWatchdog scheduled-task entry (117).
# Every 10 min: any bug-group question unanswered >30 min -> alert @ai_zkw
# (boss, decided 0829; via tools/duty_alert.py bot->support-account-DM chain).
# Safety net for the duty session on 173 (blocking-SSH sentinel dies with the
# session; this task does not). Logs under deploy\duty\logs\, keep 10 days.
# ASCII-only (PS 5.1 GBK). Register:
#   schtasks /Create /TN DutyGroupWatchdog /SC MINUTE /MO 10 /F ^
#     /TR "powershell -ExecutionPolicy Bypass -File D:\boundless\deploy\duty\run_duty_watchdog.ps1"
$ErrorActionPreference = "SilentlyContinue"
$py  = "C:\Users\Administrator\AppData\Local\Programs\Python\Python313\python.exe"
$wd  = "D:\boundless\engines\chengjie"
$logDir = "D:\boundless\deploy\duty\logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$stamp = Get-Date -Format "yyyyMMdd"
$logFile = Join-Path $logDir "duty_watchdog_$stamp.log"
$ts = Get-Date -Format "s"
try {
  Set-Location $wd
  $out = & $py "tools\duty_watchdog.py" 2>&1 | Out-String
} catch {
  $out = "EXCEPTION: $_"
}
"[$ts]`n$out`n" | Out-File -FilePath $logFile -Append -Encoding UTF8
Get-ChildItem $logDir -Filter "duty_watchdog_*.log" | Sort-Object LastWriteTime -Desc |
  Select-Object -Skip 10 | Remove-Item -Force -ErrorAction SilentlyContinue
