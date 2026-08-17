# run_config_snapshot.ps1 — 实例配置目录写保护快照守护薄壳（deploy/cron，.117 chengjie 双实例）
#
# 职责：对每个实例 config 目录维护一个本地 git 快照仓（.git 直接住在 config 目录内），每轮：
#   git add -A → 有暂存变更才 git commit -m "snapshot <ISO时间戳>"（无变更零空提交，退出 0），
#   提交后把 diff --stat 摘要打进日志（计划任务动作已重定向进 logs\cron\<任务名>.log）。
# 用途 = 多写者竞态兜底 + 误改回滚证据链（实施18 复盘：agent 写回旧内容静默覆盖 DeepSeek
# 切换，无人察觉直到日志巡检——有了每 10 分钟快照，任何配置文本变化都留痕，可 git log/diff
# 溯源、可单文件 checkout 回滚；命令速查见 deploy\cron\README.md §3.4）。
#
# 纪律：
#   - config.local.yaml 含 auth_token/api_key：快照仓永远只在实例数据根本地、绝不配 remote、
#     绝不能进 D:\workspace\boundless 这类会 push 的仓库；
#   - 只快照配置文本（yaml/json/key 等）：初始化写 .gitignore 排除 *.db/*.db-shm/*.db-wal/
#     *.db.bak*/*.log/logs/（SQLite 与日志是运行数据）以及 purged_trash/（清除回收站进了
#     git 历史就物理删不净，违背客户删除权兑现，README §4）；
#   - 历史不清理（配置文本极小，十年也没多少，不做保留策略）；
#   - 只读对待运行进程：本壳只加 .git/.gitignore，绝不改写配置内容本身。
#
# 退出码：0 = 本轮成功（含「无变更不提交」的正常空转）
#         1 = 有目录快照失败（git init/add/commit 报错；已成功目录不受影响，下轮幂等重试）
#         2 = 配置错误（git 不可用 / 配置目录不存在）
#
# 用法（计划任务由 install_tasks.ps1 注册 Boundless-chengjie-config_snapshot；人工排障可直接跑）：
#   powershell -ExecutionPolicy Bypass -File deploy\cron\run_config_snapshot.ps1
#   powershell -ExecutionPolicy Bypass -File deploy\cron\run_config_snapshot.ps1 -ConfigDirs "D:\chengjie-instances\zhiliao\data\config"

[CmdletBinding()]
param(
    [string[]]$ConfigDirs = @(),    # 配置目录清单（逗号分隔亦可）；缺省 = .117 双实例生产 config 目录
    [string]$GitExe = 'git'         # git 全路径覆盖（SYSTEM 账户看机器级 PATH；不在则传全路径）
)

$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}

$RepoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)

function Say([string]$msg) { Write-Host ("[run_config_snapshot {0:yyyy-MM-dd HH:mm:ss}] {1}" -f (Get-Date), $msg) }
function Die([string]$msg, [int]$code) { Say "错误: $msg"; exit $code }

function Resolve-RepoPath([string]$p) {
    if ([IO.Path]::IsPathRooted($p)) { return [IO.Path]::GetFullPath($p) }
    return [IO.Path]::GetFullPath((Join-Path $RepoRoot $p))
}

# 目录 → 实例标签（…\zhiliao\data\config → zhiliao），仅为日志可读性；与 install_tasks 的 Root-Tag 同思路
function Dir-Tag([string]$p) {
    $cur = $p
    foreach ($skip in @('config', 'data')) {
        if ((Split-Path -Leaf $cur) -ieq $skip) { $cur = Split-Path -Parent $cur }
    }
    return (Split-Path -Leaf $cur)
}

# 缺省 = .117 双实例生产 config 目录（迁移后形态在仓库外，migrate_117_runbook §4；
# 与 install_tasks.ps1 的 $Defaults.chengjie.cfg 保持同步——改这里记得同步那边）
$DefaultConfigDirs = @('D:\chengjie-instances\zhiliao\data\config',
                       'D:\chengjie-instances\tongyi\data\config')

# .gitignore 内容（仅缺失时写入，已存在不覆盖——尊重人工调整）
$IgnoreLines = @(
    '# 快照只管配置文本（yaml/json/key 等）；SQLite/WAL/日志是运行数据，不进版本库',
    '*.db',
    '*.db-shm',
    '*.db-wal',
    '*.db.bak*',
    '*.log',
    'logs/',
    '# 清除回收站绝不能进快照历史：进了 git 物理删不净，违背客户删除权兑现（README §4）',
    'purged_trash/'
)

