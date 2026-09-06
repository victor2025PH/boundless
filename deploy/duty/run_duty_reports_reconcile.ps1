# run_duty_reports_reconcile.ps1 -- DutyReportsReconcile scheduled-task entry (117).
# Daily 08:00: every Cursor field-agent report under tmp_diag must hang on a ticket
# (<code>/ticket.txt / evidence ledger received.diag / ticket body mentions the code).
# Unlinked > 0 -> tools/duty_alert.py pings duty. Why it exists (L-7 B, 2026-09-06):
# 09-05 skuio uploaded 52 reports, duty hand-scripted tickets for 29, 28 were never
# ticketed nor answered -- the two ledgers had never been compared. Read-only: never
# writes the ticket DB, never posts to the bug group. Logs under deploy\duty\logs\,
# keep 14 days. ASCII-only (PS 5.1 GBK). Register (hidden runner, zero console flash):
#   schtasks /Create /TN DutyReportsReconcile /SC DAILY /ST 08:00 /F /RL HIGHEST ^
#     /TR "C:\Windows\SysWOW64\wscript.exe //B //Nologo \"D:\workspace\boundless\deploy\instances\run_hidden.vbs\" C:\Windows\Sysnative\WindowsPowerShell\v1.0\powershell.exe -NoProfile -ExecutionPolicy Bypass -File D:\boundless\deploy\duty\run_duty_reports_reconcile.ps1"
$ErrorActionPreference = "SilentlyContinue"
$py  = "C:\Users\Administrator\AppData\Local\Programs\Python\Python313\python.exe"
$wd  = "D:\boundless\engines\chengjie"
$logDir = "D:\boundless\deploy\duty\logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$stamp = Get-Date -Format "yyyyMMdd"
$logFile = Join-Path $logDir "duty_reports_reconcile_$stamp.log"
$ts = Get-Date -Format "s"
try {
  Set-Location $wd
  $env:PYTHONIOENCODING = "utf-8"
  $out = & $py "tools\duty_reconcile.py" --reports --alert --data-root D:\chengjie-instances\zhiliao\data 2>&1 | Out-String
  $rc = $LASTEXITCODE
} catch {
  $out = "EXCEPTION: $_"
  $rc = -1
}
"[$ts] rc=$rc`n$out`n" | Out-File -FilePath $logFile -Append -Encoding UTF8
Get-ChildItem $logDir -Filter "duty_reports_reconcile_*.log" | Sort-Object LastWriteTime -Desc |
  Select-Object -Skip 14 | Remove-Item -Force -ErrorAction SilentlyContinue
