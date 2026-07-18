# migrate_117.ps1 — 通译 LingoX 生产迁移编排骨架（配套作战手册 migrate_117_runbook.md）
# 用法:
#   powershell -ExecutionPolicy Bypass -File .\migrate_117.ps1                    # 缺省 = DryRun，只打印步骤
#   powershell -ExecutionPolicy Bypass -File .\migrate_117.ps1 -DryRun            # 同上（显式）
#   powershell -ExecutionPolicy Bypass -File .\migrate_117.ps1 -Execute [-DataDir <数据根>] [-PythonExe <python>]
#
# 职责（阶段1「拉起通译新实例」的自动化外壳，串起既有脚本，不重造轮子）：
#   preflight_instance.ps1 → 初始化闸（缺初始化则打印 README §3.1 命令后停下，绝不代做）
#   → start_tongyi.ps1 → 等端口就绪 → verify_instance.ps1 → 汇总报告 + 后续人工步骤提示。
#
# 安全护栏（有意为之，别"补全"）：
#   - 缺省 DryRun：只打印将执行的命令，一个进程都不拉、一个文件都不动；
#   - 初始化（建数据根/拷 config/填密钥/建 junction）永远是人工步骤：脚本只探测缺什么并
#     打印带真实路径的命令样板——密钥必须人手填，播种错误配置的代价远大于多敲几行命令；
#   - 破坏性动作（停现网智聊、禁用老自启任务、stack.json 翻 enabled、开防火墙）一律不进
#     本脚本，只存在于 runbook 的人工步骤（阶段0/4/5）；
#   - 无任何明文密钥/IP 硬编码：机器信息读 deploy/machines.json，端口读 deploy/stack.json；
#   - 本脚本在目标机（.117）上本地运行，不做任何 SSH/远程操作。
#
# 退出码: 0=DryRun 完成 / Execute 全部通过   1=某一步失败（已打印失败步骤与回滚提示）

[CmdletBinding()]
param(
    [switch]$DryRun,                 # 缺省行为；与 -Execute 互斥
    [switch]$Execute,                # 真跑（生产在 .117 上带参运行；本机演练传临时 -DataDir）
    [string]$DataDir   = '',         # 通译数据根（缺省 <本目录>\tongyi\data，同 start_tongyi.ps1）
    [string]$PythonExe = 'python',   # 透传 preflight（README §10：.venv-pilot 场景传 venv 的 python.exe）
    [string]$MachineId = 'tongyi',   # deploy/machines.json 里的目标机条目 id（仅用于报告抬头，不连接）
    [int]$ReadyTimeoutSec = 60       # -Execute 时等待引擎就绪的上限（试点实测约 25s）
)

$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}

if ($DryRun -and $Execute) {
    Write-Host '[migrate-117] 错误: -DryRun 与 -Execute 互斥，二选一' -ForegroundColor Red
    exit 1
}
$Mode = if ($Execute) { 'EXECUTE' } else { 'DRYRUN' }

# ── 路径与单一源（端口读 stack.json，机器台账读 machines.json——不硬编码）──
$RepoRoot  = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$EngineDir = Join-Path $RepoRoot 'engines\chengjie'
$DataRoot  = if ($DataDir) {
    [IO.Path]::GetFullPath([IO.Path]::Combine((Get-Location).ProviderPath, $DataDir))
} else { Join-Path $PSScriptRoot 'tongyi\data' }

$stackPath = Join-Path $RepoRoot 'deploy\stack.json'
$stack     = Get-Content -LiteralPath $stackPath -Raw -Encoding UTF8 | ConvertFrom-Json
$svc       = $stack.services | Where-Object { $_.id -eq 'chengjie_tongyi' }
if (-not $svc) {
    Write-Host "[migrate-117] 错误: stack.json 缺 chengjie_tongyi 条目（$stackPath）" -ForegroundColor Red
    exit 1
}
$Ports    = @($svc.ports | ForEach-Object { [int]$_ })
$MainPort = $Ports[0]
$BaseUrl  = "http://127.0.0.1:$MainPort"

