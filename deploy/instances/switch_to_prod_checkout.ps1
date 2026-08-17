# switch_to_prod_checkout.ps1 — 生产双实例代码源切换:开发工作树 → 生产专用 checkout
# (prod_checkout_runbook.md 的可执行封装;手册手工步骤保留作排障参考)
#
# 用法:
#   powershell -ExecutionPolicy Bypass -File .\switch_to_prod_checkout.ps1 -Preflight   # 只体检不动手
#   powershell -ExecutionPolicy Bypass -File .\switch_to_prod_checkout.ps1             # 体检+金丝雀切换
#   参数: -ProdRoot/-DevRoot 改路径; -SkipWatchdog 不改计划任务; -Force 越过 WIP 阻断(慎用)
#
# 前置检查(任一不过即拒动,对应 runbook §0/§0.5):
#   P1 prod checkout 在位且已同步 origin/main(落后则自动 --ff-only 拉平)
#   P2 main..feat 提交差集为空(防切换回退未合并的生产修复;2026-07-20 实测教训)
#   P3 开发树 engines/ 无未提交 WIP(防抽走正被在线迭代的半成品;-Force 可越过)
#   P4 Baileys node_modules 不早于 package.json(前置产物时效;7.0 升级实测教训)
#   P5 双实例当前 GO(带病切换会把新旧问题搅在一起)
# 切换(金丝雀:tongyi → 验收 → zhiliao → 验收):
#   stop(防呆脚本) → domains junction 重指 prod → start(prod 脚本) → verify 11 项
# 收尾: watchdog 计划任务改指 prod;扫码门户/sidecar 若从旧树运行则提示手工迁移。
# 回滚: 任一实例验收失败即停,给出"junction 指回 + 旧树重启"的两条命令,不自动连滚。

[CmdletBinding()]
param(
    [string]$ProdRoot = 'D:\boundless-prod',
    [string]$DevRoot  = 'D:\workspace\boundless',
    [switch]$Preflight,
    [switch]$SkipWatchdog,
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}
# git 把进度写 stderr,PS5 在 $ErrorActionPreference=Stop 下会把 2>&1 的 stderr 当异常——
# 统一用本包装收敛(退出码才是真信号,与 repo 其他脚本同哲学)。
function Git-Quiet { param([Parameter(ValueFromRemainingArguments)]$rest)
    $ErrorActionPreference = 'Continue'
    & git @rest 2>$null | Out-Null
    $script:LASTEXITCODE = $LASTEXITCODE
    $ErrorActionPreference = 'Stop'
}

$Instances = @(
    @{ id = 'tongyi';  port = 18899; data = 'D:\chengjie-instances\tongyi\data' },
    @{ id = 'zhiliao'; port = 18799; data = 'D:\chengjie-instances\zhiliao\data' }
)
$fails = New-Object System.Collections.ArrayList

function Say([string]$m, [string]$c = 'Gray') { Write-Host "[switch] $m" -ForegroundColor $c }
function Bad([string]$m) { [void]$fails.Add($m); Say "FAIL: $m" 'Red' }
function Die([string]$m) { Say "中止: $m" 'Red'; exit 1 }

# ── P1 prod checkout 就绪并对齐 origin/main ─────────────────────────────
if (-not (Test-Path (Join-Path $ProdRoot 'engines\chengjie\main.py'))) { Die "prod checkout 不在位: $ProdRoot" }
Git-Quiet -C $ProdRoot fetch origin main
$prodHead = (& git -C $ProdRoot rev-parse HEAD).Trim()
$mainHead = (& git -C $ProdRoot rev-parse origin/main).Trim()
if ($prodHead -ne $mainHead) {
    Say "prod checkout 落后 origin/main,尝试 --ff-only 拉平…" 'Yellow'
    Git-Quiet -C $ProdRoot pull --ff-only origin main
    $prodHead = (& git -C $ProdRoot rev-parse HEAD).Trim()
    if ($prodHead -ne $mainHead) { Bad "prod checkout 无法快进到 origin/main(本地被改动过?)" }
}
if ($prodHead -eq $mainHead) { Say "P1 OK  prod = origin/main @ $($mainHead.Substring(0,8))" 'Green' }

# ── P2 main..feat 提交差集 ───────────────────────────────────────────────
Git-Quiet -C $DevRoot fetch origin main
$diffCommits = @(& git -C $DevRoot log origin/main..HEAD --oneline)
if ($diffCommits.Count) { Bad "开发分支有 $($diffCommits.Count) 个未合并提交(先经 PR 送入 main): $($diffCommits[0])" }
else { Say "P2 OK  main..feat 差集为空" 'Green' }

# ── P3 开发树 WIP 静默 ───────────────────────────────────────────────────
$wip = @(& git -C $DevRoot status --porcelain -- engines/)
if ($wip.Count) {
    if ($Force) { Say "P3 WARN  engines/ 有 $($wip.Count) 项 WIP,-Force 越过(切换后这些半成品从生产消失)" 'Yellow' }
    else { Bad "engines/ 有 $($wip.Count) 项未提交 WIP(在线迭代中,切换会抽走它们;落提交后再切,或 -Force)" }
} else { Say "P3 OK  开发树 engines/ 无 WIP" 'Green' }

# ── P4 Baileys 依赖时效 ──────────────────────────────────────────────────
$wa = Join-Path $ProdRoot 'engines\chengjie\services\whatsapp-baileys'
$pkg = Join-Path $wa 'package.json'
$nm  = Join-Path $wa 'node_modules'
if (-not (Test-Path $nm)) { Bad "Baileys node_modules 缺失: 在 $wa 执行 npm install --no-audit --no-fund" }
elseif ((Get-Item $pkg).LastWriteTime -gt (Get-Item $nm).LastWriteTime) {
    Bad "Baileys package.json 晚于 node_modules(清单已变,依赖过期): 重新 npm install"
} else { Say "P4 OK  Baileys 依赖不早于清单" 'Green' }

