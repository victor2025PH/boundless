# livewp_ensure.ps1 - make sure the boundless live wallpaper is what Lively shows (ASCII-ONLY source).
# Idempotent; safe to run every minute. Deployed to C:\Users\Public\boundless-hud\livewp_ensure.ps1
# by deploy/hud-livewp/push_livewp.ps1 and driven from two places, both in the INTERACTIVE session:
#   1. BoundlessLively task (at logon)           -> livewp_ensure.ps1
#   2. sentinel.ps1 v7 drift watchdog (per minute) -> livewp_ensure.ps1     (only when player != livewp)
#   3. push_livewp.ps1 -SetWallpaper one-shot     -> livewp_ensure.ps1 -Force
# Why (2026-09-17, 173 incident): Lively restores wallpapers per display DeviceId. The 173 Samsung 4K
# re-enumerates as SAM0FEE or SAM0FEF between boots; livewp was saved under FEF, the stock "Fluids"
# under FEE -> after a reboot Lively logged "Screen missing, skipping restoration of boundless-livewp"
# and showed Fluids for two days while the sentinel kept writing data.js underneath. Logon-only
# `Start-Process Lively.exe` (the old lively_launch.ps1) cannot catch that; this script checks what
# the player is actually rendering and re-applies only when needed (no flash on a healthy box).
# Exit codes: 0 showing livewp / nothing to do, 1 Lively not installed for this user, 2 re-apply failed.
[CmdletBinding()]
param(
  [switch]$Force,
  [int]$PlayerWaitSec = 30,
  [int]$ApplyWaitSec = 25
)
$ErrorActionPreference = 'Continue'
$Base = 'C:\Users\Public\boundless-hud'
$Index = Join-Path $Base 'livewp\index.html'
$LogPath = Join-Path $Base 'livewp_ensure.log'
$LastPath = Join-Path $Base 'livewp_ensure.last'

function Log([string]$msg) {
  $line = ('{0} {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $msg)
  Add-Content -Path $LogPath -Value $line -Encoding ascii
  if ((Test-Path $LogPath) -and ((Get-Item $LogPath).Length -gt 262144)) {
    Get-Content $LogPath -Tail 200 | Set-Content $LogPath -Encoding ascii
  }
}
function Get-PlayerUrls() {
  $out = @()
  try {
    Get-CimInstance Win32_Process -Filter "name='Lively.Player.WebView2.exe'" -ErrorAction Stop | ForEach-Object {
      $cl = [string]$_.CommandLine
      if ($cl -match '--wallpaper-url\s+"([^"]+)"') { $out += $Matches[1] } else { $out += '?' }
    }
  } catch {}
  return $out
}
function Test-Showing() {
  foreach ($u in (Get-PlayerUrls)) { if ($u -ieq $Index) { return $true } }
  return $false
}
function Get-UiWindows() {
  return @(Get-Process 'Lively.UI.WinUI' -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowHandle -ne [IntPtr]::Zero })
}

if (-not (Test-Path $Index)) { Log 'no livewp deployed (index.html missing) - nothing to do'; exit 0 }

# ---------- Lively binary + library project (per-user paths: task runs as the console user) ----------
# Literal 'C:\Program Files' is listed too: under a 32-bit host $env:ProgramFiles is the (x86) dir.
$exe = @(
  (Join-Path $env:LOCALAPPDATA 'Programs\Lively Wallpaper\Lively.exe'),
  'C:\Program Files\Lively Wallpaper\Lively.exe',
  (Join-Path $env:ProgramFiles 'Lively Wallpaper\Lively.exe'),
  (Join-Path ${env:ProgramFiles(x86)} 'Lively Wallpaper\Lively.exe')
) | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $exe) { Log ('LIVELY_NOT_FOUND for user ' + $env:USERNAME); exit 1 }

$lib = Join-Path $env:LOCALAPPDATA 'Lively Wallpaper\Library\wallpapers\boundless-livewp'
$infoPath = Join-Path $lib 'LivelyInfo.json'
$info = '{"AppVersion":"2.2.1.0","Title":"BOUNDLESS LIVE CLUSTER","Thumbnail":"","Preview":"",' +
  '"Desc":"stargate particles + machine identity + live cluster panel","Author":"BOUNDLESS",' +
  '"License":"","Contact":"","Type":1,' +
  '"FileName":"C:\\Users\\Public\\boundless-hud\\livewp\\index.html",' +
  '"Arguments":"","IsAbsolutePath":true,"Id":"boundless-livewp"}'
