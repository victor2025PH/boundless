# bootstrap.ps1 -- run by ChatXAgentSetup.exe after the files are copied. Already elevated.
# Detects local ChatX / AvatarHub, enrolls (room key file, or pending approval), installs the service.
# ASCII only. Never prints node_key or the room key.
[CmdletBinding()]
param(
  [string]$Controller = "https://bd2026.cc/fleet",
  [string]$InstallDir = "",
  [string]$StateDir = ""
)
$ErrorActionPreference = 'Stop'
function Say($m) { Write-Host "[chatx-agent] $m" }
function Native([string]$exe, [string[]]$a) {
  $old = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
  try { $o = & $exe @a 2>&1 | ForEach-Object { "$_" }; $script:NativeExit = $LASTEXITCODE; return ($o -join "`n") }
  finally { $ErrorActionPreference = $old }
}

if (-not $InstallDir) { $InstallDir = Split-Path -Parent $MyInvocation.MyCommand.Path }
if (-not $StateDir) { $StateDir = Join-Path $env:ProgramData "ChatX\fleet" }
New-Item -ItemType Directory -Force -Path $StateDir | Out-Null
$target = Join-Path $InstallDir "chatx-agent.exe"
if (-not (Test-Path -LiteralPath $target)) { Say "missing $target"; exit 1 }

$keyFile = Join-Path $StateDir "room.key"
$stRaw = Native $target @('--state-dir', $StateDir, 'status')
$enrollment = 'none'
try { $enrollment = [string](($stRaw | ConvertFrom-Json).enrollment) } catch { $enrollment = 'none' }

if ($enrollment -eq 'enrolled') {
  Say "already enrolled"
  if (Test-Path -LiteralPath $keyFile) { Remove-Item -LiteralPath $keyFile -Force -ErrorAction SilentlyContinue }
} else {
  $enrollArgs = @('--state-dir', $StateDir, 'enroll', '--controller', $Controller, '--detect')
  if (Test-Path -LiteralPath $keyFile) { $enrollArgs += @('--room-key-file', $keyFile) }
  $out = Native $target $enrollArgs
  if ($NativeExit -ne 0) { Say "enroll failed"; exit 1 }
  $status = ''
  try { $status = [string](($out | ConvertFrom-Json).status) } catch { $status = '' }
  if ($status -eq 'pending') { Say "installed; waiting for admin approval in the console" }
  elseif ($status -eq 'active') { Say "enrolled" }
  else { Say "enroll finished" }
}

$svc = Native $target @('--state-dir', $StateDir, 'install-service')
if ($NativeExit -ne 0) { Say "install-service failed"; exit 1 }
Say "service installed (scheduled task ChatX Fleet Agent)"
exit 0
