# run_seat_monitor.ps1 -- SeatLogMonitor 计划任务入口（本机 117）。
# 每 15 分钟巡检 deploy/machines.json 里 chatx_seat=true 机器的 ChatX backend.log，
# 有新真问题才投递运维群 tg-ywqz。
# 输出落 logs\seat_monitor\，保留最近 10 天。
$ErrorActionPreference = "SilentlyContinue"
# monitor 用 UTF-8 写 stdout；PS5.1 默认按 GBK 解码再以 UTF-8 落盘 → 日志乱码（2026-09-18 前
# 全是「鍧愬腑」体）。显式按 UTF-8 收 stdout。
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$env:PYTHONIOENCODING = "utf-8"
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
