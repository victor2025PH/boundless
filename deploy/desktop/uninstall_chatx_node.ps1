# uninstall_chatx_node.ps1 -- remove the ChatX desktop app from ONE machine.
#
# Runs ON THE TARGET machine (scp'd there, or run by hand). Deliberately a
# SEPARATE tool from install_chatx_node.ps1: that script uninstalls only as a
# step on the way to installing again, so there was no way to say "just take it
# off this machine" (factory-reset / decommission / reinstall-from-scratch).
# The uninstall logic here is lifted from the install script's step 3, which is
# the version that survived the 2026-07-31 rollout -- keep the two in sync.
#
# This removes the PROGRAM only. User DATA under %APPDATA% survives on purpose
# (that is the upgrade-self-heal contract). For a factory-fresh machine pair it
# with wipe_chatx_data_node.ps1 (delete) or backup_chatx_data_node.ps1 (rename).
#
# ASCII-only on purpose: PS 5.1 decodes BOM-less UTF-8 as GBK and mangles CJK
# (the watchdog_emotion_tts.ps1 lesson). The app's product name is CJK, so we
# NEVER match by display name -- only by install path (telegram-ai-desktop, the
# npm package name electron-builder uses for the directory).
#
# -DryRun = safe probe: prints what it WOULD remove, touches nothing.
# Exit: 0 gone (also when nothing was installed) / 1 something survived
[CmdletBinding()]
param([switch]$DryRun)

$ErrorActionPreference = 'Continue'
function Say($m) { Write-Output ("[uninst] " + $m) }

$installDirs = @(
  (Join-Path $env:LOCALAPPDATA 'Programs\telegram-ai-desktop'),
  'C:\Program Files\telegram-ai-desktop',
  'C:\Program Files (x86)\telegram-ai-desktop'
)

# --- 1. inventory (always printed; -DryRun stops after this) ------------------
foreach ($d in $installDirs) {
  if (-not (Test-Path $d)) { continue }
  $a = Get-ChildItem $d -Filter *.exe -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -notlike 'Uninstall*' } | Select-Object -First 1
  if ($a) { Say ("installed: " + $d + " (" + $a.VersionInfo.FileVersion + ")") }
  else { Say ("leftover dir (no app exe): " + $d) }
}

$procs = Get-Process -ErrorAction SilentlyContinue |
  Where-Object { $_.Path -and $_.Path -like '*telegram-ai-desktop*' }
Say ("running processes: " + @($procs).Count)

# Registry entries are the only reliable handle on a per-user NSIS install; some
# nodes carry BOTH a stale per-machine install and the current per-user one.
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
Say ("registered installs: " + @($found).Count)

# A Run key left pointing at a deleted exe is a silent startup error every logon.
$runKeys = @('HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Run',
             'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Run')
$staleRun = @()
foreach ($rk in $runKeys) {
  $p = Get-ItemProperty $rk -ErrorAction SilentlyContinue
  if (-not $p) { continue }
  foreach ($prop in $p.PSObject.Properties) {
    if ($prop.Name -like 'PS*') { continue }
    if ("$($prop.Value)" -like '*telegram-ai-desktop*') {
      Say ("autostart entry: " + $rk + " :: " + $prop.Name)
      $staleRun += [pscustomobject]@{ Key = $rk; Name = $prop.Name }
    }
  }
}

if (-not $found -and -not ($installDirs | Where-Object { Test-Path $_ })) {
  Say "nothing installed (already clean)"
  if (-not $staleRun) { if ($DryRun) { Say "dry-run: nothing touched" }; exit 0 }
}
if ($DryRun) { Say "dry-run: nothing touched"; exit 0 }

# --- 2. stop app + backend/sidecars (they hold locks on resources/) ----------
if ($procs) {
  Say ("stopping: " + (($procs | ForEach-Object { $_.Id }) -join ','))
  $procs | Stop-Process -Force -ErrorAction SilentlyContinue
  Start-Sleep -Seconds 3
  Get-Process -ErrorAction SilentlyContinue |
    Where-Object { $_.Path -and $_.Path -like '*telegram-ai-desktop*' } |
    Stop-Process -Force -ErrorAction SilentlyContinue
  Start-Sleep -Seconds 2
}

# --- 3. run each registered uninstaller --------------------------------------
foreach ($app in $found) {
  $loc = $app.InstallLocation
  Say ("uninstalling: " + $loc + " (" + $app.DisplayVersion + ")")
  $u = $app.QuietUninstallString
  if (-not $u) { $u = $app.UninstallString + ' /S' }
  $m = [regex]::Match($u, '^\s*"([^"]+)"\s*(.*)$')
  if ($m.Success) { $exe = $m.Groups[1].Value } else { $exe = $u }
  if (-not (Test-Path $exe)) { Say "  uninstaller missing, will force-remove dir"; continue }
  # _?= keeps NSIS synchronous. Without it NSIS copies itself to %TEMP% and
  # returns IMMEDIATELY, so the caller races the still-running uninstaller
  # (measured on .198, 2026-07-31: 0xC0000005). InstallLocation is empty in some
  # registry entries, so derive the dir from the uninstaller path instead.
  if (-not $loc) { $loc = Split-Path $exe -Parent }
  try {
    Start-Process -FilePath $exe -ArgumentList @('/S', ("_?=" + $loc)) -Wait -PassThru | Out-Null
  } catch { Say ("  uninstall raised (ignored): " + $_.Exception.Message) }
  # Even with _?= the elevated child can outlive the parent -- wait for the app
  # binaries to actually go away before touching the disk again.
  for ($i = 0; $i -lt 30; $i++) {
    $left = @(Get-ChildItem $loc -Filter *.exe -ErrorAction SilentlyContinue |
      Where-Object { $_.Name -notlike 'Uninstall*' })
    if ($left.Count -eq 0) { break }
    Start-Sleep -Seconds 1
  }
}
if (-not $found) { Say "no registered install to uninstall" }

# --- 4. force-remove whatever the uninstaller left behind --------------------
foreach ($d in $installDirs) {
  if (-not (Test-Path $d)) { continue }
  Get-Process -ErrorAction SilentlyContinue |
    Where-Object { $_.Path -and $_.Path -like ($d + '*') } |
    Stop-Process -Force -ErrorAction SilentlyContinue
  Start-Sleep -Seconds 1
  Say ("removing dir: " + $d)
  Remove-Item $d -Recurse -Force -ErrorAction SilentlyContinue
  if (Test-Path $d) {
    Start-Sleep -Seconds 3
    Remove-Item $d -Recurse -Force -ErrorAction SilentlyContinue
  }
}

# --- 5. drop stale autostart entries ----------------------------------------
foreach ($e in $staleRun) {
  Say ("removing autostart: " + $e.Key + " :: " + $e.Name)
  Remove-ItemProperty -Path $e.Key -Name $e.Name -Force -ErrorAction SilentlyContinue
}

# --- 6. verify --------------------------------------------------------------
$fail = 0
foreach ($d in $installDirs) {
  if (Test-Path $d) { Say ("STILL PRESENT: " + $d); $fail = 1 }
}
$still = @()
foreach ($k in $keys) {
  $still += Get-ItemProperty $k -ErrorAction SilentlyContinue |
    Where-Object {
      ($_.InstallLocation -and $_.InstallLocation -like '*telegram-ai-desktop*') -or
      ($_.UninstallString -and $_.UninstallString -like '*telegram-ai-desktop*')
    }
}
if ($still) { Say ("registry entry SURVIVED: " + @($still).Count); $fail = 1 }
if ($fail) { exit 1 }
Say "OK removed"
exit 0
