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

# Leftover dirs with no/broken registry entry still cause dual-install smell
# (verify_chatx_vs_local flags both per-user + Program Files). Wipe empties.
foreach ($orphan in @(
  (Join-Path $env:LOCALAPPDATA 'Programs\telegram-ai-desktop'),
  'C:\Program Files\telegram-ai-desktop',
  'C:\Program Files (x86)\telegram-ai-desktop'
)) {
  if (-not (Test-Path $orphan)) { continue }
  $alive = @(Get-ChildItem $orphan -Filter *.exe -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -notlike 'Uninstall*' })
  if ($alive.Count -eq 0) {
    Say ("removing empty leftover dir: " + $orphan)
    Remove-Item $orphan -Recurse -Force -ErrorAction SilentlyContinue
  } elseif (-not ($found | Where-Object { $_.InstallLocation -like ($orphan + '*') })) {
    # Still has binaries but no registry entry we uninstalled -- force wipe
    Say ("force-removing orphan install (no registry): " + $orphan)
    Get-Process -ErrorAction SilentlyContinue |
      Where-Object { $_.Path -and $_.Path -like ($orphan + '*') } |
      Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2
    Remove-Item $orphan -Recurse -Force -ErrorAction SilentlyContinue
  }
}

# --- 4. install ------------------------------------------------------------
# Retry once. MEASURED on .198 / .104 / .173 (2026-07-31): the first silent run
# often dies with 0xC0000005 (-1073741819) seconds in, and the very next attempt
# -- same file, same args -- succeeds. 3/3 hosts, every time.
#
# Event log names the faulting module: System.dll (the NSIS System plugin, whose
# job is generating call thunks at runtime) at offset 0x1581. Ruled out by
# measurement: disk space, Smart App Control (off), Exploit Protection (all
# NOTSET), Defender detections (none for this file), and "left over from the
# uninstall" -- .173 crashed on a run with nothing to uninstall. Do NOT delete
# this retry because an install looked fine once.
#
# 2026-08-09, .173 (Win11 build 26200) measured, in this order:
#   run A: 4/4 silent attempts from the SSH session crashed -> deploy failed
#   probe: same file, same /S, run in the CONSOLE session (task with /IT) -> OK 1st try
#   run B: silent from the SSH session again -> crashed twice, 3rd attempt OK
# So the crash rate is much higher on Win11 than on the Win10 nodes (.198/.104,
# build 19045, where attempt 1 or 2 always wins), but it is still a flake, NOT a
# hard "SSH session cannot install" rule -- run B disproves that. The console
# session is therefore a last-resort extra life, not the explanation. Root cause
# remains inside the third-party stub; do not rewrite these notes as a diagnosis
# without new measurement.
if ($found) { Start-Sleep -Seconds 8 }
$code = $null
for ($attempt = 1; $attempt -le 4; $attempt++) {
  Say ("installing (silent), attempt $attempt ...")
  $p = Start-Process -FilePath $Setup -ArgumentList '/S' -Wait -PassThru
  $code = $p.ExitCode
  Say ("installer exit=" + $code)
  if ($code -eq 0) { break }
  if ($attempt -lt 4) {
    Say "  attempt crashed (known NSIS System.dll flake) -- retrying in 10s"
    Start-Sleep -Seconds 10
  }
}
$cands = @(
  (Join-Path $env:LOCALAPPDATA 'Programs\telegram-ai-desktop'),
  'C:\Program Files\telegram-ai-desktop'
)
function Get-InstalledApp {
  foreach ($d in $cands) {
    if (-not (Test-Path $d)) { continue }
    $e = Get-ChildItem $d -Filter *.exe -ErrorAction SilentlyContinue |
      Where-Object { $_.Name -notlike 'Uninstall*' } | Select-Object -First 1
    if ($e) { return $e }
  }
  return $null
}

# --- 4b. console-session fallback (see note above) -------------------------
# Last resort after the silent retries are spent: a ONCE task with /IT runs in the
# console session of the same user. Measured to install on the first try on .173
# right after 4 silent attempts had failed. No exit code comes back through
# schtasks, so poll for the app binary (NSIS unpacks ~1.5 GB) rather than trusting
# a single sleep. Verified as a standalone probe; this in-script branch has not yet
# been hit in a real deploy (run B recovered at attempt 3 before reaching it).
if ($code -ne 0) {
  $sess = (query session 2>&1) -join "`n"
  if ($sess -notmatch 'console') {
    Say "silent install failed and no console session exists -- giving up"
    exit 3
  }
  Say "silent install failed 4x -- retrying in the console session (see note)"
  schtasks /Delete /TN ChatXInstallFallback /F 2>$null | Out-Null
  schtasks /Create /TN ChatXInstallFallback /SC ONCE /ST 23:59 /F /IT /RL LIMITED `
    /TR ('"' + $Setup + '" /S') | Out-Null
  schtasks /Run /TN ChatXInstallFallback | Out-Null
  $deadline = (Get-Date).AddMinutes(4)
  while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 10
    if (Get-InstalledApp) { $code = 0; break }
    $still = Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq $Setup }
    if (-not $still) {
      # stub exited; give the last file moves a moment before calling it a loss
      Start-Sleep -Seconds 5
      if (Get-InstalledApp) { $code = 0 }
      break
    }
  }
  schtasks /Delete /TN ChatXInstallFallback /F 2>$null | Out-Null
  if ($code -eq 0) { Say "console-session install OK" }
}
if ($code -ne 0) { exit 3 }

# --- 5. verify -------------------------------------------------------------
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
