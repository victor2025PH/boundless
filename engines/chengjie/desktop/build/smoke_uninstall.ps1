# smoke_uninstall.ps1 -- live silent-channel matrix for the uninstaller
# data-disposition contract (build/installer.nsh C1/C2/C3-boundary/C5).
#
# WHY A DEDICATED TEST USER: NSIS resolves $APPDATA/$LOCALAPPDATA/HKCU from the
# *running* user and cannot be redirected. Running install/uninstall on the
# build machine's own account would uninstall the operator's live seat app
# (the 2026-08-13 restore-script incident, same family). So we create a plain
# (non-admin) local user and run every install/uninstall inside that profile:
#   * its %APPDATA% is isolated -- wiping it touches nothing real;
#   * a non-admin user cannot Stop-Process another session's processes, so the
#     uninstaller's process-reaping can never kill the operator's seat app.
#
# SCENARIOS (silent channels only -- the interactive page needs a human eye):
#   S1 install -> seed data -> REINSTALL (isUpdated chain)   => data SURVIVES (C1)
#   S2 uninstall /S                                          => data SURVIVES (C2)
#   S3 reinstall -> uninstall /S --delete-app-data           => data GONE,
#      updater cache GONE, sibling *_bak20* dir SURVIVES (C3 boundary), and
#      install dir gone (C5 sanity).
#
# ASCII-only on purpose (PS5.1-GBK lesson); the CJK product dir name is built
# from char codes at runtime. Exit 0 = all green, 1 = assertion failed,
# 2 = environment/setup failure. NOT wired into predist (needs admin + ~8 min);
# run explicitly after touching installer.nsh:
#   powershell -ExecutionPolicy Bypass -File build\smoke_uninstall.ps1 [-Setup path]
[CmdletBinding()]
param(
  [string]$Setup = "",
  [switch]$KeepUser   # leave the test user behind for manual poking
)

$ErrorActionPreference = 'Stop'
function Say($m) { Write-Output ("[smoke-uninst] " + $m) }
$script:fails = 0
function Assert($cond, $msg) {
  if ($cond) { Say ("PASS: " + $msg) }
  else { Say ("FAIL: " + $msg); $script:fails++ }
}

# ---- locate the installer ---------------------------------------------------
if (-not $Setup) {
  $dist = Join-Path $PSScriptRoot '..\dist'
  $cand = Get-ChildItem $dist -Filter 'ChatX-Setup-*.exe' -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1
  if (-not $cand) { Say "no ChatX-Setup-*.exe under desktop\dist -- build first or pass -Setup"; exit 2 }
  $Setup = $cand.FullName
}
Say ("setup: " + $Setup)

# ---- test user lifecycle ----------------------------------------------------
$user = 'cxuninst'
# <=14 chars ON PURPOSE: net.exe prompts "password longer than 14 characters,
# continue? (Y/N)" interactively and a non-interactive run dies on it
$pass = 'Cx!' + [guid]::NewGuid().ToString('N').Substring(0, 11)
$cred = New-Object pscredential ($user, (ConvertTo-SecureString $pass -AsPlainText -Force))