$machineLabel = $MachineId
try {
    $machines = Get-Content -LiteralPath (Join-Path $RepoRoot 'deploy\machines.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    $mc = $machines.machines | Where-Object { $_.id -eq $MachineId }
    if ($mc) { $machineLabel = "$($mc.zh) $($mc.ip)（$($mc.user)，ssh 别名 $($mc.ssh[0])）" }
} catch {
    Write-Host "[migrate-117] 警告: machines.json 读取失败，报告抬头用 id 代替（$($_.Exception.Message)）" -ForegroundColor Yellow
}

# ── 步骤定义（display 命令即 Execute 真跑的命令，DryRun 逐条打印）────────
# 子脚本自带 exit 0/1，必须以独立 powershell.exe 进程调用（进程内 & 调用会把本编排一起 exit 掉）
$ps = 'powershell.exe -NoProfile -ExecutionPolicy Bypass -File'
$steps = @(
    [pscustomobject]@{
        id      = 'preflight'
        title   = '预检（python/依赖/全链导入/端口空闲/数据根可写）'
        command = "$ps `"$PSScriptRoot\preflight_instance.ps1`" -DataDir `"$DataRoot`" -Ports $($Ports -join ',') -PythonExe `"$PythonExe`""
        manual  = $false
    },
    [pscustomobject]@{
        id      = 'init_gate'
        title   = '初始化闸（人工步骤核对：config.yaml / overlay+密钥 / domains junction）'
        command = '（探测性检查，缺项时打印 README §3.1 初始化命令并停下——初始化永远人工做）'
        manual  = $true
    },
    [pscustomobject]@{
        id      = 'start'
        title   = '拉起通译实例（幂等；端口被占且非引擎时报错不清杀）'
        command = "$ps `"$PSScriptRoot\start_tongyi.ps1`" -DataDir `"$DataRoot`""
        manual  = $false
    },
    [pscustomobject]@{
        id      = 'wait_ready'
        title   = "等待引擎就绪（端口 $MainPort 出现 HTTP 响应，上限 ${ReadyTimeoutSec}s；试点实测约 25s）"
        command = "（内置轮询 $BaseUrl/login，每 3s 一次）"
        manual  = $false
    },
    [pscustomobject]@{
        id      = 'verify'
        title   = '生产验收（只读：HTTP 活性/品牌断言/health 鉴权/端口持有者/数据根体检/授权文件）'
        command = "$ps `"$PSScriptRoot\verify_instance.ps1`" -Base $BaseUrl -Instance tongyi -DataDir `"$DataRoot`""
        manual  = $false
    }
)

Write-Host "=============== migrate-117 — 通译 LingoX 生产迁移编排 [$Mode] ==============="
Write-Host "  目标机 : $machineLabel（本脚本须在该机本地运行，不做远程操作）"
Write-Host "  引擎   : $EngineDir"
Write-Host "  数据根 : $DataRoot"
Write-Host "  端口   : $($Ports -join ', ')（主位 $MainPort，来源 stack.json chengjie_tongyi）"
Write-Host "  手册   : deploy\instances\migrate_117_runbook.md（本脚本只覆盖其阶段1）"
Write-Host '-------------------------------------------------------------------------------'

# ── 初始化闸的探测逻辑（DryRun 也跑——只是 Test-Path，零副作用）───────────
function Get-InitMissing {
    $missing = @()
    if (-not (Test-Path (Join-Path $DataRoot 'config\config.yaml')))       { $missing += 'config\config.yaml' }
    if (-not (Test-Path (Join-Path $DataRoot 'config\config.local.yaml'))) { $missing += 'config\config.local.yaml' }
    if (-not (Test-Path (Join-Path $DataRoot 'domains')))                  { $missing += 'domains（junction）' }
    return $missing
}

function Show-InitCommands {
    Write-Host '  ── 初始化命令样板（README §3.1，人工执行；密钥必须人手填）──────────' -ForegroundColor Yellow
    Write-Host "  `$eng  = `"$EngineDir`""
    Write-Host "  `$data = `"$DataRoot`""
    Write-Host '  New-Item -ItemType Directory -Force -Path "$data\config","$data\sessions","$data\logs","$data\events\spool","$data\ledger_outbox" | Out-Null'
    Write-Host '  Copy-Item "$eng\config\config.example.yaml" "$data\config\config.yaml"'
    Write-Host "  Copy-Item `"$PSScriptRoot\tongyi\config.local.yaml`" `"`$data\config\config.local.yaml`""
    Write-Host '  New-Item -ItemType Junction -Path "$data\domains" -Target "$eng\domains" | Out-Null'
    Write-Host '  # 然后编辑 $data\config\config.local.yaml：替换模板注释行填入 ai.api_key/base_url/model'
    Write-Host '  #   与强随机 web_admin.auth_token/secret_key（勿在文件尾追加第二个 web_admin: 块！）'
    Write-Host '  # 授权（可选）：通译授权码存 $data\config\license.key（与智聊不同 lic_id，README §5）'
}

# ── DryRun：打印步骤与命令后退出，不执行任何一步 ─────────────────────────
if ($Mode -eq 'DRYRUN') {
    $i = 0
    foreach ($s in $steps) {
        $i++
        $tag = if ($s.manual) { '人工闸' } else { '自动' }
        Write-Host ("  [{0}/{1}] ({2}) {3}" -f $i, $steps.Count, $tag, $s.title)
        Write-Host ("        {0}" -f $s.command) -ForegroundColor DarkGray
        if ($s.id -eq 'init_gate') {
            $missing = Get-InitMissing
            if ($missing.Count) {
                Write-Host ("        当前探测：缺 {0} —— Execute 会在此停下，需先人工初始化：" -f ($missing -join '、')) -ForegroundColor Yellow
                Show-InitCommands
            } else {
                Write-Host '        当前探测：初始化三件套齐全，Execute 将直接放行' -ForegroundColor Green
            }
        }
    }
    Write-Host '-------------------------------------------------------------------------------'
    Write-Host '  DRYRUN 完成：以上为将执行的全部命令，未启动任何进程、未改动任何文件。'
    Write-Host '  实跑: migrate_117.ps1 -Execute [-DataDir <数据根>]；'
    Write-Host '  阶段2 灰度验证 / 阶段3 观察期 / 阶段4 stack.json+域名 / 阶段5 老自启任务'
    Write-Host '  均为人工步骤，见 migrate_117_runbook.md。'
    exit 0
}

# ── Execute：逐步执行，失败即停并给回滚提示 ──────────────────────────────
$report = New-Object System.Collections.ArrayList
function Add-Report([string]$id, [string]$status, [string]$note) {
    [void]$report.Add([pscustomobject]@{ step = $id; status = $status; note = $note })
}
function Show-ReportAndExit([int]$code) {
    Write-Host '-------------------------------------------------------------------------------'
    foreach ($r in $report) {
        $color = switch ($r.status) { 'OK' {'Green'} 'FAIL' {'Red'} default {'Gray'} }
        Write-Host ("  [{0,-4}] {1,-10} {2}" -f $r.status, $r.step, $r.note) -ForegroundColor $color
    }
    if ($code -eq 0) {
        Write-Host '  MIGRATE(阶段1): 全部通过。后续人工步骤（runbook）:' -ForegroundColor Green
        Write-Host '    阶段2 灰度验证（登录/翻译/事件/授权的人工清单）→ 阶段3 观察期（建议 ≥1 周）'
        Write-Host '    → 阶段4 stack.json 翻 enabled + 对外暴露/域名 → 阶段5 老自启任务处置'
    } else {
        Write-Host '  MIGRATE(阶段1): 有失败步骤。回滚（新实例可随时安全停，现网未被触碰）:' -ForegroundColor Red
        Write-Host "    powershell -ExecutionPolicy Bypass -File `"$PSScriptRoot\stop_instance.ps1`" -Instance tongyi"
        Write-Host '    数据根为全新数据，修复问题后可原地重跑本脚本（幂等）。'
    }
    exit $code
}

# 1) preflight
Write-Host '  [1/5] 预检 ...'
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$PSScriptRoot\preflight_instance.ps1" -DataDir "$DataRoot" -Ports ($Ports -join ',') -PythonExe "$PythonExe"
if ($LASTEXITCODE -ne 0) {
    Add-Report 'preflight' 'FAIL' "退出码 $LASTEXITCODE — 处理 FAIL 项后重跑（依赖缺失/端口被占见 runbook 应急速查）"
    Show-ReportAndExit 1
}
Add-Report 'preflight' 'OK' '预检通过（WARN 允许，import main 烟测为硬闸）'

# 2) 初始化闸（只探测，绝不代做）
Write-Host '  [2/5] 初始化闸 ...'
$missing = Get-InitMissing
if ($missing.Count) {
    Write-Host ("[migrate-117] 数据根未初始化，缺: {0}" -f ($missing -join '、')) -ForegroundColor Yellow
    Show-InitCommands
    Add-Report 'init_gate' 'FAIL' '初始化未完成——人工按上列命令初始化（含密钥）后重跑本脚本'
    Show-ReportAndExit 1
}
Add-Report 'init_gate' 'OK' 'config.yaml / config.local.yaml / domains 三件套在位'

# 3) start
Write-Host '  [3/5] 拉起通译实例 ...'
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$PSScriptRoot\start_tongyi.ps1" -DataDir "$DataRoot"
if ($LASTEXITCODE -ne 0) {
    Add-Report 'start' 'FAIL' "start_tongyi.ps1 退出码 $LASTEXITCODE（错误信息见其输出；日志在 <数据根>\logs\boot_*.err.log）"
    Show-ReportAndExit 1
}
Add-Report 'start' 'OK' '启动脚本通过（幂等：已在跑也算通过）'

# 4) 等就绪（任意 HTTP 状态码=web 线程活）
Write-Host "  [4/5] 等待引擎就绪（上限 ${ReadyTimeoutSec}s）..."
$deadline = (Get-Date).AddSeconds($ReadyTimeoutSec)
$ready = $false
do {
    Start-Sleep -Seconds 3
    try {
        $req = [System.Net.HttpWebRequest]::Create("$BaseUrl/login")
        $req.Timeout = 3000; $req.AllowAutoRedirect = $false
        $resp = $req.GetResponse(); $resp.Close(); $ready = $true
    } catch [System.Net.WebException] {
        if ($_.Exception.Response) { $ready = $true } else { Write-Host '        ... 等待中' -ForegroundColor DarkGray }
    } catch {}
} while (-not $ready -and (Get-Date) -lt $deadline)
if (-not $ready) {
    Add-Report 'wait_ready' 'FAIL' "${ReadyTimeoutSec}s 内 $BaseUrl 无 HTTP 响应——查 <数据根>\logs\boot_*.err.log 与 logs\app.log（runbook 应急速查·健康不过）"
    Show-ReportAndExit 1
}
Add-Report 'wait_ready' 'OK' "$BaseUrl 有 HTTP 响应"

# 5) verify（只读验收）
Write-Host '  [5/5] 生产验收 ...'
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$PSScriptRoot\verify_instance.ps1" -Base $BaseUrl -Instance tongyi -DataDir "$DataRoot"
if ($LASTEXITCODE -ne 0) {
    Add-Report 'verify' 'FAIL' "verify_instance.ps1 退出码 $LASTEXITCODE（FAIL 项见其清单）"
    Show-ReportAndExit 1
}
Add-Report 'verify' 'OK' '验收清单全过（WARN 项人工复核）'

Show-ReportAndExit 0
