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
    [switch]$DryRun,

    # ── 异地副本（默认关，必须由运营显式开启）───────────────────────────────
    # 为什么不默认开：备份包里是**明文客户聊天记录**（messages.text / FTS 镜像均未加密，
    # 见体检 PII 项）。把它推上对象存储是一个数据保护决策，不该由脚本替运营做：
    #   1) 需要一个**专用桶**（现有 r2:avatarhub 是下载镜像，不能混放客户数据）；
    #   2) 强烈建议走 rclone crypt remote 加密后再传，否则等于把 PII 明文放到云上；
    #   3) 保留策略与本地 -Keep 独立，需在云侧另配生命周期规则。
    # 三件事都定了再开 -Offsite；本地备份不依赖它，关着也不影响主路。
    [switch]$Offsite,
    [string]$RcloneExe    = 'C:\Tools\rclone\rclone.exe',
    [string]$RcloneConf   = '',          # 缺省用仓库内 deploy\secrets\rclone_r2.conf
    [string]$OffsiteRemote = '',         # 例：r2:boundless-instance-backups/zhiliao
    [int]$OffsiteKeepDays = 30           # 远端按**天龄**清理（见下方为什么不用 sync）
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

# rclone 把进度/NOTICE 写 stderr，而本脚本 $ErrorActionPreference='Stop' 下，PS 5.1
# 会把原生命令的 stderr 当成 NativeCommandError 抛出 —— 实测：check 明明报
# "0 differences found"（成功），脚本却在那一行中断、后面的清理压根没跑。
# 故所有 rclone 调用统一经此包装：局部降级 EAP，把输出原样喂给 Say，只用退出码判成败。
function Invoke-Rclone {
    param([string[]]$RcArgs, [string]$Tag = 'rclone')
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $RcloneExe @RcArgs 2>&1 | ForEach-Object { Say "  ${Tag}: $_" }
        return $LASTEXITCODE
    } finally { $ErrorActionPreference = $prev }
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

# 异地副本：仅在显式 -Offsite 时执行；失败**不**改变退出码——本地备份已经成功，
# 不能因为云侧不可达就把整个任务报红（否则运营会为了消红而关掉整个备份）。
if ($Offsite) {
    if (-not $OffsiteRemote) {
        Say 'offsite SKIP: -Offsite 已开但未给 -OffsiteRemote（例 r2:boundless-backups/zhiliao）'
    }
    elseif (-not (Test-Path -LiteralPath $RcloneExe)) {
        Say "offsite SKIP: rclone 不存在 $RcloneExe"
    }
    else {
        if (-not $RcloneConf) { $RcloneConf = Join-Path $RepoRoot 'deploy\secrets\rclone_r2.conf' }
        if (-not (Test-Path -LiteralPath $RcloneConf)) {
            Say "offsite SKIP: rclone 配置不存在 $RcloneConf"
        }
        else {
            Say "offsite 上传 $($newest.Name) -> $OffsiteRemote"
            $rcUp = Invoke-Rclone -Tag 'copy' -RcArgs @(
                '--config', $RcloneConf, 'copy', $newest.FullName, $OffsiteRemote, '--no-traverse')
            if ($rcUp -ne 0) {
                Say "offsite 上传 FAILED exit=$rcUp（本地备份仍然有效，任务不因此报红）"
            }
            else {
                # 「传上去了」只是退出码，不是内容正确。check 比对远端与本地的 hash——
                # 与本仓 media-artifact 纪律同一条：产物必须验内容才许报 OK。
                $rcChk = Invoke-Rclone -Tag 'check' -RcArgs @(
                    '--config', $RcloneConf, 'check', $newest.FullName, $OffsiteRemote, '--one-way')
                if ($rcChk -eq 0) { Say 'offsite OK（hash 已比对一致）' }
                else { Say "offsite 上传成功但 **校验不一致** exit=$rcChk —— 远端那份不可信，请人工复核" }
            }

            # 远端清理刻意用「按天龄删」而不是 rclone sync：
            # sync 会把「本地没有的」远端对象一并删掉 —— 本地备份盘一旦损坏或被误删，
            # 下一次 sync 就把异地副本也抹了，恰好在最需要它的时刻。copy + 按龄删则
            # 本地灾难不会级联到异地，代价只是远端保留窗（默认 30 天）与本地 -Keep 不同步。
            if ($OffsiteKeepDays -gt 0) {
                $rcDel = Invoke-Rclone -Tag 'prune' -RcArgs @(
                    '--config', $RcloneConf, 'delete', $OffsiteRemote, '--min-age', "$($OffsiteKeepDays)d")
                if ($rcDel -eq 0) { Say "offsite 远端清理完成（保留最近 $OffsiteKeepDays 天）" }
                else { Say "offsite 远端清理 FAILED exit=$rcDel（不影响本次备份有效性）" }
            }
        }
    }
}

exit 0
