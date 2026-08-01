# install_chatx_node.ps1 -- install/upgrade the ChatX desktop app on ONE machine.
#
# Runs ON THE TARGET machine (copied there by push_chatx.ps1, or run by hand).
# ASCII-only on purpose: PS 5.1 decodes BOM-less UTF-8 as GBK and mangles CJK,
# which is exactly how watchdog_emotion_tts.ps1 got burned. The app itself is
# named with CJK chars, so we NEVER match it by name -- we match by install path
# (telegram-ai-desktop, the npm package name electron-builder uses for the dir).
#
# What it does, in order:
#   1. verify the setup exe (exists + optional sha256 match -- a truncated scp is
#      the single most likely failure here, and NSIS fails confusingly on it)
#   2. stop any running app (an NSIS upgrade over a running app leaves locked files)
#   3. uninstall previous installs found in the registry (BOTH hives: some nodes
#      have an old per-machine install under C:\Program Files while the current
#      build ships per-user -- without this you end up with two copies and two
#      shortcuts, the stale one still auto-starting)
#   4. install silently (/S)
#   5. report where it landed + version, so the caller can verify
#
# Exit codes: 0 ok / 2 bad setup file / 3 install failed / 4 post-install missing
[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)][string]$Setup,
  [string]$ExpectSha256 = "",
  [switch]$KeepSetup
)

$ErrorActionPreference = 'Stop'
function Say($m) { Write-Output ("[chatx] " + $m) }

# --- 1. verify installer ---------------------------------------------------
if (-not (Test-Path $Setup)) { Say "setup not found: $Setup"; exit 2 }
$sz = [math]::Round((Get-Item $Setup).Length / 1MB, 1)
Say "setup: $Setup ($sz MB)"
if ($ExpectSha256) {
  $got = (Get-FileHash $Setup -Algorithm SHA256).Hash
  if ($got -ne $ExpectSha256.ToUpper()) {
    Say "sha256 MISMATCH expected=$ExpectSha256 got=$got"
    exit 2
  }
  Say "sha256 ok"
}

# --- 2. stop running app ---------------------------------------------------
# Match by executable path, not process name (the name is CJK).
$running = Get-Process -ErrorAction SilentlyContinue |
  Where-Object { $_.Path -and $_.Path -like '*telegram-ai-desktop*' }
if ($running) {
  Say ("stopping running app: " + ($running.Id -join ','))
  $running | Stop-Process -Force -ErrorAction SilentlyContinue
  Start-Sleep -Seconds 3
} else {
  Say "app not running"
}
# The bundled backend/sidecars are separate processes; leaving one alive holds a
# file lock on resources/ and makes the install silently partial.
Get-Process -ErrorAction SilentlyContinue |
  Where-Object { $_.Path -and $_.Path -like '*telegram-ai-desktop*' } |
  Stop-Process -Force -ErrorAction SilentlyContinue

