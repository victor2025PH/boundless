# watchdog_instances.ps1 — 智聊/通译双实例探活自愈（实施29；与 status/start/stop 同目录成套）
#
# 背景（为什么需要它）：实例进程此前由交互会话（RDP）手工拉起，注销/重启即随会话消亡；
# 原 Boundless_*_Boot 开机任务用 InteractiveToken 登录类型，开机时无交互会话根本不会触发
# （Last Result 恒 267011=从未运行）。本脚本装成 \Boundless\Boundless-chengjie-watchdog 计划任务
# （S4U 账户、session 0、每 5 分钟）后：开机 ≤1 个节拍自动拉起、宕机 ≤1 个节拍自愈，
# 不依赖任何人登录；cron_sentinel.ps1 顺带巡检本任务自身退出码（\Boundless\ 前缀全覆盖）。
#
# 每轮动作（探测复用 status_instances.ps1 -Json；数据根自动探测规则见该脚本头注）：
#   退役实例（.ops\retired\<id>.flag 存在，cutover_merge.ps1 写）→ 跳过不探不拉；
#   DOWN（端口不在听）         → restart_instance.ps1 -FromWatchdog（含 /login 就绪 + 写冷却）；
#   假活（unresponsive）       → status 分级：warming/login_ready 当存活不计 strike；
#                                冷却窗内也不计（Phase4）；窗外连续 N 轮才 restart_instance；
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

. (Join-Path $PSScriptRoot '_restart_cooldown.ps1')

$FlagDir = Join-Path $env:LOCALAPPDATA 'boundless-watchdog'
New-Item -ItemType Directory -Force -Path $FlagDir | Out-Null
$Log = Join-Path $FlagDir 'watchdog.log'
function Say([string]$m) { $l = "[{0:yyyy-MM-dd HH:mm:ss}] {1}" -f (Get-Date), $m; Add-Content $Log $l; Write-Host $l }

$base = $BaseUrl
if (-not $base) { $base = [string]$env:PERSONA_SYNC_BASE }
if (-not $base) { $base = 'https://bd2026.cc' }
$key = $IngestKey
if (-not $key) { $key = [string]$env:EVENT_INGEST_KEY }

# Phase9: webhook/TG prefer ASCII note_en (encoding-proof); local Say may still use zh note.
function Get-InstAlertNote($inst) {
    if ($null -eq $inst) { return '' }
    try {
        if ($inst.PSObject.Properties.Name -contains 'note_en') {
            $en = [string]$inst.note_en
            if ($en) { return $en }
        }
    } catch {}
    try { return [string]$inst.note } catch { return '' }
}

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

# ── 探测（复用 status_instances.ps1；Phase7 读 UTF-8 快照文件，不经 Out-String）──
$statusScript = Join-Path $PSScriptRoot 'status_instances.ps1'
if (-not (Test-Path -LiteralPath $statusScript)) { Say "错误: 探测脚本缺失 $statusScript"; exit 2 }
$opsDir = 'D:\chengjie-instances\.ops'
if (-not (Test-Path -LiteralPath $opsDir)) { New-Item -ItemType Directory -Force -Path $opsDir | Out-Null }
$snapPath = Join-Path $opsDir 'last_status.json'
$statusArgs = @(
    '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $statusScript,
    '-Json', '-SnapshotPath', $snapPath
)
if ($ZhiliaoData) { $statusArgs += @('-ZhiliaoData', $ZhiliaoData) }
if ($TongyiData)  { $statusArgs += @('-TongyiData',  $TongyiData) }
# Run status (writes snapshot); ignore stdout encoding — file is SSOT
$null = & powershell.exe @statusArgs 2>&1
$st = $null
if (Test-Path -LiteralPath $snapPath) {
    try {
        $st = Get-Content -LiteralPath $snapPath -Raw -Encoding UTF8 | ConvertFrom-Json
    } catch {
        Say ("警告: 读取快照失败: $($_.Exception.Message)")
    }
}
if (-not $st -or -not $st.instances) {
    # Fallback: parse stdout (legacy) — may garble CJK notes but keeps heal working
    $raw = (& powershell.exe @statusArgs 2>&1 | Out-String)
    try { $st = $raw | ConvertFrom-Json } catch {}
    if (-not $st -or -not $st.instances) {
        Say ("错误: status_instances 快照/JSON 不可解析")
        exit 2
    }
}

function Wait-PortUp([int]$port, [int]$sec) {
    $deadline = (Get-Date).AddSeconds($sec)
    do {
        if (@(Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue).Count) { return $true }
        Start-Sleep -Seconds 3
    } while ((Get-Date) -lt $deadline)
    return $false
}

