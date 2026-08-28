# apply_chatx_hotpatch_node.ps1 -- apply a ChatX hotpatch zip on ONE machine.
#
# Runs ON THE TARGET (copied there by push_chatx_hotpatch.ps1, or by the in-app
# helper after a public download). Replaces selected files under the installed
# resources\ tree and writes resources\hotpatch.json. User data is never touched
# (same upgrade-self-heal contract as install_chatx_node.ps1).
#
# Hard rules:
#   - baseAppVersion must match the installed exe (else refuse)
#   - only allowlisted relative paths (asar / inject / sidecars / optional backend)
#   - sha256 of every file must match the manifest
#   - previous files land in resources\.hotpatch-bak\ so one rollback is always possible
#
# ASCII-only (PS 5.1 GBK lesson). Match the app by install path, never CJK name.
# Exit: 0 ok / 2 bad zip or manifest / 3 version mismatch / 4 apply failed
#       5 health/relaunch not confirmed (files may already be in place)
[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)][string]$Zip,
  [string]$Manifest = "",
  # Optional: exact install dir. The in-app helper passes it because NSIS allows
  # picking a custom directory -- the probe list below would then patch the wrong
  # copy (or refuse). Left empty, behaviour is exactly the old probe order.
  [string]$InstallDir = "",
  [switch]$Relaunch,
  [switch]$SkipHealth,
  [switch]$DryRun
)

$ErrorActionPreference = 'Continue'
function Say($m) { Write-Output ("[hotapply] " + $m) }

function AllowedRel([string]$rel) {
  $p = ("$rel" -replace '\\', '/').TrimStart('/')
  if (-not $p) { return $false }
  if ($p -match '\.\.|:' ) { return $false }
  if ($p -match '(?i)ffmpeg') { return $false }
  return [bool](
    $p -eq 'app.asar' -or
    $p -eq 'build-info.json' -or
    $p -eq 'hotpatch.json' -or
    $p.StartsWith('app.asar.unpacked/') -or
    $p.StartsWith('shared/') -or
    $p.StartsWith('services/') -or
    $p.StartsWith('seed-data/') -or
    $p.StartsWith('backend/')
  )
}

function NormSemver([string]$v) {
  if ("$v" -match '(\d+)\.(\d+)\.(\d+)') { return ($Matches[1] + '.' + $Matches[2] + '.' + $Matches[3]) }
  return ""
}

function VersionLooksLike([string]$got, [string]$expect) {
  $e = NormSemver $expect
  $g = NormSemver $got
  if ($e -and $g -and ($e -eq $g)) { return $true }
  if ($e -and ("$got" -like ("*" + $e + "*"))) { return $true }
  $parts = $e.Split('.')
  if ($parts.Count -eq 3) {
    $disp = $parts[0] + '.' + $parts[1] + $parts[2].PadLeft(2, '0')
    if ("$got" -like ("*" + $disp + "*")) { return $true }
  }
  return $false
}

