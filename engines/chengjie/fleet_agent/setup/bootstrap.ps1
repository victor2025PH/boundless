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

function Test-Reparse([string]$Path) {
  $item = Get-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue
  if (-not $item) { return $false }
  return [bool]($item.Attributes -band [IO.FileAttributes]::ReparsePoint)
}
function Assert-StateParent([string]$Dir) {
  # Parent must be SYSTEM or Administrators. A junction on either path can
  # redirect /setowner /T and /reset /T at an arbitrary directory.
  $parent = Split-Path -Parent $Dir
  if (Test-Reparse $parent) { Say "parent is a reparse point"; exit 3 }
  if (Test-Reparse $Dir) { Say "state dir is a reparse point"; exit 3 }
  if (-not (Test-Path -LiteralPath $parent)) {
    New-Item -ItemType Directory -Force -Path $parent | Out-Null
    Native icacls.exe @($parent, '/setowner', '*S-1-5-32-544') | Out-Null
    if ($NativeExit -ne 0) { Say "parent setowner failed ($NativeExit)"; exit 3 }
  }
  if (Test-Reparse $parent) { Say "parent is a reparse point"; exit 3 }
  if (Test-Reparse $Dir) { Say "state dir is a reparse point"; exit 3 }
  $sid = (Get-Acl -LiteralPath $parent).GetOwner([System.Security.Principal.SecurityIdentifier]).Value
  if ($sid -ne 'S-1-5-18' -and $sid -ne 'S-1-5-32-544') { Say "parent owner is not trusted"; exit 3 }
}
function Test-DirLocked([string]$Dir) {
  $acl = Get-Acl -LiteralPath $Dir
  $sid = $acl.GetOwner([System.Security.Principal.SecurityIdentifier]).Value
  if ($sid -ne 'S-1-5-18' -and $sid -ne 'S-1-5-32-544') { return $false }
  if (-not $acl.AreAccessRulesProtected) { return $false }
  $aces = @($acl.Access)
  if ($aces.Count -lt 1) { return $false }
  foreach ($ace in $aces) {
    $id = $ace.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value
    if ($id -ne 'S-1-5-18' -and $id -ne 'S-1-5-32-544') { return $false }
  }
  return $true
}
function Remove-Sensitive([string]$Dir, [bool]$Force) {
  # A failed delete must abort. /setowner /T would otherwise adopt the file.
  foreach ($name in @('agent.json', 'machine_id', 'room.key')) {
    $p = Join-Path $Dir $name
    if (-not (Test-Path -LiteralPath $p)) { continue }
    $drop = $Force
    if (-not $drop) {
      $sid = (Get-Acl -LiteralPath $p).GetOwner([System.Security.Principal.SecurityIdentifier]).Value
      $drop = ($sid -ne 'S-1-5-18' -and $sid -ne 'S-1-5-32-544')
    }
    if ($drop) {
      Remove-Item -LiteralPath $p -Force
      if (Test-Path -LiteralPath $p) { throw "could not delete $name" }
      Say "removed $name"
    }
  }
}
function Lock-StateDir([string]$Dir) {
  Assert-StateParent $Dir
  New-Item -ItemType Directory -Force -Path $Dir | Out-Null
  if (Test-Reparse $Dir) { Say "state dir is a reparse point"; exit 3 }
  Native icacls.exe @($Dir, '/setowner', '*S-1-5-32-544') | Out-Null
  if ($NativeExit -ne 0) { Say "icacls setowner failed ($NativeExit)"; exit 3 }
  # /reset with no /T drops explicit ACEs /grant:r would otherwise leave behind.
  Native icacls.exe @($Dir, '/reset') | Out-Null
  if ($NativeExit -ne 0) { Say "icacls reset failed ($NativeExit)"; exit 3 }
  Native icacls.exe @($Dir, '/inheritance:r', '/grant:r', '*S-1-5-18:(OI)(CI)F', '*S-1-5-32-544:(OI)(CI)F') | Out-Null
  if ($NativeExit -ne 0) { Say "icacls grant failed ($NativeExit)"; exit 3 }
  try { if (-not (Test-DirLocked $Dir)) { Say "state dir ACL is not locked"; exit 3 } } catch { Say "could not read state dir ACL"; exit 3 }
  try { Remove-Sensitive $Dir $false } catch { Say "could not delete untrusted state"; exit 3 }
  Native icacls.exe @($Dir, '/setowner', '*S-1-5-32-544', '/T', '/C') | Out-Null
  if ($NativeExit -ne 0) { Say "icacls setowner failed ($NativeExit)"; exit 3 }
  $children = @(Get-ChildItem -Force -LiteralPath $Dir -ErrorAction SilentlyContinue)
  if ($children.Count -gt 0) {
    Native icacls.exe @((Join-Path $Dir '*'), '/reset', '/T', '/C') | Out-Null
    if ($NativeExit -ne 0) { Say "icacls reset failed ($NativeExit)"; exit 3 }
  }
  Native icacls.exe @($Dir, '/inheritance:r', '/grant:r', '*S-1-5-18:(OI)(CI)F', '*S-1-5-32-544:(OI)(CI)F', '/T', '/C') | Out-Null
  if ($NativeExit -ne 0) { Say "icacls grant failed ($NativeExit)"; exit 3 }
  try { if (-not (Test-DirLocked $Dir)) { Say "state dir ACL is not locked"; exit 3 } } catch { Say "could not read state dir ACL"; exit 3 }
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
