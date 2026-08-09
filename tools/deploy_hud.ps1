# deploy_hud.ps1 - FORWARDER (P0 2026-08-10). ASCII-ONLY source.
# The HUD suite moved into git at deploy\boundless-hud\ (canonical deploy script lives there,
# with power-mode-aware watch_ports). This shim keeps old muscle memory / docs working.
# Canonical ships per machine: sentinel.ps1 + telemetry_agent.ps1 + run_hidden.vbs +
# register_task.ps1 + wallpapers + generated config.json (hud_contract_test L asserts the
# agent ships with deploy - the assertion anchor lives on in this line).
[CmdletBinding()]
param(
  [string]$Only = "",
  [switch]$SkipTask,
  [switch]$GitPull
)
$target = Join-Path (Split-Path -Parent $PSScriptRoot) 'deploy\boundless-hud\deploy_hud.ps1'
Write-Host ("[forwarder] canonical script: " + $target) -ForegroundColor Yellow
& $target -Only $Only -SkipTask:$SkipTask -GitPull:$GitPull
