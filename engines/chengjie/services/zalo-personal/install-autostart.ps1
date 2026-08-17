# install-autostart.ps1 - register a scheduled task to auto-start the Zalo personal service at logon.
# Usage (current user, no admin needed):
#   powershell -ExecutionPolicy Bypass -File install-autostart.ps1 [-DataDir <instance data root>]
# Uninstall: Unregister-ScheduledTask -TaskName "Zalo-Personal-Service" -Confirm:$false
#
# 与 services/messenger-web/install-autostart.ps1 同构（同一套任务形状，便于统一运维）。
# WHY：手工起的边车活不过一次关机——坐席点接入只会看到「连接服务未运行」。注册计划任务
#   使其开机常驻 + 崩溃自恢复，并把 AITR_DATA_DIR 钉到实例数据根（否则回落引擎根 token → 入站 401 丢）。
# 注意：zca-js 的二维码在工作台 UI 内展示（无服务器端交互窗），本可用 S4U/开机触发；但为与
#   messenger/baileys 统一运维形状，仍用 AtLogOn + Interactive。

param(
    [string]$DataDir = "D:\chengjie-instances\zhiliao\data"
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

if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
}

Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description "Zalo personal (zca-js) microservice: auto-start at logon (AITR_DATA_DIR=$DataDir), restart on crash, long-lived." | Out-Null

Write-Host "[install] Registered scheduled task: $taskName"
Write-Host "[install]   svcDir  = $svcDir"
Write-Host "[install]   DataDir = $DataDir (AITR_DATA_DIR injected)"
Write-Host "[install]   trigger = AtLogOn ($env:USERNAME), hidden, restart-on-crash x3"
Write-Host "[install] Note: server.js has an EADDRINUSE guard; if a sidecar is already on :8792 the task's node exits immediately (harmless)."
