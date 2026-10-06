# Install-ChatXAgent.ps1 -- one-shot installer for the 智拓群控 node Agent (Windows).
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
#   ... -NoEnroll                                  # skip a new enroll. An unlocked agent.json keeps identity fields and each instance's name/url/auth_token only; config_path is re-detected and restart_cmd must be set again by an admin
#   ... -ConfigPath C:\path\config.local.yaml      # pin one ChatX instance instead of auto-detect
#   ... -ManageAdbServer                           # phone-room: opt in to the bundled adb server (refused on a live-stream host)
#   ... -PlatformToolsDir C:\src\platform-tools    # adb.exe + AdbWinApi.dll + AdbWinUsbApi.dll; also picked up from beside this script
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
  [switch]$NoEnroll,
  [switch]$ManageAdbServer,
  [string]$PlatformToolsDir = ""
)

$ErrorActionPreference = 'Stop'
# Pin PSModulePath to this Windows PowerShell 5.1's own module dirs. Started from pwsh 7
# (directly, or via a setup launched from pwsh 7) the process inherits a 7.x PSModulePath
# and 5.1 cannot autoload Get-Acl / Get-FileHash / ScheduledTasks.
if ($PSVersionTable.PSVersion.Major -le 5) {
  $env:PSModulePath = (Join-Path $PSHOME 'Modules') + ';' + (Join-Path $env:ProgramFiles 'WindowsPowerShell\Modules')
}
function Say($m) { Write-Host "[chatx-agent] $m" }
function Fail($m) { Write-Host "[chatx-agent] ERROR: $m" -ForegroundColor Red; exit 1 }
# 原生程序写 stderr 在 EAP=Stop + 重定向下会变成终止错误（PS 5.1），统一在 Continue 下跑并合并输出
function Native([string]$exe, [string[]]$a) {
  $old = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
  try { $o = & $exe @a 2>&1 | ForEach-Object { "$_" }; $script:NativeExit = $LASTEXITCODE; return ($o -join "`n") }
  finally { $ErrorActionPreference = $old }
}
# stdout only: agent log lines go to stderr and would break ConvertFrom-Json (issue g4).
function NativeOut([string]$exe, [string[]]$a) {
  $old = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
  try { $o = & $exe @a 2>$null | ForEach-Object { "$_" }; $script:NativeExit = $LASTEXITCODE; return ($o -join "`n") }
  finally { $ErrorActionPreference = $old }
}
function Test-Reparse([string]$Path) {
  if (-not (Test-Path -LiteralPath $Path)) { return $false }
  try { $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop }
  catch { return $true }
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
function Find-PlatformToolsSource([string]$ExePath) {
  $dirs = @()
  if ($PlatformToolsDir) { $dirs += $PlatformToolsDir }
  if ($ExePath) {
    $parent = Split-Path -Parent $ExePath
    if ($parent) { $dirs += (Join-Path $parent 'platform-tools') }
  }
  $dirs += (Join-Path $PSScriptRoot 'platform-tools')
  foreach ($d in $dirs) {
    if ($d -and (Test-Path -LiteralPath (Join-Path $d 'adb.exe'))) { return $d }
  }
  return ""
}
function Install-BundledPlatformTools([string]$DestRoot, [string]$ExePath) {
  $src = Find-PlatformToolsSource $ExePath
  $destExe = Join-Path $DestRoot 'platform-tools\adb.exe'
  if ($src) {
    $srcExe = Join-Path $src 'adb.exe'
    if ([IO.Path]::GetFullPath($srcExe) -eq [IO.Path]::GetFullPath($destExe)) {
      Say "platform-tools already in place: $destExe"
      return
    }
  }
  if (-not $src) {
    if (Test-Path -LiteralPath $destExe) { Say "platform-tools already present: $destExe" }
    else { Say "no platform-tools staged; phone-room nodes need adb.exe (see fleet_agent\platform-tools\README.txt)" }
    return
  }
  $to = Join-Path $DestRoot 'platform-tools'
  New-Item -ItemType Directory -Force -Path $to | Out-Null
  Copy-Item -Path (Join-Path $src '*') -Destination $to -Force
  Say "platform-tools -> $to"
}
function Enable-PhoneAdb([string]$Exe, [string]$Dir) {
  if (Test-LiveStreamHost $Dir) {
    Say "live-stream host: bundled adb server management stays off"
    return
  }
  if (-not (Test-Path -LiteralPath (Join-Path $Dir 'agent.json'))) {
    Say "agent.json missing; bundled adb server management not written"
    return
  }
  NativeOut $Exe @('--state-dir', $Dir, 'enable-phone-adb') | Out-Null
  if ($NativeExit -ne 0) { Fail "could not enable bundled adb server management" }
  Say "bundled adb server management enabled"
}
function Lock-StateDir([string]$Dir) {
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
function Copy-DefaultUiMap([string]$ToDir, [string]$Kept) {
  # Create the state-dir map only when it is absent. A kept snapshot wins over the default.
  $dest = Join-Path $ToDir 'phone_ui_map.json'
  if (Test-Path -LiteralPath $dest) { return }
  if ($Kept -and (Test-Path -LiteralPath $Kept)) {
    Copy-Item -LiteralPath $Kept -Destination $dest -Force
    return
  }
  $candidates = @()
  if ($Exe) { $candidates += (Join-Path (Split-Path -Parent $Exe) 'phone_ui_map.json') }
  $candidates += (Join-Path $PSScriptRoot 'phone_ui_map.json')
  $candidates += (Join-Path $InstallDir 'phone_ui_map.json')
  foreach ($src in $candidates) {
    if ($src -and (Test-Path -LiteralPath $src)) {
      Copy-Item -LiteralPath $src -Destination $dest -Force
      Say "placed default phone_ui_map.json"
      return
    }
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
Install-BundledPlatformTools $InstallDir $Exe

# 3. local instance. Snapshot before an unlocked fleet is renamed aside.
$stateDir = Join-Path $env:ProgramData "ChatX\fleet"
$snap = ""
$uiSnap = ""
if ((Test-Path -LiteralPath $stateDir) -and -not (Test-Reparse $stateDir)) {
  $preLocked = $false
  try { $preLocked = Test-DirLocked $stateDir } catch { $preLocked = $false }
  $agentJson = Join-Path $stateDir "agent.json"
  if ((-not $preLocked) -and (Test-Path -LiteralPath $agentJson) -and -not (Test-Reparse $agentJson)) {
    $snap = Join-Path $env:TEMP ("chatx-agent-migrate-" + [guid]::NewGuid().ToString("N") + ".json")
    Copy-Item -LiteralPath $agentJson -Destination $snap -Force
  }
  $uiMap = Join-Path $stateDir "phone_ui_map.json"
  if ((-not $preLocked) -and (Test-Path -LiteralPath $uiMap) -and -not (Test-Reparse $uiMap)) {
    $uiSnap = Join-Path $env:TEMP ("chatx-phone-ui-map-" + [guid]::NewGuid().ToString("N") + ".json")
    Copy-Item -LiteralPath $uiMap -Destination $uiSnap -Force
  }
}
$lockFailed = $false
$lockError = ""
try {
  Lock-StateDir $stateDir
  if ($snap) {
    Native $target @('--state-dir', $stateDir, 'migrate-legacy', '--controller', $Controller, '--snapshot', $snap) | Out-Null
    if ($NativeExit -ne 0) { throw "migrate failed" }
    if (-not $NoInstance) {
      Native $target @('--state-dir', $stateDir, 'detect') | Out-Null
      if ($NativeExit -ne 0) { throw "detect failed" }
    }
  }
} catch {
  $lockFailed = $true
  $lockError = "$_"
} finally {
  if ($snap) { Remove-Item -LiteralPath $snap -Force -ErrorAction SilentlyContinue }
}
if ($lockFailed) {
  if ($uiSnap) { Remove-Item -LiteralPath $uiSnap -Force -ErrorAction SilentlyContinue }
  Fail $lockError
}
try { Copy-DefaultUiMap $stateDir $uiSnap }
finally { if ($uiSnap) { Remove-Item -LiteralPath $uiSnap -Force -ErrorAction SilentlyContinue } }
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
    $stRaw = NativeOut $target @('--state-dir', $stateDir, 'status')
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
  $out = NativeOut $target $enrollArgs
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

# 5. phone-room opt-in, then the service (the flag is on disk before the service starts)
if ($ManageAdbServer) { Enable-PhoneAdb $target $stateDir }

# 6. service
$svc = Native $target @('--state-dir', $stateDir, 'install-service')
if ($NativeExit -ne 0) { Fail "install-service failed: $svc" }
Say "service installed and started (scheduled task 'ChatX Fleet Agent')"
Start-Sleep -Seconds 2
Write-Host (Native $target @('--state-dir', $stateDir, 'service-status'))
Say "done. logs: $stateDir\logs\agent.log ; status: `"$target`" status"
