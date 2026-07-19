# run_enforce_readiness.ps1 — grant enforce 切换就绪度每日巡检薄壳（deploy/cron，P5 证据期）
#
# 职责：对本机引擎的 grant 缓存跑 tools\persona_bus\enforce_readiness.py --json，
# 结果追加进日志（计划任务重定向）；连续 7 天 ready 即满足 PERSONA_BUS §4.2 前置条件 2。
# 退出码：0=ready  1=not ready（原样透传，供监控看趋势；not ready 属证据期常态，不算任务失败）
#
# 用法（计划任务由 install_tasks.ps1 或手工注册；人工排障可直接跑）：
#   powershell -ExecutionPolicy Bypass -File deploy\cron\run_enforce_readiness.ps1 -Engine chengjie

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('avatarhub', 'chengjie', 'huoke')]
    [string]$Engine,
    [string]$CacheFile = '',        # 缺省 data\persona_bus_out\<engine>_grants.json（与 run_grants_sync 输出对齐）
    [string]$PythonExe = 'python'
)

$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}

$RepoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)

function Say([string]$msg) { Write-Host ("[run_enforce_readiness {0:yyyy-MM-dd HH:mm:ss}] {1}" -f (Get-Date), $msg) }
function Die([string]$msg, [int]$code) { Say "错误: $msg"; exit $code }

$checker = Join-Path $RepoRoot 'tools\persona_bus\enforce_readiness.py'
if (-not (Test-Path -LiteralPath $checker)) { Die "找不到检查器：$checker（仓库不完整？）" 2 }

$py = Get-Command $PythonExe -ErrorAction SilentlyContinue
if (-not $py) { Die "python 不可用（'$PythonExe' 不在 PATH；SYSTEM 账户任务传 -PythonExe 全路径）" 2 }

$cache = if ($CacheFile) { $CacheFile } else {
    Join-Path $RepoRoot ("data\persona_bus_out\{0}_grants.json" -f $Engine)
}

Say ("检查 {0} 的 grant 缓存就绪度：{1}" -f $Engine, $cache)
& $py.Source $checker --cache $cache --json
$code = $LASTEXITCODE
if ($code -eq 0) { Say '结论：READY（计入连续通过天数）' }
else             { Say "结论：NOT READY（退出码 $code）——证据期常态，连续 7 天 READY 后按 PERSONA_BUS §4.2 灰度 enforce" }
exit $code
