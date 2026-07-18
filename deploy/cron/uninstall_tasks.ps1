# uninstall_tasks.ps1 — 按前缀卸载 Boundless 定时任务（deploy/cron）
#
# 只碰计划任务文件夹 \Boundless\ 下、名字形如 Boundless-* 的任务；其他任务一概不看。
# 缺省 = WhatIf 演练：只列出将卸载的任务，不动任何东西；-Execute 才真卸载（管理员）。
#
# 用法：
#   powershell -ExecutionPolicy Bypass -File deploy\cron\uninstall_tasks.ps1                       # 演练：全部 Boundless-*
#   powershell -ExecutionPolicy Bypass -File deploy\cron\uninstall_tasks.ps1 -Engine chengjie      # 演练：只看 chengjie 的
#   powershell -ExecutionPolicy Bypass -File deploy\cron\uninstall_tasks.ps1 -Engine chengjie -Tasks uploader -Execute
#   powershell -ExecutionPolicy Bypass -File deploy\cron\uninstall_tasks.ps1 -Engine website       # 演练：只看 kpi_weekly（Boundless-website-*）
#
# 退出码：0 = 演练完成 / 全部卸载成功（含「无匹配任务」）   1 = 有卸载失败

[CmdletBinding()]
param(
    [ValidateSet('', 'avatarhub', 'chengjie', 'huoke', 'website')]
    [string]$Engine = '',           # 空 = 全部；website = kpi_weekly 所属段（Boundless-website-*）
    [string[]]$Tasks = @(),         # 空 = 全部任务类型；可选 uploader/purge/export/grants_sync/kpi_weekly（逗号分隔亦可）
    [switch]$Execute                # 缺省 WhatIf 演练；-Execute 才 Unregister-ScheduledTask
)

$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}

$TaskPath = '\Boundless\'

function Say([string]$msg) { Write-Host "[uninstall_tasks] $msg" }

$Tasks = @($Tasks | ForEach-Object { $_ -split ',' } | ForEach-Object { $_.Trim().ToLower() } | Where-Object { $_ })
$bad = @($Tasks | Where-Object { $_ -notin @('uploader', 'purge', 'export', 'grants_sync', 'kpi_weekly') })
if ($bad.Count) { Say "错误: 未知任务类型 $($bad -join ', ')（可选 uploader/purge/export/grants_sync/kpi_weekly）"; exit 1 }

$prefix = if ($Engine) { "Boundless-$Engine-" } else { 'Boundless-' }

$found = @(Get-ScheduledTask -TaskPath $TaskPath -ErrorAction SilentlyContinue |
    Where-Object { $_.TaskName -like "$prefix*" })
if ($Tasks.Count) {
    $found = @($found | Where-Object {
        $name = $_.TaskName
        # 任务名 Boundless-<engine>-<task>[-<实例>] → 第三段是任务类型
        $parts = $name -split '-'
        ($parts.Count -ge 3) -and ($parts[2].ToLower() -in $Tasks)
    })
}

$mode = if ($Execute) { 'EXECUTE（真卸载）' } else { 'WHATIF 演练（只列出，不卸载；加 -Execute 才卸载）' }
Say "模式: $mode  匹配前缀: $TaskPath$prefix*$(if ($Tasks.Count) { "  任务类型: $($Tasks -join ',')" })"

if (-not $found.Count) {
    Say '无匹配任务（未安装或已卸载），无事可做。'
    exit 0
}

$failed = 0
foreach ($t in $found) {
    if ($Execute) {
        try {
            Unregister-ScheduledTask -TaskName $t.TaskName -TaskPath $TaskPath -Confirm:$false
            Say ("已卸载: {0}{1}" -f $TaskPath, $t.TaskName)
        } catch {
            Say ("卸载失败: {0} —— {1}（需管理员 PowerShell）" -f $t.TaskName, $_.Exception.Message)
            $failed++
        }
    } else {
        Say ("将卸载: {0}{1}  （State={2}）" -f $TaskPath, $t.TaskName, $t.State)
    }
}

if (-not $Execute) {
    Say ("演练完成：以上 {0} 个任务一个都没卸。核对无误后加 -Execute 真卸载。" -f $found.Count)
    exit 0
}
if ($failed) { Say "卸载完成但有 $failed 个失败"; exit 1 }
Say ("全部卸载完成（{0} 个）。空的 \Boundless\ 文件夹留着无害（Windows 不提供 cmdlet 删任务文件夹）。" -f $found.Count)
exit 0
