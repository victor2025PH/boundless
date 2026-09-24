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

function Remove-UntrustedState([string]$Dir) {
  # Before icacls /setowner. A planted agent.json would otherwise be adopted and
  # its restart_cmd would later run as SYSTEM.
  foreach ($name in @('agent.json', 'machine_id', 'room.key')) {
    $p = Join-Path $Dir $name
    if (-not (Test-Path -LiteralPath $p)) { continue }
    $sid = (Get-Acl -LiteralPath $p).GetOwner([System.Security.Principal.SecurityIdentifier]).Value
    if ($sid -ne 'S-1-5-18' -and $sid -ne 'S-1-5-32-544') {
      Remove-Item -LiteralPath $p -Force
      Say "removed untrusted $name"
    }
  }
}
function Lock-StateDir([string]$Dir) {
  New-Item -ItemType Directory -Force -Path $Dir | Out-Null
  Remove-UntrustedState $Dir
  Native icacls.exe @($Dir, '/setowner', '*S-1-5-32-544', '/T', '/C') | Out-Null
  if ($NativeExit -ne 0) { Say "icacls setowner failed ($NativeExit)"; exit 1 }
  $children = @(Get-ChildItem -Force -LiteralPath $Dir -ErrorAction SilentlyContinue)
  if ($children.Count -gt 0) {
    Native icacls.exe @((Join-Path $Dir '*'), '/reset', '/T', '/C') | Out-Null
    if ($NativeExit -ne 0) { Say "icacls reset failed ($NativeExit)"; exit 1 }
  }
  Native icacls.exe @($Dir, '/inheritance:r', '/grant:r', '*S-1-5-18:(OI)(CI)F', '*S-1-5-32-544:(OI)(CI)F', '/T', '/C') | Out-Null
  if ($NativeExit -ne 0) { Say "icacls grant failed ($NativeExit)"; exit 1 }
}

if (-not $InstallDir) { $InstallDir = Split-Path -Parent $MyInvocation.MyCommand.Path }
if (-not $StateDir) { $StateDir = Join-Path $env:ProgramData "ChatX\fleet" }
# Lock the dir before machine_id / agent.json / room.key. Abort before any secret write.
Lock-StateDir $StateDir
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
  if ($NativeExit -ne 0) {
    Say "enroll failed; installing the service so it can retry"
  } else {
    $status = ''
    try { $status = [string](($out | ConvertFrom-Json).status) } catch { $status = '' }
    if ($status -eq 'pending') { Say "installed; waiting for admin approval in the console" }
    elseif ($status -eq 'active') { Say "enrolled" }
    else { Say "enroll finished" }
  }
}

$svc = Native $target @('--state-dir', $StateDir, 'install-service')
if ($NativeExit -ne 0) { Say "install-service failed"; exit 1 }
Say "service installed (scheduled task ChatX Fleet Agent)"
exit 0
