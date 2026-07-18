# run_uploader.ps1 — 事件补传 uploader 计划任务薄壳（deploy/cron，P4 运营接线）
#
# 职责：组装并执行
#   python platform\observability\uploader.py --spool-dir <SpoolDir> --endpoint <base>/api/collect --batch N
# 密钥经环境变量 EVENT_INGEST_KEY 传给子进程（绝不上命令行——日志/任务定义零泄漏）。
# 铁律（uploader.py 文件头）：每 spool 目录一个任务实例，勿并发跑同一 spool 目录。
#
# 退出码：0 = 本轮成功（含「spool 尚不存在 / 无新数据」）
#         1 = 上传失败（uploader 原样透传：网络/5xx 偏移未推进下轮续传；401 换密钥）
#         2 = 配置错误（缺 -SpoolDir / 缺密钥 / python 不可用），错误信息在 stdout
#             （计划任务动作已把 stdout/stderr 重定向进 logs\cron\<任务名>.log）
#
# 用法（计划任务由 install_tasks.ps1 注册；人工排障可直接跑）：
#   powershell -ExecutionPolicy Bypass -File deploy\cron\run_uploader.ps1 -SpoolDir engines\avatarhub\data\events\spool
#   powershell -ExecutionPolicy Bypass -File deploy\cron\run_uploader.ps1 -SpoolDir <…> -DryRun   # 只统计不联网不写 state

[CmdletBinding()]
param(
    [string]$SpoolDir  = '',        # 必填：spool 目录（相对仓库根或绝对路径）
    [string]$BaseUrl   = '',        # 集团基址（缺省 env PERSONA_SYNC_BASE，再缺省 https://bd2026.cc）
    [string]$Endpoint  = '',        # 收集器完整地址；给了就不再拼 <BaseUrl>/api/collect
    [string]$IngestKey = '',        # 上报密钥（缺省 env EVENT_INGEST_KEY；仅经环境变量下传）
    [int]$Batch        = 200,       # 每批条数（收集端上限 500）
    [string]$StateFile = '',        # 断点游标（缺省 <spool>\.upload_state.json，uploader 自定）
    [string]$Source    = '',        # X-Event-Source（缺省本机 hostname）
    [string]$PythonExe = 'python',
    [switch]$DryRun                 # 透传 --dry-run：只统计将上传行数，不联网、不写 state
)

$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}

$RepoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)

function Say([string]$msg) { Write-Host ("[run_uploader {0:yyyy-MM-dd HH:mm:ss}] {1}" -f (Get-Date), $msg) }
function Die([string]$msg, [int]$code) { Say "错误: $msg"; exit $code }

function Resolve-RepoPath([string]$p) {
    if ([IO.Path]::IsPathRooted($p)) { return [IO.Path]::GetFullPath($p) }
    return [IO.Path]::GetFullPath((Join-Path $RepoRoot $p))
}

# ── 配置解析（缺什么错什么，绝不吞错静默跑）─────────────────────────
if (-not $SpoolDir) { Die '-SpoolDir 必填（README 任务矩阵：每 spool 目录一个任务实例，勿并发）' 2 }
$spool = Resolve-RepoPath $SpoolDir

$uploaderPy = Join-Path $RepoRoot 'platform\observability\uploader.py'
if (-not (Test-Path -LiteralPath $uploaderPy)) { Die "找不到 uploader 脚本：$uploaderPy（仓库不完整？）" 2 }

$py = Get-Command $PythonExe -ErrorAction SilentlyContinue
if (-not $py) { Die "python 不可用（'$PythonExe' 不在 PATH；SYSTEM 账户任务需机器级 PATH，或安装器传 -PythonExe 全路径）" 2 }

$key = $IngestKey
if (-not $key) { $key = [string]$env:EVENT_INGEST_KEY }
if (-not $key -and -not $DryRun) {
    Die '缺少上报密钥：设机器级环境变量 EVENT_INGEST_KEY（推荐，见 deploy\cron\README.md §1），或传 -IngestKey' 2
}

if (-not $Endpoint) {
    $base = $BaseUrl
    if (-not $base) { $base = [string]$env:PERSONA_SYNC_BASE }
    if (-not $base) { $base = 'https://bd2026.cc' }
    $Endpoint = $base.TrimEnd('/') + '/api/collect'
}

# ── 组装命令并执行（密钥只经环境变量，不进命令行）────────────────────
Set-Location $RepoRoot
$argList = @($uploaderPy, '--spool-dir', $spool, '--endpoint', $Endpoint, '--batch', $Batch)
if ($StateFile) { $argList += @('--state-file', (Resolve-RepoPath $StateFile)) }
if ($Source)    { $argList += @('--source', $Source) }
if ($DryRun)    { $argList += '--dry-run' }
if ($key)       { $env:EVENT_INGEST_KEY = $key }

Say ("命令: {0} {1}  [密钥经 EVENT_INGEST_KEY 环境变量传入，已掩码]" -f $py.Source, ($argList -join ' '))
& $py.Source @argList
$code = $LASTEXITCODE

if ($code -eq 0) { Say '本轮完成（退出码 0）' }
else             { Say "本轮失败（退出码 $code）：网络/5xx 偏移未推进下轮自动续传；HTTP 401 = 密钥错须人工处理" }
exit $code
