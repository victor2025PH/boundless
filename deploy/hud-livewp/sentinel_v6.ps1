# boundless desktop sentinel - per-machine wallpaper state machine (ASCII-ONLY source).
# Runs every minute via Task Scheduler in the INTERACTIVE user session.
# States (priority order):
#   netdown  - gateway unreachable (and hub TCP dead for non-hub boxes)  -> red banner wallpaper
#   gpufault - nvidia-smi missing heartbeat                              -> dark red banner
#   ipdrift  - actual LAN IP differs from the roster IP (DHCP drift)     -> purple banner
#   hubdown  - LAN fine but hub :9000 unreachable                        -> amber banner
#   svcdown  - one of this box's compute service ports not listening     -> orange banner
#   normal   - everything fine -> static identity wallpaper, or live cluster board on the hub box
# Warning templates are pre-rendered PNGs with Chinese text baked in (tools/make_machine_wallpapers.py);
# this script only stamps dynamic ASCII details (timestamps / dead ports) so it stays encoding-safe.
# v2 (P3): ipdrift state + writes self status blob for the hub board to collect over ssh.
# v4 (P0 board broadcast, 2026-08-07): non-hub boxes with cfg.board_url pull their
# per-machine cluster board PNG from the hub (xiaojie :7912) every minute in normal
# state. The server 404s stale boards (>3 min), so ANY non-200/failed fetch simply
# keeps the static identity wallpaper - freshness judgment stays server-side.
# v5 (telemetry watchdog, 2026-08-07): this minute-task also keeps telemetry_agent.ps1
# alive (second-level GPU stats feeder for the live HUD; agent self-guards against
# double-start via pid file). No extra scheduled task - the sentinel IS the watchdog.
[CmdletBinding()]
param(
  [string]$ConfigPath = ""
)
$ErrorActionPreference = 'Stop'
$Base = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $ConfigPath) { $ConfigPath = Join-Path $Base 'config.json' }
$cfg = Get-Content $ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
$WpDir = Join-Path $Base 'wallpapers'
$StatePath = Join-Path $Base 'state.json'
$LogPath = Join-Path $Base 'sentinel.log'

