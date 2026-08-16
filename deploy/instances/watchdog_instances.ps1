# watchdog_instances.ps1 — 智聊/通译双实例探活自愈（实施29；与 status/start/stop 同目录成套）
#
# 背景（为什么需要它）：实例进程此前由交互会话（RDP）手工拉起，注销/重启即随会话消亡；
# 原 Boundless_*_Boot 开机任务用 InteractiveToken 登录类型，开机时无交互会话根本不会触发
# （Last Result 恒 267011=从未运行）。本脚本装成 \Boundless\Boundless-chengjie-watchdog 计划任务
# （S4U 账户、session 0、每 5 分钟）后：开机 ≤1 个节拍自动拉起、宕机 ≤1 个节拍自愈，
# 不依赖任何人登录；cron_sentinel.ps1 顺带巡检本任务自身退出码（\Boundless\ 前缀全覆盖）。
#
# 每轮动作（探测复用 status_instances.ps1 -Json；数据根自动探测规则见该脚本头注）：
#   DOWN（端口不在听）         → start_<实例>.ps1 -DataDir <根> 幂等拉起，等端口就绪；
#   假活（在听但 HTTP 无响应）  → 连续 -HttpDeadRestartAfter 轮（缺省 3≈15 分钟）才 stop+start
#                                强制重启，防「启动中/瞬时卡顿」误杀；计数落 flag 目录，恢复即清零；
#   端口被非引擎进程占用       → 只告警绝不清杀（与 start/stop 同一防呆哲学：双实例误杀代价大）；
#   domains 缺等配置性 DEGRADED → 只落日志（DailyVerify/人工处置；非存活问题，重启无益）。
# 告警：VPS /api/ops/alert（Bearer=EVENT_INGEST_KEY），与 cron_sentinel 同款「失败边沿告警 +
#       down-flag 幂等 + 恢复补发」；每次真实拉起/重启动作本身必告警——宕机事件必须让人知道；
#       崩溃循环 = 每轮一条自愈告警，就是要吵醒人。无密钥时只落本地日志。
#
# 用法（计划任务由 install_tasks.ps1 注册，-Engine chengjie 缺省任务集已含 watchdog；人工可直接跑）：
#   powershell -ExecutionPolicy Bypass -File deploy\instances\watchdog_instances.ps1              # 探测+自愈一轮
#   powershell -ExecutionPolicy Bypass -File deploy\instances\watchdog_instances.ps1 -NoSelfHeal  # 只探测告警不动手
# 退出码：0=全部存活（含本轮自愈成功） 1=有实例不健康且本轮未能自愈 2=探测/配置故障

[CmdletBinding()]
param(
    [string]$ZhiliaoData = '',        # 透传 status/start（缺省自动探测：参数>进程>生产缺省>仓库缺省）
    [string]$TongyiData  = '',
    [string]$BaseUrl   = '',          # 告警中继基址（缺省 env PERSONA_SYNC_BASE / https://bd2026.cc）
    [string]$IngestKey = '',          # 中继密钥（缺省 env EVENT_INGEST_KEY；无密钥只落日志不发告警）
    [string]$PythonExe = '',          # python 全路径：其目录前置进本进程 PATH（S4U/SYSTEM 账户无用户级 PATH，
                                      # start_*.ps1 的 Get-Command python 与 cmd 链裸 python 都靠它解析）
    [int]$HttpDeadRestartAfter = 3,   # 连续 N 轮假活才强制重启；0=永不重启假活（只告警）
    [int]$StartWaitSec = 90,          # 拉起后等端口就绪秒数（引擎初始化常见 10~30s）
    [switch]$NoSelfHeal               # 只探测+告警，不 start/stop（演练/排障）
)

$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}

$FlagDir = Join-Path $env:LOCALAPPDATA 'boundless-watchdog'
New-Item -ItemType Directory -Force -Path $FlagDir | Out-Null
$Log = Join-Path $FlagDir 'watchdog.log'
function Say([string]$m) { $l = "[{0:yyyy-MM-dd HH:mm:ss}] {1}" -f (Get-Date), $m; Add-Content $Log $l; Write-Host $l }

