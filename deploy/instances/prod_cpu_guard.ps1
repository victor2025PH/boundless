# prod_cpu_guard.ps1 - keep render/scrape workloads below the production engine (2026-09-19)
#
# Why: ZHUJI-117 serves seats (zhiliao :18799, AboveNormal) and ALSO runs video renders
# (ffmpeg + Playwright chrome-headless-shell). On 09-19 they pinned all 8 cores at 100% and
# the seat inbox polls timed out -> "connection lost" banners with no restart at all.
# IFEO PerfOptions\CpuPriorityClass=5 already starts ffmpeg*/chrome-headless-shell at
# BelowNormal, but Chromium re-raises its own renderer/GPU children to AboveNormal at spawn,
# so a periodic demotion is needed to make the policy stick. Runs as a 1-minute scheduled
# task (Boundless-prod-cpu-guard). Idempotent; only logs when it changed something.
#
# Never touches python/main.py (the engine), Cursor, or anything not on the list below.
# Rollback: Unregister-ScheduledTask Boundless-prod-cpu-guard ; delete the IFEO PerfOptions keys.
$ErrorActionPreference = 'SilentlyContinue'
$log = 'D:\chengjie-instances\.ops\prod_cpu_guard.log'
$targets = '^ffmpeg', '^ffprobe', '^chrome-headless-shell$'
$changed = @()
foreach ($p in Get-Process) {
    $hit = $false
    foreach ($t in $targets) { if ($p.ProcessName -match $t) { $hit = $true; break } }
    if (-not $hit) { continue }
    $cur = [string]$p.PriorityClass
    if ($cur -in @('Normal', 'AboveNormal', 'High', 'RealTime')) {
        try {
            $p.PriorityClass = 'BelowNormal'
            $changed += ('{0}({1}) {2}->BelowNormal' -f $p.ProcessName, $p.Id, $cur)
        } catch {
            $changed += ('{0}({1}) {2}->FAILED:{3}' -f $p.ProcessName, $p.Id, $cur, $_.Exception.Message)
        }
    }
}
if ($changed.Count -gt 0) {
    if ((Test-Path $log) -and ((Get-Item $log).Length -gt 2MB)) { Move-Item $log "$log.1" -Force }
    Add-Content -Path $log -Value ('[{0:yyyy-MM-dd HH:mm:ss}] demoted {1}: {2}' -f (Get-Date), $changed.Count, ($changed -join '; ')) -Encoding UTF8
}
exit 0
