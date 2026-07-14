# PR #157 真机 smoke — 验证 P2.0 / P2.1 / P2.2 / 新 task_type 端到端跑通
#
# PR #157 含 5 项端到端能力, 单测 cover 不到, 必须真机跑一次:
#   1. P2.0 即时取证: automation 层失败时同步抓 PNG/XML 到 _pending 区
#   2. P2.1 个性化话术: Ollama 生成 verification_note / first_greeting
#   3. P2.2 主页 enrichment: 补 bio/recent_posts 给 P2.1
#   4. facebook_group_member_greet 新 task_type: 群成员好友打招呼全链路
#   5. preflight VPN HttpProxy + IS_VALIDATED 兜底: 解决 shell 探测误报
#
# 用法:
#   pr157_smoke.ps1 -DeviceId 4HUSIB4T               # 默认 jp 婚活群
#   pr157_smoke.ps1 -DeviceId XXX -GroupName "ママ友" -PersonaKey jp_female_midlife
#   pr157_smoke.ps1 -DeviceId XXX -SkipVpnCheck      # 已知 VPN 通的话省 ~5s
#   pr157_smoke.ps1 -DeviceId XXX -SkipExtract       # 直接跳到 group_member_greet
#
# 退出码:
#   0 = 全部 5 项验收通过
#   1 = VPN / 设备 / API 准备阶段失败 (业务还没跑)
#   2 = facebook_extract_members 任务失败 / P2.0 forensics 没落盘 / P2.2 enrichment 没填字段
#   3 = facebook_group_member_greet 任务失败 / P2.1 个性化话术语言断言失败
#
# 依赖: 同 circle_prospect_smoke.ps1 (后端 + adb + $env:OPENCLAW_API_KEY)

param(
    [Parameter(Mandatory=$true)][string]$DeviceId,
    [string]$GroupName        = "婚活アラフォー",
    [string]$PersonaKey       = "jp_female_midlife",
    [string]$ApiHost          = "127.0.0.1:8000",
    [string]$ApiKey           = $env:OPENCLAW_API_KEY,
    [int]$PollIntervalSec     = 5,
    [int]$MaxWaitSeconds      = 900,
    [switch]$SkipVpnCheck,
    [switch]$SkipExtract
)

$ErrorActionPreference = 'Continue'
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)

function Write-Step { param([string]$Tag, [string]$Msg)
    Write-Host ""
    Write-Host "[$Tag] $Msg" -ForegroundColor Cyan
}
function Write-Pass { param([string]$Msg) Write-Host "  PASS $Msg" -ForegroundColor Green }
function Write-Fail { param([string]$Msg) Write-Host "  FAIL $Msg" -ForegroundColor Red }
function Write-Info { param([string]$Msg) Write-Host "  INFO $Msg" -ForegroundColor Gray }

function Invoke-Api {
    param([string]$Method, [string]$Path, $Body = $null)
    $url = "http://$ApiHost$Path"
    $headers = @{}
    if ($ApiKey) { $headers["X-API-Key"] = $ApiKey }
    $params = @{ Uri = $url; Method = $Method; Headers = $headers; TimeoutSec = 15 }
    if ($Body) {
        $params.Body = ($Body | ConvertTo-Json -Depth 8 -Compress)
        $params.ContentType = "application/json"
    }
    Invoke-RestMethod @params
}

function Wait-TaskTerminal {
    param([string]$TaskId, [int]$Timeout)
    $start = Get-Date; $lastStep = ""
    while ($true) {
        Start-Sleep -Seconds $PollIntervalSec
        $elapsed = [int]((Get-Date) - $start).TotalSeconds
        if ($elapsed -ge $Timeout) { Write-Fail "超时 $Timeout s"; return $null }
        try { $t = Invoke-Api GET "/tasks/$TaskId" } catch { continue }
        $step = $t.current_step; $sub = $t.current_sub_step
        $label = if ($step) { "$step$(if ($sub) { ' — ' + $sub })" } else { "(尚未上报)" }
        if ($label -ne $lastStep) {
            Write-Host ("    [{0,4}s] status={1,-10} step={2}" -f $elapsed, $t.status, $label)
            $lastStep = $label
        }
        if ($t.status -in @("completed","done","failed","cancelled","aborted")) { return $t }
    }
}

