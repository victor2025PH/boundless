# Deploy the LIVE cluster HUD kiosk entry to cluster machines (P1, 2026-08-07). ASCII-ONLY source.
# Source of truth: deploy/machines.json. Per machine it ships:
#   C:\Users\Public\boundless-hud\hud_launch.ps1   browser-chain launcher (Edge->Chrome, --app fullscreen)
# and creates a PUBLIC-desktop shortcut pointing at the hub HUD page
#   http://<hub>:7913/hud?machine=<id>
# The shortcut filename is Chinese ("cluster live board"), built from [char] codepoints
# so this source file itself stays pure ASCII (red line 8 discipline).
# The HUD server runs ONLY on the hub (task BoundlessHudServer, hud_server.py :7913).
[CmdletBinding()]
param(
  [string]$Only = "",
  # -Gesture: append gesture=1&gsrc=phone to NON-hub machines' HUD URL (phone-station
  # gesture layer, see phone-cluster plan stage 1/2). Hub stays plain (policy: hub
  # gestures come later with the P4 command-deck camera, not the phone path).
  [switch]$Gesture
)
$ErrorActionPreference = 'Continue'
$Root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$Data = Get-Content (Join-Path $Root 'deploy\machines.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$HubIp = [string]$Data.hub.ip
$LocalIp = (Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
  Where-Object { $_.IPAddress -like '192.168.*' } | Select-Object -First 1).IPAddress

$Launcher = @'
param([string]$Url)
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
  ('--app=' + $Url), '--start-fullscreen', '--no-first-run', '--disable-features=TranslateUI')
'@

foreach ($m in $Data.machines) {
  if ($Only -and ([string]$m.id) -notlike "*$Only*") { continue }
  $alias = [string]$m.ssh[0]
  $isLocal = ($m.ip -eq $LocalIp)
  $isHub = ($m.ip -eq $HubIp)
  $url = ('http://{0}:7913/hud?machine={1}' -f $HubIp, [string]$m.id)
  if ($Gesture -and -not $isHub) { $url = $url + '&gesture=1&gsrc=phone' }
  Write-Host ("===== {0} ({1}) local={2} =====" -f $m.id, $m.ip, $isLocal) -ForegroundColor Green

  $tmpL = Join-Path $env:TEMP 'hud_launch.ps1'
  [IO.File]::WriteAllText($tmpL, $Launcher, [Text.UTF8Encoding]::new($false))

  # per-box registrar (runs ON the target box): writes the public-desktop shortcut.
  # Shortcut display name = Chinese "cluster live board" via codepoints (ASCII source).
  $reg = @(
    '$n = -join @([char]0x96C6,[char]0x7FA4,[char]0x5B9E,[char]0x51B5,[char]0x677F)'
    '$lnk = Join-Path "C:\Users\Public\Desktop" ($n + ".lnk")'
    '$ws = New-Object -ComObject WScript.Shell'
    '$sc = $ws.CreateShortcut($lnk)'
    '$sc.TargetPath = "powershell.exe"'
    ('$sc.Arguments = ''-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File C:\Users\Public\boundless-hud\hud_launch.ps1 -Url {0}''' -f $url)
    '$sc.WorkingDirectory = "C:\Users\Public\boundless-hud"'
    '$sc.Description = "BOUNDLESS cluster HUD"'
    '$sc.Save()'
    'Write-Output ("HUD_LNK_OK " + $lnk)'
  ) -join "`r`n"
  $tmpR = Join-Path $env:TEMP 'hud_lnk_reg.ps1'
  [IO.File]::WriteAllText($tmpR, $reg, [Text.UTF8Encoding]::new($false))

  # ALWAYS go through ssh (hub included, loopback via its own alias): writing to
  # C:\Users\Public\Desktop needs elevation, and the sshd context is admin while an
  # interactive non-elevated shell is not (locally Save() throws UnauthorizedAccess).
  scp -o BatchMode=yes -o ConnectTimeout=12 $tmpL ("{0}:C:/Users/Public/boundless-hud/hud_launch.ps1" -f $alias) 2>&1 | Out-Null
  scp -o BatchMode=yes -o ConnectTimeout=12 $tmpR ("{0}:C:/Users/Public/boundless-hud/hud_lnk_reg.ps1" -f $alias) 2>&1 | Out-Null
  ssh -o BatchMode=yes -o ConnectTimeout=30 $alias "powershell -NoProfile -ExecutionPolicy Bypass -File C:\Users\Public\boundless-hud\hud_lnk_reg.ps1" 2>&1 |
    Where-Object { $_ -match 'HUD_LNK_OK|ERROR|Exception' }
  Remove-Item $tmpL, $tmpR -Force -ErrorAction SilentlyContinue
}
Write-Host 'deploy_hud_live done' -ForegroundColor Cyan
