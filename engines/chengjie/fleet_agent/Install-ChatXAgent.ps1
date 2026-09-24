# Install-ChatXAgent.ps1 -- one-shot installer for the ChatX fleet node Agent (Windows).
#
# What it does (idempotent, re-run to upgrade):
#   1. get chatx-agent.exe   (-Exe local file, or download -DownloadUrl; sha256 checked when -Sha256 given
#                             or when manifest.json sits next to the exe URL)
#   2. copy to %ProgramFiles%\ChatX Agent\  (stops the running task first)
#   3. detect the local ChatX instance and AvatarHub (127.0.0.1:9000). If nothing is
#      listening, still enroll as a heartbeat-only node.
#   4. enroll: -Code (one-time code), -RoomKeyFile (room pack), or neither (pending approval)
#   5. install-service       (scheduled task "ChatX Fleet Agent", ONSTART, SYSTEM) and start it
#
# Usage (run as Administrator):
#   powershell -ExecutionPolicy Bypass -File Install-ChatXAgent.ps1 -Controller https://bd2026.cc/fleet
#   ... -Code 12345678                             # one-time code, skips the pending queue
#   ... -RoomKeyFile .\room.key                    # room pack; the key is read from the file only
#   ... -Exe .\chatx-agent.exe                     # offline: use a local exe instead of downloading
#   ... -NoEnroll                                  # skip a new enroll. An unlocked agent.json keeps identity fields only; instances are cleared and restart_cmd must be set again by an admin
#   ... -ConfigPath C:\path\config.local.yaml      # pin one ChatX instance instead of auto-detect
#
# ASCII only (PowerShell 5.1 + GBK console lesson). Never prints node_key / auth tokens.
[CmdletBinding()]
param(
  [string]$Controller = "https://bd2026.cc/fleet",
  [string]$Code = "",
  [string]$RoomKeyFile = "",
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
# 原生程序写 stderr 在 EAP=Stop + 重定向下会变成终止错误（PS 5.1），统一在 Continue 下跑并合并输出
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
  $parent = Split-Path -Parent $Dir
  if (Test-Reparse $parent) { Fail "parent is a reparse point" }
  if (Test-Reparse $Dir) { Fail "state dir is a reparse point" }
  if (-not (Test-Path -LiteralPath $parent)) {
    New-Item -ItemType Directory -Force -Path $parent | Out-Null
  }
  if (Test-Reparse $parent) { Fail "parent is a reparse point" }
  if (Test-Reparse $Dir) { Fail "state dir is a reparse point" }
  $parentLocked = $false
  try { $parentLocked = Test-ParentLocked $parent } catch { $parentLocked = $false }
  if (-not $parentLocked) {
    Native icacls.exe @($parent, '/setowner', '*S-1-5-32-544') | Out-Null
    if ($NativeExit -ne 0) { Fail "parent setowner failed ($NativeExit)" }
    Native icacls.exe @($parent, '/reset') | Out-Null
    if ($NativeExit -ne 0) { Fail "parent reset failed ($NativeExit)" }
    Native icacls.exe @($parent, '/inheritance:r', '/grant:r', '*S-1-5-18:(OI)(CI)F', '*S-1-5-32-544:(OI)(CI)F', '*S-1-5-32-545:(OI)(CI)RX') | Out-Null
    if ($NativeExit -ne 0) { Fail "parent grant failed ($NativeExit)" }
  }
  try { if (-not (Test-ParentLocked $parent)) { Fail "parent owner is not trusted" } } catch { Fail "could not read parent ACL" }
  if (Test-Reparse $parent) { Fail "parent is a reparse point" }
  if (Test-Reparse $Dir) { Fail "state dir is a reparse point" }
}
function Assert-NoChildReparse([string]$Dir) {
  $pending = New-Object System.Collections.Generic.Queue[string]
  $pending.Enqueue($Dir)
  while ($pending.Count -gt 0) {
    $cur = $pending.Dequeue()
    foreach ($c in @(Get-ChildItem -Force -LiteralPath $cur -ErrorAction SilentlyContinue)) {
      if ($c.Attributes -band [IO.FileAttributes]::ReparsePoint) { Fail "child reparse point" }
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
  if (Test-Reparse $Dir) { Fail "state dir is a reparse point" }
  $wasLocked = $false
  if ($existed) {
    try { $wasLocked = Test-DirLocked $Dir } catch { $wasLocked = $false }
  }
  if (-not $wasLocked) {
    Native icacls.exe @($Dir, '/setowner', '*S-1-5-32-544') | Out-Null
    if ($NativeExit -ne 0) { Fail "icacls setowner failed ($NativeExit)" }
    Native icacls.exe @($Dir, '/reset') | Out-Null
    if ($NativeExit -ne 0) { Fail "icacls reset failed ($NativeExit)" }
    Native icacls.exe @($Dir, '/inheritance:r', '/grant:r', '*S-1-5-18:(OI)(CI)F', '*S-1-5-32-544:(OI)(CI)F') | Out-Null
    if ($NativeExit -ne 0) { Fail "icacls grant failed ($NativeExit)" }
  }
  try { if (-not (Test-DirLocked $Dir)) { Fail "state dir ACL is not locked" } } catch { Fail "could not read state dir ACL" }
  Assert-NoChildReparse $Dir
  try { Remove-Sensitive $Dir $false } catch { Fail "could not delete untrusted state" }
  if (-not $wasLocked) {
    Native icacls.exe @($Dir, '/setowner', '*S-1-5-32-544', '/T', '/C') | Out-Null
    if ($NativeExit -ne 0) { Fail "icacls setowner failed ($NativeExit)" }
    $children = @(Get-ChildItem -Force -LiteralPath $Dir -ErrorAction SilentlyContinue)
    if ($children.Count -gt 0) {
      Native icacls.exe @((Join-Path $Dir '*'), '/reset', '/T', '/C') | Out-Null
      if ($NativeExit -ne 0) { Fail "icacls reset failed ($NativeExit)" }
    }
    Native icacls.exe @($Dir, '/inheritance:r', '/grant:r', '*S-1-5-18:(OI)(CI)F', '*S-1-5-32-544:(OI)(CI)F', '/T', '/C') | Out-Null
    if ($NativeExit -ne 0) { Fail "icacls grant failed ($NativeExit)" }
    try { if (-not (Test-DirLocked $Dir)) { Fail "state dir ACL is not locked" } } catch { Fail "could not read state dir ACL" }
  }
}

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) { Fail "run this script as Administrator (scheduled task needs SYSTEM)" }

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
$help = Native $tmp @('--help')
if ($help -notmatch 'enroll') { Fail "downloaded file does not look like chatx-agent" }

# 2. place exe (stop running task first: exe may be locked)
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
$target = Join-Path $InstallDir "chatx-agent.exe"
Native 'schtasks.exe' @('/End', '/TN', 'ChatX Fleet Agent') | Out-Null
Get-Process -Name chatx-agent -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq $target } | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Milliseconds 800
if (Test-Path -LiteralPath $target) { Copy-Item -LiteralPath $target -Destination "$target.bak" -Force }
Move-Item -LiteralPath $tmp -Destination $target -Force
Say "installed $target"

