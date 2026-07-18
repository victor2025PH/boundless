# list_tasks.ps1 — 列出 Boundless 定时任务现状与上次运行结果（deploy/cron，只读巡检）
#
# 用法：powershell -ExecutionPolicy Bypass -File deploy\cron\list_tasks.ps1
# 输出：\Boundless\ 下每个任务的 状态 / 上次运行时间与结果（Get-ScheduledTaskInfo）/
#       下次运行时间 / 错过次数 / 日志路径提示；无任务时优雅输出「未安装」。
# 退出码：恒 0（巡检脚本；失败任务用输出体现，不用退出码）

[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}

$TaskPath = '\Boundless\'
$RepoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$LogDir   = Join-Path $RepoRoot 'logs\cron'

function Decode-Result([int64]$code) {
    switch ($code) {
        0          { return '成功' }
        1          { return '失败（退出码 1：本轮有失败，看日志）' }
        2          { return '失败（退出码 2：配置错误——缺密钥/python/数据根，看日志首行）' }
        3          { return '失败（退出码 3：chengjie 双实例守门，README §3）' }
        267009     { return '正在运行' }                     # 0x41301
        267011     { return '尚未运行过' }                   # 0x41303
        267014     { return '被终止（超时/手动停止）' }       # 0x41306
        2147750687 { return '已有实例在跑，本轮被跳过' }      # 0x800710E0
        default {
            if ($code -ge 0x80000000) { return ('失败 0x{0:X8}（系统错误码）' -f $code) }
            return "失败（退出码 $code）"
        }
    }
}

$tasks = @(Get-ScheduledTask -TaskPath $TaskPath -ErrorAction SilentlyContinue | Sort-Object TaskName)
if (-not $tasks.Count) {
    Write-Host "未安装：计划任务文件夹 $TaskPath 下没有任何任务。"
    Write-Host '安装（先演练后 -Execute）：powershell -ExecutionPolicy Bypass -File deploy\cron\install_tasks.ps1 -Engine <avatarhub|chengjie|huoke>'
    exit 0
}

Write-Host ("Boundless 定时任务现状（{0}，共 {1} 个）" -f $env:COMPUTERNAME, $tasks.Count)
Write-Host ''
foreach ($t in $tasks) {
    $info = $null
    try { $info = $t | Get-ScheduledTaskInfo } catch {}
    Write-Host ("■ {0}{1}" -f $TaskPath, $t.TaskName)
    Write-Host ("    状态       {0}" -f $t.State)
    if ($info) {
        $last = if ($info.LastRunTime -and $info.LastRunTime.Year -gt 2000) {
            $info.LastRunTime.ToString('yyyy-MM-dd HH:mm:ss')
        } else { '—' }
        $next = if ($info.NextRunTime) { $info.NextRunTime.ToString('yyyy-MM-dd HH:mm:ss') } else { '—' }
        Write-Host ("    上次运行   {0}  → {1}" -f $last, (Decode-Result ([int64]$info.LastTaskResult)))
        Write-Host ("    下次运行   {0}    错过次数 {1}" -f $next, $info.NumberOfMissedRuns)
    } else {
        Write-Host '    上次运行   （无法读取 Get-ScheduledTaskInfo）'
    }
    Write-Host ("    日志       {0}" -f (Join-Path $LogDir ($t.TaskName + '.log')))
}
Write-Host ''
Write-Host '提示：失败详情看对应日志尾部；退出码语义见 deploy\cron\README.md §8。'
exit 0
