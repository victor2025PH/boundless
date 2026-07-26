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
# 注意：headed 登录（MSG_HEADLESS=0）需要交互式桌面会话，故用 AtLogOn + Interactive，
#   不能改成 S4U/开机触发（那样弹不出登录窗口，人就没法完成 Facebook 账密/2FA）。

param(
    [string]$DataDir = "D:\chengjie-instances\zhiliao\data"
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
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero)

$principal = New-ScheduledTaskPrincipal `
    -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive `
    -RunLevel Limited

# Idempotent: unregister first if it already exists.
if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
}

Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description "Messenger web (Playwright) microservice: auto-start at logon (AITR_DATA_DIR=$DataDir), restart on crash, long-lived." | Out-Null

Write-Host "[install] Registered scheduled task: $taskName"
Write-Host "[install]   svcDir  = $svcDir"
Write-Host "[install]   DataDir = $DataDir (AITR_DATA_DIR injected)"
Write-Host "[install]   trigger = AtLogOn ($env:USERNAME), hidden, restart-on-crash x3"
Write-Host "[install] Note: server.js has an EADDRINUSE guard; if a sidecar is already on :8791 the task's node exits immediately (harmless)."
