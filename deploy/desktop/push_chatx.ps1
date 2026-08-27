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
#   powershell -File deploy\desktop\push_chatx.ps1 -TargetSsh zhituo -FreshInstall -Smoke -Relaunch
#     ^ factory-fresh: backup(rename) data -> uninstall -> install. Codifies the
#       2026-08-11 4-node rollout order so nobody re-improvises it (and nobody
#       runs wipe AFTER backup again -- that used to eat the recovery copy).
#       Old data survives as %APPDATA%\<dir>_bak<stamp>; delete once node is healthy.
#   powershell -File deploy\desktop\push_chatx.ps1 -TargetSsh zhituo -FreshInstall -KeepAccounts -Relaunch -Smoke
#     ^ factory-fresh BUT the operator's logins survive: after the fresh install,
#       restore_chatx_accounts_node.ps1 copies the login/account artifacts
#       (web partitions, protocol sessions+registry, sidecar profiles, overlay)
#       back from the _bak dir. First-boot seeding only fills MISSING files, so
#       the restored logins are never overwritten. Chat dbs/caches stay fresh.
#
# Exit: 0 ok / 2 no setup found / 3 stage or hash verify failed / 4 install failed
#       5 smoke failed (install may still be fine; read the output)
#       7 fresh-install prep failed (backup rename or uninstall; data dir locked?)
#       8 target disk too low even after auto-remedy (old setups + stale scoped_dir temps)
#       9 account restore failed (recovery _bak dir is still intact on the node)
#      10 seat backend freshness verification failed (stale backend survived /
#         backend never booted / seat port silent -- see [seatverify] output)
[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)][string]$TargetSsh,
  [string]$Setup = "",
  [string]$StageDir = 'C:\Users\Administrator\Downloads\chatx',
  [switch]$Install,
  [switch]$Relaunch,
  [switch]$Smoke,
  [switch]$VerifyLocal,
  [switch]$FreshInstall,
  [switch]$KeepAccounts
)

$ErrorActionPreference = 'Stop'
function Say($m) { Write-Output ("[push] " + $m) }

if ($KeepAccounts -and -not $FreshInstall) {
  Say "-KeepAccounts only makes sense with -FreshInstall (plain -Install keeps ALL data already)"
  exit 2
}

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

