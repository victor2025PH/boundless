# push_chatx_hotpatch.ps1 -- ship a hotpatch zip to one or more SSH nodes.
#
# Runs on the BUILD machine (117). Sister of push_chatx.ps1: same stage/hash/
# apply/relaunch shape, but the payload is a small zip instead of a 400+ MB
# NSIS installer. Default targets match chatx_fleet_status.ps1.
#
#   powershell -File push_chatx_hotpatch.ps1
#   powershell -File push_chatx_hotpatch.ps1 -Targets yunsheng,kouxing
#   powershell -File push_chatx_hotpatch.ps1 -TargetSsh zhituo -Relaunch
#
# ASCII-only (PS 5.1 GBK). Exit: 0 all ok / 2 no patch / 3 a node failed
[CmdletBinding()]
param(
  [string]$TargetSsh = "",
  [string[]]$Targets = @(),
  [string]$PatchZip = "",
  [string]$Manifest = "",
  [string]$StageDir = 'C:\Users\Administrator\Downloads\chatx',
  [string]$DesktopDir = "D:\boundless\engines\chengjie\desktop",
  [switch]$Relaunch,
  [switch]$DryRun,
  # impl81 P0-3b: after a successful push, flush pending fix-notifies so the
  # reporters in the bug groups actually learn their fix has shipped.
  # Default OFF (it sends real group messages) - opt-in per push.
  [switch]$NotifyPending,
  # impl81 P2-3: refresh bug_intake.update_hint (the "how to get the fix" line
  # in fix-notifies) as part of the push, e.g. -UpdateHint "hotpatch pushed,
  # restart ChatX". Writes the production overlay (comment-preserving) - only
  # when explicitly passed, never automatic.
  [string]$UpdateHint = ""
)

$ErrorActionPreference = 'Stop'
function Say($m) { Write-Output ("[hotpush] " + $m) }

$Targets = @($Targets | ForEach-Object { "$_".Split(',') } | ForEach-Object { "$_".Trim() } | Where-Object { $_ })
if ($TargetSsh) { $Targets = @($TargetSsh) + $Targets }
if ($Targets.Count -eq 0) { $Targets = @('yunsheng', 'kouxing', 'lianbei') }
$Targets = @($Targets | Select-Object -Unique)

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$outDir = Join-Path $DesktopDir 'dist\hotpatch'
if (-not $PatchZip) {
  $ptr = Join-Path $outDir 'hotpatch.json'
  if (Test-Path $ptr) {
    try {
      $p = Get-Content -LiteralPath $ptr -Raw -Encoding UTF8 | ConvertFrom-Json
      if ($p.zip) { $PatchZip = Join-Path $outDir $p.zip }
      if (-not $Manifest) { $Manifest = $ptr }
    } catch { }
  }
  if (-not $PatchZip) {
    $cand = Get-ChildItem $outDir -Filter 'ChatX-Hotpatch-*.zip' -ErrorAction SilentlyContinue |
      Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if ($cand) { $PatchZip = $cand.FullName }
  }
}
if (-not $PatchZip -or -not (Test-Path $PatchZip)) { Say "no hotpatch zip (run make_chatx_hotpatch.ps1 first)"; exit 2 }
if (-not $Manifest) {
  $sib = [IO.Path]::ChangeExtension($PatchZip, '.json')
  $idJson = Join-Path $outDir (([IO.Path]::GetFileNameWithoutExtension($PatchZip) -replace '^ChatX-Hotpatch-', '') + '.json')
  if (Test-Path $sib) { $Manifest = $sib }
  elseif (Test-Path $idJson) { $Manifest = $idJson }
  elseif (Test-Path (Join-Path $outDir 'hotpatch.json')) { $Manifest = Join-Path $outDir 'hotpatch.json' }
}
if (-not $Manifest -or -not (Test-Path $Manifest)) { Say "manifest missing"; exit 2 }

$sha = (Get-FileHash $PatchZip -Algorithm SHA256).Hash
$sz = [math]::Round((Get-Item $PatchZip).Length / 1MB, 1)
Say ("zip: " + (Split-Path -Leaf $PatchZip) + " (" + $sz + " MB)")
Say ("sha256: " + $sha)
Say ("targets: " + ($Targets -join ','))
if ($DryRun) { Say "dry-run: nothing uploaded"; exit 0 }

$leaf = Split-Path -Leaf $PatchZip
$maniLeaf = Split-Path -Leaf $Manifest
$stageFwd = $StageDir.Replace('\', '/')
$failed = @()

foreach ($t in $Targets) {
  Say ("---- " + $t + " ----")
  try {
    ssh $t "mkdir `"$StageDir`" 2>nul & echo staged-dir-ok" | Out-Null
    foreach ($f in @('apply_chatx_hotpatch_node.ps1', 'stop_chatx_node.ps1', 'relaunch_chatx_node.ps1')) {
      $src = Join-Path $here $f
      if (-not (Test-Path $src)) { throw ("missing script " + $f) }
      scp -q $src ("{0}:{1}/{2}" -f $t, $stageFwd, $f)
    }
    scp -q $PatchZip ("{0}:{1}/{2}" -f $t, $stageFwd, $leaf)
    scp -q $Manifest ("{0}:{1}/{2}" -f $t, $stageFwd, $maniLeaf)
    $remote = (ssh $t "certutil -hashfile $StageDir\$leaf SHA256")[1]
    if (-not $remote -or ($remote.Trim().ToUpper() -ne $sha)) {
      throw ("hash MISMATCH remote=" + $remote)
    }
    $relFlag = ""
    if ($Relaunch) { $relFlag = " -Relaunch" }
    ssh $t "powershell -ExecutionPolicy Bypass -File $StageDir\apply_chatx_hotpatch_node.ps1 -Zip $StageDir\$leaf -Manifest $StageDir\$maniLeaf$relFlag"
    if ($LASTEXITCODE -ne 0) { throw ("apply exit=" + $LASTEXITCODE) }
    Say ($t + " OK")
  } catch {
    Say ($t + " FAIL: " + $_.Exception.Message)
    $failed += $t
  }
}

if ($failed.Count) {
  Say ("FAILED nodes: " + ($failed -join ','))
  exit 3
}
Say "OK all targets"

# impl81 P2-3: operator explicitly passed a new update hint -> write it BEFORE
# any notify, so flushed notifies already carry the fresh "how to get it" line.
if ($UpdateHint) {
  try {
    Push-Location "D:\boundless\engines\chengjie"
    & python "tools\duty_update_hint.py" --set $UpdateHint 2>&1 | ForEach-Object { Say ("[hint] " + $_) }
    Pop-Location
  } catch { Say ("[hint] update skipped: " + $_.Exception.Message) }
}

# impl81 P0-3b: fix shipped != reporter told. Surface the pending fix-notify
# backlog every push; -NotifyPending flushes it (real group messages, opt-in).
$dutyTool = "D:\boundless\engines\chengjie\tools\duty_notify_pending.py"
if (Test-Path $dutyTool) {
  $flushArg = @()
  if ($NotifyPending) { $flushArg = @('--flush') }
  try {
    Push-Location "D:\boundless\engines\chengjie"
    & python $dutyTool @flushArg 2>&1 | ForEach-Object { Say ("[notify] " + $_) }
    Pop-Location
  } catch { Say ("[notify] check skipped: " + $_.Exception.Message) }
  if (-not $NotifyPending) {
    Say "[notify] pending fix-notifies are NOT sent automatically; re-run with -NotifyPending or: python tools\duty_notify_pending.py --flush"
  }
}
exit 0
