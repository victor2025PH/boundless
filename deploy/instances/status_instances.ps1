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

function Get-PortHolders([int]$port) {
    @(Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty OwningProcess -Unique)
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
        engine_owned = $false
        data_root = $root
        data_source = $src
        initialized = (Test-Path (Join-Path $root 'config\config.yaml'))
        overlay  = (Test-Path (Join-Path $root 'config\config.local.yaml'))
        domains  = (Test-Path (Join-Path $root 'domains'))
        spool    = (Test-Path (Join-Path $root 'events\spool'))
        license  = (Test-Path (Join-Path $root 'config\license.key'))
        verdict = 'DOWN'; note = $portNote
    }

    if ($pids.Count) {
        $s.listening = $true
        $s.pids = $pids
        $isOurs = $false
        foreach ($holderPid in $pids) {
            $p = Get-CimInstance Win32_Process -Filter "ProcessId=$holderPid" -ErrorAction SilentlyContinue
            if ($p -and $p.CommandLine -like '*main.py*') { $isOurs = $true }
        }
        $s.engine_owned = $isOurs
        $s.http = Probe-Http $s.port
        if ($isOurs) {
            $s.verdict = 'GO'
            if ($s.http -eq 0) { $s.verdict = 'DEGRADED'; $s.note = '端口在听但 HTTP 无响应（启动中或假活）' }
            elseif (-not $s.note) { $s.note = "HTTP $($s.http)（health 需鉴权，非 0 即视为活）" }
        } else {
            $s.verdict = 'DEGRADED'
            $s.note = "端口被非引擎进程占用 PID=$($pids -join ',')"
        }
    } else {
        if (-not (Test-Path $s.data_root)) { $s.note = '未初始化（数据根缺失，见 README §3）' }
        elseif (-not $s.initialized)       { $s.note = '未初始化（缺 config\config.yaml，见 README §3）' }
        else                               { $s.note = '未在跑（start_' + $inst.id + '.ps1 拉起）' }
    }
    if ($s.verdict -eq 'GO' -and -not $s.domains) { $s.verdict = 'DEGRADED'; $s.note += '；缺 domains junction（域包未加载）' }
    $states += [pscustomobject]$s
}

$worst = 0; $best = 2
foreach ($s in $states) {
    $lvl = switch ($s.verdict) { 'GO' {0} 'DEGRADED' {1} 'DOWN' {2} default {2} }
    if ($lvl -gt $worst) { $worst = $lvl }
    if ($lvl -lt $best)  { $best  = $lvl }
}
# 汇总退出码：全 GO=0；全 DOWN=2；其余（混合/降级）=1
$exitCode = if ($worst -eq 0) { 0 } elseif ($best -eq 2) { 2 } else { 1 }

if ($Json) {
    [ordered]@{
        timestamp = (Get-Date).ToString('o')
        verdict = $exitCode
        verdict_label = @('GO','DEGRADED','DOWN')[$exitCode]
        instances = $states
    } | ConvertTo-Json -Depth 5
    exit $exitCode
}

Write-Host '=============== chengjie 双实例 status（智聊 ChatX / 通译 LingoX）==============='
foreach ($s in $states) {
    $color = switch ($s.verdict) { 'GO' {'Green'} 'DEGRADED' {'Yellow'} 'DOWN' {'Red'} default {'Gray'} }
    $pidTxt = if ($s.pids.Count) { "PID=$($s.pids -join ',')" } else { '' }
    Write-Host ("  [{0,-8}] {1,-8} :{2,-6} {3,-14} {4}" -f $s.verdict, $s.id, $s.port, $pidTxt, $s.note) -ForegroundColor $color
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
