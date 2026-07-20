# install-autostart.ps1 - register a scheduled task to auto-start the Baileys service at logon.
# Usage (current user, no admin needed):
#   powershell -ExecutionPolicy Bypass -File install-autostart.ps1 [-DataDir <instance data root>]
# Uninstall: Unregister-ScheduledTask -TaskName "WhatsApp-Baileys-Service" -Confirm:$false
# Notes: starts start.ps1 hidden at logon; auto-restart on crash; no run-time limit (long-lived).
#        Decoupled from the main app - the main app connects via platform_login.whatsapp.baileys_url.
#
# 2026-07-20 迁仓修复：
#   - $svcDir 从脚本自身位置派生（原硬编码 D:\workspace\telegram-mtproto-ai 已随单仓迁移失效）。
#   - 注入 AITR_DATA_DIR：边车据此解析「真正在跑的实例」的入站 token（overlay 优先），
#     否则回落仓库内 config.yaml 的 token → 与实例 token 不符 → 入站被 401 静默丢弃。
#     默认指向 zhiliao 实例数据根；多实例/换机用 -DataDir 覆盖。

param(
    [string]$DataDir = "D:\chengjie-instances\zhiliao\data"
)

$ErrorActionPreference = "Stop"

$taskName = "WhatsApp-Baileys-Service"
$svcDir   = $PSScriptRoot
$runner   = Join-Path $svcDir "autostart-run.ps1"
$psExe    = (Get-Command powershell.exe).Source

if (-not (Test-Path $runner)) { throw "autostart-run.ps1 not found: $runner" }
if (-not (Test-Path (Join-Path $DataDir "config\config.yaml"))) {
    Write-Host "[install] WARN: $DataDir\config\config.yaml not found — sidecar may fall back to repo token" -ForegroundColor Yellow
}

# 用 -File 调专用启动壳 autostart-run.ps1（-DataDir 传实例数据根），比 -Command 内联
# `$env:...; & '路径'` 稳（后者在隐藏窗口/任务上下文的引号转义脆弱 → 曾致任务启动失败）。
$arg = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$runner`" -DataDir `"$DataDir`""

$action = New-ScheduledTaskAction -Execute $psExe -Argument $arg -WorkingDirectory $svcDir

# Start at logon (current user; normal rights are enough to bind high port 8790 + write user dirs).
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
    -Description "WhatsApp (Baileys) protocol microservice: auto-start at logon (AITR_DATA_DIR=$DataDir), restart on crash, long-lived." | Out-Null

Write-Host "[install] Registered scheduled task: $taskName"
Write-Host "[install]   svcDir  = $svcDir"
Write-Host "[install]   DataDir = $DataDir (AITR_DATA_DIR injected)"
Write-Host "[install]   trigger = AtLogOn ($env:USERNAME), hidden, restart-on-crash x3"
Write-Host "[install] Note: start.ps1 has no port guard; if a sidecar is already on :8790, the task's node exits on EADDRINUSE (harmless)."
