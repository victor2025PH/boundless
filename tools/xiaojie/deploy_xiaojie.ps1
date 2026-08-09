# Deploy XiaoJie desktop robot to all six machines. ASCII-ONLY source.
# Architecture (deliberately zero-install on nodes):
#   - xiaojie_server.py runs ONLY on the hub box (:7912, stdlib python, secrets stay here)
#   - every machine (hub included) gets a scheduled task that opens the robot page in
#     Edge --app mode (chromeless window, present on every Win10/11 box) sized/positioned
#     per machine resolution from deploy/machines.json
#   - robot page fetches /api/context /api/chat /api/tts from the hub server (same origin)
[CmdletBinding()]
param(
  [string]$Only = "",
  [switch]$SkipServer
)
$ErrorActionPreference = 'Continue'
$Root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$Data = Get-Content (Join-Path $Root 'deploy\machines.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$HubIp = [string]$Data.hub.ip
$SrvUrl = "http://${HubIp}:7912"
$LocalIp = (Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
  Where-Object { $_.IPAddress -like '192.168.*' } | Select-Object -First 1).IPAddress

# window geometry (physical px hints; Edge treats them as DIPs so high-DPI screens
# land slightly off -- acceptable, the window is draggable)
$WinW = 360; $WinH = 640

$Launcher = @'
param([string]$Url, [int]$X = 100, [int]$Y = 100, [int]$W = 360, [int]$H = 640)
# browser chain: Edge -> Chrome (lianbei has no Edge, 2026-08-06 proof); both honor --app
$cands = @(
  (Join-Path ${env:ProgramFiles(x86)} 'Microsoft\Edge\Application\msedge.exe'),
  (Join-Path $env:ProgramFiles 'Microsoft\Edge\Application\msedge.exe'),
  (Join-Path $env:ProgramFiles 'Google\Chrome\Application\chrome.exe'),
  (Join-Path ${env:ProgramFiles(x86)} 'Google\Chrome\Application\chrome.exe')
)
$bin = $cands | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $bin) {
  foreach ($k in @('msedge.exe', 'chrome.exe')) {
    try {
      $bin = (Get-ItemProperty ("HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\" + $k) -ErrorAction Stop).'(default)'
      if ($bin) { break }
    } catch {}
  }
}
if (-not $bin) { $bin = 'msedge.exe' }
Start-Process -FilePath $bin -ArgumentList @(
  ('--app=' + $Url), ('--window-size=' + $W + ',' + $H), ('--window-position=' + $X + ',' + $Y),
  '--disable-features=TranslateUI', '--no-first-run'
)
'@

$SrvTaskCmd = 'powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -Command "Start-Process -WindowStyle Hidden -FilePath D:\Miniconda3\python.exe -ArgumentList D:\boundless\tools\xiaojie\xiaojie_server.py"'

foreach ($m in $Data.machines) {
  if ($Only -and ([string]$m.id) -notlike "*$Only*") { continue }
  $alias = [string]$m.ssh[0]
  $isLocal = ($m.ip -eq $LocalIp)
  $isHub = ($m.ip -eq $HubIp)
  Write-Host ("===== {0} ({1}) local={2} =====" -f $m.id, $m.ip, $isLocal) -ForegroundColor Green

  # geometry from roster resolution: bottom-right corner with margins
  $res = ([string]$m.resolution) -split 'x'
  $sw = [int]$res[0]; $sh = [int]$res[1]
  $x = [Math]::Max(20, $sw - $WinW - 28)
  $y = [Math]::Max(20, $sh - $WinH - 90)
  $url = "$SrvUrl/robot?machine=$($m.id)"

  # ship launcher
  $tmpL = Join-Path $env:TEMP 'xiaojie_launch.ps1'
  [IO.File]::WriteAllText($tmpL, $Launcher, [Text.UTF8Encoding]::new($false))
  $trCmd = "powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File C:\Users\Public\boundless-hud\xiaojie_launch.ps1 -Url $url -X $x -Y $y -W $WinW -H $WinH"

  # per-box task registrar (schtasks quoting is sane only from inside PowerShell on the box)
  $reg = @(
    "schtasks /create /f /tn BoundlessXiaojie /sc onlogon /tr '$trCmd' | Out-Null"
    "schtasks /run /tn BoundlessXiaojie | Out-Null"
    "Write-Output XIAOJIE_TASK_OK"
  ) -join '; '
  $tmpR = Join-Path $env:TEMP 'xiaojie_reg.ps1'
  [IO.File]::WriteAllText($tmpR, $reg, [Text.UTF8Encoding]::new($false))

  if ($isLocal) {
    Copy-Item $tmpL 'C:\Users\Public\boundless-hud\xiaojie_launch.ps1' -Force
    if ($isHub -and -not $SkipServer) {
      # server task via launcher FILE: schtasks /tr with nested quotes gets shredded locally
      $srvLaunch = 'Start-Process -WindowStyle Hidden -FilePath D:\Miniconda3\python.exe -ArgumentList D:\boundless\tools\xiaojie\xiaojie_server.py'
      [IO.File]::WriteAllText('C:\Users\Public\boundless-hud\xiaojie_server_launch.ps1', $srvLaunch, [Text.UTF8Encoding]::new($false))
      schtasks /create /f /tn BoundlessXiaojieServer /sc onlogon /tr 'powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File C:\Users\Public\boundless-hud\xiaojie_server_launch.ps1' | Out-Null
      Write-Host 'server task registered (start at logon)'
    }
    schtasks /create /f /tn BoundlessXiaojie /sc onlogon /tr $trCmd | Out-Null
    schtasks /run /tn BoundlessXiaojie | Out-Null
    Write-Host 'XIAOJIE_TASK_OK (local)'
  } else {
    scp -o BatchMode=yes -o ConnectTimeout=12 $tmpL ("{0}:C:/Users/Public/boundless-hud/xiaojie_launch.ps1" -f $alias) 2>&1 | Out-Null
    scp -o BatchMode=yes -o ConnectTimeout=12 $tmpR ("{0}:C:/Users/Public/boundless-hud/xiaojie_reg.ps1" -f $alias) 2>&1 | Out-Null
    ssh -o BatchMode=yes -o ConnectTimeout=30 $alias "powershell -NoProfile -ExecutionPolicy Bypass -File C:\Users\Public\boundless-hud\xiaojie_reg.ps1" 2>&1 |
      Where-Object { $_ -match 'XIAOJIE_TASK_OK|ERROR' }
  }
  Write-Host ("robot window -> {0} at {1},{2}" -f $url, $x, $y)
}
Write-Host 'deploy_xiaojie done' -ForegroundColor Cyan
