# run_grants_sync.ps1 — 人设 grant 缓存同步计划任务薄壳（deploy/cron，P5 软门控接线）
#
# 职责：组装并执行
#   python tools\persona_bus\fetch_grants.py --system <Engine> --out <OutFile> [--base <BaseUrl>]
# 从集团 /api/sync/personas/grants 拉取本引擎 grant 清单，原子写本地缓存 JSON，
# 供 platform\identity\grant_gate.py 运行时软门控只读命中（缺省 warn 不挡业务，
# 引擎侧经 env PERSONA_GRANT_CACHE 指向该缓存文件）。
# 密钥经环境变量 EVENT_INGEST_KEY 传给子进程（绝不上命令行——日志/任务定义零泄漏）。
#
# 退出码：0 = 本轮成功（缓存已更新）
#         1 = 拉取失败（fetch_grants 原样透传：网络/5xx/鉴权可重试；旧缓存仍供门控离线使用）
#         2 = 配置错误（缺密钥 / python 不可用 / fetch 脚本缺失），错误信息在 stdout
#             （计划任务动作已把 stdout/stderr 重定向进 logs\cron\<任务名>.log）
#
# 用法（计划任务由 install_tasks.ps1 注册；人工排障可直接跑）：
#   powershell -ExecutionPolicy Bypass -File deploy\cron\run_grants_sync.ps1 -Engine avatarhub
#   powershell -ExecutionPolicy Bypass -File deploy\cron\run_grants_sync.ps1 -Engine chengjie -OutFile data\persona_bus_out\chengjie_grants.json

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('avatarhub', 'chengjie', 'huoke')]
    [string]$Engine,
    [string]$BaseUrl   = '',        # 集团基址（缺省让 fetch 走 env PERSONA_SYNC_BASE，再缺省 https://bd2026.cc）
    [string]$IngestKey = '',        # Bearer 密钥（缺省 env EVENT_INGEST_KEY；仅经环境变量下传）
    [string]$OutFile   = '',        # 缓存输出路径（缺省 data\persona_bus_out\<engine>_grants.json；fetch 原子写并自建父目录）
    [string]$PythonExe = 'python',
    [int]$TimeoutS     = 30         # HTTP 超时秒（透传 --timeout）
)

$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}

$RepoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)

function Say([string]$msg) { Write-Host ("[run_grants_sync {0:yyyy-MM-dd HH:mm:ss}] {1}" -f (Get-Date), $msg) }
function Die([string]$msg, [int]$code) { Say "错误: $msg"; exit $code }

function Resolve-RepoPath([string]$p) {
    if ([IO.Path]::IsPathRooted($p)) { return [IO.Path]::GetFullPath($p) }
    return [IO.Path]::GetFullPath((Join-Path $RepoRoot $p))
}

# ── 配置解析（缺什么错什么，绝不吞错静默跑）─────────────────────────
$fetchPy = Join-Path $RepoRoot 'tools\persona_bus\fetch_grants.py'
if (-not (Test-Path -LiteralPath $fetchPy)) { Die "找不到 fetch 脚本：$fetchPy（仓库不完整？）" 2 }

$py = Get-Command $PythonExe -ErrorAction SilentlyContinue
if (-not $py) { Die "python 不可用（'$PythonExe' 不在 PATH；SYSTEM 账户任务需机器级 PATH，或安装器传 -PythonExe 全路径）" 2 }

$key = $IngestKey
if (-not $key) { $key = [string]$env:EVENT_INGEST_KEY }
if (-not $key) {
    Die '缺少同步密钥：设机器级环境变量 EVENT_INGEST_KEY（推荐，见 deploy\cron\README.md §1；grants API 与 /api/collect 共用同一把 M2M 密钥），或传 -IngestKey' 2
}

$out = if ($OutFile) { Resolve-RepoPath $OutFile } else {
    Join-Path $RepoRoot ("data\persona_bus_out\{0}_grants.json" -f $Engine)
}

# ── 组装命令并执行（密钥只经环境变量，不进命令行）────────────────────
Set-Location $RepoRoot
$argList = @($fetchPy, '--system', $Engine, '--out', $out, '--timeout', $TimeoutS)
if ($BaseUrl) { $argList += @('--base', $BaseUrl) }
$env:EVENT_INGEST_KEY = $key

Say ("命令: {0} {1}  [密钥经 EVENT_INGEST_KEY 环境变量传入，已掩码]" -f $py.Source, ($argList -join ' '))
& $py.Source @argList
$code = $LASTEXITCODE

if ($code -eq 0) { Say ("本轮完成（退出码 0）：缓存已更新 → {0}" -f $out) }
else             { Say "本轮失败（退出码 $code）：网络/5xx 可重试，旧缓存仍供门控离线使用（缺省 warn 放行）；HTTP 401 = 密钥错须人工处理" }
exit $code
