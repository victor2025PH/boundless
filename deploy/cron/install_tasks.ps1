# install_tasks.ps1 — 引擎机定时任务安装器（deploy/cron，P4/P5 运营接线）
#
# 用法（缺省 = WhatIf 演练：只打印将注册的任务定义与完整命令行，绝不注册）：
#   powershell -ExecutionPolicy Bypass -File deploy\cron\install_tasks.ps1 -Engine avatarhub          # .176 演练
#   powershell -ExecutionPolicy Bypass -File deploy\cron\install_tasks.ps1 -Engine chengjie           # .117 演练（缺省双实例）
#   powershell -ExecutionPolicy Bypass -File deploy\cron\install_tasks.ps1 -Engine huoke -Execute     # .198 真注册（管理员）
#   … -Tasks uploader,purge            只装部分任务
#   … -SpoolDirs "a,b" -DataRoots "c"  覆盖缺省（-File 语义下多值用逗号串）
#   … -RunAs CurrentUser               S4U 运行（不存密码），缺省 SYSTEM
#   … -ExportTransfer                  export 任务带「传输到 VPS + 导入」两段（先打通 SSH）
#
# 设计要点：
#   - 任务放计划任务文件夹 \Boundless\，名 Boundless-<engine>-<task>[-<实例>]；
#   - 动作 = cmd.exe /d /c <powershell -File 壳脚本 …> >> logs\cron\<任务名>.log 2>&1
#     （logs/ 已被根 .gitignore 忽略；cmd 链先 mkdir 日志目录，重建仓库后任务不哑火）；
#   - 工作目录 = 仓库根；MultipleInstances=IgnoreNew（上一轮没跑完就跳过本轮，防叠跑）；
#   - 密钥缺省不进任务定义（任务 XML 本机管理员可读）：运行时由壳脚本读机器级
#     EVENT_INGEST_KEY；-IngestKey 显式传入才嵌进参数（打印时掩码，README §4）；
#   - 触发器：uploader 每 5 分钟 / purge 每 10 分钟（-Once + Repetition，持续 3650 天）；
#     export 每日 03:30（-Daily）。
#
# 退出码：0 = 演练完成 / 全部注册成功   1 = 注册失败（需管理员 PowerShell）   2 = 参数错误

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('avatarhub', 'chengjie', 'huoke')]
    [string]$Engine,
    [string[]]$Tasks = @('uploader', 'purge', 'export'),   # 多选：uploader/purge/export（逗号分隔亦可）
    [string]$BaseUrl   = '',        # 集团基址覆盖（缺省壳脚本走 env PERSONA_SYNC_BASE / https://bd2026.cc）
    [string]$IngestKey = '',        # 显式嵌密钥进任务定义（不推荐；缺省运行时读机器级 EVENT_INGEST_KEY）
    [string[]]$SpoolDirs = @(),     # uploader spool 目录（每目录一个任务实例）；缺省按引擎（见 $Defaults）
    [string[]]$DataRoots = @(),     # purge/export 数据根；缺省按引擎（chengjie=双实例数据根）
    [ValidateSet('SYSTEM', 'CurrentUser')]
    [string]$RunAs = 'SYSTEM',      # 运行账户：SYSTEM（缺省）或当前用户（S4U，不存密码）
    [string]$PythonExe = '',        # 传给壳脚本的 python 全路径（SYSTEM 账户 PATH 缺 python 时用）
    [int]$UploaderEveryMinutes = 5,
    [int]$PurgeEveryMinutes    = 10,
    [string]$ExportDailyAt     = '03:30',
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
$bad = @($Tasks | Where-Object { $_ -notin @('uploader', 'purge', 'export') })
if ($bad.Count)    { Die "未知任务类型: $($bad -join ', ')（可选 uploader/purge/export）" 2 }
if (-not $Tasks.Count) { Die '-Tasks 至少给一个任务类型' 2 }

$Defaults = @{
    avatarhub = @{ spool = @('engines\avatarhub\data\events\spool')
                   roots = @('engines\avatarhub') }
    chengjie  = @{ spool = @('deploy\instances\zhiliao\data\events\spool',
                             'deploy\instances\tongyi\data\events\spool')
                   roots = @('deploy\instances\zhiliao\data',
                             'deploy\instances\tongyi\data') }
    huoke     = @{ spool = @('engines\huoke\data\events\spool')
                   roots = @('engines\huoke') }
}[$Engine]

$spools = @($SpoolDirs | ForEach-Object { $_ -split ',' } | ForEach-Object { $_.Trim() } | Where-Object { $_ })
if (-not $spools.Count) { $spools = $Defaults.spool }
$spools = @($spools | ForEach-Object { Resolve-RepoPath $_ })

$roots = @($DataRoots | ForEach-Object { $_ -split ',' } | ForEach-Object { $_.Trim() } | Where-Object { $_ })
if (-not $roots.Count) { $roots = $Defaults.roots }
$roots = @($roots | ForEach-Object { Resolve-RepoPath $_ })
$rootsArg = ($roots -join ',')   # 壳脚本按逗号拆分（-File 语义下数组只能靠逗号串）

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

# 动作命令行（cmd 链：确保日志目录存在 → powershell 壳 → 全量重定向进日志）
function Build-CmdArgs([pscustomobject]$plan) {
    $log = Join-Path $LogDir ($plan.Name + '.log')
    return "/d /c if not exist `"$LogDir`" mkdir `"$LogDir`" & `"$PsExe`" -NoProfile -ExecutionPolicy Bypass -File `"$($plan.Wrapper)`" $($plan.WrapperArgs) >> `"$log`" 2>&1"
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

$machineKey = [Environment]::GetEnvironmentVariable('EVENT_INGEST_KEY', 'Machine')
if (-not $machineKey -and -not $IngestKey) {
    Warn '机器级 EVENT_INGEST_KEY 未设置且未传 -IngestKey：uploader/purge 任务运行时将以退出码 2 失败（README §1）'
}
if ($IngestKey) {
    Warn '-IngestKey 将把密钥嵌进任务定义（本机管理员可读 XML）；推荐改用机器级环境变量（README §4）'
}
if (-not (Get-Command ($(if ($PythonExe) { $PythonExe } else { 'python' })) -ErrorAction SilentlyContinue)) {
    Warn 'python 不在当前 PATH：SYSTEM 账户任务需机器级 PATH 含 python，或传 -PythonExe 全路径'
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
    Write-Host ''
    Write-Host ("── 任务 {0}/{1} ─ {2} ──────────────────────────────" -f $i, $plans.Count, $plan.Name)
    Write-Host ("  文件夹   {0}" -f $TaskPath)
    Write-Host ("  触发器   {0}" -f $plan.TriggerDesc)
    Write-Host ("  账户     {0}" -f $(if ($RunAs -eq 'SYSTEM') { 'NT AUTHORITY\SYSTEM（ServiceAccount, Highest）' } else { "$env:USERDOMAIN\$env:USERNAME（S4U 不存密码, Limited）" }))
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
        $trigger = if ($plan.Kind -eq 'repeat') {
            New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2) `
                -RepetitionInterval (New-TimeSpan -Minutes $plan.Minutes) `
                -RepetitionDuration (New-TimeSpan -Days 3650)
        } else {
            New-ScheduledTaskTrigger -Daily -At $ExportDailyAt
        }
        $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable `
            -ExecutionTimeLimit (New-TimeSpan -Hours 1)
        $principal = if ($RunAs -eq 'SYSTEM') {
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
