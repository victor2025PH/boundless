# ASCII-ONLY: cockpit_kiosk.ps1 -- U-cockpit kiosk shell (C0.5, 2026-08-13)
#
# Turns each cluster PC into a "native appliance": on logon it lights up all
# attached displays with the right HUD page, fullscreen, zero address bar,
# zero permission prompts, zero hand-typed URLs.
#
#   - Fetches the cockpit ledger from the hub:  GET <hub>/api/cockpit
#     (single source of truth: screens/order/params live in cockpit.json)
#   - Detects which cluster machine this host is (local IPv4 match)
#   - Maps this machine's screens (by cockpit "order") onto the physical
#     displays left-to-right (by desktop X coordinate)
#   - Launches one Edge kiosk window per display with flags that:
#       * treat the hub http origin as secure  -> getUserMedia works on LAN
#         (kills the "microphone unavailable on IP origin" trap)
#       * auto-accept camera/microphone capture prompts
#   - Watchdog: -Watch is the keep-alive. Page-alive = this machine appears
#     in hub /health.screens (SSE connected). Edge process alone is NOT
#     enough (zombie tab). Hub down = hold, never kill. Two consecutive
#     misses when Edge is alive = zombie relaunch (grace vs hub restart).
#     No Edge = relaunch immediately. Never kill a healthy on-air page.
#   - LAN JSON uses an empty proxy (Windows system proxy hijacks LAN).
#   - Session-0 refuse: SSH / service session never Start-Process Edge
#     (invisible session-0 windows). Launch only from an interactive session
#     or via schtasks /run of an /it task.
#
# Usage (run on ANY cluster machine):
#   powershell -ExecutionPolicy Bypass -File cockpit_kiosk.ps1              # launch now
#   ... -Stop                    # close all kiosk windows (Watch will revive in <=4 min)
#   ... -Register                # copy to Public dir + logon task + 2-min Watch task
#   ... -Watch                   # keep-alive: relaunch when page is not on-air
#   ... -Machine yunsheng        # override machine autodetection
#   ! -Windowed                  # debug: small framed windows instead of kiosk
#
# One-line bootstrap on a fresh machine (hub serves this file at /kiosk.ps1):
#   powershell -c "iwr http://192.168.0.176:7913/kiosk.ps1 -OutFile $env:TEMP\ck.ps1; powershell -ExecutionPolicy Bypass -File $env:TEMP\ck.ps1 -Register"

param(
  [string]$Hub = "http://192.168.0.176:7913",
  [string]$Machine = "",
  [switch]$Stop,
  [switch]$Register,
  [switch]$Watch,
  [switch]$Windowed
)

$ErrorActionPreference = "Stop"
$MARK = "boundless-kiosk"   # embedded in user-data-dir; -Stop kills by this mark
$STAMP = "C:\Users\Public\boundless-hud\kiosk_watch.json"

function Find-Edge {
  foreach ($p in @("$env:ProgramFiles (x86)\Microsoft\Edge\Application\msedge.exe",
                   "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe")) {
    if (Test-Path $p) { return $p }
  }
  throw "msedge.exe not found"
}

function Stop-Kiosk {
  $n = 0
  Get-CimInstance Win32_Process -Filter "Name='msedge.exe'" | ForEach-Object {
    if ($_.CommandLine -match $MARK) {
      # child helpers die with the parent between enumeration and kill -> ignore
      try { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue } catch {}
      $n++
    }
  }
  Write-Host "[kiosk] closed $n edge processes"
}

function Count-Kiosk {
  $n = 0
  Get-CimInstance Win32_Process -Filter "Name='msedge.exe'" | ForEach-Object {
    if ($_.CommandLine -match $MARK) { $n++ }
  }
  return $n
}

function Test-InteractiveSession {
  # Session 0 = SSH / service: Edge windows never reach the console desktop.
  return ((Get-Process -Id $PID).SessionId -ne 0)
}

function Get-HubJson([string]$Rel) {
  # Empty proxy: urllib/IRM default to the Windows system proxy (LAN 502-alive trap).
  $url = $Hub.TrimEnd('/') + $Rel
  try {
    $req = [System.Net.HttpWebRequest]::Create($url)
    $req.Method = "GET"
    $req.Timeout = 8000
    $req.Proxy = [System.Net.GlobalProxySelection]::GetEmptyWebProxy()
    $resp = $req.GetResponse()
    try {
      $sr = New-Object IO.StreamReader($resp.GetResponseStream())
      $txt = $sr.ReadToEnd()
      $sr.Close()
    } finally { $resp.Close() }
    return $txt | ConvertFrom-Json
  } catch {
    return $null
  }
}

