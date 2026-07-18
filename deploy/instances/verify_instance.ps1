# verify_instance.ps1 — chengjie 双实例生产验收一键校验（只读：只发 GET / Test-Path，不写实例任何数据）
# 用法: powershell -ExecutionPolicy Bypass -File .\verify_instance.ps1
#         [-Base http://127.0.0.1:18899] [-Instance tongyi] [-DataDir <数据根>]
#         [-Json] [-TimeoutSec 5] [-CheckSpoolGrowth] [-SpoolGrowthWaitSec 10] [-WhatIf]
# 检查项（全部只读）：
#   1) http_alive        GET /login 应 200（登录页公开端点）
#   2) brand_manifest    GET /manifest.webmanifest（免鉴权）：name/short_name/description
#                        含期望品牌且不含另一实例品牌（只比对这三个文本字段——icons 里
#                        固定含 chatx.png 资源路径，属引擎共用静态资源，不算品牌串味）
#   3) brand_login       /login HTML 含期望品牌、不含另一实例品牌（overlay 未生效即暴露）
#   4) health_endpoint   GET /api/admin/health 无凭据应 401/403（活着且鉴权已启用；
#                        200 = 端点未鉴权 → WARN 提示核查 auth_token）
#   5) port_owner        Base 指向本机(127.0.0.1/localhost)时：端口在听且持有者命令行含 main.py
#   6) 数据根体检         config.yaml / config.local.yaml / domains junction / events\spool
#                        （数据根本机可见才查：显式给了 -DataDir 但缺失 → FAIL；未给且缺省
#                        位置不存在 → SKIP，视为远程验收模式）
#   7) license_key       <数据根>\config\license.key 在位（无 → WARN：社区模式/共享回落，README §5）
#   8) spool_growth      -CheckSpoolGrowth 时观察窗口内 events-*.jsonl 总字节是否增长
#                        （期间请在另一窗口操作后台制造事件；无增长 → WARN，低流量属正常）
# 退出码: 0=无 FAIL（允许 WARN/SKIP） 1=有 FAIL。-Json 输出机器可读结果（同 status_instances.ps1 风格）。
# -WhatIf 只打印将执行的检查清单，不发任何请求（干跑核对参数用）。

[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$Base = '',                     # 缺省按 -Instance 推导 http://127.0.0.1:<主端口>
    [ValidateSet('tongyi', 'zhiliao')]
    [string]$Instance = 'tongyi',
    [string]$DataDir = '',                  # 数据根（缺省 <本目录>\<实例>\data；远程验收可不给）
    [switch]$Json,
    [int]$TimeoutSec = 5,
    [switch]$CheckSpoolGrowth,
    [int]$SpoolGrowthWaitSec = 10
)

$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}

# ── 实例元数据（端口与 stack.json chengjie_* 条目一致；品牌词与实例 overlay 模板一致）──
$Meta = @{
    tongyi  = @{ name = '通译 LingoX'; port = 18899; expect = '通译|LingoX'; forbid = '智聊|ChatX' }
    zhiliao = @{ name = '智聊 ChatX';  port = 18799; expect = '智聊|ChatX';  forbid = '通译|LingoX' }
}
$m = $Meta[$Instance]
if (-not $Base) { $Base = "http://127.0.0.1:$($m.port)" }
$Base = $Base.TrimEnd('/')

$DataDirGiven = [bool]$DataDir
if (-not $DataDir) { $DataDir = Join-Path $PSScriptRoot "$Instance\data" }
$DataDir = [IO.Path]::GetFullPath([IO.Path]::Combine((Get-Location).ProviderPath, $DataDir))

# ── 结果收集 ─────────────────────────────────────────────────────────────
$checks = New-Object System.Collections.ArrayList
function Add-Check([string]$item, [string]$status, [string]$detail) {
    [void]$checks.Add([pscustomobject]@{ item = $item; status = $status; detail = $detail })
    if (-not $Json) {
        $color = switch ($status) { 'PASS' {'Green'} 'WARN' {'Yellow'} 'FAIL' {'Red'} default {'Gray'} }
        Write-Host ("  [{0,-4}] {1,-18} {2}" -f $status, $item, $detail) -ForegroundColor $color
    }
}

