# bootstrap.ps1 -- run by ChatXAgentSetup.exe after the files are copied. Already elevated.
# Detects local ChatX / AvatarHub, enrolls (room key file, or pending approval), installs the service.
# ASCII only. Never prints node_key or the room key.
[CmdletBinding()]
param(
  [string]$Controller = "https://bd2026.cc/fleet",
  [string]$InstallDir = "",
  [string]$StateDir = "",
  [string]$Snapshot = ""
)
$ErrorActionPreference = 'Stop'
function Say($m) { Write-Host "[chatx-agent] $m" }
function Native([string]$exe, [string[]]$a) {
  $old = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
  try { $o = & $exe @a 2>&1 | ForEach-Object { "$_" }; $script:NativeExit = $LASTEXITCODE; return ($o -join "`n") }
  finally { $ErrorActionPreference = $old }
}

function Test-Reparse([string]$Path) {
  # Missing is not a reparse point. Any other failure is treated as one.
  if (-not (Test-Path -LiteralPath $Path)) { return $false }
  try { $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop }
  catch { return $true }
  return [bool]($item.Attributes -band [IO.FileAttributes]::ReparsePoint)
}
function Test-ParentLocked([string]$Dir) {
  # SYSTEM and Administrators full control. Users may have read and execute only.
  $acl = Get-Acl -LiteralPath $Dir
  $sid = $acl.GetOwner([System.Security.Principal.SecurityIdentifier]).Value
  if ($sid -ne 'S-1-5-18' -and $sid -ne 'S-1-5-32-544') { return $false }
  if (-not $acl.AreAccessRulesProtected) { return $false }
  $aces = @($acl.Access)
  if ($aces.Count -lt 1) { return $false }
  $write = 0x2 -bor 0x4 -bor 0x10 -bor 0x40 -bor 0x100 -bor 0x10000 -bor 0x40000 -bor 0x80000 -bor 0x10000000 -bor 0x40000000
  foreach ($ace in $aces) {
    $id = $ace.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value
    if ($id -eq 'S-1-5-18' -or $id -eq 'S-1-5-32-544') { continue }
    if ($id -eq 'S-1-5-32-545' -and $ace.AccessControlType -eq 'Allow' -and (([int]$ace.FileSystemRights -band $write) -eq 0)) { continue }
    return $false
  }
  return $true
}
function Assert-StateParent([string]$Dir) {
  # Parent gets a protected DACL before fleet is renamed or created.
  $parent = Split-Path -Parent $Dir
  if (Test-Reparse $parent) { throw "parent is a reparse point" }
  if (-not (Test-Path -LiteralPath $parent)) {
    New-Item -ItemType Directory -Force -Path $parent | Out-Null
  }
  if (Test-Reparse $parent) { throw "parent is a reparse point" }
  $parentLocked = $false
  try { $parentLocked = Test-ParentLocked $parent } catch { $parentLocked = $false }
  if (-not $parentLocked) {
    Native icacls.exe @($parent, '/setowner', '*S-1-5-32-544') | Out-Null
    if ($NativeExit -ne 0) { throw "parent setowner failed ($NativeExit)" }
    Native icacls.exe @($parent, '/reset') | Out-Null
    if ($NativeExit -ne 0) { throw "parent reset failed ($NativeExit)" }
    Native icacls.exe @($parent, '/inheritance:r', '/grant:r', '*S-1-5-18:(OI)(CI)F', '*S-1-5-32-544:(OI)(CI)F', '*S-1-5-32-545:(OI)(CI)RX') | Out-Null
    if ($NativeExit -ne 0) { throw "parent grant failed ($NativeExit)" }
  }
  try { if (-not (Test-ParentLocked $parent)) { throw "parent owner is not trusted" } } catch { throw }
  if (Test-Reparse $parent) { throw "parent is a reparse point" }
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
function Lock-StateDir([string]$Dir) {
  # An unlocked or reparse fleet is renamed aside. icacls runs only on a new empty directory.
  Assert-StateParent $Dir
  $present = Test-Path -LiteralPath $Dir
  $wasLocked = $false
  if ($present -and -not (Test-Reparse $Dir)) {
    try { $wasLocked = Test-DirLocked $Dir } catch { $wasLocked = $false }
  }
  if ($present -and -not $wasLocked) {
    $legacy = (Split-Path -Leaf $Dir) + ".legacy-" + [guid]::NewGuid().ToString("N")
    Rename-Item -LiteralPath $Dir -NewName $legacy
    $present = $false
  }
  if (-not $present) {
    New-Item -ItemType Directory -Path $Dir | Out-Null
    Native icacls.exe @($Dir, '/setowner', '*S-1-5-32-544') | Out-Null
    if ($NativeExit -ne 0) { throw "icacls setowner failed ($NativeExit)" }
    Native icacls.exe @($Dir, '/reset') | Out-Null
    if ($NativeExit -ne 0) { throw "icacls reset failed ($NativeExit)" }
    Native icacls.exe @($Dir, '/inheritance:r', '/grant:r', '*S-1-5-18:(OI)(CI)F', '*S-1-5-32-544:(OI)(CI)F') | Out-Null
    if ($NativeExit -ne 0) { throw "icacls grant failed ($NativeExit)" }
  }
  if (Test-Reparse $Dir) { throw "state dir is a reparse point" }
  try { if (-not (Test-DirLocked $Dir)) { throw "state dir ACL is not locked" } } catch { throw }
}

if (-not $InstallDir) { $InstallDir = Split-Path -Parent $MyInvocation.MyCommand.Path }
if (-not $StateDir) { $StateDir = Join-Path $env:ProgramData "ChatX\fleet" }
# Copy agent.json before an unlocked fleet is renamed aside. Never follow a reparse point.
$snap = ""
if ($Snapshot -and (Test-Path -LiteralPath $Snapshot)) {
  $snap = $Snapshot
} elseif ((Test-Path -LiteralPath $StateDir) -and -not (Test-Reparse $StateDir)) {
  $preLocked = $false
  try { $preLocked = Test-DirLocked $StateDir } catch { $preLocked = $false }
  $agentJson = Join-Path $StateDir "agent.json"
  if ((-not $preLocked) -and (Test-Path -LiteralPath $agentJson) -and -not (Test-Reparse $agentJson)) {
    $snap = Join-Path $env:TEMP ("chatx-agent-migrate-" + [guid]::NewGuid().ToString("N") + ".json")
    Copy-Item -LiteralPath $agentJson -Destination $snap -Force
  }
}
$lockFailed = $false
try {
  # Lock the dir before machine_id / agent.json / room.key. Abort before any secret write.
  Lock-StateDir $StateDir
  $target = Join-Path $InstallDir "chatx-agent.exe"
  if (-not (Test-Path -LiteralPath $target)) { throw "missing $target" }
  if ($snap) {
    Native $target @('--state-dir', $StateDir, 'migrate-legacy', '--controller', $Controller, '--snapshot', $snap) | Out-Null
    if ($NativeExit -ne 0) { throw "migrate failed" }
    Native $target @('--state-dir', $StateDir, 'detect') | Out-Null
    if ($NativeExit -ne 0) { throw "detect failed" }
  }
} catch {
  Say "$_"
  $lockFailed = $true
} finally {
  if ($snap) { Remove-Item -LiteralPath $snap -Force -ErrorAction SilentlyContinue }
}
if ($lockFailed) { exit 3 }
$target = Join-Path $InstallDir "chatx-agent.exe"

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
