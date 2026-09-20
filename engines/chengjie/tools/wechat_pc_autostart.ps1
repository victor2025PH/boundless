# wechat_pc_autostart.ps1 — 把「个人微信 PC 副驾」注册为当前用户登录后自启的计划任务（实施97 线 B）
# 用法（引擎根目录，普通权限即可，任务注册在当前用户下）：
#   powershell -ExecutionPolicy Bypass -File tools\wechat_pc_autostart.ps1 -Install -Tier semi
#   powershell -ExecutionPolicy Bypass -File tools\wechat_pc_autostart.ps1 -Status
#   powershell -ExecutionPolicy Bypass -File tools\wechat_pc_autostart.ps1 -Uninstall
# 行为：登录后延迟 90 秒（等微信自己起来并由主人扫码/自动登录），以隐藏窗口运行
#   tools\wechat_pc_devlink.ps1 -Tier <Tier> [...]，进程退出后每 2 分钟自动拉起（最多无限次）。
#   副驾自身在微信未登录/窗口不在时只做巡检不发送，所以早起无害。
# 安全：默认 copilot 只读档；auto_reply 需同时给 -RiskAck。卸载只删任务，不碰数据。
[CmdletBinding()]
param(
    [switch]$Install,
    [switch]$Uninstall,
    [switch]$Status,
    [ValidateSet('', 'copilot', 'semi', 'auto_reply')]
    [string]$Tier = '',            # 空 = 以 ConfigFile 的 platform_login.wechat_pc 为准（引导页改档热生效）
    [switch]$RiskAck,
    [string]$BackendUrl = 'http://127.0.0.1:18898',
    [string]$TokenFile  = 'D:\wxpc_dev\TOKEN.txt',
    [string]$AccountId  = 'wxpc-dev-1',
    [string]$ConfigFile = 'D:\wxpc_dev\data\config\config.local.yaml',
    [string]$StateDir   = 'D:\wxpc_dev\state',
    [int]$DelaySec      = 90,
    [string]$TaskName   = 'ChatX WeChat PC Copilot'
)
$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}
$engine = Split-Path -Parent $PSScriptRoot
$devlink = Join-Path $engine 'tools\wechat_pc_devlink.ps1'

if ($Status -or (-not $Install -and -not $Uninstall)) {
    $t = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if (-not $t) { Write-Host "[autostart] 未注册（$TaskName）"; exit 0 }
    $info = $t | Get-ScheduledTaskInfo
    Write-Host ("[autostart] {0}  状态={1}  上次运行={2}  上次结果={3}  下次={4}" -f $TaskName, $t.State, $info.LastRunTime, $info.LastTaskResult, $info.NextRunTime)
    Write-Host ("[autostart] 动作: {0} {1}" -f $t.Actions[0].Execute, $t.Actions[0].Arguments)
    exit 0
}

if ($Uninstall) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "[autostart] 已删除计划任务 $TaskName（副驾进程若在跑不受影响，需手动结束）"
    } else {
        Write-Host "[autostart] 未注册，无需卸载"
    }
    exit 0
}

# ── Install ──
if (-not (Test-Path $devlink)) { Write-Host "[autostart] 找不到 $devlink" -ForegroundColor Red; exit 1 }
if ($Tier -eq 'auto_reply' -and -not $RiskAck) {
    Write-Host '[autostart] auto_reply 档必须同时给 -RiskAck（确认已知个人微信自动化的封号风险）' -ForegroundColor Yellow
    exit 1
}
$argList = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-WindowStyle', 'Hidden', '-File', "`"$devlink`"",
             '-BackendUrl', $BackendUrl, '-TokenFile', "`"$TokenFile`"", '-AccountId', $AccountId,
             '-ConfigFile', "`"$ConfigFile`"", '-StateDir', "`"$StateDir`"")
if ($Tier) { $argList += @('-Tier', $Tier) }
if ($RiskAck) { $argList += '-RiskAck' }
$tierShown = if ($Tier) { $Tier } else { 'config' }
$action  = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument ($argList -join ' ') -WorkingDirectory $engine
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$trigger.Delay = 'PT{0}S' -f [Math]::Max(0, $DelaySec)
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 2) `
    -ExecutionTimeLimit (New-TimeSpan -Days 0) -MultipleInstances IgnoreNew
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Description "智聊 · 个人微信 PC 副驾（tier=$tierShown）登录后自启；进程退出 2 分钟内自动拉起" | Out-Null
Write-Host ("[autostart] 已注册 {0}：登录后 {1}s 启动 tier={2}，退出自动拉起。立即试跑：Start-ScheduledTask -TaskName '{0}'" -f $TaskName, $DelaySec, $tierShown)
