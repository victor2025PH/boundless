<#
  register_sign_worker_task.ps1 —— 把「本机签发机」注册成 Windows 计划任务，实现 7x24 常驻（免管理员）。
  签发机持私钥、轮询官网签发队列就地签发（路线A/B/CRL）。私钥永不上服务器，这台机器需常驻运行。

  为什么用计划任务（而非启动目录脚本）：可在"任务计划程序"里查看/管理；靠"重复触发 + 单实例(IgnoreNew)"
  自带崩溃自愈（进程死了 ~2 分钟内自动重启），无多余 cmd 循环进程；随可用即启，登录后自动恢复。
  （注册"当前用户+重复触发"任务无需管理员；仅"开机未登录也跑"需管理员，见结尾提示。）

  用法(项目目录下 PowerShell)：
    powershell -ExecutionPolicy Bypass -File register_sign_worker_task.ps1              # 免管理员：登录后随可用即启
    powershell -ExecutionPolicy Bypass -File register_sign_worker_task.ps1 -Boot        # 管理员版：SYSTEM 账户+开机自启(未登录也跑)，会自动弹 UAC 提权
    powershell -ExecutionPolicy Bypass -File register_sign_worker_task.ps1 -Action status
    powershell -ExecutionPolicy Bypass -File register_sign_worker_task.ps1 -Action stop     # 停当前进程(下次触发仍自启)
    powershell -ExecutionPolicy Bypass -File register_sign_worker_task.ps1 -Action remove   # 卸载任务+停进程

  两种模式二选一（同名任务，装哪个是哪个）：
    默认(无 -Boot)：当前用户身份，免管理员；【登录后】随可用即启、崩溃自愈。适合这台机器保持登录。
    -Boot(管理员)：以 SYSTEM 账户 + 开机触发注册；【关机重启后未登录也自动跑】。需管理员(脚本会自动提权)。
#>
param(
    [ValidateSet('install', 'remove', 'status', 'stop')]
    [string]$Action = 'install',
    [switch]$Boot
)

$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}

$TaskName = 'AvatarHub_SignWorker'
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$Bat = Join-Path $Here 'sign_worker_watch.bat'
$Vbs = Join-Path $Here 'tools\run_hidden.vbs'
$Log = Join-Path $Here 'logs\sign_worker.log'
# 迁移：清理上一版遗留的"启动目录 VBS"自启方式
$OldLauncher = Join-Path ([Environment]::GetFolderPath('Startup')) 'AvatarHub_SignWorker.vbs'

function Stop-Procs {
    Get-CimInstance Win32_Process -Filter "Name='cmd.exe'" -EA SilentlyContinue |
        Where-Object { $_.CommandLine -match 'sign_worker_watch' } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -EA SilentlyContinue }
    Start-Sleep -Milliseconds 400
    Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='python3.exe'" -EA SilentlyContinue |
        Where-Object { $_.CommandLine -match 'sign_worker\.py' } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -EA SilentlyContinue }
    Get-CimInstance Win32_Process -Filter "Name='wscript.exe'" -EA SilentlyContinue |
        Where-Object { $_.CommandLine -match 'sign_worker_watch|AvatarHub_SignWorker' } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -EA SilentlyContinue }
}

