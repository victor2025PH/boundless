# install-autostart.ps1 - register a scheduled task to auto-start the Messenger web service at logon.
# Usage (current user, no admin needed):
#   powershell -ExecutionPolicy Bypass -File install-autostart.ps1 [-DataDir <instance data root>]
# Uninstall: Unregister-ScheduledTask -TaskName "Messenger-Web-Service" -Confirm:$false
#
# 与 services/whatsapp-baileys/install-autostart.ps1 同构（同一套任务形状，便于统一运维）。
#
# WHY（2026-07-26）：此前 Messenger 侧**没有任何常驻机制**——WhatsApp 有 Service 任务 + 看门狗，
#   Messenger 一个都没有。加上 start.ps1 的迁仓失效路径（已修），这条链路实际从未真正在线过：
#   手工起过也活不过一次关机，坐席点接入只会看到「连接服务未运行」。
#
# 会话形状：默认 S4U + 开机触发（生产机无自动登录，断电/更新重启后无人登录也要起；restore 本就
#   headless，稳态无窗口）。代价：headed 登录（MSG_HEADLESS=0）的 Chromium 落在 session 0 看不见，
#   需人工过 Facebook 账密/2FA 时 Stop-ScheduledTask 后在桌面手跑 start.ps1，完事再 Start-ScheduledTask；
#   或用 -Interactive 装回旧形状（登录触发、随桌面会话）。

param(
    [string]$DataDir = "D:\chengjie-instances\zhiliao\data",
    # 默认 S4U + 开机触发（无人登录也起，headed 登录窗在 session 0 不可见，需人工登录时先
    # Stop-ScheduledTask 再在桌面跑 start.ps1）；-Interactive 恢复旧形状（登录触发、随桌面会话）。
    [switch]$Interactive
)

$ErrorActionPreference = "Stop"

$taskName = "Messenger-Web-Service"
$svcDir   = $PSScriptRoot
$runner   = Join-Path $svcDir "autostart-run.ps1"
$psExe    = (Get-Command powershell.exe).Source

if (-not (Test-Path $runner)) { throw "autostart-run.ps1 not found: $runner" }
if (-not (Test-Path (Join-Path $DataDir "config\config.yaml"))) {
    Write-Host "[install] WARN: $DataDir\config\config.yaml not found - sidecar may fall back to repo token" -ForegroundColor Yellow
}

$arg = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$runner`" -DataDir `"$DataDir`""

$action = New-ScheduledTaskAction -Execute $psExe -Argument $arg -WorkingDirectory $svcDir

# Start at logon (current user; normal rights are enough to bind high port 8791 + write user dirs).
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

# Idempotent: unregister first if it already exists.
if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
}

Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger $triggers `
    -Settings $settings `
    -Principal $principal `
    -Description "Messenger web (Playwright) microservice: auto-start at logon (AITR_DATA_DIR=$DataDir), restart on crash, long-lived." | Out-Null

Write-Host "[install] Registered scheduled task: $taskName"
Write-Host "[install]   svcDir  = $svcDir"
Write-Host "[install]   DataDir = $DataDir (AITR_DATA_DIR injected)"
$trigDesc = if ($Interactive) { "AtLogOn ($env:USERNAME), Interactive" } else { "AtStartup+PT1M & AtLogOn ($env:USERNAME), S4U" }
Write-Host "[install]   trigger = $trigDesc, hidden, restart-on-crash x3"
Write-Host "[install] Note: server.js has an EADDRINUSE guard; if a sidecar is already on :8791 the task's node exits immediately (harmless)."
