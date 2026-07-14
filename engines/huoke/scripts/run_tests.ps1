# OpenClaw 端到端测试（需先启动 server.py 且手机已 ADB 连接）
# 在项目根目录执行: .\scripts\run_tests.ps1

$base = "http://127.0.0.1:8000"
$deviceId = "89NZVGKFD6BYUO5P"

Write-Host "=== 1. GET /devices ==="
try {
    $devices = Invoke-RestMethod -Uri "$base/devices" -Method Get -TimeoutSec 10
    $devices | Format-Table -AutoSize
    $connected = $devices | Where-Object { $_.status -eq "connected" }
    if (-not $connected) { Write-Host "警告: 无已连接设备，部分测试可能失败" }
} catch {
    Write-Host "失败: $_"
    exit 1
}

Write-Host "`n=== 2. POST /tasks (主机执行) ==="
$body = @{ type = "telegram_send_message"; device_id = $deviceId; params = @{ username = "@ykj123"; message = "E2E test from script" }; run_on_host = $true } | ConvertTo-Json -Depth 5 -Compress
$task = Invoke-RestMethod -Uri "$base/tasks" -Method Post -Body $body -ContentType "application/json"
$tid = $task.task_id
Write-Host "task_id: $tid"

Write-Host "`n=== 3. 等待 20s 后 GET /tasks/$tid ==="
Start-Sleep -Seconds 20
$t = Invoke-RestMethod -Uri "$base/tasks/$tid" -Method Get
Write-Host "status: $($t.status), result.success: $($t.result.success)"
if ($t.status -eq "completed" -and $t.result.success -eq $true) {
    Write-Host "通过: 主机执行任务成功"
} else {
    Write-Host "未通过: 任务未完成或失败"
}

Write-Host "`n=== 4. GET /tasks?status=pending ==="
$pending = Invoke-RestMethod -Uri "$base/tasks?status=pending" -Method Get
Write-Host "pending 数量: $($pending.Count)"

Write-Host "`n=== 完成 ==="
