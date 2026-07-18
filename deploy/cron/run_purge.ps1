# run_purge.ps1 — 人设清除 purge agent 计划任务薄壳（deploy/cron，P5 运营接线）
#
# 职责：按引擎分支调用对应执行器（单轮 --once；缺省 dry-run 演练，-Commit 才真删+回执）：
#   avatarhub → python engines\avatarhub\persona_purge_agent.py         --once [--commit] --input <根>
#   chengjie  → python engines\chengjie\scripts\persona_purge_agent.py --once [--commit] --input <根>
#   huoke     → python engines\huoke\src\persona_purge_agent.py        --once [--commit] --input <根>
# 密钥经环境变量 EVENT_INGEST_KEY 传给子进程（绝不上命令行）。
#
# 双实例多根（README §3 / migrate_117_runbook §4.4）：
#   chengjie 的 ack 是引擎级回执，必须全部实例数据根删净才能回执。引擎侧已提供
#   --data-roots "R1;R2"（分号分隔；agent 保证全根成功才 ack）。本壳运行时探测
#   agent 脚本现状：有该旗标 → 多根合成一次 --data-roots 调用；没有（旧版仓库）→
#   多根 + -Commit 以退出码 3 守门拒绝（绝不逐根跑 commit：删完首根就 ack = 其余
#   根成漏网），dry-run 无回执、允许逐根演练。avatarhub/huoke 无多根语义，同守门。
#
# 退出码：0 = 本轮成功（含「无待办指令」「拉取失败 fail-silent」——avatarhub 版语义）
#         1 = 有指令执行失败未回执（agent 透传；已删项不回滚，下轮幂等重试）
#         2 = 配置错误（缺密钥 / 数据根不存在 / python 不可用）
#         3 = 多根 + -Commit 守门拒绝（agent 不支持 --data-roots 的旧版/异引擎）
#
# 用法（计划任务由 install_tasks.ps1 注册；人工排障可直接跑）：
#   powershell -ExecutionPolicy Bypass -File deploy\cron\run_purge.ps1 -Engine avatarhub            # 演练
#   powershell -ExecutionPolicy Bypass -File deploy\cron\run_purge.ps1 -Engine avatarhub -Commit    # 真删
#   powershell -ExecutionPolicy Bypass -File deploy\cron\run_purge.ps1 -Engine chengjie -DataRoots "engines\chengjie" -Commit   # 迁移前单实例期

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('avatarhub', 'chengjie', 'huoke')]
    [string]$Engine,
    [string[]]$DataRoots = @(),     # 引擎根/实例数据根（逗号分隔亦可）；chengjie 缺省双实例数据根
    [string]$BaseUrl   = '',        # 注册表基址（缺省让 agent 走 env PERSONA_SYNC_BASE / https://bd2026.cc）
    [string]$IngestKey = '',        # 机器密钥（缺省 env EVENT_INGEST_KEY；仅经环境变量下传）
    [string]$PythonExe = 'python',
    [switch]$Commit                 # 缺省 dry-run 演练（agent 原生缺省）；-Commit = 软删入 trash + 回执
)

$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}

$RepoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)

function Say([string]$msg) { Write-Host ("[run_purge {0:yyyy-MM-dd HH:mm:ss}] {1}" -f (Get-Date), $msg) }
function Die([string]$msg, [int]$code) { Say "错误: $msg"; exit $code }

function Resolve-RepoPath([string]$p) {
    if ([IO.Path]::IsPathRooted($p)) { return [IO.Path]::GetFullPath($p) }
    return [IO.Path]::GetFullPath((Join-Path $RepoRoot $p))
}

# ── 引擎分支表（脚本位置 + 缺省数据根）───────────────────────────────
$AgentScript = @{
    avatarhub = 'engines\avatarhub\persona_purge_agent.py'
    chengjie  = 'engines\chengjie\scripts\persona_purge_agent.py'
    huoke     = 'engines\huoke\src\persona_purge_agent.py'
}[$Engine]
$DefaultRoots = @{
    avatarhub = @('engines\avatarhub')
    chengjie  = @('deploy\instances\zhiliao\data', 'deploy\instances\tongyi\data')
    huoke     = @('engines\huoke')
}[$Engine]

