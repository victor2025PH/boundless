# 用途：在主控本机执行，调用主控 API `POST /cluster/push-update-all`，向所有在线 Worker 推送与主控相同的代码包（OTA），Worker 自动 `pull-update` 并重启。
# 前置：主控 `config/cluster.yaml` 为 `role: coordinator`；主控服务已启动；Worker 能访问主控的 `http://<主控局域网IP>:8000/cluster/update-package`。
# 用法：
#   powershell -ExecutionPolicy Bypass -File "...\scripts\push_worker_update_from_coordinator.ps1"
# 可选：-ApiBase 默认 http://127.0.0.1:8000

param(
    [string]$ApiBase = "http://127.0.0.1:8000"
)

$ErrorActionPreference = "Stop"
$uri = "$($ApiBase.TrimEnd('/'))/cluster/push-update-all"
Write-Host "[push-update-all] POST $uri" -ForegroundColor Cyan
try {
    $resp = Invoke-RestMethod -Uri $uri -Method Post -Body "{}" -ContentType "application/json" -TimeoutSec 300
    Write-Host ($resp | ConvertTo-Json -Depth 6)
    if ($resp.ok) { Write-Host "[OK] $($resp.summary)" -ForegroundColor Green }
    exit 0
} catch {
    Write-Host "[ERROR] $($_.Exception.Message)" -ForegroundColor Red
    if ($_.ErrorDetails.Message) { Write-Host $_.ErrorDetails.Message }
    exit 1
}