# 统一 git 调用：-C 定目录；safe.directory 用命令行护栏带上——计划任务跑 SYSTEM 而快照仓归
# Administrator 所有，git 的 dubious ownership 检查会拒绝跨属主操作；命令行 -c 属 protected
# config（git ≥2.38 认），只豁免本次调用、不落盘任何全局配置。
# ⚠ PS5.1 下 $ErrorActionPreference=Stop 叠加原生命令 2>&1 会把 stderr 行误升成终止错误，
#   调用期间临时降为 Continue（函数级 preference 只影响本函数内部）。
function Invoke-Git([string]$dir, [string[]]$gitArgs) {
    $safe = $dir -replace '\\', '/'
    $ErrorActionPreference = 'Continue'
    $out = & $GitCmd.Source -C $dir -c "safe.directory=$safe" @gitArgs 2>&1
    $code = $LASTEXITCODE
    foreach ($line in @($out)) { if ($null -ne $line -and "$line" -ne '') { Write-Host ("    | {0}" -f $line) } }
    return $code
}

# ── 配置解析（缺什么错什么，绝不吞错静默跑）─────────────────────────
$dirs = @($ConfigDirs | ForEach-Object { $_ -split ',' } | ForEach-Object { $_.Trim() } | Where-Object { $_ })
if (-not $dirs.Count) { $dirs = $DefaultConfigDirs }
$dirs = @($dirs | ForEach-Object { Resolve-RepoPath $_ })

$GitCmd = Get-Command $GitExe -ErrorAction SilentlyContinue
if (-not $GitCmd) { Die "git 不可用（'$GitExe' 不在 PATH；SYSTEM 账户任务需机器级 PATH 含 git，或传 -GitExe 全路径）" 2 }

$missing = @($dirs | Where-Object { -not (Test-Path -LiteralPath $_ -PathType Container) })
if ($missing.Count) {
    Die ("配置目录不存在：{0}（.117 迁移后形态 = D:\chengjie-instances\<实例>\data\config，见 deploy\instances\migrate_117_runbook.md）" -f ($missing -join '；')) 2
}

# ── 逐目录快照（退出码取最差；单目录失败不拖累其余目录）──────────────
$worst = 0
$committed = 0
$stamp = Get-Date -Format "yyyy-MM-dd'T'HH:mm:sszzz"
foreach ($dir in $dirs) {
    Say ("── [{0}] {1}" -f (Dir-Tag $dir), $dir)

    # ① 首轮：初始化本地快照仓（绝不配 remote）+ 写 .gitignore（先于首次 add，db 等永不进索引）
    if (-not (Test-Path -LiteralPath (Join-Path $dir '.git'))) {
        if ((Invoke-Git $dir @('init', '-q', '-b', 'main')) -ne 0) { Say '快照仓初始化失败（git init）'; $worst = 1; continue }
        Say '首轮：已初始化本地快照仓（git init -b main，无 remote）'
    }
    $ignoreFile = Join-Path $dir '.gitignore'
    if (-not (Test-Path -LiteralPath $ignoreFile)) {
        [IO.File]::WriteAllText($ignoreFile, (($IgnoreLines -join "`n") + "`n"), [Text.UTF8Encoding]::new($false))
        Say '已写 .gitignore（db/wal/shm/日志/purged_trash 一律不进快照）'
    }

    # ② 快照身份与行尾策略每轮幂等重设（自愈：坏守护比没守护更危险）；
    #    autocrlf=false 保证快照与回滚字节级保真，不被换行转换掺和
    $null = Invoke-Git $dir @('config', 'user.name', 'config-snapshot')
    $null = Invoke-Git $dir @('config', 'user.email', 'snapshot@local')
    $null = Invoke-Git $dir @('config', 'core.autocrlf', 'false')

    # ③ add -A → 有暂存变更才 commit（diff --cached --quiet：0=无变更 1=有变更 其他=探测失败）
    if ((Invoke-Git $dir @('add', '-A')) -ne 0) { Say 'git add 失败'; $worst = 1; continue }
    $probe = Invoke-Git $dir @('diff', '--cached', '--quiet')
    if ($probe -eq 0) { Say '无变更：不产生空提交（正常空转）'; continue }
    if ($probe -ne 1) { Say "暂存探测失败（git diff --cached 退出码 $probe）"; $worst = 1; continue }

    if ((Invoke-Git $dir @('commit', '-q', '-m', "snapshot $stamp")) -ne 0) { Say 'git commit 失败'; $worst = 1; continue }
    $committed++
    Say ("已提交快照: snapshot {0}；本次 diff --stat 摘要：" -f $stamp)
    $null = Invoke-Git $dir @('show', '--stat', '--oneline', 'HEAD')
}

if ($worst -eq 0) { Say ("本轮完成（退出码 0）：{0} 个目录，{1} 个有新快照提交" -f $dirs.Count, $committed) }
else              { Say "本轮有失败（退出码 $worst）：看上方对应目录的 git 输出定位；下轮幂等重试" }
exit $worst
