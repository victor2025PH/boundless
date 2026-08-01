# smoke_chatx_node.ps1 -- prove an INSTALLED ChatX actually works on this machine.
#
# Runs ON THE TARGET. Structural checks (file exists, version string) only prove
# the installer copied bytes; they cannot tell you the product runs. This starts
# the bundled backend.exe -- the exact process the Electron shell spawns -- on a
# throwaway data dir and a free port, then drives real endpoints.
#
# Side-effect free: temp AITR_DATA_DIR (seeded fresh from the bundled config),
# random port, process killed at the end. Never touches an existing install's data.
# ASCII-only (PS 5.1 reads BOM-less UTF-8 as GBK).
#
# Exit: 0 all good / 2 backend missing / 3 never became ready / 4 a check failed
[CmdletBinding()]
param(
  [int]$ReadyTimeoutSec = 180,
  [switch]$KeepData,
  # Acceptance test for the 2026-07-31 "一键开齐入站识别 点了没用" fix, run against
  # the SHIPPED build: pressing the preset writes config.local.yaml, which trips a
  # config hot reload ~30s later. Before the fix that reload wiped the hosted vision
  # backend out of memory -- the button destroyed the very thing it had just turned
  # on, and the self-check went amber until the next restart. Costs ~1 min.
  [switch]$MediaRegression
)
$ErrorActionPreference = 'Continue'
function Say($m) { Write-Output ("[smoke] " + $m) }

$dir = Join-Path $env:LOCALAPPDATA 'Programs\telegram-ai-desktop'
$backend = Join-Path $dir 'resources\backend\backend.exe'
if (-not (Test-Path $backend)) { Say "backend not found: $backend"; exit 2 }

$app = Get-ChildItem $dir -Filter *.exe | Where-Object { $_.Name -notlike 'Uninstall*' } | Select-Object -First 1
Say ("app version: " + $app.VersionInfo.FileVersion)

# free port
$l = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, 0)
$l.Start(); $port = $l.LocalEndpoint.Port; $l.Stop()
$data = Join-Path $env:TEMP ("chatx-smoke-" + [guid]::NewGuid().ToString('N').Substring(0, 8))
New-Item -ItemType Directory -Force -Path $data | Out-Null
Say "port=$port data=$data"

$env:AITR_DESKTOP_MODE = '1'
$env:AITR_MANAGED_EDITION = '1'      # packaged builds run managed; match it
$env:AITR_DATA_DIR = $data
$env:AITR_WEB_HOST = '127.0.0.1'
$env:AITR_WEB_PORT = "$port"
$env:AITR_WEB_TOKEN = 'admin'
$proc = Start-Process -FilePath $backend -WorkingDirectory $data -PassThru -WindowStyle Hidden
Say ("backend pid=" + $proc.Id)

