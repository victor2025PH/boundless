# backup_chatx_data_node.ps1 -- rename ChatX per-user data dirs to *_<stamp>
# instead of deleting them: the app sees a factory-fresh machine on next start
# (same effect as wipe_chatx_data_node.ps1) but the old logins/chat dbs stay on
# disk for recovery. Pair with a fresh install; delete the _bak dirs once the
# node is confirmed healthy.
#
# Matches data dirs by SHAPE (dir under %APPDATA% containing data\config\config.yaml)
# exactly like wipe_chatx_data_node.ps1 -- never by CJK name (PS 5.1 GBK lesson).
# Skips dirs that already look like backups. ASCII-only on purpose.
# Exit: 0 ok / 1 a rename failed (data dir may be locked)
[CmdletBinding()]
param([string]$Stamp = ("bak" + (Get-Date -Format "yyyyMMdd_HHmm")))

$ErrorActionPreference = 'Continue'
function Say($m) { Write-Output ("[bakdata] " + $m) }

# stop app + sidecars first (renaming a locked dir fails); same match as stop script
$procs = Get-Process -ErrorAction SilentlyContinue |
  Where-Object { $_.Path -and $_.Path -like '*telegram-ai-desktop*' }
if ($procs) {
  Say ("stopping: " + ($procs.Id -join ','))
  $procs | Stop-Process -Force -ErrorAction SilentlyContinue
  Start-Sleep -Seconds 3
  Get-Process -ErrorAction SilentlyContinue |
    Where-Object { $_.Path -and $_.Path -like '*telegram-ai-desktop*' } |
    Stop-Process -Force -ErrorAction SilentlyContinue
  Start-Sleep -Seconds 2
}

$fail = 0
$n = 0
Get-ChildItem $env:APPDATA -Directory -ErrorAction SilentlyContinue | ForEach-Object {
  if ($_.Name -like '*bak20*') { return }
  if (-not (Test-Path (Join-Path $_.FullName 'data\config\config.yaml'))) { return }
  $mb = 0.0
  try {
    $mb = [math]::Round((((Get-ChildItem $_.FullName -Recurse -File -ErrorAction SilentlyContinue |
      Measure-Object Length -Sum).Sum) / 1MB), 1)
  } catch { }
  $new = $_.Name + '_' + $Stamp
  Say ("rename: " + $_.FullName + " (" + $mb + " MB) -> " + $new)
  try {
    Rename-Item -Path $_.FullName -NewName $new -ErrorAction Stop
    $n++
  } catch {
    Say ("  RENAME FAILED: " + $_.Exception.Message)
    $fail = 1
  }
}
if ($n -eq 0) { Say "no data dirs found (already clean)" }

# updater cache pins old versions; small + no user data -> safe to drop
$upd = Join-Path $env:LOCALAPPDATA 'telegram-ai-desktop-updater'
if (Test-Path $upd) {
  Say "removing updater cache"
  Remove-Item $upd -Recurse -Force -ErrorAction SilentlyContinue
}

if ($fail) { exit 1 }
Say ("OK renamed=" + $n)
exit 0
