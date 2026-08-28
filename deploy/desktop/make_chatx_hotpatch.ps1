# make_chatx_hotpatch.ps1 -- build a ChatX hotpatch zip WITHOUT an NSIS installer.
#
# Runs on the BUILD machine (117). Default path is the fast one:
#   copy-shared + write-build-info + electron-builder --win dir
# That produces dist\win-unpacked (identical asar + extraResources to a full
# ship) but skips the 400+ MB NSIS setup. Then we zip only the allowlisted
# resources files and write a manifest next to it.
#
# PAYLOAD IS OPT-IN BY AREA, and that is the whole point of the feature.
# Default = app.asar + build-info.json + shared\ (inject) + app.asar.unpacked\
# ~= 5 MB, which covers the common case (a shell/UI/renderer fix lives in the
# asar). The heavy areas are switches because they are NOT small:
#   services\  = 4700 files / 518 MB raw (sidecar node_modules) -> ~226 MB zip
#   backend\   = PyInstaller output
# Shipping those by default made a one-line JS fix cost half an installer,
# i.e. exactly the wait this channel exists to remove (measured 2026-08-27).
#
# So: a shell/renderer fix needs no switch. Changed shared\inject? It is in by
# default. Touched services\* (messenger-web / whatsapp-baileys)? add
# -IncludeServices -- and note that at ~226 MB vs a 476 MB full installer the
# saving is thin, so consider a normal release instead. Python side changed?
# npm run build:backend first, then -IncludeBackend.
# The run prints which areas went in and which were left out -- read that line
# before shipping: a patch that swaps the asar while its sidecar stays behind
# is a version-mismatched runtime, worse than not patching at all.
#
#   powershell -File make_chatx_hotpatch.ps1 -Fixes B117 -NotesFile notes.txt
#   powershell -File make_chatx_hotpatch.ps1 -SkipDirBuild -FromUnpacked <path> ...
#
# The user-facing notes are Chinese, but pass them via -NotesFile (read as UTF8),
# not -Notes on the command line: a PS 5.1 console hands CJK argv over as GBK and
# the text can land mangled in the manifest that users actually read.
#
# ASCII-only (PS 5.1 GBK). Exit: 0 ok / 2 bad input / 3 build failed / 4 pack failed
[CmdletBinding()]
param(
  [int]$Patch = 0,
  [string]$BaseVersion = "",
  [string[]]$Fixes = @(),
  [string]$Notes = "",
  [string]$NotesFile = "",
  [string]$NotesDev = "",
  [switch]$IncludeBackend,
  [switch]$IncludeSeed,
  [switch]$IncludeServices,
  [switch]$SkipDirBuild,
  [string]$FromUnpacked = "",
  [string]$DesktopDir = "D:\boundless\engines\chengjie\desktop",
  [string]$OutDir = ""
)

$ErrorActionPreference = 'Stop'
function Say($m) { Write-Output ("[hotmake] " + $m) }
function Fail($m, $code) { Say $m; exit $code }

function AllowedRel([string]$rel) {
  $p = ("$rel" -replace '\\', '/').TrimStart('/')
  if (-not $p) { return $false }
  if ($p -match '\.\.|:') { return $false }
  if ($p -match '(?i)ffmpeg') { return $false }
  return [bool](
    $p -eq 'app.asar' -or
    $p -eq 'build-info.json' -or
    $p.StartsWith('app.asar.unpacked/') -or
    $p.StartsWith('shared/') -or
    $p.StartsWith('services/') -or
    $p.StartsWith('seed-data/') -or
    $p.StartsWith('backend/')
  )
}

