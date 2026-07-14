<#
  register_machine_task.ps1 —— 让【本机】自动出现在官网后台「机器」页并保持在线（免管理员）。
  用途：给不运行 Hub / 换脸等"已内置登记"服务的机器（如 STT 分机 140、口型冷备 198）一条命令自登记。
        跑 Hub 或换脸的机器已会自动登记，通常无需再装本任务。

  原理：注册一个"当前用户 + 每 3 小时重复"的计划任务，隐藏运行 admin_client.py 上报本机信息
        （指纹/主机/显卡/显存/版本/档位）。首次安装立即登记一次，之后每 3 小时刷新 last_seen。
        管理通道，绝不上报内容数据。

  用法(项目目录下 PowerShell)：
    powershell -ExecutionPolicy Bypass -File register_machine_task.ps1            # 安装并立即登记
    powershell -ExecutionPolicy Bypass -File register_machine_task.ps1 -Action now      # 立即登记一次(不装任务)
    powershell -ExecutionPolicy Bypass -File register_machine_task.ps1 -Action status
    powershell -ExecutionPolicy Bypass -File register_machine_task.ps1 -Action remove
#>
param(
    [ValidateSet('install', 'remove', 'status', 'now')]
    [string]$Action = 'install'
)

$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}

$TaskName = 'AvatarHub_MachineReg'
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$Bat = Join-Path $Here 'machine_register.bat'
$Vbs = Join-Path $Here 'tools\run_hidden.vbs'
$Log = Join-Path $Here 'logs\machine_register.log'

switch ($Action) {
    'remove' {
        try { Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false; Write-Host "[OK] 已卸载: $TaskName" }
        catch { Write-Host "[!] 未找到任务 $TaskName" }
    }
    'status' {
        $t = Get-ScheduledTask -TaskName $TaskName -EA SilentlyContinue
        if ($t) { $i = Get-ScheduledTaskInfo -TaskName $TaskName; Write-Host "任务: $TaskName  状态: $($t.State)  上次: $($i.LastRunTime)  返回码: $($i.LastTaskResult)" }
        else { Write-Host "[!] 未注册。安装: register_machine_task.ps1" }
        Write-Host "日志: $Log"
    }
    'now' {
        if (-not (Test-Path $Bat)) { throw "找不到 $Bat" }
        Start-Process -FilePath 'wscript.exe' -ArgumentList @('//B', '//Nologo', "`"$Vbs`"", "`"$Bat`"") -WorkingDirectory $Here -Wait
        Write-Host "[OK] 已登记一次，看日志: $Log"
    }
    default {
        if (-not (Test-Path $Bat)) { throw "找不到 $Bat（请在项目目录下运行）" }
        if (-not (Test-Path $Vbs)) { throw "找不到 $Vbs" }
        $act = New-ScheduledTaskAction -Execute 'wscript.exe' `
            -Argument ('//B //Nologo "{0}" "{1}"' -f $Vbs, $Bat) -WorkingDirectory $Here
        # 立即起 + 每 3 小时重复，近似无限；单次登记很快即结束（非常驻）
        $trg = New-ScheduledTaskTrigger -Once -At (Get-Date) `
            -RepetitionInterval (New-TimeSpan -Hours 3) -RepetitionDuration (New-TimeSpan -Days 3650)
        $set = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew `
            -ExecutionTimeLimit (New-TimeSpan -Minutes 3)
        Register-ScheduledTask -TaskName $TaskName -Action $act -Trigger $trg -Settings $set `
            -Description "AvatarHub 机器登记：每 3 小时向官网后台上报本机信息（指纹/显卡/版本），保持后台机器名单在线。" `
            -Force | Out-Null
        Start-ScheduledTask -TaskName $TaskName
        Write-Host "[OK] 已注册并立即登记本机: $TaskName（每 3 小时刷新一次）"
        Write-Host "     状态: register_machine_task.ps1 -Action status   日志: $Log"
    }
}
