# relaunch_chatx_node.ps1 -- start the installed ChatX app in the CONSOLE user's
# desktop session, from an SSH session.
#
# Why the scheduled-task dance: a GUI process started directly from Windows
# OpenSSH lands in the SSH logon session -- it runs, but its window is invisible
# to the person sitting at the machine. A ONCE task created with /IT runs
# interactively in the console session of the same user, which is exactly what
# we want after a remote silent upgrade (install_chatx_node.ps1 kills the app;
# without this the operator comes back to a closed app and files a ticket).
#
# ASCII-only on purpose (PS 5.1 decodes BOM-less UTF-8 as GBK). The app exe name
# is CJK, so we resolve it by directory + wildcard, never by name.
# Exit: 0 relaunched / 1 task ran but process not seen (tell operator) / 2 no exe
[CmdletBinding()]
param([int]$WaitSec = 8)

$ErrorActionPreference = 'Continue'
function Say($m) { Write-Output ("[relaunch] " + $m) }

$dir = Join-Path $env:LOCALAPPDATA 'Programs\telegram-ai-desktop'
$app = Get-ChildItem $dir -Filter *.exe -ErrorAction SilentlyContinue |
  Where-Object { $_.Name -notlike 'Uninstall*' } | Select-Object -First 1
if (-not $app) { Say "app exe not found under $dir"; exit 2 }
Say ("app: " + $app.FullName)

$already = Get-Process -ErrorAction SilentlyContinue |
  Where-Object { $_.Path -eq $app.FullName }
if ($already) { Say ("already running pid=" + ($already.Id -join ',')); exit 0 }

schtasks /Delete /TN ChatXRelaunch /F 2>$null | Out-Null
# /IT = interactive: show in the console session. Task runs as the current user.
schtasks /Create /TN ChatXRelaunch /SC ONCE /ST 23:59 /F /IT /RL LIMITED `
  /TR ('"' + $app.FullName + '"') | Out-Null
schtasks /Run /TN ChatXRelaunch | Out-Null
Start-Sleep -Seconds $WaitSec
schtasks /Delete /TN ChatXRelaunch /F 2>$null | Out-Null

$p = Get-Process -ErrorAction SilentlyContinue |
  Where-Object { $_.Path -eq $app.FullName } | Select-Object -First 1
if ($p) { Say ("relaunched pid=" + $p.Id); exit 0 }
Say "task ran but app process not visible yet - operator may need to open it from the desktop shortcut"
exit 1
