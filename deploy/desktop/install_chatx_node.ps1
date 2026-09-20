# install_chatx_node.ps1 -- install/upgrade the ChatX desktop app on ONE machine.
#
# Runs ON THE TARGET machine (copied there by push_chatx.ps1, or run by hand).
# ASCII-only on purpose: PS 5.1 decodes BOM-less UTF-8 as GBK and mangles CJK,
# which is exactly how watchdog_emotion_tts.ps1 got burned. The app itself is
# named with CJK chars, so we NEVER match it by name -- we match by install path
# (telegram-ai-desktop, the npm package name electron-builder uses for the dir).
#
# What it does, in order:
#   1. verify the setup exe (exists + optional sha256 match -- a truncated scp is
#      the single most likely failure here, and NSIS fails confusingly on it)
#   2. stop any running app (an NSIS upgrade over a running app leaves locked files)
#   3. uninstall previous installs found in the registry (BOTH hives: some nodes
#      have an old per-machine install under C:\Program Files while the current
#      build ships per-user -- without this you end up with two copies and two
#      shortcuts, the stale one still auto-starting)
#   4. install silently (/S) under a watchdog: crash => retry; idle stub with the
#      payload fully extracted (NSIS CopyFiles hang, .173 2026-09-17) => finish by
#      hand from the extracted tree (robocopy + registry + shortcuts)
#   5. report where it landed + version, so the caller can verify
#
# Exit codes: 0 ok / 2 bad setup file / 3 install failed / 4 post-install missing
#             5 stale app/backend processes (or port 18799) could not be reaped
[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)][string]$Setup,
  [string]$ExpectSha256 = "",
  [switch]$KeepSetup,
  [int]$SeatPort = 18799
)

$ErrorActionPreference = 'Stop'
function Say($m) { Write-Output ("[chatx] " + $m) }

# --- 1. verify installer ---------------------------------------------------
if (-not (Test-Path $Setup)) { Say "setup not found: $Setup"; exit 2 }
$sz = [math]::Round((Get-Item $Setup).Length / 1MB, 1)
Say "setup: $Setup ($sz MB)"
if ($ExpectSha256) {
  $got = (Get-FileHash $Setup -Algorithm SHA256).Hash
  if ($got -ne $ExpectSha256.ToUpper()) {
    Say "sha256 MISMATCH expected=$ExpectSha256 got=$got"
    exit 2
  }
  Say "sha256 ok"
}

# --- 2. stop running app ---------------------------------------------------
# Match by executable path, not process name (the name is CJK).
$running = Get-Process -ErrorAction SilentlyContinue |
  Where-Object { $_.Path -and $_.Path -like '*telegram-ai-desktop*' }
if ($running) {
  Say ("stopping running app: " + ($running.Id -join ','))
  $running | Stop-Process -Force -ErrorAction SilentlyContinue
  Start-Sleep -Seconds 3
} else {
  Say "app not running"
}
# The bundled backend/sidecars are separate processes; leaving one alive holds a
# file lock on resources/ and makes the install silently partial.
Get-Process -ErrorAction SilentlyContinue |
  Where-Object { $_.Path -and $_.Path -like '*telegram-ai-desktop*' } |
  Stop-Process -Force -ErrorAction SilentlyContinue