function RelUnder([string]$root, [string]$full) {
  # -LiteralPath is mandatory, not stylistic: Resolve-Path treats [ ] ? * as
  # wildcards, finds no match, returns null, and the next .StartsWith() throws
  # "cannot call a method on a null-valued expression" -- which aborts the whole
  # run AFTER electron-builder already spent its minutes. Measured 2026-08-28 on
  # the first real -IncludeBackend run: backend\_internal\docx\templates\
  # default-docx-template\[Content_Types].xml (python-docx ships it) is the one
  # file out of 2728 that has brackets. Same reason every other file touch in
  # this script is already -LiteralPath.
  $r = (Resolve-Path -LiteralPath $root).Path.TrimEnd('\')
  $f = (Resolve-Path -LiteralPath $full).Path
  if (-not $f.StartsWith($r, [StringComparison]::OrdinalIgnoreCase)) { return "" }
  return $f.Substring($r.Length).TrimStart('\').Replace('\', '/')
}

function ShaOf([string]$path) {
  return (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLower()
}

$Fixes = @($Fixes | ForEach-Object { "$_".Split(',') } | ForEach-Object { "$_".Trim() } | Where-Object { $_ })
if (-not $OutDir) { $OutDir = Join-Path $DesktopDir 'dist\hotpatch' }
$pkgPath = Join-Path $DesktopDir 'package.json'
if (-not (Test-Path $pkgPath)) { Fail ("package.json missing: " + $pkgPath) 2 }
$pkg = Get-Content -LiteralPath $pkgPath -Raw -Encoding UTF8 | ConvertFrom-Json
if (-not $BaseVersion) { $BaseVersion = [string]$pkg.version }
if ($BaseVersion -notmatch '^\d+\.\d+\.\d+$') { Fail ("base version not semver: " + $BaseVersion) 2 }

if (-not $Notes -and $NotesFile) {
  if (-not (Test-Path $NotesFile)) { Fail ("-NotesFile not found: " + $NotesFile) 2 }
  $Notes = (Get-Content -LiteralPath $NotesFile -Raw -Encoding UTF8).Trim()
}
# ASCII default on purpose (PS 5.1 GBK). Pass -Notes with the user-facing Chinese.
if (-not $Notes) { $Notes = "Stability and experience fixes. Takes effect after restart." }

$prevPtr = Join-Path $OutDir 'hotpatch.json'
if ($Patch -lt 1) {
  $Patch = 1
  if (Test-Path $prevPtr) {
    try {
      $prev = Get-Content -LiteralPath $prevPtr -Raw -Encoding UTF8 | ConvertFrom-Json
      if ([string]$prev.baseAppVersion -eq $BaseVersion) { $Patch = ([int]$prev.patch) + 1 }
    } catch { $Patch = 1 }
  }
}
Say ("base=" + $BaseVersion + " patch=p" + $Patch + " fixes=" + ($Fixes -join ','))

$unpacked = $FromUnpacked
if (-not $SkipDirBuild) {
  Push-Location $DesktopDir
  try {
    Say "copy-shared + write-build-info"
    node copy-shared.js
    if ($LASTEXITCODE -ne 0) { Fail "copy-shared failed" 3 }
    node build\write-build-info.js
    if ($LASTEXITCODE -ne 0) { Fail "write-build-info failed" 3 }
    Say "electron-builder --win dir (no NSIS installer)"
    npx --yes electron-builder --win dir
    if ($LASTEXITCODE -ne 0) { Fail "electron-builder dir failed" 3 }
  } finally { Pop-Location }
  $unpacked = Join-Path $DesktopDir 'dist\win-unpacked'
}
if (-not $unpacked -or -not (Test-Path $unpacked)) { Fail "win-unpacked missing (run without -SkipDirBuild, or pass -FromUnpacked)" 2 }
$res = Join-Path $unpacked 'resources'
if (-not (Test-Path $res)) { Fail ("resources missing under " + $unpacked) 2 }
$asar = Join-Path $res 'app.asar'
if (-not (Test-Path $asar)) { Fail "app.asar missing -- refusing to ship a partial patch" 2 }

$want = New-Object System.Collections.Generic.List[string]
$want.Add('app.asar')
if (Test-Path (Join-Path $res 'build-info.json')) { $want.Add('build-info.json') }
$dirs = @('shared', 'app.asar.unpacked')
if ($IncludeServices) { $dirs += 'services' }
if ($IncludeSeed) { $dirs += 'seed-data' }
if ($IncludeBackend) { $dirs += 'backend' }
$skipped = @('services', 'seed-data', 'backend') | Where-Object { $dirs -notcontains $_ }
Say ("areas in: app.asar," + ($dirs -join ',') + "  omitted: " + (($skipped -join ',') -replace '^$', 'none'))
if ($skipped -contains 'services') {
  Say "note: services\ omitted -- if this batch changed messenger-web/whatsapp-baileys, re-run with -IncludeServices"
}
foreach ($d in $dirs) {
  $root = Join-Path $res $d
  if (-not (Test-Path $root)) { continue }
  Get-ChildItem $root -Recurse -File -ErrorAction SilentlyContinue | ForEach-Object {
    $rel = RelUnder $res $_.FullName
    if (AllowedRel $rel) { $want.Add($rel) }
  }
}
$want = @($want | Select-Object -Unique)
if ($want.Count -lt 1) { Fail "no files selected" 4 }

$files = @()
foreach ($rel in $want) {
  $full = Join-Path $res ($rel -replace '/', '\')
  if (-not (Test-Path -LiteralPath $full)) { continue }
  $item = Get-Item -LiteralPath $full
  $files += [ordered]@{
    path   = $rel
    sha256 = ShaOf $full
    bytes  = [int64]$item.Length
  }
}

$git = [ordered]@{ commit = 'unknown'; branch = 'unknown'; dirty_count = -1 }
try {
  Push-Location (Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $DesktopDir)))
  $git.commit = (git rev-parse HEAD).Trim()
  $git.branch = (git rev-parse --abbrev-ref HEAD).Trim()
  $dirty = @(git status --porcelain -- engines/chengjie/desktop engines/chengjie/services engines/chengjie/src)
  $git.dirty_count = $dirty.Count
} catch { } finally { Pop-Location }

$id = $BaseVersion + '-p' + $Patch
$zipName = 'ChatX-Hotpatch-' + $id + '.zip'
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$stage = Join-Path $env:TEMP ('chatx-hotmake-' + $id)
if (Test-Path $stage) { Remove-Item $stage -Recurse -Force }
New-Item -ItemType Directory -Force -Path $stage | Out-Null
foreach ($f in $files) {
  $src = Join-Path $res (($f.path) -replace '/', '\')
  $dst = Join-Path $stage (($f.path) -replace '/', '\')
  $dstDir = Split-Path $dst -Parent
  if (-not (Test-Path -LiteralPath $dstDir)) {
    [IO.Directory]::CreateDirectory($dstDir) | Out-Null
  }
  # .NET copy, not Copy-Item: -Destination has no -Literal counterpart, so a
  # bracketed leaf name can still be read as a wildcard on the way out.
  [IO.File]::Copy($src, $dst, $true)
}

$zipPath = Join-Path $OutDir $zipName
if (Test-Path $zipPath) { Remove-Item $zipPath -Force }
Add-Type -AssemblyName System.IO.Compression.FileSystem
[IO.Compression.ZipFile]::CreateFromDirectory($stage, $zipPath)
Remove-Item $stage -Recurse -Force -ErrorAction SilentlyContinue
$zipSha = ShaOf $zipPath
$zipSize = (Get-Item $zipPath).Length
Say ("zip " + $zipName + " " + [math]::Round($zipSize / 1MB, 1) + " MB files=" + $files.Count)

$mani = [ordered]@{
  schema         = 1
  kind           = 'chatx_hotpatch'
  id             = $id
  baseAppVersion = $BaseVersion
  patch          = $Patch
  fixes          = @($Fixes)
  notes          = $Notes
  notes_dev      = $NotesDev
  created_at     = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
  created_on     = [string]$env:COMPUTERNAME
  git            = $git
  zip            = $zipName
  sha256         = $zipSha
  size_bytes     = [int64]$zipSize
  files          = $files
}
$maniPath = Join-Path $OutDir ($id + '.json')
$ptrPath = Join-Path $OutDir 'hotpatch.json'
$json = ($mani | ConvertTo-Json -Depth 8)
[IO.File]::WriteAllText($maniPath, $json, [Text.UTF8Encoding]::new($false))
[IO.File]::WriteAllText($ptrPath, $json, [Text.UTF8Encoding]::new($false))
Say ("manifest " + $maniPath)
Say ("pointer  " + $ptrPath)
Say ("OK " + $BaseVersion + "+p" + $Patch)
exit 0
