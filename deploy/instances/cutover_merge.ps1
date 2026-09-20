# cutover_merge.ps1 — 融合实例一键切换：通译 LingoX 并库进 智聊 ChatX（P2 cutover）
#
# 干什么（低峰窗口 02:00-03:00 执行，全程 ~5-10 分钟）：
#   0) 预检（只读）：双实例状态、registry.key、磁盘余量、merge 干跑报告、overlay 键差异提示
#   1) 关看门狗计划任务（防止合并中被自动拉活写库；结束后 finally 恢复）
#   2) 停两台实例（tongyi → zhiliao；端口释放才继续 —— 停机中合并 = 唯一一致性窗口）
#   3) 全量备份两边 config 目录（全套 SQLite）→ D:\chengjie-instances\.ops\cutover_backup\<ts>\
#   4) scripts/merge_instance_data.py --apply（凭证换密 + business_line 打标 + FTS 同步）
#   5) 校验：apply 退出码 + 复跑干跑「待插入必须归零」（收敛性证明）
#   6) 写通译退役旗标 .ops\retired\tongyi.flag（watchdog 见旗标即跳过，绝不拉活退役实例）
#   7) 起智聊 → 轮询 /login 200 → 刷新 status 快照 → 恢复看门狗
#
# 用法（缺省 = 只跑预检干跑，零副作用；-Execute 才真切）：
#   powershell -ExecutionPolicy Bypass -File deploy\instances\cutover_merge.ps1              # 预检+干跑
#   powershell -ExecutionPolicy Bypass -File deploy\instances\cutover_merge.ps1 -Execute     # 真切（要敲 MERGE 确认）
#   powershell -ExecutionPolicy Bypass -File deploy\instances\cutover_merge.ps1 -Execute -Force   # 免确认（窗口内挂批跑）
#   powershell -ExecutionPolicy Bypass -File deploy\instances\cutover_merge.ps1 -Rollback    # 回滚（最新备份；-BackupId 指定）
#
# 回滚语义：恢复智聊 config 目录到备份时点（/MIR 镜像还原）→ 删退役旗标 → 双实例重新拉起。
#   通译数据根在整个流程中【只读】，回滚天然无损；备份目录永不自动删除。
# 退出码：0=成功  1=预检/校验失败（未动生产）  2=执行段失败（看输出提示回滚）  3=用法错误

[CmdletBinding()]
param(
    [switch]$Execute,                 # 真切（缺省只预检+干跑）
    [switch]$Rollback,                # 回滚模式（与 -Execute 互斥）
    [string]$BackupId = '',           # 回滚用备份 ID（缺省 = 最新一份）
    [switch]$Force,                   # 跳过交互确认 + 窗口时段告警
    [string]$SourceData = 'D:\chengjie-instances\tongyi\data',
    [string]$TargetData = 'D:\chengjie-instances\zhiliao\data',
    [string]$PythonExe  = '',         # 缺省 PATH 里的 python
    [string]$BusinessLine = 'translation',
    [int]$ReadyWaitSec  = 240,        # 起智聊后等 /login 200 的秒数
    [switch]$SkipWatchdogToggle       # 无看门狗计划任务的机器（试点/开发机）
)

$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}

$RepoRoot   = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$EngineDir  = Join-Path $RepoRoot 'engines\chengjie'
$MergeCli   = Join-Path $EngineDir 'scripts\merge_instance_data.py'
$OpsDir     = 'D:\chengjie-instances\.ops'
$BackupRoot = Join-Path $OpsDir 'cutover_backup'
$RetiredDir = Join-Path $OpsDir 'retired'
$WatchdogTask = '\Boundless\Boundless-chengjie-watchdog'
$ZhiliaoLogin = 'http://127.0.0.1:18799/login'
$TongyiLogin  = 'http://127.0.0.1:18899/login'

$Py = if ($PythonExe) { $PythonExe } else { 'python' }

function Say([string]$m)  { Write-Host ("[cutover] {0}" -f $m) }
function Warn([string]$m) { Write-Host ("[cutover] ⚠ {0}" -f $m) -ForegroundColor Yellow }
function Die([string]$m, [int]$code = 1) {
    Write-Host ("[cutover] ✖ {0}" -f $m) -ForegroundColor Red
    exit $code
}

if ($Execute -and $Rollback) { Die '-Execute 与 -Rollback 互斥' 3 }

