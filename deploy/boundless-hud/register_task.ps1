# Register the sentinel minute-task on THIS machine (runs remotely via deploy_hud.ps1).
# Executed inside PowerShell so schtasks argument quoting is sane (ssh->cmd eats nested quotes).
# v3 (2026-08-07): launch via wscript run_hidden.vbs. A bare powershell.exe action in an
# INTERACTIVE minute-task creates a visible conhost window on every run (even with
# -WindowStyle Hidden the console exists first) -> fleet-wide "popup every minute"
# complaint. wscript.exe is a GUI host: child console is born hidden (SW_HIDE), same
# user session so wallpaper APIs still work. Must stay interactive-token (wallpaper
# writes HKCU + SystemParametersInfo of the logged-on user) - do NOT move to SYSTEM.
# ASCII-ONLY source.
[CmdletBinding()]
param(
  [string]$TaskName = 'BoundlessHudSentinel'
)
$ErrorActionPreference = 'Stop'
$Base = 'C:\Users\Public\boundless-hud'
$vbs = Join-Path $Base 'run_hidden.vbs'
if (-not (Test-Path $vbs)) { throw ('missing ' + $vbs + ' (deploy must copy it alongside sentinel.ps1)') }
# SysWOW64\wscript.exe: present on every 64-bit box AND the only copy left on hardened
# machines where System32\wscript.exe was removed (117). Because the host is 32-bit,
# reference PowerShell via Sysnative (the real 64-bit System32).
$cmd = ('C:\Windows\SysWOW64\wscript.exe //B //Nologo ' + $vbs +
  ' C:\Windows\Sysnative\WindowsPowerShell\v1.0\powershell.exe' +
  ' -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File ' +
  (Join-Path $Base 'sentinel.ps1'))
schtasks /create /f /tn $TaskName /sc minute /mo 1 /tr $cmd | Out-Null
# hard execution cap: a wedged run (e.g. nvidia-smi hang) must self-clear, otherwise the
# minute task refuses to overlap and the desktop board silently freezes (2026-08-06 proof)
try {
  $set = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 3) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew
  Set-ScheduledTask -TaskName $TaskName -Settings $set | Out-Null
} catch {
  Write-Output ("TASK_SETTINGS_WARN " + $_.Exception.Message)
}
$q = schtasks /query /tn $TaskName /fo LIST /v 2>$null | Select-String -Pattern 'Logon Mode|Task To Run|Status'
Write-Output ("TASK_OK " + (($q | ForEach-Object { ($_.Line -replace '\s+', ' ').Trim() }) -join ' | '))
