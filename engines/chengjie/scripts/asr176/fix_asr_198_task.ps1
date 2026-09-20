# One-shot: lift the 72h ExecutionTimeLimit that killed AITR_ASR_198, then restart.
# Root cause 2026-09-18: schtasks /Create default PT72H closed the console at
# 19:37 (started 2026-09-15 19:36) -> Intel Fortran "window-CLOSE event" abort.
# Run ON 192.168.0.198 (or: ssh asr198 powershell -NoProfile -File C:\aitr_asr\fix_asr_198_task.ps1)
$ErrorActionPreference = 'Stop'
$taskName = 'AITR_ASR_198'

$t = Get-ScheduledTask -TaskName $taskName
$before = [string]$t.Settings.ExecutionTimeLimit
$t.Settings.ExecutionTimeLimit = 'PT0S'
$t.Settings.DisallowStartIfOnBatteries = $false
$t.Settings.StopIfGoingOnBatteries = $false
$t.Settings.AllowHardTerminate = $true
Set-ScheduledTask -InputObject $t | Out-Null
$after = [string](Get-ScheduledTask -TaskName $taskName).Settings.ExecutionTimeLimit
Write-Output ("limit before=" + $before + " after=" + $after)

& "$PSScriptRoot\restart_asr_198.ps1"
