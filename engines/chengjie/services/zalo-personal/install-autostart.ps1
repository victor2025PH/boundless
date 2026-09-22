# install-autostart.ps1 - register a scheduled task to auto-start the Zalo personal service at logon.
# Usage (current user, no admin needed):
#   powershell -ExecutionPolicy Bypass -File install-autostart.ps1 [-DataDir <instance data root>]
# Uninstall: Unregister-ScheduledTask -TaskName "Zalo-Personal-Service" -Confirm:$false
#
# 与 services/messenger-web/install-autostart.ps1 同构（同一套任务形状，便于统一运维）。
# WHY：手工起的边车活不过一次关机——坐席点接入只会看到「连接服务未运行」。注册计划任务
#   使其开机常驻 + 崩溃自恢复，并把 AITR_DATA_DIR 钉到实例数据根（否则回落引擎根 token → 入站 401 丢）。
# 会话形状：zca-js 的二维码在工作台 UI 内展示（无服务器端交互窗），默认 S4U + 开机触发（无人登录
#   也起）；-Interactive 装回旧形状（登录触发），与 messenger/baileys 同一套开关。

param(
    [string]$DataDir = "D:\chengjie-instances\zhiliao\data",
    # 默认 S4U + 开机触发（无人登录也起，headed 登录窗在 session 0 不可见，需人工登录时先
    # Stop-ScheduledTask 再在桌面跑 start.ps1）；-Interactive 恢复旧形状（登录触发、随桌面会话）。
    [switch]$Interactive
)

$ErrorActionPreference = "Stop"

$taskName = "Zalo-Personal-Service"
$svcDir   = $PSScriptRoot
$runner   = Join-Path $svcDir "autostart-run.ps1"
$psExe    = (Get-Command powershell.exe).Source

if (-not (Test-Path $runner)) { throw "autostart-run.ps1 not found: $runner" }
if (-not (Test-Path (Join-Path $DataDir "config\config.yaml"))) {
    Write-Host "[install] WARN: $DataDir\config\config.yaml not found - sidecar may fall back to repo token" -ForegroundColor Yellow
}

$arg = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$runner`" -DataDir `"$DataDir`""

$action = New-ScheduledTaskAction -Execute $psExe -Argument $arg -WorkingDirectory $svcDir

$logonTrigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
if ($Interactive) {
    $triggers = @($logonTrigger)
} else {
    # 开机后 1 分钟起（等网络栈/DNS；autostart-run 内还有 120s 探网）+ 登录也触发（已在跑则 IgnoreNew）
    $bootTrigger = New-ScheduledTaskTrigger -AtStartup
    $bootTrigger.Delay = 'PT1M'
    $triggers = @($bootTrigger, $logonTrigger)
}

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero)

$principal = New-ScheduledTaskPrincipal `
    -UserId ([Security.Principal.WindowsIdentity]::GetCurrent().Name) `
    -LogonType $(if ($Interactive) { 'Interactive' } else { 'S4U' }) `
    -RunLevel Limited

if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
}

Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger $triggers `
    -Settings $settings `
    -Principal $principal `
    -Description "Zalo personal (zca-js) microservice: auto-start at logon (AITR_DATA_DIR=$DataDir), restart on crash, long-lived." | Out-Null

Write-Host "[install] Registered scheduled task: $taskName"
Write-Host "[install]   svcDir  = $svcDir"
Write-Host "[install]   DataDir = $DataDir (AITR_DATA_DIR injected)"
$trigDesc = if ($Interactive) { "AtLogOn ($env:USERNAME), Interactive" } else { "AtStartup+PT1M & AtLogOn ($env:USERNAME), S4U" }
Write-Host "[install]   trigger = $trigDesc, hidden, restart-on-crash x3"
Write-Host "[install] Note: server.js has an EADDRINUSE guard; if a sidecar is already on :8792 the task's node exits immediately (harmless)."
