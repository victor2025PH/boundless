# start_tongyi.ps1 — 通译 LingoX 实例启动（chengjie 双实例，同目录 README.md 是运行手册）
# 用法: powershell -ExecutionPolicy Bypass -File .\start_tongyi.ps1
#
# 委派引擎既有入口 `python main.py`（同 stack.json: runtime=python entry=main.py），
# 差异仅两点（引擎零改动的双实例机制，侦察结论见 README §0）：
#   1) 工作目录 = 实例数据根（sessions/ logs/ 等 cwd 相对路径随之隔离）；
#   2) AITR_DATA_DIR = 实例数据根（config 与全套 SQLite 落 <数据根>\config\）。
# 防呆：数据根/config 缺失一律报错退出，绝不自动创建/播种/覆盖任何现有数据；
#       端口被非本引擎进程占用时报错而不清杀（双实例下清幽灵可能误杀另一实例）。

$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}

# ── 实例常量（挪数据根/换端口只改这里，并同步 stack.json 条目与实例 overlay）──
$InstanceId   = 'tongyi'
$InstanceName = '通译 LingoX'
$Port         = 18899                                   # = 实例 config.local.yaml 的 web_admin.port
$RepoRoot     = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$EngineDir    = Join-Path $RepoRoot 'engines\chengjie'
$DataRoot     = Join-Path $PSScriptRoot 'tongyi\data'

function Fail([string]$msg) {
    Write-Host "[start-$InstanceId] 错误: $msg" -ForegroundColor Red
    exit 1
}

# ── 防呆检查（只读，不创建）─────────────────────────────────────────────
if (-not (Test-Path (Join-Path $EngineDir 'main.py'))) {
    Fail "引擎入口不存在: $EngineDir\main.py（仓库不完整？）"
}
if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    Fail "PATH 里找不到 python（与 stack.json runtime=python 同一约定）"
}
if (-not (Test-Path $DataRoot)) {
    Fail "实例数据根不存在: $DataRoot`n  → 先按 deploy\instances\README.md §3.1 初始化（本脚本绝不自动创建，防止半初始化状态带病启动）"
}
$cfg = Join-Path $DataRoot 'config\config.yaml'
if (-not (Test-Path $cfg)) {
    Fail "实例主配置不存在: $cfg`n  → 按 README §3.1 从 config.example.yaml 起底（缺 config 时引擎会自播种 example 占位——绕过了必填项检查，故此处直接拦下）"
}
if (-not (Test-Path (Join-Path $DataRoot 'config\config.local.yaml'))) {
    Fail "实例 overlay 不存在: $DataRoot\config\config.local.yaml`n  → 拷贝模板 deploy\instances\tongyi\config.local.yaml 并填 ai.api_key / web_admin.auth_token（README §3.1 第 3/5 步）。没有它通译会以 example 默认端口 18787 起——与智聊备用位冲突"
}
if (-not (Test-Path (Join-Path $DataRoot 'domains'))) {
    Fail "实例域包目录缺失: $DataRoot\domains`n  → New-Item -ItemType Junction -Path `"$DataRoot\domains`" -Target `"$EngineDir\domains`"（README §3 第 4 步；通译用 general 域包）"
}

# ── 幂等/端口防呆：已在跑则退出 0；被别人占则报错（绝不 Stop-Process）────
$own = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
if ($own.Count) {
    $pids = @($own | Select-Object -ExpandProperty OwningProcess -Unique)
    $isOurs = $false
    foreach ($holderPid in $pids) {
        $p = Get-CimInstance Win32_Process -Filter "ProcessId=$holderPid" -ErrorAction SilentlyContinue
        if ($p -and $p.CommandLine -like '*main.py*') { $isOurs = $true }
    }
    if ($isOurs) {
        Write-Host "[start-$InstanceId] $InstanceName 已在跑（端口 $Port，PID=$($pids -join ',')），幂等跳过" -ForegroundColor Green
        exit 0
    }
    Fail "端口 $Port 被非本引擎进程占用 PID=$($pids -join ',')。双实例模式不自动清杀（可能误伤另一实例/其他服务），请人工核实后再起"
}