# 3. local instance. Snapshot before the lock drops an unlocked agent.json.
$stateDir = Join-Path $env:ProgramData "ChatX\fleet"
$snap = ""
$preLocked = $false
if (Test-Path -LiteralPath $stateDir) {
  try { $preLocked = Test-DirLocked $stateDir } catch { $preLocked = $false }
}
$agentJson = Join-Path $stateDir "agent.json"
if ((-not $preLocked) -and (Test-Path -LiteralPath $agentJson)) {
  $snap = Join-Path $env:TEMP ("chatx-agent-migrate-" + [guid]::NewGuid().ToString("N") + ".json")
  Copy-Item -LiteralPath $agentJson -Destination $snap -Force
}
Lock-StateDir $stateDir
if ($snap) {
  Native $target @('--state-dir', $stateDir, 'migrate-legacy', '--controller', $Controller, '--snapshot', $snap) | Out-Null
  Remove-Item -LiteralPath $snap -Force -ErrorAction SilentlyContinue
  if ($NativeExit -ne 0) { Fail "migrate failed" }
  if (-not $NoInstance) {
    Native $target @('--state-dir', $stateDir, 'detect') | Out-Null
    if ($NativeExit -ne 0) { Fail "detect failed" }
  }
}
$detect = $false
if (-not $NoInstance) {
  if ($ConfigPath -or $AuthToken) {
    $ia = @("--state-dir", $stateDir, "add-instance", "$InstanceName=$InstanceUrl")
    if ($AuthToken) { $ia += @("--auth-token", $AuthToken) }
    if ($ConfigPath) { $ia += @("--config-path", $ConfigPath) }
    Native $target $ia | Out-Null
    if ($NativeExit -ne 0) { Fail "add-instance failed" }
    Say "instance $InstanceName -> $InstanceUrl"
  } else {
    $detect = $true
    Say "will detect local ChatX and AvatarHub during enroll (none is ok)"
  }
}

# 4. enroll. No -Code and no room key -> the PC shows up as pending until an admin approves it.
#    Re-running on a machine that is already enrolled or already waiting does not mint another request.
if (-not $NoEnroll) {
  $skipEnroll = $false
  if (-not $Code -and -not $RoomKeyFile) {
    $stRaw = Native $target @('--state-dir', $stateDir, 'status')
    try {
      $enr = [string](($stRaw | ConvertFrom-Json).enrollment)
      if ($enr -eq 'enrolled' -or $enr -eq 'pending') { $skipEnroll = $true; Say "enrollment already $enr" }
    } catch { }
  }
  if ($skipEnroll) { $enrollArgs = @() } else {
  $enrollArgs = @('--state-dir', $stateDir, 'enroll', '--controller', $Controller)
  if ($detect) { $enrollArgs += '--detect' }
  if ($Code) { $enrollArgs += @('--code', $Code) }
  if ($RoomKeyFile) { $enrollArgs += @('--room-key-file', $RoomKeyFile) }
  $out = Native $target $enrollArgs
  if ($NativeExit -ne 0) { Say "enroll failed; installing the service so it can retry" }
  else {
  try {
    $j = $out | ConvertFrom-Json
    if ($j.status -eq 'pending') { Say "installed; waiting for admin approval (console: pending)" }
    else { Say "enrolled node_id=$($j.node_id) machine=$($j.machine_id)" }
  } catch { Say "enroll finished" }
  }
  }
}

# 5. service
$svc = Native $target @('--state-dir', $stateDir, 'install-service')
if ($NativeExit -ne 0) { Fail "install-service failed: $svc" }
Say "service installed and started (scheduled task 'ChatX Fleet Agent')"
Start-Sleep -Seconds 2
Write-Host (Native $target @('--state-dir', $stateDir, 'service-status'))
Say "done. logs: $stateDir\logs\agent.log ; status: `"$target`" status"
