<#
  register_fe_patrol_task.ps1 - daily frontend patrol (fe_smoke + fe_interact).
  Runs fe_patrol_once.bat; failures go to alerts.py webhook. Hub offline -> SKIP.

  Usage:
    powershell -ExecutionPolicy Bypass -File register_fe_patrol_task.ps1
    powershell -ExecutionPolicy Bypass -File register_fe_patrol_task.ps1 -Hour 7 -Minute 30
    powershell -ExecutionPolicy Bypass -File register_fe_patrol_task.ps1 -Action status
    powershell -ExecutionPolicy Bypass -File register_fe_patrol_task.ps1 -Action remove
#>
param(
    [ValidateSet('install', 'remove', 'status')]
    [string]$Action = 'install',
    [int]$Hour = 6,
    [int]$Minute = 0
)

$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}

$TaskName = 'AvatarHub_FePatrol'
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$Once = Join-Path $Here 'fe_patrol_once.bat'

switch ($Action) {
    'remove' {
        try {
            Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
            Write-Host ('[OK] removed task: ' + $TaskName)
        }
        catch {
            Write-Host ('[!] task not found: ' + $TaskName)
        }
    }
    'status' {
        $t = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        if (-not $t) {
            Write-Host ('[!] not registered: ' + $TaskName)
            return
        }
        $info = Get-ScheduledTaskInfo -TaskName $TaskName
        Write-Host ('task: ' + $TaskName + '  state: ' + $t.State)
        Write-Host ("last: {0}  rc: {1}  next: {2}" -f `
                $info.LastRunTime, $info.LastTaskResult, $info.NextRunTime)
        Write-Host ('log: ' + (Join-Path $Here 'logs\fe_patrol_last.log'))
    }
    default {
        if (-not (Test-Path $Once)) { throw ('missing ' + $Once) }
        if ($Hour -lt 0 -or $Hour -gt 23) { throw 'Hour 0-23' }
        if ($Minute -lt 0 -or $Minute -gt 59) { throw 'Minute 0-59' }

        $at = Get-Date -Hour $Hour -Minute $Minute -Second 0
        if ($at -lt (Get-Date)) { $at = $at.AddDays(1) }

        # wscript (GUI) + tools\run_hidden.vbs: running the bat directly as the task
        # action flashes a console window on the interactive desktop; the vbs runs it
        # hidden, waits, and propagates the exit code.
        $Vbs = Join-Path $Here 'tools\run_hidden.vbs'
        if (-not (Test-Path $Vbs)) { throw ('missing ' + $Vbs) }
        $act = New-ScheduledTaskAction -Execute 'wscript.exe' `
            -Argument ('//B //Nologo "{0}" "{1}"' -f $Vbs, $Once) -WorkingDirectory $Here

        $trg = New-ScheduledTaskTrigger -Daily -At $at

        $set = New-ScheduledTaskSettingsSet -StartWhenAvailable `
            -MultipleInstances IgnoreNew `
            -ExecutionTimeLimit (New-TimeSpan -Minutes 15)

        Register-ScheduledTask -TaskName $TaskName -Action $act -Trigger $trg -Settings $set `
            -Description 'AvatarHub fe_patrol daily smoke+interact with alerts' `
            -Force | Out-Null

        Write-Host ('[OK] registered task: ' + $TaskName)
        Write-Host ('     daily at ' + $Hour.ToString('00') + ':' + $Minute.ToString('00') + '  bat: fe_patrol_once.bat')
        Write-Host '     status: register_fe_patrol_task.ps1 -Action status'
        Write-Host '     remove: register_fe_patrol_task.ps1 -Action remove'
    }
}
