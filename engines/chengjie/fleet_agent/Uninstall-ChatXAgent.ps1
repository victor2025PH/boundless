# Uninstall-ChatXAgent.ps1 -- remove the scheduled task + program files. State dir is kept unless -PurgeState
# (node_key / machine_id live there; keeping it lets a reinstall reuse the enrollment).
#   powershell -ExecutionPolicy Bypass -File Uninstall-ChatXAgent.ps1 [-PurgeState]
[CmdletBinding()]
param(
  [string]$InstallDir = (Join-Path $env:ProgramFiles "ChatX Agent"),
  [switch]$PurgeState
)
$ErrorActionPreference = 'Continue'
function Say($m) { Write-Host "[chatx-agent] $m" }
$target = Join-Path $InstallDir "chatx-agent.exe"
$stateDir = Join-Path $env:ProgramData "ChatX\fleet"
if (Test-Path -LiteralPath $target) { & $target --state-dir $stateDir uninstall-service | Out-Null }
schtasks /Delete /TN "ChatX Fleet Agent" /F 2>$null | Out-Null
Get-Process -Name chatx-agent -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Milliseconds 800
if (Test-Path -LiteralPath $InstallDir) { Remove-Item -LiteralPath $InstallDir -Recurse -Force; Say "removed $InstallDir" }
if ($PurgeState -and (Test-Path -LiteralPath $stateDir)) { Remove-Item -LiteralPath $stateDir -Recurse -Force; Say "removed $stateDir" }
else { Say "kept $stateDir (use -PurgeState to delete enrollment)" }
Say "done"