function ShaOf([string]$path) {
  return (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLower()
}

$installDir = @(
  $InstallDir,
  (Join-Path $env:LOCALAPPDATA 'Programs\telegram-ai-desktop'),
  'C:\Program Files\telegram-ai-desktop',
  'C:\Program Files (x86)\telegram-ai-desktop'
) | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
if (-not $installDir) { Say "install dir missing"; exit 2 }
$resDir = Join-Path $installDir 'resources'
if (-not (Test-Path $resDir)) { Say "resources dir missing"; exit 2 }

$app = Get-ChildItem $installDir -Filter *.exe -ErrorAction SilentlyContinue |
  Where-Object { $_.Name -notlike 'Uninstall*' } | Select-Object -First 1
if (-not $app) { Say "app exe missing"; exit 2 }
$fileVer = [string]$app.VersionInfo.FileVersion
$prodVer = [string]$app.VersionInfo.ProductVersion
Say ("install=" + $installDir)
Say ("exe file=" + $fileVer + " product=" + $prodVer)

if (-not (Test-Path $Zip)) { Say ("zip not found: " + $Zip); exit 2 }
if (-not $Manifest) {
  $sib = [IO.Path]::ChangeExtension($Zip, '.json')
  if (Test-Path $sib) { $Manifest = $sib }
}
if (-not $Manifest -or -not (Test-Path $Manifest)) { Say "manifest json missing"; exit 2 }

try {
  $mani = Get-Content -LiteralPath $Manifest -Raw -Encoding UTF8 | ConvertFrom-Json
} catch { Say ("manifest parse failed: " + $_.Exception.Message); exit 2 }
if (-not $mani -or $mani.kind -ne 'chatx_hotpatch') { Say "manifest kind is not chatx_hotpatch"; exit 2 }
$base = [string]$mani.baseAppVersion
$patch = 0
try { $patch = [int]$mani.patch } catch { }
if (-not $base -or $patch -lt 1) { Say "manifest missing baseAppVersion/patch"; exit 2 }
Say ("patch=" + $base + "+p" + $patch + " files=" + @($mani.files).Count)

if (-not (VersionLooksLike $fileVer $base) -and -not (VersionLooksLike $prodVer $base)) {
  Say ("BASE VERSION MISMATCH expect=" + $base + " got file=" + $fileVer + " product=" + $prodVer)
  Say "this hotpatch is for a different installer; refuse (do a full publish or rebuild)"
  exit 3
}

$localManiPath = Join-Path $resDir 'hotpatch.json'
$localPatch = 0
if (Test-Path $localManiPath) {
  try {
    $localMani = Get-Content -LiteralPath $localManiPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $localPatch = [int]$localMani.patch
    if ([string]$localMani.baseAppVersion -ne $base) { $localPatch = 0 }
  } catch { $localPatch = 0 }
}
Say ("local patch=" + $localPatch)
if ($localPatch -gt $patch) {
  Say ("local patch is NEWER than incoming (p" + $localPatch + " > p" + $patch + "); refuse downgrade")
  exit 3
}
if ($localPatch -eq $patch) { Say "already at this patch; nothing to do"; exit 0 }

foreach ($f in @($mani.files)) {
  $rel = [string]$f.path
  if (-not (AllowedRel $rel)) { Say ("FORBIDDEN path in manifest: " + $rel); exit 2 }
}

$work = Join-Path $env:TEMP ("chatx-hotpatch-" + [guid]::NewGuid().ToString('N').Substring(0, 8))
New-Item -ItemType Directory -Force -Path $work | Out-Null
try {
  Add-Type -AssemblyName System.IO.Compression.FileSystem
  [IO.Compression.ZipFile]::ExtractToDirectory((Resolve-Path $Zip).Path, $work)
} catch {
  Say ("unzip failed: " + $_.Exception.Message)
  Remove-Item $work -Recurse -Force -ErrorAction SilentlyContinue
  exit 2
}

$fail = 0
$planned = @()
foreach ($f in @($mani.files)) {
  $rel = ([string]$f.path) -replace '/', '\'
  $src = Join-Path $work $rel
  if (-not (Test-Path -LiteralPath $src)) { Say ("MISSING in zip: " + $f.path); $fail = 1; continue }
  $got = ShaOf $src
  $expect = ([string]$f.sha256).ToLower()
  if ($expect -and $got -ne $expect) {
    Say ("SHA MISMATCH " + $f.path + " expect=" + $expect + " got=" + $got)
    $fail = 1
    continue
  }
  $planned += [pscustomobject]@{ Rel = $rel; Src = $src; Dst = (Join-Path $resDir $rel) }
}
if ($fail) {
  Say "zip contents failed verification"
  Remove-Item $work -Recurse -Force -ErrorAction SilentlyContinue
  exit 2
}

if ($DryRun) {
  foreach ($p in $planned) { Say ("dry-run would write: " + $p.Rel) }
  Say "dry-run: nothing touched"
  Remove-Item $work -Recurse -Force -ErrorAction SilentlyContinue
  exit 0
}

# --- stop app (asar / backend.exe are locked while running) -------------------
$stop = Join-Path $PSScriptRoot 'stop_chatx_node.ps1'
if (Test-Path $stop) {
  & powershell -ExecutionPolicy Bypass -File $stop | ForEach-Object { Say $_ }
} else {
  Get-Process -ErrorAction SilentlyContinue |
    Where-Object { $_.Path -and $_.Path -like '*telegram-ai-desktop*' } |
    Stop-Process -Force -ErrorAction SilentlyContinue
  Start-Sleep -Seconds 3
}

$bakRoot = Join-Path $resDir ('.hotpatch-bak\p' + $patch)
New-Item -ItemType Directory -Force -Path $bakRoot | Out-Null
$copied = @()
try {
  foreach ($p in $planned) {
    $dstDir = Split-Path $p.Dst -Parent
    if (-not (Test-Path $dstDir)) { New-Item -ItemType Directory -Force -Path $dstDir | Out-Null }
    if (Test-Path -LiteralPath $p.Dst) {
      $bak = Join-Path $bakRoot $p.Rel
      $bakDir = Split-Path $bak -Parent
      if (-not (Test-Path $bakDir)) { New-Item -ItemType Directory -Force -Path $bakDir | Out-Null }
      Copy-Item -LiteralPath $p.Dst -Destination $bak -Force
    }
    Copy-Item -LiteralPath $p.Src -Destination $p.Dst -Force
    $copied += $p
    Say ("wrote " + $p.Rel)
  }
  [IO.File]::WriteAllText($localManiPath, ($mani | ConvertTo-Json -Depth 8), [Text.UTF8Encoding]::new($false))
  Say ("wrote hotpatch.json " + $base + "+p" + $patch)
} catch {
  Say ("apply raised: " + $_.Exception.Message)
  foreach ($p in $copied) {
    $bak = Join-Path $bakRoot $p.Rel
    if (Test-Path -LiteralPath $bak) {
      Copy-Item -LiteralPath $bak -Destination $p.Dst -Force -ErrorAction SilentlyContinue
    }
  }
  Remove-Item $work -Recurse -Force -ErrorAction SilentlyContinue
  exit 4
}
Remove-Item $work -Recurse -Force -ErrorAction SilentlyContinue

if (-not $Relaunch) { Say "OK applied (no relaunch)"; exit 0 }

# NOT $relaunch: PowerShell variable names are case-insensitive, so that name is
# the [switch]$Relaunch parameter above, and assigning a String to it throws
# ArgumentTransformationMetadataException. Measured 2026-08-28 on the first real
# SSH push: files landed, hotpatch.json was written, and the relaunch never ran
# -- the seat was left patched but with no app running. Same family as the
# documented $ShellPid/$Pid collision; keep local names distinct from params.
$relaunchScript = Join-Path $PSScriptRoot 'relaunch_chatx_node.ps1'
if (Test-Path $relaunchScript) {
  & powershell -ExecutionPolicy Bypass -File $relaunchScript | ForEach-Object { Say $_ }
  if ($LASTEXITCODE -ne 0 -and $LASTEXITCODE -ne $null) {
    Say "relaunch not confirmed"
    if (-not $SkipHealth) { exit 5 }
  }
} else {
  Start-Process -FilePath $app.FullName -WorkingDirectory $installDir
  Start-Sleep -Seconds 8
}

if (-not $SkipHealth) {
  $alive = Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq $app.FullName }
  if (-not $alive) { Say "app process not visible after relaunch"; exit 5 }
  Say ("health: process pid=" + (($alive | ForEach-Object { $_.Id }) -join ','))
}
Say "OK"
exit 0