# ── 只读 HTTP GET（手写 HttpWebRequest：401/403 也要拿到状态码与响应体；响应体按 UTF-8 解码）──
function Invoke-HttpGet([string]$Url) {
    $out = [ordered]@{ code = 0; body = ''; err = '' }
    $resp = $null
    try {
        $req = [System.Net.HttpWebRequest]::Create($Url)
        $req.Method = 'GET'
        $req.Timeout = $TimeoutSec * 1000
        $req.ReadWriteTimeout = $TimeoutSec * 1000
        $req.AllowAutoRedirect = $false
        $req.UserAgent = 'verify_instance.ps1'
        $resp = $req.GetResponse()
    } catch [System.Net.WebException] {
        if ($_.Exception.Response) { $resp = $_.Exception.Response }
        else { $out.err = $_.Exception.Message; return [pscustomobject]$out }
    } catch {
        $out.err = $_.Exception.Message
        return [pscustomobject]$out
    }
    try {
        $out.code = [int]$resp.StatusCode
        $ms = New-Object System.IO.MemoryStream
        $resp.GetResponseStream().CopyTo($ms)
        $out.body = [Text.Encoding]::UTF8.GetString($ms.ToArray())
    } catch {
        $out.err = $_.Exception.Message
    } finally {
        $resp.Close()
    }
    return [pscustomobject]$out
}

# ── -WhatIf：只打印计划，不发任何请求 ────────────────────────────────────
if ($WhatIfPreference) {
    Write-Host "WhatIf: verify_instance 将执行以下只读检查（实例=$Instance，Base=$Base，数据根=$DataDir）"
    Write-Host "  1) GET $Base/login                  期望 200"
    Write-Host "  2) GET $Base/manifest.webmanifest   品牌含「$($m.expect)」不含「$($m.forbid)」"
    Write-Host "  3) GET $Base/login                  HTML 品牌断言（同上词表）"
    Write-Host "  4) GET $Base/api/admin/health       期望 401/403（鉴权已启用）"
    Write-Host "  5) 本机端口监听 + 持有者 main.py（仅 Base 指向本机时）"
    Write-Host "  6) 数据根体检 Test-Path：config.yaml / config.local.yaml / domains / events\spool"
    Write-Host "  7) license.key 在位检查（缺 → WARN 社区模式）"
    if ($CheckSpoolGrowth) { Write-Host "  8) spool 增长观察 ${SpoolGrowthWaitSec}s（events-*.jsonl 字节数）" }
    Write-Host "WhatIf: 未发出任何请求、未触碰任何文件。"
    exit 0
}

if (-not $Json) {
    Write-Host "=============== verify — $($m.name) 生产验收校验（只读）==============="
    Write-Host "  Base: $Base"
    Write-Host "  数据根: $DataDir $(if (-not $DataDirGiven) { '（缺省位置）' })"
    Write-Host '----------------------------------------------------------------------'
}

# ── 1) HTTP 活性：/login 应 200 ──────────────────────────────────────────
$login = Invoke-HttpGet "$Base/login"
if ($login.code -eq 200) {
    Add-Check 'http_alive' 'PASS' "GET /login → 200"
} elseif ($login.code -gt 0) {
    Add-Check 'http_alive' 'WARN' "GET /login → HTTP $($login.code)（有响应但非 200，人工核查）"
} else {
    Add-Check 'http_alive' 'FAIL' "GET /login 无响应: $($login.err)"
}

