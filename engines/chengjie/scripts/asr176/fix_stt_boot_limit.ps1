# Inspect STT_Boot 72h trap on 140 and lift it. Does not restart STT unless
# the process is already dead. Run ON 192.168.0.140:
#   powershell -NoProfile -ExecutionPolicy Bypass -File C:\temp\fix_stt_boot_limit.ps1
$ErrorActionPreference = 'Continue'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new()

Write-Output '=== STT_Boot ==='
$t = Get-ScheduledTask -TaskName 'STT_Boot' -ErrorAction SilentlyContinue
if (-not $t) { Write-Output 'STT_Boot missing'; exit 1 }
$info = $t | Get-ScheduledTaskInfo
Write-Output ("state=" + $t.State)
Write-Output ("limit=" + $t.Settings.ExecutionTimeLimit)
Write-Output ("lastRun=" + $info.LastRunTime)
Write-Output ("lastResult=" + $info.LastTaskResult)
Write-Output ("batteryDisallow=" + $t.Settings.DisallowStartIfOnBatteries)
Write-Output ("stopOnBattery=" + $t.Settings.StopIfGoingOnBatteries)

$limit = [string]$t.Settings.ExecutionTimeLimit
$needsLift = ($limit -and $limit -ne 'PT0S' -and $limit -ne '00:00:00')
if ($needsLift) {
    $t.Settings.ExecutionTimeLimit = 'PT0S'
    $t.Settings.DisallowStartIfOnBatteries = $false
    $t.Settings.StopIfGoingOnBatteries = $false
    Set-ScheduledTask -InputObject $t | Out-Null
    $after = [string](Get-ScheduledTask -TaskName 'STT_Boot').Settings.ExecutionTimeLimit
    Write-Output ("lifted STT_Boot ExecutionTimeLimit " + $limit + " -> " + $after)
} else {
    Write-Output 'STT_Boot already unlimited'
}

Write-Output '=== listen 7854 ==='
netstat -ano | Select-String ':7854' | Select-Object -First 5
Write-Output '=== gpu apps ==='
nvidia-smi --query-compute-apps=pid,process_name,used_memory --format=csv
Write-Output '=== local health ==='
try {
    $h = Invoke-WebRequest -Uri 'http://127.0.0.1:7854/health' -TimeoutSec 8 -UseBasicParsing
    Write-Output ("health " + $h.StatusCode + " " + $h.Content.Substring(0, [Math]::Min(240, $h.Content.Length)))
} catch {
    Write-Output ("health FAIL " + $_.Exception.Message)
}