# --- 2b. reap-verify + seat-port precheck (WP-5 root fix, 2026-08-17) --------
# The 2026-08-13 1.0.25 incident on .173: the Stop-Process calls above are
# fire-and-forget (-ErrorAction SilentlyContinue swallows access-denied / slow
# exits), so a previous build's backend sidecar can SURVIVE the install cycle
# still holding the seat port. The new shell's backend then fails to bind and
# exits; the node degrades to "exe only (backend not serving)" minutes after a
# green smoke (the smoke runs on its own throwaway port and cannot see this).
# Fix: VERIFY the reap actually worked, kill the port owner directly (taskkill
# /T /F walks a different privilege path than Stop-Process), and REFUSE to
# install while the seat port is still taken. A foreign (non-ChatX) port owner
# is reported but never killed -- that machine needs a human decision.
function Get-SeatPortOwners {
  $owners = @()
  try {
    $owners = @(Get-NetTCPConnection -LocalPort $SeatPort -State Listen -ErrorAction SilentlyContinue |
      Select-Object -ExpandProperty OwningProcess -Unique)
  } catch {}
  if ($owners.Count -eq 0) {
    # netstat fallback for hosts without the NetTCPIP module
    $lines = @(netstat -ano -p tcp 2>$null | Select-String ("[:.]" + $SeatPort + "\s+.*LISTENING"))
    foreach ($ln in $lines) {
      $tok = ($ln.ToString().Trim() -split '\s+')[-1]
      if ($tok -match '^\d+$' -and [int]$tok -gt 0) { $owners += [int]$tok }
    }
  }
  return @($owners | Sort-Object -Unique)
}
$foreignOnPort = @()
for ($round = 1; $round -le 3; $round++) {
  $survivors = @(Get-Process -ErrorAction SilentlyContinue |
    Where-Object { $_.Path -and $_.Path -like '*telegram-ai-desktop*' })
  $portOwners = Get-SeatPortOwners
  $foreignOnPort = @()
  if ($survivors.Count -eq 0 -and $portOwners.Count -eq 0) { break }
  foreach ($sp in $survivors) {
    Say ("  reaping survivor pid=" + $sp.Id + " (" + $sp.Path + ")")
    cmd /c ("taskkill /PID " + $sp.Id + " /T /F >nul 2>&1")
  }
  foreach ($ownerId in $portOwners) {
    $op = Get-Process -Id $ownerId -ErrorAction SilentlyContinue
    $opath = ''
    if ($op -and $op.Path) { $opath = $op.Path }
    if ($opath -like '*telegram-ai-desktop*') {
      Say ("  reaping port $SeatPort owner pid=" + $ownerId + " (" + $opath + ")")
      cmd /c ("taskkill /PID " + $ownerId + " /T /F >nul 2>&1")
    } else {
      $foreignOnPort += ("pid=" + $ownerId + " path=" + $opath)
    }
  }
  Start-Sleep -Seconds 3
}
$survivors = @(Get-Process -ErrorAction SilentlyContinue |
  Where-Object { $_.Path -and $_.Path -like '*telegram-ai-desktop*' })
$portOwners = Get-SeatPortOwners
if ($survivors.Count -gt 0) {
  Say ("FATAL: stale app/backend processes survived 3 reap rounds: pid=" +
    (($survivors | ForEach-Object { $_.Id }) -join ','))
  Say "installing over them would reproduce the 'exe only (backend not serving)' incident -- aborting"
  exit 5
}
if ($portOwners.Count -gt 0) {
  if ($foreignOnPort.Count -gt 0) {
    Say ("FATAL: seat port $SeatPort is held by a FOREIGN process (not killing it): " + ($foreignOnPort -join ' ; '))
  } else {
    Say ("FATAL: seat port $SeatPort still LISTENING after reap: pid=" + ($portOwners -join ','))
  }
  Say "free the port first, then rerun the install"
  exit 5
}
Say ("reap verified: no app processes left, seat port $SeatPort free")