$haveInfo = $false
if (Test-Path $infoPath) {
  try { $haveInfo = ((Get-Content $infoPath -Raw) -like '*boundless-hud\\livewp\\index.html*') } catch {}
}
if (-not $haveInfo) {
  New-Item -ItemType Directory -Force -Path $lib | Out-Null
  [IO.File]::WriteAllText($infoPath, $info, [Text.UTF8Encoding]::new($false))
  Log 'library project LivelyInfo.json (re)written'
}

# ---------- Lively core up? ----------
$justStarted = $false
if (-not (Get-Process 'Lively' -ErrorAction SilentlyContinue)) {
  Start-Process -FilePath $exe
  $justStarted = $true
  $t0 = Get-Date
  while (-not (Get-Process 'Lively' -ErrorAction SilentlyContinue)) {
    if (((Get-Date) - $t0).TotalSeconds -gt 30) { Log 'Lively.exe did not come up within 30s'; break }
    Start-Sleep -Milliseconds 500
  }
  Log 'Lively started'
}

# ---------- what is the player rendering? ----------
# Give Lively time to restore its own layout first (fresh start / logon); stop waiting as soon as a
# player is visible - if it is on livewp we are done, if it is on anything else that is the drift.
# A core that has been up for a while and still has no player is not going to grow one: short wait.
$waitSec = $PlayerWaitSec
if (-not $justStarted) {
  try {
    $core = Get-Process 'Lively' -ErrorAction Stop | Sort-Object StartTime | Select-Object -First 1
    if (((Get-Date) - $core.StartTime).TotalSeconds -gt 90) { $waitSec = [Math]::Min($PlayerWaitSec, 6) }
  } catch {}
}
$seen = @()
$t0 = Get-Date
while ($true) {
  $seen = @(Get-PlayerUrls)
  if ($seen.Count -gt 0) { break }
  if (((Get-Date) - $t0).TotalSeconds -gt $waitSec) { break }
  Start-Sleep -Seconds 2
}
$showing = $false
foreach ($u in $seen) { if ($u -ieq $Index) { $showing = $true } }
if ($showing -and -not $Force) {
  [IO.File]::WriteAllText($LastPath, [string][DateTimeOffset]::Now.ToUnixTimeSeconds(), [Text.ASCIIEncoding]::new())
  exit 0
}
$why = if ($Force) { 'forced' } elseif ($seen.Count -eq 0) { 'no player after ' + $waitSec + 's' } else { 'player on: ' + ($seen -join ' | ') }
Log ('re-apply (' + $why + ')' + $(if ($justStarted) { ' [cold start]' } else { '' }))
[IO.File]::WriteAllText($LastPath, [string][DateTimeOffset]::Now.ToUnixTimeSeconds(), [Text.ASCIIEncoding]::new())

# ---------- re-apply via CLI (proven recipe: closewp all, then setwp the library project folder) ----------
$uiBefore = (Get-UiWindows).Count
Start-Process -FilePath $exe -ArgumentList @('closewp', '--monitor', '-1')
Start-Sleep -Seconds 4
Start-Process -FilePath $exe -ArgumentList @('setwp', '--file', ('"' + $lib + '"'))
$t0 = Get-Date
$ok = $false
while (-not $ok) {
  Start-Sleep -Seconds 2
  $ok = Test-Showing
  if (-not $ok -and ((Get-Date) - $t0).TotalSeconds -gt $ApplyWaitSec) { break }
}
# setwp/closewp may raise the Lively main window; if it was not open before, send it back to the tray.
if ($uiBefore -eq 0) {
  $ui = Get-UiWindows
  if ($ui.Count -gt 0) {
    try {
      Add-Type -Namespace BlWp -Name Win -MemberDefinition '[DllImport("user32.dll")] public static extern bool PostMessage(System.IntPtr h, uint m, System.IntPtr w, System.IntPtr l);' -ErrorAction SilentlyContinue
      foreach ($p in $ui) { [BlWp.Win]::PostMessage($p.MainWindowHandle, 0x0010, [IntPtr]::Zero, [IntPtr]::Zero) | Out-Null }
      Log 'Lively UI window closed back to tray'
    } catch {}
  }
}
if ($ok) { Log 'OK livewp showing'; exit 0 }
Log ('FAILED: player after setwp = ' + ((Get-PlayerUrls) -join ' | '))
exit 2
