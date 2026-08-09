# deploy_hud.ps1 - six-machine deploy for the boundless desktop HUD suite. ASCII-ONLY source.
# Canonical git home of the suite (P0 2026-08-10): D:\boundless\deploy\boundless-hud\
#   sentinel.ps1 / telemetry_agent.ps1 / register_task.ps1 / run_hidden.vbs / modes.json / this script
# Related pieces that stay under tools\ (live-HUD hud_server.py imports + hud_contract_test.py
# pin those paths; moving them would break another active work line):
#   tools\render_cluster_board.py   board renderer (hub-only, board_cmd below points at it)
#   tools\xiaojie\xiaojie_server.py :7912 board server (task BoundlessXiaojieServer, hub-only)
#   tools\make_machine_wallpapers.py  wallpaper template generator (outputs brand-assets\05_backgrounds\machines)
#
# What it does per machine (roster = deploy\machines.json, six boxes):
#   1. optional -GitPull: if the box has a D:\boundless checkout, git pull --ff-only first
#   2. copy suite files + per-machine wallpapers into C:\Users\Public\boundless-hud
#   3. generate config.json (probe targets + per-CURRENT-MODE watch_ports from modes.json;
#      mode state read from the hub box file C:\<avatarhub>\logs\cluster_mode.json)
#   4. re-register task BoundlessHudSentinel via register_task.ps1 (hidden wscript launcher)
# Cluster power-mode switching itself is NOT here: hub POST /api/cluster/mode owns it
# (gates/rollback/cooldown server-side; this script only bakes the matching watch_ports).
[CmdletBinding()]
param(
  [string]$Only = "",
  [switch]$SkipTask,
  [switch]$GitPull
)
$ErrorActionPreference = 'Continue'
$Here = $PSScriptRoot
$Root = (Resolve-Path (Join-Path $Here '..\..')).Path
$Data = Get-Content (Join-Path $Root 'deploy\machines.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$WpSrc = Join-Path $Root 'brand-assets\05_backgrounds\machines'
$LocalIp = (Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
  Where-Object { $_.IPAddress -like '192.168.*' } | Select-Object -First 1).IPAddress
$HubIp = [string]$Data.hub.ip
$HubPort = [int]$Data.hub.port
$Gateway = [string]$Data.gateway
$RemoteBase = 'C:\Users\Public\boundless-hud'
$TaskName = 'BoundlessHudSentinel'

# Current power mode (for watch_ports overrides): ask the hub API (judgment stays server-side,
# and this file stays ASCII - no Chinese avatarhub path literal). Hub down -> roster defaults.
$Modes = $null; $CurMode = ''
try { $Modes = Get-Content (Join-Path $Here 'modes.json') -Raw -Encoding UTF8 | ConvertFrom-Json } catch {}
try {
  $st = Invoke-RestMethod ('http://{0}:{1}/api/cluster/mode' -f [string]$Data.hub.ip, [int]$Data.hub.port) -TimeoutSec 5
  if ($st.ok) { $CurMode = [string]$st.mode }
} catch {}

function Get-WatchPorts($m) {
  if ($Modes -and $CurMode -and $Modes.modes.$CurMode) {
    $mm = $Modes.modes.$CurMode.machines.($m.id)
    if ($mm -and $null -ne $mm.watch_ports) { return @($mm.watch_ports) }
  }
  return @($m.watch_ports)
}

function New-MachineConfig($m) {
  $isHub = ($m.ip -eq $HubIp)
  $cfg = [ordered]@{
    id          = [string]$m.id
    is_hub      = $isHub
    gateway_ip  = $Gateway
    hub_ip      = $HubIp
    hub_port    = $HubPort
    expected_ip = [string]$m.ip
    watch_ports = (Get-WatchPorts $m)
    mode        = 'ops'
  }
  if ($isHub) {
    # batch-render six perspective boards each minute (renderer stays under tools\, see header)
    $cfg.board_cmd = 'D:\Miniconda3\python.exe D:\boundless\tools\render_cluster_board.py --all --hub-out C:\Users\Public\boundless-hud\board.png'
  } else {
    $cfg.board_url = ('http://{0}:7912/board/{1}.png' -f $HubIp, [string]$m.id)
  }
  return ($cfg | ConvertTo-Json -Compress)
}

$stateNames = @('netdown', 'gpufault', 'ipdrift', 'hubdown', 'svcdown')

foreach ($m in $Data.machines) {
  if ($Only -and ([string]$m.id) -notlike "*$Only*") { continue }
  $alias = [string]$m.ssh[0]
  $isLocal = ($m.ip -eq $LocalIp)
  Write-Host ("===== {0} ({1}) local={2} mode={3} =====" -f $m.id, $m.ip, $isLocal, ($CurMode + '')) -ForegroundColor Green

  if ($GitPull -and -not $isLocal -and ([string]$m.workdir) -like '*boundless*') {
    ssh -o BatchMode=yes -o ConnectTimeout=15 $alias "git -C D:\boundless pull --ff-only" 2>&1 |
      Select-Object -Last 1 | ForEach-Object { Write-Host ("  git pull: " + $_) }
  }

  $cfgJson = New-MachineConfig $m
  $tmpCfg = Join-Path $env:TEMP ("hudcfg_{0}.json" -f $m.id)
  [IO.File]::WriteAllText($tmpCfg, $cfgJson, [Text.UTF8Encoding]::new($false))

  $files = @(
    @{ src = (Join-Path $WpSrc ("{0}-wallpaper.png" -f $m.id)); dst = 'wallpapers/normal.png' },
    @{ src = (Join-Path $WpSrc ("{0}-showcase.png" -f $m.id)); dst = 'wallpapers/showcase.png' }
  )
  foreach ($st in $stateNames) {
    $files += @{ src = (Join-Path $WpSrc ("states\{0}-{1}.png" -f $m.id, $st)); dst = ("wallpapers/{0}.png" -f $st) }
  }
  foreach ($f in @('sentinel.ps1', 'telemetry_agent.ps1', 'run_hidden.vbs', 'register_task.ps1')) {
    $files += @{ src = (Join-Path $Here $f); dst = $f }
  }
  $files += @{ src = $tmpCfg; dst = 'config.json' }

  $missing = $files | Where-Object { -not (Test-Path $_.src) }
  if ($missing) { Write-Host ("MISSING sources: " + (($missing | ForEach-Object { $_.src }) -join '; ')) -ForegroundColor Red; continue }

  if ($isLocal) {
    New-Item -ItemType Directory -Force -Path (Join-Path $RemoteBase 'wallpapers') | Out-Null
    foreach ($f in $files) {
      Copy-Item $f.src (Join-Path $RemoteBase ($f.dst -replace '/', '\')) -Force
    }
    if (-not $SkipTask) {
      & (Join-Path $RemoteBase 'register_task.ps1') | Where-Object { $_ -match 'TASK_OK|TASK_SETTINGS_WARN' }
      Write-Host "  task registered (local, hidden launcher)"
    }
    schtasks /run /tn $TaskName | Out-Null
  } else {
    ssh -o BatchMode=yes -o ConnectTimeout=12 $alias "cmd /c if not exist $RemoteBase\wallpapers mkdir $RemoteBase\wallpapers" 2>&1 | Out-Null
    foreach ($f in $files) {
      $dst = ($RemoteBase -replace '\\', '/') + '/' + $f.dst
      scp -o BatchMode=yes -o ConnectTimeout=12 $f.src ("{0}:{1}" -f $alias, $dst) 2>&1 | Out-Null
    }
    if (-not $SkipTask) {
      ssh -o BatchMode=yes -o ConnectTimeout=30 $alias "powershell -NoProfile -ExecutionPolicy Bypass -File C:\Users\Public\boundless-hud\register_task.ps1" 2>&1 |
        Where-Object { $_ -match 'TASK_OK|ERROR' }
    }
    ssh -o BatchMode=yes -o ConnectTimeout=20 $alias "schtasks /run /tn $TaskName" 2>&1 | Out-Null
  }
  Remove-Item $tmpCfg -Force -ErrorAction SilentlyContinue
  Write-Host ("  deployed {0} files" -f $files.Count)
}
Write-Host "deploy_hud done" -ForegroundColor Cyan
