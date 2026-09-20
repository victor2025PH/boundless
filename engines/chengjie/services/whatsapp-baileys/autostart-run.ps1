# autostart-run.ps1 — 计划任务 WhatsApp-Baileys-Service 的启动壳（用 -File 调用，避免 -Command 内联
#   env + `& '路径'` 的引号/隐藏窗口脆弱性——那正是任务启动失败 0xFFFFFFFF 的诱因之一）。
# 作用：注入本机在跑实例的数据根(AITR_DATA_DIR，供 start.ps1 解析该实例的入站 token)，再调 start.ps1。
# 换实例/换机：改下面 DataDir 默认值，或 install-autostart.ps1 -DataDir 覆盖后重装任务。
param([string]$DataDir = "D:\chengjie-instances\zhiliao\data")
$ErrorActionPreference = "Stop"
$env:AITR_DATA_DIR = $DataDir

# Wait for network/DNS before starting Node (max 120s, poll every 5s).
# WHY: on boot (2026-07-22 09:21) this task fired before the network stack was ready and every
# WhatsApp session failed with DNS ENOTFOUND, leaving the service half-dead until the next
# trigger 9 minutes later. Resolving web.whatsapp.com first avoids that window.
# On timeout we START ANYWAY (never block boot; the Node side retries on its own).
# [System.Net.Dns] is used instead of Resolve-DnsName: always present on PS 5.1, no module dep.
$dnsDeadline = (Get-Date).AddSeconds(120)
while ($true) {
    try {
        [void][System.Net.Dns]::GetHostAddresses("web.whatsapp.com")
        Write-Host "[wa-autostart] network ready (web.whatsapp.com resolved)"
        break
    } catch {
        if ((Get-Date) -ge $dnsDeadline) {
            Write-Host "[wa-autostart] WARN: DNS still failing after 120s, starting anyway"
            break
        }
        Start-Sleep -Seconds 5
    }
}

& (Join-Path $PSScriptRoot "start.ps1")
