# _seat_edition_probe.ps1 -- which ChatX package is installed on this seat, and which
# update channel it will follow. Consumers: chatx_fleet_status.ps1 (edition column) and
# anyone ssh-ing in by hand. ASCII only, headless-safe, read-only.
#
# Why (L-5 / boss decision D-L1, 2026-09-06): K-5 shipped 1.0.74 by publishing the CLEAN
# build as the public latest.yml while the seats were pushed the SMART build. Machines
# that clicked "update" silently turned into clean installs (no resources/seed-data ->
# every seed-borne "factory-on" default fell through). Nobody could see that from the
# version column alone: both packages are "1.0.74". This probe makes the two facts that
# matter first-class:
#   flavor  = what is INSTALLED   : resources/build-info.json .flavor (internal|clean|lite,
#             shipped since 1.0.24); cross-checked with resources/seed-data presence.
#   channel = what it will FOLLOW : resources/app-update.yml (electron-updater's runtime
#             config) -> internal when channel=latest-internal / url ...downloads/internal/,
#             public when it points at the plain downloads/ root.
# Output (one line):
#   "EDITION flavor=<internal|clean|lite|?> seed=<1|0> channel=<internal|public|?> ver=<x.y.z|?>"
$ErrorActionPreference = 'SilentlyContinue'
$res = Join-Path $env:LOCALAPPDATA 'Programs\telegram-ai-desktop\resources'
if (-not (Test-Path $res)) { Write-Output 'EDITION flavor=? seed=0 channel=? ver=? (not installed)'; exit 0 }

$flavor = '?'
$ver = '?'
try {
  $bi = Get-Content (Join-Path $res 'build-info.json') -Raw -Encoding UTF8 | ConvertFrom-Json
  if ($bi.flavor) { $flavor = [string]$bi.flavor }
  if ($bi.version) { $ver = [string]$bi.version }
} catch { }

$seed = 0
if (Test-Path (Join-Path $res 'seed-data\seed-manifest.json')) { $seed = 1 }
# Pre-1.0.24 packages have no build-info.json: derive flavor from the seed dir.
if ($flavor -eq '?') { if ($seed -eq 1) { $flavor = 'internal' } else { $flavor = 'clean' } }

$channel = '?'
try {
  $au = Get-Content (Join-Path $res 'app-update.yml') -Raw -Encoding UTF8
  if ($au) {
    if ($au -match '(?m)^\s*channel:\s*latest-internal\s*$' -or $au -match '/downloads/internal/') { $channel = 'internal' }
    elseif ($au -match '(?m)^\s*url:\s*\S+') { $channel = 'public' }
  }
} catch { }

Write-Output ('EDITION flavor=' + $flavor + ' seed=' + $seed + ' channel=' + $channel + ' ver=' + $ver)
