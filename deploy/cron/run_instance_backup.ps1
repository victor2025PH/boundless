# run_instance_backup.ps1 -- scheduled-task shell for FULL production instance backup.
#
# ASCII-only on purpose (PS 5.1 decodes BOM-less UTF-8 as GBK), same convention as
# the script it wraps: engines\chengjie\scripts\instance_backup.ps1.
#
# Why this shell exists (2026-08-27 P0 remediation):
#   The core CLI (scripts\instance_backup.py) has NO retention -- by design, it makes
#   exactly one zip and lets the operator choose storage. Running it daily unattended
#   therefore grows without bound (~470MB/run). This shell adds the two things a
#   scheduled task needs and nothing else: retention (-Keep) and a dated log.
#
#   Scope note: the tenant guard task (tenant_guard_task.ps1 -Mode backup) explicitly
#   does NOT cover the production instances ("生产双实例不在管辖", see its header) --
#   it snapshots hosted tenants only. Before this shell, production zhiliao had no
#   routine backup at all; its newest artifact was a 10-day-old drill zip.
#
#   Cloud/offsite upload is deliberately NOT built in here either -- the core script
#   documents that customers pick their own storage. Offsite remains a separate,
#   explicit operator decision.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File deploy\cron\run_instance_backup.ps1
#   powershell -ExecutionPolicy Bypass -File deploy\cron\run_instance_backup.ps1 -Keep 14
#   powershell -ExecutionPolicy Bypass -File deploy\cron\run_instance_backup.ps1 -DryRun
#
# Exit: 0 ok / 2 config error / passthrough non-zero from the python core.

[CmdletBinding()]
param(
    [string]$DataRoot = 'D:\chengjie-instances\zhiliao\data',
    [string]$OutDir   = '',
    [int]$Keep        = 7,
    [string]$Label    = 'nightly',
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'

$RepoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$Wrapper  = Join-Path $RepoRoot 'engines\chengjie\scripts\instance_backup.ps1'

$LogDir = Join-Path $RepoRoot 'logs\cron'
if (-not (Test-Path -LiteralPath $LogDir)) { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }
$LogFile = Join-Path $LogDir ('instance_backup_{0}.log' -f (Get-Date -Format 'yyyyMMdd'))

function Say([string]$msg) {
    $line = "[instance_backup {0:yyyy-MM-dd HH:mm:ss}] {1}" -f (Get-Date), $msg
    Write-Host $line
    try { Add-Content -LiteralPath $LogFile -Value $line -Encoding UTF8 } catch {}
}

if (-not (Test-Path -LiteralPath $Wrapper)) { Say "config error: wrapper missing $Wrapper"; exit 2 }
if (-not (Test-Path -LiteralPath $DataRoot -PathType Container)) { Say "config error: data root missing $DataRoot"; exit 2 }

# Default out dir = <instance>\backups (sibling of data), matching the existing artifacts.
if (-not $OutDir) { $OutDir = Join-Path (Split-Path -Parent $DataRoot) 'backups' }
if (-not (Test-Path -LiteralPath $OutDir)) { New-Item -ItemType Directory -Path $OutDir -Force | Out-Null }

Say "start  data_root=$DataRoot  out_dir=$OutDir  keep=$Keep  dry_run=$($DryRun.IsPresent)"

if ($DryRun) {
    $existing = @(Get-ChildItem -LiteralPath $OutDir -Filter '*.zip' -File -ErrorAction SilentlyContinue)
    Say ("dry-run: would back up {0} and keep newest {1} of {2} existing zip(s)" -f $DataRoot, $Keep, $existing.Count)
    exit 0
}

& powershell -NoProfile -ExecutionPolicy Bypass -File $Wrapper -DataRoot $DataRoot -OutDir $OutDir -Label $Label
$code = $LASTEXITCODE

if ($code -ne 0) {
    Say "backup FAILED exit=$code (retention skipped on purpose: never prune when the new copy is unproven)"
    exit $code
}

# Retention runs only after a proven-good new copy exists.
$zips = @(Get-ChildItem -LiteralPath $OutDir -Filter '*.zip' -File -ErrorAction SilentlyContinue |
          Sort-Object LastWriteTime -Descending)
if ($zips.Count -gt $Keep) {
    foreach ($old in $zips[$Keep..($zips.Count - 1)]) {
        try {
            Remove-Item -LiteralPath $old.FullName -Force
            Say ("pruned {0} ({1} MB)" -f $old.Name, [math]::Round($old.Length / 1MB, 1))
        } catch {
            Say ("prune failed for {0}: {1}" -f $old.Name, $_.Exception.Message)
        }
    }
}

$newest = @(Get-ChildItem -LiteralPath $OutDir -Filter '*.zip' -File | Sort-Object LastWriteTime -Descending)[0]
Say ("done   newest={0} ({1} MB)  kept={2}" -f $newest.Name, [math]::Round($newest.Length / 1MB, 1),
     @(Get-ChildItem -LiteralPath $OutDir -Filter '*.zip' -File).Count)
exit 0