$base = "http://127.0.0.1:$port"
$fail = 0
try {
  $ready = $false
  $sw = [Diagnostics.Stopwatch]::StartNew()
  while ($sw.Elapsed.TotalSeconds -lt $ReadyTimeoutSec) {
    if ($proc.HasExited) { Say ("backend exited early, code=" + $proc.ExitCode); exit 3 }
    try {
      $r = Invoke-WebRequest -Uri "$base/login" -UseBasicParsing -TimeoutSec 5
      if ($r.StatusCode -eq 200) { $ready = $true; break }
    } catch { Start-Sleep -Seconds 2 }
  }
  if (-not $ready) { Say "never became ready in ${ReadyTimeoutSec}s"; exit 3 }
  Say ("ready in " + [math]::Round($sw.Elapsed.TotalSeconds, 1) + "s")

  $sess = New-Object Microsoft.PowerShell.Commands.WebRequestSession
  Invoke-WebRequest -Uri "$base/login" -Method Post -WebSession $sess -UseBasicParsing -TimeoutSec 20 `
    -Body @{ username = 'admin'; password = 'admin' } | Out-Null

  function Check($name, $path, $needle) {
    try {
      $resp = Invoke-WebRequest -Uri ($base + $path) -WebSession $sess -UseBasicParsing -TimeoutSec 30
      $ok = ($resp.StatusCode -eq 200)
      if ($needle -and ($resp.Content -notmatch $needle)) { $ok = $false }
      Say ("  " + $(if ($ok) { "OK  " } else { "FAIL" }) + " $name  ($path)")
      if (-not $ok) { $script:fail++ }
    } catch {
      Say ("  FAIL $name  ($path) -> " + $_.Exception.Message)
      $script:fail++
    }
  }

  Say "endpoints:"
  Check 'workbench page'  '/workspace'                          $null
  Check 'me api'          '/api/workspace/me'                   $null
  Check 'channel wizard'  '/workspace/setup'                    'swMediaRetry'
  Check 'media selfcheck' '/api/companion/media-capabilities'   '"capabilities"'
  Check 'feature center'  '/api/setup/features'                 $null
  # P2-198: one-click diagnostic bundle (probe mode answers {ok:true} without
  # building the zip; an old build 404s here -> the settings card stays hidden)
  Check 'diag bundle probe' '/api/admin/diagnostic-bundle?probe=1' '"ok"'

  # today's fix: the self-check must report a machine-readable reason + whether a
  # retry can fix it, instead of the old dead-end "contact support" copy.
  try {
    $j = (Invoke-WebRequest -Uri "$base/api/companion/media-capabilities" -WebSession $sess `
          -UseBasicParsing -TimeoutSec 30).Content | ConvertFrom-Json
    $vis = $j.capabilities | Where-Object { $_.key -eq 'vision_inbound' }
    Say ("  vision stage=" + $vis.stage + " reason=" + $vis.reason + " retryable=" + $vis.retryable)
    if ($null -eq $vis.reason) { Say "  FAIL reason field missing (old build?)"; $script:fail++ }
  } catch { Say ("  FAIL media json -> " + $_.Exception.Message); $script:fail++ }

  if ($MediaRegression) {
    Say "media regression (press the preset, then survive a config hot reload):"
    function VisionStage() {
      $j = (Invoke-WebRequest -Uri "$base/api/companion/media-capabilities" -WebSession $sess `
            -UseBasicParsing -TimeoutSec 30).Content | ConvertFrom-Json
      return ($j.capabilities | Where-Object { $_.key -eq 'vision_inbound' })
    }
    $before = VisionStage
    Say ("  before preset: stage=" + $before.stage)
    # Write requests need the CSRF pair (cookie + header) exactly like the browser
    # does -- without it the backend answers 403 and the test measures nothing.
    $tok = ($sess.Cookies.GetCookies($base) | Where-Object { $_.Name -eq 'csrf_token' }).Value
    Say ("  csrf token acquired: " + [bool]$tok)
    try {
      $r = Invoke-WebRequest -Uri "$base/api/companion/media-capabilities/preset" -Method Post `
        -WebSession $sess -UseBasicParsing -TimeoutSec 60 -ContentType 'application/json' `
        -Headers @{ 'X-CSRF-Token' = $tok } `
        -Body '{"name":"understand_all"}'
      $pj = $r.Content | ConvertFrom-Json
      Say ("  preset applied=" + $pj.ok + " provisioned_vision=" + $pj.provisioned.vision)
    } catch { Say ("  FAIL preset -> " + $_.Exception.Message); $script:fail++ }
    $mid = VisionStage
    Say ("  right after preset: stage=" + $mid.stage)
    # Hot reload is throttled to 30s and only fires on a request checkpoint, so
    # wait past the window and then keep poking to make sure it actually ran.
    Start-Sleep -Seconds 35
    for ($i = 0; $i -lt 6; $i++) {
      Invoke-WebRequest -Uri "$base/api/workspace/me" -WebSession $sess -UseBasicParsing -TimeoutSec 20 | Out-Null
      Start-Sleep -Seconds 2
    }
    $after = VisionStage
    Say ("  after hot reload: stage=" + $after.stage + " reason=" + $after.reason)
    if ($after.stage -ne 'active') {
      Say "  FAIL vision lost its backend across the hot reload (the original bug is back)"
      $script:fail++
    } else {
      Say "  OK  vision still active after the reload"
    }
  }
} finally {
  if ($proc -and -not $proc.HasExited) {
    Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2
  }
  Get-Process -Name backend -ErrorAction SilentlyContinue |
    Where-Object { $_.Path -like "*$([IO.Path]::GetFileName($data))*" } |
    Stop-Process -Force -ErrorAction SilentlyContinue
  if (-not $KeepData) { Remove-Item $data -Recurse -Force -ErrorAction SilentlyContinue }
}

if ($fail -gt 0) { Say "FAILED checks: $fail"; exit 4 }
Say "OK - installed build serves the workbench"
exit 0
