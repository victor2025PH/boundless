# tenant_guard_task.ps1 — 托管租户三守护计划任务壳（履约 / 自愈 / 夜间灾备）
#
# 与 fulfill_chatx_task.ps1（装机 license 履约）同款模式：单发免僵尸、state 幂等、
# UTF-8 日志按日轮转。三个任务共用本壳，-Mode 区分：
#   fulfill  每 5 分钟：paid 托管单 → provision+expose+可达性闸门 → 回填交付
#            （公网未就绪=持单+告警，绝不给客户发打不开的网址；机密同装机守护目录）
#   watch    每 10 分钟：租户自愈一轮（DOWN 且应在跑 → 幂等拉起；suspended 旗尊重；
#            冷却/连败转人工由 tenant_ops watch 内建；生产双实例不在管辖）
#   backup   每日 04:40：全租户灾备快照（SQLite backup API，活库安全；保留 7 份）
#
# 机密（仅 fulfill 需要，与装机守护共用目录 D:\chengjie-instances\.ops\fulfill\）：
#   site.txt / admin_key.txt   —— 缺任一拒绝注册 fulfill（防空转假装在履约）
#
# 用法：
#   .\tenant_guard_task.ps1 -Mode fulfill            # 单次履约轮（计划任务入口）
#   .\tenant_guard_task.ps1 -Mode watch              # 单次自愈轮
#   .\tenant_guard_task.ps1 -Mode backup             # 单次全量灾备
#   .\tenant_guard_task.ps1 -Register                # 注册全部三个计划任务
#   .\tenant_guard_task.ps1 -Unregister              # 摘除全部三个
#   .\tenant_guard_task.ps1 -Mode fulfill -DryRun    # 履约干跑（只列单不开通）
#
# 日志：logs\tenant_guard\<mode>_YYYYMMDD.log（按日一份，保留 30 份；UTF-8）。

[CmdletBinding()]
param(
    [ValidateSet('fulfill', 'watch', 'backup')]
    [string]$Mode = 'fulfill',
    [switch]$Register,
    [switch]$Unregister,
    [switch]$DryRun,
    [int]$FulfillIntervalMin = 5,
    [int]$WatchIntervalMin = 10,
    [string]$BackupTime = '04:40',
    [string]$SecretsDir = 'D:\chengjie-instances\.ops\fulfill'
)

$ErrorActionPreference = 'Continue'
$root = Split-Path -Parent $PSScriptRoot
$env:PYTHONIOENCODING = 'utf-8'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}

$siteFile = Join-Path $SecretsDir 'site.txt'
$keyFile  = Join-Path $SecretsDir 'admin_key.txt'

$Tasks = @(
    @{ Name = 'TenantFulfillWatch';  Args = '-Mode fulfill' },
    @{ Name = 'TenantSelfHeal';      Args = '-Mode watch' },
    @{ Name = 'TenantBackupNightly'; Args = '-Mode backup' }
)

if ($Unregister) {
    foreach ($t in $Tasks) { schtasks /Delete /TN $t.Name /F 2>$null }
    Write-Host '[tenant-guard] 三任务已摘除'
    exit 0
}

