# install-autostart.ps1 - register a scheduled task to auto-start the Baileys service at logon.
# Usage (current user, no admin needed):
#   powershell -ExecutionPolicy Bypass -File install-autostart.ps1 [-DataDir <instance data root>]
# Uninstall: Unregister-ScheduledTask -TaskName "WhatsApp-Baileys-Service" -Confirm:$false
# Notes: starts start.ps1 hidden at boot (S4U, +1min) and at logon; auto-restart on crash; no run-time limit.
#        Baileys 扫码走工作台 UI，无服务器端窗口，S4U 无副作用；-Interactive 装回旧的登录触发形状。
#        Decoupled from the main app - the main app connects via platform_login.whatsapp.baileys_url.
#
# 2026-07-20 迁仓修复：
#   - $svcDir 从脚本自身位置派生（原硬编码 D:\workspace\telegram-mtproto-ai 已随单仓迁移失效）。
#   - 注入 AITR_DATA_DIR：边车据此解析「真正在跑的实例」的入站 token（overlay 优先），
#     否则回落仓库内 config.yaml 的 token → 与实例 token 不符 → 入站被 401 静默丢弃。
#     默认指向 zhiliao 实例数据根；多实例/换机用 -DataDir 覆盖。

param(
    [string]$DataDir = "D:\chengjie-instances\zhiliao\data",
    # 默认 S4U + 开机触发（无人登录也起，headed 登录窗在 session 0 不可见，需人工登录时先
    # Stop-ScheduledTask 再在桌面跑 start.ps1）；-Interactive 恢复旧形状（登录触发、随桌面会话）。
    [switch]$Interactive
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
    -Description "WhatsApp (Baileys) protocol microservice: auto-start at logon (AITR_DATA_DIR=$DataDir), restart on crash, long-lived." | Out-Null

Write-Host "[install] Registered scheduled task: $taskName"
Write-Host "[install]   svcDir  = $svcDir"
Write-Host "[install]   DataDir = $DataDir (AITR_DATA_DIR injected)"
$trigDesc = if ($Interactive) { "AtLogOn ($env:USERNAME), Interactive" } else { "AtStartup+PT1M & AtLogOn ($env:USERNAME), S4U" }
Write-Host "[install]   trigger = $trigDesc, hidden, restart-on-crash x3"
Write-Host "[install] Note: start.ps1 has no port guard; if a sidecar is already on :8790, the task's node exits on EADDRINUSE (harmless)."
