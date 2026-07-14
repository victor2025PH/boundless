# 社群客服拓展一键 smoke test — 验证 #119/#120/#121/#122 治本是否生效
#
# 解决 4-27 凌晨真机重试时的 5 个手动步骤散落问题:
#   1. 不知道手机端 VPN 真不真通 (host curl ipinfo.io 无意义, SIM 走自己的网络)
#   2. 派任务前忘了 force-stop FB+Messenger, 上次 task 残留状态干扰
#   3. 派任务靠手敲 curl + JSON, 容易拼错 task_type / params
#   4. 监控只能 grep 日志, 看不见 dashboard 实时步骤 (#122 merge 后才有)
#   5. fail 时手动找 logs/screenshots/task_*.png 截图分析
#
# 用法:
#   circle_prospect_smoke.ps1 -DeviceId 4HUSIB4T                # 默认 jp 婚活
#   circle_prospect_smoke.ps1 -DeviceId IJ8HZLOR -DryRun        # 只验 VPN + force-stop
#   circle_prospect_smoke.ps1 -DeviceId XXX -GroupName "ママ友" -PersonaKey jp_female_midlife
#
# 依赖:
#   - 后端在跑 (默认 127.0.0.1:8000)
#   - $env:OPENCLAW_API_KEY 已设 (如果后端启用了鉴权)
#   - adb 在 PATH
#
# 退出码:
#   0 = 任务 completed
#   1 = VPN 检查失败 (no_ip / leak / 国家不匹配)
#   2 = adb 错误 (设备 offline)
#   3 = 任务派发失败 (HTTP 4xx/5xx)
#   4 = 任务最终 status=failed
#   5 = 超时 (-MaxWaitSeconds)

param(
    [Parameter(Mandatory=$true)][string]$DeviceId,
    [string]$GroupName        = "婚活アラフォー",
    [string]$PersonaKey       = "jp_female_midlife",
    [string]$ApiHost          = "127.0.0.1:8000",
    [string]$ApiKey           = $env:OPENCLAW_API_KEY,
    [int]$PollIntervalSec     = 5,
    [int]$MaxWaitSeconds      = 600,
    [switch]$DryRun,
    [switch]$SkipVpnCheck
)

$ErrorActionPreference = 'Continue'
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)

function Write-Step {
    param([int]$Step, [int]$Total, [string]$Msg)
    Write-Host ""
    Write-Host "[$Step/$Total] $Msg" -ForegroundColor Cyan
}

function Invoke-Api {
    param([string]$Method, [string]$Path, $Body = $null)
    $url = "http://$ApiHost$Path"
    $headers = @{}
    if ($ApiKey) { $headers["X-API-Key"] = $ApiKey }
    $params = @{ Uri = $url; Method = $Method; Headers = $headers; TimeoutSec = 15 }
    if ($Body) {
        $params.Body = ($Body | ConvertTo-Json -Depth 8 -Compress)
        $params.ContentType = "application/json"
    }
    Invoke-RestMethod @params
}

# ── [1/4] VPN 真通验证 ─────────────────────────────────
$total = if ($DryRun) { 2 } else { 4 }

if ($SkipVpnCheck) {
    Write-Step 1 $total "VPN 检查 — 已跳过 (-SkipVpnCheck)"
} else {
    Write-Step 1 $total "VPN 真通验证 — POST /proxy/health/$DeviceId/check"
    try {
        $health = Invoke-Api POST "/proxy/health/$DeviceId/check"
    } catch {
        Write-Host "  ✗ 后端调用失败: $($_.Exception.Message)" -ForegroundColor Red
        Write-Host "  提示: 后端在跑吗? curl http://$ApiHost/health 测试" -ForegroundColor Yellow
        exit 1
    }
    $state = $health.state
    Write-Host "  state            : $state" -ForegroundColor $(if ($state -eq 'ok') { 'Green' } else { 'Red' })
    Write-Host "  expected_ip      : $($health.expected_ip)"
    Write-Host "  actual_ip        : $($health.actual_ip)"
    Write-Host "  ip_match         : $($health.ip_match)"
    Write-Host "  consecutive_fails: $($health.consecutive_fails)"
    if ($health.error) { Write-Host "  error            : $($health.error)" -ForegroundColor Red }
    if ($state -ne 'ok' -or -not $health.ip_match) {
        Write-Host "  ✗ VPN 验证不通过. 4-27 教训: VPN client 显 connected ≠ 流量真通." -ForegroundColor Red
        Write-Host "    建议: 重启 GL.iNet 路由器 / 切节点 / 重连 VPN, 然后 -SkipVpnCheck 跳过这步重试." -ForegroundColor Yellow
        exit 1
    }
    Write-Host "  ✓ VPN 真通, IP=$($health.actual_ip)" -ForegroundColor Green
}