# NOTE: use the LocalAccounts cmdlets, NOT net.exe -- on this fleet net.exe
# stalls ~30s and exits -1 without a word (domain/SAM lookup quirk), while
# New-/Remove-LocalUser complete in under a second.
function Remove-TestUser {
  $prof = Get-CimInstance Win32_UserProfile -ErrorAction SilentlyContinue |
    Where-Object { $_.LocalPath -like ("*\" + $user) }
  if ($prof) { $prof | Remove-CimInstance -ErrorAction SilentlyContinue }
  try { Remove-LocalUser -Name $user -ErrorAction Stop } catch { }
  # orphaned profile dir would make the NEXT run's profile land at
  # cxuninst.<MACHINE> and break every path assertion -- always reclaim it
  $orphan = Join-Path (Split-Path $env:USERPROFILE -Parent) $user
  if (Test-Path $orphan) {
    Remove-Item $orphan -Recurse -Force -ErrorAction SilentlyContinue
    if (Test-Path $orphan) { Start-Sleep -Seconds 2; Remove-Item $orphan -Recurse -Force -ErrorAction SilentlyContinue }
  }
}

Remove-TestUser
try {
  New-LocalUser -Name $user -Password (ConvertTo-SecureString $pass -AsPlainText -Force) `
    -Description 'ChatX uninstall smoke (auto-managed)' -ErrorAction Stop | Out-Null
  # unlike `net user /add`, New-LocalUser grants NO group membership -- without
  # Users (well-known SID S-1-5-32-545) the account cannot log on at all and
  # Start-Process -Credential fails outright
  Add-LocalGroupMember -Group (Get-LocalGroup -SID 'S-1-5-32-545') -Member $user -ErrorAction Stop
} catch {
  Say ("cannot create test user (need admin): " + $_.Exception.Message)
  exit 2
}
Say ("test user created: " + $user)

# the test user may lack read ACLs on the dev tree (D:\...); stage the setup
# under Public which every local user can read
$stage = Join-Path $env:PUBLIC 'cx_smoke_setup.exe'
Copy-Item $Setup $stage -Force
$Setup = $stage
Say ("staged setup: " + $Setup)

# run a program inside the test user's session and wait for it to finish.
# Start-Process -Credential uses CreateProcessWithLogonW (loads the profile);
# if group policy blocks it we fall back to a schtasks one-shot (the same
# console-session trick the seat install script uses as its crash fallback).
function Invoke-AsTestUser([string]$exe, [string[]]$argList) {
  try {
    $p = Start-Process -FilePath $exe -ArgumentList $argList -Credential $cred `
      -LoadUserProfile -PassThru -Wait -WindowStyle Hidden
    return $p.ExitCode
  } catch {
    Say ("Start-Process -Credential failed (" + $_.Exception.Message.Trim() + "); schtasks fallback")
    $tn = 'CxUninstSmoke'
    $tr = '"' + $exe + '" ' + ($argList -join ' ')
    schtasks /Create /TN $tn /TR $tr /SC ONCE /ST 23:59 /RU $user /RP $pass /F | Out-Null
    schtasks /Run /TN $tn | Out-Null
    for ($i = 0; $i -lt 180; $i++) {
      Start-Sleep -Seconds 2
      $st = (schtasks /Query /TN $tn /FO LIST /V | Select-String 'Status:|状态:') -join ' '
      if ($st -notmatch 'Running|正在运行') { break }
    }
    schtasks /Delete /TN $tn /F | Out-Null
    return 0  # schtasks path cannot surface the exit code; assertions below decide
  }
}

# ---- paths inside the test profile ------------------------------------------
$tProfile = Join-Path (Split-Path $env:USERPROFILE -Parent) $user
$prodName = [string]::new([char[]](0x667A, 0x804A))   # CJK product dir name, ASCII source
$tRoaming = Join-Path $tProfile 'AppData\Roaming'
$tData    = Join-Path $tRoaming $prodName
$tBak     = Join-Path $tRoaming ($prodName + '_bak20260101_0000')
$tUpd     = Join-Path $tProfile 'AppData\Local\telegram-ai-desktop-updater'
$tApp     = Join-Path $tProfile 'AppData\Local\Programs\telegram-ai-desktop'

function Install-App {
  # NSIS under non-interactive sessions is a known flake (0xC0000005 on .173
  # AND once on .198) -- retry twice before declaring failure.
  for ($try = 1; $try -le 2; $try++) {
    Invoke-AsTestUser $Setup @('/S', '/currentuser') | Out-Null
    $exe = Get-ChildItem $tApp -Filter '*.exe' -ErrorAction SilentlyContinue |
      Where-Object { $_.Name -notlike 'Uninstall*' } | Select-Object -First 1
    if ($exe) { return $true }
    Say ("install attempt " + $try + " left no app exe; retrying")
  }
  return $false
}

function Uninstall-App([string[]]$extraArgs) {
  $un = Get-ChildItem $tApp -Filter 'Uninstall*.exe' -ErrorAction SilentlyContinue | Select-Object -First 1
  if (-not $un) { Say "no uninstaller found"; return $false }
  # _?= keeps NSIS synchronous (the .198 race lesson from uninstall_chatx_node)
  $argList = @('/S') + $extraArgs + @('_?=' + $tApp)
  Invoke-AsTestUser $un.FullName $argList | Out-Null
  # even synchronous, give file handles a beat to settle
  Start-Sleep -Seconds 2
  return $true
}

function Seed-Data {
  New-Item -ItemType Directory -Path (Join-Path $tData 'data\config') -Force | Out-Null
  New-Item -ItemType Directory -Path (Join-Path $tData 'Partitions\persist_acc1') -Force | Out-Null
  Set-Content -Path (Join-Path $tData 'data\config\config.yaml') -Value 'smoke: 1' -Encoding ASCII
  Set-Content -Path (Join-Path $tData 'data\config\account_registry.db') -Value 'db' -Encoding ASCII
  Set-Content -Path (Join-Path $tData 'config.json') -Value '{}' -Encoding ASCII
  New-Item -ItemType Directory -Path $tBak -Force | Out-Null
  Set-Content -Path (Join-Path $tBak 'marker.txt') -Value 'recovery-copy' -Encoding ASCII
  New-Item -ItemType Directory -Path $tUpd -Force | Out-Null
  Set-Content -Path (Join-Path $tUpd 'pending.exe') -Value 'x' -Encoding ASCII
}

$overall = 2
try {
  # ---- S0: fresh install -----------------------------------------------------
  if (-not (Install-App)) { Say "install failed twice -- aborting"; exit 2 }
  Say "installed into test profile"
  Seed-Data
  Say "seeded fake user data (+ _bak recovery dir + updater cache)"

  # ---- S1: reinstall == upgrade chain (isUpdated) => data survives (C1) ------
  if (-not (Install-App)) { Say "reinstall failed -- aborting"; exit 2 }
  Assert (Test-Path (Join-Path $tData 'data\config\config.yaml')) "S1 upgrade chain keeps user data (isUpdated guard)"
  Assert (Test-Path $tUpd) "S1 upgrade chain keeps updater cache"

  # ---- S2: plain silent uninstall => data survives (C2) ----------------------
  if (-not (Uninstall-App @())) { exit 2 }
  $appExeGone = -not (Get-ChildItem $tApp -Filter '*.exe' -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -notlike 'Uninstall*' })
  Assert $appExeGone "S2 /S removed the program"
  Assert (Test-Path (Join-Path $tData 'data\config\config.yaml')) "S2 /S keeps user data (default contract)"
  Assert (Test-Path $tUpd) "S2 /S keeps updater cache"

  # ---- S3: silent full wipe => data gone, _bak survives ----------------------
  Remove-Item $tApp -Recurse -Force -ErrorAction SilentlyContinue  # clear uninstaller leftover
  if (-not (Install-App)) { Say "reinstall for S3 failed -- aborting"; exit 2 }
  # let Defender finish scanning the ~500MB installer.exe the install step just
  # copied into the updater cache: probe for an exclusive-open handle instead of
  # gambling on a fixed sleep (uninstalling seconds after installing is not a
  # real-user timeline -- this smoke asserts STEADY-STATE wipe; the lock
  # fallbacks REBOOTOK/RunOnce are pinned by the static gate, not here)
  $instCache = Join-Path $tUpd 'installer.exe'
  if (Test-Path $instCache) {
    for ($i = 0; $i -lt 45; $i++) {
      try { $fs = [IO.File]::Open($instCache, 'Open', 'Read', 'None'); $fs.Close(); break }
      catch { Start-Sleep -Seconds 2 }
    }
    Say ("AV settle probe done after " + (2 * $i) + "s")
  }
  if (-not (Uninstall-App @('--delete-app-data'))) { exit 2 }
  Assert (-not (Test-Path $tData)) "S3 --delete-app-data erased the data dir"
  # the updater cache holds the ~500MB installer.exe the install step just
  # copied; Defender often scan-locks it longer than the uninstaller's 15s
  # retry budget. The CONTRACT is: erased now OR scheduled for removal on
  # reboot via /REBOOTOK (PendingFileRenameOperations) -- both fulfil the wipe.
  $updGone = -not (Test-Path $tUpd)
  $updScheduled = $false
  if (-not $updGone) {
    $pfro = (Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager' `
      -Name PendingFileRenameOperations -ErrorAction SilentlyContinue).PendingFileRenameOperations
    if ($pfro) {
      $updScheduled = [bool]($pfro | Where-Object { $_ -like '*telegram-ai-desktop-updater*' })
    }
    if (-not $updScheduled) {
      # per-user uninstall runs UNELEVATED: /REBOOTOK cannot write HKLM PFRO
      # (measured 2026-08-21, silently no-ops). The fallback that actually
      # fires for real users is the HKCU RunOnce `rd /s /q` the wipe writes in
      # the TEST USER's hive -- load it offline and accept it as "scheduled".
      $hive = "HKU\cxsmokehive"
      $ntuser = Join-Path $tProfile 'NTUSER.DAT'
      reg load $hive $ntuser 2>&1 | Out-Null
      if ($LASTEXITCODE -eq 0) {
        $ro = Get-ItemProperty ("Registry::HKEY_USERS\cxsmokehive\Software\Microsoft\Windows\CurrentVersion\RunOnce") -ErrorAction SilentlyContinue
        if ($ro) {
          $updScheduled = [bool]($ro.PSObject.Properties | Where-Object {
            $_.Name -like 'ChatXWipe*' -and "$($_.Value)" -like '*-updater*' })
        }
        [gc]::Collect(); [gc]::WaitForPendingFinalizers()
        reg unload $hive 2>&1 | Out-Null
      } else {
        # profile still referenced -> read the live hive under the user's SID
        $sid = (Get-LocalUser -Name $user -ErrorAction SilentlyContinue).SID.Value
        if ($sid) {
          $ro = Get-ItemProperty ("Registry::HKEY_USERS\" + $sid + "\Software\Microsoft\Windows\CurrentVersion\RunOnce") -ErrorAction SilentlyContinue
          if ($ro) {
            $updScheduled = [bool]($ro.PSObject.Properties | Where-Object {
              $_.Name -like 'ChatXWipe*' -and "$($_.Value)" -like '*-updater*' })
          }
        }
      }
    }
  }
  Assert ($updGone -or $updScheduled) "S3 updater cache erased now or scheduled for auto removal (REBOOTOK/RunOnce)"
  if ((-not $updGone) -and $updScheduled) {
    Say "  (updater cache was scan-locked; reboot/logon fallback engaged as designed)"
  }
  Assert (Test-Path (Join-Path $tBak 'marker.txt')) "S3 sibling *_bak20* recovery dir SURVIVED (ops discipline)"

  if ($script:fails -eq 0) { $overall = 0 } else { $overall = 1 }
}
finally {
  Remove-Item (Join-Path $env:PUBLIC 'cx_smoke_setup.exe') -Force -ErrorAction SilentlyContinue
  if (-not $KeepUser) {
    # best-effort: kill anything still running in the test session, then drop
    # the user + profile so repeated runs stay clean
    Get-Process -ErrorAction SilentlyContinue |
      Where-Object { $_.Path -and $_.Path -like ('*\' + $user + '\*') } |
      Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 1
    Remove-TestUser
    Say "test user removed"
  } else {
    Say ("test user kept: " + $user + " / " + $pass)
  }
}

if ($overall -eq 0) { Say "ALL GREEN" } else { Say ("FAILURES: " + $script:fails) }
exit $overall
