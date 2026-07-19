# install_tasks.ps1 — 引擎机定时任务安装器（deploy/cron，P4/P5 运营接线）
#
# 用法（缺省 = WhatIf 演练：只打印将注册的任务定义与完整命令行，绝不注册）：
#   powershell -ExecutionPolicy Bypass -File deploy\cron\install_tasks.ps1 -Engine avatarhub          # .176 演练
#   powershell -ExecutionPolicy Bypass -File deploy\cron\install_tasks.ps1 -Engine chengjie           # .117 演练（缺省双实例）
#   powershell -ExecutionPolicy Bypass -File deploy\cron\install_tasks.ps1 -Engine huoke -Execute     # .198 真注册（管理员）
#   … -Tasks uploader,purge            只装部分任务
#   … -Tasks config_snapshot           只装配置快照守护（仅 chengjie；缺省任务集已自动含，见下）
#   … -WithKpiWeekly                   追加 KPI 周报任务（website 所在 Windows 机可选装；等价 -Tasks …,kpi_weekly）
#   … -SpoolDirs "a,b" -DataRoots "c"  覆盖缺省（-File 语义下多值用逗号串）
#   … -RunAs CurrentUser               S4U 运行（不存密码），缺省 SYSTEM
#   … -ExportTransfer                  export 任务带「传输到 VPS + 导入」两段（先打通 SSH）
#
# 设计要点：
#   - 任务放计划任务文件夹 \Boundless\，名 Boundless-<engine>-<task>[-<实例>]；
#     kpi_weekly 例外：名 Boundless-website-kpi_weekly（全矩阵报表，不属任何引擎，README §2）；
#   - 动作 = cmd.exe /d /c <powershell -File 壳脚本 …> >> logs\cron\<任务名>.log 2>&1
#     （logs/ 已被根 .gitignore 忽略；cmd 链先 mkdir 日志目录，重建仓库后任务不哑火）；
#   - 工作目录 = 仓库根；MultipleInstances=IgnoreNew（上一轮没跑完就跳过本轮，防叠跑）；
#   - 密钥缺省不进任务定义（任务 XML 本机管理员可读）：运行时由壳脚本读机器级
#     EVENT_INGEST_KEY；-IngestKey 显式传入才嵌进参数（打印时掩码，README §4）；
#   - 触发器：uploader 每 5 分钟 / purge 每 10 分钟 / grants_sync 每 30 分钟 /
#     config_snapshot 每 10 分钟（仅 chengjie，未显式 -Tasks 时自动进 chengjie 任务集；
#     avatarhub/huoke 无实例 config 目录形态，显式传入也跳过）/
#     watchdog 每 5 分钟（仅 chengjie，双实例探活自愈，实施29；同 config_snapshot 自动进
#     chengjie 任务集；账户固定装机用户 S4U+Highest——引擎继承 watchdog 的账户，SYSTEM 会让
#     引擎改以 SYSTEM 身份跑（用户身份漂移），故不吃 -RunAs）
#     （以上均 -Once + Repetition，持续 3650 天）；export 每日 03:30（-Daily）；
#     kpi_weekly 每周一 09:00（-Weekly）。
#
# 退出码：0 = 演练完成 / 全部注册成功   1 = 注册失败（需管理员 PowerShell）   2 = 参数错误

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('avatarhub', 'chengjie', 'huoke')]
    [string]$Engine,
    [string[]]$Tasks = @('uploader', 'purge', 'export', 'grants_sync'),   # 多选：uploader/purge/export/grants_sync/config_snapshot/watchdog/kpi_weekly（逗号分隔亦可；config_snapshot 与 watchdog 未显式 -Tasks 时自动进 chengjie 任务集，kpi_weekly 用 -WithKpiWeekly 追加）
    [string]$BaseUrl   = '',        # 集团基址覆盖（缺省壳脚本走 env PERSONA_SYNC_BASE / https://bd2026.cc）
    [string]$IngestKey = '',        # 显式嵌密钥进任务定义（不推荐；缺省运行时读机器级 EVENT_INGEST_KEY）
    [string[]]$SpoolDirs = @(),     # uploader spool 目录（每目录一个任务实例）；缺省按引擎（见 $Defaults）
    [string[]]$DataRoots = @(),     # purge/export 数据根；缺省按引擎（chengjie=双实例数据根）
    [string[]]$ConfigDirs = @(),    # config_snapshot 配置目录清单；缺省按引擎（chengjie=双实例生产 config 目录，其余引擎无此形态）
    [ValidateSet('SYSTEM', 'CurrentUser')]
    [string]$RunAs = 'SYSTEM',      # 运行账户：SYSTEM（缺省）或当前用户（S4U，不存密码）
    [string]$PythonExe = '',        # 传给壳脚本的 python 全路径（SYSTEM 账户 PATH 缺 python 时用）
    [string]$NodeExe   = '',        # 传给 kpi_weekly 壳的 node 全路径（SYSTEM 账户 PATH 缺 node 时用）
    [string]$GitExe    = '',        # 传给 config_snapshot 壳的 git 全路径（SYSTEM 账户 PATH 缺 git 时用）
    [int]$UploaderEveryMinutes       = 5,
    [int]$PurgeEveryMinutes          = 10,
    [int]$GrantsSyncEveryMinutes     = 30,
    [int]$ConfigSnapshotEveryMinutes = 10,
    [int]$WatchdogEveryMinutes       = 5,
    [string]$ExportDailyAt       = '03:30',
    [string]$KpiWeeklyAt         = '09:00', # 每周一（-Weekly Monday）
    [switch]$WithKpiWeekly,         # 追加 KPI 周报任务（website 所在 Windows 机可选装；VPS 用 crontab，README §5.3）
    [switch]$ExportTransfer,        # export 任务补第 3 段（-Transfer -Import 透传壳脚本）
    [switch]$Execute                # 缺省 WhatIf 演练；-Execute 才 Register-ScheduledTask
)

