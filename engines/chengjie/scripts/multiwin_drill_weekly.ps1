# multiwin_drill_weekly.ps1 — 多开治理·防双发防线的周期性活体验证
#
# 为什么要周批：三层防线（草稿处置原子闸门 / 发送幂等 / 人工通过真投递）的最后一环
# 只有**真发**才能证伪，而 pytest 按纪律不依赖常驻服务、gate_sweep 也不该真发消息。
# 于是与 TranslationEvalWeekly / AvatarPrerenderNightly 同族：低峰跑一次真发演练，
# 让「幂等真拦住了吗、并发闸门还在吗、人工通过真投递了吗」有周期性证据而不是只在
# 人工想起来时才验一次。
#
# 目标账号 = 该账号自己的 **Saved Messages**（chat_key='me'），客户永不可见；
# 演练脚本自带四道护栏（非 'me' 需显式 --allow-peer / 无 --confirm 只预检 /
# 判定以 prometheus 增量为权威 / 断言按本轮 TAG 限定），见 tools/live_multiwin_drill.py。
#
# 注册计划任务（周六 07:10，避开 TranslationEvalWeekly 的 06:30 与夜间渲染 04:30）：
#   schtasks /Create /TN MultiwinDrillWeekly /SC WEEKLY /D SAT /ST 07:10 /F ^
#     /TR "powershell -ExecutionPolicy Bypass -File D:\boundless\engines\chengjie\scripts\multiwin_drill_weekly.ps1"
# 查看 / 手动触发 / 删除：
#   schtasks /Query /TN MultiwinDrillWeekly /V /FO LIST
#   schtasks /Run   /TN MultiwinDrillWeekly
#   schtasks /Delete /TN MultiwinDrillWeekly /F
#
# 参数：
#   -Base        实例地址（默认 http://127.0.0.1:18799）
#   -DataRoot    实例数据根（读 web_admin.auth_token；默认智聊实例）
#   -Account     指定 telegram 账号（默认取首个在线协议号）
#   -DryRun      只跑只读预检（不发任何消息），用于验证接线
#
# 退出码：0=演练全过 / 非 0=有断言失败（便于计划任务或外部监控告警）。
# 实例不在线时**不算失败**（exit 0）：夜间实例可能正在重启窗口内，缺一周数据点可见即可，
# 不该把「服务当时没起」变成红色告警噪声（与 regression.ps1 的 ui-regress 同哲学）。

param(
    [string]$Base = "http://127.0.0.1:18799",
    [string]$DataRoot = "D:\chengjie-instances\zhiliao\data",
    [string]$Account = "",
    [switch]$DryRun
)

$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

# 日志编码统一 UTF-8：PowerShell 5 的 *>> 会把中文输出混写成 UTF-16 乱码
$env:PYTHONIOENCODING = "utf-8"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$logDir = Join-Path $root "logs\multiwin_drill"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$ts  = Get-Date -Format "yyyyMMdd_HHmmss"
$log = Join-Path $logDir "weekly_$ts.log"

"[weekly] start $ts root=$root base=$Base dryRun=$DryRun" | Out-File $log -Encoding utf8

# ── 实例存活探测：不在线 → 跳过（不算失败）──────────────────────────────
$alive = $true
try {
    $null = Invoke-WebRequest -Uri "$Base/login" -Method GET -TimeoutSec 5 -UseBasicParsing
} catch {
    # 任何 HTTP 响应（含 401/403）都代表实例活着；只有连不上才算不在线
    if (-not $_.Exception.Response) { $alive = $false }
}
if (-not $alive) {
    "[weekly] SKIP - instance unreachable at $Base (not a failure; weekly datapoint missing)" |
        Out-File $log -Append -Encoding utf8
    Write-Host "[weekly] SKIP - instance unreachable at $Base" -ForegroundColor Yellow
    exit 0
}

# ── 跑演练 ────────────────────────────────────────────────────────────────
$args = @("-m", "tools.live_multiwin_drill", "--base", $Base, "--data-root", $DataRoot)
if ($Account) { $args += @("--account", $Account) }
if (-not $DryRun) { $args += "--confirm" }   # 不加 --confirm 则只做只读预检

"[weekly] running: python $($args -join ' ')" | Out-File $log -Append -Encoding utf8
python @args 2>&1 | Out-File $log -Append -Encoding utf8
$rc = $LASTEXITCODE
"[weekly] drill exit=$rc" | Out-File $log -Append -Encoding utf8

# ── 日志轮转：保留最近 14 份 ──────────────────────────────────────────────
Get-ChildItem $logDir -Filter "weekly_*.log" | Sort-Object Name -Descending |
    Select-Object -Skip 14 | Remove-Item -Force -ErrorAction SilentlyContinue

if ($rc -eq 0) {
    "[weekly] done - ALL GREEN" | Out-File $log -Append -Encoding utf8
    Write-Host "[weekly] done - ALL GREEN (log: $log)" -ForegroundColor Green
} else {
    # 非 0 = 防双发防线有断言没过：这是真问题，需要人看日志
    "[weekly] done - FAILURES (exit $rc) - 防双发防线有断言未通过，见上方逐项 PASS/FAIL" |
        Out-File $log -Append -Encoding utf8
    Write-Host "[weekly] FAILURES (exit $rc) - see $log" -ForegroundColor Red
}
exit $rc
