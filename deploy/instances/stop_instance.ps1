# stop_instance.ps1 — 按实例停止 chengjie 双实例（防呆：只停「持有实例端口且命令行是引擎 main.py」的进程树）
# 用法: powershell -ExecutionPolicy Bypass -File .\stop_instance.ps1 -Instance tongyi [-TimeoutSec 15]
# 行为：
#   - 实例端口无监听 → 视为未在跑，幂等退出 0；
#   - 端口持有者命令行不含 main.py → 报错退出 1，绝不误杀（与 start 脚本同一防呆哲学）；
#   - 停止 = taskkill /T /F 进程树（引擎无优雅停止 HTTP 端点；main.py 的 SIGTERM 优雅路径
#     在 Windows 分离进程上不可达——exit_sentinel 会把这次记为强停，属预期）；
#     python 由 cmd /c 链拉起，python 树死后 cmd 壳自行退出；
#   - 停止后确认端口释放（默认等 15s），未释放报错退出 1。

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('tongyi', 'zhiliao')]
    [string]$Instance,
    [int]$TimeoutSec = 15
)

$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}

# 端口登记与 start_*.ps1 / status_instances.ps1 / stack.json 保持一致
$PortMap = @{ tongyi = @(18899, 18887); zhiliao = @(18799, 18787) }
$Ports   = $PortMap[$Instance]

function Fail([string]$msg) {
    Write-Host "[stop-$Instance] 错误: $msg" -ForegroundColor Red
    exit 1
}

# ── 找端口持有者（主位+备用位都查，防端口漂移后漏停）────────────────────
$holders = @{}   # pid -> 端口列表
foreach ($port in $Ports) {
    foreach ($c in @(Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue)) {
        $holderPid = [int]$c.OwningProcess
        if (-not $holders.ContainsKey($holderPid)) { $holders[$holderPid] = @() }
        if ($holders[$holderPid] -notcontains $port) { $holders[$holderPid] += $port }
    }
}
if (-not $holders.Count) {
    Write-Host "[stop-$Instance] 端口 $($Ports -join '/') 无监听——实例未在跑，幂等退出" -ForegroundColor Green
    exit 0
}

# ── 防呆：逐 PID 验明正身（命令行必须含 main.py），有一个不像就整体拒停 ────
$targets = @()
foreach ($holderPid in $holders.Keys) {
    $p = Get-CimInstance Win32_Process -Filter "ProcessId=$holderPid" -ErrorAction SilentlyContinue
    if (-not $p) { continue }   # 竞态：刚退出
    if ($p.CommandLine -like '*main.py*') {
        $targets += [pscustomobject]@{ ProcessId = $holderPid; Name = $p.Name; Ports = $holders[$holderPid] }
    } else {
        Fail "端口 $($holders[$holderPid] -join ',') 持有者 PID=$holderPid($($p.Name)) 命令行不含 main.py，不是本引擎实例。拒绝停止（人工核实：Get-CimInstance Win32_Process -Filter `"ProcessId=$holderPid`" | Select CommandLine）"
    }
}
if (-not $targets.Count) {
    Write-Host "[stop-$Instance] 持有进程已自行退出，幂等退出" -ForegroundColor Green
    exit 0
}

# ── 停进程树 ─────────────────────────────────────────────────────────────
foreach ($t in $targets) {
    Write-Host "[stop-$Instance] 停止 PID=$($t.ProcessId)($($t.Name)) 端口=$($t.Ports -join ',') （taskkill /T /F 进程树）"
    & taskkill /PID $t.ProcessId /T /F 2>&1 | ForEach-Object { Write-Host "[stop-$Instance]   $_" -ForegroundColor DarkGray }
}

# ── 确认端口释放 ─────────────────────────────────────────────────────────
$deadline = (Get-Date).AddSeconds($TimeoutSec)
do {
    Start-Sleep -Milliseconds 500
    $still = @()
    foreach ($port in $Ports) {
        if (@(Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue).Count) { $still += $port }
    }
    if (-not $still.Count) {
        Write-Host "[stop-$Instance] done — 端口 $($Ports -join '/') 已全部释放" -ForegroundColor Green
        exit 0
    }
} while ((Get-Date) -lt $deadline)

Fail "等待 ${TimeoutSec}s 后端口 $($still -join ',') 仍在监听，请人工排查（netstat -ano | findstr $($still[0])）"