$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}

$RepoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$TaskPath = '\Boundless\'
$LogDir   = Join-Path $RepoRoot 'logs\cron'
$PsExe    = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'

function Say([string]$msg)  { Write-Host "[install_tasks] $msg" }
function Warn([string]$msg) { Write-Host "[install_tasks] 警告: $msg" -ForegroundColor Yellow }
function Die([string]$msg, [int]$code) { Write-Host "[install_tasks] 错误: $msg" -ForegroundColor Red; exit $code }

function Resolve-RepoPath([string]$p) {
    if ([IO.Path]::IsPathRooted($p)) { return [IO.Path]::GetFullPath($p) }
    return [IO.Path]::GetFullPath((Join-Path $RepoRoot $p))
}

# 目录 → 任务名标签：叶子为 data 时取上一级（deploy\instances\zhiliao\data → zhiliao），
# 叶子为 spool 时逐级上溯（…\zhiliao\data\events\spool → zhiliao）。与 run_export.ps1 同规则。
function Root-Tag([string]$p) {
    $cur = $p
    foreach ($skip in @('spool', 'events', 'data')) {
        if ((Split-Path -Leaf $cur) -ieq $skip) { $cur = Split-Path -Parent $cur }
    }
    return (Split-Path -Leaf $cur)
}

# ── 参数归一 ─────────────────────────────────────────────────────────
$Tasks = @($Tasks | ForEach-Object { $_ -split ',' } | ForEach-Object { $_.Trim().ToLower() } | Where-Object { $_ })
# config_snapshot / watchdog 属 chengjie 缺省任务集（未显式 -Tasks 时自动带上；显式 -Tasks 尊重调用者清单）
if ($Engine -eq 'chengjie' -and -not $PSBoundParameters.ContainsKey('Tasks')) {
    foreach ($extra in @('config_snapshot', 'watchdog')) {
        if ($extra -notin $Tasks) { $Tasks += $extra }
    }
}
if ($WithKpiWeekly -and 'kpi_weekly' -notin $Tasks) { $Tasks += 'kpi_weekly' }
$bad = @($Tasks | Where-Object { $_ -notin @('uploader', 'purge', 'export', 'grants_sync', 'config_snapshot', 'watchdog', 'kpi_weekly') })
if ($bad.Count)    { Die "未知任务类型: $($bad -join ', ')（可选 uploader/purge/export/grants_sync/config_snapshot/watchdog/kpi_weekly）" 2 }
if (-not $Tasks.Count) { Die '-Tasks 至少给一个任务类型' 2 }
# 配置快照/探活自愈仅 chengjie 有双实例形态：其他引擎显式传入也跳过（不算参数错误，装其余任务照常）
foreach ($cjOnly in @('config_snapshot', 'watchdog')) {
    if ($cjOnly -in $Tasks -and $Engine -ne 'chengjie') {
        Warn "$cjOnly 仅适用 chengjie（双实例形态）；$Engine 无此形态，已跳过该任务类型"
        $Tasks = @($Tasks | Where-Object { $_ -ne $cjOnly })
        if (-not $Tasks.Count) { Die "剔除 $cjOnly 后无任务可装" 2 }
    }
}

