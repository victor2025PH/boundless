# hotpatch_wa_sidecar.ps1 -- ship ONE file (whatsapp-baileys/server.js) to an
# installed ChatX node without a 453MB reinstall. Runs on the build machine.
#
# Why this exists (2026-07-31): the WA sidecar is packaged VERBATIM from
# services/whatsapp-baileys (electron-builder extraResources), so a sidecar-only
# fix (e.g. the device-suffix wrong-recipient bug) is a one-file hot patch:
#   syntax-check local -> stop app -> scp server.js over the installed copy ->
#   interactive relaunch (sidecar restarts with the shell) -> /health probe.
# The next full installer will carry the same source file, so nodes converge.
#
# ASCII-only (PS 5.1 GBK lesson).
# Exit: 0 ok / 2 local syntax bad / 3 copy failed / 4 relaunch or health failed
[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)][string]$TargetSsh,
  [string]$StageDir = 'C:\Users\Administrator\Downloads\chatx'
)
$ErrorActionPreference = 'Stop'
function Say($m) { Write-Output ("[hotpatch] " + $m) }

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$repo = Split-Path -Parent (Split-Path -Parent $here)
$src = Join-Path $repo 'engines\chengjie\services\whatsapp-baileys\server.js'
$dstFwd = 'C:/Users/Administrator/AppData/Local/Programs/telegram-ai-desktop/resources/services/whatsapp-baileys/server.js'

node --check $src
if ($LASTEXITCODE -ne 0) { Say "local server.js syntax check FAILED"; exit 2 }
Say "local syntax OK"

$stageFwd = $StageDir.Replace('\', '/')
foreach ($f in @('stop_chatx_node.ps1', 'relaunch_chatx_node.ps1')) {
  scp -q (Join-Path $here $f) ("{0}:{1}/{2}" -f $TargetSsh, $stageFwd, $f)
}
ssh $TargetSsh "powershell -ExecutionPolicy Bypass -File $StageDir\stop_chatx_node.ps1"
scp -q $src ("{0}:{1}" -f $TargetSsh, $dstFwd)
if ($LASTEXITCODE -ne 0) { Say "server.js copy FAILED"; exit 3 }
Say "server.js patched"
ssh $TargetSsh "powershell -ExecutionPolicy Bypass -File $StageDir\relaunch_chatx_node.ps1"
if ($LASTEXITCODE -ne 0) { Say "relaunch not confirmed"; exit 4 }
# sidecar health: the shell brings baileys up on :8790 within a few seconds
Start-Sleep -Seconds 12
$h = ssh $TargetSsh "curl -s --max-time 6 http://127.0.0.1:8790/health"
Say ("sidecar /health: " + $h)
if (-not ("$h" -match '"ok"\s*:\s*true')) { Say "sidecar health not confirmed"; exit 4 }
Say "OK"
exit 0