# ── 2) 品牌断言：manifest（免鉴权，最可靠的品牌探针）────────────────────
$mani = Invoke-HttpGet "$Base/manifest.webmanifest"
if ($mani.code -eq 200 -and $mani.body) {
    $brandText = $mani.body
    try {
        $j = $mani.body | ConvertFrom-Json
        $brandText = @($j.name, $j.short_name, $j.description) -join ' | '
    } catch {}
    if ($brandText -notmatch $m.expect) {
        Add-Check 'brand_manifest' 'FAIL' "manifest 不含期望品牌「$($m.expect)」: $brandText"
    } elseif ($brandText -match $m.forbid) {
        Add-Check 'brand_manifest' 'FAIL' "manifest 串味，出现「$($m.forbid)」: $brandText"
    } else {
        Add-Check 'brand_manifest' 'PASS' $brandText
    }
} else {
    Add-Check 'brand_manifest' 'FAIL' "GET /manifest.webmanifest → HTTP $($mani.code) $($mani.err)"
}

# ── 3) 品牌断言：登录页 HTML（title + 全文；overlay 未生效/装错实例即暴露）──
if ($login.code -eq 200 -and $login.body) {
    $title = ''
    if ($login.body -match '<title>([^<]*)</title>') { $title = $Matches[1].Trim() }
    if ($login.body -notmatch $m.expect) {
        Add-Check 'brand_login' 'FAIL' "登录页不含期望品牌「$($m.expect)」（title=$title）"
    } elseif ($login.body -match $m.forbid) {
        Add-Check 'brand_login' 'FAIL' "登录页出现另一实例品牌「$($m.forbid)」（title=$title）"
    } else {
        Add-Check 'brand_login' 'PASS' "title=$title"
    }
} else {
    Add-Check 'brand_login' 'SKIP' '登录页不可达（见 http_alive）'
}

# ── 4) health 端点：无凭据应 401/403（活着 + 鉴权生效）──────────────────
$health = Invoke-HttpGet "$Base/api/admin/health"
if ($health.code -in 401, 403) {
    Add-Check 'health_endpoint' 'PASS' "GET /api/admin/health → $($health.code)（需鉴权，活）"
} elseif ($health.code -eq 200) {
    Add-Check 'health_endpoint' 'WARN' 'health 未鉴权即返回 200——核查实例 overlay 的 web_admin.auth_token 是否已配置'
} elseif ($health.code -gt 0) {
    Add-Check 'health_endpoint' 'WARN' "GET /api/admin/health → HTTP $($health.code)（非常规，人工核查）"
} else {
    Add-Check 'health_endpoint' 'FAIL' "health 无响应: $($health.err)"
}

# ── 5) 端口/持有者（仅 Base 指向本机时可查）─────────────────────────────
$uri = $null
try { $uri = [Uri]$Base } catch {}
$isLocal = $uri -and ($uri.Host -in '127.0.0.1', 'localhost', '::1')
if ($isLocal) {
    $port = $uri.Port
    $pids = @(Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty OwningProcess -Unique)
    if (-not $pids.Count) {
        Add-Check 'port_owner' 'FAIL' "端口 $port 无监听"
    } else {
        $isOurs = $false
        foreach ($holderPid in $pids) {
            $p = Get-CimInstance Win32_Process -Filter "ProcessId=$holderPid" -ErrorAction SilentlyContinue
            if ($p -and $p.CommandLine -like '*main.py*') { $isOurs = $true }
        }
        if ($isOurs) { Add-Check 'port_owner' 'PASS' "端口 $port 持有者是引擎 main.py（PID=$($pids -join ',')）" }
        else         { Add-Check 'port_owner' 'FAIL' "端口 $port 被非引擎进程占用 PID=$($pids -join ',')" }
    }
} else {
    Add-Check 'port_owner' 'SKIP' "Base 非本机（$($uri.Host)），跳过端口/进程检查"
}

