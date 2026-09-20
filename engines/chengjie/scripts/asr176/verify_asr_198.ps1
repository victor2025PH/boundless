$ErrorActionPreference = 'Continue'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new()
Write-Output '=== AITR_ASR_198 ==='
$t = Get-ScheduledTask -TaskName 'AITR_ASR_198'
$i = $t | Get-ScheduledTaskInfo
Write-Output ("state=" + $t.State)
Write-Output ("limit=" + $t.Settings.ExecutionTimeLimit)
Write-Output ("lastRun=" + $i.LastRunTime)
Write-Output ("lastResult=" + $i.LastTaskResult)
Write-Output ("batteryDisallow=" + $t.Settings.DisallowStartIfOnBatteries)
Write-Output ("stopOnBattery=" + $t.Settings.StopIfGoingOnBatteries)
Write-Output '=== AITR_ASR_WATCHDOG ==='
$w = Get-ScheduledTask -TaskName 'AITR_ASR_WATCHDOG' -ErrorAction SilentlyContinue
if ($w) {
    $wi = $w | Get-ScheduledTaskInfo
    Write-Output ("state=" + $w.State)
    Write-Output ("limit=" + $w.Settings.ExecutionTimeLimit)
    Write-Output ("next=" + $wi.NextRunTime)
} else { Write-Output 'MISSING' }
Write-Output '=== listen 8765 ==='
netstat -ano | Select-String ':8765 ' | Select-Object -First 6
Write-Output '=== asr python ==='
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -match 'asr_server' } |
    ForEach-Object { Write-Output ("pid=" + $_.ProcessId + " " + $_.CommandLine.Substring(0, [Math]::Min(160, $_.CommandLine.Length))) }
Write-Output '=== gpu ==='
nvidia-smi --query-gpu=memory.used,memory.total,utilization.gpu --format=csv,noheader
