# _interactive_session.ps1 — 会话定位 + 把启动动作弹进交互桌面会话（dot-source 引用）
#
# 背景（2026-09-20 事故）：智聊由 \Boundless\Boundless-chengjie-watchdog（S4U 账户）拉起，
# 落在 session 0；用户的微信在 session 1。Windows 的窗口站按会话隔离，session 0 的
# EnumWindows/UIA 看不到 session 1 的任何窗口——同一段枚举代码在 session 1 找到 14 个
# 微信窗口、在 session 0 找到 0 个。副驾于是恒报「微信未运行」，人却看着屏上的微信，
# 排查方向被带偏了大半天。
#
# 为什么用计划任务当跳板：session 0 里要直接 CreateProcessAsUser 进 session 1，得
# WTSQueryUserToken + SeTcbPrivilege，PowerShell 里做等于手写 P/Invoke 一大坨，且
# S4U 账户未必持有该特权。计划任务的 Interactive 登录类型天然由 Task Scheduler 投递到
# 当前控制台会话，这是 Windows 自己支持的路径，权限只需要能注册任务。
#
# 跳板任务必须是「短命的」：Unregister-ScheduledTask 会连带停掉仍在跑的任务实例，
# 所以任务动作只负责再跑一次启动脚本（脚本内用 Win32_Process.Create 起一个脱离父子
# 关系的引擎进程后立刻退出），任务随即完成、可安全删除，引擎留在 session 1。

Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class BoundlessSession {
    [DllImport("kernel32.dll")] public static extern uint WTSGetActiveConsoleSessionId();
    [DllImport("kernel32.dll")] public static extern uint GetCurrentProcessId();
    [DllImport("kernel32.dll", SetLastError=true)]
    public static extern bool ProcessIdToSessionId(uint dwProcessId, out uint pSessionId);
}
'@ -ErrorAction SilentlyContinue

function Get-CurrentSessionId {
    try {
        $sid = [uint32]0
        if ([BoundlessSession]::ProcessIdToSessionId([BoundlessSession]::GetCurrentProcessId(), [ref]$sid)) {
            return [int]$sid
        }
    } catch {}
    return -1
}

function Get-ConsoleSessionId {
    # 无人登录时 Windows 返回 0xFFFFFFFF —— 归一成 -1，调用方据此报「没有可驱动的桌面」
    try {
        $sid = [BoundlessSession]::WTSGetActiveConsoleSessionId()
        if ($sid -eq [uint32]::MaxValue) { return -1 }
        return [int]$sid
    } catch { return -1 }
}

function Test-SessionIsolated {
    # 仅「有交互会话 ∧ 自己不在其中」才算隔离；无人登录是另一种故障，不在此报
    $eng = Get-CurrentSessionId
    $con = Get-ConsoleSessionId
    return ($eng -ge 0 -and $con -ge 0 -and $eng -ne $con)
}

function Get-InteractiveUser {
    # 控制台会话的登录用户（跳板任务的 Principal）。explorer.exe 的属主最可靠：
    # Win32_ComputerSystem.UserName 在 RDP/切换用户时常为空或给错人。
    $con = Get-ConsoleSessionId
    foreach ($p in @(Get-CimInstance Win32_Process -Filter "Name='explorer.exe'" -ErrorAction SilentlyContinue)) {
        try {
            $o = Invoke-CimMethod -InputObject $p -MethodName GetOwner -ErrorAction Stop
            if ($o.ReturnValue -ne 0 -or -not $o.User) { continue }
            $sess = (Get-Process -Id $p.ProcessId -ErrorAction SilentlyContinue).SessionId
            if ($con -ge 0 -and $sess -ne $con) { continue }
            if ($o.Domain) { return "$($o.Domain)\$($o.User)" }
            return [string]$o.User
        } catch {}
    }
    try { return [string](Get-CimInstance Win32_ComputerSystem).UserName } catch { return '' }
}

function Invoke-InInteractiveSession {
    <#
    .SYNOPSIS
      在当前控制台会话里跑一次 PowerShell 脚本（跳板任务，跑完即删）。
    .OUTPUTS
      @{ ok = [bool]; reason = [string]; rc = [int] }
      reason: no_console_session / no_interactive_user / register_failed / run_failed / timeout
    #>
    param(
        [Parameter(Mandatory = $true)][string]$ScriptPath,
        [string[]]$ArgumentList = @(),
        [Parameter(Mandatory = $true)][string]$TaskName,
        [int]$TimeoutSec = 120
    )
    if ((Get-ConsoleSessionId) -lt 0) {
        return @{ ok = $false; reason = 'no_console_session'; rc = -1 }
    }
    $user = Get-InteractiveUser
    if (-not $user) { return @{ ok = $false; reason = 'no_interactive_user'; rc = -1 } }

    $psExe = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $argv = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-WindowStyle', 'Hidden',
              '-File', $ScriptPath) + $ArgumentList
    # 含空格的参数逐个加引号：任务的 Arguments 是一整串，靠引号还原边界
    $argStr = (($argv | ForEach-Object {
        if ($_ -match '[\s"]') { '"' + ($_ -replace '"', '\"') + '"' } else { $_ }
    }) -join ' ')

    $full = "\Boundless\$TaskName"
    try {
        $action = New-ScheduledTaskAction -Execute $psExe -Argument $argStr `
                    -WorkingDirectory (Split-Path -Parent $ScriptPath)
        $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Highest
        # ExecutionTimeLimit=0 不限时；跳板本身只跑几秒，但别让策略在慢盘上腰斩它
        $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
                        -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero)
        Register-ScheduledTask -TaskPath '\Boundless\' -TaskName $TaskName -Action $action `
            -Principal $principal -Settings $settings -Force -ErrorAction Stop | Out-Null
    } catch {
        return @{ ok = $false; reason = "register_failed: $($_.Exception.Message)"; rc = -1 }
    }

    try {
        Start-ScheduledTask -TaskPath '\Boundless\' -TaskName $TaskName -ErrorAction Stop
    } catch {
        Unregister-ScheduledTask -TaskPath '\Boundless\' -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
        return @{ ok = $false; reason = "run_failed: $($_.Exception.Message)"; rc = -1 }
    }

    $rc = -1
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    $done = $false
    do {
        Start-Sleep -Milliseconds 800
        try {
            $info = Get-ScheduledTaskInfo -TaskPath '\Boundless\' -TaskName $TaskName -ErrorAction Stop
            # 267009 = 仍在运行
            if ([uint32]$info.LastTaskResult -ne 267009) {
                $state = (Get-ScheduledTask -TaskPath '\Boundless\' -TaskName $TaskName).State
                if ($state -ne 'Running') { $rc = [int]$info.LastTaskResult; $done = $true }
            }
        } catch {}
    } while (-not $done -and (Get-Date) -lt $deadline)

    Unregister-ScheduledTask -TaskPath '\Boundless\' -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    if (-not $done) { return @{ ok = $false; reason = 'timeout'; rc = -1 } }
    return @{ ok = ($rc -eq 0); reason = $(if ($rc -eq 0) { '' } else { "exit_$rc" }); rc = $rc }
}