# --- 1.5 disk preflight (2026-08-13 lesson: .198 was down to 2.5 GB free ------
# mid-rollout because every release had parked another 476 MB setup in the stage
# dir; the NSIS unpack needs ~2 GB on top of the upload). Fail fast + tell the
# operator what to clean instead of dying halfway through a 476 MB scp.
# 2026-08-17 upgrade: low disk now triggers an AUTO-REMEDY first. Real .198 root
# cause that day was NOT parked setups (0.5 GB) but stale Chromium scoped_dir*
# staging corpses in TEMP: 45 dirs / ~69 GB of dead webview download staging.
# Both hog classes are safe to clear unattended: setups keep only the version
# being pushed; scoped_dirs only when >24h stale (live sessions keep a fresh
# mtime; files locked by running processes silently survive Remove-Item).
# Still <3 GB after remedy -> honest exit 8 with a manual-inspection hint.
$freeRaw = ssh $TargetSsh 'powershell -NoProfile -Command "[math]::Round((Get-PSDrive C).Free/1GB,1)"'
$freeGb = 0.0
if (-not [double]::TryParse(("$freeRaw".Trim()), [ref]$freeGb)) { $freeGb = 0.0 }
Say ("target C: free = " + $freeGb + " GB")
if ($freeGb -lt 3.0) {
  Say "target disk low (<3 GB) -- auto-remedy: old setups + stale scoped_dir temps ..."
  $curLeaf = Split-Path -Leaf $Setup
  $remedy = 'powershell -NoProfile -Command "Get-ChildItem ''' + $StageDir + ''' -Filter ChatX-Setup-*.exe -ErrorAction SilentlyContinue | Where-Object Name -ne ''' + $curLeaf + ''' | Remove-Item -Force -ErrorAction SilentlyContinue; Get-ChildItem $env:TEMP -Directory -Filter scoped_dir* -ErrorAction SilentlyContinue | Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-1) } | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue; [math]::Round((Get-PSDrive C).Free/1GB,1)"'
  $freeRaw = ssh $TargetSsh $remedy
  $freeGb = 0.0
  if (-not [double]::TryParse(("$freeRaw".Trim()), [ref]$freeGb)) { $freeGb = 0.0 }
  Say ("target C: free after remedy = " + $freeGb + " GB")
}
if ($freeGb -lt 3.0) {
  Say "target disk still too low (<3 GB) after auto-remedy. Inspect the big hogs manually, e.g.:"
  Say ("  ssh {0} powershell -NoProfile -Command `"Get-ChildItem C:\Users\Administrator\Downloads,{1} -Recurse -File | Sort-Object Length -Descending | Select-Object -First 10 FullName,Length`"" -f $TargetSsh, $StageDir)
  exit 8
}

# --- 2. stage: scripts + installer, then verify the hash on the far end -------
$leaf = Split-Path -Leaf $Setup
$stageFwd = $StageDir.Replace('\', '/')
ssh $TargetSsh "mkdir `"$StageDir`" 2>nul & echo staged-dir-ok" | Out-Null
foreach ($f in @('install_chatx_node.ps1', 'smoke_chatx_node.ps1', 'relaunch_chatx_node.ps1', 'verify_chatx_vs_local.ps1',
                 'backup_chatx_data_node.ps1', 'uninstall_chatx_node.ps1', 'wipe_chatx_data_node.ps1',
                 'restore_chatx_accounts_node.ps1', 'verify_seat_backend_node.ps1')) {
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

if (-not ($Install -or $FreshInstall)) {
  Say "stage-only done. To install (stops the running app!):"
  Say ("  ssh {0} powershell -ExecutionPolicy Bypass -File {1}\install_chatx_node.ps1 -Setup {1}\{2} -ExpectSha256 {3}" -f $TargetSsh, $StageDir, $leaf, $sha)
  exit 0
}

# --- 2.5 fresh-install prep (explicit + destructive-by-rename) -----------------
# Proven order from the 2026-08-11 rollout: backup(rename, app sees factory-fresh,
# recovery copy stays on disk) -> uninstall -> install. Deliberately NO wipe step:
# backup already clears the updater cache, and running wipe after backup is how
# node 198 lost its recovery copy (wipe now skips *bak20* too -- belt and braces).
if ($FreshInstall) {
  Say "fresh-install prep: backup(rename) data ..."
  ssh $TargetSsh "powershell -ExecutionPolicy Bypass -File $StageDir\backup_chatx_data_node.ps1"
  if ($LASTEXITCODE -ne 0) { Say ("backup FAILED exit=" + $LASTEXITCODE + " (data dir locked?)"); exit 7 }
  Say "fresh-install prep: uninstall old app ..."
  ssh $TargetSsh "powershell -ExecutionPolicy Bypass -File $StageDir\uninstall_chatx_node.ps1"
  if ($LASTEXITCODE -ne 0) { Say ("uninstall FAILED exit=" + $LASTEXITCODE); exit 7 }
}

# --- 3. install (disruptive: stops the app; installer has its own retry) ------
Say "installing on target (this stops the running app) ..."
ssh $TargetSsh "powershell -ExecutionPolicy Bypass -File $StageDir\install_chatx_node.ps1 -Setup $StageDir\$leaf -ExpectSha256 $sha -KeepSetup"
if ($LASTEXITCODE -ne 0) { Say ("install FAILED exit=" + $LASTEXITCODE); exit 4 }

# --- 3.5 keep-accounts: copy login artifacts back from the _bak dir -----------
# BEFORE the first launch (seeding fills only what is missing, so restored
# logins survive; restore itself wins over any half-seeded fresh state).
if ($FreshInstall -and $KeepAccounts) {
  Say "restoring account/login artifacts from the recovery dir ..."
  ssh $TargetSsh "powershell -ExecutionPolicy Bypass -File $StageDir\restore_chatx_accounts_node.ps1"
  if ($LASTEXITCODE -ne 0) {
    Say ("account restore FAILED exit=" + $LASTEXITCODE + " (recovery _bak dir is intact; fix + rerun restore on the node)")
    exit 9
  }
}

# --- 4. relaunch in the console session (operator sees the app come back) -----
if ($Relaunch) {
  # WP-5 root-fix companion: capture the TARGET clock before the relaunch, so the
  # post-relaunch seat verification can prove the backend's run_sentinel
  # started_at is NEWER than this deploy (a surviving old backend = started_at
  # from days ago = the 2026-08-13 false-positive signature).
  $t0raw = ssh $TargetSsh 'powershell -NoProfile -Command "[DateTimeOffset]::Now.ToUnixTimeSeconds()"'
  $t0 = 0.0
  if (-not [double]::TryParse(("$t0raw".Trim()), [ref]$t0)) { $t0 = 0.0 }
  ssh $TargetSsh "powershell -ExecutionPolicy Bypass -File $StageDir\relaunch_chatx_node.ps1"
  if ($LASTEXITCODE -ne 0) { Say "relaunch did not confirm; operator may need the desktop shortcut" }
  # --- 4.5 seat backend freshness verification (started_at judge) -------------
  # Only meaningful after an install in this run; skip if we could not read the
  # target clock (never fail the deploy on a broken clock probe alone).
  if (($Install -or $FreshInstall) -and $t0 -gt 0) {
    Say "verifying the seat came back on a FRESH backend (run_sentinel started_at) ..."
    ssh $TargetSsh "powershell -ExecutionPolicy Bypass -File $StageDir\verify_seat_backend_node.ps1 -SinceEpoch $t0"
    if ($LASTEXITCODE -ne 0) {
      Say ("seat backend verification FAILED exit=" + $LASTEXITCODE + " (1=stale backend / 2=never booted / 3=port silent)")
      exit 10
    }
  }
  # --- 4.6 display sanity (2026-08-17 .173 lesson) -----------------------------
  # WARNING-ONLY, never fails the deploy: a cramped logical desktop (e.g. 4K at
  # the Windows-recommended 300% -> 1280x720) hides the composer toolbar with
  # zero code involved. Catching it at install time beats the boss catching it
  # live. Same probe as the fleet ledger; remedy = deploy\desktop\set_seat_scale.ps1.
  $dprobe = Join-Path $PSScriptRoot '_seat_disp_probe.ps1'
  if (Test-Path $dprobe) {
    scp -o ConnectTimeout=6 $dprobe ($TargetSsh + ':C:/Windows/Temp/_seat_disp_probe.ps1') 2>$null | Out-Null
    $draw = ssh -o ConnectTimeout=6 $TargetSsh 'powershell -NoProfile -ExecutionPolicy Bypass -File C:\Windows\Temp\_seat_disp_probe.ps1' 2>$null
    $ds = "$draw".Trim()
    $lhWorst = -1; $dshow = $ds
    if ($ds -match '^EXACT (\d+)x(\d+)@(\d+)$') {
      $lhWorst = [int]$Matches[2]
      $dshow = ($Matches[1] + 'x' + $Matches[2] + '@' + $Matches[3] + '%')
    }
    elseif ($ds -match '^(\d+)x(\d+)@(\d+)#(-?\d+)$') {
      # Reference math lives in chatx_fleet_status.ps1 (two-candidate ladder);
      # here we only need the LARGEST candidate height for a no-false-alarm warn.
      $ph2 = [int]$Matches[2]
      $dpiPct = [int][math]::Round([int]$Matches[3] * 100.0 / 96)
      $ovr = [int]$Matches[4]
      $ladder = @(100, 125, 150, 175, 200, 225, 250, 300, 350, 400, 450, 500)
      $candA = $dpiPct
      $iRec = $ladder.IndexOf($dpiPct)
      if ($ovr -ne 0 -and $iRec -ge 0) {
        $iCur = $iRec + $ovr
        if ($iCur -ge 0 -and $iCur -lt $ladder.Count) { $candA = $ladder[$iCur] }
      }
      $lhWorst = [math]::Max([int][math]::Round($ph2 * 100.0 / $candA), [int][math]::Round($ph2 * 100.0 / $dpiPct))
      $dshow = ($Matches[1] + 'x' + $Matches[2] + ' @' + $candA + '%~')
    }
    if ($lhWorst -ge 0) {
      if ($lhWorst -lt 800) {
        Say ("WARNING: SMALL DESKTOP on target (display " + $dshow + ", logical height ~" + $lhWorst + " < 800): the composer toolbar may not fit. Fix: deploy\desktop\set_seat_scale.ps1 -TargetSsh <node> -ScalePercent 200 -RestartChatX")
      } else {
        Say ("display ok (" + $dshow + ")")
      }
    }
  }
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