$Defaults = @{
    avatarhub = @{ spool = @('engines\avatarhub\data\events\spool')
                   roots = @('engines\avatarhub')
                   cfg   = @() }
    chengjie  = @{ spool = @('deploy\instances\zhiliao\data\events\spool',
                             'deploy\instances\tongyi\data\events\spool')
                   roots = @('deploy\instances\zhiliao\data',
                             'deploy\instances\tongyi\data')
                   # 快照目标 = 生产 config 目录（.117 迁移后住仓库外数据根；与 run_config_snapshot.ps1 缺省一致）
                   cfg   = @('D:\chengjie-instances\zhiliao\data\config',
                             'D:\chengjie-instances\tongyi\data\config') }
    huoke     = @{ spool = @('engines\huoke\data\events\spool')
                   roots = @('engines\huoke')
                   cfg   = @() }
}[$Engine]

$spools = @($SpoolDirs | ForEach-Object { $_ -split ',' } | ForEach-Object { $_.Trim() } | Where-Object { $_ })
if (-not $spools.Count) { $spools = $Defaults.spool }
$spools = @($spools | ForEach-Object { Resolve-RepoPath $_ })

$roots = @($DataRoots | ForEach-Object { $_ -split ',' } | ForEach-Object { $_.Trim() } | Where-Object { $_ })
if (-not $roots.Count) { $roots = $Defaults.roots }
$roots = @($roots | ForEach-Object { Resolve-RepoPath $_ })
$rootsArg = ($roots -join ',')   # 壳脚本按逗号拆分（-File 语义下数组只能靠逗号串）

$cfgDirs = @($ConfigDirs | ForEach-Object { $_ -split ',' } | ForEach-Object { $_.Trim() } | Where-Object { $_ })
if (-not $cfgDirs.Count) { $cfgDirs = $Defaults.cfg }
$cfgDirs = @($cfgDirs | ForEach-Object { Resolve-RepoPath $_ })
$cfgDirsArg = ($cfgDirs -join ',')

