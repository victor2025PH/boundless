# OpenClaw weekly business report
# Wraps scripts/phase8_funnel_report.py and writes a markdown file under logs/reports/
#
# Usage:
#   weekly_report.bat               # default 7 days
#   weekly_report.bat --days 1      # last 24 hours
#   weekly_report.bat --actor agent_a   # only A side

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location $ProjectRoot

$reportDir = Join-Path $ProjectRoot "logs\reports"
if (-not (Test-Path $reportDir)) {
    New-Item -ItemType Directory -Path $reportDir -Force | Out-Null
}

$ts = Get-Date -Format 'yyyy-MM-dd_HHmm'
$reportFile = Join-Path $reportDir "weekly_$ts.md"

Write-Host "==========================================="
Write-Host "  OpenClaw Weekly Report"
Write-Host "==========================================="
Write-Host ""
Write-Host "Running phase8_funnel_report.py with args: $args"
Write-Host ""

# Run the python report and capture stdout
$header = @"
# OpenClaw Weekly Report

**Generated**: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')
**Args**: $($args -join ' ')

---

"@

$header | Out-File -FilePath $reportFile -Encoding UTF8

try {
    # phase8_funnel_report.py is at scripts/phase8_funnel_report.py
    $pyScript = Join-Path $ProjectRoot "scripts\phase8_funnel_report.py"
    if (-not (Test-Path $pyScript)) {
        Write-Host "[ERROR] scripts/phase8_funnel_report.py not found" -ForegroundColor Red
        exit 1
    }

    $output = & python $pyScript @args 2>&1
    $output | Out-File -FilePath $reportFile -Append -Encoding UTF8

    # EEF: append sibling PR frequency from origin/main commit log
    # (useful context: how busy was the sibling Claude / collaborator week?)
    $siblingSection = "`n`n## Sibling协同节奏 (origin/main squash-merged PRs)`n`n"
    foreach ($w in @(
        @{ label = '近 24 小时'; since = '24 hours ago' },
        @{ label = '近 7 天   '; since = '7 days ago'   },
        @{ label = '近 30 天  '; since = '30 days ago'  }
    )) {
        # PS native-exe arg splatting (avoid --since=$expansion space bug)
        $gitArgs = @('log', 'origin/main', '--pretty=%s', "--since=$($w.since)")
        $log = & git @gitArgs 2>$null
        $count = @($log | Where-Object { $_ -match '\(#\d+\)' }).Count
        $siblingSection += "- $($w.label) : $count PR`n"
    }
    $siblingSection += "`n> 数据来源: ``git log origin/main --since=...`` 匹配 ``(#NNN)`` PR 编号`n"
    Add-Content -Path $reportFile -Value $siblingSection -Encoding UTF8

    # IIK: append server crash stats from diagnose_crash (last 7 days)
    try {
        $diagPs = Join-Path $ProjectRoot "scripts\ops\diagnose_crash.ps1"
        if (Test-Path $diagPs) {
            $crashJsonRaw = & powershell -NoProfile -ExecutionPolicy Bypass -File $diagPs -Days 7 -Last 100 -Json 2>$null
            if ($crashJsonRaw) {
                $crashData = $crashJsonRaw | ConvertFrom-Json
                $crashCount = if ($crashData.crashes) { @($crashData.crashes).Count } else { 0 }
                $crashSection = "`n## 服务稳定性 (server.py 进程退出事件, 近 7 天)`n`n"
                $crashSection += "- 总 crash 数: **$crashCount**`n"
                if ($crashCount -gt 0) {
                    # Group by date
                    $byDate = @{}
                    foreach ($c in $crashData.crashes) {
                        $date = ($c.ts -split ' ')[0]
                        if (-not $byDate.ContainsKey($date)) { $byDate[$date] = 0 }
                        $byDate[$date]++
                    }
                    $crashSection += "`n按日期:`n"
                    foreach ($date in ($byDate.Keys | Sort-Object)) {
                        $crashSection += "  - $date : $($byDate[$date]) 次`n"
                    }
                    $crashSection += "`n> 详情: ``diagnose_crash.bat -Days 7``  根因: 多数为 F9 PG init failed (RUNBOOK §3 F9)`n"
                } else {
                    $crashSection += "`n稳定 (无 crash). 跑 ``status.bat`` 看实时状态.`n"
                }
                Add-Content -Path $reportFile -Value $crashSection -Encoding UTF8
            }
        }
    } catch {
        # diagnose_crash optional, ignore failure
    }

    Write-Host "Report written to: $reportFile" -ForegroundColor Green
    Write-Host ""
    Write-Host "--- Preview (first 30 lines) ---" -ForegroundColor DarkCyan
    Get-Content $reportFile -TotalCount 30
    Write-Host ""
    Write-Host "Open the full report: $reportFile" -ForegroundColor Cyan
} catch {
    Write-Host "[ERROR] Report failed: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
