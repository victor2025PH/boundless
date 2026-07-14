# 用途：在 Worker 机器本机执行，调用本机 OpenAPI `POST /cluster/pull-update`，从主控拉取与主控同版本的代码包并覆盖（不覆盖 config/）。
# 前置：主控已运行且可访问；本机 OpenClaw 服务已启动；`config` 中已配置 coordinator_url 或本脚本传入主控地址。
# 用法（PowerShell）：
#   $env:OPENCLAW_COORDINATOR_URL = "http://192.168.0.118:8000"   # 改成你的主控内网地址
#   powershell -ExecutionPolicy Bypass -File "D:\mobile-auto-0327\mobile-auto-project\scripts\worker_pull_update.ps1"
# 可选参数：-LocalApiUrl 本机 API 根地址；-CoordinatorUrl 主控根地址（覆盖环境变量）。

param(
    [string]$CoordinatorUrl = $env:OPENCLAW_COORDINATOR_URL,
    [string]$LocalApiUrl = "http://127.0.0.1:8000"
)

$ErrorActionPreference = "Stop"
if (-not $CoordinatorUrl -or $CoordinatorUrl.Trim() -eq "") {
    Write-Host "[ERROR] 未设置主控地址。请先设置环境变量 OPENCLAW_COORDINATOR_URL 或使用 -CoordinatorUrl，例如: http://192.168.0.118:8000" -ForegroundColor Red
    exit 1
}

$CoordinatorUrl = $CoordinatorUrl.TrimEnd("/")
$bodyObj = @{ coordinator_url = $CoordinatorUrl }
$body = $bodyObj | ConvertTo-Json -Compress
$uri = "$LocalApiUrl/cluster/pull-update"

Write-Host "[worker_pull_update] POST $uri" -ForegroundColor Cyan
Write-Host "[worker_pull_update] coordinator_url=$CoordinatorUrl"

try {
    $resp = Invoke-RestMethod -Uri $uri -Method Post -Body $body -ContentType "application/json" -TimeoutSec 120
    Write-Host ($resp | ConvertTo-Json -Depth 5)
    if ($resp.ok) {
        Write-Host "[OK] $($resp.message)" -ForegroundColor Green
        if ($resp.restarting) { Write-Host "服务可能正在自动重启，数秒后再打开探针确认 enriched 为 ✓。" -ForegroundColor Yellow }
    }
    exit 0
} catch {
    Write-Host "[ERROR] $($_.Exception.Message)" -ForegroundColor Red
    if ($_.ErrorDetails.Message) { Write-Host $_.ErrorDetails.Message }
    exit 1
}
