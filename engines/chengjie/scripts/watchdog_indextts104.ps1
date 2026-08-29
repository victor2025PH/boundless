# IndexTTS-2 (104:7865) watchdog - ChatX voice chain's single point.
#
# 2026-08-29 事故沉淀：104 在 14:04 意外重启（事件 41/6008），IndexTTS104 计划任务
# **没有开机触发器**（下次运行时间 N/A）→ 服务静默不起、ChatX 语音链断了 6 小时，
# 期间没有任何告警：智聊侧只会回落文字或报 minicpm_clone_unreachable，而那台机器
# ping/SSH 全通、GPU 空闲，从外面看「机器是好的」。
#
# 本脚本按 5 分钟节奏补两件事：① 重启后没起→拉起；② 进程活着但答不上来（7852 那起
# 「半死」同型：/health 200 而合成全超时）→ 也按挂掉处理。
# ASCII-only：PS 5.1 在 GBK 控制台下读 UTF-8 中文脚本会解码失败。
[CmdletBinding()]
param(
    [string]$Url = 'http://192.168.0.104:7865/health',
    [string]$TaskName = 'IndexTTS104',
    [int]$TimeoutSec = 15,
    [int]$RestartCooldownMin = 30,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
$stateFile = 'C:\idx_watchdog.state.json'
$logFile = 'C:\idx_watchdog.log'

function Write-Log([string]$msg) {
    $line = "[{0}] {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $msg
    Write-Output $line
    try {
        # 自轮转：超过 2MB 留一份 .1，避免看门狗自己把盘写满
        if ((Test-Path $logFile) -and ((Get-Item $logFile).Length -gt 2MB)) {
            Move-Item $logFile "$logFile.1" -Force
        }
        Add-Content -Path $logFile -Value $line -Encoding UTF8
    } catch { }
}

function Get-State {
    if (Test-Path $stateFile) {
        try { return Get-Content $stateFile -Raw | ConvertFrom-Json } catch { }
    }
    return [pscustomobject]@{ last_restart = ''; fail_streak = 0 }
}

function Save-State($st) {
    try { $st | ConvertTo-Json -Compress | Set-Content $stateFile -Encoding UTF8 } catch { }
}

$state = Get-State
$healthy = $false
$detail = ''

try {
    $r = Invoke-RestMethod -Uri $Url -TimeoutSec $TimeoutSec
    # model_loaded=false 同样算不健康：懒加载态下合成会失败，等于服务没在服役
    if ($r.status -eq 'ok' -and $r.model_loaded -eq $true) {
        $healthy = $true
        $detail = "engine=$($r.engine) fp16=$($r.fp16)"
    } else {
        $detail = "status=$($r.status) model_loaded=$($r.model_loaded) load_err=$($r.load_err)"
    }
} catch {
    $detail = "unreachable: " + $_.Exception.Message
}

if ($healthy) {
    if ([int]$state.fail_streak -gt 0) {
        Write-Log "RECOVERED after $($state.fail_streak) fail(s) - $detail"
    }
    $state.fail_streak = 0
    Save-State $state
    exit 0
}

$state.fail_streak = [int]$state.fail_streak + 1
Write-Log "UNHEALTHY (streak=$($state.fail_streak)) - $detail"

# 两振确认再动手：单次网络抖动不该触发重启（与 emotion_tts 看门狗同口径）
if ([int]$state.fail_streak -lt 2) {
    Save-State $state
    Write-Log "first strike - waiting for confirmation next tick"
    exit 1
}

if ($state.last_restart) {
    try {
        $since = (Get-Date) - [datetime]$state.last_restart
        if ($since.TotalMinutes -lt $RestartCooldownMin) {
            Write-Log ("cooldown {0:N1}/{1} min - skip restart" -f $since.TotalMinutes, $RestartCooldownMin)
            Save-State $state
            exit 1
        }
    } catch { }
}

if ($DryRun) {
    Write-Log "DryRun - would run: schtasks /run /tn $TaskName"
    Save-State $state
    exit 1
}

Write-Log "restarting via schtasks /run /tn $TaskName"
try {
    & schtasks /run /tn $TaskName 2>&1 | ForEach-Object { Write-Log ("  " + $_) }
    $state.last_restart = (Get-Date).ToString('o')
    $state.fail_streak = 0
    Save-State $state
    Write-Log "restart issued (model load takes ~40s; next tick verifies)"
} catch {
    Write-Log ("restart FAILED: " + $_.Exception.Message)
    Save-State $state
    exit 1
}
exit 0
