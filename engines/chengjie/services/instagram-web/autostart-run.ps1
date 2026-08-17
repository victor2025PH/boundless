# autostart-run.ps1 — 计划任务 Instagram-Web-Service 的启动壳（与 messenger-web 同构）。
# 用 -File 调用而非 -Command 内联，避免隐藏窗口/任务上下文里引号转义脆弱。
# 作用：注入本机在跑实例的数据根（AITR_DATA_DIR，供 start.ps1 解析该实例的入站 token），再调 start.ps1。
# 换实例/换机：改下面 DataDir 默认值，或 install-autostart.ps1 -DataDir 覆盖后重装任务。
param([string]$DataDir = "D:\chengjie-instances\zhiliao\data")
$ErrorActionPreference = "Stop"
$env:AITR_DATA_DIR = $DataDir

# 等网络/DNS 就绪再拉 Node（最多 120s，5s 一探）。开机时任务可能先于网络栈触发，
# 此时 Playwright 导航 instagram.com 会直接 DNS 失败。超时也照常启动（Node 侧自会重试）。
$dnsDeadline = (Get-Date).AddSeconds(120)
while ($true) {
    try {
        [void][System.Net.Dns]::GetHostAddresses("www.instagram.com")
        Write-Host "[ig-autostart] network ready (www.instagram.com resolved)"
        break
    } catch {
        if ((Get-Date) -ge $dnsDeadline) {
            Write-Host "[ig-autostart] WARN: DNS still failing after 120s, starting anyway"
            break
        }
        Start-Sleep -Seconds 5
    }
}

& (Join-Path $PSScriptRoot "start.ps1")
