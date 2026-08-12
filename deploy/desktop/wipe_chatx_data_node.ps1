# wipe_chatx_data_node.ps1 -- wipe ChatX per-user DATA for a FRESH reinstall.
#
# Runs ON THE TARGET machine (copied there by push_chatx.ps1 flow or scp).
# Deliberately a SEPARATE tool from install_chatx_node.ps1: upgrades PRESERVE
# user data by design (that is the whole upgrade-self-heal contract); wiping is
# an explicit destructive decision -- it deletes telegram/LINE logins, local
# chat dbs, overlay settings and the hosted-token cache. Only run when the goal
# is "factory-fresh reinstall" (machine fingerprint is hardware-derived, so the
# trial claim / device token re-mints itself on next first-boot).
#
# ASCII-only on purpose (PS 5.1 decodes BOM-less UTF-8 as GBK). The Electron
# userData dir is named after the CJK productName, so we NEVER match a CJK
# literal over SSH -- we match by SHAPE: a directory right under %APPDATA%
# containing data\config\config.yaml (that is AITR_DATA_DIR=<userData>\data,
# unique to this product family). Everything found is listed before deletion.
#
# -DryRun = safe probe: prints installed version, running process count and the
# data dirs (with sizes) it WOULD remove, touches nothing.
# Exit: 0 done (also when nothing found) / 1 something could not be removed
[CmdletBinding()]
param([switch]$DryRun)

$ErrorActionPreference = 'Continue'
function Say($m) { Write-Output ("[wipe] " + $m) }

# -- inventory (always printed; -DryRun stops after this = safe probe) --------
$dir = Join-Path $env:LOCALAPPDATA 'Programs\telegram-ai-desktop'
$app = Get-ChildItem $dir -Filter *.exe -ErrorAction SilentlyContinue |
  Where-Object { $_.Name -notlike 'Uninstall*' } | Select-Object -First 1
if ($app) { Say ("installed app: " + $app.VersionInfo.FileVersion) }
else { Say "installed app: none" }

$procs = Get-Process -ErrorAction SilentlyContinue |
  Where-Object { $_.Path -and $_.Path -like '*telegram-ai-desktop*' }
Say ("running processes: " + @($procs).Count)

$targets = @()
Get-ChildItem $env:APPDATA -Directory -ErrorAction SilentlyContinue | ForEach-Object {
  # 2026-08-11 rollout lesson: backup_chatx_data_node.ps1 renames live data to
  # <dir>_bak<stamp> as a RECOVERY copy -- those still match the config.yaml shape,
  # and this script used to delete them minutes after they were made (backup skips
  # '*bak20*', wipe did not). Recovery copies are only ever deleted by a human.
  if ($_.Name -like '*bak20*') { return }
  if (Test-Path (Join-Path $_.FullName 'data\config\config.yaml')) {
    $mb = 0.0
    try {
      $mb = [math]::Round((((Get-ChildItem $_.FullName -Recurse -File -ErrorAction SilentlyContinue |
        Measure-Object Length -Sum).Sum) / 1MB), 1)
    } catch { }
    Say ("data dir: " + $_.FullName + " (" + $mb + " MB)")
    $targets += $_.FullName
  }
}
$upd = Join-Path $env:LOCALAPPDATA 'telegram-ai-desktop-updater'
if (Test-Path $upd) { Say ("updater cache: " + $upd); $targets += $upd }

if (-not $targets) { Say "nothing to wipe (already clean)" }
if ($DryRun) { Say "dry-run: nothing touched"; exit 0 }
if (-not $targets) { exit 0 }

# -- stop app + backend/sidecars (file locks); match by path, never CJK name --
if ($procs) {
  Say ("stopping: " + (($procs | ForEach-Object { $_.Id }) -join ','))
  $procs | Stop-Process -Force -ErrorAction SilentlyContinue
  Start-Sleep -Seconds 3
}

$fail = 0
foreach ($t in $targets) {
  Say ("remove: " + $t)
  Remove-Item $t -Recurse -Force -ErrorAction SilentlyContinue
  if (Test-Path $t) {
    # one retry after a beat -- backend shutdown can lag the process kill
    Start-Sleep -Seconds 3
    Remove-Item $t -Recurse -Force -ErrorAction SilentlyContinue
  }
  if (Test-Path $t) { Say "  STILL PRESENT (locked?)"; $fail = 1 }
}
if ($fail) { exit 1 }
Say "OK"
exit 0
