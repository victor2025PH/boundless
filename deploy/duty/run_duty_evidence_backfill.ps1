# run_duty_evidence_backfill.ps1 -- DutyEvidenceBackfill scheduled-task entry (117).
# Every 30 min: (1) backfill bug_tickets.reporter_version from ticket bodies
# (duty loop v3 sec.C/G: callback wording must branch on reporter version);
# (2) print pending-evidence list with overdue tiers (30m/2h/24h) into the log
# so stale evidence requests surface without anyone remembering to check.
# Logs under deploy\duty\logs\, keep 10 days. ASCII-only (PS 5.1 GBK). Register:
#   schtasks /Create /TN DutyEvidenceBackfill /SC MINUTE /MO 30 /F ^
#     /TR "powershell -ExecutionPolicy Bypass -File D:\boundless\deploy\duty\run_duty_evidence_backfill.ps1"
$ErrorActionPreference = "SilentlyContinue"
$py  = "C:\Users\Administrator\AppData\Local\Programs\Python\Python313\python.exe"
$wd  = "D:\boundless\engines\chengjie"
$logDir = "D:\boundless\deploy\duty\logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$stamp = Get-Date -Format "yyyyMMdd"
$logFile = Join-Path $logDir "duty_evidence_$stamp.log"
$ts = Get-Date -Format "s"
$env:PYTHONIOENCODING = "utf-8"
try {
  Set-Location $wd
  $out = & $py "tools\duty_evidence.py" backfill 2>&1 | Out-String
  $out += & $py "tools\duty_evidence.py" list 2>&1 | Out-String
} catch {
  $out = "EXCEPTION: $_"
}
"[$ts]`n$out`n" | Out-File -FilePath $logFile -Append -Encoding UTF8
Get-ChildItem $logDir -Filter "duty_evidence_*.log" | Sort-Object LastWriteTime -Desc |
  Select-Object -Skip 10 | Remove-Item -Force -ErrorAction SilentlyContinue