function Log([string]$msg) {
  $line = ('{0} {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $msg)
  Add-Content -Path $LogPath -Value $line -Encoding ascii
  if ((Test-Path $LogPath) -and ((Get-Item $LogPath).Length -gt 524288)) {
    Get-Content $LogPath -Tail 200 | Set-Content $LogPath -Encoding ascii
  }
}

function Test-Tcp([string]$ip, [int]$port, [int]$timeoutMs) {
  try {
    $c = New-Object System.Net.Sockets.TcpClient
    $ar = $c.BeginConnect($ip, $port, $null, $null)
    $ok = $ar.AsyncWaitHandle.WaitOne($timeoutMs, $false)
    if ($ok -and $c.Connected) { $c.EndConnect($ar); $c.Close(); return $true }
    $c.Close(); return $false
  } catch { return $false }
}

function Test-Ping([string]$ip) {
  try {
    $p = New-Object System.Net.NetworkInformation.Ping
    $r = $p.Send($ip, 900)
    return ($r.Status -eq [System.Net.NetworkInformation.IPStatus]::Success)
  } catch { return $false }
}

function Test-Gpu() {
  # returns $true when GPU answers (or nvidia-smi unavailable -> skip check)
  $smi = Get-Command nvidia-smi -ErrorAction SilentlyContinue
  if (-not $smi) { return $true }
  try {
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $smi.Source
    $psi.Arguments = '-L'
    $psi.UseShellExecute = $false
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $p = [System.Diagnostics.Process]::Start($psi)
    if (-not $p.WaitForExit(6000)) { try { $p.Kill() } catch {}; return $false }
    return ($p.ExitCode -eq 0)
  } catch { return $false }
}

$SpiType = @'
using System.Runtime.InteropServices;
public class WallSpi {
  [DllImport("user32.dll", SetLastError=true, CharSet=CharSet.Unicode)]
  public static extern bool SystemParametersInfo(int uAction, int uParam, string lpvParam, int fuWinIni);
}
'@
Add-Type -TypeDefinition $SpiType -ErrorAction SilentlyContinue

function Set-Wallpaper([string]$path) {
  Set-ItemProperty -Path 'HKCU:\Control Panel\Desktop' -Name Wallpaper -Value $path
  Set-ItemProperty -Path 'HKCU:\Control Panel\Desktop' -Name WallpaperStyle -Value 10 -ErrorAction SilentlyContinue
  Set-ItemProperty -Path 'HKCU:\Control Panel\Desktop' -Name TileWallpaper -Value 0 -ErrorAction SilentlyContinue
  [WallSpi]::SystemParametersInfo(20, 0, $path, 3) | Out-Null
}

function Stamp-Template([string]$templatePath, [string]$outPath, [string[]]$lines) {
  Add-Type -AssemblyName System.Drawing
  $img = [System.Drawing.Image]::FromFile($templatePath)
  try {
    $bmp = New-Object System.Drawing.Bitmap $img
  } finally { $img.Dispose() }
  $g = [System.Drawing.Graphics]::FromImage($bmp)
  $g.TextRenderingHint = [System.Drawing.Text.TextRenderingHint]::AntiAlias
  $s = $bmp.Height / 1080.0
  $font = New-Object System.Drawing.Font('Consolas', [float](15 * $s), [System.Drawing.FontStyle]::Bold)
  $brush = New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::FromArgb(235, 240, 244, 250))
  $x = [float](150 * $s)
  $y = [float]($bmp.Height - 118 * $s)
  foreach ($ln in $lines) {
    $g.DrawString($ln, $font, $brush, $x, $y)
    $y += [float](26 * $s)
  }
  $g.Dispose()
  $tmp = "$outPath.tmp.png"
  $bmp.Save($tmp, [System.Drawing.Imaging.ImageFormat]::Png)
  $bmp.Dispose()
  Move-Item -Force $tmp $outPath
}

# ---------- probe ----------
$isHub = [bool]$cfg.is_hub
$gwOk = (Test-Ping $cfg.gateway_ip)
if (-not $gwOk) { Start-Sleep -Milliseconds 400; $gwOk = (Test-Ping $cfg.gateway_ip) }
$hubIp = if ($isHub) { '127.0.0.1' } else { [string]$cfg.hub_ip }
$hubOk = Test-Tcp $hubIp ([int]$cfg.hub_port) 2500
$gpuOk = Test-Gpu
$deadPorts = @()
foreach ($p in @($cfg.watch_ports)) {
  if (-not (Test-Tcp '127.0.0.1' ([int]$p) 1500)) { $deadPorts += [int]$p }
}
# IP drift: roster says we live at expected_ip; DHCP may disagree (the .176 double-drift history).
$ipDrift = $false
$actualIps = @()
try {
  $actualIps = @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
    Where-Object { $_.IPAddress -like '192.168.*' } | Select-Object -ExpandProperty IPAddress)
  $expIp = [string]$cfg.expected_ip
  if ($expIp -and $actualIps.Count -gt 0 -and ($actualIps -notcontains $expIp)) { $ipDrift = $true }
} catch {}

$state = 'normal'
if (-not $gwOk -and (-not $hubOk -or $isHub)) { $state = 'netdown' }
elseif (-not $gpuOk) { $state = 'gpufault' }
elseif ($ipDrift) { $state = 'ipdrift' }
elseif (-not $hubOk) { $state = 'hubdown' }
elseif ($deadPorts.Count -gt 0) { $state = 'svcdown' }

# ---------- previous state ----------
$prev = @{ state = ''; since = ''; lastTarget = '' }
if (Test-Path $StatePath) {
  try {
    $j = Get-Content $StatePath -Raw -Encoding UTF8 | ConvertFrom-Json
    $prev.state = [string]$j.state; $prev.since = [string]$j.since; $prev.lastTarget = [string]$j.lastTarget
  } catch {}
}
$now = Get-Date -Format 'HH:mm:ss'
$since = if ($prev.state -eq $state -and $prev.since) { $prev.since } else { $now }

