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
#   - Watchdog-friendly: re-running is idempotent (old kiosk windows for a
#     screen are closed first).
#
# Usage (run on ANY cluster machine):
#   powershell -ExecutionPolicy Bypass -File cockpit_kiosk.ps1              # launch now
#   ... -Stop                    # close all kiosk windows
#   ... -Register                # copy to Public dir + logon task (once per machine)
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
  [switch]$Windowed
)

$ErrorActionPreference = "Stop"
$MARK = "boundless-kiosk"   # embedded in user-data-dir; -Stop kills by this mark

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

if ($Stop) { Stop-Kiosk; exit 0 }

if ($Register) {
  $dst = "C:\Users\Public\boundless-hud\cockpit_kiosk.ps1"
  New-Item -ItemType Directory -Force -Path (Split-Path $dst) | Out-Null
  Copy-Item -Force $PSCommandPath $dst
  $tr = "powershell -ExecutionPolicy Bypass -WindowStyle Hidden -File $dst -Hub $Hub"
  schtasks /create /f /tn BoundlessCockpitKiosk /sc onlogon /it /tr $tr | Out-Null
  Write-Host "[kiosk] registered logon task BoundlessCockpitKiosk -> $dst"
  Write-Host "[kiosk] launching now..."
  & powershell -ExecutionPolicy Bypass -File $dst -Hub $Hub
  exit 0
}

# ---- fetch ledger ----
$data = Invoke-RestMethod -Uri "$Hub/api/cockpit" -TimeoutSec 8
if (-not $data.ok) { throw "hub /api/cockpit not ok" }
$screensAll = @($data.cockpit.screens | Sort-Object order)

# ---- which machine am I ----
if (-not $Machine) {
  $myIps = @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
             ForEach-Object { $_.IPAddress })
  foreach ($p in $data.machines.PSObject.Properties) {
    if ($myIps -contains $p.Value.ip) { $Machine = $p.Name; break }
  }
}
if (-not $Machine) { throw "cannot autodetect machine (local IPs: $($myIps -join ', ')); use -Machine <id>" }
$mine = @($screensAll | Where-Object { $_.machine -eq $Machine })
if (-not $mine.Count) { throw "no screens for machine '$Machine' in cockpit.json" }

# ---- physical displays, left to right ----
Add-Type -AssemblyName System.Windows.Forms
$disp = @([System.Windows.Forms.Screen]::AllScreens | Sort-Object { $_.Bounds.X })
$n = [Math]::Min($disp.Count, $mine.Count)
if ($disp.Count -ne $mine.Count) {
  Write-Host "[kiosk] note: $($disp.Count) displays vs $($mine.Count) ledger screens -> using first $n"
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
