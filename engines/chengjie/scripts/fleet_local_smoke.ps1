<#
.SYNOPSIS
  Fleet 阶段2/3 本机冒烟（127.0.0.1）：主控 + Agent enroll/心跳/ping/account_health/pull_overview。

.DESCRIPTION
  - 不改生产、不 push、不碰 bd2026.cc
  - 密钥写到临时目录 secrets.env，绝不 git add
  - 默认 worktree: D:\boundless-fleet-p2\engines\chengjie（可用 -EngineRoot 覆盖）
  - 默认 scratch:  D:\tmp\fleet_local_smoke

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File engines/chengjie/scripts/fleet_local_smoke.ps1
#>
[CmdletBinding()]
param(
  [string]$EngineRoot = "",
  [string]$ScratchDir = "D:\tmp\fleet_local_smoke",
  [int]$ControllerPort = 18798,
  [int]$StubPort = 18790,
  [switch]$SkipInit,
  [switch]$KeepRunning
)

$ErrorActionPreference = "Stop"

function Write-Step($msg) { Write-Host "`n=== $msg ===" -ForegroundColor Cyan }

if (-not $EngineRoot) {
  $here = Split-Path -Parent $MyInvocation.MyCommand.Path
  $EngineRoot = (Resolve-Path (Join-Path $here "..")).Path
}
if (-not (Test-Path (Join-Path $EngineRoot "src\fleet\agent.py"))) {
  throw "EngineRoot 无效（缺 src/fleet/agent.py）: $EngineRoot"
}

New-Item -ItemType Directory -Force -Path $ScratchDir,
  "$ScratchDir\config", "$ScratchDir\agent_state", "$ScratchDir\logs", "$ScratchDir\stub" | Out-Null

$secretsPath = Join-Path $ScratchDir "secrets.env"
$cfgPath = Join-Path $ScratchDir "config\config.yaml"
$stubPy = Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) "fleet_smoke_instance_stub.py"
if (-not (Test-Path $stubPy)) { $stubPy = Join-Path $ScratchDir "stub\instance_stub.py" }

function New-Hex([int]$n) {
  -join ((1..$n) | ForEach-Object { "{0:x}" -f (Get-Random -Max 16) })
}

function Import-Secrets {
  Get-Content $secretsPath -Encoding UTF8 | ForEach-Object {
    $line = $_.TrimStart([char]0xFEFF)
    if ($line -match "^\s*#" -or $line -notmatch "=") { return }
    $k, $v = $line.Split("=", 2)
    Set-Item -Path ("Env:" + $k.Trim()) -Value $v.Trim()
  }
}

function Ensure-Secrets {
  if ((Test-Path $secretsPath) -and $SkipInit) { Import-Secrets; return }
  $auth = New-Hex 48
  $secret = New-Hex 64
  $content = @"
CHATX_FLEET_ADMIN_TOKEN=$auth
CHATX_FLEET_SECRET_KEY=$secret
CHATX_FLEET_CONTROLLER=http://127.0.0.1:$ControllerPort
CHATX_FLEET_STUB_PORT=$StubPort
CHATX_FLEET_STATE_DIR=$ScratchDir\agent_state
"@
  [System.IO.File]::WriteAllText($secretsPath, $content)
  Import-Secrets
  Write-Host "wrote $secretsPath (DO NOT git add)"
}

