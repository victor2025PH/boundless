# fulfill_chatx_task.ps1 — chatx/lingox 订单履约计划任务壳（厂商机常驻，融合实例 P5）
#
# 把 scripts/fulfill_chatx_watch.py 从「手工挂 --watch 常驻」升级为计划任务周期单发：
# 每 N 分钟拉一次官网 paid 订单 → 本地私钥签发（license / topup 凭证）→ 回填开通
#（官网自动私信客户）。单发模式天然免僵尸进程，state 文件幂等防重复开通。
#
# 机密三件（放 D:\chengjie-instances\.ops\fulfill\，仓库外、绝不入库）：
#   site.txt                      官网地址（如 https://bd2026.cc）
#   admin_key.txt                 官网 ADMIN_KEY（x-setup-key 鉴权，同 website env）
#   vendor_license_private.hex    厂商 Ed25519 私钥（license_tool.py genkeys 产物）
#
# 用法：
#   .\fulfill_chatx_task.ps1                 # 单次履约轮（计划任务入口）
#   .\fulfill_chatx_task.ps1 -DryRun         # 只列出待履约订单，不签发（首跑体检用）
#   .\fulfill_chatx_task.ps1 -Register       # 注册计划任务 ChatxFulfillWatch（缺机密拒绝）
#   .\fulfill_chatx_task.ps1 -Register -IntervalMin 10
#   .\fulfill_chatx_task.ps1 -Unregister     # 摘除计划任务
#
# 日志：logs\fulfill\fulfill_YYYYMMDD.log（按日一份，保留 30 份；UTF-8）。
# 上线三步：备齐机密 → -DryRun 看清单 → -Register。

[CmdletBinding()]
param(
    [switch]$Register,
    [switch]$Unregister,
    [switch]$DryRun,
    [int]$IntervalMin = 5,
    [string]$SecretsDir = 'D:\chengjie-instances\.ops\fulfill'
)

$ErrorActionPreference = 'Continue'
$root = Split-Path -Parent $PSScriptRoot
$TaskName = 'ChatxFulfillWatch'

# 日志编码统一 UTF-8（PS5 *>> 会按 UTF-16 混写；与 avatar_prerender_nightly 同款处置）
$env:PYTHONIOENCODING = 'utf-8'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}

$siteFile = Join-Path $SecretsDir 'site.txt'
$keyFile  = Join-Path $SecretsDir 'admin_key.txt'
$privFile = Join-Path $SecretsDir 'vendor_license_private.hex'

function Get-MissingSecrets {
    $missing = @()
    foreach ($f in @($siteFile, $keyFile, $privFile)) {
        if (-not (Test-Path -LiteralPath $f)) { $missing += $f }
    }
    return $missing
}

if ($Unregister) {
    schtasks /Delete /TN $TaskName /F
    exit $LASTEXITCODE
}

if ($Register) {
    $missing = Get-MissingSecrets
    if ($missing.Count) {
        Write-Host '[fulfill] 机密文件未备齐，拒绝注册（防空转任务假装在履约）：' -ForegroundColor Red
        $missing | ForEach-Object { Write-Host ("  缺 " + $_) }
        Write-Host '按脚本头注把三件放进去后重跑 -Register。'
        exit 2
    }
    $tr = ('powershell -NoProfile -ExecutionPolicy Bypass -File "{0}"' -f $PSCommandPath)
    schtasks /Create /TN $TaskName /SC MINUTE /MO $IntervalMin /TR $tr /F | Out-Null
    if ($LASTEXITCODE -eq 0) {
        Write-Host ("[fulfill] 已注册计划任务 {0}（每 {1} 分钟一轮；日志 logs\fulfill\）" -f $TaskName, $IntervalMin)
    } else {
        Write-Host '[fulfill] schtasks 注册失败（需管理员权限？）' -ForegroundColor Red
    }
    exit $LASTEXITCODE
}

# ── 单次履约轮（计划任务入口）────────────────────────────────────────────────
$logDir = Join-Path $root 'logs\fulfill'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir ('fulfill_{0}.log' -f (Get-Date -Format 'yyyyMMdd'))

$missing = Get-MissingSecrets
if ($missing.Count) {
    ('[fulfill] {0} 机密缺失，跳过本轮: {1}' -f (Get-Date -Format 'o'), ($missing -join '; ')) |
        Out-File $log -Append -Encoding utf8
    exit 2
}

$env:CHATX_SITE = (Get-Content -LiteralPath $siteFile -Raw).Trim()
$env:ADMIN_KEY  = (Get-Content -LiteralPath $keyFile -Raw).Trim()

Set-Location $root
('[fulfill] tick {0}{1}' -f (Get-Date -Format 'o'), $(if ($DryRun) { ' (dry-run)' } else { '' })) |
    Out-File $log -Append -Encoding utf8
$pyArgs = @('scripts/fulfill_chatx_watch.py', '--priv', $privFile)
if ($DryRun) { $pyArgs += '--dry-run' }
# stderr 记录逐条转字符串：防 PS 把 python stderr 包成 NativeCommandError 多行噪音
& python @pyArgs 2>&1 | ForEach-Object { $_.ToString() } | Out-File $log -Append -Encoding utf8
$rc = $LASTEXITCODE
('[fulfill] exit={0}' -f $rc) | Out-File $log -Append -Encoding utf8

# 日志保留 30 份（按日轮转）
Get-ChildItem $logDir -Filter 'fulfill_*.log' | Sort-Object Name -Descending |
    Select-Object -Skip 30 | Remove-Item -Force -ErrorAction SilentlyContinue
exit $rc