function Send-KioskStat([string]$K, [string]$M, [string]$V) {
  try {
    $body = "{`"k`":`"$K`",`"m`":`"$M`",`"v`":`"$V`",`"src`":`"watch`"}"
    $bytes = [Text.Encoding]::UTF8.GetBytes($body)
    $req = [System.Net.HttpWebRequest]::Create(($Hub.TrimEnd('/') + "/api/stat"))
    $req.Method = "POST"
    $req.Timeout = 4000
    $req.ContentType = "application/json"
    $req.ContentLength = $bytes.Length
    $req.Proxy = [System.Net.GlobalProxySelection]::GetEmptyWebProxy()
    $st = $req.GetRequestStream()
    $st.Write($bytes, 0, $bytes.Length)
    $st.Close()
    $resp = $req.GetResponse(); $resp.Close()
  } catch {}
}

function Write-WatchStamp([int]$Miss, [string]$Mid) {
  $obj = @{ miss = $Miss; machine = $Mid; ts = [int][DateTimeOffset]::Now.ToUnixTimeSeconds() }
  ($obj | ConvertTo-Json -Compress) | Set-Content -Path $STAMP -Encoding ASCII
}

function Read-WatchMiss {
  if (-not (Test-Path $STAMP)) { return 0 }
  try {
    $st = Get-Content $STAMP -Raw | ConvertFrom-Json
    return [int]$st.miss
  } catch { return 0 }
}

function Find-MyMachine($data) {
  if (-not $data) { return "" }
  $myIps = @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
             ForEach-Object { $_.IPAddress })
  if ($data.machines) {
    foreach ($p in $data.machines.PSObject.Properties) {
      if ($myIps -contains $p.Value.ip) { return [string]$p.Name }
    }
  }
  return ""
}

function Get-LiveNames($health, $tour) {
  $n = @()
  if ($health -and $health.screens) { $n = @($health.screens) }
  if ($n.Count -eq 0 -and $tour -and $tour.screens) {
    $n = @($tour.screens.PSObject.Properties | ForEach-Object { $_.Name })
  }
  return @($n)
}

if ($Stop) { Stop-Kiosk; exit 0 }

if ($Register) {
  $dst = "C:\Users\Public\boundless-hud\cockpit_kiosk.ps1"
  New-Item -ItemType Directory -Force -Path (Split-Path $dst) | Out-Null
  $src = [IO.Path]::GetFullPath($PSCommandPath)
  $dstFull = [IO.Path]::GetFullPath($dst)
  if ($src -ne $dstFull) { Copy-Item -Force $PSCommandPath $dst }
  $mid = $Machine
  if (-not $mid) {
    $ck = Get-HubJson "/api/cockpit"
    $mid = Find-MyMachine $ck
  }
  $tr = "powershell -ExecutionPolicy Bypass -WindowStyle Hidden -File $dst -Hub $Hub"
  schtasks /create /f /tn BoundlessCockpitKiosk /sc onlogon /it /tr $tr | Out-Null
  $trw = $tr + " -Watch"
  if ($mid) { $trw = $trw + " -Machine $mid" }
  schtasks /create /f /tn BoundlessCockpitKioskWatch /sc minute /mo 2 /it /tr $trw | Out-Null
  Write-Host "[kiosk] registered logon task BoundlessCockpitKiosk -> $dst"
  Write-Host "[kiosk] registered keep-alive BoundlessCockpitKioskWatch (2 min, page-alive)"
  if (-not (Test-InteractiveSession)) {
    Write-Host "[kiosk] session 0 -- not launching Edge; run: schtasks /run /tn BoundlessCockpitKiosk"
    exit 0
  }
  Write-Host "[kiosk] launching now..."
  & powershell -ExecutionPolicy Bypass -File $dst -Hub $Hub
  exit 0
}

