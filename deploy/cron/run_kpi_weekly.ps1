# run_kpi_weekly.ps1 — 全矩阵 KPI 周报计划任务薄壳（deploy/cron，P4 运营接线）
#
# 职责：组装并执行
#   node website\scripts\kpi-weekly-report.mjs --week <Week> --format md --out <报告文件>
# 报告落 deploy\cron\logs\reports\kpi_weekly_<yyyyMMdd_HHmm>.md（目录不存在自动建；
# logs/ 已被根 .gitignore 忽略，报告含经营数据勿外传）。
# 生成器只读聚合双库（README §5.3 口径同源）：库缺失/为空时输出骨架报告退出 0，任务可常开。
#
# 适用机器：website 所在 Windows 机可选装（install_tasks.ps1 -WithKpiWeekly）；
# VPS 生产机不适用 Windows 计划任务，用 Linux crontab 等价命令（README §5.3 ①）。
#
# 退出码：0 = 本轮成功（含库缺失时的骨架报告——生成器语义）
#         1 = 生成失败（参数错 / node 依赖缺失如 better-sqlite3 / 库查询异常，node 原样透传）
#         2 = 配置错误（node 不可用 / 生成器脚本缺失），错误信息在 stdout
#             （计划任务动作已把 stdout/stderr 重定向进 logs\cron\<任务名>.log）
#
# 用法（计划任务由 install_tasks.ps1 -WithKpiWeekly 注册；人工排障可直接跑）：
#   powershell -ExecutionPolicy Bypass -File deploy\cron\run_kpi_weekly.ps1                    # 上一完整 ISO 周
#   powershell -ExecutionPolicy Bypass -File deploy\cron\run_kpi_weekly.ps1 -Week this         # 本 ISO 周（跑到当下）
#   powershell -ExecutionPolicy Bypass -File deploy\cron\run_kpi_weekly.ps1 -Week 2026-W29     # 指定 ISO 周

[CmdletBinding()]
param(
    [string]$Week    = 'last',      # 报告窗口：this|last|YYYY-Www（透传 --week）；空串 = 生成器缺省近 7 天
    [string]$OutDir  = '',          # 报告目录（缺省 deploy\cron\logs\reports；不存在自动建）
    [string]$NodeExe = 'node'       # node 全路径（SYSTEM 账户 PATH 缺 node 时由安装器传 -NodeExe）
)

$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}

$RepoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)

function Say([string]$msg) { Write-Host ("[run_kpi_weekly {0:yyyy-MM-dd HH:mm:ss}] {1}" -f (Get-Date), $msg) }
function Die([string]$msg, [int]$code) { Say "错误: $msg"; exit $code }

function Resolve-RepoPath([string]$p) {
    if ([IO.Path]::IsPathRooted($p)) { return [IO.Path]::GetFullPath($p) }
    return [IO.Path]::GetFullPath((Join-Path $RepoRoot $p))
}

# ── 配置解析（缺什么错什么，绝不吞错静默跑）─────────────────────────
$kpiMjs = Join-Path $RepoRoot 'website\scripts\kpi-weekly-report.mjs'
if (-not (Test-Path -LiteralPath $kpiMjs)) { Die "找不到生成器脚本：$kpiMjs（仓库不完整？）" 2 }

$node = Get-Command $NodeExe -ErrorAction SilentlyContinue
if (-not $node) { Die "node 不可用（'$NodeExe' 不在 PATH；SYSTEM 账户任务需机器级 PATH，或安装器传 -NodeExe 全路径）" 2 }

# 生成器依赖 better-sqlite3（website\node_modules）；缺装时 node 会以 ERR_MODULE_NOT_FOUND 失败退非 0
$dep = Join-Path $RepoRoot 'website\node_modules\better-sqlite3'
if (-not (Test-Path -LiteralPath $dep)) {
    Say "警告: 未见 $dep —— 若下方 node 报 ERR_MODULE_NOT_FOUND，先 cd website && npm install"
}

$reports = if ($OutDir) { Resolve-RepoPath $OutDir } else { Join-Path $PSScriptRoot 'logs\reports' }
New-Item -ItemType Directory -Force -Path $reports | Out-Null
$outFile = Join-Path $reports ("kpi_weekly_{0:yyyyMMdd_HHmm}.md" -f (Get-Date))

# ── 组装命令并执行（只读聚合，无密钥；EVENTS_DB/LEDGER_DB/LEADS_DIR 由机器环境定库位）─
Set-Location $RepoRoot
$argList = @($kpiMjs)
if ($Week) { $argList += @('--week', $Week) }
$argList += @('--format', 'md', '--out', $outFile)

Say ("命令: {0} {1}" -f $node.Source, ($argList -join ' '))
& $node.Source @argList
$code = $LASTEXITCODE

if ($code -eq 0) { Say ("本轮完成（退出码 0）：报告 → {0}（库缺失时为骨架报告，见报告页首提示）" -f $outFile) }
else             { Say "本轮失败（退出码 $code）：看上方 node 输出；ERR_MODULE_NOT_FOUND = website 依赖未装（cd website && npm install）" }
exit $code
