# Find expired "temporary" config changes — Phase 2 P1.5
#
# 扫 config/*.yaml (递归) 找 `# TEMPORARY until YYYY-MM-DD` 注释,
# 比对今天日期, 过期就警告. 防 4-21 / 4-26 同类事故 (yaml 临时改动
# 潜伏 6 天 → 5h 死循环).
#
# 用法:
#   check_temp_configs.ps1                # 走 config/, 控制台彩色输出
#   check_temp_configs.ps1 -Json          # 输出 JSON (status.ps1 用)
#   check_temp_configs.ps1 -Path other/   # 扫别的目录
#   check_temp_configs.ps1 -DaysAhead 7   # 提前 7 天提醒"即将过期"
#
# 退出码:
#   0 = 没过期
#   1 = 有即将过期 (within DaysAhead)
#   2 = 有已过期
#
# 注释格式约定 (写在 yaml 临时改动行的紧邻上方):
#   # TEMPORARY until 2026-05-15: <一句话说明原因 + 原值>
#   manual_gate:
#     enforce_preflight: false   # ← 临时改动
#
# 见 CLAUDE.md 段落"配置临时改动 TTL 约定".

param(
    [string]$Path = "",
    [switch]$Json,
    [int]$DaysAhead = 7
)

$ErrorActionPreference = 'Continue'
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
if (-not $Path) { $Path = Join-Path $ProjectRoot "config" }

if (-not (Test-Path $Path)) {
    if ($Json) {
        '{"error":"path not found","path":"' + $Path + '"}' | Write-Output
    } else {
        Write-Host "[ERROR] Path not found: $Path" -ForegroundColor Red
    }
    exit 2
}

$today = (Get-Date).Date
$findings = @()

# 扫所有 yaml (递归), 用 regex 找 TEMPORARY until YYYY-MM-DD
$files = Get-ChildItem -Path $Path -Recurse -Include *.yaml,*.yml -File -ErrorAction SilentlyContinue
foreach ($f in $files) {
    $lineNo = 0
    foreach ($line in (Get-Content -LiteralPath $f.FullName -ErrorAction SilentlyContinue)) {
        $lineNo++
        if ($line -match 'TEMPORARY\s+until\s+(\d{4}-\d{2}-\d{2})') {
            # 立即保存 date string, 避免后续 -match 覆盖 $Matches
            $dueStr = $Matches[1]
            $dueDate = $null
            try { $dueDate = [DateTime]::ParseExact($dueStr, 'yyyy-MM-dd', $null).Date }
            catch { continue }
            $daysLeft = ($dueDate - $today).Days
            $rel = $f.FullName.Substring($ProjectRoot.Length).TrimStart('\','/')
            # 提取注释文本 (TEMPORARY until 之后的部分)
            $note = ''
            if ($line -match 'TEMPORARY\s+until\s+\d{4}-\d{2}-\d{2}\s*:\s*(.+)$') {
                $note = $Matches[1].Trim()
            } elseif ($line -match 'TEMPORARY\s+until\s+\d{4}-\d{2}-\d{2}\s+(.+)$') {
                $note = $Matches[1].Trim()
            }
            $findings += [PSCustomObject]@{
                file      = $rel
                line      = $lineNo
                due       = $dueStr
                days_left = $daysLeft
                note      = $note
                severity  = if ($daysLeft -lt 0) { 'expired' }
                            elseif ($daysLeft -le $DaysAhead) { 'soon' }
                            else { 'ok' }
            }
        }
    }
}

# 输出
$expired = @($findings | Where-Object { $_.severity -eq 'expired' })
$soon = @($findings | Where-Object { $_.severity -eq 'soon' })
$ok = @($findings | Where-Object { $_.severity -eq 'ok' })

if ($Json) {
    @{
        total      = $findings.Count
        expired    = $expired.Count
        soon       = $soon.Count
        ok         = $ok.Count
        days_ahead = $DaysAhead
        findings   = $findings
    } | ConvertTo-Json -Depth 5
} else {
    Write-Host ""
    Write-Host "=== Temporary config TTL scan ===" -ForegroundColor Cyan
    Write-Host ("scanned {0} yaml file(s) under {1}" -f $files.Count, $Path) -ForegroundColor DarkGray
    Write-Host ("found {0} TTL marker(s): {1} expired, {2} due within {3}d, {4} ok" `
        -f $findings.Count, $expired.Count, $soon.Count, $DaysAhead, $ok.Count) -ForegroundColor DarkGray
    if ($findings.Count -eq 0) {
        Write-Host "   [OK] no TEMPORARY markers found" -ForegroundColor Green
        Write-Host "        (add '# TEMPORARY until YYYY-MM-DD: <reason>' for transient yaml changes)" -ForegroundColor DarkGray
    }
    foreach ($x in $expired) {
        Write-Host ("   [EXPIRED -{0}d] {1}:{2} due={3}" -f (-$x.days_left), $x.file, $x.line, $x.due) -ForegroundColor Red
        if ($x.note) { Write-Host ("                 {0}" -f $x.note) -ForegroundColor DarkRed }
    }
    foreach ($x in $soon) {
        Write-Host ("   [DUE in {0}d]   {1}:{2} due={3}" -f $x.days_left, $x.file, $x.line, $x.due) -ForegroundColor Yellow
        if ($x.note) { Write-Host ("                 {0}" -f $x.note) -ForegroundColor DarkYellow }
    }
    foreach ($x in $ok) {
        Write-Host ("   [OK +{0}d]      {1}:{2} due={3}" -f $x.days_left, $x.file, $x.line, $x.due) -ForegroundColor DarkGreen
    }
    Write-Host ""
}

# Exit code
if ($expired.Count -gt 0) { exit 2 }
elseif ($soon.Count -gt 0) { exit 1 }
else { exit 0 }
