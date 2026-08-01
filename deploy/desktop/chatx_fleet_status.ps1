# chatx_fleet_status.ps1 -- one-glance ChatX version ledger across nodes.
#
# Why: "which build is .198 actually running?" cost real time on 2026-07-31
# (assumed 0.2.11, was 0.2.12, now 1.0.1). The RUNNING backend is the truth,
# not the installed exe: /api/desktop/ping is the auth-free identity probe the
# desktop shell itself uses to avoid adopting a stranger's backend, so we read
# the same endpoint per node. Backend down -> fall back to the installed exe
# version (marked "exe only", i.e. installed but not currently serving).
#
# ASCII-only (PS 5.1 GBK lesson). Read-only: never starts/stops anything.
#
# Usage: powershell -File deploy\desktop\chatx_fleet_status.ps1 [-Targets zhituo,huanyan-node]
[CmdletBinding()]
param([string[]]$Targets = @('zhituo', 'huanyan-node'))

$ErrorActionPreference = 'Continue'
Write-Output ("{0,-16} {1,-10} {2,-24} {3}" -f 'node', 'backend', 'app/version', 'note')
Write-Output ('-' * 70)

foreach ($t in $Targets) {
  $note = ''
  $backend = 'DOWN'
  $ver = ''
  # 1. running backend identity (auth-free by design)
  $ping = ssh -o ConnectTimeout=6 $t "curl -s --max-time 5 http://127.0.0.1:18799/api/desktop/ping" 2>$null
  if ($LASTEXITCODE -eq 0 -and $ping -and ("$ping" -match '"version"\s*:\s*"([^"]+)"')) {
    $backend = 'UP'
    $ver = $Matches[1]
    if ("$ping" -match '"app"\s*:\s*"([^"]+)"') { $ver = $Matches[1] + ' ' + $ver }
  } else {
    # 2. fall back to installed exe version (installed but not serving)
    $exe = ssh -o ConnectTimeout=6 $t ("powershell -NoProfile -Command " +
      "(Get-ChildItem \`"`$env:LOCALAPPDATA\Programs\telegram-ai-desktop\`" -Filter *.exe " +
      "-ErrorAction SilentlyContinue ^| Where-Object Name -notlike 'Uninstall*' ^| " +
      "Select-Object -First 1).VersionInfo.ProductVersion") 2>$null
    if ($exe) { $ver = ("" + $exe).Trim(); $note = 'exe only (backend not serving)' }
    else { $note = 'not installed / unreachable' }
  }
  Write-Output ("{0,-16} {1,-10} {2,-24} {3}" -f $t, $backend, $ver, $note)
}
