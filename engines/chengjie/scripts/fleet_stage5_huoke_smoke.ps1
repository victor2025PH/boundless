<#
.SYNOPSIS
  Fleet stage5 huoke-chain smoke (local pytest; no prod / no real WA / no push).

.DESCRIPTION
  - handoff token happy path (local merge + cross-db source)
  - four failures TokenNotFound/Expired/AlreadyConsumed/Revoked: log + class name only
  - commandbus stop enqueue -> pull -> ack; cancel same-phone queued
  - natural-flow missing click_id: gap report if no *.py hits (see docs section E)

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File engines/chengjie/scripts/fleet_stage5_huoke_smoke.ps1
#>
[CmdletBinding()]
param(
  [string]$EngineRoot = ""
)

$ErrorActionPreference = "Stop"

if (-not $EngineRoot) {
  $here = Split-Path -Parent $MyInvocation.MyCommand.Path
  $EngineRoot = (Resolve-Path (Join-Path $here "..")).Path
}

$tests = @(
  "tests/test_contacts_handoff.py",
  "tests/test_player_care_handoff.py",
  "tests/test_player_care_commandbus.py"
)

Write-Host ""
Write-Host "=== stage5 huoke smoke (pytest) ===" -ForegroundColor Cyan
Write-Host "EngineRoot=$EngineRoot"

Push-Location $EngineRoot
try {
  $env:PYTHONPATH = ""
  $arg = @("-m", "pytest") + $tests + @("-q", "-p", "no:cacheprovider", "--tb=short")
  & python @arg
  if ($LASTEXITCODE -ne 0) { throw "pytest failed exit=$LASTEXITCODE" }

  Write-Host ""
  Write-Host "=== natural-flow click_id gap check (*.py only) ===" -ForegroundColor Cyan
  $hits = Get-ChildItem -Path $EngineRoot -Recurse -Filter *.py -ErrorAction SilentlyContinue |
    Where-Object { $_.FullName -notmatch '\\(__pycache__|\.venv|build|dist|site-packages)\\' } |
    Select-String -Pattern "click_id" -ErrorAction SilentlyContinue |
    Select-Object -First 5
  if ($hits) {
    Write-Host "FOUND click_id in Python:" -ForegroundColor Yellow
    $hits | ForEach-Object { Write-Host ("  {0}:{1}" -f $_.Path, $_.LineNumber) }
  } else {
    Write-Host "GAP: no click_id in engines/chengjie *.py (optional click_id natural-flow not automated)." -ForegroundColor Yellow
    Write-Host "Suggest: inbound empty click_id + content_id/post_url still opens session; attribution must not bind account."
  }

  Write-Host ""
  Write-Host "STAGE5 HUOKE SMOKE PASS" -ForegroundColor Green
  Write-Host "docs: engines/chengjie/docs/FLEET_STAGE2_CONTRACT_SAMPLES.md section E"
} finally {
  Pop-Location
}
