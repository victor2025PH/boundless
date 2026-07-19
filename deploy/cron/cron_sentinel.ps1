# cron_sentinel.ps1 — Windows 引擎机 cron 哨兵（实施26）：巡检 \Boundless\ 计划任务上次结果，
# 有失败/超期未跑即经 VPS 中继端点 /api/ops/alert 告警（用 EVENT_INGEST_KEY，不在本机存 TG token）。
#
# 设计：与 VPS ledger-backup-cron 同款——失败边沿告警 + down-flag 幂等 + 恢复补发；成功静默。
# 判定：任务 LastTaskResult 非 0 且非良性码（267009=正在运行,267011=从未运行）即视为失败。
# 用法（计划任务每 30 分钟）：powershell -ExecutionPolicy Bypass -File deploy\cron\cron_sentinel.ps1 [-Prefix Boundless] [-BaseUrl https://bd2026.cc]

[CmdletBinding()]
param(
    [string]$Prefix    = 'Boundless',   # 只巡检名字含此前缀的计划任务
    [string]$BaseUrl   = '',            # 告警中继基址（缺省 env PERSONA_SYNC_BASE / https://bd2026.cc）
    [string]$IngestKey = '',            # 中继密钥（缺省 env EVENT_INGEST_KEY）
    [int]$StaleHours   = 26             # 上次运行超过此小时数（且应已跑过）视为超期；0=不查超期
)

$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}

$FlagDir = Join-Path $env:LOCALAPPDATA 'boundless-sentinel'
New-Item -ItemType Directory -Force -Path $FlagDir | Out-Null
$DownFlag = Join-Path $FlagDir 'cron.down'
$Log = Join-Path $FlagDir 'cron_sentinel.log'
function Say([string]$m) { $l = "[{0:yyyy-MM-dd HH:mm:ss}] {1}" -f (Get-Date), $m; Add-Content $Log $l; Write-Host $l }

$base = $BaseUrl
if (-not $base) { $base = [string]$env:PERSONA_SYNC_BASE }
if (-not $base) { $base = 'https://bd2026.cc' }
$key = $IngestKey
if (-not $key) { $key = [string]$env:EVENT_INGEST_KEY }

function Send-Alert([string]$text) {
    if (-not $key) { Say '无 EVENT_INGEST_KEY，跳过告警发送（仅落日志）'; return }
    try {
        $body = @{ text = $text; source = "cron@$env:COMPUTERNAME" } | ConvertTo-Json -Compress
        $r = Invoke-RestMethod -Uri ($base.TrimEnd('/') + '/api/ops/alert') -Method Post `
            -Headers @{ Authorization = "Bearer $key" } -ContentType 'application/json' -Body $body -TimeoutSec 15
        Say ("告警已发 sent=$($r.sent)/$($r.recipients)")
    } catch {
        Say "告警发送失败: $($_.Exception.Message)"
    }
}

# ── 巡检 \Boundless\ 任务 ────────────────────────────────────────────
$tasks = @(Get-ScheduledTask -ErrorAction SilentlyContinue | Where-Object { $_.TaskName -like "*$Prefix*" -or $_.TaskPath -like "*$Prefix*" })
if (-not $tasks.Count) { Say "未发现 $Prefix 计划任务，退出（本机可能未装运营 cron）"; exit 0 }

$benign = @(0, 267009, 267011)   # 0=成功 267009=正在运行 267011=从未运行
$failed = @()
foreach ($t in $tasks) {
    $info = $t | Get-ScheduledTaskInfo -ErrorAction SilentlyContinue
    if (-not $info) { continue }
    $rc = $info.LastTaskResult
    if ($benign -notcontains $rc) {
        $failed += ("{0}=rc{1}(@{2:MM-dd HH:mm})" -f $t.TaskName, $rc, $info.LastRunTime)
    }
    elseif ($StaleHours -gt 0 -and $info.LastRunTime -and $info.LastRunTime -gt [datetime]'2000-01-01' `
            -and ((Get-Date) - $info.LastRunTime).TotalHours -gt $StaleHours) {
        $failed += ("{0}=超期({1:MM-dd HH:mm} 未再跑)" -f $t.TaskName, $info.LastRunTime)
    }
}

if ($failed.Count) {
    $summary = ($failed -join ' · ')
    Say "发现异常任务 $($failed.Count) 个: $summary"
    Send-Alert "⛔ 运营 cron 异常（$($failed.Count)）: $summary"
    New-Item -ItemType File -Force -Path $DownFlag | Out-Null
    exit 1
}

# 全绿：若此前告过警，补发恢复并清 flag
if (Test-Path $DownFlag) {
    Say '任务全部恢复正常，补发恢复通知'
    Send-Alert "✅ 运营 cron 已全部恢复正常（$($tasks.Count) 个任务）"
    Remove-Item $DownFlag -Force
}
Say "巡检通过：$($tasks.Count) 个 $Prefix 任务全绿"
exit 0