# ---------- act ----------
$target = ''
if ($state -eq 'normal') {
  $normalName = if ([string]$cfg.mode -eq 'showcase') { 'showcase.png' } else { 'normal.png' }
  $target = Join-Path $WpDir $normalName
  if ($isHub -and $cfg.board_cmd) {
    $boardPng = Join-Path $Base 'board.png'
    try {
      $proc = Start-Process -FilePath 'cmd.exe' -ArgumentList ('/c ' + [string]$cfg.board_cmd) `
        -WindowStyle Hidden -PassThru
      if ($proc.WaitForExit(50000) -and $proc.ExitCode -eq 0 -and (Test-Path $boardPng)) {
        $target = $boardPng
      } else {
        try { $proc.Kill() } catch {}
        Log ('board render failed exit=' + $proc.ExitCode + ' -> fallback static')
      }
    } catch { Log ('board render error: ' + $_.Exception.Message) }
  }
  elseif ((-not $isHub) -and $cfg.board_url -and ([string]$cfg.mode -ne 'showcase')) {
    # v4: pull this machine's perspective board from the hub; any failure -> static.
    $boardPng = Join-Path $Base 'board.png'
    $tmp = $boardPng + '.tmp'
    try {
      Invoke-WebRequest -Uri ([string]$cfg.board_url) -OutFile $tmp -TimeoutSec 5 -UseBasicParsing | Out-Null
      if ((Test-Path $tmp) -and ((Get-Item $tmp).Length -gt 10240)) {
        Move-Item -Force $tmp $boardPng
        $target = $boardPng
      } else {
        Remove-Item $tmp -Force -ErrorAction SilentlyContinue
        Log 'board fetch: response too small -> fallback static'
      }
    } catch {
      Remove-Item $tmp -Force -ErrorAction SilentlyContinue
      Log ('board fetch failed: ' + $_.Exception.Message)
    }
  }
  if (($prev.lastTarget -ne $target) -or ($target -like '*board.png')) { Set-Wallpaper $target }
} else {
  $tmpl = Join-Path $WpDir ($state + '.png')
  if (-not (Test-Path $tmpl)) { Log ("missing template " + $tmpl); exit 1 }
  $slot = if ($prev.lastTarget -like '*stamped_a.png') { 'stamped_b.png' } else { 'stamped_a.png' }
  $stamped = Join-Path $Base $slot
  $l1 = ('STATE={0}  since {1}  checked {2}  host={3}' -f $state.ToUpper(), $since, $now, [string]$cfg.id)
  $l2 = switch ($state) {
    'svcdown' { 'dead ports: ' + (($deadPorts | ForEach-Object { $_.ToString() }) -join ', ') }
    'hubdown' { ('hub {0}:{1} tcp timeout; gateway ok' -f $hubIp, [string]$cfg.hub_port) }
    'netdown' { ('gateway {0} no reply' -f [string]$cfg.gateway_ip) }
    'gpufault' { 'nvidia-smi -L failed or timed out' }
    'ipdrift' { ('expected {0}  actual {1}' -f [string]$cfg.expected_ip, ($actualIps -join '/')) }
    default { '' }
  }
  Stamp-Template $tmpl $stamped @($l1, $l2)
  Set-Wallpaper $stamped
  $target = $stamped
}

@{ state = $state; since = $since; lastTarget = $target } | ConvertTo-Json -Compress |
  Set-Content $StatePath -Encoding ascii
if ($prev.state -ne $state) {
  Log ('state ' + $prev.state + ' -> ' + $state + ' target=' + (Split-Path $target -Leaf))
}

# ---------- v5: telemetry agent watchdog (state-independent, every minute) ----------
$telAgent = Join-Path $Base 'telemetry_agent.ps1'
if ((Test-Path $telAgent) -and (Get-Command nvidia-smi -ErrorAction SilentlyContinue)) {
  $telPidFile = Join-Path $Base 'telemetry.pid'
  $telAlive = $false
  if (Test-Path $telPidFile) {
    try {
      $tp = [int](Get-Content $telPidFile -ErrorAction Stop)
      if (Get-Process -Id $tp -ErrorAction SilentlyContinue) { $telAlive = $true }
    } catch {}
  }
  if (-not $telAlive) {
    Start-Process -WindowStyle Hidden -FilePath 'powershell.exe' -ArgumentList @(
      '-NoProfile', '-ExecutionPolicy', 'Bypass', '-WindowStyle', 'Hidden', '-File', $telAgent)
    Log 'telemetry agent (re)started'
  }
}

# ---------- self status blob (hub board collects this over ssh; ascii only) ----------
# nvidia-smi MUST run under a hard timeout: a wedged driver query hung the whole sentinel
# for 10+ minutes on 2026-08-06 (minute task refuses to overlap -> desktop board froze).
$vramUsed = -1; $vramTotal = -1; $gpuUtil = -1
$smi = Get-Command nvidia-smi -ErrorAction SilentlyContinue
if ($smi) {
  try {
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $smi.Source
    $psi.Arguments = '--query-gpu=memory.used,memory.total,utilization.gpu --format=csv,noheader,nounits'
    $psi.UseShellExecute = $false
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $pq = [System.Diagnostics.Process]::Start($psi)
    if ($pq.WaitForExit(6000)) {
      $q = $pq.StandardOutput.ReadLine()
      if ($q -match '^\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)') {
        $vramUsed = [int]$Matches[1]; $vramTotal = [int]$Matches[2]; $gpuUtil = [int]$Matches[3]
      }
    } else {
      try { $pq.Kill() } catch {}
      Log 'vram query timed out (6s), skipped'
    }
  } catch {}
}
@{
  id = [string]$cfg.id; state = $state; since = $since
  checked = (Get-Date -Format 'yyyy-MM-dd HH:mm:ss')
  ips = $actualIps; dead_ports = $deadPorts
  vram_used_mb = $vramUsed; vram_total_mb = $vramTotal; gpu_util = $gpuUtil
} | ConvertTo-Json -Compress | Set-Content (Join-Path $Base 'status.json') -Encoding ascii

# ---------- v6: live wallpaper data bridge (2026-08-29) ----------
# When livewp/index.html exists (Lively dynamic wallpaper deployed), write data.js:
# self state (incl. fault states -> page shows its own alert banner, closing the
# "Lively hides sentinel warning wallpapers" gap) + hub /api/history cluster round.
# file:// pages cannot fetch cross-origin (hub sends no CORS header); a same-dir
# <script> reload is exempt, so the sentinel is the bridge. All soft-fail.
$lwDir = Join-Path $Base 'livewp'
if (Test-Path (Join-Path $lwDir 'index.html')) {
  $histRaw = '[]'
  try {
    $hr = Invoke-WebRequest -Uri ('http://' + $hubIp + ':7913/api/history?n=1') -TimeoutSec 5 -UseBasicParsing
    if ($hr.StatusCode -eq 200 -and $hr.Content) { $histRaw = [string]$hr.Content }
  } catch {}
  try {
    $lwSelf = @{
      id = [string]$cfg.id; state = $state; since = $since
      checked = (Get-Date -Format 'yyyy-MM-dd HH:mm:ss')
      dead_ports = $deadPorts
      vram_used_mb = $vramUsed; vram_total_mb = $vramTotal; gpu_util = $gpuUtil
      wrote_epoch = [DateTimeOffset]::Now.ToUnixTimeSeconds()
    } | ConvertTo-Json -Compress
    $lwJs = 'window.HUD_DATA={self:' + $lwSelf + ',hist:' + $histRaw + '};'
    [IO.File]::WriteAllText((Join-Path $lwDir 'data.js'), $lwJs, [Text.UTF8Encoding]::new($false))
  } catch {}
}
exit 0
