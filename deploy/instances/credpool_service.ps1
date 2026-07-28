# credpool_service.ps1 — 中央凭据池常驻服务（启停 / 状态 / 开机自启）
#
# 「转正」的最后一块：池必须活过重启，否则机器一重启，新扫的号就会因为池不可达
# 而回落自带凭据——不会坏，但隔离静默失效，而且没人会发现。
#
# 用法：
#   powershell -ExecutionPolicy Bypass -File deploy\instances\credpool_service.ps1 -Start
#   powershell -ExecutionPolicy Bypass -File deploy\instances\credpool_service.ps1 -Stop
#   powershell -ExecutionPolicy Bypass -File deploy\instances\credpool_service.ps1 -Status
#   powershell -ExecutionPolicy Bypass -File deploy\instances\credpool_service.ps1 -InstallTask   # 注册开机自启
#   powershell -ExecutionPolicy Bypass -File deploy\instances\credpool_service.ps1 -RemoveTask

[CmdletBinding()]
param(
    [switch]$Start,
    [switch]$Stop,
    [switch]$Restart,
    [switch]$Status,
    [switch]$InstallTask,
    [switch]$RemoveTask,
    [int]$Port = 8000
)

$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}

$TaskName = 'CredPoolService'
$Script   = Join-Path $PSScriptRoot 'credpool_stage.py'
$LogDir   = 'D:\chengjie-instances\.ops\credpool\logs'
# 经计划任务启动时 stdout 会被 Task Scheduler 丢掉 → 必须自己重定向到固定文件，
# 否则「日报班有没有在跑 / 池启动时说了什么」下次出事查无对证（本仓 EmotionTTS
# 那次事故的核心教训就是「服务端无文件日志，第一现场全失」）。
$SvcLog   = Join-Path $LogDir 'credpool-service.log'
$LogMaxMB = 5
$Python   = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $Python) { throw '找不到 python' }

function Rotate-SvcLog {
    if ((Test-Path $SvcLog) -and ((Get-Item $SvcLog).Length -gt ($LogMaxMB * 1MB))) {
        Move-Item -Force $SvcLog "$SvcLog.1"
    }
}

function Get-PoolPids {
    # 按端口取监听进程，并核对命令行确为本池（端口非通用，误伤概率低）
    $pids = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty OwningProcess -Unique)
    @($pids | Where-Object {
        $p = Get-CimInstance Win32_Process -Filter "ProcessId=$_" -ErrorAction SilentlyContinue
        (-not $p) -or (-not $p.CommandLine) -or ($p.CommandLine -like '*credpool_stage.py*')
    })
}

function Show-Status {
    $pids = Get-PoolPids
    if ($pids.Count) {
        Write-Host "[credpool] 运行中 PID=$($pids -join ',') 端口=$Port" -ForegroundColor Green
    } else {
        Write-Host "[credpool] 未运行（端口 $Port 无监听）" -ForegroundColor Yellow
    }
    try {
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/api/health" -UseBasicParsing -TimeoutSec 5
        Write-Host "[credpool] health: $($r.Content)"
    } catch {
        Write-Host "[credpool] health 不可达：$($_.Exception.Message)" -ForegroundColor Yellow
    }
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($task) {
        Write-Host "[credpool] 开机自启：已注册（$($task.State)）"
    } else {
        Write-Host "[credpool] 开机自启：未注册（跑 -InstallTask）" -ForegroundColor Yellow
    }
}

if ($Stop -or $Restart) {
    $pids = Get-PoolPids
    if (-not $pids.Count) {
        Write-Host '[credpool] 未在运行'
        if (-not $Restart) { exit 0 }
    }
    $pids | ForEach-Object { Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Seconds 2
    if ($pids.Count) { Write-Host "[credpool] 已停止 PID=$($pids -join ',')" }
    if (-not $Restart) { exit 0 }
    $Start = $true   # 落到下面的 -Start 分支重新拉起
}

if ($RemoveTask) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "[credpool] 已移除计划任务 $TaskName"
    exit 0
}

if ($InstallTask) {
    New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
    # ONSTART 只保开机；日常崩溃由 -Start 手动或看门狗补（与本机其它服务同款做法）
    # 经 cmd /c 起是为了 `>>` 重定向（见 $SvcLog 注释）；外层再包一对引号是 cmd 的
    # 老规矩——首个 token 带引号时不这么写会被拆错。
    # PYTHONIOENCODING 必须钉住：cmd 默认代码页会把中文启动日志写成乱码，
    # 而这份日志正是出事时的第一现场（首轮实测就写出了一堆问号）。
    $inner = '"set PYTHONIOENCODING=utf-8&& "{0}" -u "{1}" serve --port {2} >> "{3}" 2>&1"' `
        -f $Python, $Script, $Port, $SvcLog
    $action = New-ScheduledTaskAction -Execute $env:ComSpec `
        -Argument ('/c ' + $inner) `
        -WorkingDirectory (Split-Path -Parent $PSScriptRoot)
    $trigger = New-ScheduledTaskTrigger -AtStartup
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries -StartWhenAvailable `
        -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 2)
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
        -Settings $settings -RunLevel Highest -Force | Out-Null
    Write-Host "[credpool] 已注册开机自启：$TaskName（开机后自动 serve --port $Port）"
    Show-Status
    exit 0
}

if ($Start) {
    if ((Get-PoolPids).Count) { Write-Host '[credpool] 已在运行'; Show-Status; exit 0 }
    New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
    $out = Join-Path $LogDir ("credpool-{0}.log" -f (Get-Date -Format 'yyyyMMdd'))
    # 优先经计划任务拉起：任务跑在 Task Scheduler 服务下，**完全脱离本脚本的进程树**。
    # 为什么讲究这个（已绊倒两次：看门狗首次演练、2026-07-27 手动重启）：Start-Process
    # 起来的池是本脚本的孙进程，任何**捕获本脚本输出**的调用方（管道、subprocess
    # PIPE、CI）都要等整棵树退出才拿到 EOF —— 而池是常驻的，永远不退，于是调用方
    # 自己挂死（连超时都救不了：kill 子进程后仍卡在同一个管道上）。经计划任务起就
    # 没有这棵树，`-Restart` 从任何地方调都安全。任务没注册时回落 Start-Process。
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($task) {
        Rotate-SvcLog
        # 先 /end：上一轮可能留着「任务实例在跑但端口没起来」的僵尸，
        # 默认 MultipleInstances=IgnoreNew 会让 /run 被静默忽略。
        cmd /c "schtasks /end /tn `"$TaskName`" >nul 2>&1"
        schtasks /run /tn $TaskName | Out-Null
        $out = $SvcLog
        Write-Host "[credpool] 经计划任务 $TaskName 拉起（脱离本进程树）"
    } else {
        Start-Process -FilePath $Python -ArgumentList @('-u', $Script, 'serve', '--port', $Port) `
            -WorkingDirectory (Split-Path -Parent $PSScriptRoot) `
            -RedirectStandardOutput $out -RedirectStandardError "$out.err" `
            -WindowStyle Hidden
        Write-Host '[credpool] 计划任务未注册，已就地拉起（建议跑 -InstallTask）' -ForegroundColor Yellow
    }
    Start-Sleep -Seconds 8
    Show-Status
    Write-Host "[credpool] 日志：$out"
    exit 0
}

Show-Status
