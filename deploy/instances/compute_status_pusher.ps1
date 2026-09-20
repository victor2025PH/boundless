# compute_status_pusher.ps1 — 算力看板推送器守护（实施70 2026-08-27）
# 由计划任务 Boundless-compute-pusher 每分钟拉起；python 侧 127.0.0.1:18797 单例锁
# 保证同刻只有一个循环在跑（重复拉起=秒退，零副作用）。日志 5MB 轮转一份。
$ErrorActionPreference = "SilentlyContinue"
$script = "D:\boundless\engines\chengjie\tools\compute_status_report.py"
$log = "D:\chengjie-instances\.ops\compute_pusher.log"
if ((Test-Path $log) -and ((Get-Item $log).Length -gt 5MB)) { Move-Item $log "$log.1" -Force }
$env:PYTHONIOENCODING = "utf-8"
& python $script >> $log 2>&1
