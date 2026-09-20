# publish_gate.ps1 — before touching production instances: should you restart?
# Thin wrapper around restart_instance -Advise for both products.
# Exit: 0 = no restart needed (hot-only / clean) or advice printed; 1 = must_restart dirty.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File deploy\instances\publish_gate.ps1
#   powershell -ExecutionPolicy Bypass -File deploy\instances\publish_gate.ps1 -Instance zhiliao

[CmdletBinding()]
param(
    [ValidateSet('zhiliao', 'tongyi', 'both')]
    [string]$Instance = 'both'
)

$ErrorActionPreference = 'Stop'
$script = Join-Path $PSScriptRoot 'restart_instance.ps1'
$targets = if ($Instance -eq 'both') { @('zhiliao', 'tongyi') } else { @($Instance) }
$code = 0
foreach ($id in $targets) {
    Write-Host ""
    Write-Host "======== publish gate: $id ========" -ForegroundColor Cyan
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $script -Instance $id -Advise
    if ($LASTEXITCODE -ne 0) { $code = 1 }
}
Write-Host ""
Write-Host "Hint: templates/i18n/config.local overlay = refresh browser, do NOT restart." -ForegroundColor DarkGray
Write-Host "Hint: business .py changes = one restart_instance per product, never restart_main.ps1." -ForegroundColor DarkGray
Write-Host "Hint: if cooldown active, wait or use -Force only for true emergencies." -ForegroundColor DarkGray
exit $code
