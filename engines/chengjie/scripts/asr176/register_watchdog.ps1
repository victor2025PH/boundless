# Register AITR_ASR_WATCHDOG on this host (every 5 min, SYSTEM).
# Run ON the ASR machine after watchdog_asr.ps1 is in C:\aitr_asr\.
$ErrorActionPreference = 'Stop'
$wd = 'C:\aitr_asr\watchdog_asr.ps1'
if (-not (Test-Path $wd)) { throw "missing $wd" }
$tr = "powershell -NoProfile -ExecutionPolicy Bypass -File $wd"
schtasks /Create /F /TN 'AITR_ASR_WATCHDOG' /SC MINUTE /MO 5 /RU SYSTEM /RL HIGHEST /TR $tr | Out-Null
$t = Get-ScheduledTask -TaskName 'AITR_ASR_WATCHDOG'
$t.Settings.ExecutionTimeLimit = 'PT5M'
$t.Settings.DisallowStartIfOnBatteries = $false
$t.Settings.StopIfGoingOnBatteries = $false
$t.Settings.StartWhenAvailable = $true
Set-ScheduledTask -InputObject $t | Out-Null
Write-Output 'AITR_ASR_WATCHDOG registered (every 5 min)'
Get-ScheduledTask -TaskName 'AITR_ASR_WATCHDOG' | Get-ScheduledTaskInfo |
    Select-Object TaskName, LastRunTime, LastTaskResult, NextRunTime | Format-List
