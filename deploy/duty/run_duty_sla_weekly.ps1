# run_duty_sla_weekly.ps1 -- DutySlaWeekly scheduled-task entry (117).
# Every Sat 07:20: bug-group first-response SLA over the past 7 days ->
# trend row appended to logs\eval\duty_sla_trend.jsonl + summary to @ai_zkw
# (boss, decided 0829; via tools/duty_alert.py).
# Logs under deploy\duty\logs\, keep 10. ASCII-only (PS 5.1 GBK). Register:
#   schtasks /Create /TN DutySlaWeekly /SC WEEKLY /D SAT /ST 07:20 /F ^
#     /TR "powershell -ExecutionPolicy Bypass -File D:\boundless\deploy\duty\run_duty_sla_weekly.ps1"
$ErrorActionPreference = "SilentlyContinue"
$py  = "C:\Users\Administrator\AppData\Local\Programs\Python\Python313\python.exe"
$wd  = "D:\boundless\engines\chengjie"
$logDir = "D:\boundless\deploy\duty\logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$stamp = Get-Date -Format "yyyyMMdd"
$logFile = Join-Path $logDir "duty_sla_$stamp.log"
$ts = Get-Date -Format "s"
try {
  Set-Location $wd
  $out = & $py "tools\duty_sla_report.py" --days 7 --out-jsonl "logs\eval\duty_sla_trend.jsonl" --notify 2>&1 | Out-String
} catch {
  $out = "EXCEPTION: $_"
}
"[$ts]`n$out`n" | Out-File -FilePath $logFile -Append -Encoding UTF8
Get-ChildItem $logDir -Filter "duty_sla_*.log" | Sort-Object LastWriteTime -Desc |
  Select-Object -Skip 10 | Remove-Item -Force -ErrorAction SilentlyContinue