# --- 3. uninstall previous installs (both hives) ---------------------------
$keys = @(
  'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*',
  'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*',
  'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*'
)
$found = @()
foreach ($k in $keys) {
  $found += Get-ItemProperty $k -ErrorAction SilentlyContinue |
    Where-Object {
      ($_.InstallLocation -and $_.InstallLocation -like '*telegram-ai-desktop*') -or
      ($_.UninstallString -and $_.UninstallString -like '*telegram-ai-desktop*')
    }
}
foreach ($app in $found) {
  $loc = $app.InstallLocation
  Say ("uninstalling previous: " + $loc + " (" + $app.DisplayVersion + ")")
  $u = $app.QuietUninstallString
  if (-not $u) { $u = $app.UninstallString + ' /S' }
  # UninstallString looks like: "C:\path\Uninstall app.exe" /S  -> split exe from args
  $m = [regex]::Match($u, '^\s*"([^"]+)"\s*(.*)$')
  if ($m.Success) { $exe = $m.Groups[1].Value; $rest = $m.Groups[2].Value }
  else { $exe = $u; $rest = '' }
  if (-not (Test-Path $exe)) { Say "  uninstaller missing, skip"; continue }
  # _?= keeps NSIS synchronous. Without it NSIS copies itself to %TEMP% and
  # returns IMMEDIATELY -- the installer then races the still-running uninstaller
  # over the same files and dies with 0xC0000005 (measured on .198, 2026-07-31).
  # InstallLocation is empty in some of these registry entries, so derive the dir
  # from the uninstaller path instead of trusting the value.
  if (-not $loc) { $loc = Split-Path $exe -Parent }
  try {
    Start-Process -FilePath $exe -ArgumentList @('/S', ("_?=" + $loc)) -Wait -PassThru | Out-Null
  } catch { Say ("  uninstall raised (ignored): " + $_.Exception.Message) }
  # Belt and braces: even with _?= the elevated child can outlive the parent.
  # Wait for the app binaries to actually go away before touching the disk again.
  for ($i = 0; $i -lt 30; $i++) {
    $left = @(Get-ChildItem $loc -Filter *.exe -ErrorAction SilentlyContinue |
      Where-Object { $_.Name -notlike 'Uninstall*' })
    if ($left.Count -eq 0) { break }
    Start-Sleep -Seconds 1
  }
  Say ("  previous removed: " + (-not (Test-Path (Join-Path $loc 'resources'))))
}
if (-not $found) { Say "no previous install registered" }

# --- 4. install ------------------------------------------------------------
# Retry once. MEASURED on .198 / .104 / .173 (2026-07-31): the first silent run
# often dies with 0xC0000005 (-1073741819) seconds in, and the very next attempt
# -- same file, same args -- succeeds. 3/3 hosts, every time.
#
# Event log names the faulting module: System.dll (the NSIS System plugin, whose
# job is generating call thunks at runtime) at offset 0x1581. Ruled out by
# measurement: disk space, Smart App Control (off), Exploit Protection (all
# NOTSET), Defender detections (none for this file), and "left over from the
# uninstall" -- .173 crashed on a run with nothing to uninstall. Root cause is
# still open; it lives inside a third-party installer stub, so absorb it here
# rather than pretend to explain it. Do NOT delete this retry because an install
# looked fine once.
if ($found) { Start-Sleep -Seconds 8 }
$code = $null
for ($attempt = 1; $attempt -le 2; $attempt++) {
  Say ("installing (silent), attempt $attempt ...")
  $p = Start-Process -FilePath $Setup -ArgumentList '/S' -Wait -PassThru
  $code = $p.ExitCode
  Say ("installer exit=" + $code)
  if ($code -eq 0) { break }
  if ($attempt -lt 2) {
    Say "  first attempt crashed (known NSIS System.dll flake) -- retrying in 10s"
    Start-Sleep -Seconds 10
  }
}
if ($code -ne 0) { exit 3 }

# --- 5. verify -------------------------------------------------------------
$cands = @(
  (Join-Path $env:LOCALAPPDATA 'Programs\telegram-ai-desktop'),
  'C:\Program Files\telegram-ai-desktop'
)
$dir = $cands | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $dir) { Say "post-install: install dir NOT found"; exit 4 }
$app = Get-ChildItem $dir -Filter *.exe |
  Where-Object { $_.Name -notlike 'Uninstall*' } | Select-Object -First 1
if (-not $app) { Say "post-install: app exe NOT found in $dir"; exit 4 }
Say ("installed at: " + $dir)
Say ("version: " + $app.VersionInfo.ProductVersion + " (file " + $app.VersionInfo.FileVersion + ")")
$res = Join-Path $dir 'resources\backend\backend.exe'
Say ("backend bundled: " + (Test-Path $res))
if (-not $KeepSetup) { Remove-Item $Setup -Force -ErrorAction SilentlyContinue }
Say "OK"
exit 0