# ── [2/4] Force-stop FB + Messenger ───────────────────
Write-Step 2 $total "force-stop FB + Messenger (复位上次 task 残留状态)"
foreach ($pkg in @("com.facebook.katana", "com.facebook.orca")) {
    $r = & adb -s $DeviceId shell am force-stop $pkg 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  ✗ adb force-stop $pkg 失败: $r" -ForegroundColor Red
        Write-Host "    提示: adb devices 看 $DeviceId 在线吗?" -ForegroundColor Yellow
        exit 2
    }
    Write-Host "  ✓ killed $pkg" -ForegroundColor Green
}
Start-Sleep -Seconds 1

if ($DryRun) {
    Write-Host ""
    Write-Host "[DryRun] 跳过派任务 + 监控. 环境验证通过, 可去掉 -DryRun 真跑." -ForegroundColor Yellow
    exit 0
}

# ── [3/4] 派 facebook_join_group 任务 ─────────────────
Write-Step 3 $total "派 facebook_join_group(group=$GroupName, persona=$PersonaKey)"
$body = @{
    type        = "facebook_join_group"
    device_id   = $DeviceId
    params      = @{ group_name = $GroupName; persona_key = $PersonaKey }
    priority    = 50
    created_via = "ops_smoke_circle_prospect"
    run_on_host = $true
}
try {
    $task = Invoke-Api POST "/tasks" $body
} catch {
    Write-Host "  ✗ 派发失败: $($_.Exception.Message)" -ForegroundColor Red
    if ($_.ErrorDetails.Message) { Write-Host "    detail: $($_.ErrorDetails.Message)" -ForegroundColor Red }
    exit 3
}
$taskId = $task.task_id
Write-Host "  ✓ task_id = $taskId" -ForegroundColor Green
Write-Host "  详情 API:  http://$ApiHost/tasks/$taskId" -ForegroundColor Gray
Write-Host "  Dashboard: http://$ApiHost/dashboard  (找最近任务列表里这条)" -ForegroundColor Gray

# ── [4/4] Poll 直到终态 ───────────────────────────────
Write-Step 4 $total "监控 task 步骤 (#122 current_step 字段, 每 ${PollIntervalSec}s 刷新)"
$start = Get-Date
$lastStep = ""
while ($true) {
    Start-Sleep -Seconds $PollIntervalSec
    $elapsed = [int]((Get-Date) - $start).TotalSeconds
    if ($elapsed -ge $MaxWaitSeconds) {
        Write-Host ""
        Write-Host "  ✗ 超时 ($MaxWaitSeconds s 内未到终态). 当前 status=$($t.status)" -ForegroundColor Red
        exit 5
    }
    try {
        $t = Invoke-Api GET "/tasks/$taskId"
    } catch {
        Write-Host "  poll 失败 (重试): $($_.Exception.Message)" -ForegroundColor DarkYellow
        continue
    }
    $step = $t.current_step
    $sub  = $t.current_sub_step
    $stepLabel = if ($step) { "$step$(if ($sub) { ' — ' + $sub })" } else { "(尚未上报步骤)" }
    if ($stepLabel -ne $lastStep) {
        Write-Host ("  [{0,4}s] status={1,-10} step={2}" -f $elapsed, $t.status, $stepLabel)
        $lastStep = $stepLabel
    }
    if ($t.status -in @("completed", "done")) {
        Write-Host ""
        Write-Host "  ✓ 任务成功完成 ($elapsed s)" -ForegroundColor Green
        exit 0
    }
    if ($t.status -in @("failed", "cancelled", "aborted")) {
        Write-Host ""
        Write-Host "  ✗ 任务终态 status=$($t.status)" -ForegroundColor Red
        if ($t.last_error) { Write-Host "    last_error: $($t.last_error)" -ForegroundColor Red }
        $shotPath = Join-Path $ProjectRoot "logs/screenshots/task_$taskId.png"
        if (Test-Path $shotPath) {
            Write-Host "    截图: $shotPath" -ForegroundColor Yellow
        } else {
            Write-Host "    截图未找到 (期望: $shotPath)" -ForegroundColor DarkYellow
        }
        exit 4
    }
}