if ($Register) {
    # fulfill 依赖机密：缺则拒注册整组（自愈/灾备虽不依赖，但半组注册容易让人误以为履约在跑）
    $missing = @($siteFile, $keyFile) | Where-Object { -not (Test-Path -LiteralPath $_) }
    if ($missing.Count) {
        Write-Host '[tenant-guard] 机密未备齐，拒绝注册：' -ForegroundColor Red
        $missing | ForEach-Object { Write-Host ("  缺 " + $_) }
        exit 2
    }
    # 经 run_hidden.vbs (wscript SW_HIDE) 启动：交互式分钟任务直指 powershell.exe 会在
    # 每次触发时闪一个可见控制台窗（-WindowStyle Hidden 也来不及——窗口先于 PS 存在），
    # 即 2026-08-07「全网每分钟弹窗」事故同款病根；vbs 缺失时回退裸 powershell（保任务不断）。
    $vbs = Join-Path (Split-Path (Split-Path $root -Parent) -Parent) 'deploy\instances\run_hidden.vbs'
    if (Test-Path -LiteralPath $vbs) {
        $shell = 'C:\Windows\SysWOW64\wscript.exe //B //Nologo ' + $vbs +
            ' C:\Windows\Sysnative\WindowsPowerShell\v1.0\powershell.exe' +
            ' -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{0}" {1}'
    } else {
        $shell = 'powershell -NoProfile -ExecutionPolicy Bypass -File "{0}" {1}'
    }
    schtasks /Create /TN 'TenantFulfillWatch' /SC MINUTE /MO $FulfillIntervalMin `
        /TR ($shell -f $PSCommandPath, '-Mode fulfill') /F | Out-Null
    $rc1 = $LASTEXITCODE
    schtasks /Create /TN 'TenantSelfHeal' /SC MINUTE /MO $WatchIntervalMin `
        /TR ($shell -f $PSCommandPath, '-Mode watch') /F | Out-Null
    $rc2 = $LASTEXITCODE
    schtasks /Create /TN 'TenantBackupNightly' /SC DAILY /ST $BackupTime `
        /TR ($shell -f $PSCommandPath, '-Mode backup') /F | Out-Null
    $rc3 = $LASTEXITCODE
    if (($rc1 + $rc2 + $rc3) -eq 0) {
        Write-Host ("[tenant-guard] 已注册：TenantFulfillWatch(每{0}min) + TenantSelfHeal(每{1}min) + TenantBackupNightly({2})" `
            -f $FulfillIntervalMin, $WatchIntervalMin, $BackupTime)
        Write-Host '  日志: logs\tenant_guard\   摘除: -Unregister'
    } else {
        Write-Host "[tenant-guard] 注册有失败 rc=($rc1,$rc2,$rc3)（需管理员权限？）" -ForegroundColor Red
    }
    exit ([int](($rc1 + $rc2 + $rc3) -ne 0))
}

# ── 单次执行（计划任务入口）────────────────────────────────────────────────
$logDir = Join-Path $root 'logs\tenant_guard'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir ('{0}_{1}.log' -f $Mode, (Get-Date -Format 'yyyyMMdd'))

Set-Location $root
('[{0}] tick {1}{2}' -f $Mode, (Get-Date -Format 'o'), $(if ($DryRun) { ' (dry-run)' } else { '' })) |
    Out-File $log -Append -Encoding utf8

switch ($Mode) {
    'fulfill' {
        if (-not ((Test-Path $siteFile) -and (Test-Path $keyFile))) {
            ('[fulfill] 机密缺失，跳过本轮') | Out-File $log -Append -Encoding utf8
            exit 2
        }
        $env:ADMIN_KEY = (Get-Content -LiteralPath $keyFile -Raw).Trim()
        $site = (Get-Content -LiteralPath $siteFile -Raw).Trim()
        $pyArgs = @('scripts/tenant_fulfill_watch.py', '--site', $site)
        if ($DryRun) { $pyArgs += '--dry-run' }
        & python @pyArgs 2>&1 | ForEach-Object { $_.ToString() } | Out-File $log -Append -Encoding utf8
        $rc = $LASTEXITCODE
        $env:ADMIN_KEY = ''
    }
    'watch' {
        & python scripts/tenant_ops.py watch 2>&1 | ForEach-Object { $_.ToString() } |
            Out-File $log -Append -Encoding utf8
        $rc = $LASTEXITCODE
    }
    'backup' {
        & python scripts/tenant_ops.py backup --all --keep 7 2>&1 | ForEach-Object { $_.ToString() } |
            Out-File $log -Append -Encoding utf8
        $rc = $LASTEXITCODE
    }
}

('[{0}] exit={1}' -f $Mode, $rc) | Out-File $log -Append -Encoding utf8
Get-ChildItem $logDir -Filter ('{0}_*.log' -f $Mode) | Sort-Object Name -Descending |
    Select-Object -Skip 30 | Remove-Item -Force -ErrorAction SilentlyContinue
exit $rc