# --- 3. uninstall previous installs (both hives) ---------------------------
$keys = @(
  'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*',
  'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*',
  'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*'
)
$found = @()
foreach ($k in $keys) {
  $found += Get-ItemProperty $k -ErrorAction SilentlyContinue |
    Where-Object {
      ($_.InstallLocation -and $_.InstallLocation -like '*telegram-ai-desktop*') -or
      ($_.UninstallString -and $_.UninstallString -like '*telegram-ai-desktop*')
    }
}
foreach ($app in $found) {
  $loc = $app.InstallLocation
  Say ("uninstalling previous: " + $loc + " (" + $app.DisplayVersion + ")")
  $u = $app.QuietUninstallString
  if (-not $u) { $u = $app.UninstallString + ' /S' }
  # UninstallString looks like: "C:\path\Uninstall app.exe" /S  -> split exe from args
  $m = [regex]::Match($u, '^\s*"([^"]+)"\s*(.*)$')
  if ($m.Success) { $exe = $m.Groups[1].Value; $rest = $m.Groups[2].Value }
  else { $exe = $u; $rest = '' }
  if (-not (Test-Path $exe)) { Say "  uninstaller missing, skip"; continue }
  # _?= keeps NSIS synchronous. Without it NSIS copies itself to %TEMP% and
  # returns IMMEDIATELY -- the installer then races the still-running uninstaller
  # over the same files and dies with 0xC0000005 (measured on .198, 2026-07-31).
  # InstallLocation is empty in some of these registry entries, so derive the dir
  # from the uninstaller path instead of trusting the value.
  if (-not $loc) { $loc = Split-Path $exe -Parent }
  try {
    Start-Process -FilePath $exe -ArgumentList @('/S', ("_?=" + $loc)) -Wait -PassThru | Out-Null
  } catch { Say ("  uninstall raised (ignored): " + $_.Exception.Message) }
  # Belt and braces: even with _?= the elevated child can outlive the parent.
  # Wait for the app binaries to actually go away before touching the disk again.
  for ($i = 0; $i -lt 30; $i++) {
    $left = @(Get-ChildItem $loc -Filter *.exe -ErrorAction SilentlyContinue |
      Where-Object { $_.Name -notlike 'Uninstall*' })
    if ($left.Count -eq 0) { break }
    Start-Sleep -Seconds 1
  }
  Say ("  previous removed: " + (-not (Test-Path (Join-Path $loc 'resources'))))
}
if (-not $found) { Say "no previous install registered" }

# Leftover dirs with no/broken registry entry still cause dual-install smell
# (verify_chatx_vs_local flags both per-user + Program Files). Wipe empties.
foreach ($orphan in @(
  (Join-Path $env:LOCALAPPDATA 'Programs\telegram-ai-desktop'),
  'C:\Program Files\telegram-ai-desktop',
  'C:\Program Files (x86)\telegram-ai-desktop'
)) {
  if (-not (Test-Path $orphan)) { continue }
  $alive = @(Get-ChildItem $orphan -Filter *.exe -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -notlike 'Uninstall*' })
  if ($alive.Count -eq 0) {
    Say ("removing empty leftover dir: " + $orphan)
    Remove-Item $orphan -Recurse -Force -ErrorAction SilentlyContinue
  } elseif (-not ($found | Where-Object { $_.InstallLocation -like ($orphan + '*') })) {
    # Still has binaries but no registry entry we uninstalled -- force wipe
    Say ("force-removing orphan install (no registry): " + $orphan)
    Get-Process -ErrorAction SilentlyContinue |
      Where-Object { $_.Path -and $_.Path -like ($orphan + '*') } |
      Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2
    Remove-Item $orphan -Recurse -Force -ErrorAction SilentlyContinue
  }
}