$base = $BaseUrl
if (-not $base) { $base = [string]$env:PERSONA_SYNC_BASE }
if (-not $base) { $base = 'https://bd2026.cc' }
$key = $IngestKey
if (-not $key) { $key = [string]$env:EVENT_INGEST_KEY }

function Send-Alert([string]$text) {
    if (-not $key) { Say '无 EVENT_INGEST_KEY，跳过告警发送（仅落日志）'; return }
    try {
        $body = @{ text = $text; source = "watchdog@$env:COMPUTERNAME" } | ConvertTo-Json -Compress
        $r = Invoke-RestMethod -Uri ($base.TrimEnd('/') + '/api/ops/alert') -Method Post `
            -Headers @{ Authorization = "Bearer $key" } -ContentType 'application/json' -Body $body -TimeoutSec 15
        Say ("告警已发 sent=$($r.sent)/$($r.recipients)")
    } catch {
        Say "告警发送失败: $($_.Exception.Message)"
    }
}

# ── python PATH 准备（拉起引擎用；探测本身不需要）───────────────────────
if ($PythonExe) {
    if (Test-Path -LiteralPath $PythonExe) {
        $env:Path = (Split-Path -Parent $PythonExe) + ';' + $env:Path
    } else {
        Say "警告: -PythonExe 不存在（$PythonExe），按原 PATH 继续（DOWN 时拉起可能失败）"
    }
}

# ── 探测（复用 status_instances.ps1 -Json，数据根探测/判定单一源）────────
$statusScript = Join-Path $PSScriptRoot 'status_instances.ps1'
if (-not (Test-Path -LiteralPath $statusScript)) { Say "错误: 探测脚本缺失 $statusScript"; exit 2 }
$statusArgs = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $statusScript, '-Json')
if ($ZhiliaoData) { $statusArgs += @('-ZhiliaoData', $ZhiliaoData) }
if ($TongyiData)  { $statusArgs += @('-TongyiData',  $TongyiData) }
$raw = (& powershell.exe @statusArgs 2>&1 | Out-String)
$st = $null
try { $st = $raw | ConvertFrom-Json } catch {}
if (-not $st -or -not $st.instances) {
    Say ("错误: status_instances.ps1 -Json 输出不可解析: " + $raw.Substring(0, [Math]::Min(300, $raw.Length)))
    exit 2
}

function Wait-PortUp([int]$port, [int]$sec) {
    $deadline = (Get-Date).AddSeconds($sec)
    do {
        if (@(Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue).Count) { return $true }
        Start-Sleep -Seconds 3
    } while ((Get-Date) -lt $deadline)
    return $false
}

function Get-Strikes([string]$file) {
    if (Test-Path $file) { try { return [int](Get-Content $file -Raw).Trim() } catch { return 0 } }
    return 0
}

$overall = 0
foreach ($inst in $st.instances) {
    $downFlag   = Join-Path $FlagDir "$($inst.id).down"
    $strikeFile = Join-Path $FlagDir "$($inst.id).httpdead"
    $alive = $inst.listening -and $inst.engine_owned -and ($inst.http -gt 0)

    if ($alive) {
        if (Test-Path $strikeFile) { Remove-Item $strikeFile -Force -ErrorAction SilentlyContinue }
        if (Test-Path $downFlag) {
            Say "$($inst.id) 已恢复存活（端口 $($inst.port)），补发恢复通知并清 down-flag"
            Send-Alert "✅ $($inst.name) 已恢复存活（端口 $($inst.port)，$env:COMPUTERNAME）"
            Remove-Item $downFlag -Force -ErrorAction SilentlyContinue
        }
        if ($inst.verdict -ne 'GO') { Say "$($inst.id) 存活但体检降级（非存活问题，不动手）: $($inst.note)" }
        else { Say "$($inst.id) GO（端口 $($inst.port) HTTP $($inst.http)）" }
        continue
    }

    # ── 不健康的三种形态 ───────────────────────────────────────────────
    if ($inst.listening -and -not $inst.engine_owned) {
        # 端口被外人占：动手可能误杀（双实例/其他服务），只边沿告警
        $overall = 1
        Say "$($inst.id) 端口 $($inst.port) 被非引擎进程占用（PID=$(@($inst.pids) -join ',')），拒绝自愈，需人工"
        if (-not (Test-Path $downFlag)) {
            Send-Alert "⛔ $($inst.name) 端口 $($inst.port) 被非引擎进程占用（PID=$(@($inst.pids) -join ',')，$env:COMPUTERNAME），watchdog 拒绝自动清杀，需人工核实"
            New-Item -ItemType File -Force -Path $downFlag | Out-Null
        }
        continue
    }

    if ($inst.listening) {
        # 假活：端口在听但 HTTP 无响应——连续 N 轮才强制重启，防启动中误杀
        $n = (Get-Strikes $strikeFile) + 1
        Set-Content -Path $strikeFile -Value $n
        if ($HttpDeadRestartAfter -le 0 -or $n -lt $HttpDeadRestartAfter) {
            Say "$($inst.id) 疑似假活（端口在听 HTTP 无响应），计数 $n/$HttpDeadRestartAfter，本轮先观察"
            continue
        }
        Remove-Item $strikeFile -Force -ErrorAction SilentlyContinue
        if ($NoSelfHeal) {
            $overall = 1
            Say "$($inst.id) 假活满 $n 轮（-NoSelfHeal 演练，不动手）"
            if (-not (Test-Path $downFlag)) {
                Send-Alert "⛔ $($inst.name) 假活满 $n 轮（端口在听 HTTP 无响应，$env:COMPUTERNAME；watchdog 演练模式未动手）"
                New-Item -ItemType File -Force -Path $downFlag | Out-Null
            }
            continue
        }
        Say "$($inst.id) 假活满 $n 轮，强制重启（stop_instance + start_$($inst.id)）"
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'stop_instance.ps1') `
            -Instance $inst.id 2>&1 | ForEach-Object { Say "  [stop] $_" }
        if ($LASTEXITCODE -ne 0) {
            $overall = 1
            Say "$($inst.id) 强制重启失败于 stop（rc=$LASTEXITCODE），需人工"
            if (-not (Test-Path $downFlag)) {
                Send-Alert "⛔ $($inst.name) 假活且 watchdog 停止失败（rc=$LASTEXITCODE，$env:COMPUTERNAME），需人工介入"
                New-Item -ItemType File -Force -Path $downFlag | Out-Null
            }
            continue
        }
        # 停成功后走下面的统一拉起路径
    }

    # ── DOWN（或假活已停）：start_<实例>.ps1 幂等拉起 ──────────────────
    if ($NoSelfHeal) {
        $overall = 1
        Say "$($inst.id) DOWN（-NoSelfHeal 演练，不动手）: $($inst.note)"
        if (-not (Test-Path $downFlag)) {
            Send-Alert "⛔ $($inst.name) DOWN（$($inst.note)，$env:COMPUTERNAME；watchdog 演练模式未动手）"
            New-Item -ItemType File -Force -Path $downFlag | Out-Null
        }
        continue
    }
    $startScript = Join-Path $PSScriptRoot "start_$($inst.id).ps1"
    Say "$($inst.id) 不在跑，拉起: start_$($inst.id).ps1 -DataDir $($inst.data_root)（数据根来源=$($inst.data_source)）"
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $startScript -DataDir $inst.data_root 2>&1 |
        ForEach-Object { Say "  [start] $_" }
    $rc = $LASTEXITCODE
    if ($rc -eq 0 -and (Wait-PortUp $inst.port $StartWaitSec)) {
        Say "$($inst.id) 自愈成功：端口 $($inst.port) 恢复监听"
        Send-Alert "⚠️ $($inst.name) 曾宕机，watchdog 已自动拉起（端口 $($inst.port) 恢复监听，$env:COMPUTERNAME）。请留意宕机原因（boot 日志在 <数据根>\logs\）"
        Remove-Item $downFlag, $strikeFile -Force -ErrorAction SilentlyContinue
    } else {
        $overall = 1
        Say "$($inst.id) 自愈失败（start rc=$rc，等待 ${StartWaitSec}s 端口未就绪），需人工"
        if (-not (Test-Path $downFlag)) {
            Send-Alert "⛔ $($inst.name) 宕机且 watchdog 自动拉起失败（start rc=$rc，$env:COMPUTERNAME），需人工介入"
            New-Item -ItemType File -Force -Path $downFlag | Out-Null
        }
    }
}

Say ("本轮结束 exit={0}" -f $overall)
exit $overall
