# status_instances.ps1 — 智聊/通译双实例健康探测（只读，绝不改任何状态）
# 用法: powershell -ExecutionPolicy Bypass -File .\status_instances.ps1 [-Json]
#       [-ZhiliaoData <数据根>] [-TongyiData <数据根>]   ← 显式指定数据根（最高优先级）
# 数据根自动探测（实施29：修「生产根不在缺省位时误报 DEGRADED/DOWN」）——每实例按序取第一个命中：
#   ① 显式参数
#   ② 端口持有引擎进程的 AITR_DATA_DIR（start_*.ps1 以 cmd 链注入，上溯父进程命令行解析，最可信）
#   ③ 生产缺省 D:\chengjie-instances\<实例>\data（.117 迁移后形态，与 install_tasks.ps1 chengjie
#      缺省同源；仅当其 config\config.yaml 存在才认，防在别的机器上误指）
#   ④ 仓库缺省 deploy\instances\<实例>\data（向后兼容旧形态）
# 判定（对齐 deploy.ps1 三态语义）：
#   GO       主端口在听，且持有者是引擎进程（python main.py）；HTTP 有响应更佳
#   DEGRADED 端口在听但持有者不像本引擎 / 数据目录体检有缺
#   DOWN     端口不在听（含「数据根未初始化」）
# 退出码: 0=全 GO  1=有 DEGRADED/部分 DOWN  2=全 DOWN（供 cron/监控消费）
# 说明: /api/admin/health 需鉴权（stack.json auth=true），故 HTTP 探测只看
#       「有无 HTTP 响应」（401/403 也算活着），不解析 body。

[CmdletBinding()]
param(
    [switch]$Json,
    [string]$SnapshotPath = '',  # Phase7: UTF-8 no-BOM snapshot path (ops/watchdog SSOT)
    [switch]$NoSnapshot,         # skip default snapshot write
    [string]$ZhiliaoData = '',   # 与 start_zhiliao.ps1 -DataDir 同义（缺省按头注顺序自动探测）
    [string]$TongyiData  = ''    # 与 start_tongyi.ps1  -DataDir 同义（缺省按头注顺序自动探测）
)

$ErrorActionPreference = 'SilentlyContinue'
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}

# .117 生产数据根基址（迁移后实例住仓库外；install_tasks.ps1 的 chengjie 缺省 cfg 同源）
$ProdBase = 'D:\chengjie-instances'

$Instances = @(
    [pscustomobject]@{ id='zhiliao'; name='智聊 ChatX';  port=18799; alt_port=18787; param=$ZhiliaoData },
    [pscustomobject]@{ id='tongyi';  name='通译 LingoX'; port=18899; alt_port=18887; param=$TongyiData }
)

# portproxy 豁免表（2026-08-29 事故：14:14 整机重启后引擎未起，LAN 直连入口的
# netsh portproxy 监听（svchost/iphlpsvc 持有 192.168.x.x:18799）被本探测算成
# 「端口被非引擎进程占用」→ watchdog 按防呆哲学拒绝自愈 → 5.4h 永久宕机。
# stop_instance.ps1 自 2026-08-12 起就有同款窄豁免，此处对齐：只豁免
# 「svchost 且 监听地址:端口 与在册 v4tov4 规则完全匹配、转发目标 127.0.0.1」；
# 回环上的陌生进程照旧算占用，真端口劫持不会被掩护。
$script:ppRules = @()
try {
    foreach ($ln in @(netsh interface portproxy show v4tov4 2>$null)) {
        if ("$ln" -match '^\s*(\d+\.\d+\.\d+\.\d+)\s+(\d+)\s+(\S+)\s+(\d+)\s*$' -and $Matches[3] -eq '127.0.0.1') {
            $script:ppRules += ("{0}:{1}" -f $Matches[1], [int]$Matches[2])
        }
    }
} catch {}
$script:svchostPidCache = @{}

