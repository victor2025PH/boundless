# run_duty_callback_poll.ps1 -- DutyCallbackPoll scheduled-task entry (117).
# Every 5 min: pull bug-verify button callbacks from bd2026.cc and write them
# back into bug tickets (y=verified, n=confirmed + duty alert). User-visible
# feedback already happened at click time via the site webhook; this is only
# ledger sync. Logs under deploy\duty\logs\, keep 10. ASCII-only. Register:
#   schtasks /Create /TN DutyCallbackPoll /SC MINUTE /MO 5 /F ^
#     /TR "powershell -ExecutionPolicy Bypass -File D:\boundless\deploy\duty\run_duty_callback_poll.ps1"
$ErrorActionPreference = "SilentlyContinue"
$py  = "C:\Users\Administrator\AppData\Local\Programs\Python\Python313\python.exe"
$wd  = "D:\boundless\engines\chengjie"
$logDir = "D:\boundless\deploy\duty\logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$stamp = Get-Date -Format "yyyyMMdd"
$logFile = Join-Path $logDir "callback_poll_$stamp.log"
$ts = Get-Date -Format "s"
try {
  Set-Location $wd
  $out = & $py "tools\duty_callback_poll.py" 2>&1 | Out-String
} catch {
  $out = "EXCEPTION: $_"
}
"[$ts]`n$out`n" | Out-File -FilePath $logFile -Append -Encoding UTF8
Get-ChildItem $logDir -Filter "callback_poll_*.log" | Sort-Object LastWriteTime -Desc |
  Select-Object -Skip 10 | Remove-Item -Force -ErrorAction SilentlyContinue
