# run_export.ps1 — 人设/授权导出三段式计划任务薄壳（deploy/cron，P4/P5 运营接线）
#
# 三段式（README §5；缺省只做 1+2，-Transfer/-Import 补第 3 段）：
#   1) 导出   persona：python tools\persona_bus\export_<engine>_personas.py --input <根> --out <staging>（绝对只读）
#             license：avatarhub → python tools\license_ledger\export_avatarhub.py --out <staging>
#                      chengjie  → python engines\chengjie\scripts\ledger_outbox.py --export <staging> --input <实例outbox>
#                                  （签发即台账 outbox；未接线/无签发记录时跳过，不算失败）
#                      huoke     → 无授权数据源，自动跳过
#   2) 校验   persona：python tools\persona_bus\validate_personas.py <file>
#             license：python tools\license_ledger\validate_export.py <file>   —— 不过不许传输（本壳强制）
#   3) 传输+导入  scp -F deploy\ssh_config.boundless <staging> vps-bd2026:<RemoteDir>/
#                 ssh vps-bd2026 "cd <RemoteApp> && node scripts/ledger-import-personas.mjs <file>"
#                 （license 文件走 ledger-import-licenses.mjs；幂等键 (source_system, source_key)，重复导入 upsert 不重登）
#
# chengjie 双实例：缺省两实例数据根各出一份文件（chengjie_personas_<实例>.json / chengjie_licenses_<实例>.json）。
# staging 缺省 <仓库根>\data\persona_bus_out\（data/ 已 gitignore；导出文件含显示名/指纹/客户名，
# 属经营数据，勿外传、用后可清）。
#
# 退出码：0 = 全部成功   1 = 导出/校验/传输/导入任一步失败（校验失败的文件绝不传输）
#         2 = 配置错误（数据根不存在 / python 或 ssh 不可用）
#
# 用法：
#   powershell -ExecutionPolicy Bypass -File deploy\cron\run_export.ps1 -Engine avatarhub                     # 导出+校验
#   powershell -ExecutionPolicy Bypass -File deploy\cron\run_export.ps1 -Engine chengjie -Transfer -Import    # 全三段
#   powershell -ExecutionPolicy Bypass -File deploy\cron\run_export.ps1 -Engine chengjie -DataRoots "engines\chengjie"  # 迁移前单实例期
#   powershell -ExecutionPolicy Bypass -File deploy\cron\run_export.ps1 -Engine avatarhub -Kinds persona      # 只导人设

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('avatarhub', 'chengjie', 'huoke')]
    [string]$Engine,
    [string[]]$DataRoots = @(),     # 引擎根/实例数据根（逗号分隔亦可）；chengjie 缺省双实例数据根
    [string[]]$Kinds = @('persona', 'license'),   # 导出内容多选：persona / license（逗号分隔亦可）
    [string]$OutDir    = '',        # staging 目录（缺省 <仓库根>\data\persona_bus_out）
    [switch]$Transfer,              # 第 3 段前半：scp 到 VPS
    [switch]$Import,                # 第 3 段后半：ssh 远端执行导入（隐含 -Transfer）
    [string]$SshHost   = 'vps-bd2026',                 # deploy\ssh_config.boundless 里的别名
    [string]$RemoteDir = '/home/ubuntu/persona_inbox', # VPS 侧落地目录
    [string]$RemoteApp = '/home/ubuntu/yuntech',       # VPS 侧 website 应用目录（node_modules 在此）
    [string]$PythonExe = 'python'
)

$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}

$RepoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)

function Say([string]$msg) { Write-Host ("[run_export {0:yyyy-MM-dd HH:mm:ss}] {1}" -f (Get-Date), $msg) }
function Die([string]$msg, [int]$code) { Say "错误: $msg"; exit $code }

function Resolve-RepoPath([string]$p) {
    if ([IO.Path]::IsPathRooted($p)) { return [IO.Path]::GetFullPath($p) }
    return [IO.Path]::GetFullPath((Join-Path $RepoRoot $p))
}

# 数据根 → 文件名标签：叶子目录名为 data 时取上一级（deploy\instances\zhiliao\data → zhiliao）
function Root-Tag([string]$root) {
    $leaf = Split-Path -Leaf $root
    if ($leaf -ieq 'data') { return Split-Path -Leaf (Split-Path -Parent $root) }
    return $leaf
}