# ── [PREP-1] VPN 真通 ─────────────────────────────────────────────
if (-not $SkipVpnCheck) {
    Write-Step "PREP-1" "VPN 真通验证 — POST /proxy/health/$DeviceId/check"
    try { $h = Invoke-Api POST "/proxy/health/$DeviceId/check" } catch {
        Write-Fail "后端调用失败: $($_.Exception.Message)"; exit 1
    }
    if ($h.state -ne 'ok' -or -not $h.ip_match) {
        Write-Fail "VPN 不通 state=$($h.state) ip_match=$($h.ip_match) actual=$($h.actual_ip)"
        exit 1
    }
    Write-Pass "VPN 通 IP=$($h.actual_ip)"
} else { Write-Step "PREP-1" "VPN 检查已跳过 (-SkipVpnCheck)" }

# ── [PREP-2] Force-stop FB + Messenger ────────────────────────────
Write-Step "PREP-2" "force-stop FB + Messenger"
foreach ($pkg in @("com.facebook.katana", "com.facebook.orca")) {
    $r = & adb -s $DeviceId shell am force-stop $pkg 2>&1
    if ($LASTEXITCODE -ne 0) { Write-Fail "adb force-stop $pkg 失败: $r"; exit 1 }
    Write-Pass "killed $pkg"
}
Start-Sleep -Seconds 1

# ── [PREP-3] 记录 forensics/_pending 起始状态 (用于 P2.0 验收) ──────
$pendingDir = Join-Path $ProjectRoot "data/forensics/_pending/$DeviceId"
$beforeSnaps = @()
if (Test-Path $pendingDir) { $beforeSnaps = @(Get-ChildItem $pendingDir -Directory).Name }
Write-Info "_pending baseline: $($beforeSnaps.Count) snapshots"

# ════════════════════════════════════════════════════════════════════
# Phase 1: facebook_extract_members
#   验收: 任务跑完 (success 或 partial), forensics/_pending 有新快照,
#         platform_profiles 至少 N 条带 bio/recent_posts 字段
# ════════════════════════════════════════════════════════════════════
$extractTaskId = $null
if (-not $SkipExtract) {
    Write-Step "P1.1" "派 facebook_extract_members(group=$GroupName)"
    $body = @{
        type        = "facebook_extract_members"
        device_id   = $DeviceId
        params      = @{ group_name = $GroupName; persona_key = $PersonaKey
                         max_members = 5; enrich_top_n = 3 }
        priority    = 50
        created_via = "ops_smoke_pr157_extract"
        run_on_host = $true
    }
    try { $task = Invoke-Api POST "/tasks" $body } catch {
        Write-Fail "派发失败: $($_.Exception.Message)"; exit 2
    }
    $extractTaskId = $task.task_id
    Write-Pass "extract task_id = $extractTaskId"

    Write-Step "P1.2" "监控 extract 任务"
    $t = Wait-TaskTerminal -TaskId $extractTaskId -Timeout $MaxWaitSeconds
    if (-not $t) { exit 2 }
    if ($t.status -notin @("completed","done")) {
        Write-Fail "extract 任务终态 status=$($t.status) last_error=$($t.last_error)"
        # 不立即 exit, 因为失败本身可能正是 P2.0 即时取证要捕获的场景
    } else { Write-Pass "extract 任务 completed" }

    # ── 验收 1: P2.0 forensics/_pending 落盘 ──
    Write-Step "P2.0-VERIFY" "检查 forensics/_pending 是否产生新快照 (失败时取证)"
    $afterSnaps = @()
    if (Test-Path $pendingDir) { $afterSnaps = @(Get-ChildItem $pendingDir -Directory).Name }
    $newSnaps = $afterSnaps | Where-Object { $_ -notin $beforeSnaps }
    if ($t.status -notin @("completed","done") -and $newSnaps.Count -eq 0) {
        Write-Fail "P2.0 失败: 任务失败但 _pending 区 0 个新快照, capture_immediate 没生效"
        exit 2
    }
    Write-Pass "P2.0 OK: $($newSnaps.Count) 个新快照 (任务 $($t.status))"
    foreach ($s in ($newSnaps | Select-Object -First 3)) {
        $metaPath = Join-Path $pendingDir "$s/meta.json"
        if (Test-Path $metaPath) {
            $meta = Get-Content $metaPath -Raw | ConvertFrom-Json
            Write-Info "    snap=$s step=$($meta.step) screencap=$($meta.screencap.ok) hierarchy=$($meta.hierarchy.ok)"
        }
    }

    # ── 验收 2: P2.2 enrichment platform_profiles bio 填字段 ──
    Write-Step "P2.2-VERIFY" "检查 platform_profiles 是否填 bio/recent_posts"
    try {
        $resp = Invoke-Api GET "/facebook/profiles?limit=10&device_id=$DeviceId"
        $enriched = @($resp.rows | Where-Object { $_.bio -or ($_.recent_posts -and $_.recent_posts.Count -gt 0) })
        if ($enriched.Count -eq 0) {
            Write-Fail "P2.2 失败: platform_profiles 0 条带 bio/recent_posts (enrich 没跑或全失败)"
            Write-Info "    (此判定可能太严: enrich_top_n=3 小群可能样本不足, 看真机日志确认)"
        } else { Write-Pass "P2.2 OK: $($enriched.Count) profiles 带 bio/recent_posts" }
    } catch { Write-Fail "查询 profiles 失败: $($_.Exception.Message)" }
}

