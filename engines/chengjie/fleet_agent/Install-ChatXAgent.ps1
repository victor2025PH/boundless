# Install-ChatXAgent.ps1 -- one-shot installer for the ChatX fleet node Agent (Windows).
#
# What it does (idempotent, re-run to upgrade):
#   1. get chatx-agent.exe   (-Exe local file, or download -DownloadUrl; sha256 checked when -Sha256 given
#                             or when manifest.json sits next to the exe URL)
#   2. copy to %ProgramFiles%\ChatX Agent\  (stops the running task first)
#   3. register the local ChatX instance (auto-detects %APPDATA%\<app>\data\config\config.yaml,
#                             default backend http://127.0.0.1:18799)
#   4. enroll with the one-time code (-Code) -> node_key in %ProgramData%\ChatX\fleet\agent.json
#   5. install-service       (scheduled task "ChatX Fleet Agent", ONSTART, SYSTEM) and start it
#
# Usage (run as Administrator):
#   powershell -ExecutionPolicy Bypass -File Install-ChatXAgent.ps1 -Controller https://bd2026.cc/fleet -Code ABCD-1234
#   ... -Exe .\chatx-agent.exe                     # offline: use a local exe instead of downloading
#   ... -NoEnroll                                  # upgrade only, keep existing enrollment
#   ... -InstanceUrl http://127.0.0.1:18799 -AuthToken xxx   # override auto-detection
#
# ASCII only (PowerShell 5.1 + GBK console lesson). Never prints node_key / auth tokens.
[CmdletBinding()]
param(
  [string]$Controller = "https://bd2026.cc/fleet",
  [string]$Code = "",
  [string]$Exe = "",
  [string]$DownloadUrl = "https://bd2026.cc/downloads/fleet/chatx-agent.exe",
  [string]$Sha256 = "",
  [string]$InstallDir = (Join-Path $env:ProgramFiles "ChatX Agent"),
  [string]$InstanceName = "chatx",
  [string]$InstanceUrl = "http://127.0.0.1:18799",
  [string]$ConfigPath = "",
  [string]$AuthToken = "",
  [switch]$NoInstance,
  [switch]$NoEnroll
)

$ErrorActionPreference = 'Stop'
function Say($m) { Write-Host "[chatx-agent] $m" }
function Fail($m) { Write-Host "[chatx-agent] ERROR: $m" -ForegroundColor Red; exit 1 }

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) { Fail "run this script as Administrator (scheduled task needs SYSTEM)" }
if (-not $NoEnroll -and -not $Code) { Fail "-Code <enroll code> is required (or -NoEnroll for upgrade-only)" }

# 1. obtain exe
$tmp = Join-Path $env:TEMP ("chatx-agent-" + [guid]::NewGuid().ToString('N').Substring(0, 8) + ".exe")
if ($Exe) {
  if (-not (Test-Path -LiteralPath $Exe)) { Fail "exe not found: $Exe" }
  Copy-Item -LiteralPath $Exe -Destination $tmp -Force
} else {
  Say "downloading $DownloadUrl"
  [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
  Invoke-WebRequest -Uri $DownloadUrl -OutFile $tmp -UseBasicParsing
  if (-not $Sha256) {
    try {
      $mfUrl = ($DownloadUrl -replace '[^/]+$', 'manifest.json')
      $mf = Invoke-RestMethod -Uri $mfUrl -UseBasicParsing
      if ($mf.sha256) { $Sha256 = "$($mf.sha256)"; Say "manifest version $($mf.version)" }
    } catch { Say "manifest.json not available, skipping checksum" }
  }
}
if ($Sha256) {
  $got = (Get-FileHash -LiteralPath $tmp -Algorithm SHA256).Hash.ToLower()
  if ($got -ne $Sha256.ToLower()) { Remove-Item $tmp -Force; Fail "sha256 mismatch: got $($got.Substring(0,12)) want $($Sha256.Substring(0,12))" }
  Say "sha256 ok"
}
$ver = & $tmp --help 2>&1 | Select-String -Pattern 'enroll' -Quiet
if (-not $ver) { Fail "downloaded file does not look like chatx-agent" }

# 2. place exe (stop running task first: exe may be locked)
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
$target = Join-Path $InstallDir "chatx-agent.exe"
schtasks /End /TN "ChatX Fleet Agent" 2>$null | Out-Null
Get-Process -Name chatx-agent -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq $target } | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Milliseconds 800
if (Test-Path -LiteralPath $target) { Copy-Item -LiteralPath $target -Destination "$target.bak" -Force }
Move-Item -LiteralPath $tmp -Destination $target -Force
Say "installed $target"

# 3. local instance
$stateDir = Join-Path $env:ProgramData "ChatX\fleet"
New-Item -ItemType Directory -Force -Path $stateDir | Out-Null
if (-not $NoInstance) {
  if (-not $ConfigPath -and -not $AuthToken) {
    $cands = @()
    foreach ($root in @($env:APPDATA, (Join-Path $env:SystemDrive 'Users'))) {
      if ($root) { $cands += Get-ChildItem -Path $root -Filter config.yaml -Recurse -Depth 5 -ErrorAction SilentlyContinue |
                     Where-Object { $_.FullName -match '\\data\\config\\config\.yaml$' } }
    }
    $hit = $cands | Where-Object { (Get-Content -LiteralPath $_.FullName -Raw -ErrorAction SilentlyContinue) -match 'web_admin' } | Select-Object -First 1
    if ($hit) { $ConfigPath = $hit.FullName; Say "detected instance config $ConfigPath" }
    else { Say "no local ChatX config found; instance registered without token (add later: chatx-agent add-instance)" }
  }
  $args = @("--state-dir", $stateDir, "add-instance", "$InstanceName=$InstanceUrl")
  if ($AuthToken) { $args += @("--auth-token", $AuthToken) }
  if ($ConfigPath) { $args += @("--config-path", $ConfigPath) }
  & $target @args | Out-Null
  Say "instance $InstanceName -> $InstanceUrl"
}

# 4. enroll
if (-not $NoEnroll) {
  $out = & $target --state-dir $stateDir enroll --controller $Controller --code $Code 2>&1
  if ($LASTEXITCODE -ne 0) { Fail "enroll failed: $out" }
  try { $j = $out | ConvertFrom-Json; Say "enrolled node_id=$($j.node_id) machine=$($j.machine_id)" } catch { Say "enrolled" }
}

# 5. service
$svc = & $target --state-dir $stateDir install-service 2>&1
if ($LASTEXITCODE -ne 0) { Fail "install-service failed: $svc" }
Say "service installed and started (scheduled task 'ChatX Fleet Agent')"
Start-Sleep -Seconds 2
& $target --state-dir $stateDir service-status
Say "done. logs: $stateDir\logs\agent.log ; status: `"$target`" status"