if ($Watch) {
  $alive = Count-Kiosk
  $health = Get-HubJson "/health"
  if (-not $health) {
    Write-Host "[kiosk] watch: hub unreachable -- hold"
    exit 0
  }
  $tour = $null
  $live = Get-LiveNames $health $null
  if ($live.Count -eq 0) {
    $tour = Get-HubJson "/api/tour"
    $live = Get-LiveNames $health $tour
  }
  if (-not $Machine) {
    $ck = Get-HubJson "/api/cockpit"
    $Machine = Find-MyMachine $ck
  }
  $onAir = $Machine -and ($live -contains $Machine)
  if ($onAir) {
    Write-WatchStamp 0 $Machine
    Write-Host "[kiosk] watch ok: page live ($Machine)"
    exit 0
  }
  if (-not $Machine) {
    if ($alive -gt 0) {
      Write-Host "[kiosk] watch: unknown machine, edge alive -- hold"
      exit 0
    }
    Write-Host "[kiosk] watch: unknown machine, no edge -- relaunching"
  } else {
    $miss = (Read-WatchMiss) + 1
    Write-WatchStamp $miss $Machine
    if ($alive -eq 0) {
      Write-Host "[kiosk] watch: page missing, no edge -- relaunching"
      Send-KioskStat "kiosk_watch" $Machine "relaunch_dead"
    } elseif ($miss -lt 2) {
      Write-Host "[kiosk] watch: page missing miss $miss/2 -- hold"
      exit 0
    } else {
      Write-Host "[kiosk] watch: page zombie -- relaunching"
      Send-KioskStat "kiosk_watch" $Machine "relaunch_zombie"
    }
  }
}

# ---- fetch ledger ----
$data = Get-HubJson "/api/cockpit"
if (-not $data -or -not $data.ok) { throw "hub /api/cockpit not ok" }
$screensAll = @($data.cockpit.screens | Sort-Object order)

# ---- which machine am I ----
if (-not $Machine) { $Machine = Find-MyMachine $data }
if (-not $Machine) { throw "cannot autodetect machine; use -Machine <id>" }
$mine = @($screensAll | Where-Object { $_.machine -eq $Machine })
if (-not $mine.Count) { throw "no screens for machine '$Machine' in cockpit.json" }

# ---- physical displays, left to right ----
Add-Type -AssemblyName System.Windows.Forms
$disp = @([System.Windows.Forms.Screen]::AllScreens | Sort-Object { $_.Bounds.X })
$n = [Math]::Min($disp.Count, $mine.Count)
if ($disp.Count -ne $mine.Count) {
  Write-Host "[kiosk] note: $($disp.Count) displays vs $($mine.Count) ledger screens -> using first $n"
}

if (-not (Test-InteractiveSession)) {
  Write-Host "[kiosk] session 0 -- refuse to launch Edge (invisible). Use schtasks /run /tn BoundlessCockpitKiosk"
  exit 1
}

Stop-Kiosk   # idempotent relaunch
$edge = Find-Edge
for ($i = 0; $i -lt $n; $i++) {
  $scr = $mine[$i]; $d = $disp[$i].Bounds
  $qs = "machine=$Machine&screen=$($scr.id)"
  if ($scr.params) { $qs = "$qs&$($scr.params)" }
  # cache-buster: Edge kiosk profile 对同 URL 吃缓存旧 HTML(AGENTS 已录坑),每次启动唯一化
  $qs = "$qs&r=$([DateTimeOffset]::Now.ToUnixTimeSeconds())"
  $url = "$Hub/hud?$qs"
  $prof = "$env:LOCALAPPDATA\$MARK\$($scr.id)"
  $args = @(
    "--user-data-dir=$prof", "--no-first-run", "--disable-session-crashed-bubble",
    "--unsafely-treat-insecure-origin-as-secure=$Hub",
    "--use-fake-ui-for-media-stream",
    "--autoplay-policy=no-user-gesture-required",
    "--window-position=$($d.X),$($d.Y)"
  )
  if ($Windowed) { $args += @("--app=$url", "--window-size=1200,800") }
  else { $args += @("--kiosk", $url, "--edge-kiosk-type=fullscreen") }
  Start-Process -FilePath $edge -ArgumentList $args
  Write-Host "[kiosk] display$i ($($d.X),$($d.Y)) -> $($scr.id) $url"
  Start-Sleep -Milliseconds 400
}
Write-Host "[kiosk] done: $n window(s) for '$Machine'"