# --- 4. install ------------------------------------------------------------
# Retry once. MEASURED on .198 / .104 / .173 (2026-07-31): the first silent run
# often dies with 0xC0000005 (-1073741819) seconds in, and the very next attempt
# -- same file, same args -- succeeds. 3/3 hosts, every time.
#
# Event log names the faulting module: System.dll (the NSIS System plugin, whose
# job is generating call thunks at runtime) at offset 0x1581. Ruled out by
# measurement: disk space, Smart App Control (off), Exploit Protection (all
# NOTSET), Defender detections (none for this file), and "left over from the
# uninstall" -- .173 crashed on a run with nothing to uninstall. Do NOT delete
# this retry because an install looked fine once.
#
# 2026-08-09, .173 (Win11 build 26200) measured, in this order:
#   run A: 4/4 silent attempts from the SSH session crashed -> deploy failed
#   probe: same file, same /S, run in the CONSOLE session (task with /IT) -> OK 1st try
#   run B: silent from the SSH session again -> crashed twice, 3rd attempt OK
# So the crash rate is much higher on Win11 than on the Win10 nodes (.198/.104,
# build 19045, where attempt 1 or 2 always wins), but it is still a flake, NOT a
# hard "SSH session cannot install" rule -- run B disproves that. The console
# session is therefore a last-resort extra life, not the explanation. Root cause
# remains inside the third-party stub; do not rewrite these notes as a diagnosis
# without new measurement.
#
# 2026-09-17, .173 (Win11 26200.9168), 1.0.87: a THIRD failure shape, distinct from
# the crash above. The stub does not die - it goes idle forever: CPU flat,
# Responding=True, $PLUGINSDIR\7z-out fully extracted (9118 files / 1.28 GB, CRC
# checked by Nsis7z), yet INSTDIR stays empty for 7+ hours. NSIS `CopyFiles`
# (= SHFileOperation FO_COPY) hangs on this box for ANY large tree (bisected:
# locales/55 files OK, resources\backend/2958 HANG, resources\services/6077 HANG);
# robocopy of the same tree takes 6 s. Defender RTP on/off did not matter. The
# console-session task fallback below cannot help (same copy engine, and /IT tasks
# returned LastResult=0x1 on that build). The old `Start-Process -Wait` waited
# forever and the seat sat with NO app installed. Now: a watchdog polls the stub;
# when the extracted tree is complete and both it and INSTDIR stop changing while
# the stub burns no CPU, finish the install BY HAND from the extracted tree
# (robocopy /MIR + the registry entry the stub would have written + shortcuts).
# The tree is the stub's own CRC-verified output, so the bytes are identical to a
# normal install; only the uninstaller exe is missing (never written by NSIS), and
# uninstallOldVersion tolerates that on the next upgrade.
$cands = @(
  (Join-Path $env:LOCALAPPDATA 'Programs\telegram-ai-desktop'),
  'C:\Program Files\telegram-ai-desktop'
)
$instDir = $cands[0]
# electron-builder per-user uninstall key = UUID v5(appId, electron-builder ns);
# appId com.telegram-mtproto-ai.desktop is frozen (upgrade-chain identity), so the
# GUID is a constant. Pinned by tests/test_install_node_script_invariants.py.
$uninstallGuid = '1c379198-1526-5b8e-9781-a53ed585cb26'
$productName = [string][char]0x667A + [char]0x804A   # CJK product name, built from code points (ASCII-only file)
$setupVersion = ''
if ((Split-Path $Setup -Leaf) -match '(\d+\.\d+\.\d+)') { $setupVersion = $matches[1] }

function Measure-Tree($dir) {
  if (-not $dir -or -not (Test-Path $dir)) { return @{ files = 0; bytes = [long]0 } }
  $f = @(Get-ChildItem $dir -Recurse -File -Force -ErrorAction SilentlyContinue)
  $b = [long]0
  foreach ($x in $f) { $b += $x.Length }
  return @{ files = $f.Count; bytes = $b }
}
function Get-Newest7zOut([datetime]$since) {
  $d = Get-ChildItem $env:TEMP -Directory -Filter 'ns*.tmp' -ErrorAction SilentlyContinue |
    Where-Object { $_.CreationTime -ge $since.AddSeconds(-5) } |
    Sort-Object CreationTime -Descending | Select-Object -First 1
  if (-not $d) { return $null }
  $o = Join-Path $d.FullName '7z-out'
  if (Test-Path $o) { return $o }
  return $null
}
function Get-InstalledApp {
  foreach ($d in $cands) {
    if (-not (Test-Path $d)) { continue }
    $e = Get-ChildItem $d -Filter *.exe -ErrorAction SilentlyContinue |
      Where-Object { $_.Name -notlike 'Uninstall*' } | Select-Object -First 1
    if ($e) { return $e }
  }
  return $null
}