# ── 配置解析 ─────────────────────────────────────────────────────────
$Kinds = @($Kinds | ForEach-Object { $_ -split ',' } | ForEach-Object { $_.Trim().ToLower() } | Where-Object { $_ })
$badKinds = @($Kinds | Where-Object { $_ -notin @('persona', 'license') })
if ($badKinds.Count)  { Die "未知导出内容: $($badKinds -join ', ')（可选 persona/license）" 2 }
if (-not $Kinds.Count) { Die '-Kinds 至少给一个（persona/license）' 2 }

$DefaultRoots = @{
    avatarhub = @('engines\avatarhub')
    chengjie  = @('deploy\instances\zhiliao\data', 'deploy\instances\tongyi\data')
    huoke     = @('engines\huoke')
}[$Engine]

$roots = @($DataRoots | ForEach-Object { $_ -split ',' } | ForEach-Object { $_.Trim() } | Where-Object { $_ })
if (-not $roots.Count) { $roots = $DefaultRoots }
$roots = @($roots | ForEach-Object { Resolve-RepoPath $_ })

$missing = @($roots | Where-Object { -not (Test-Path -LiteralPath $_ -PathType Container) })
if ($missing.Count) {
    Die ("数据根不存在：{0}。chengjie 迁移前的现网单实例期请传 -DataRoots `"engines\chengjie`"（README §3）" -f ($missing -join '；')) 2
}

$personaExporter  = Join-Path $RepoRoot ("tools\persona_bus\export_{0}_personas.py" -f $Engine)
$personaValidator = Join-Path $RepoRoot 'tools\persona_bus\validate_personas.py'
$licenseValidator = Join-Path $RepoRoot 'tools\license_ledger\validate_export.py'
$avhLicExporter   = Join-Path $RepoRoot 'tools\license_ledger\export_avatarhub.py'
$cjOutboxExporter = Join-Path $RepoRoot 'engines\chengjie\scripts\ledger_outbox.py'
$need = @()
if ('persona' -in $Kinds) { $need += @($personaExporter, $personaValidator) }
if ('license' -in $Kinds) {
    $need += $licenseValidator
    if ($Engine -eq 'avatarhub') { $need += $avhLicExporter }
    if ($Engine -eq 'chengjie')  { $need += $cjOutboxExporter }
}
foreach ($f in $need) {
    if (-not (Test-Path -LiteralPath $f)) { Die "找不到脚本：$f（仓库不完整？）" 2 }
}

$py = Get-Command $PythonExe -ErrorAction SilentlyContinue
if (-not $py) { Die "python 不可用（'$PythonExe' 不在 PATH；SYSTEM 账户任务需机器级 PATH，或安装器传 -PythonExe 全路径）" 2 }

if ($Import) { $Transfer = $true }
$sshCfg = Join-Path $RepoRoot 'deploy\ssh_config.boundless'
if ($Transfer) {
    foreach ($tool in @('ssh', 'scp')) {
        if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
            Die "-Transfer 需要 Windows OpenSSH 客户端（缺 $tool）；先装通道或去掉 -Transfer 只做导出+校验" 2
        }
    }
    if (-not (Test-Path -LiteralPath $sshCfg)) { Die "找不到 SSH 配置：$sshCfg" 2 }
}

$staging = if ($OutDir) { Resolve-RepoPath $OutDir } else { Join-Path $RepoRoot 'data\persona_bus_out' }
New-Item -ItemType Directory -Force -Path $staging | Out-Null

# ── 段 1+2：导出 → 校验（逐文件收集 kind，供第 3 段选导入器）──────────
Set-Location $RepoRoot
$okFiles = @()     # @{ path; kind = 'persona'|'license' }
$failed  = 0

function Export-Validate([string[]]$exportCmd, [string]$validator, [string]$outFile, [string]$kind) {
    Say ("[1/导出] {0}" -f ($exportCmd -join ' '))
    # 原生命令 stdout 必须导去主机（Out-Host）：函数内不重定向会混进返回值，
    # 返回值成非空数组 → 调用方 if (-not …) 恒为假，失败被吞（勿改回裸调用）
    & $py.Source @exportCmd | Out-Host
    if ($LASTEXITCODE -ne 0) {
        Say "导出失败（退出码 $LASTEXITCODE），该文件跳过后续段"
        return $false
    }
    Say ("[2/校验] {0} {1}" -f (Split-Path -Leaf $validator), $outFile)
    & $py.Source $validator $outFile | Out-Host
    if ($LASTEXITCODE -ne 0) {
        Say "校验不过（退出码 $LASTEXITCODE）：该文件不传输，先修数据再重跑"
        return $false
    }
    $script:okFiles += @{ path = $outFile; kind = $kind }
    return $true
}

if ('persona' -in $Kinds) {
    foreach ($root in $roots) {
        $outFile = if ($roots.Count -gt 1) {
            Join-Path $staging ("{0}_personas_{1}.json" -f $Engine, (Root-Tag $root))
        } else {
            Join-Path $staging ("{0}_personas.json" -f $Engine)
        }
        if (-not (Export-Validate @($personaExporter, '--input', $root, '--out', $outFile) $personaValidator $outFile 'persona')) { $failed++ }
    }
}

if ('license' -in $Kinds) {
    switch ($Engine) {
        'avatarhub' {
            # 数据源：secrets/orders.json + trials.json + license.key 等（导出器缺省引擎目录，只读）
            $outFile = Join-Path $staging 'avatarhub_licenses.json'
            if (-not (Export-Validate @($avhLicExporter, '--input', $roots[0], '--out', $outFile) $licenseValidator $outFile 'license')) { $failed++ }
        }
        'chengjie' {
            # 签发即台账 outbox（每实例一份；CHENGJIE_LEDGER_OUTBOX 分实例落盘，README §3）
            foreach ($root in $roots) {
                $tag = Root-Tag $root
                $candidates = @(
                    (Join-Path $root 'ledger_outbox\ledger_outbox.jsonl'),
                    (Join-Path $root 'ledger_outbox'),
                    (Join-Path $root 'config\ledger_outbox.jsonl')
                )
                $src = @($candidates | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf }) | Select-Object -First 1
                if (-not $src) {
                    Say ("[license] 跳过（{0}）：未见 outbox 文件（引擎钩子未接线或尚无签发记录）；候选: {1}" -f $tag, ($candidates -join ' | '))
                    continue
                }
                $outFile = if ($roots.Count -gt 1) {
                    Join-Path $staging ("chengjie_licenses_{0}.json" -f $tag)
                } else {
                    Join-Path $staging 'chengjie_licenses.json'
                }
                if (-not (Export-Validate @($cjOutboxExporter, '--export', $outFile, '--input', $src) $licenseValidator $outFile 'license')) { $failed++ }
            }
        }
        'huoke' {
            Say '[license] 跳过：huoke 无授权发放数据源（tools/license_ledger 无 huoke 导出器）'
        }
    }
}

# ── 段 3：传输 + 导入（仅校验通过的文件；按 kind 选导入器）────────────
$Importer = @{ persona = 'scripts/ledger-import-personas.mjs'; license = 'scripts/ledger-import-licenses.mjs' }
if ($Transfer -and $okFiles.Count) {
    Say ("[3/传输] 建远端目录 {0}:{1}" -f $SshHost, $RemoteDir)
    & ssh -F $sshCfg $SshHost "mkdir -p $RemoteDir"
    if ($LASTEXITCODE -ne 0) { Say "远端目录创建失败（退出码 $LASTEXITCODE）"; $failed++ }
    else {
        foreach ($f in $okFiles) {
            $fname = Split-Path -Leaf $f.path
            Say ("[3/传输] scp {0} → {1}:{2}/" -f $fname, $SshHost, $RemoteDir)
            & scp -F $sshCfg $f.path "${SshHost}:${RemoteDir}/"
            if ($LASTEXITCODE -ne 0) { Say "scp 失败（退出码 $LASTEXITCODE）"; $failed++; continue }
            if ($Import) {
                Say ("[3/导入] node {0} {1}/{2}" -f $Importer[$f.kind], $RemoteDir, $fname)
                & ssh -F $sshCfg $SshHost "cd $RemoteApp && node $($Importer[$f.kind]) $RemoteDir/$fname"
                if ($LASTEXITCODE -ne 0) { Say "远端导入失败（退出码 $LASTEXITCODE）"; $failed++ }
            }
        }
    }
} elseif (-not $Transfer) {
    Say ("缺省只做 导出+校验；产物在 {0}（-Transfer/-Import 补第 3 段，见 README §5）" -f $staging)
}

if ($failed -eq 0) { Say ("全部完成：{0} 份文件（退出码 0）" -f $okFiles.Count); exit 0 }
Say ("本轮有 {0} 步失败（退出码 1）；成功产物 {1} 份" -f $failed, $okFiles.Count)
exit 1
