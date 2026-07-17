# status_instances.ps1 — 智聊/通译双实例健康探测（只读，绝不改任何状态）
# 用法: powershell -ExecutionPolicy Bypass -File .\status_instances.ps1 [-Json]
# 判定（对齐 deploy.ps1 三态语义）：
#   GO       主端口在听，且持有者是引擎进程（python main.py）；HTTP 有响应更佳
#   DEGRADED 端口在听但持有者不像本引擎 / 数据目录体检有缺
#   DOWN     端口不在听（含「数据根未初始化」）
# 退出码: 0=全 GO  1=有 DEGRADED/部分 DOWN  2=全 DOWN（供 cron/监控消费）
# 说明: /api/admin/health 需鉴权（stack.json auth=true），故 HTTP 探测只看
#       「有无 HTTP 响应」（401/403 也算活着），不解析 body。

[CmdletBinding()]
param([switch]$Json)

$ErrorActionPreference = 'SilentlyContinue'
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}

$Instances = @(
    [pscustomobject]@{ id='zhiliao'; name='智聊 ChatX';  port=18799; alt_port=18787; data=(Join-Path $PSScriptRoot 'zhiliao\data') },
    [pscustomobject]@{ id='tongyi';  name='通译 LingoX'; port=18899; alt_port=18887; data=(Join-Path $PSScriptRoot 'tongyi\data') }
)

function Get-PortHolders([int]$port) {
    @(Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty OwningProcess -Unique)
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
    $s = [ordered]@{
        id = $inst.id; name = $inst.name; port = $inst.port
        listening = $false; pids = @(); http = 0
        data_root = $inst.data
        initialized = (Test-Path (Join-Path $inst.data 'config\config.yaml'))
        overlay  = (Test-Path (Join-Path $inst.data 'config\config.local.yaml'))
        domains  = (Test-Path (Join-Path $inst.data 'domains'))
        spool    = (Test-Path (Join-Path $inst.data 'events\spool'))
        license  = (Test-Path (Join-Path $inst.data 'config\license.key'))
        verdict = 'DOWN'; note = ''
    }
    $pids = Get-PortHolders $inst.port
    if (-not $pids.Count -and $inst.alt_port) {
        $pids = Get-PortHolders $inst.alt_port
        if ($pids.Count) { $s.port = $inst.alt_port; $s.note = "主端口 $($inst.port) 不在听，备用位 $($inst.alt_port) 在听（端口漂移？核对实例 overlay）" }
    }
    if ($pids.Count) {
        $s.listening = $true
        $s.pids = $pids
        $isOurs = $false
        foreach ($holderPid in $pids) {
            $p = Get-CimInstance Win32_Process -Filter "ProcessId=$holderPid" -ErrorAction SilentlyContinue
            if ($p -and $p.CommandLine -like '*main.py*') { $isOurs = $true }
        }
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
        if (-not (Test-Path $inst.data)) { $s.note = '未初始化（数据根缺失，见 README §3）' }
        elseif (-not $s.initialized)     { $s.note = '未初始化（缺 config\config.yaml，见 README §3）' }
        else                             { $s.note = '未在跑（start_' + $inst.id + '.ps1 拉起）' }
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