# ── 组装任务计划（先全部算出来，WhatIf 与 Execute 用同一份定义）───────
# common: 附加到每个壳命令的透传参数
$commonArgs = ''
if ($BaseUrl)   { $commonArgs += " -BaseUrl `"$BaseUrl`"" }
if ($PythonExe) { $commonArgs += " -PythonExe `"$PythonExe`"" }
if ($IngestKey) { $commonArgs += " -IngestKey `"$IngestKey`"" }

$plans = @()
if ('uploader' -in $Tasks) {
    foreach ($sp in $spools) {
        $suffix = if ($spools.Count -gt 1) { '-' + (Root-Tag $sp) } else { '' }
        $plans += [pscustomobject]@{
            Name        = "Boundless-$Engine-uploader$suffix"
            Kind        = 'repeat'
            Minutes     = $UploaderEveryMinutes
            TriggerDesc = "每 $UploaderEveryMinutes 分钟（-Once 起点 + Repetition，持续 3650 天）"
            Wrapper     = Join-Path $PSScriptRoot 'run_uploader.ps1'
            WrapperArgs = "-SpoolDir `"$sp`"$commonArgs"
            Checks      = @(@{ desc = "spool 目录 $sp"; ok = (Test-Path -LiteralPath $sp)
                               note = '不存在时 uploader 打印「尚无事件可传」退出 0，引擎首启后自动产生' })
        }
    }
}
if ('purge' -in $Tasks) {
    $plans += [pscustomobject]@{
        Name        = "Boundless-$Engine-purge"
        Kind        = 'repeat'
        Minutes     = $PurgeEveryMinutes
        TriggerDesc = "每 $PurgeEveryMinutes 分钟（-Once 起点 + Repetition，持续 3650 天）"
        Wrapper     = Join-Path $PSScriptRoot 'run_purge.ps1'
        WrapperArgs = "-Engine $Engine -Commit -DataRoots `"$rootsArg`"$commonArgs"
        Checks      = @($roots | ForEach-Object {
                          @{ desc = "数据根 $_"; ok = (Test-Path -LiteralPath $_ -PathType Container)
                             note = '不存在时壳脚本以退出码 2 失败（chengjie 迁移前传 -DataRoots "engines\chengjie"，README §3）' } }) +
                      @(if ($roots.Count -gt 1) {
                          $agentRel = @{ avatarhub = 'engines\avatarhub\persona_purge_agent.py'
                                         chengjie  = 'engines\chengjie\scripts\persona_purge_agent.py'
                                         huoke     = 'engines\huoke\src\persona_purge_agent.py' }[$Engine]
                          $agentPath = Join-Path $RepoRoot $agentRel
                          $hasFlag = (Test-Path -LiteralPath $agentPath) -and
                                     ([IO.File]::ReadAllText($agentPath)).Contains('--data-roots')
                          @{ desc = "多根 + --commit：执行器支持 --data-roots（$agentRel）"; ok = $hasFlag
                             note = if (-not (Test-Path -LiteralPath $agentPath)) {
                                        "执行器文件缺失：$agentPath（仓库不完整？）"
                                    } else {
                                        '执行器不支持多根——运行时会被壳脚本以退出码 3 守门拒绝（README §3），先改单根或等引擎侧落地'
                                    } } })
        }
}
if ('export' -in $Tasks) {
    $exportArgs = "-Engine $Engine -DataRoots `"$rootsArg`""
    if ($ExportTransfer) { $exportArgs += ' -Transfer -Import' }
    # export 壳不吃 -IngestKey，只透传 BaseUrl 以外的公共参数
    if ($PythonExe) { $exportArgs += " -PythonExe `"$PythonExe`"" }
    $plans += [pscustomobject]@{
        Name        = "Boundless-$Engine-export"
        Kind        = 'daily'
        Minutes     = 0
        TriggerDesc = "每日 $ExportDailyAt（导出→校验" + $(if ($ExportTransfer) { '→传输→导入' } else { '；三段式后两段用 -ExportTransfer 重装补上' }) + '）'
        Wrapper     = Join-Path $PSScriptRoot 'run_export.ps1'
        WrapperArgs = $exportArgs
        Checks      = @($roots | ForEach-Object {
                          @{ desc = "数据根 $_"; ok = (Test-Path -LiteralPath $_ -PathType Container)
                             note = '不存在时壳脚本以退出码 2 失败' } })
    }
}
if ('grants_sync' -in $Tasks) {
    $fetchPy = Join-Path $RepoRoot 'tools\persona_bus\fetch_grants.py'
    $plans += [pscustomobject]@{
        Name        = "Boundless-$Engine-grants_sync"
        Kind        = 'repeat'
        Minutes     = $GrantsSyncEveryMinutes
        TriggerDesc = "每 $GrantsSyncEveryMinutes 分钟（-Once 起点 + Repetition，持续 3650 天）"
        Wrapper     = Join-Path $PSScriptRoot 'run_grants_sync.ps1'
        WrapperArgs = "-Engine $Engine$commonArgs"
        Checks      = @(@{ desc = "fetch 脚本 tools\persona_bus\fetch_grants.py"; ok = (Test-Path -LiteralPath $fetchPy)
                           note = '缺失时壳脚本以退出码 2 失败（仓库不完整？git pull 后重试）' })
    }
}
if ('config_snapshot' -in $Tasks) {
    # 壳只吃 -ConfigDirs/-GitExe（本地 git 快照，无密钥、无 python，不透传 $commonArgs）
    $snapArgs = "-ConfigDirs `"$cfgDirsArg`""
    if ($GitExe) { $snapArgs += " -GitExe `"$GitExe`"" }
    $plans += [pscustomobject]@{
        Name        = "Boundless-$Engine-config_snapshot"
        Kind        = 'repeat'
        Minutes     = $ConfigSnapshotEveryMinutes
        TriggerDesc = "每 $ConfigSnapshotEveryMinutes 分钟（-Once 起点 + Repetition，持续 3650 天）"
        Wrapper     = Join-Path $PSScriptRoot 'run_config_snapshot.ps1'
        WrapperArgs = $snapArgs
        Checks      = @($cfgDirs | ForEach-Object {
                          @{ desc = "配置目录 $_"; ok = (Test-Path -LiteralPath $_ -PathType Container)
                             note = '不存在时壳脚本以退出码 2 失败（.117 迁移后形态 = D:\chengjie-instances\<实例>\data\config）' } })
    }
}
if ('watchdog' -in $Tasks) {
    # 双实例探活自愈（实施29）：壳=deploy\instances\watchdog_instances.ps1（探测复用 status_instances.ps1）。
    # 账户固定装机用户 S4U+Highest（ForceS4U，不吃 -RunAs）：watchdog 拉起的引擎继承其账户，
    # SYSTEM 会让引擎改以 SYSTEM 身份跑（用户身份漂移：user-home 缓存/文件属主全变）；
    # S4U=session 0 不依赖交互登录、不存密码，开机/注销后照常触发——正是原 Boot 任务
    # （InteractiveToken，开机时无交互会话永不触发）的病根修法。
    $instDir  = Join-Path (Split-Path -Parent $PSScriptRoot) 'instances'
    $wdScript = Join-Path $instDir 'watchdog_instances.ps1'
    $prodRoots = @('D:\chengjie-instances\zhiliao\data', 'D:\chengjie-instances\tongyi\data')
    $plans += [pscustomobject]@{
        Name        = "Boundless-$Engine-watchdog"
        Kind        = 'repeat'
        Minutes     = $WatchdogEveryMinutes
        TriggerDesc = "每 $WatchdogEveryMinutes 分钟（-Once 起点 + Repetition，持续 3650 天）"
        Wrapper     = $wdScript
        WrapperArgs = $commonArgs.Trim()
        ForceS4U    = $true
        Checks      = @(@{ desc = "成套脚本 deploy\instances\{status,start_zhiliao,start_tongyi,stop_instance}.ps1"
                           ok   = ((Test-Path (Join-Path $instDir 'status_instances.ps1')) -and
                                   (Test-Path (Join-Path $instDir 'start_zhiliao.ps1')) -and
                                   (Test-Path (Join-Path $instDir 'start_tongyi.ps1')) -and
                                   (Test-Path (Join-Path $instDir 'stop_instance.ps1')))
                           note = '仓库不完整？git pull / sync_ops_scripts.ps1 后重试' },
                        @{ desc = "生产数据根 $($prodRoots -join ' + ')"
                           ok   = ((Test-Path -LiteralPath $prodRoots[0]) -and (Test-Path -LiteralPath $prodRoots[1]))
                           note = '不存在时按 status_instances.ps1 探测序回落仓库缺省（.117 迁移后形态应存在）' },
                        @{ desc = 'python 全路径已传（-PythonExe；S4U 账户无用户级 PATH，拉起引擎必需）'
                           ok   = [bool]$PythonExe
                           note = '未传时 watchdog 沿用进程 PATH，DOWN 自愈拉起可能失败——建议 -PythonExe 全路径' })
    }
}
if ('kpi_weekly' -in $Tasks) {
    # 全矩阵报表不属任何引擎：名固定 Boundless-website-kpi_weekly（website 所在 Windows 机可选装）；
    # 壳只吃 -Week/-OutDir/-NodeExe，不透传 $commonArgs（无密钥、无 python）
    $kpiMjs = Join-Path $RepoRoot 'website\scripts\kpi-weekly-report.mjs'
    $kpiDep = Join-Path $RepoRoot 'website\node_modules\better-sqlite3'
    $kpiArgs = '-Week last'
    if ($NodeExe) { $kpiArgs += " -NodeExe `"$NodeExe`"" }
    $plans += [pscustomobject]@{
        Name        = 'Boundless-website-kpi_weekly'
        Kind        = 'weekly'
        Minutes     = 0
        TriggerDesc = "每周一 $KpiWeeklyAt（-Weekly Monday；报告落 deploy\cron\logs\reports\kpi_weekly_<时间戳>.md）"
        Wrapper     = Join-Path $PSScriptRoot 'run_kpi_weekly.ps1'
        WrapperArgs = $kpiArgs
        Checks      = @(@{ desc = "生成器 website\scripts\kpi-weekly-report.mjs"; ok = (Test-Path -LiteralPath $kpiMjs)
                           note = '缺失时壳脚本以退出码 2 失败（仓库不完整？git pull 后重试）' },
                        @{ desc = "依赖 website\node_modules\better-sqlite3"; ok = (Test-Path -LiteralPath $kpiDep)
                           note = '未装依赖时 node 以 ERR_MODULE_NOT_FOUND 失败——先 cd website && npm install' })
    }
}

# 动作命令行（cmd 链：确保日志目录存在 → powershell 壳 → 全量重定向进日志）
# ⚠ if 体必须括号隔离：`if not exist X mkdir X & cmd` 在 X 已存在时会把 `& cmd` 一并当
#   if 体跳过（cmd 单行 if 语法），任务看似 Last Result 0 实则壳脚本从未执行——实测踩过。
function Build-CmdArgs([pscustomobject]$plan) {
    $log = Join-Path $LogDir ($plan.Name + '.log')
    return "/d /c (if not exist `"$LogDir`" mkdir `"$LogDir`") & `"$PsExe`" -NoProfile -ExecutionPolicy Bypass -File `"$($plan.Wrapper)`" $($plan.WrapperArgs) >> `"$log`" 2>&1"
}
function Mask([string]$s) {
    if ($IngestKey) { return $s.Replace($IngestKey, '***') }
    return $s
}

# ── 环境体检（两种模式都做；只警告不阻断）────────────────────────────
$mode = if ($Execute) { 'EXECUTE（真注册）' } else { 'WHATIF 演练（只打印，不注册；加 -Execute 才注册）' }
Say "模式: $mode"
Say "机器: $env:COMPUTERNAME  引擎: $Engine  仓库根: $RepoRoot"
Say "任务: $($Tasks -join ', ')  账户: $RunAs  日志目录: $LogDir"

$needsKey    = @($Tasks | Where-Object { $_ -in @('uploader', 'purge', 'grants_sync') }).Count -gt 0
$needsPython = @($Tasks | Where-Object { $_ -notin @('kpi_weekly', 'config_snapshot') }).Count -gt 0
$machineKey = [Environment]::GetEnvironmentVariable('EVENT_INGEST_KEY', 'Machine')
if ($needsKey -and -not $machineKey -and -not $IngestKey) {
    Warn '机器级 EVENT_INGEST_KEY 未设置且未传 -IngestKey：uploader/purge/grants_sync 任务运行时将以退出码 2 失败（README §1）'
}
if ($IngestKey) {
    Warn '-IngestKey 将把密钥嵌进任务定义（本机管理员可读 XML）；推荐改用机器级环境变量（README §4）'
}
if ($needsPython -and -not (Get-Command ($(if ($PythonExe) { $PythonExe } else { 'python' })) -ErrorAction SilentlyContinue)) {
    Warn 'python 不在当前 PATH：SYSTEM 账户任务需机器级 PATH 含 python，或传 -PythonExe 全路径'
}
if ('kpi_weekly' -in $Tasks -and -not (Get-Command ($(if ($NodeExe) { $NodeExe } else { 'node' })) -ErrorAction SilentlyContinue)) {
    Warn 'node 不在当前 PATH：SYSTEM 账户 kpi_weekly 任务需机器级 PATH 含 node，或传 -NodeExe 全路径'
}
if ('config_snapshot' -in $Tasks -and -not (Get-Command ($(if ($GitExe) { $GitExe } else { 'git' })) -ErrorAction SilentlyContinue)) {
    Warn 'git 不在当前 PATH：SYSTEM 账户 config_snapshot 任务需机器级 PATH 含 git，或传 -GitExe 全路径'
}
foreach ($p in $plans) {
    if (-not (Test-Path -LiteralPath $p.Wrapper)) { Die "壳脚本缺失: $($p.Wrapper)" 2 }
}

# ── 逐任务：打印定义（WhatIf 到此为止）/ 注册（Execute）──────────────
$failed = 0
$i = 0
foreach ($plan in $plans) {
    $i++
    $cmdArgs = Build-CmdArgs $plan
    $log = Join-Path $LogDir ($plan.Name + '.log')
    $forceS4U = ($plan.PSObject.Properties['ForceS4U'] -and $plan.ForceS4U)
    $acctDesc = if ($forceS4U) {
        "$env:USERDOMAIN\$env:USERNAME（S4U 不存密码, Highest；watchdog 固定本账户——引擎继承其身份，不吃 -RunAs）"
    } elseif ($RunAs -eq 'SYSTEM') { 'NT AUTHORITY\SYSTEM（ServiceAccount, Highest）' }
    else { "$env:USERDOMAIN\$env:USERNAME（S4U 不存密码, Limited）" }
    Write-Host ''
    Write-Host ("── 任务 {0}/{1} ─ {2} ──────────────────────────────" -f $i, $plans.Count, $plan.Name)
    Write-Host ("  文件夹   {0}" -f $TaskPath)
    Write-Host ("  触发器   {0}" -f $plan.TriggerDesc)
    Write-Host ("  账户     {0}" -f $acctDesc)
    Write-Host ("  并发     IgnoreNew（上一轮未结束则跳过本轮）+ StartWhenAvailable + 时限 1h")
    Write-Host ("  工作目录 {0}" -f $RepoRoot)
    Write-Host ("  日志     {0}" -f $log)
    Write-Host ("  动作     cmd.exe {0}" -f (Mask $cmdArgs))
    foreach ($c in $plan.Checks) {
        if ($c.ok) { Write-Host ("  [检查] {0} — OK" -f $c.desc) }
        else       { Write-Host ("  [检查] {0} — 不满足：{1}" -f $c.desc, $c.note) -ForegroundColor Yellow }
    }

    if (-not $Execute) { continue }

    try {
        $action = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument $cmdArgs -WorkingDirectory $RepoRoot
        $trigger = switch ($plan.Kind) {
            'repeat' {
                New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2) `
                    -RepetitionInterval (New-TimeSpan -Minutes $plan.Minutes) `
                    -RepetitionDuration (New-TimeSpan -Days 3650)
            }
            'weekly' { New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday -At $KpiWeeklyAt }
            default  { New-ScheduledTaskTrigger -Daily -At $ExportDailyAt }
        }
        $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable `
            -ExecutionTimeLimit (New-TimeSpan -Hours 1)
        $principal = if ($forceS4U) {
            # watchdog 专用：装机用户 S4U + Highest（引擎继承其账户；Highest 才能 taskkill 提权进程）
            New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType S4U -RunLevel Highest
        } elseif ($RunAs -eq 'SYSTEM') {
            New-ScheduledTaskPrincipal -UserId 'NT AUTHORITY\SYSTEM' -LogonType ServiceAccount -RunLevel Highest
        } else {
            New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType S4U -RunLevel Limited
        }
        New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
        Register-ScheduledTask -TaskName $plan.Name -TaskPath $TaskPath -Action $action `
            -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
        Say ("已注册: {0}{1}" -f $TaskPath, $plan.Name)
    } catch {
        Warn ("注册失败: {0} —— {1}（SYSTEM 账户/计划任务注册需管理员 PowerShell）" -f $plan.Name, $_.Exception.Message)
        $failed++
    }
}

Write-Host ''
if (-not $Execute) {
    Say ("演练完成：以上 {0} 个任务一个都没注册。核对无误后加 -Execute（管理员 PowerShell）真注册。" -f $plans.Count)
    exit 0
}
if ($failed) { Die "注册完成但有 $failed 个失败（见上方警告）" 1 }
Say ("全部注册成功（{0} 个）。用 deploy\cron\list_tasks.ps1 巡检现状与上次运行结果。" -f $plans.Count)
exit 0
