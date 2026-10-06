# bootstrap.ps1 -- run by ChatXAgentSetup.exe after the files are copied. Already elevated.
# Detects local ChatX / AvatarHub, enrolls (room key file, or pending approval), installs the service.
# ASCII only. Never prints node_key or the room key.
[CmdletBinding()]
param(
  [string]$Controller = "https://bd2026.cc/fleet",
  [string]$InstallDir = "",
  [string]$StateDir = "",
  [string]$Snapshot = "",
  # Keep a machine_id cached by agent 0.3.4 or older as-is (setup /KEEPIDENTITY=1).
  [switch]$KeepIdentity,
  # Phone-room only (setup /MANAGEADBSERVER=1). Writes adb_manage_server=true.
  # A live-stream host is refused by the agent as well as by the check below.
  [switch]$ManageAdbServer
)
$ErrorActionPreference = 'Stop'
# Pin PSModulePath to this Windows PowerShell 5.1's own module dirs. Started from pwsh 7
# (directly, or via a setup launched from pwsh 7) the process inherits a 7.x PSModulePath
# and 5.1 cannot autoload Get-Acl / Get-FileHash / ScheduledTasks.
if ($PSVersionTable.PSVersion.Major -le 5) {
  $env:PSModulePath = (Join-Path $PSHOME 'Modules') + ';' + (Join-Path $env:ProgramFiles 'WindowsPowerShell\Modules')
}
function Say($m) { Write-Host "[chatx-agent] $m" }
function Native([string]$exe, [string[]]$a) {
  $old = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
  try { $o = & $exe @a 2>&1 | ForEach-Object { "$_" }; $script:NativeExit = $LASTEXITCODE; return ($o -join "`n") }
  finally { $ErrorActionPreference = $old }
}
# stdout only. The agent logs to stderr (e.g. "machine_id=... source=os_guid" right
# after a migration); merged into the JSON it broke ConvertFrom-Json, the status
# read as 'none' and an enrolled node sent a redundant pending request (issue g4).
function NativeOut([string]$exe, [string[]]$a) {
  $old = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
  try { $o = & $exe @a 2>$null | ForEach-Object { "$_" }; $script:NativeExit = $LASTEXITCODE; return ($o -join "`n") }
  finally { $ErrorActionPreference = $old }
}
function Test-LiveStreamHost([string]$Dir) {
  $v = [string]$env:CHATX_FLEET_LIVE_STREAM
  $low = $v.ToLower()
  if ($low -eq '1' -or $low -eq 'true' -or $low -eq 'yes' -or $low -eq 'on') { return $true }
  $flags = @()
  if ($env:ProgramData) { $flags += (Join-Path $env:ProgramData 'ChatX\live-stream.flag') }
  if ($Dir) {
    $flags += (Join-Path $Dir 'live-stream.flag')
    $parent = Split-Path -Parent $Dir
    if ($parent) { $flags += (Join-Path $parent 'live-stream.flag') }
  }
  foreach ($f in $flags) {
    if ($f -and (Test-Path -LiteralPath $f)) { return $true }
  }
  return $false
}
function Enable-PhoneAdb([string]$Exe, [string]$Dir) {
  # Opt in only. Does not download or launch adb; the agent does that later, and only off a live-stream host.
  if (Test-LiveStreamHost $Dir) {
    Say "live-stream host: bundled adb server management stays off"
    return
  }
  if (-not (Test-Path -LiteralPath (Join-Path $Dir 'agent.json'))) {
    Say "agent.json missing; bundled adb server management not written"
    return
  }
  NativeOut $Exe @('--state-dir', $Dir, 'enable-phone-adb') | Out-Null
  if ($NativeExit -ne 0) { throw "could not enable bundled adb server management" }
  Say "bundled adb server management enabled"
}
function Test-PlatformToolsAdb([string]$Dir) {
  # Presence only. Do not download or launch adb.
  $paths = @('C:\platform-tools\adb.exe')
  if ($Dir) { $paths += (Join-Path $Dir 'platform-tools\adb.exe') }
  if ($env:ProgramData) { $paths += (Join-Path $env:ProgramData 'ChatX\platform-tools\adb.exe') }
  if ($env:ProgramFiles) { $paths += (Join-Path $env:ProgramFiles 'ChatX Agent\platform-tools\adb.exe') }
  if ($env:ANDROID_SDK_ROOT) { $paths += (Join-Path $env:ANDROID_SDK_ROOT 'platform-tools\adb.exe') }
  if ($env:ANDROID_HOME) { $paths += (Join-Path $env:ANDROID_HOME 'platform-tools\adb.exe') }
  if ($env:LOCALAPPDATA) { $paths += (Join-Path $env:LOCALAPPDATA 'Android\Sdk\platform-tools\adb.exe') }
  foreach ($p in $paths) {
    if ($p -and (Test-Path -LiteralPath $p)) { return $true }
  }
  $cmd = Get-Command adb.exe -ErrorAction SilentlyContinue
  if (-not $cmd) { $cmd = Get-Command adb -ErrorAction SilentlyContinue }
  if ($cmd -and ([string]$cmd.Source) -like '*\platform-tools\adb.exe') { return $true }
  return $false
}
function Write-InstallFinish([string]$Dir, [string]$HostName, [string]$Short, [bool]$AdbOk) {
  # ASCII note for the setup finish page. No secrets.
  $safeHost = ([string]$HostName) -replace '[^\x20-\x7E]', ''
  if (-not $safeHost) { $safeHost = ([string]$env:COMPUTERNAME) -replace '[^\x20-\x7E]', '' }
  $safeShort = ([string]$Short) -replace '[^A-Za-z0-9\-]', ''
  if ($safeShort.Length -gt 16) { $safeShort = $safeShort.Substring(0, 16) }
  $adbFlag = '0'
  if ($AdbOk) { $adbFlag = '1' }
  $path = Join-Path $Dir 'install-finish.txt'
  $body = "host=$safeHost`r`nshort=$safeShort`r`nadb=$adbFlag`r`n"
  [System.IO.File]::WriteAllText($path, $body)
}
function Stop-StrayAgent([string]$exe) {
  # Any "run" process of this exe started outside the scheduled task (issue g3).
  try {
    Get-CimInstance Win32_Process -Filter "Name='chatx-agent.exe'" -ErrorAction Stop |
      Where-Object { $_.ExecutablePath -eq $exe -and ([string]$_.CommandLine) -match '\srun(\s|$)' } |
      ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
  } catch { Say "could not list agent processes: $_" }
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
function Copy-DefaultUiMap([string]$FromDir, [string]$ToDir, [string]$Kept) {
  # Create the state-dir map only when it is absent. A kept snapshot wins over the default.
  $dest = Join-Path $ToDir 'phone_ui_map.json'
  if (Test-Path -LiteralPath $dest) { return }
  if ($Kept -and (Test-Path -LiteralPath $Kept)) {
    Copy-Item -LiteralPath $Kept -Destination $dest -Force
    return
  }
  $src = Join-Path $FromDir 'phone_ui_map.json'
  if (Test-Path -LiteralPath $src) {
    Copy-Item -LiteralPath $src -Destination $dest -Force
    Say "placed default phone_ui_map.json"
  }
}

if (-not $InstallDir) { $InstallDir = Split-Path -Parent $MyInvocation.MyCommand.Path }
if (-not $StateDir) { $StateDir = Join-Path $env:ProgramData "ChatX\fleet" }
# Copy agent.json before an unlocked fleet is renamed aside. Never follow a reparse point.
$snap = ""
$uiSnap = ""
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
  $uiMap = Join-Path $StateDir "phone_ui_map.json"
  if ((-not $preLocked) -and (Test-Path -LiteralPath $uiMap) -and -not (Test-Reparse $uiMap)) {
    $uiSnap = Join-Path $env:TEMP ("chatx-phone-ui-map-" + [guid]::NewGuid().ToString("N") + ".json")
    Copy-Item -LiteralPath $uiMap -Destination $uiSnap -Force
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
if ($lockFailed) {
  if ($uiSnap) { Remove-Item -LiteralPath $uiSnap -Force -ErrorAction SilentlyContinue }
  exit 3
}
try { Copy-DefaultUiMap $InstallDir $StateDir $uiSnap }
finally { if ($uiSnap) { Remove-Item -LiteralPath $uiSnap -Force -ErrorAction SilentlyContinue } }
$target = Join-Path $InstallDir "chatx-agent.exe"

$keyFile = Join-Path $StateDir "room.key"
# 0.3.5: a machine_id cached by an older agent (or copied over with a cloned disk image)
# has no hardware fingerprint. Re-derive it from this PC's hardware so two cloned PCs
# stop sharing one node. An enrollment bound to the old id is dropped, and the enroll
# below asks for approval again under the new id (it never rotates another PC's key).
$finishHost = ''
$finishShort = ''
if (-not $KeepIdentity) {
  $idRaw = NativeOut $target @('--state-dir', $StateDir, 'identity', '--reinstall')
  if ($NativeExit -ne 0) {
    Say "identity check failed; keeping the cached machine_id"
  } else {
    try {
      $idInfo = $idRaw | ConvertFrom-Json
      if ($idInfo.changed) { Say ("machine_id renewed (" + [string]$idInfo.reason + "): " + [string]$idInfo.short_id + "; approval is needed again") }
      else { Say ("machine_id " + [string]$idInfo.short_id) }
      if ($idInfo.short_id) { $finishShort = [string]$idInfo.short_id }
      if ($idInfo.host_name) { $finishHost = [string]$idInfo.host_name }
    } catch { Say "identity check output unreadable" }
  }
}
$stRaw = NativeOut $target @('--state-dir', $StateDir, 'status')
$enrollment = 'none'
try {
  $stObj = $stRaw | ConvertFrom-Json
  $enrollment = [string]$stObj.enrollment
  if ($stObj.host_name) { $finishHost = [string]$stObj.host_name }
  if ($stObj.machine_id_short) { $finishShort = [string]$stObj.machine_id_short }
} catch { $enrollment = 'none' }

if ($enrollment -eq 'enrolled') {
  Say "already enrolled"
  if (Test-Path -LiteralPath $keyFile) { Remove-Item -LiteralPath $keyFile -Force -ErrorAction SilentlyContinue }
} else {
  $enrollArgs = @('--state-dir', $StateDir, 'enroll', '--controller', $Controller, '--detect')
  if (Test-Path -LiteralPath $keyFile) { $enrollArgs += @('--room-key-file', $keyFile) }
  $out = NativeOut $target $enrollArgs
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

if ($ManageAdbServer) { Enable-PhoneAdb $target $StateDir }
Stop-StrayAgent $target
$svc = Native $target @('--state-dir', $StateDir, 'install-service')
if ($NativeExit -ne 0) { Say "install-service failed"; exit 1 }
Say "service installed (scheduled task ChatX Fleet Agent)"
try {
  if (-not $finishHost) { $finishHost = [string]$env:COMPUTERNAME }
  Write-InstallFinish $StateDir $finishHost $finishShort (Test-PlatformToolsAdb $InstallDir)
} catch { Say "could not write the finish note" }
exit 0