# ── 组装子进程环境并启动 ────────────────────────────────────────────────
# Win32_Process.Create 不继承本 shell 的 $env: 改动，故环境变量全部写进 cmd 链
# （set "K=V" && …），保证子进程一定拿到：
#   AITR_DATA_DIR          实例数据根（config 定位机制，README §0 优先级 2）
#   EVENT_SPOOL_DIR        事件 spool（platform/observability 契约；本实例产品号=tongyi）
#   CHENGJIE_LEDGER_OUTBOX 授权台账钩子落盘目录（实施09 §五.3，同事正在引擎侧接线，
#                          环境变量名以他为准；引擎未接线前设置无副作用）
#   LICENSE_KEY            实例自己的授权（README §5：env 优先于共享的引擎根 license.key；
#                          用 set /p 从文件读，令牌不出现在进程命令行上）
#   AITR_DESKTOP_MODE/AITR_CONFIG_PATH/AITR_WEB_*  显式清空，防外部环境串味
$logDir = Join-Path $DataRoot 'logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null   # 仅日志目录，数据一概不建
$ts  = Get-Date -Format 'yyyyMMdd_HHmmss'
$out = Join-Path $logDir "boot_${ts}.out.log"
$err = Join-Path $logDir "boot_${ts}.err.log"

$spool  = Join-Path $DataRoot 'events\spool'
$ledger = Join-Path $DataRoot 'ledger_outbox'
$lic    = Join-Path $DataRoot 'config\license.key'

$chain = @(
    "set `"AITR_DATA_DIR=$DataRoot`"",
    "set `"EVENT_SPOOL_DIR=$spool`"",
    "set `"CHENGJIE_LEDGER_OUTBOX=$ledger`"",
    "set `"AITR_DESKTOP_MODE=`"",
    "set `"AITR_CONFIG_PATH=`"",
    "set `"AITR_WEB_HOST=`"",
    "set `"AITR_WEB_PORT=`"",
    "set `"AITR_WEB_TOKEN=`""
)
if (Test-Path $lic) {
    $chain += "set /p LICENSE_KEY=<`"$lic`""
    Write-Host "[start-$InstanceId] 使用实例授权: $lic（LICENSE_KEY 注入，优先于共享 license.key）"
} else {
    $chain += "set `"LICENSE_KEY=`""
    Write-Host "[start-$InstanceId] 未见实例授权文件（$lic），按社区模式/共享文件回落启动" -ForegroundColor Yellow
}
$chain += "python `"$EngineDir\main.py`" > `"$out`" 2> `"$err`""
$cmdline = 'cmd.exe /c ' + ($chain -join ' && ')

Write-Host "[start-$InstanceId] 启动 $InstanceName  端口=$Port"
Write-Host "[start-$InstanceId]   代码: $EngineDir （共享，只读；改代码走 git）"
Write-Host "[start-$InstanceId]   数据: $DataRoot"
# Win32_Process.Create（不继承句柄）：Start-Process 会让 python 继承本 shell 的
# stdout 管道句柄，凡捕获本脚本输出的调用方（deploy.ps1/自动化）会挂死等 EOF。
$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
    CommandLine = $cmdline; CurrentDirectory = $DataRoot }
if ($r.ReturnValue -ne 0) { Fail "进程创建失败 ReturnValue=$($r.ReturnValue)" }

Start-Sleep -Seconds 4
$listening = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue).Count -gt 0
if ($listening) {
    Write-Host "[start-$InstanceId] done — 端口 $Port 已在听  日志=$out" -ForegroundColor Green
} else {
    Write-Host "[start-$InstanceId] 已拉起（PID=$($r.ProcessId)），引擎初始化通常需 10~30s；用 status_instances.ps1 复核。日志=$out"
}
exit 0