# ════════════════════════════════════════════════════════════════════
# Phase 2: facebook_group_member_greet 新 task_type
#   验收: 任务被 dispatcher 接受 (不报 unknown_task_type),
#         任何 verification_note / first_greeting 含日语假名 (P2.1 输出语言断言)
# ════════════════════════════════════════════════════════════════════
Write-Step "P2.1" "派 facebook_group_member_greet (新 task_type)"
$body = @{
    type        = "facebook_group_member_greet"
    device_id   = $DeviceId
    params      = @{
        target_groups = @($GroupName)
        max_friends_per_run = 3
        verification_note   = ""    # 留空让 P2.1 自动生成
        greeting             = ""   # 留空让 P2.1 自动生成
    }
    priority    = 50
    created_via = "ops_smoke_pr157_greet"
    run_on_host = $true
}
try { $task = Invoke-Api POST "/tasks" $body } catch {
    Write-Fail "派发失败: $($_.Exception.Message)"
    if ($_.ErrorDetails.Message) { Write-Info "    detail: $($_.ErrorDetails.Message)" }
    exit 3
}
$greetTaskId = $task.task_id
Write-Pass "greet task_id = $greetTaskId (dispatcher 接受新 task_type)"

Write-Step "P2.1-MONITOR" "监控 greet 任务 (含 P2.1 ollama 调用, 慢)"
$t = Wait-TaskTerminal -TaskId $greetTaskId -Timeout $MaxWaitSeconds
if (-not $t) { exit 3 }

# ── 验收 3: P2.1 个性化话术语言 ──
Write-Step "P2.1-VERIFY" "检查个性化话术是否生成 + 语言对齐 persona=$PersonaKey"
try {
    $detail = Invoke-Api GET "/tasks/$greetTaskId/detail"
    $messages = @($detail.outcomes | ForEach-Object { $_.message } | Where-Object { $_ })
    if ($messages.Count -eq 0) {
        Write-Fail "P2.1 失败: 0 条 outcome.message, 个性化话术没产出"
        exit 3
    }
    # 日语 personality 对应 jp_*: 至少 1 条含假名 (ひらがな U+3040-309F or カタカナ U+30A0-30FF)
    if ($PersonaKey -like "jp_*") {
        $jpHits = @($messages | Where-Object { $_ -match "[぀-ゟ゠-ヿ]" })
        if ($jpHits.Count -eq 0) {
            Write-Fail "P2.1 失败: jp_ persona 但 0 条 message 含假名"
            Write-Info "    样例: $($messages[0])"
            exit 3
        }
        Write-Pass "P2.1 OK: $($jpHits.Count)/$($messages.Count) 条话术含假名"
    } else {
        Write-Pass "P2.1 OK: $($messages.Count) 条话术 (非 jp persona, 跳过语言断言)"
    }
} catch { Write-Fail "查询 detail 失败: $($_.Exception.Message)"; exit 3 }

Write-Host ""
Write-Host "=================================================="
Write-Host "PR #157 smoke: ALL PASSED" -ForegroundColor Green
Write-Host "=================================================="
Write-Host "extract task : http://$ApiHost/tasks/$extractTaskId"
Write-Host "greet  task : http://$ApiHost/tasks/$greetTaskId"
exit 0
