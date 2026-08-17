# instance_backup.ps1 -- full backup of ONE instance data root into a manifest zip.
#
# Thin operator wrapper over scripts/instance_backup.py (the testable core:
# denylist excludes, sqlite ONLINE snapshots for live dbs, per-file sha256
# manifest). ASCII-only on purpose (PS 5.1 decodes BOM-less UTF-8 as GBK).
#
# Usage:
#   powershell -File scripts\instance_backup.ps1 -DataRoot D:\chengjie-instances\zhiliao\data
#   powershell -File scripts\instance_backup.ps1 -DataRoot ... -OutDir E:\bak -Label pre-migration
#
# Scheduled-task example (weekly, operator picks the storage; auto-cloud is
# deliberately NOT built in -- customers choose their own storage):
#   schtasks /Create /TN ChatXInstanceBackupWeekly /SC WEEKLY /D SUN /ST 06:40 /F ^
#     /TR "powershell -ExecutionPolicy Bypass -File <engine>\scripts\instance_backup.ps1 -DataRoot <root> -OutDir <dir>"
#
# Exit: passthrough from the python core (0 ok / non-zero failed)
[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)][string]$DataRoot,
  [string]$OutDir = "",
  [string]$Label = ""
)
$ErrorActionPreference = 'Stop'
$eng = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$core = Join-Path $eng 'scripts\instance_backup.py'
if (-not (Test-Path $core)) { Write-Output "[backup] core missing: $core"; exit 2 }
$argv = @($core, 'backup', '--data-root', $DataRoot)
if ($OutDir) { $argv += @('--out-dir', $OutDir) }
if ($Label)  { $argv += @('--label', $Label) }
& python @argv
exit $LASTEXITCODE
