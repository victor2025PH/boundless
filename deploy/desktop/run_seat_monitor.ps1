# run_seat_monitor.ps1 -- SeatLogMonitor 计划任务入口（本机 117）。
# 每 15 分钟巡检 198/104 的 ChatX backend.log，有新真问题才投递 @Sousaun。
# 输出落 logs\seat_monitor\，保留最近 200KB * 5 份。ASCII 注释（PS5.1 GBK 稳）。
$ErrorActionPreference = "SilentlyContinue"
$py  = "C:\Users\Administrator\AppData\Local\Programs\Python\Python313\python.exe"
$mon = "D:\boundless\deploy\desktop\monitor_seat_logs.py"
$logDir = "D:\boundless\deploy\desktop\logs\seat_monitor"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$stamp = Get-Date -Format "yyyyMMdd"
$logFile = Join-Path $logDir "monitor_$stamp.log"
$ts = Get-Date -Format "s"
try {
  $out = & $py $mon 2>&1 | Out-String
} catch {
  $out = "EXCEPTION: $_"
}
"[$ts]`n$out`n" | Out-File -FilePath $logFile -Append -Encoding UTF8
# 轮转：保留最近 10 天
Get-ChildItem $logDir -Filter "monitor_*.log" | Sort-Object LastWriteTime -Desc |
  Select-Object -Skip 10 | Remove-Item -Force -ErrorAction SilentlyContinue