# Phase3: one heal path — restart_instance.ps1 (stop+start+/login ready+cooldown write).
function Invoke-WatchdogRestart {
    param(
        [Parameter(Mandatory = $true)][string]$InstanceId,
        [string]$DataRoot = '',
        [string]$Reason = 'watchdog'
    )
    $script = Join-Path $PSScriptRoot 'restart_instance.ps1'
    # Do not name this $args — that shadows PowerShell's automatic $args.
    $argList = @(
        '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $script,
        '-Instance', $InstanceId,
        '-FromWatchdog',
        '-Reason', $Reason,
        '-ReadyWaitSec', ([string]$StartWaitSec)
    )
    if ($DataRoot) { $argList += @('-DataDir', $DataRoot) }
    & powershell.exe @argList 2>&1 | ForEach-Object { Say "  [restart] $_" }
    $rc = [int]$LASTEXITCODE
    return @{ ok = ($rc -eq 0); rc = $rc }
}

function Get-Strikes([string]$file) {
    if (Test-Path $file) { try { return [int](Get-Content $file -Raw).Trim() } catch { return 0 } }
    return 0
}

$overall = 0
foreach ($inst in $st.instances) {
    # ── 退役实例跳过（融合切换 cutover_merge.ps1 写 .ops\retired\<id>.flag）────
    # 通译并库进智聊后必须保持停机：拉活会造成同一批平台账号被两个实例双拉双发。
    # 复活/回滚 = 删除 flag 文件（cutover_merge.ps1 -Rollback 会自动删）。
    $retFlag = Join-Path $opsDir ("retired\{0}.flag" -f $inst.id)
    if (Test-Path -LiteralPath $retFlag) {
        Say "$($inst.id) 已退役（$retFlag），跳过探活/自愈"
        Remove-Item (Join-Path $FlagDir "$($inst.id).down"), (Join-Path $FlagDir "$($inst.id).httpdead") -Force -ErrorAction SilentlyContinue
        continue
    }
    $downFlag   = Join-Path $FlagDir "$($inst.id).down"
    $strikeFile = Join-Path $FlagDir "$($inst.id).httpdead"
    # Phase5: graded readiness from status_instances (ready|warming|unresponsive|…)
    $phase = ''
    if ($inst.PSObject.Properties.Name -contains 'http_phase') { $phase = [string]$inst.http_phase }
    $loginReady = $false
    if ($inst.PSObject.Properties.Name -contains 'login_ready') { $loginReady = [bool]$inst.login_ready }
    # Treat warming + login_ready as alive: do not accumulate httpdead strikes / force-kill
    $serving = ($inst.http -gt 0) -or $loginReady -or ($phase -eq 'warming') -or ($phase -eq 'ready')
    $alive = $inst.listening -and $inst.engine_owned -and $serving

    if ($alive) {
        if (Test-Path $strikeFile) { Remove-Item $strikeFile -Force -ErrorAction SilentlyContinue }
        Remove-Item (Join-Path $FlagDir "$($inst.id).unresponsive.warn") -Force -ErrorAction SilentlyContinue
        if (Test-Path $downFlag) {
            Say "$($inst.id) 已恢复存活（端口 $($inst.port)），补发恢复通知并清 down-flag"
            Send-Alert "✅ $($inst.name) 已恢复存活（端口 $($inst.port)，$env:COMPUTERNAME）"
            Remove-Item $downFlag -Force -ErrorAction SilentlyContinue
        }
        if ($phase -eq 'warming') {
            $age = if ($inst.PSObject.Properties.Name -contains 'proc_age_sec') { $inst.proc_age_sec } else { '?' }
            Say "$($inst.id) warming（age=${age}s，/login+health 未齐）— 预热豁免，不计入假活"
        } elseif ($inst.verdict -ne 'GO') {
            $nAlert = Get-InstAlertNote $inst
            Say "$($inst.id) 存活但体检降级（非存活问题，不动手）: $nAlert"
        } else {
            Say "$($inst.id) GO（端口 $($inst.port) HTTP $($inst.http) login=$loginReady phase=$phase）"
        }
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
        # Phase4 warm-up exemption: during shared cooldown, HTTP can look "dead"
        # while the process is still warming. Do NOT accumulate strikes — otherwise
        # the moment cooldown ends, n already >= threshold and we immediately kill
        # a healthy-but-slow boot (workbench load-timeout flap).
        $cdWarm = Test-RestartCooldownActive -Instance $inst.id
        if ($cdWarm.active) {
            if (Test-Path $strikeFile) { Remove-Item $strikeFile -Force -ErrorAction SilentlyContinue }
            Say "$($inst.id) 端口在听但 HTTP 未就绪，重启冷却中（剩约 $($cdWarm.left_min) 分钟 reason=$($cdWarm.record.reason)），本轮不计假活 strike（预热豁免）"
            continue
        }
        # 假活：端口在听但 HTTP 无响应——连续 N 轮才强制重启，防启动中误杀
        $n = (Get-Strikes $strikeFile) + 1
        Set-Content -Path $strikeFile -Value $n
        # Phase6 early warning: at N-1 (default strike 2 of 3) alert once before force-kill
        if ($HttpDeadRestartAfter -gt 1 -and $n -eq ($HttpDeadRestartAfter - 1)) {
            $warnFlag = Join-Path $FlagDir "$($inst.id).unresponsive.warn"
            if (-not (Test-Path $warnFlag)) {
                Send-Alert "⏳ $($inst.name) unresponsive $n/$HttpDeadRestartAfter（phase=$phase，$env:COMPUTERNAME）— 下一轮将经 restart_instance 强杀，请人工先看 boot 日志"
                New-Item -ItemType File -Force -Path $warnFlag | Out-Null
            }
        }
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
        # Phase3: single entry = restart_instance.ps1 (ready gate + cooldown write).
        Say "$($inst.id) 假活满 $n 轮，经 restart_instance -FromWatchdog 重启"
        $rr = Invoke-WatchdogRestart -InstanceId $inst.id -DataRoot ([string]$inst.data_root) -Reason 'watchdog_httpdead'
        if ($rr.ok) {
            Say "$($inst.id) 自愈成功（httpdead → restart_instance）"
            Send-Alert "⚠️ $($inst.name) 假活，watchdog 已经 restart_instance 拉起（端口 $($inst.port)，$env:COMPUTERNAME）。请查 boot 日志"
            Remove-Item $downFlag, $strikeFile -Force -ErrorAction SilentlyContinue
            Remove-Item (Join-Path $FlagDir "$($inst.id).unresponsive.warn") -Force -ErrorAction SilentlyContinue
            $flap = Test-RestartFlap -Instance $inst.id
            if ($flap.flapping) {
                $rs = @($flap.reasons) -join ','
                Send-Alert "🚨 $($inst.name) RESTART FLAP: $($flap.count)x in $($flap.window_min)m ($env:COMPUTERNAME; reasons=$rs). Stop further kills."
            }
        } else {
            $overall = 1
            Say "$($inst.id) restart_instance 失败 rc=$($rr.rc)，需人工"
            if (-not (Test-Path $downFlag)) {
                Send-Alert "⛔ $($inst.name) 假活且 restart_instance 失败（rc=$($rr.rc)，$env:COMPUTERNAME），需人工介入"
                New-Item -ItemType File -Force -Path $downFlag | Out-Null
            }
        }
        continue
    }

    # ── DOWN：同一入口拉起（含 /login 就绪，比只等端口更稳）──────────────
    if ($NoSelfHeal) {
        $overall = 1
        $nAlert = Get-InstAlertNote $inst
        Say "$($inst.id) DOWN（-NoSelfHeal 演练，不动手）: $nAlert"
        if (-not (Test-Path $downFlag)) {
            Send-Alert "DOWN $($inst.name) ($nAlert, $env:COMPUTERNAME; watchdog NoSelfHeal, no action)"
            New-Item -ItemType File -Force -Path $downFlag | Out-Null
        }
        continue
    }
    # DOWN 仍拉起，即使冷却中（进程已死；-FromWatchdog 含 -Force）
    Say "$($inst.id) DOWN，经 restart_instance -FromWatchdog 拉起（数据根=$($inst.data_root) 来源=$($inst.data_source)）"
    $rr = Invoke-WatchdogRestart -InstanceId $inst.id -DataRoot ([string]$inst.data_root) -Reason 'watchdog_start'
    if ($rr.ok) {
        Say "$($inst.id) 自愈成功（DOWN → restart_instance）"
        Send-Alert "⚠️ $($inst.name) 曾宕机，watchdog 已经 restart_instance 拉起（端口 $($inst.port)，$env:COMPUTERNAME）。请留意宕机原因（boot 日志在 <数据根>\logs\）"
        Remove-Item $downFlag, $strikeFile -Force -ErrorAction SilentlyContinue
    } else {
        $overall = 1
        Say "$($inst.id) 自愈失败（restart_instance rc=$($rr.rc)），需人工"
        if (-not (Test-Path $downFlag)) {
            Send-Alert "⛔ $($inst.name) 宕机且 restart_instance 失败（rc=$($rr.rc)，$env:COMPUTERNAME），需人工介入"
            New-Item -ItemType File -Force -Path $downFlag | Out-Null
        }
    }
}

Say ("本轮结束 exit={0}" -f $overall)
exit $overall