# ── 配置解析 ─────────────────────────────────────────────────────────
$roots = @($DataRoots | ForEach-Object { $_ -split ',' } | ForEach-Object { $_.Trim() } | Where-Object { $_ })
if (-not $roots.Count) { $roots = $DefaultRoots }
$roots = @($roots | ForEach-Object { Resolve-RepoPath $_ })

$missing = @($roots | Where-Object { -not (Test-Path -LiteralPath $_ -PathType Container) })
if ($missing.Count) {
    Die ("数据根不存在：{0}。chengjie 迁移前的现网单实例期请传 -DataRoots `"engines\chengjie`"（README §3）" -f ($missing -join '；')) 2
}

$agentPy = Join-Path $RepoRoot $AgentScript
if (-not (Test-Path -LiteralPath $agentPy)) { Die "找不到执行器脚本：$agentPy（仓库不完整？）" 2 }

$py = Get-Command $PythonExe -ErrorAction SilentlyContinue
if (-not $py) { Die "python 不可用（'$PythonExe' 不在 PATH；SYSTEM 账户任务需机器级 PATH，或安装器传 -PythonExe 全路径）" 2 }

$key = $IngestKey
if (-not $key) { $key = [string]$env:EVENT_INGEST_KEY }
if (-not $key) {
    Die '缺少机器密钥：设机器级环境变量 EVENT_INGEST_KEY（推荐，见 deploy\cron\README.md §1），或传 -IngestKey' 2
}

# ── 多根处置：探测 agent 是否支持 --data-roots（读脚本现状，不猜语法）─
# chengjie 版已提供 --data-roots "R1;R2"（分号分隔，agent 全根成功才 ack）；
# 旧版/异引擎多根 + commit 一律守门拒绝，防「删完首根就 ack、其余根漏删」。
$supportsDataRoots = $false
if ($roots.Count -gt 1) {
    $supportsDataRoots = ([IO.File]::ReadAllText($agentPy)).Contains('--data-roots')
    if (-not $supportsDataRoots -and $Commit) {
        Say ("守门拒绝：{0} 执行器（{1}）不支持 --data-roots，而传入了 {2} 个数据根 + -Commit。" -f $Engine, $AgentScript, $roots.Count)
        Say 'ack 是引擎级回执，逐根 commit 会在删完首根后就回执，其余根成漏网（PERSONA_BUS §5.3 / runbook §4.4）。'
        Say '可：a) 不带 -Commit 逐根演练；b) 暂以单根 -DataRoots 运行；c) 等引擎侧多根旗标落地。'
        exit 3
    }
}

# ── 执行（退出码取最差）──────────────────────────────────────────────
Set-Location $RepoRoot
$env:EVENT_INGEST_KEY = $key
$worst = 0
$mode = if ($Commit) { 'commit（软删入 trash + 回执）' } else { 'dry-run 演练（不删不回执）' }

# 调用清单：多根且支持 --data-roots → 合成一次调用（agent 保证全根删净才 ack）；
# 否则逐根 --input（多根走到这里必是 dry-run，已被上方守门保证）
$invocations = @()
if ($roots.Count -gt 1 -and $supportsDataRoots) {
    $invocations += , @('--data-roots', ($roots -join ';'))
    Say ("[{0}] 模式={1} 多实例数据根 x{2}（--data-roots，引擎级回执）" -f $Engine, $mode, $roots.Count)
} else {
    foreach ($root in $roots) { $invocations += , @('--input', $root) }
}

foreach ($extra in $invocations) {
    $argList = @($agentPy, '--once') + $extra
    if ($Commit)  { $argList += '--commit' }
    if ($BaseUrl) { $argList += @('--base', $BaseUrl) }
    Say ("命令: {0} {1}  [密钥经 EVENT_INGEST_KEY 环境变量传入，已掩码]" -f $py.Source, ($argList -join ' '))
    & $py.Source @argList
    $code = $LASTEXITCODE
    if ($code -ne 0) { Say "本次调用退出码 $code" }
    if ($code -gt $worst) { $worst = $code }
}

if ($worst -eq 0) { Say '本轮完成（退出码 0）' }
else              { Say "本轮有失败（退出码 $worst）：未回执的指令下轮幂等重试；连续失败请人工看日志" }
exit $worst