# Finish the install from the stub's extracted tree. Sets $script:bypassOk = $true
# only when the tree proves it is the complete payload of THIS setup exe; any doubt
# => $false (caller kills the stub and retries normally). Never touches user data.
# Result goes through a script variable, NOT the output stream: Say() writes to
# stdout, and assigning the call result would swallow those log lines into it.
function Complete-FromExtracted([string]$outDir, [string]$archive) {
  $script:bypassOk = $false
  $appExe = Get-ChildItem $outDir -Filter *.exe -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -notlike 'Uninstall*' } | Select-Object -First 1
  $biPath = Join-Path $outDir 'resources\build-info.json'
  if (-not $appExe -or -not (Test-Path $biPath)) { Say "  bypass: extracted tree lacks app exe / build-info.json"; $script:bypassOk = $false; return }
  $bi = Get-Content $biPath -Raw -ErrorAction SilentlyContinue
  if ($setupVersion -and ($bi -notmatch ('"version"\s*:\s*"' + [regex]::Escape($setupVersion) + '"'))) {
    Say ("  bypass: build-info version does not match setup " + $setupVersion); $script:bypassOk = $false; return
  }
  $exeVer = $appExe.VersionInfo.ProductVersion
  if ($setupVersion -and $exeVer -and ($exeVer -notlike ($setupVersion + '*'))) {
    Say ("  bypass: app exe version " + $exeVer + " does not match setup " + $setupVersion); $script:bypassOk = $false; return
  }
  $m1 = Measure-Tree $outDir
  Start-Sleep -Seconds 5
  $m2 = Measure-Tree $outDir
  if ($m1.files -ne $m2.files -or $m1.bytes -ne $m2.bytes) { Say "  bypass: extracted tree still changing"; $script:bypassOk = $false; return }
  if ($archive -and (Test-Path $archive)) {
    # 7z payload inflates ~2.8x (490 MB -> 1.37 GB measured); a tree smaller than the
    # archive itself means Nsis7z bailed midway -- do not ship half a tree.
    $arcLen = (Get-Item $archive).Length
    if ($m2.bytes -lt $arcLen) { Say ("  bypass: tree " + $m2.bytes + " B smaller than archive " + $arcLen + " B (partial extraction)"); $script:bypassOk = $false; return }
  }
  Say ("  bypass: extracted tree verified: files=" + $m2.files + " bytes=" + $m2.bytes + " version=" + $exeVer)

  Get-Process -ErrorAction SilentlyContinue |
    Where-Object { $_.Path -and $_.Path -like ($instDir + '\*') } |
    Stop-Process -Force -ErrorAction SilentlyContinue
  if (-not (Test-Path $instDir)) { New-Item -ItemType Directory -Path $instDir -Force | Out-Null }
  # /MIR: INSTDIR must equal the payload (stale files from a skipped uninstall go away).
  $rcp = Start-Process -FilePath 'robocopy.exe' -ArgumentList @(
    ('"' + $outDir + '"'), ('"' + $instDir + '"'), '/MIR', '/COPY:DAT', '/DCOPY:T', '/R:3', '/W:2', '/NFL', '/NDL', '/NJH', '/NJS', '/NP'
  ) -Wait -PassThru -NoNewWindow
  if ($rcp.ExitCode -ge 8) { Say ("  bypass: robocopy reported failures exit=" + $rcp.ExitCode); $script:bypassOk = $false; return }
  $m3 = Measure-Tree $instDir
  if ($m3.files -ne $m2.files -or $m3.bytes -ne $m2.bytes) {
    Say ("  bypass: INSTDIR mismatch after copy files=" + $m3.files + " bytes=" + $m3.bytes); $script:bypassOk = $false; return
  }
  Say ("  bypass: robocopy ok (" + $m3.files + " files)")

  # Registry entry electron-builder's stub writes (registryAddInstallInfo), minus the
  # uninstaller strings (the exe does not exist; a dangling UninstallString makes the
  # next installer ExecWait a missing file and Apps&Features show a dead entry).
  $target = Join-Path $instDir $appExe.Name
  $regKey = 'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\' + $uninstallGuid
  try {
    if (-not (Test-Path $regKey)) { New-Item -Path $regKey -Force | Out-Null }
    Set-ItemProperty -Path $regKey -Name 'DisplayName' -Value ($productName + ' ChatX ' + $setupVersion)
    Set-ItemProperty -Path $regKey -Name 'DisplayVersion' -Value $setupVersion
    Set-ItemProperty -Path $regKey -Name 'InstallLocation' -Value $instDir
    Set-ItemProperty -Path $regKey -Name 'DisplayIcon' -Value ($target + ',0')
    Set-ItemProperty -Path $regKey -Name 'NoModify' -Value 1 -Type DWord
    Set-ItemProperty -Path $regKey -Name 'NoRepair' -Value 1 -Type DWord
    Set-ItemProperty -Path $regKey -Name 'ChatXManualInstall' -Value ('robocopy bypass ' + (Get-Date -Format 's') + ' (NSIS CopyFiles hang)')
    Remove-ItemProperty -Path $regKey -Name 'UninstallString' -ErrorAction SilentlyContinue
    Remove-ItemProperty -Path $regKey -Name 'QuietUninstallString' -ErrorAction SilentlyContinue
    Say "  bypass: registry entry written"
  } catch { Say ("  bypass: registry write failed (non-fatal): " + $_.Exception.Message) }
  try {
    $ws = New-Object -ComObject WScript.Shell
    foreach ($dir in @([Environment]::GetFolderPath('Desktop'), (Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'))) {
      $lnk = Join-Path $dir ($productName + '.lnk')
      $s = $ws.CreateShortcut($lnk); $s.TargetPath = $target; $s.WorkingDirectory = $instDir; $s.IconLocation = $target; $s.Save()
    }
    Say "  bypass: shortcuts written"
  } catch { Say ("  bypass: shortcut write failed (non-fatal): " + $_.Exception.Message) }
  $script:bypassOk = $true
}

if ($found) { Start-Sleep -Seconds 8 }
$code = $null
$bypassed = $false
$HangIdleSec = 120     # tree + INSTDIR + CPU all flat this long => CopyFiles hang
$HardCapSec = 1200     # nothing at all for 20 min (hidden dialog etc.) => kill + retry
for ($attempt = 1; $attempt -le 4; $attempt++) {
  Say ("installing (silent), attempt $attempt ...")
  $tStart = Get-Date
  $p = Start-Process -FilePath $Setup -ArgumentList '/S' -PassThru
  $lastSig = ''
  $flatSince = Get-Date
  $lastCpu = 0.0
  $outDir = $null
  while (-not $p.HasExited) {
    Start-Sleep -Seconds 5
    $p.Refresh()
    if ($p.HasExited) { break }
    if (-not $outDir) { $outDir = Get-Newest7zOut $tStart }
    $mo = Measure-Tree $outDir
    $mi = Measure-Tree $instDir
    $cpu = 0.0
    try { $cpu = [double]$p.TotalProcessorTime.TotalSeconds } catch {}
    $sig = ('{0}/{1}|{2}/{3}' -f $mo.files, $mo.bytes, $mi.files, $mi.bytes)
    if ($sig -ne $lastSig -or ($cpu - $lastCpu) -gt 0.5) { $lastSig = $sig; $lastCpu = $cpu; $flatSince = Get-Date }
    $flat = ((Get-Date) - $flatSince).TotalSeconds
    if ($flat -ge $HangIdleSec -and $mo.bytes -gt 0) {
      Say ("  installer idle " + [int]$flat + "s with payload extracted (" + $mo.files + " files) and INSTDIR frozen -- NSIS CopyFiles hang; finishing from the extracted tree")
      $archive = Join-Path (Split-Path $outDir -Parent) 'app-64.7z'
      Complete-FromExtracted $outDir $archive
      Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue
      Start-Sleep -Seconds 2
      if ($script:bypassOk) { $bypassed = $true; $code = 0 } else { $code = -3 }
      break
    }
    if (((Get-Date) - $tStart).TotalSeconds -ge $HardCapSec) {
      Say ("  installer made no progress for " + $HardCapSec + "s (no extraction) -- killing this attempt")
      Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue
      Start-Sleep -Seconds 2
      $code = -2
      break
    }
  }
  if ($p.HasExited -and $null -eq $code) { $code = $p.ExitCode }
  # Killed/bypassed stubs never clean their $PLUGINSDIR (~1.3 GB each; .173 had 114 of them = 20 GB).
  if ($code -ne 0 -or $bypassed) {
    Get-ChildItem $env:TEMP -Directory -Filter 'ns*.tmp' -ErrorAction SilentlyContinue |
      Where-Object { $_.CreationTime -ge $tStart.AddSeconds(-5) } |
      Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
  }
  if ($bypassed) { Say "installer exit=(bypassed: robocopy from extracted tree)"; break }
  Say ("installer exit=" + $code)
  if ($code -eq 0) { break }
  if ($attempt -lt 4) {
    Say "  attempt crashed/hung (known NSIS flakes) -- retrying in 10s"
    Start-Sleep -Seconds 10
    $code = $null
  }
}

# --- 4b. console-session fallback (see note above) -------------------------
# Last resort after the silent retries are spent: a ONCE task with /IT runs in the
# console session of the same user. Measured to install on the first try on .173
# right after 4 silent attempts had failed. No exit code comes back through
# schtasks, so poll for the app binary (NSIS unpacks ~1.5 GB) rather than trusting
# a single sleep. Verified as a standalone probe; this in-script branch has not yet
# been hit in a real deploy (run B recovered at attempt 3 before reaching it).
if ($code -ne 0) {
  $sess = (query session 2>&1) -join "`n"
  if ($sess -notmatch 'console') {
    Say "silent install failed and no console session exists -- giving up"
    exit 3
  }
  Say "silent install failed 4x -- retrying in the console session (see note)"
  # schtasks via cmd /c: under ErrorActionPreference=Stop, a native command that
  # writes to a REDIRECTED stderr raises NativeCommandError and kills the script
  # (measured on .173 2026-08-13: the pre-delete of a not-yet-existing task did
  # exactly that, so this fallback died before ever creating the task).
  cmd /c 'schtasks /Delete /TN ChatXInstallFallback /F >nul 2>&1'
  cmd /c ('schtasks /Create /TN ChatXInstallFallback /SC ONCE /ST 23:59 /F /IT /RL LIMITED /TR "\"' + $Setup + '\" /S" >nul 2>&1')
  if ($LASTEXITCODE -ne 0) { Say "console-session task create FAILED"; exit 3 }
  cmd /c 'schtasks /Run /TN ChatXInstallFallback >nul 2>&1'
  if ($LASTEXITCODE -ne 0) { Say "console-session task run FAILED"; exit 3 }
  $deadline = (Get-Date).AddMinutes(4)
  while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 10
    if (Get-InstalledApp) { $code = 0; break }
    $still = Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq $Setup }
    if (-not $still) {
      # stub exited; give the last file moves a moment before calling it a loss
      Start-Sleep -Seconds 5
      if (Get-InstalledApp) { $code = 0 }
      break
    }
  }
  cmd /c 'schtasks /Delete /TN ChatXInstallFallback /F >nul 2>&1'
  if ($code -eq 0) { Say "console-session install OK" }
}
if ($code -ne 0) { exit 3 }

# --- 5. verify -------------------------------------------------------------
$dir = $cands | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $dir) { Say "post-install: install dir NOT found"; exit 4 }
$app = Get-ChildItem $dir -Filter *.exe |
  Where-Object { $_.Name -notlike 'Uninstall*' } | Select-Object -First 1
if (-not $app) { Say "post-install: app exe NOT found in $dir"; exit 4 }
Say ("installed at: " + $dir)
Say ("version: " + $app.VersionInfo.ProductVersion + " (file " + $app.VersionInfo.FileVersion + ")")
$res = Join-Path $dir 'resources\backend\backend.exe'
Say ("backend bundled: " + (Test-Path $res))
if ($bypassed) { Say "install path: robocopy bypass (NSIS CopyFiles hang) -- no uninstaller exe on this node, see deploy notes" }
if (-not $KeepSetup) { Remove-Item $Setup -Force -ErrorAction SilentlyContinue }
Say "OK"
exit 0
