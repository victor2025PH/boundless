# chatx_readout.ps1 -- one-command observation readout for a ChatX desktop node.
#
# Pulls, over SSH, everything the observation period needs to judge the P0/P1/P2
# features shipped 2026-07-31 (guards / opener / direct-output gloss):
#   1. version + license state
#   2. guard + usage counters (backend.log ASCII anchors, restart-proof)
#   3. db-derived volumes (scp inbox.db copy -> readout_db.py)
#
# Remote commands with embedded quotes DO NOT survive PS 5.1 -> ssh -> cmd
# (native arg re-quoting mangles them; version-dependent). So everything
# quote-sensitive is written into a probe .cmd file, scp'd over and executed
# there -- file contents bypass the command-line quoting layer entirely.
#
# ASCII-only on purpose (PS 5.1 GBK decode lesson). Read-only: never mutates
# node state beyond one temp file (deleted after use).
#
# Usage:
#   powershell -File deploy\desktop\chatx_readout.ps1 -TargetSsh zhituo
#   powershell -File deploy\desktop\chatx_readout.ps1 -TargetSsh huanyan-node -Days 14
[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)][string]$TargetSsh,
  [int]$Days = 7
)

$ErrorActionPreference = 'Stop'
function Say($m) { Write-Output $m }

$roam = 'C:\Users\Administrator\AppData\Roaming\telegram-ai-desktop'
$log = "$roam\logs\backend.log"
$db = "$roam/data/config/inbox.db"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path

Say ("=== ChatX readout: {0} @ {1} ===" -f $TargetSsh, (Get-Date -Format 'yyyy-MM-dd HH:mm'))

# --- 0. node backend endpoint + token (config.json has no quoting hazards) ----
$cfgRaw = ssh $TargetSsh "type $roam\config.json" 2>$null | Out-String
$base = ''; $tok = ''
try {
  $cfg = $cfgRaw | ConvertFrom-Json
  $base = [string]$cfg.backend.base_url
  $tok = [string]$cfg.backend.token
} catch {}
if (-not $base) { Say "config.json unreadable -- API sections will be skipped" }

# --- 1+2. probe script: license + log counters in ONE remote round-trip -------
$probe = @()
$probe += '@echo off'
if ($base) {
  $probe += ('curl -s --max-time 6 http://127.0.0.1:{0}/api/desktop/ping' -f ($base -replace '.*:', ''))
  $probe += 'echo.'
  $probe += ('curl -s --max-time 6 -H "Authorization: Bearer {0}" {1}/api/admin/license' -f $tok, $base)
  $probe += 'echo.'
}
$probe += 'echo ---COUNTS---'
$anchors = [ordered]@{
  'lang_new'   = 'findstr /C:"guard=lang_mismatch" "{0}" ^| find /c /v ""'
  'lang_leg'   = 'findstr /C:"[send]" "{0}" ^| findstr /C:" len=" ^| find /c /v ""'
  'dup_new'    = 'findstr /C:"guard=near_duplicate" "{0}" ^| find /c /v ""'
  'dup_leg'    = 'findstr /C:"[send]" "{0}" ^| findstr /C:" sim=" ^| find /c /v ""'
  'force'      = 'findstr /C:"guard=force" "{0}" ^| find /c /v ""'
  'hold'       = 'findstr /C:"translate_hold" "{0}" ^| find /c /v ""'
  'sr_calls'   = 'findstr /C:"[smart_reply]" "{0}" ^| find /c /v ""'
  'sr_opener'  = 'findstr /C:"[smart_reply]" "{0}" ^| findstr /C:"mode=opener" ^| find /c /v ""'
  'sr_gloss'   = 'findstr /C:"[smart_reply]" "{0}" ^| findstr /C:"gloss=1" ^| find /c /v ""'
}
foreach ($k in $anchors.Keys) {
  $probe += ("for /f %%c in ('" + ($anchors[$k] -f $log) + "') do echo {0}=%%c" -f $k)
}
$probeLocal = Join-Path $env:TEMP ("chatx-probe-" + $TargetSsh + ".cmd")
# ASCII, CRLF (cmd is picky about line endings)
[System.IO.File]::WriteAllText($probeLocal, ($probe -join "`r`n") + "`r`n", [System.Text.Encoding]::ASCII)
$remoteProbe = 'C:/Users/Administrator/AppData/Local/Temp/chatx_probe.cmd'
scp -q $probeLocal ("{0}:{1}" -f $TargetSsh, $remoteProbe)
$out = ssh $TargetSsh ($remoteProbe -replace '/', '\') 2>$null | Out-String
ssh $TargetSsh ("del " + ($remoteProbe -replace '/', '\')) 2>$null | Out-Null

# parse: before ---COUNTS--- = two JSON lines (ping, license); after = key=value
$head, $tail = ($out -split '---COUNTS---', 2)
foreach ($line in ($head -split "`r?`n")) {
  $t = $line.Trim()
  if (-not $t) { continue }
  if ($t -match '"version"') { Say ("ping:    " + $t) }
  elseif ($t -match '"state"') {
    try {
      $L = $t | ConvertFrom-Json
      Say ("license: state={0} plan={1} days_left={2} chars_used={3}/{4}" -f `
        $L.state, $L.plan, $L.days_left, $L.quota.used_chars, $L.quota.included_chars)
    } catch { Say ("license: " + $t) }
  }
}
Say "--- log counters (since last log rotation) ---"
$vals = @{}
foreach ($line in ($tail -split "`r?`n")) {
  if ($line -match '^(\w+)=(\d+)') { $vals[$Matches[1]] = [int]$Matches[2] }
}
$disp = [ordered]@{
  'guard lang_mismatch'      = [int]$vals['lang_new'] + [int]$vals['lang_leg']
  'guard near_duplicate'     = [int]$vals['dup_new'] + [int]$vals['dup_leg']
  'guard force-send'         = [int]$vals['force']
  'translate HOLD'           = [int]$vals['hold']
  'smart-reply calls'        = [int]$vals['sr_calls']
  'smart-reply opener mode'  = [int]$vals['sr_opener']
  'smart-reply gloss hits'   = [int]$vals['sr_gloss']
}
foreach ($k in $disp.Keys) { Say ("  {0,-28} {1}" -f $k, $disp[$k]) }

# --- 3. db volumes (copy off-node first; analyzer never touches the live db) --
$tmp = Join-Path $env:TEMP ("chatx-readout\" + $TargetSsh)
New-Item -ItemType Directory -Force $tmp | Out-Null
scp -q "${TargetSsh}:$db" "$tmp\inbox.db" 2>$null
scp -q "${TargetSsh}:$db-wal" "$tmp\inbox.db-wal" 2>$null
if (Test-Path "$tmp\inbox.db") {
  Say "--- db volumes ---"
  python (Join-Path $here 'readout_db.py') "$tmp\inbox.db" --days $Days
} else {
  Say "db copy failed -- skipping volume section"
}
Say "=== readout done ==="