function Get-PortHolders([int]$port) {
    $conns = @(Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue)
    $holders = @()
    foreach ($c in $conns) {
        $holderPid = [int]$c.OwningProcess
        if ($script:ppRules -contains ("{0}:{1}" -f $c.LocalAddress, [int]$c.LocalPort)) {
            if (-not $script:svchostPidCache.ContainsKey($holderPid)) {
                $p = Get-CimInstance Win32_Process -Filter "ProcessId=$holderPid" -ErrorAction SilentlyContinue
                $script:svchostPidCache[$holderPid] = ($p -and $p.Name -ieq 'svchost.exe')
            }
            if ($script:svchostPidCache[$holderPid]) { continue }
        }
        if ($holders -notcontains $holderPid) { $holders += $holderPid }
    }
    return ,$holders
}

# 从端口持有进程反查真实数据根：start_*.ps1 的 cmd 链形如
#   cmd.exe /c set "AITR_DATA_DIR=<根>" && … && python main.py
# 持有者是 python（自身命令行无环境变量）时上溯父 cmd.exe 解析；最多上溯 2 层。
function Sniff-DataRoot([object[]]$pids) {
    foreach ($holderPid in @($pids)) {
        $p = Get-CimInstance Win32_Process -Filter "ProcessId=$holderPid" -ErrorAction SilentlyContinue
        for ($hop = 0; ($hop -lt 2) -and $p; $hop++) {
            if ($p.CommandLine -match 'AITR_DATA_DIR=([^"&]+)') {
                $root = $Matches[1].Trim()
                if ($root) { return $root.TrimEnd('\') }
            }
            $p = Get-CimInstance Win32_Process -Filter "ProcessId=$($p.ParentProcessId)" -ErrorAction SilentlyContinue
        }
    }
    return $null
}

function Probe-Http([int]$port) {
    # 任何 HTTP 状态码（含 401/403）都证明引擎 web 线程活着；连接拒绝/超时=无响应
    $url = "http://127.0.0.1:$port/api/admin/health"
    try {
        $resp = Invoke-WebRequest -Uri $url -TimeoutSec 4 -UseBasicParsing -ErrorAction Stop
        return [int]$resp.StatusCode
    } catch {
        $code = 0
        try { $code = [int]$_.Exception.Response.StatusCode.value__ } catch {}
        return $code   # 0 = 无 HTTP 响应
    }
}

function Probe-Login([int]$port) {
    # Public /login readiness (same gate restart_instance waits for). 200 = seat-ready.
    $url = "http://127.0.0.1:$port/login"
    try {
        $resp = Invoke-WebRequest -Uri $url -TimeoutSec 4 -UseBasicParsing -ErrorAction Stop
        return [int]$resp.StatusCode
    } catch {
        $code = 0
        try { $code = [int]$_.Exception.Response.StatusCode.value__ } catch {}
        return $code
    }
}

function Get-ProcessAgeSec([int[]]$pids) {
    # Youngest owning process age (seconds). -1 if unknown.
    $minAge = [int]::MaxValue
    foreach ($procId in $pids) {
        $proc = Get-Process -Id $procId -ErrorAction SilentlyContinue
        if ($proc -and $proc.StartTime) {
            $age = [int]((Get-Date) - $proc.StartTime).TotalSeconds
            if ($age -ge 0 -and $age -lt $minAge) { $minAge = $age }
        }
    }
    if ($minAge -eq [int]::MaxValue) { return -1 }
    return $minAge
}

$states = @()
foreach ($inst in $Instances) {
    # ── 端口/持有者探测（先于数据根解析：②需要 pids）─────────────────────
    $pids = Get-PortHolders $inst.port
    $portNote = ''
    $effPort = $inst.port
    if (-not $pids.Count -and $inst.alt_port) {
        $pids = Get-PortHolders $inst.alt_port
        if ($pids.Count) { $effPort = $inst.alt_port; $portNote = "主端口 $($inst.port) 不在听，备用位 $($inst.alt_port) 在听（端口漂移？核对实例 overlay）" }
    }

    # ── 退役实例（融合切换 cutover_merge.ps1 写 .ops\retired\<id>.flag，与 watchdog 同旗标）──
    # 不在听 → RETIRED（不计退出码——退役是预期形态，不再整机误报 DEGRADED）；
    # 仍在听 → 幽灵进程告警注记，照常走判定（退役实例复活属异常，必须可见）。
    $retFlag = Join-Path $ProdBase (".ops\retired\{0}.flag" -f $inst.id)
    if (Test-Path -LiteralPath $retFlag) {
        # 旗标体是 JSON（cutover_merge 写 reason=merged-into-zhiliao；实施102 阶段4 在 117 写
        # reason=migrated-to-173）——注记照 reason 说话，别把「迁走」写成「并入」。
        $retReason = 'merged-into-zhiliao'
        try { $rj = Get-Content -LiteralPath $retFlag -Raw -Encoding UTF8 | ConvertFrom-Json; if ($rj.reason) { $retReason = [string]$rj.reason } } catch {}
        if (-not $pids.Count) {
            $states += [pscustomobject][ordered]@{
                id = $inst.id; name = $inst.name; port = $inst.port
                listening = $false; pids = @(); http = 0
                login = 0; login_ready = $false
                proc_age_sec = -1
                http_phase = 'retired'
                engine_owned = $false
                data_root = ''; data_source = '退役旗标'
                initialized = $false; overlay = $false
                domains = $false; spool = $false; license = $false
                verdict = 'RETIRED'
                note = ('已退役（' + $retReason + '；旗标 ' + $retFlag + '，回滚时摘旗即恢复探测）')
                note_en = ('retired (' + $retReason + '; flag present, remove it to resume probing)')
                retired = $true
            }
            continue
        }
        $portNote = ('⚠ 退役旗在但端口仍有监听（幽灵进程？核对后处置） ' + $portNote).Trim()
    }

    # ── 数据根解析：参数 > 进程 > 生产缺省 > 仓库缺省（头注①→④）──────────
    $root = $inst.param; $src = '参数'
    if (-not $root -and $pids.Count) {
        $root = Sniff-DataRoot $pids
        if ($root) { $src = '进程' }
    }
    if (-not $root) {
        $prod = Join-Path $ProdBase (Join-Path $inst.id 'data')
        if (Test-Path (Join-Path $prod 'config\config.yaml')) { $root = $prod; $src = '生产缺省' }
    }
    if (-not $root) { $root = Join-Path $PSScriptRoot (Join-Path $inst.id 'data'); $src = '仓库缺省' }

    $s = [ordered]@{
        id = $inst.id; name = $inst.name; port = $effPort
        listening = $false; pids = @(); http = 0
        login = 0; login_ready = $false
        proc_age_sec = -1
        http_phase = 'down'   # ready | warming | unresponsive | down | foreign
        engine_owned = $false
        data_root = $root
        data_source = $src
        initialized = (Test-Path (Join-Path $root 'config\config.yaml'))
        overlay  = (Test-Path (Join-Path $root 'config\config.local.yaml'))
        domains  = (Test-Path (Join-Path $root 'domains'))
        spool    = (Test-Path (Join-Path $root 'events\spool'))
        license  = (Test-Path (Join-Path $root 'config\license.key'))
        verdict = 'DOWN'; note = $portNote; note_en = ''
    }

    if ($pids.Count) {
        $s.listening = $true
        $s.pids = $pids
        $s.proc_age_sec = Get-ProcessAgeSec $pids
        $isOurs = $false
        foreach ($holderPid in $pids) {
            $p = Get-CimInstance Win32_Process -Filter "ProcessId=$holderPid" -ErrorAction SilentlyContinue
            if ($p -and $p.CommandLine -like '*main.py*') { $isOurs = $true }
        }
        $s.engine_owned = $isOurs
        $s.http = Probe-Http $s.port
        $s.login = Probe-Login $s.port
        $s.login_ready = ($s.login -eq 200)
        if ($isOurs) {
            # Phase5 graded readiness:
            #   ready        = health responds OR /login 200 (seat-capable)
            #   warming      = listen+ours but neither ready yet, process young (<180s)
            #   unresponsive = listen+ours, neither ready, process old (true zombie risk)
            if (($s.http -gt 0) -or $s.login_ready) {
                $s.http_phase = 'ready'
                $s.verdict = 'GO'
                if (-not $s.note) {
                    if ($s.login_ready -and ($s.http -eq 0)) {
                        $s.note = "login ready, health lag (HTTP 0) — still seat-ready"
                    } else {
                        $s.note = "HTTP $($s.http) login $($s.login)（health 需鉴权，非 0 即视为活）"
                    }
                }
                # Phase8: ASCII note_en is ops/API SSOT (encoding-proof)
                if ($s.login_ready -and ($s.http -eq 0)) {
                    $s.note_en = "login ready, health lag (HTTP 0) - still seat-ready"
                } else {
                    $s.note_en = "HTTP $($s.http) login $($s.login) (health may need auth; non-zero = alive)"
                }
                if ($portNote) {
                    $s.note_en = "alt-port drift; " + $s.note_en
                }
            } elseif (($s.proc_age_sec -ge 0) -and ($s.proc_age_sec -lt 180)) {
                $s.http_phase = 'warming'
                $s.verdict = 'DEGRADED'
                $s.note = "warming: port up, /login+health not ready yet (age=$($s.proc_age_sec)s) — do not force-kill"
                $s.note_en = "warming: port up, /login+health not ready yet (age=$($s.proc_age_sec)s) - do not force-kill"
            } else {
                $s.http_phase = 'unresponsive'
                $s.verdict = 'DEGRADED'
                $ageTxt = if ($s.proc_age_sec -ge 0) { "$($s.proc_age_sec)s" } else { '?' }
                $s.note = "端口在听但 /login 与 health 均无响应（age=$ageTxt，假活风险）"
                $s.note_en = "unresponsive: /login+health dead (age=$ageTxt) - zombie risk"
            }
        } else {
            $s.http_phase = 'foreign'
            $s.verdict = 'DEGRADED'
            $s.note = "端口被非引擎进程占用 PID=$($pids -join ',')"
            $s.note_en = "foreign process on port PID=$($pids -join ',')"
        }
    } else {
        $s.http_phase = 'down'
        if (-not (Test-Path $s.data_root)) {
            $s.note = '未初始化（数据根缺失，见 README §3）'
            $s.note_en = 'not initialized (data root missing; see README §3)'
        } elseif (-not $s.initialized) {
            $s.note = '未初始化（缺 config\config.yaml，见 README §3）'
            $s.note_en = 'not initialized (missing config\config.yaml; see README §3)'
        } else {
            $s.note = '未在跑（start_' + $inst.id + '.ps1 拉起）'
            $s.note_en = 'not running (start_' + $inst.id + '.ps1)'
        }
    }
    if ($s.verdict -eq 'GO' -and -not $s.domains) {
        $s.verdict = 'DEGRADED'
        $s.note += '；缺 domains junction（域包未加载）'
        if (-not $s.note_en) { $s.note_en = 'ready' }
        $s.note_en += '; missing domains junction'
    }
    if (-not $s.note_en) { $s.note_en = 'see note' }
    $states += [pscustomobject]$s
}

$worst = 0; $best = 2
foreach ($s in $states) {
    if ($s.verdict -eq 'RETIRED') { continue }   # 退役=预期形态，不计汇总退出码
    $lvl = switch ($s.verdict) { 'GO' {0} 'DEGRADED' {1} 'DOWN' {2} default {2} }
    if ($lvl -gt $worst) { $worst = $lvl }
    if ($lvl -lt $best)  { $best  = $lvl }
}
# 汇总退出码：全 GO=0；全 DOWN=2；其余（混合/降级）=1
$exitCode = if ($worst -eq 0) { 0 } elseif ($best -eq 2) { 2 } else { 1 }

$payload = [ordered]@{
    timestamp = (Get-Date).ToString('o')
    verdict = $exitCode
    verdict_label = @('GO','DEGRADED','DOWN')[$exitCode]
    instances = $states
}

# Phase7: always persist UTF-8 no-BOM snapshot (avoid Out-String / console CP garble).
# Watchdog + ops card read this file; stdout -Json remains for quick scripts.
function Write-StatusSnapshot([object]$obj) {
    $targets = New-Object System.Collections.Generic.List[string]
    if ($SnapshotPath) { [void]$targets.Add($SnapshotPath) }
    elseif (-not $NoSnapshot) {
        [void]$targets.Add('D:\chengjie-instances\.ops\last_status.json')
        [void]$targets.Add((Join-Path $PSScriptRoot '.ops\last_status.json'))
    }
    if (-not $targets.Count) { return }
    $json = ($obj | ConvertTo-Json -Depth 8)
    $utf8 = New-Object System.Text.UTF8Encoding $false
    foreach ($p in $targets) {
        try {
            $dir = Split-Path -Parent $p
            if ($dir -and -not (Test-Path -LiteralPath $dir)) {
                New-Item -ItemType Directory -Force -Path $dir | Out-Null
            }
            [System.IO.File]::WriteAllText($p, $json, $utf8)
        } catch {}
    }
}
Write-StatusSnapshot $payload

if ($Json) {
    # Force UTF-8 console so nested powershell capture keeps CJK notes intact when needed
    try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}
    $OutputEncoding = [Text.Encoding]::UTF8
    $payload | ConvertTo-Json -Depth 8
    exit $exitCode
}

Write-Host '=============== chengjie 双实例 status（智聊 ChatX / 通译 LingoX）==============='
foreach ($s in $states) {
    $color = switch ($s.verdict) { 'GO' {'Green'} 'DEGRADED' {'Yellow'} 'DOWN' {'Red'} default {'Gray'} }
    $pidTxt = if ($s.pids.Count) { "PID=$($s.pids -join ',')" } else { '' }
    Write-Host ("  [{0,-8}] {1,-8} :{2,-6} {3,-14} {4}" -f $s.verdict, $s.id, $s.port, $pidTxt, $s.note) -ForegroundColor $color
    if ($s.listening) {
        Write-Host ("             phase={0} login={1} health={2} age={3}s" -f $s.http_phase, $s.login, $s.http, $s.proc_age_sec) -ForegroundColor DarkGray
    }
    if ($s.retired) { continue }   # 退役行：注记已说明一切，明细行的「缺」是噪音
    Write-Host ("             root={0}（{1}）" -f $s.data_root, $s.data_source) -ForegroundColor DarkGray
    $init = if ($s.initialized) { 'ok' } else { '缺' }
    $ovl  = if ($s.overlay)     { 'ok' } else { '缺' }
    $dom  = if ($s.domains)     { 'ok' } else { '缺' }
    $spl  = if ($s.spool)       { 'ok' } else { '缺' }
    $lic  = if ($s.license)     { '实例级' } else { '无(社区/共享回落)' }
    Write-Host ("             config={0} overlay={1} domains={2} spool={3} license={4}" -f $init, $ovl, $dom, $spl, $lic) -ForegroundColor DarkGray
}
Write-Host '--------------------------------------------------------------------------------'
$label = @('GO','DEGRADED','DOWN')[$exitCode]
$vc = @('Green','Yellow','Red')[$exitCode]
Write-Host ("  VERDICT: [{0}] {1}" -f $exitCode, $label) -ForegroundColor $vc
exit $exitCode
