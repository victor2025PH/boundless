# push_chatx.ps1 -- ship the ChatX desktop installer to ONE node over SSH, verify,
# optionally install + relaunch + smoke. Runs on the BUILD machine (117).
#
# This scriptizes the 2026-07-31 manual flow that shipped 1.0.1 to .198:
#   stage (scp + sha256 both ends) -> install (silent, kills running app) ->
#   relaunch (interactive, console session) -> smoke (throwaway data dir).
# Default is STAGE-ONLY: -Install is the explicit, disruptive step (it stops the
# operator's running app), so it never happens by accident.
#
# ASCII-only on purpose: PS 5.1 decodes BOM-less UTF-8 as GBK and mangles CJK
# (the watchdog_emotion_tts.ps1 lesson). The app exe/product name is CJK, so
# nothing here ever matches by display name -- only by install path.
#
# Usage:
#   powershell -File deploy\desktop\push_chatx.ps1 -TargetSsh zhituo                # stage only
#   powershell -File deploy\desktop\push_chatx.ps1 -TargetSsh zhituo -Install -Relaunch -Smoke
#
# Exit: 0 ok / 2 no setup found / 3 stage or hash verify failed / 4 install failed
#       5 smoke failed (install may still be fine; read the output)
[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)][string]$TargetSsh,
  [string]$Setup = "",
  [string]$StageDir = 'C:\Users\Administrator\Downloads\chatx',
  [switch]$Install,
  [switch]$Relaunch,
  [switch]$Smoke,
  [switch]$VerifyLocal
)

$ErrorActionPreference = 'Stop'
function Say($m) { Write-Output ("[push] " + $m) }

$here = Split-Path -Parent $MyInvocation.MyCommand.Path            # deploy\desktop
$repo = Split-Path -Parent (Split-Path -Parent $here)              # repo root

# --- 1. resolve installer -----------------------------------------------------
if (-not $Setup) {
  $dist = Join-Path $repo 'engines\chengjie\desktop\dist'
  $cand = Get-ChildItem $dist -Filter 'ChatX-Setup-*.exe' -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1
  if ($cand) { $Setup = $cand.FullName }
}
if (-not $Setup -or -not (Test-Path $Setup)) { Say "no installer found (build with: npm run dist:win)"; exit 2 }
$sz = [math]::Round((Get-Item $Setup).Length / 1MB, 1)
$sha = (Get-FileHash $Setup -Algorithm SHA256).Hash
Say ("setup: {0} ({1} MB)" -f (Split-Path -Leaf $Setup), $sz)
Say ("sha256: " + $sha)

# --- 2. stage: scripts + installer, then verify the hash on the far end -------
$leaf = Split-Path -Leaf $Setup
$stageFwd = $StageDir.Replace('\', '/')
ssh $TargetSsh "mkdir `"$StageDir`" 2>nul & echo staged-dir-ok" | Out-Null
foreach ($f in @('install_chatx_node.ps1', 'smoke_chatx_node.ps1', 'relaunch_chatx_node.ps1', 'verify_chatx_vs_local.ps1')) {
  scp -q (Join-Path $here $f) ("{0}:{1}/{2}" -f $TargetSsh, $stageFwd, $f)
}
Say "uploading installer ..."
scp -q $Setup ("{0}:{1}/{2}" -f $TargetSsh, $stageFwd, $leaf)
# certutil output: line 1 header, line 2 lowercase hash, line 3 footer
$remote = (ssh $TargetSsh "certutil -hashfile $StageDir\$leaf SHA256")[1]
if (-not $remote -or ($remote.Trim().ToUpper() -ne $sha)) {
  Say ("hash MISMATCH after upload: remote=" + $remote); exit 3
}
Say "staged + hash verified on target"

if (-not $Install) {
  Say "stage-only done. To install (stops the running app!):"
  Say ("  ssh {0} powershell -ExecutionPolicy Bypass -File {1}\install_chatx_node.ps1 -Setup {1}\{2} -ExpectSha256 {3}" -f $TargetSsh, $StageDir, $leaf, $sha)
  exit 0
}

# --- 3. install (disruptive: stops the app; installer has its own retry) ------
Say "installing on target (this stops the running app) ..."
ssh $TargetSsh "powershell -ExecutionPolicy Bypass -File $StageDir\install_chatx_node.ps1 -Setup $StageDir\$leaf -ExpectSha256 $sha -KeepSetup"
if ($LASTEXITCODE -ne 0) { Say ("install FAILED exit=" + $LASTEXITCODE); exit 4 }

# --- 4. relaunch in the console session (operator sees the app come back) -----
if ($Relaunch) {
  ssh $TargetSsh "powershell -ExecutionPolicy Bypass -File $StageDir\relaunch_chatx_node.ps1"
  if ($LASTEXITCODE -ne 0) { Say "relaunch did not confirm; operator may need the desktop shortcut" }
}

# --- 5. smoke the INSTALLED build (throwaway data dir; never touches real data)
if ($Smoke) {
  ssh $TargetSsh "powershell -ExecutionPolicy Bypass -File $StageDir\smoke_chatx_node.ps1"
  if ($LASTEXITCODE -ne 0) { Say ("smoke FAILED exit=" + $LASTEXITCODE); exit 5 }
}

# --- 6. shape/version/seed match vs local gold (optional) ----------------------
if ($VerifyLocal) {
  $beSize = ''
  $beLocal = Join-Path $repo 'engines\chengjie\desktop\dist\win-unpacked\resources\backend\backend.exe'
  if (Test-Path $beLocal) { $beSize = [string](Get-Item $beLocal).Length }
  $seedPath = Join-Path $repo 'engines\chengjie\desktop\build\seed-data\seed-manifest.json'
  $p = $kb = $mr = $af = $pr = $vr = -1
  if (Test-Path $seedPath) {
    $c = (Get-Content $seedPath -Raw -Encoding UTF8 | ConvertFrom-Json).counts
    $p = [int]$c.personas; $kb = [int]$c.kb_entries; $mr = [int]$c.media_rows
    $af = [int]$c.album_files; $pr = [int]$c.prerendered_files; $vr = [int]$c.voice_ref_files
  }
  Say "verifying shape vs local gold ..."
  # Expected version comes from the installer we just shipped (was hardcoded 1.0.6
  # once; every later rollout would false-fail the verify step).
  $expVer = ''
  if ($leaf -match 'ChatX-Setup-([0-9\.]+)\.exe') { $expVer = $Matches[1] }
  $vcmd = "powershell -ExecutionPolicy Bypass -File $StageDir\verify_chatx_vs_local.ps1"
  if ($expVer) { $vcmd += " -ExpectVersion $expVer" }
  if ($beSize) { $vcmd += " -ExpectBackendSize $beSize" }
  if ($p -ge 0) {
    $vcmd += " -ExpectPersonas $p -ExpectKb $kb -ExpectMediaRows $mr -ExpectAlbumFiles $af -ExpectPrerendered $pr -ExpectVoiceRefs $vr"
  }
  ssh $TargetSsh $vcmd
  if ($LASTEXITCODE -ne 0) { Say ("verify FAILED exit=" + $LASTEXITCODE); exit 6 }
}

Say "OK"
exit 0
