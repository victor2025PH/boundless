$ErrorActionPreference = 'Stop'
$taskName = 'OllamaServe198'
$t = Get-ScheduledTask -TaskName $taskName
$before = [string]$t.Settings.ExecutionTimeLimit
$t.Settings.ExecutionTimeLimit = 'PT0S'
$t.Settings.DisallowStartIfOnBatteries = $false
$t.Settings.StopIfGoingOnBatteries = $false
Set-ScheduledTask -InputObject $t | Out-Null
$after = [string](Get-ScheduledTask -TaskName $taskName).Settings.ExecutionTimeLimit
Write-Output ("OllamaServe198 limit before=" + $before + " after=" + $after + " (no restart)")