# ── 6/7) 数据根体检 + 授权文件（本机可见数据根才查）──────────────────────
$dataVisible = Test-Path $DataDir
if (-not $dataVisible -and -not $DataDirGiven) {
    Add-Check 'data_root' 'SKIP' "缺省数据根不存在（远程验收模式只跑 HTTP 检查；本机验收请传 -DataDir）"
} elseif (-not $dataVisible) {
    Add-Check 'data_root' 'FAIL' "显式指定的数据根不存在: $DataDir"
} else {
    Add-Check 'data_root' 'PASS' $DataDir
    foreach ($probe in @(
        @{ item = 'config.yaml';       path = 'config\config.yaml';       miss = 'FAIL'; hint = 'README §3 初始化未完成' },
        @{ item = 'config.local.yaml'; path = 'config\config.local.yaml'; miss = 'FAIL'; hint = '实例 overlay 缺失（端口/品牌不隔离）' },
        @{ item = 'domains junction';  path = 'domains';                  miss = 'FAIL'; hint = '域包 junction 缺失（README §3 第 4 步）' },
        @{ item = 'events\spool';      path = 'events\spool';             miss = 'WARN'; hint = '事件 spool 目录缺失（README §7；start 脚本传 EVENT_SPOOL_DIR 指向此处）' }
    )) {
        $p = Join-Path $DataDir $probe.path
        if (Test-Path $p) { Add-Check $probe.item 'PASS' '在位' }
        else              { Add-Check $probe.item $probe.miss "缺失: $p — $($probe.hint)" }
    }
    if (Test-Path (Join-Path $DataDir 'config\license.key')) {
        Add-Check 'license_key' 'PASS' '实例级授权文件在位（start 脚本注入 LICENSE_KEY）'
    } else {
        Add-Check 'license_key' 'WARN' '无实例授权文件——按社区模式/共享文件回落（README §5；生产建议实例级授权）'
    }
}

# ── 8) spool 增长观察（可选；无增长只 WARN——事件靠业务流量驱动）──────────
if ($CheckSpoolGrowth) {
    $spoolDir = Join-Path $DataDir 'events\spool'
    if (-not (Test-Path $spoolDir)) {
        Add-Check 'spool_growth' 'SKIP' 'spool 目录不存在（见上面体检项）'
    } else {
        $measure = {
            param($dir)
            $sum = 0
            foreach ($f in @(Get-ChildItem -Path $dir -Filter 'events-*.jsonl' -File -ErrorAction SilentlyContinue)) { $sum += $f.Length }
            $sum
        }
        $before = & $measure $spoolDir
        if (-not $Json) {
            Write-Host "  ... 观察 spool 增长 ${SpoolGrowthWaitSec}s（请在另一窗口操作后台/触发翻译制造事件）" -ForegroundColor DarkGray
        }
        Start-Sleep -Seconds $SpoolGrowthWaitSec
        $after = & $measure $spoolDir
        if ($after -gt $before) {
            Add-Check 'spool_growth' 'PASS' "events-*.jsonl 增长 $($after - $before) 字节（$before → $after）"
        } else {
            Add-Check 'spool_growth' 'WARN' "观察窗口内无增长（$before 字节）——低流量属正常；可登录后台操作后复测"
        }
    }
}

# ── 汇总 ─────────────────────────────────────────────────────────────────
$nFail = @($checks | Where-Object status -eq 'FAIL').Count
$nWarn = @($checks | Where-Object status -eq 'WARN').Count
$exitCode = if ($nFail -eq 0) { 0 } else { 1 }

if ($Json) {
    [ordered]@{
        timestamp = (Get-Date).ToString('o')
        instance  = $Instance
        base      = $Base
        data_root = $DataDir
        verdict   = $exitCode
        verdict_label = @('PASS', 'FAIL')[$exitCode]
        fail_count = $nFail
        warn_count = $nWarn
        checks    = $checks
    } | ConvertTo-Json -Depth 5
    exit $exitCode
}

Write-Host '----------------------------------------------------------------------'
if ($exitCode -eq 0) {
    Write-Host "  VERIFY: PASS（$($checks.Count) 项，$nWarn 个警告）— $($m.name) 验收通过" -ForegroundColor Green
} else {
    Write-Host "  VERIFY: FAIL（$nFail 项失败 / $nWarn 个警告）— 处理 FAIL 后复跑；回滚见 migrate_117_runbook.md 对应阶段" -ForegroundColor Red
}
exit $exitCode
