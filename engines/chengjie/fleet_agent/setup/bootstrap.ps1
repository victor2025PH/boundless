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
  $item = Get-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue
  if (-not $item) { return $false }
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
  # Parent gets a protected DACL before fleet is created or reset. A junction
  # on either path can redirect /setowner /T and /reset /T at an arbitrary directory.
  $parent = Split-Path -Parent $Dir
  if (Test-Reparse $parent) { Say "parent is a reparse point"; exit 3 }
  if (Test-Reparse $Dir) { Say "state dir is a reparse point"; exit 3 }
  if (-not (Test-Path -LiteralPath $parent)) {
    New-Item -ItemType Directory -Force -Path $parent | Out-Null
  }
  if (Test-Reparse $parent) { Say "parent is a reparse point"; exit 3 }
  if (Test-Reparse $Dir) { Say "state dir is a reparse point"; exit 3 }
  $parentLocked = $false
  try { $parentLocked = Test-ParentLocked $parent } catch { $parentLocked = $false }
  if (-not $parentLocked) {
    Native icacls.exe @($parent, '/setowner', '*S-1-5-32-544') | Out-Null
    if ($NativeExit -ne 0) { Say "parent setowner failed ($NativeExit)"; exit 3 }
    Native icacls.exe @($parent, '/reset') | Out-Null
    if ($NativeExit -ne 0) { Say "parent reset failed ($NativeExit)"; exit 3 }
    Native icacls.exe @($parent, '/inheritance:r', '/grant:r', '*S-1-5-18:(OI)(CI)F', '*S-1-5-32-544:(OI)(CI)F', '*S-1-5-32-545:(OI)(CI)RX') | Out-Null
    if ($NativeExit -ne 0) { Say "parent grant failed ($NativeExit)"; exit 3 }
  }
  try { if (-not (Test-ParentLocked $parent)) { Say "parent owner is not trusted"; exit 3 } } catch { Say "could not read parent ACL"; exit 3 }
  if (Test-Reparse $parent) { Say "parent is a reparse point"; exit 3 }
  if (Test-Reparse $Dir) { Say "state dir is a reparse point"; exit 3 }
}
function Assert-NoChildReparse([string]$Dir) {
  # Queue walk. Get-ChildItem -Recurse would follow a junction.
  $pending = New-Object System.Collections.Generic.Queue[string]
  $pending.Enqueue($Dir)
  while ($pending.Count -gt 0) {
    $cur = $pending.Dequeue()
    foreach ($c in @(Get-ChildItem -Force -LiteralPath $cur -ErrorAction SilentlyContinue)) {
      if ($c.Attributes -band [IO.FileAttributes]::ReparsePoint) { Say "child reparse point"; exit 3 }
      if ($c.PSIsContainer) { $pending.Enqueue($c.FullName) }
    }
  }
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
  $existed = Test-Path -LiteralPath $Dir
  New-Item -ItemType Directory -Force -Path $Dir | Out-Null
  if (Test-Reparse $Dir) { Say "state dir is a reparse point"; exit 3 }
  # Sample before /reset. An already-locked dir must not be unlocked again.
  $wasLocked = $false
  if ($existed) {
    try { $wasLocked = Test-DirLocked $Dir } catch { $wasLocked = $false }
  }
  if (-not $wasLocked) {
    Native icacls.exe @($Dir, '/setowner', '*S-1-5-32-544') | Out-Null
    if ($NativeExit -ne 0) { Say "icacls setowner failed ($NativeExit)"; exit 3 }
    # /reset with no /T drops explicit ACEs /grant:r would otherwise leave behind.
    Native icacls.exe @($Dir, '/reset') | Out-Null
    if ($NativeExit -ne 0) { Say "icacls reset failed ($NativeExit)"; exit 3 }
    Native icacls.exe @($Dir, '/inheritance:r', '/grant:r', '*S-1-5-18:(OI)(CI)F', '*S-1-5-32-544:(OI)(CI)F') | Out-Null
    if ($NativeExit -ne 0) { Say "icacls grant failed ($NativeExit)"; exit 3 }
  }
  try { if (-not (Test-DirLocked $Dir)) { Say "state dir ACL is not locked"; exit 3 } } catch { Say "could not read state dir ACL"; exit 3 }
  Assert-NoChildReparse $Dir
  try { Remove-Sensitive $Dir $false } catch { Say "could not delete untrusted state"; exit 3 }
  if (-not $wasLocked) {
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
}

if (-not $InstallDir) { $InstallDir = Split-Path -Parent $MyInvocation.MyCommand.Path }
if (-not $StateDir) { $StateDir = Join-Path $env:ProgramData "ChatX\fleet" }
# Copy agent.json before the lock deletes or adopts an unlocked file.
$snap = ""
if ($Snapshot -and (Test-Path -LiteralPath $Snapshot)) {
  $snap = $Snapshot
} else {
  $preLocked = $false
  if (Test-Path -LiteralPath $StateDir) {
    try { $preLocked = Test-DirLocked $StateDir } catch { $preLocked = $false }
  }
  $agentJson = Join-Path $StateDir "agent.json"
  if ((-not $preLocked) -and (Test-Path -LiteralPath $agentJson)) {
    $snap = Join-Path $env:TEMP ("chatx-agent-migrate-" + [guid]::NewGuid().ToString("N") + ".json")
    Copy-Item -LiteralPath $agentJson -Destination $snap -Force
  }
}
# Lock the dir before machine_id / agent.json / room.key. Abort before any secret write.
Lock-StateDir $StateDir
$target = Join-Path $InstallDir "chatx-agent.exe"
if (-not (Test-Path -LiteralPath $target)) { Say "missing $target"; exit 1 }
if ($snap) {
  Native $target @('--state-dir', $StateDir, 'migrate-legacy', '--controller', $Controller, '--snapshot', $snap) | Out-Null
  Remove-Item -LiteralPath $snap -Force -ErrorAction SilentlyContinue
  if ($NativeExit -ne 0) { Say "migrate failed"; exit 3 }
  Native $target @('--state-dir', $StateDir, 'detect') | Out-Null
  if ($NativeExit -ne 0) { Say "detect failed"; exit 3 }
}

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
