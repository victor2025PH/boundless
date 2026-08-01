# P23 激活兜底（2026-08-01，一次性）：确保智聊在低峰装载已落盘的 .py 批次。
#
# 背景：共享树多 agent 线连续施工，白天没有安全重启窗口（装载别人写一半的
# 启动路径 = 全站风险）。本脚本由一次性计划任务 EnsureP23Restart（每日 04:15，
# 成功即自删）驱动，三重护栏：
#   1. 搭车检测：若 2026-08-01 12:00 后已有任何一次 zhiliao 重启（共享代码根 →
#      任何重启都装载全部落盘代码），说明目的已达成 → 跳过 + 自删任务。
#   2. 编译闸门：全树脏 .py（排除删除态）逐个 py_compile，任何语法坏 →
#      本夜不重启（保留任务明夜再试），宁可晚生效不装坏代码。
#   3. 重启走唯一入口 restart_instance.ps1（自带冷却/脏树闸门/等 /login/告警）。
# 日志：logs\goals\ensure_restart.log（UTF-8）。
$ErrorActionPreference = "Continue"
$engine = Split-Path -Parent $PSScriptRoot
$repo = Split-Path -Parent (Split-Path -Parent $engine)   # engines/chengjie → 仓库根
Set-Location $engine
$env:PYTHONIOENCODING = "utf-8"
$logDir = Join-Path $engine "logs\goals"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir "ensure_restart.log"
function _log($m) {
    ("[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $m) |
        Out-File -Append -Encoding utf8 $log
}
function _selfdelete() {
    schtasks /Delete /TN EnsureP23Restart /F 2>&1 | Out-Null
    _log "task self-deleted"
}

_log "=== ensure_restart_once run ==="

# ── 护栏 1：搭车检测 ────────────────────────────────────────────────────────
$landed = Get-Date "2026-08-01 12:00"
$cool = "D:\chengjie-instances\.ops\restart_cooldown\last_restart_zhiliao.json"
try {
    $j = Get-Content $cool -Raw -Encoding UTF8 | ConvertFrom-Json
    $last = Get-Date $j.ts
    if ($last -gt $landed) {
        _log "piggyback detected (last restart $last > $landed) - nothing to do"
        _selfdelete
        exit 0
    }
} catch { _log "cooldown file unreadable ($cool) - proceed with own restart" }

# ── 护栏 2：脏 .py 编译闸门（仓库根路径；删除态/不存在的跳过）──────────────
Set-Location $repo
$bad = 0
$files = git status --porcelain |
    Where-Object { $_ -match '\.py$' -and $_ -notmatch '^\?\?' } |
    ForEach-Object { $_.Substring(3).Trim() } |
    Where-Object { Test-Path $_ }
foreach ($f in $files) {
    python -m py_compile $f 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) { _log "SYNTAX FAIL: $f"; $bad++ }
}
Set-Location $engine
if ($bad -gt 0) {
    _log "compile gate blocked ($bad syntax failures) - keep task, retry next night"
    exit 1
}
_log "compile gate clean ($($files.Count) dirty .py)"

# ── 护栏 3：唯一入口重启 ────────────────────────────────────────────────────
& powershell -ExecutionPolicy Bypass -File (
    Join-Path $repo "deploy\instances\restart_instance.ps1") -Instance zhiliao 2>&1 |
    Out-File -Append -Encoding utf8 $log
if ($LASTEXITCODE -eq 0) {
    _log "restart OK - P23 .py loaded"
    _selfdelete
    exit 0
}
_log "restart script exited $LASTEXITCODE - keep task, retry next night"
exit 1
