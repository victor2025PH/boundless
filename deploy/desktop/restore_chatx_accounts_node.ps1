# restore_chatx_accounts_node.ps1 -- copy the LOGIN/ACCOUNT artifacts from the
# newest *_bak<stamp> recovery dir (made by backup_chatx_data_node.ps1) into a
# freshly-installed ChatX data dir, BEFORE first launch.
#
# This is the "factory-fresh install but keep the operator's logins" half of
# push_chatx.ps1 -FreshInstall -KeepAccounts. Safe by design:
#   * the recovery dir is never modified or deleted (rename-backup stays intact);
#   * first-boot seeding (_ensure_seeded / _ensure_seeded_extras) only fills in
#     MISSING files, so anything we restore here is left alone by the app;
#   * restore wins over fresh state: each kept item is removed from the dest
#     first, then copied whole from the backup (no half-merged profiles).
#
# What counts as "logins/accounts" (measured against the real layout 2026-08-13):
#   config.json                          shell settings + web-multiopen account LIST
#   Partitions\                          web-client logins (persist:<acc.id> cookies)
#   data\config\account_registry.db(+wal/shm)  protocol account registry
#   data\config\config.local.yaml        overlay: creds/keys the wizard wrote
#   data\sessions\                       telegram/LINE protocol session files
#   data\*-sessions\                     sidecar logins (whatsapp/messenger profiles)
# Everything else (chat dbs, caches, logs, kb, media) stays factory-fresh.
# CROSS-REF: the uninstaller's "keep my data" copy (desktop/build/installer.nsh
# customHeader, cxKeepDetail) is the user-facing rendering of this same
# inventory -- if this keep-list changes shape, update that copy too.
#
# ASCII-only on purpose (PS 5.1 GBK lesson). Never matches the CJK dir name --
# the backup dir is found by SHAPE (contains data\config\config.yaml) and the
# destination name is derived by stripping the _bak<stamp> suffix.
#
# Usage (on the target node):
#   powershell -ExecutionPolicy Bypass -File restore_chatx_accounts_node.ps1            # newest bak
#   powershell -... -File restore_chatx_accounts_node.ps1 -DryRun                       # show plan only
#   powershell -... -File restore_chatx_accounts_node.ps1 -From "C:\...\<dir>_bak20260813_0700"
# Exit: 0 ok (also when some items are absent in the bak) / 1 copy failed /
#       2 no backup dir found / 3 cannot derive destination name
[CmdletBinding()]
param(
  [string]$From = "",
  [string]$AppDataRoot = $env:APPDATA,
  [switch]$DryRun
)

$ErrorActionPreference = 'Continue'
function Say($m) { Write-Output ("[restore] " + $m) }

# --- 1. locate the recovery dir (newest *_bak20* with the product shape) -----
if ($From) {
  $bak = Get-Item $From -ErrorAction SilentlyContinue
  if (-not $bak) { Say "backup dir not found: $From"; exit 2 }
} else {
  $bak = Get-ChildItem $AppDataRoot -Directory -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -like '*_bak20*' } |
    Where-Object { Test-Path (Join-Path $_.FullName 'data\config\config.yaml') } |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1
  if (-not $bak) { Say "no *_bak20* recovery dir under $AppDataRoot"; exit 2 }
}
Say ("backup: " + $bak.FullName)

# --- 2. derive the live data dir name (strip _bak<stamp>) --------------------
if ($bak.Name -notmatch '^(.+)_bak20\d{6}_\d{4}$') {
  Say ("cannot derive destination from name: " + $bak.Name + " (pass -From a standard _bak dir)")
  exit 3
}
$dest = Join-Path $AppDataRoot $Matches[1]
Say ("dest:   " + $dest)

# --- 3. the keep-list (relative to the data dir root) ------------------------
$items = @('config.json', 'Partitions', 'data\config\account_registry.db',
           'data\config\account_registry.db-wal', 'data\config\account_registry.db-shm',
           'data\config\config.local.yaml', 'data\sessions')
$dataRoot = Join-Path $bak.FullName 'data'
Get-ChildItem $dataRoot -Directory -Filter '*-sessions' -ErrorAction SilentlyContinue |
  ForEach-Object { $items += ('data\' + $_.Name) }

# --- 4. stop anything holding locks (same match as backup/wipe: by path) -----
# Only when operating on the REAL %APPDATA%: a synthetic -AppDataRoot means a
# test run, and killing the build machine's own seat app from a test is exactly
# what happened on 2026-08-13 (14 processes down, operator app included).
$realRun = ($AppDataRoot -eq $env:APPDATA)
$procs = Get-Process -ErrorAction SilentlyContinue |
  Where-Object { $_.Path -and $_.Path -like '*telegram-ai-desktop*' }
if ($procs -and $realRun -and -not $DryRun) {
  Say ("stopping: " + ($procs.Id -join ','))
  $procs | Stop-Process -Force -ErrorAction SilentlyContinue
  Start-Sleep -Seconds 3
}

# --- 5. copy each item whole (remove dest item first: restore wins) ----------
$fail = 0
$done = 0
foreach ($rel in $items) {
  $src = Join-Path $bak.FullName $rel
  if (-not (Test-Path $src)) { continue }
  $dst = Join-Path $dest $rel
  $mb = 0.0
  try {
    if ((Get-Item $src) -is [System.IO.DirectoryInfo]) {
      $mb = [math]::Round((((Get-ChildItem $src -Recurse -File -ErrorAction SilentlyContinue |
        Measure-Object Length -Sum).Sum) / 1MB), 1)
    } else { $mb = [math]::Round(((Get-Item $src).Length / 1MB), 1) }
  } catch { }
  if ($DryRun) { Say ("would restore: " + $rel + " (" + $mb + " MB)"); continue }
  New-Item -ItemType Directory -Path (Split-Path $dst -Parent) -Force -ErrorAction SilentlyContinue | Out-Null
  if (Test-Path $dst) { Remove-Item $dst -Recurse -Force -ErrorAction SilentlyContinue }
  if ((Get-Item $src) -is [System.IO.DirectoryInfo]) {
    # robocopy: fast on chromium-profile trees, long-path safe; <8 = success
    robocopy $src $dst /E /NFL /NDL /NJH /NJS /NP /R:2 /W:2 | Out-Null
    if ($LASTEXITCODE -ge 8) { Say ("  COPY FAILED (robocopy " + $LASTEXITCODE + "): " + $rel); $fail = 1; continue }
  } else {
    Copy-Item $src $dst -Force -ErrorAction SilentlyContinue
    if (-not (Test-Path $dst)) { Say ("  COPY FAILED: " + $rel); $fail = 1; continue }
  }
  Say ("restored: " + $rel + " (" + $mb + " MB)")
  $done++
}

if ($DryRun) { Say "dry-run: nothing touched"; exit 0 }
if ($fail) { Say "FINISHED WITH FAILURES"; exit 1 }
Say ("OK restored=" + $done + " (recovery dir kept: " + $bak.Name + ")")
exit 0