function Ensure-Config {
  if ((Test-Path $cfgPath) -and $SkipInit) { return }
  Write-Step "init fleet_control config"
  Push-Location $EngineRoot
  try {
    python main.py --init fleet_control --config $cfgPath --force `
      --set "web_admin.auth_token=$env:CHATX_FLEET_ADMIN_TOKEN" `
      --set "web_admin.secret_key=$env:CHATX_FLEET_SECRET_KEY" `
      --set "web_admin.host=127.0.0.1" `
      --set "web_admin.port=$ControllerPort" `
      --set "fleet_control.public_url=http://127.0.0.1:$ControllerPort"
    if ($LASTEXITCODE -ne 0) { throw "main.py --init failed" }
  } finally { Pop-Location }
}

function Start-BgPython([string]$ArgsLine, [string]$OutLog, [string]$ErrLog, [string]$WorkDir) {
  $p = Start-Process -FilePath python -ArgumentList $ArgsLine -WorkingDirectory $WorkDir `
    -WindowStyle Hidden -RedirectStandardOutput $OutLog -RedirectStandardError $ErrLog -PassThru
  return $p
}

function Wait-Http([string]$Url, [int]$Seconds = 90) {
  $deadline = (Get-Date).AddSeconds($Seconds)
  while ((Get-Date) -lt $deadline) {
    try {
      $r = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 2
      if ($r.StatusCode -ge 200 -and $r.StatusCode -lt 500) { return $true }
    } catch { Start-Sleep -Seconds 1 }
  }
  return $false
}

function Stop-PortListeners([int]$Port) {
  $lines = netstat -ano | Select-String ":$Port\s+.*LISTENING"
  foreach ($ln in $lines) {
    if ($ln -match "\s(\d+)\s*$") {
      $procId = [int]$Matches[1]
      if ($pid -gt 0) {
        Write-Host "killing pid $procId on port $Port"
        Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
      }
    }
  }
}

# ── main ──
Ensure-Secrets
Ensure-Config

Write-Step "stop leftover listeners"
Stop-PortListeners $ControllerPort
Stop-PortListeners $StubPort
Start-Sleep -Seconds 1

Write-Step "start instance stub :$StubPort"
$env:STUB_PORT = "$StubPort"
$env:STUB_TOKEN = $env:CHATX_FLEET_ADMIN_TOKEN
Copy-Item $stubPy "$ScratchDir\stub\instance_stub.py" -Force
$stubProc = Start-BgPython "`"$ScratchDir\stub\instance_stub.py`"" `
  "$ScratchDir\logs\stub.out.log" "$ScratchDir\logs\stub.err.log" $ScratchDir
if (-not (Wait-Http "http://127.0.0.1:$StubPort/api/ping" 20)) {
  # ping needs auth — probe without expecting 200 if token required; use a raw connect
  Start-Sleep -Seconds 1
}
try {
  $h = @{ Authorization = "Bearer $($env:CHATX_FLEET_ADMIN_TOKEN)" }
  $pingBody = (Invoke-WebRequest "http://127.0.0.1:$StubPort/api/ping" -Headers $h -UseBasicParsing).Content
  Write-Host "stub /api/ping => $pingBody"
} catch {
  Get-Content "$ScratchDir\logs\stub.err.log" -Tail 40 -ErrorAction SilentlyContinue
  throw "stub failed: $_"
}

Write-Step "start controller :$ControllerPort"
$ctrlProc = Start-BgPython "main.py --config `"$cfgPath`"" `
  "$ScratchDir\logs\controller.out.log" "$ScratchDir\logs\controller.err.log" $EngineRoot
if (-not (Wait-Http "http://127.0.0.1:$ControllerPort/health" 120)) {
  Get-Content "$ScratchDir\logs\controller.err.log" -Tail 80 -ErrorAction SilentlyContinue
  throw "controller failed to become healthy"
}
Write-Host "controller up (pid=$($ctrlProc.Id))"

Push-Location $EngineRoot
try {
  $env:CHATX_FLEET_STATE_DIR = "$ScratchDir\agent_state"
  Write-Step "new enroll code"
  $_prevEap = $ErrorActionPreference; $ErrorActionPreference = "Continue"
  $codeRaw = python -m src.fleet.admin --controller $env:CHATX_FLEET_CONTROLLER --token $env:CHATX_FLEET_ADMIN_TOKEN `
    new-code --label "smoke-local" --group "local-smoke" --ttl-min 60 2>&1 | Out-String
  $ErrorActionPreference = $_prevEap
  $codeRaw | Set-Content "$ScratchDir\logs\new-code.txt" -Encoding UTF8
  if ($codeRaw -notmatch '"code"\s*:\s*"([^"]+)"') { throw "cannot parse enroll code from:`n$codeRaw" }
  $code = $Matches[1]
  Write-Host "enroll_code=$code"

  Write-Step "agent enroll + instance"
  $_prevEap = $ErrorActionPreference; $ErrorActionPreference = "Continue"
  python -m src.fleet.agent --state-dir $env:CHATX_FLEET_STATE_DIR enroll `
    --controller $env:CHATX_FLEET_CONTROLLER --code $code `
    --instance "stub=http://127.0.0.1:$StubPort" --auth-token $env:CHATX_FLEET_ADMIN_TOKEN
  $ErrorActionPreference = $_prevEap
  if ($LASTEXITCODE -ne 0) { throw "enroll failed" }

  $_prevEap = $ErrorActionPreference; $ErrorActionPreference = "Continue"
  $statusJson = python -m src.fleet.agent --state-dir $env:CHATX_FLEET_STATE_DIR status 2>&1 | Out-String
  $ErrorActionPreference = $_prevEap
  if ($statusJson -notmatch '"node_id"\s*:\s*"(n_[a-f0-9]+)"') { throw "no node_id in status" }
  $nodeId = $Matches[1]
  Write-Host "node_id=$nodeId"

  Write-Step "heartbeat"
  $_prevEap = $ErrorActionPreference; $ErrorActionPreference = "Continue"
  python -m src.fleet.agent --state-dir $env:CHATX_FLEET_STATE_DIR heartbeat
  $ErrorActionPreference = $_prevEap
  if ($LASTEXITCODE -ne 0) { throw "heartbeat failed" }

  Write-Step "dispatch ping + run --once"
  $_prevEap = $ErrorActionPreference; $ErrorActionPreference = "Continue"
  python -m src.fleet.admin --controller $env:CHATX_FLEET_CONTROLLER --token $env:CHATX_FLEET_ADMIN_TOKEN `
    task $nodeId ping --payload '{\"echo\":\"local-smoke\"}'
  $ErrorActionPreference = $_prevEap
  $_prevEap = $ErrorActionPreference; $ErrorActionPreference = "Continue"
  $once = python -m src.fleet.agent --state-dir $env:CHATX_FLEET_STATE_DIR run --once --wait 8 2>&1 | Out-String
  $ErrorActionPreference = $_prevEap
  $once | Set-Content "$ScratchDir\logs\run_once_ping.txt" -Encoding UTF8
  if ($once -notmatch '"kind"\s*:\s*"ping"' -or $once -notmatch '"status"\s*:\s*"done"') {
    throw "ping not done:`n$once"
  }
  Write-Host "ping => done"

  Write-Step "dispatch account_health + run --once"
  $_prevEap = $ErrorActionPreference; $ErrorActionPreference = "Continue"
  python -m src.fleet.admin --controller $env:CHATX_FLEET_CONTROLLER --token $env:CHATX_FLEET_ADMIN_TOKEN `
    task $nodeId account_health --target '{\"instance\":\"stub\"}'
  $ErrorActionPreference = $_prevEap
  $_prevEap = $ErrorActionPreference; $ErrorActionPreference = "Continue"
  $once2 = python -m src.fleet.agent --state-dir $env:CHATX_FLEET_STATE_DIR run --once --wait 8 2>&1 | Out-String
  $ErrorActionPreference = $_prevEap
  $once2 | Set-Content "$ScratchDir\logs\run_once_health.txt" -Encoding UTF8
  if ($once2 -notmatch '"kind"\s*:\s*"account_health"' -or $once2 -notmatch '"status"\s*:\s*"done"') {
    throw "account_health not done:`n$once2"
  }
  Write-Host "account_health => done"

  Write-Step "dispatch pull_overview + run --once"
  $_prevEap = $ErrorActionPreference; $ErrorActionPreference = "Continue"
  python -m src.fleet.admin --controller $env:CHATX_FLEET_CONTROLLER --token $env:CHATX_FLEET_ADMIN_TOKEN `
    task $nodeId pull_overview --target '{\"instance\":\"stub\"}'
  $ErrorActionPreference = $_prevEap
  $_prevEap = $ErrorActionPreference; $ErrorActionPreference = "Continue"
  $once3 = python -m src.fleet.agent --state-dir $env:CHATX_FLEET_STATE_DIR run --once --wait 8 2>&1 | Out-String
  $ErrorActionPreference = $_prevEap
  $once3 | Set-Content "$ScratchDir\logs\run_once_overview.txt" -Encoding UTF8
  if ($once3 -notmatch '"kind"\s*:\s*"pull_overview"' -or $once3 -notmatch '"status"\s*:\s*"done"') {
    throw "pull_overview not done:`n$once3"
  }
  Write-Host "pull_overview => done"

  Write-Step "summary"
  $_prevEap = $ErrorActionPreference; $ErrorActionPreference = "Continue"
  python -m src.fleet.admin --controller $env:CHATX_FLEET_CONTROLLER --token $env:CHATX_FLEET_ADMIN_TOKEN tasks --node $nodeId
  $ErrorActionPreference = $_prevEap
  Write-Host "`nSMOKE PASS" -ForegroundColor Green
  Write-Host "scratch: $ScratchDir"
  Write-Host "secrets: $secretsPath (not for git)"
} finally {
  Pop-Location
  if (-not $KeepRunning) {
    Write-Step "cleanup processes"
    if ($ctrlProc -and -not $ctrlProc.HasExited) { Stop-Process -Id $ctrlProc.Id -Force -ErrorAction SilentlyContinue }
    if ($stubProc -and -not $stubProc.HasExited) { Stop-Process -Id $stubProc.Id -Force -ErrorAction SilentlyContinue }
    Stop-PortListeners $ControllerPort
    Stop-PortListeners $StubPort
  } else {
    Write-Host "KeepRunning: controller pid=$($ctrlProc.Id) stub pid=$($stubProc.Id)"
  }
}