# ── P5 双实例现状 GO ─────────────────────────────────────────────────────
foreach ($i in $Instances) {
    $listen = @(Get-NetTCPConnection -LocalPort $i.port -State Listen -ErrorAction SilentlyContinue)
    if (-not $listen.Count) { Bad "$($i.id) 端口 $($i.port) 无监听(实例未在跑,先用 status_instances.ps1 排查)" }
}
if (-not ($fails | Where-Object { $_ -like '*端口*' })) { Say "P5 OK  双实例端口在听" 'Green' }

if ($fails.Count) { Die "前置检查未过($($fails.Count) 项),不动手。逐项处理后重跑。" }
Say "前置检查全部通过" 'Green'
if ($Preflight) { Say "(-Preflight 模式,到此为止)" 'Cyan'; exit 0 }

# ── 金丝雀切换 ───────────────────────────────────────────────────────────
foreach ($i in $Instances) {
    $id = $i.id
    Say "── 切换 $id ──" 'Cyan'
    & powershell -ExecutionPolicy Bypass -File (Join-Path $ProdRoot "deploy\instances\stop_instance.ps1") -Instance $id
    if ($LASTEXITCODE -ne 0) { Die "$id 停止失败(端口未释放/持有者可疑),生产保持原状" }

    $junc = Join-Path $i.data 'domains'
    $target = Join-Path $ProdRoot 'engines\chengjie\domains'
    $old = (Get-Item $junc -ErrorAction SilentlyContinue)
    if ($old -and $old.LinkType -eq 'Junction') {
        $oldTarget = @($old.Target)[0]
        $old.Delete()
        New-Item -ItemType Junction -Path $junc -Target $target | Out-Null
        Say "$id domains junction: $oldTarget → $target"
    } elseif (-not $old) {
        New-Item -ItemType Junction -Path $junc -Target $target | Out-Null
        Say "$id domains junction 新建 → $target" 'Yellow'
    } else { Die "$id 的 $junc 不是 junction(是实体目录?),人工核实——绝不自动删除实体数据" }

    & powershell -ExecutionPolicy Bypass -File (Join-Path $ProdRoot "deploy\instances\start_$id.ps1") -DataDir $i.data
    if ($LASTEXITCODE -ne 0) {
        Say "回滚提示: (Get-Item '$junc').Delete(); New-Item -ItemType Junction -Path '$junc' -Target '$DevRoot\engines\chengjie\domains' | Out-Null" 'Yellow'
        Say "         powershell -File $DevRoot\deploy\instances\start_$id.ps1 -DataDir $($i.data)" 'Yellow'
        Die "$id 从 prod checkout 启动失败,已给出回滚命令,另一实例未动"
    }
    Start-Sleep -Seconds 20
    & powershell -ExecutionPolicy Bypass -File (Join-Path $ProdRoot "deploy\instances\verify_instance.ps1") -Instance $id -DataDir $i.data
    if ($LASTEXITCODE -ne 0) { Die "$id 验收未过(verify_instance 有 FAIL),已停在此实例;按上方回滚提示处理" }
    Say "$id 切换完成并验收通过" 'Green'
}

# ── 计划任务批量改指 prod(watchdog/uploader/snapshot/sentinel/Baileys 等)──
# 2026-07-20 盘点:引用开发树的任务共 10 个(含 WhatsApp-Baileys-Service)。逐个替换
# 动作参数里的树路径;watchdog 优先(它会从脚本所在树拉起实例,指错树=从错误代码自愈)。
if (-not $SkipWatchdog) {
    $swept = 0
    foreach ($task in @(Get-ScheduledTask -ErrorAction SilentlyContinue |
            Where-Object { $_.TaskName -like '*Boundless*' -or $_.TaskName -like '*Baileys*' })) {
        $acts = @($task.Actions)
        $touched = $false
        $newActs = foreach ($a in $acts) {
            if ($a.Arguments -like "*$DevRoot*" -or $a.Execute -like "*$DevRoot*") {
                $touched = $true
                New-ScheduledTaskAction `
                    -Execute ($a.Execute -replace [regex]::Escape($DevRoot), $ProdRoot) `
                    -Argument ($a.Arguments -replace [regex]::Escape($DevRoot), $ProdRoot)
            } else { $a }
        }
        if ($touched) {
            Set-ScheduledTask -TaskName $task.TaskName -TaskPath $task.TaskPath -Action $newActs | Out-Null
            Say "计划任务改指 prod: $($task.TaskName)" 'Green'
            $swept++
        }
    }
    Say ($(if ($swept) { "共改指 $swept 个计划任务" } else { "计划任务均未引用开发树,无需改动" })) 'Green'
}

# ── 旧树残留进程提示 ─────────────────────────────────────────────────────
$oldProcs = Get-CimInstance Win32_Process -Filter "Name like 'python%' or Name like 'node%'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like "*$DevRoot*" }
foreach ($p in $oldProcs) {
    Say "仍从旧树运行: PID=$($p.ProcessId) $($p.CommandLine.Substring(0,[Math]::Min(90,$p.CommandLine.Length)))…(扫码门户/sidecar 请择机从 $ProdRoot 重启)" 'Yellow'
}

Say "切换完成。其余 \Boundless\* 计划任务请按 runbook §2 逐个核对路径。" 'Green'
exit 0

