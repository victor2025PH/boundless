<#
  register_selfcheck_task.ps1 —— 把「全链路自检(带告警)」注册成 Windows 计划任务，实现 7x24 定时巡检。
  比自循环进程更健壮：崩溃由调度器自动重跑、可开机自启、单实例防叠加、每次有超时上限。

  用法(在项目目录下 PowerShell 里)：
    powershell -ExecutionPolicy Bypass -File register_selfcheck_task.ps1                    # 默认每 5 分钟
    powershell -ExecutionPolicy Bypass -File register_selfcheck_task.ps1 -IntervalMinutes 10
    powershell -ExecutionPolicy Bypass -File register_selfcheck_task.ps1 -Action status     # 查看状态/上次结果
    powershell -ExecutionPolicy Bypass -File register_selfcheck_task.ps1 -Action remove      # 卸载

  说明：任务动作 = cmd /c selfcheck_once.bat（内部 call env_config.bat 加载 SVC_*，跑 selfcheck_pipeline.py --alert）。
        红旗(换脸掉CPU/核心离线/阶段偏慢)会经 alerts.py 自动 raise/clear；输出见 logs\selfcheck_last.log。
#>
param(
    [ValidateSet('install', 'remove', 'status')]
    [string]$Action = 'install',
    [int]$IntervalMinutes = 5
)

$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}

$TaskName = 'AvatarHub_SelfCheck'
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$Once = Join-Path $Here 'selfcheck_once.bat'

switch ($Action) {
    'remove' {
        try {
            Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
            Write-Host "[OK] 已卸载定时任务: $TaskName"
        }
        catch {
            Write-Host "[!] 未找到任务 $TaskName（可能本就未注册）"
        }
    }
    'status' {
        $t = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        if (-not $t) {
            Write-Host "[!] 未注册任务 $TaskName。安装: register_selfcheck_task.ps1 (可选 -IntervalMinutes N)"
            return
        }
        $info = Get-ScheduledTaskInfo -TaskName $TaskName
        Write-Host "任务: $TaskName  状态: $($t.State)"
        Write-Host ("上次运行: {0}  返回码: {1}  下次运行: {2}" -f `
                $info.LastRunTime, $info.LastTaskResult, $info.NextRunTime)
        Write-Host "最近一次输出: $(Join-Path $Here 'logs\selfcheck_last.log')"
    }
    default {
        if (-not (Test-Path $Once)) { throw "找不到 $Once（请在项目目录下运行本脚本）" }
        if ($IntervalMinutes -lt 1) { throw "IntervalMinutes 至少 1 分钟" }

        # 动作：wscript(GUI 程序) + tools\run_hidden.vbs 静默拉起 bat——任务动作直接跑
        # cmd/bat 会在交互桌面上闪黑窗(商用机不可接受)；vbs 隐藏执行、等待完成并回传退出码。
        $Vbs = Join-Path $Here 'tools\run_hidden.vbs'
        if (-not (Test-Path $Vbs)) { throw "找不到 $Vbs（需随项目一起分发）" }
        $act = New-ScheduledTaskAction -Execute 'wscript.exe' `
            -Argument ('//B //Nologo "{0}" "{1}"' -f $Vbs, $Once) -WorkingDirectory $Here

        # 触发：立即开始，每 N 分钟重复；重复时长给一个很大的有限值(规避 MaxValue 抛错)= 近似无限
        $trg = New-ScheduledTaskTrigger -Once -At (Get-Date) `
            -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes) `
            -RepetitionDuration (New-TimeSpan -Days 3650)

        # 设置：错过即补跑 / 同名实例不叠加 / 单次最多 10 分钟兜底
        $set = New-ScheduledTaskSettingsSet -StartWhenAvailable `
            -MultipleInstances IgnoreNew `
            -ExecutionTimeLimit (New-TimeSpan -Minutes 10)

        Register-ScheduledTask -TaskName $TaskName -Action $act -Trigger $trg -Settings $set `
            -Description "AvatarHub 全链路自检：每 $IntervalMinutes 分钟一次，红旗自动告警(alerts.py)" `
            -Force | Out-Null

        Write-Host "[OK] 已注册计划任务: $TaskName"
        Write-Host "     频率: 每 $IntervalMinutes 分钟   动作: selfcheck_once.bat (--alert)"
        Write-Host "     查看: register_selfcheck_task.ps1 -Action status"
        Write-Host "     卸载: register_selfcheck_task.ps1 -Action remove"
    }
}