# ── 小工具 ──────────────────────────────────────────────────────────────────
function Get-DirBytes([string]$path) {
    if (-not (Test-Path -LiteralPath $path)) { return 0 }
    return [long]((Get-ChildItem -LiteralPath $path -Recurse -File -ErrorAction SilentlyContinue |
        Measure-Object Length -Sum).Sum)
}

function Invoke-Robocopy([string]$src, [string]$dst, [switch]$Mirror) {
    $flags = @('/E', '/NFL', '/NDL', '/NJH', '/NP', '/R:2', '/W:2')
    if ($Mirror) { $flags = @('/MIR') + $flags[1..($flags.Count - 1)] }
    & robocopy $src $dst @flags | Out-Null
    $rc = $LASTEXITCODE
    # robocopy: 0-7 = 成功（含无差异/有复制），>=8 = 失败
    if ($rc -ge 8) { throw "robocopy 失败 rc=$rc（$src → $dst）" }
}

# PS5.1 陷阱：EAP=Stop 时原生命令的 2>&1 会把 stderr 行包装成终止性 ErrorRecord
# （merge CLI 的摘要就打在 stderr）。所有带 2>&1 的原生调用都要临时降 EAP。
function Invoke-Native([scriptblock]$block) {
    $eap = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { & $block; return [int]$LASTEXITCODE } finally { $ErrorActionPreference = $eap }
}

function Invoke-MergeCli([switch]$Apply, [string]$ReportPath) {
    $mergeArgs = @($MergeCli, '--source', $SourceData, '--target', $TargetData,
                   '--business-line', $BusinessLine)
    if ($Apply)      { $mergeArgs += '--apply' }
    if ($ReportPath) { $mergeArgs += @('--report', $ReportPath) }
    Push-Location $EngineDir
    try {
        return Invoke-Native {
            & $Py @mergeArgs 2>&1 | ForEach-Object { Write-Host "  [merge] $_" }
        }
    } finally { Pop-Location }
}

function Get-ReportInserts([string]$reportPath) {
    # 报告 tables[].inserted 求和（干跑=待插入数；apply 后复跑干跑应为 0 = 收敛）
    $code = "import json,sys; r=json.load(open(sys.argv[1],encoding='utf-8')); " +
            "print(sum(int(t.get('inserted') or 0) for t in r.get('tables',[])))"
    $n = & $Py -c $code $reportPath 2>$null
    if ($LASTEXITCODE -ne 0) { return -1 }
    return [int]$n
}

