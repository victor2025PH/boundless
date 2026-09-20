# instance_restore.ps1 -- restore an instance backup zip onto a data root, with
# per-file sha256 verification and PRAGMA integrity_check on every database.
#
# Thin operator wrapper over scripts/instance_backup.py. ASCII-only (PS 5.1).
# Refuses a non-empty target unless -Force (never silently overlays data).
#
# After a successful restore, DO NOT just start the instance:
#   1. license rebind (machine fingerprint changed) -- vendor re-signs, see SOP
#   2. make sure the OLD machine's instance is stopped (same Telegram session
#      started twice kicks each other)
#   -> docs/智聊实例备份迁移换机SOP_2026-08.md
#
# Usage:
#   powershell -File scripts\instance_restore.ps1 -Zip <backup.zip> -TargetRoot D:\chengjie-instances\zhiliao\data
#
# Exit: passthrough from the python core (0 ok / 4 verification failed)
[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)][string]$Zip,
  [Parameter(Mandatory = $true)][string]$TargetRoot,
  [switch]$Force
)
$ErrorActionPreference = 'Stop'
$eng = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$core = Join-Path $eng 'scripts\instance_backup.py'
if (-not (Test-Path $core)) { Write-Output "[restore] core missing: $core"; exit 2 }
$argv = @($core, 'restore', '--zip', $Zip, '--target-root', $TargetRoot)
if ($Force) { $argv += '--force' }
& python @argv
exit $LASTEXITCODE
