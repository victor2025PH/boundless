# verify_chatx_vs_local.ps1 -- after install, prove a node matches the local build
# (version via -ExpectVersion; was hardcoded 1.0.6 once and false-failed every
# later rollout). Runs ON THE TARGET. ASCII-only (PS 5.1 GBK).
# Exit 0 ok / 2 missing / 4 mismatch.
[CmdletBinding()]
param(
  [string]$ExpectVersion = '1.0.6',
  [string]$ExpectBackendSize = '',
  [int]$ExpectPersonas = -1,
  [int]$ExpectKb = -1,
  [int]$ExpectMediaRows = -1,
  [int]$ExpectAlbumFiles = -1,
  [int]$ExpectPrerendered = -1,
  [int]$ExpectVoiceRefs = -1
)
$ErrorActionPreference = 'Continue'
function Say($m) { Write-Output ("[verify] " + $m) }

$cands = @(
  (Join-Path $env:LOCALAPPDATA 'Programs\telegram-ai-desktop'),
  'C:\Program Files\telegram-ai-desktop'
)
$dir = $cands | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $dir) { Say "install dir missing"; exit 2 }

$fail = 0
$app = Get-ChildItem $dir -Filter *.exe | Where-Object { $_.Name -notlike 'Uninstall*' } | Select-Object -First 1
if (-not $app) { Say "app exe missing"; exit 2 }
$ver = [string]$app.VersionInfo.FileVersion
$prod = [string]$app.VersionInfo.ProductVersion
Say ("dir=" + $dir)
Say ("app=" + $app.Name + " file=" + $ver + " product=" + $prod)
# Honor -ExpectVersion (semver form), plus the displayVersion variant electron
# uses for ProductVersion (1.0.14 -> "1.014", 1.0.6 -> "1.006").
$rxV = [regex]::Escape($ExpectVersion)
$okVer = ($ver -match $rxV) -or ($prod -match $rxV)
if (-not $okVer) {
  $parts = $ExpectVersion.Split('.')
  if ($parts.Count -eq 3) {
    $disp = $parts[0] + '.' + $parts[1] + $parts[2].PadLeft(2, '0')
    $okVer = ($prod -match [regex]::Escape($disp)) -or ($ver -match [regex]::Escape($disp))
  }
}
if (-not $okVer) { Say ("VERSION MISMATCH expect=" + $ExpectVersion + " got file=" + $ver + " product=" + $prod); $fail++ }

$must = @(
  'resources\backend\backend.exe',
  'resources\seed-data\seed-manifest.json',
  'resources\services\whatsapp-baileys\package.json',
  'resources\services\messenger-web\package.json'
)
foreach ($rel in $must) {
  $p = Join-Path $dir $rel
  $ok = Test-Path $p
  Say ("  " + $(if ($ok) { "OK  " } else { "MISS" }) + " " + $rel)
  if (-not $ok) { $fail++ }
}

$be = Join-Path $dir 'resources\backend\backend.exe'
if (Test-Path $be) {
  $sz = (Get-Item $be).Length
  Say ("backend_size=" + $sz)
  if ($ExpectBackendSize -and ([int64]$ExpectBackendSize -ne $sz)) {
    Say ("BACKEND SIZE MISMATCH expect=" + $ExpectBackendSize + " got=" + $sz)
    $fail++
  }
}

$mani = Join-Path $dir 'resources\seed-data\seed-manifest.json'
if (Test-Path $mani) {
  $raw = Get-Content $mani -Raw -Encoding UTF8
  Say ("seed_manifest=" + (($raw -replace '\s+', ' ').Trim()))
  try {
    $got = $raw | ConvertFrom-Json
    $g = if ($got.counts) { $got.counts } else { $got }
    $checks = @{
      personas           = $ExpectPersonas
      kb_entries         = $ExpectKb
      media_rows         = $ExpectMediaRows
      album_files        = $ExpectAlbumFiles
      prerendered_files  = $ExpectPrerendered
      voice_ref_files    = $ExpectVoiceRefs
    }
    foreach ($k in $checks.Keys) {
      $exp = [int]$checks[$k]
      if ($exp -lt 0) { continue }
      $actual = [int]$g.$k
      if ($actual -ne $exp) {
        Say ("SEED KEY MISMATCH $k expect=$exp got=$actual")
        $fail++
      } else {
        Say ("  OK   seed.$k=$actual")
      }
    }
  } catch {
    Say ("seed parse failed: " + $_.Exception.Message)
    $fail++
  }
}

$userDir = Join-Path $env:LOCALAPPDATA 'Programs\telegram-ai-desktop'
$machineDir = 'C:\Program Files\telegram-ai-desktop'
if ((Test-Path $userDir) -and (Test-Path $machineDir)) {
  Say "DUAL INSTALL: both per-user and Program Files present"
  $fail++
} else {
  Say "single install location ok"
}

if ($fail -gt 0) { Say ("FAIL checks=" + $fail); exit 4 }
Say ("OK - node matches expected " + $ExpectVersion + " package shape")
exit 0