function Stop-One([string]$inst) {
    Say "停止实例 $inst …"
    $stopScript = Join-Path $PSScriptRoot 'stop_instance.ps1'
    $rc = Invoke-Native {
        & powershell.exe -NoProfile -ExecutionPolicy Bypass `
            -File $stopScript -Instance $inst 2>&1 |
            ForEach-Object { Write-Host "  [stop-$inst] $_" }
    }
    if ($rc -ne 0) { throw "stop_instance $inst 失败 rc=$rc" }
}

function Start-One([string]$inst, [string]$dataDir) {
    $script = Join-Path $PSScriptRoot ("start_{0}.ps1" -f $inst)
    Say "拉起实例 $inst …"
    $rc = Invoke-Native {
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $script -DataDir $dataDir 2>&1 |
            ForEach-Object { Write-Host "  [start-$inst] $_" }
    }
    if ($rc -ne 0) { throw "start_$inst 失败 rc=$rc" }
}

function Wait-Login([string]$url, [int]$sec) {
    $deadline = (Get-Date).AddSeconds($sec)
    do {
        try {
            $r = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 5
            if ($r.StatusCode -eq 200) { return $true }
        } catch {}
        Start-Sleep -Seconds 5
    } while ((Get-Date) -lt $deadline)
    return $false
}

function Set-WatchdogTask([bool]$enable) {
    if ($SkipWatchdogToggle) { return $null }
    $verb = if ($enable) { '/ENABLE' } else { '/DISABLE' }
    $rc = Invoke-Native {
        schtasks /Change /TN $WatchdogTask $verb 2>&1 |
            ForEach-Object { Write-Host "  [schtasks] $_" }
    }
    if ($rc -ne 0) {
        Warn "看门狗任务 $verb 失败——无此任务的机器可加 -SkipWatchdogToggle"
        return $false
    }
    Say ("看门狗任务已{0}（{1}）" -f ($(if ($enable) { '恢复' } else { '暂停' })), $WatchdogTask)
    return $true
}

function Show-OverlayKeyDiff {
    # 只读提示：通译 overlay 有、智聊 overlay 没有的顶层键（config 不随数据合并，
    # 翻译引擎端点/per_lang_order 等运营键要人工确认是否需要并入智聊 overlay）。
    $srcOv = Join-Path $SourceData 'config\config.local.yaml'
    $dstOv = Join-Path $TargetData 'config\config.local.yaml'
    if (-not ((Test-Path $srcOv) -and (Test-Path $dstOv))) { return }
    $code = @'
import sys, yaml
src = yaml.safe_load(open(sys.argv[1], encoding='utf-8')) or {}
dst = yaml.safe_load(open(sys.argv[2], encoding='utf-8')) or {}
def walk(d, prefix=''):
    out = set()
    for k, v in (d or {}).items():
        p = prefix + str(k)
        out.add(p)
        if isinstance(v, dict) and prefix.count('.') < 1:
            out |= walk(v, p + '.')
    return out
only = sorted(walk(src) - walk(dst))
print('\n'.join(only) if only else '(none)')
'@
    $tmp = Join-Path $env:TEMP 'cutover_overlay_diff.py'
    Set-Content -LiteralPath $tmp -Value $code -Encoding UTF8
    Say '通译 overlay 独有键（config 不随数据合并，需人工评估是否并入智聊 overlay）:'
    Warn '注意：通译的裁剪键（*.enabled: false 之类）是「关功能」语义，并入会关掉智聊的功能——只挑真正的运营配置（translation.engines 端点等）评估'
    & $Py $tmp $srcOv $dstOv 2>&1 | ForEach-Object { Write-Host "    $_" }
    Remove-Item $tmp -Force -ErrorAction SilentlyContinue
}

# ════════════════════════════════════════════════════════════════════════════
# 回滚模式
# ════════════════════════════════════════════════════════════════════════════
if ($Rollback) {
    if (-not (Test-Path -LiteralPath $BackupRoot)) { Die "无备份目录 $BackupRoot" 1 }
    $bak = if ($BackupId) {
        Join-Path $BackupRoot $BackupId
    } else {
        (Get-ChildItem -LiteralPath $BackupRoot -Directory | Sort-Object Name -Descending |
            Select-Object -First 1).FullName
    }
    if (-not ($bak -and (Test-Path -LiteralPath $bak))) { Die "备份不存在: $bak" 1 }
    $bakZhiliao = Join-Path $bak 'zhiliao_config'
    if (-not (Test-Path -LiteralPath $bakZhiliao)) { Die "备份内缺 zhiliao_config: $bak" 1 }

    Say "回滚开始，备份点 = $bak"
    if (-not $Force) {
        $ans = Read-Host '将【镜像还原】智聊 config 目录并重新拉起双实例。输入 ROLLBACK 确认'
        if ($ans -cne 'ROLLBACK') { Die '未确认，退出' 3 }
    }
    $wdToggled = Set-WatchdogTask $false
    try {
        try { Stop-One 'zhiliao' } catch { Warn "停智聊: $($_.Exception.Message)（可能本就未跑）" }
        try { Stop-One 'tongyi' }  catch { Warn "停通译: $($_.Exception.Message)（可能本就未跑）" }
        Say '镜像还原智聊 config …'
        Invoke-Robocopy $bakZhiliao (Join-Path $TargetData 'config') -Mirror
        $flag = Join-Path $RetiredDir 'tongyi.flag'
        if (Test-Path -LiteralPath $flag) {
            Remove-Item $flag -Force
            Say '已删除通译退役旗标'
        }
        Start-One 'zhiliao' $TargetData
        if (-not (Wait-Login $ZhiliaoLogin $ReadyWaitSec)) { throw "智聊 /login 超时（$ZhiliaoLogin）" }
        Start-One 'tongyi' $SourceData
        if (-not (Wait-Login $TongyiLogin $ReadyWaitSec)) { throw "通译 /login 超时（$TongyiLogin）" }
    } catch {
        if ($null -ne $wdToggled -and $wdToggled) { Set-WatchdogTask $true | Out-Null }
        Die "回滚失败: $($_.Exception.Message)（备份仍在 $bak，可人工按 README §8 处置）" 2
    }
    if ($null -ne $wdToggled -and $wdToggled) { Set-WatchdogTask $true | Out-Null }
    Say '✅ 回滚完成：双实例已恢复切换前形态（备份保留不删）'
    exit 0
}

# ════════════════════════════════════════════════════════════════════════════
# 阶段 0：预检（只读；-Execute 与否都跑）
# ════════════════════════════════════════════════════════════════════════════
Say ("模式 = {0}" -f ($(if ($Execute) { 'EXECUTE（真切）' } else { 'DRY-RUN（只预检，零副作用）' })))

foreach ($p in @($SourceData, $TargetData)) {
    if (-not (Test-Path -LiteralPath (Join-Path $p 'config\config.yaml'))) {
        Die "数据根缺 config\config.yaml: $p" 1
    }
}
if (-not (Test-Path -LiteralPath $MergeCli)) { Die "迁移 CLI 缺失: $MergeCli" 1 }
try { & $Py --version | Out-Null } catch { Die "python 不可用（$Py）；用 -PythonExe 指定" 1 }

# registry.key：目标必须有；源没有仅在「源库存在加密凭证」时会被 merge CLI 拦（干跑即暴露）
if (-not (Test-Path -LiteralPath (Join-Path $TargetData 'config\registry.key'))) {
    Warn '目标 registry.key 缺失——若目标从未启用账号编排属正常；换密将退化为「按目标无密」处理'
}

# 磁盘余量：备份 = 两边 config 目录全量，要求剩余 ≥ 1.2×
$needBytes = (Get-DirBytes (Join-Path $SourceData 'config')) +
             (Get-DirBytes (Join-Path $TargetData 'config'))
if (-not (Test-Path -LiteralPath $OpsDir)) { New-Item -ItemType Directory -Force -Path $OpsDir | Out-Null }
$free = (Get-PSDrive -Name ($OpsDir.Substring(0, 1))).Free
if ($free -lt ($needBytes * 1.2)) {
    Die ("磁盘余量不足：需 ~{0:N0} MB（含 1.2x 裕量），仅剩 {1:N0} MB" -f
        (($needBytes * 1.2) / 1MB), ($free / 1MB)) 1
}
Say ("备份体量 ~{0:N0} MB，磁盘剩余 {1:N0} MB —— OK" -f ($needBytes / 1MB), ($free / 1MB))

# 双实例现状（只读探测，刷新 .ops\last_status.json）
& powershell.exe -NoProfile -ExecutionPolicy Bypass `
    -File (Join-Path $PSScriptRoot 'status_instances.ps1') `
    -Json -SnapshotPath (Join-Path $OpsDir 'last_status.json') | Out-Null
Say ("双实例状态已刷新（{0}\last_status.json）" -f $OpsDir)

# merge 干跑（含凭证换密可行性 + 全表 ok 校验；报告落预检临时文件）
$preReport = Join-Path $env:TEMP ("cutover_preflight_{0}.json" -f (Get-Date -Format 'yyyyMMdd_HHmmss'))
Say '合并干跑（只读）…'
$rc = Invoke-MergeCli -ReportPath $preReport
if ($rc -ne 0) { Die "干跑失败（rc=$rc）——修完再来；生产未动" 1 }
$pending = Get-ReportInserts $preReport
Say ("干跑通过：待插入 {0} 行（报告 {1}）" -f $pending, $preReport)

Show-OverlayKeyDiff

if (-not $Execute) {
    Say '预检全部通过。真切请在低峰窗口（02:00-03:00）加 -Execute 重跑。'
    exit 0
}

# ════════════════════════════════════════════════════════════════════════════
# 真切段（-Execute）
# ════════════════════════════════════════════════════════════════════════════
$hour = (Get-Date).Hour
if (($hour -lt 1 -or $hour -ge 4) -and -not $Force) {
    Warn ("当前 {0} 点不在低峰窗口（建议 02:00-03:00）" -f $hour)
}
if (-not $Force) {
    $ans = Read-Host '即将停两台实例并把通译数据并入智聊（有备份+回滚）。输入 MERGE 确认'
    if ($ans -cne 'MERGE') { Die '未确认，退出（生产未动）' 3 }
}

$ts = Get-Date -Format 'yyyyMMdd_HHmmss'
$bakDir = Join-Path $BackupRoot $ts
New-Item -ItemType Directory -Force -Path $bakDir, $RetiredDir | Out-Null

$wdToggled = Set-WatchdogTask $false
if ($wdToggled -eq $false) {
    # 真切段安全闸：看门狗还活着就动手，5min 节拍内可能把停机中的实例拉活
    # （合并写库期间被写入 = 一致性破坏）。无此任务的机器用 -SkipWatchdogToggle。
    Die '看门狗任务暂停失败，拒绝继续（生产未动）' 1
}
$failed = $false
try {
    # 1) 停两台（tongyi 先停：源侧从此只读）
    Stop-One 'tongyi'
    Stop-One 'zhiliao'

    # 2) 备份两边 config（停机后拷贝 = wal/shm 一致快照）
    Say "备份 → $bakDir"
    Invoke-Robocopy (Join-Path $SourceData 'config') (Join-Path $bakDir 'tongyi_config')
    Invoke-Robocopy (Join-Path $TargetData 'config') (Join-Path $bakDir 'zhiliao_config')
    $gitHead = ''
    try { $gitHead = (git -C $RepoRoot rev-parse HEAD 2>$null) } catch {}
    @{
        ts = $ts; source = $SourceData; target = $TargetData
        business_line = $BusinessLine; git_head = "$gitHead"
        host = $env:COMPUTERNAME
    } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $bakDir 'manifest.json') -Encoding UTF8
    Say '备份完成（含 manifest.json）'

    # 3) 实写合并
    $applyReport = Join-Path $bakDir 'merge_report.json'
    Say '实写合并 --apply …'
    $rc = Invoke-MergeCli -Apply -ReportPath $applyReport
    if ($rc -ne 0) { throw "合并实写失败（rc=$rc，报告 $applyReport）" }

    # 4) 收敛校验：复跑干跑，待插入必须为 0
    $verifyReport = Join-Path $bakDir 'merge_verify.json'
    Say '收敛校验（复跑干跑，期望待插入=0）…'
    $rc = Invoke-MergeCli -ReportPath $verifyReport
    if ($rc -ne 0) { throw "校验干跑失败（rc=$rc）" }
    $left = Get-ReportInserts $verifyReport
    if ($left -ne 0) { throw "未收敛：复跑仍有 $left 行待插入（报告 $verifyReport）" }
    Say '收敛校验通过（0 行残留）'

    # 5) 通译退役旗标（watchdog 见旗标即跳过；复活 = 删文件 或 -Rollback）
    @{
        ts = $ts; reason = 'merged-into-zhiliao'; backup = $bakDir
    } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $RetiredDir 'tongyi.flag') -Encoding UTF8
    Say '已写通译退役旗标（watchdog 不再拉活通译）'

    # 6) 起智聊 + 就绪门
    Start-One 'zhiliao' $TargetData
    if (-not (Wait-Login $ZhiliaoLogin $ReadyWaitSec)) {
        throw "智聊 /login 未在 ${ReadyWaitSec}s 内就绪——立即人工核查 boot 日志；如需回滚: cutover_merge.ps1 -Rollback"
    }
    Say '智聊 /login 200 —— 就绪'

    # 7) 刷新状态快照（watchdog 下一拍读到的就是切换后事实）
    & powershell.exe -NoProfile -ExecutionPolicy Bypass `
        -File (Join-Path $PSScriptRoot 'status_instances.ps1') `
        -Json -SnapshotPath (Join-Path $OpsDir 'last_status.json') | Out-Null
} catch {
    $failed = $true
    Warn "执行失败: $($_.Exception.Message)"
} finally {
    if ($null -ne $wdToggled -and $wdToggled) { Set-WatchdogTask $true | Out-Null }
}

if ($failed) {
    Die ("切换未完成。备份在 {0}；回滚: powershell -File deploy\instances\cutover_merge.ps1 -Rollback -BackupId {1}" -f $bakDir, $ts) 2
}

Say '════════════════════════════════════════════════════════'
Say '✅ 切换完成。收尾清单（人工）：'
Say ("  1. 备份+报告: {0}（永不自动删；观察期后人工归档）" -f $bakDir)
Say '  2. 通译保持停机+退役旗标；数据根原地保留 = 二级回滚点'
Say '  3. 通译坐席改用智聊地址登录（web_users 已并入；同名冲突见 merge_report）'
Say '  4. 18899 端口对外入口（反代/收藏夹）改指 18799 或下线'
Say '  5. 预检打印的「通译 overlay 独有键」评估并入智聊 config.local.yaml（30s 热重载）'
Say '  6. 观察期后: deploy\stack.json 的 chengjie_tongyi 条目 enabled=false'
Say ("  7. 回滚（观察期内如需）: cutover_merge.ps1 -Rollback -BackupId {0}" -f $ts)
exit 0
