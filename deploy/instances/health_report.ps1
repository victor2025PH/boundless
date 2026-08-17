# health_report.ps1 — 账号反封号健康巡检（实施31 · 2026-07-20；与 status_instances.ps1 成套）
#
# 一键把引擎 GET /api/ops/account-health 的聚合结果打成「号体检单」：运行态 / 健康红黄绿灯 /
# 今日发送与预热上限 / 时·日配额 / 熔断 / 冻结 / 近7天运维事件（暂停·封禁计数）。观察期用它
# 随时看「我的号安不安全、被风控过吗、还能发多少」，不必逐个查 orchestrator/limiter/审计库。
#
# 用法：
#   powershell -ExecutionPolicy Bypass -File deploy\instances\health_report.ps1              # 智聊(18799)
#   powershell -ExecutionPolicy Bypass -File deploy\instances\health_report.ps1 -Instance tongyi
#   powershell -ExecutionPolicy Bypass -File deploy\instances\health_report.ps1 -All          # 双实例
#   powershell -ExecutionPolicy Bypass -File deploy\instances\health_report.ps1 -Json         # 机器可读
# 退出码：0=fleet 绿（全 ok） 1=黄（有 stopped） 2=红（有 at_risk/frozen） 3=探测失败/端点不可用

[CmdletBinding()]
param(
    [ValidateSet('zhiliao', 'tongyi')]
    [string]$Instance = 'zhiliao',
    [switch]$All,
    [switch]$Json
)

$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}

$Meta = @{
    zhiliao = @{ name = '智聊 ChatX';  port = 18799 }
    tongyi  = @{ name = '通译 LingoX'; port = 18899 }
}
$targets = if ($All) { @('zhiliao', 'tongyi') } else { @($Instance) }

function Get-Token([string]$inst) {
    $cfg = "D:\chengjie-instances\$inst\data\config\config.local.yaml"
    if (-not (Test-Path $cfg)) { return '' }
    $line = (Get-Content $cfg | Select-String 'auth_token:' | Select-Object -First 1).Line
    if (-not $line) { return '' }
    return ($line -split 'auth_token:')[1].Trim().Trim('"').Trim("'")
}

function Get-Health([string]$inst) {
    $port = $Meta[$inst].port
    $tok = Get-Token $inst
    if (-not $tok) { return @{ ok = $false; err = "未读到 $inst 的 auth_token" } }
    try {
        $r = Invoke-RestMethod -Uri "http://127.0.0.1:$port/api/ops/account-health" `
            -Headers @{ Authorization = "Bearer $tok" } -TimeoutSec 15
        return @{ ok = $true; data = $r }
    } catch {
        $code = $null
        try { $code = $_.Exception.Response.StatusCode.value__ } catch {}
        $hint = if ($code -eq 404) { '端点不存在（实例需重启加载实施31代码）' } else { $_.Exception.Message }
        return @{ ok = $false; err = $hint }
    }
}

$results = @()
foreach ($t in $targets) { $results += @{ inst = $t; res = (Get-Health $t) } }

if ($Json) {
    ($results | ForEach-Object { @{ instance = $_.inst; ok = $_.res.ok; data = $_.res.data; err = $_.res.err } }) |
        ConvertTo-Json -Depth 8
    exit 0
}

$worstExit = 0
foreach ($item in $results) {
    $inst = $item.inst; $res = $item.res; $m = $Meta[$inst]
    Write-Host ""
    Write-Host ("====== 无界 · 账号反封号健康巡检（{0} / {1}）======" -f $m.name, $m.port) -ForegroundColor Cyan
    if (-not $res.ok) {
        Write-Host ("  探测失败: {0}" -f $res.err) -ForegroundColor Red
        if ($worstExit -lt 3) { $worstExit = 3 }
        continue
    }
    $d = $res.data
    $fleetColor = switch ($d.fleet_light) { 'green' {'Green'} 'amber' {'Yellow'} 'red' {'Red'} default {'Gray'} }
    $orch = if ($d.orchestrator_running) { '运行中' } else { '未运行' }
    $froz = if ($d.global_frozen) { '是（全局冻结！）' } else { '否' }
    Write-Host ("  机群灯: {0}   编排器: {1}   全局冻结: {2}   账号数: {3}" -f `
        $d.fleet_light.ToUpper(), $orch, $froz, $d.total) -ForegroundColor $fleetColor
    Write-Host "  ----------------------------------------------------------------"
    foreach ($a in $d.accounts) {
        $tag = switch ($a.overall) { 'ok' {'OK  '} 'stopped' {'STOP'} 'at_risk' {'RISK'} 'frozen' {'FRZN'} default {'??  '} }
        $col = switch ($a.overall) { 'ok' {'Green'} 'stopped' {'Yellow'} 'at_risk' {'Red'} 'frozen' {'Red'} default {'Gray'} }
        $h = $a.health; $rt = $a.rate
        Write-Host ("  [{0}] {1}:{2}   运行={3} worker={4}" -f `
            $tag, $a.platform, $a.account_id, $(if ($a.running){'是'}else{'否'}), $a.worker_state) -ForegroundColor $col
        Write-Host ("         健康 {0}({1})  今日 {2}/{3}(预热上限)  配额 日{4}/{5} 时{6}/{7}  熔断 {8}" -f `
            $h.light, $h.score, $h.sends_today, $h.recommended_cap, `
            $rt.day_used, $rt.day_limit, $rt.hour_used, $rt.hour_limit, `
            $(if ($rt.circuit_open){'开(暂停自动发)'}else{'否'})) -ForegroundColor DarkGray
        $evTotal = $a.events_7d.total
        $evTxt = if ($evTotal -gt 0) {
            ($a.events_7d.by_kind.PSObject.Properties | ForEach-Object { "$($_.Name)×$($_.Value)" }) -join ' '
        } else { '无' }
        $evColor = if ($evTotal -gt 0) { 'Yellow' } else { 'DarkGray' }
        Write-Host ("         近7天风控事件: {0}" -f $evTxt) -ForegroundColor $evColor
        if (-not $h.proxy_bound) {
            Write-Host "         提示: 未绑独立代理（健康扣分；多号高频建议一号一 IP 降关联）" -ForegroundColor DarkYellow
        }
    }
    Write-Host "  ----------------------------------------------------------------"
    $instExit = switch ($d.fleet_light) { 'red' {2} 'amber' {1} default {0} }
    $vColor = switch ($instExit) { 2 {'Red'} 1 {'Yellow'} default {'Green'} }
    Write-Host ("  VERDICT: [{0}] fleet={1}" -f $instExit, $d.fleet_light) -ForegroundColor $vColor
    if ($instExit -gt $worstExit) { $worstExit = $instExit }
}
Write-Host ""
exit $worstExit