switch ($Action) {
    'remove' {
        try { Stop-ScheduledTask -TaskName $TaskName -EA SilentlyContinue } catch {}
        try { Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false; Write-Host "[OK] 已卸载任务: $TaskName" }
        catch { Write-Host "[!] 未找到任务 $TaskName" }
        if (Test-Path $OldLauncher) { Remove-Item $OldLauncher -Force -EA SilentlyContinue }
        Stop-Procs
        Write-Host "[OK] 已停止签发机进程"
    }
    'stop' {
        try { Stop-ScheduledTask -TaskName $TaskName -EA SilentlyContinue } catch {}
        Stop-Procs
        Write-Host "[OK] 已停止签发机（任务仍在，下次触发/登录会自启；彻底停用用 -Action remove）"
    }
    'status' {
        $t = Get-ScheduledTask -TaskName $TaskName -EA SilentlyContinue
        if ($t) {
            $info = Get-ScheduledTaskInfo -TaskName $TaskName
            Write-Host "任务: $TaskName  状态: $($t.State)  上次运行: $($info.LastRunTime)  返回码: $($info.LastTaskResult)"
        } else { Write-Host "[!] 任务未注册。安装: register_sign_worker_task.ps1" }
        $py = Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='python3.exe'" -EA SilentlyContinue |
            Where-Object { $_.CommandLine -match 'sign_worker\.py' }
        Write-Host ("签发进程: {0}" -f $(if ($py) { '在  PID=' + ($py.ProcessId -join ',') } else { '否' }))
        Write-Host "日志: $Log"
    }
    default {
        if (-not (Test-Path $Bat)) { throw "找不到 $Bat（请在项目目录下运行）" }
        if (-not (Test-Path $Vbs)) { throw "找不到 $Vbs（需随项目分发）" }

        # 触发：立即起，每 2 分钟重复(死了即自愈)，近似无限时长（两种模式共用）
        $trgRepeat = New-ScheduledTaskTrigger -Once -At (Get-Date) `
            -RepetitionInterval (New-TimeSpan -Minutes 2) -RepetitionDuration (New-TimeSpan -Days 3650)
        # 设置：随可用即启 / 单实例不叠加(在跑就跳过=自愈核心) / 无运行时长上限(--watch 永不结束)
        $set = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero)

        if ($Boot) {
            # 管理员版：SYSTEM 账户 + 开机触发 → 关机重启后未登录也自动跑。需管理员，没有则自动提权重跑。
            $isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltinRole]::Administrator)
            if (-not $isAdmin) {
                Write-Host "[i] -Boot 需要管理员权限，正在弹出 UAC 提权并重跑本脚本…"
                Start-Process -FilePath 'powershell.exe' -Verb RunAs `
                    -ArgumentList ('-NoProfile -ExecutionPolicy Bypass -File "{0}" -Boot' -f $PSCommandPath)
                return
            }
            if (Test-Path $OldLauncher) { Remove-Item $OldLauncher -Force -EA SilentlyContinue }
            Stop-Procs
            # SYSTEM 会话无可见桌面，直接 cmd 跑 bat（输出仍进日志）；无需隐藏窗口
            $act = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument ('/c "{0}"' -f $Bat) -WorkingDirectory $Here
            $trgBoot = New-ScheduledTaskTrigger -AtStartup
            $prin = New-ScheduledTaskPrincipal -UserId 'S-1-5-18' -LogonType ServiceAccount -RunLevel Highest  # S-1-5-18 = LocalSystem
            Register-ScheduledTask -TaskName $TaskName -Action $act -Trigger @($trgBoot, $trgRepeat) `
                -Settings $set -Principal $prin `
                -Description "AvatarHub 本机签发机(管理员/SYSTEM)：开机自启，未登录也运行；崩溃自愈。私钥不上服务器。" `
                -Force | Out-Null
            Start-ScheduledTask -TaskName $TaskName
            Write-Host "[OK] 已注册开机自启常驻任务(SYSTEM 账户): $TaskName"
            Write-Host "     关机重启后【无需登录】自动运行 · 崩溃自愈 · 单实例"
            Write-Host "     状态: register_sign_worker_task.ps1 -Action status   日志: $Log"
        }
        else {
            # 免管理员版：当前用户身份，登录后随可用即启
            if (Test-Path $OldLauncher) { Remove-Item $OldLauncher -Force -EA SilentlyContinue; Write-Host "[i] 已移除上一版启动目录自启" }
            Stop-Procs
            # 动作：wscript 隐藏拉起 bat（无黑窗）
            $act = New-ScheduledTaskAction -Execute 'wscript.exe' `
                -Argument ('//B //Nologo "{0}" "{1}"' -f $Vbs, $Bat) -WorkingDirectory $Here
            Register-ScheduledTask -TaskName $TaskName -Action $act -Trigger $trgRepeat -Settings $set `
                -Description "AvatarHub 本机签发机：轮询官网签发队列，用本地私钥就地签发授权/CRL（私钥不上服务器）。崩溃自愈、随可用即启。" `
                -Force | Out-Null
            Start-ScheduledTask -TaskName $TaskName
            Write-Host "[OK] 已注册并启动常驻计划任务: $TaskName"
            Write-Host "     自愈: 每 2 分钟检查，进程死则自动重启 · 单实例 · 登录后随可用即启"
            Write-Host "     状态: register_sign_worker_task.ps1 -Action status   日志: $Log"
            Write-Host "     升级为『关机重启未登录也跑』：register_sign_worker_task.ps1 -Boot（管理员）"
        }
    }
}
