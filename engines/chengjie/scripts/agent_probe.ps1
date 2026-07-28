# agent_probe.ps1 - pre-flight probe for multi-agent shared-worktree work.
# ASCII-only output (PS5.1 GBK decode lesson, see watchdog_emotion_tts.ps1).
#
# What it answers before you touch any file:
#   1) Which dirty files were modified recently (someone else mid-flight?)
#   2) Is the target a known collision hot-zone?
#   3) Instance restart cooldown state (piggyback instead of double-restart/FLAP)
#
# Usage:  powershell -ExecutionPolicy Bypass -File scripts\agent_probe.ps1
#         [-HotMinutes 10] [-WindowMinutes 30]
# Exit code is always 0 (advisory tool, never blocks).

param(
    [int]$HotMinutes = 10,
    [int]$WindowMinutes = 30
)

$ErrorActionPreference = 'SilentlyContinue'
$engineRoot = Split-Path -Parent $PSScriptRoot
Set-Location $engineRoot

# Collision hot-zones: files/areas repeatedly touched by parallel agent lines
# (2026-07-28 incidents: unified_inbox 3-way edits, ops_overview near-miss,
#  shared/copilot dumb-button + theme-token gaps, i18n pack merge races).
$hotZoneFiles = @(
    'src/web/templates/unified_inbox.html',
    'src/web/templates/ops_overview.html',
    'src/skills/skill_manager.py'
)
$hotZonePrefixes = @(
    'shared/copilot/',
    'desktop/renderer/shared/copilot/',
    'src/web/i18n_packs/'
)

function Resolve-RepoFile([string]$rel) {
    # git may print paths repo-root-relative; engine cwd needs the prefix stripped
    if (Test-Path -LiteralPath (Join-Path $engineRoot $rel) -PathType Leaf) { return $rel }
    if ($rel.StartsWith('engines/chengjie/')) {
        $s = $rel.Substring('engines/chengjie/'.Length)
        if (Test-Path -LiteralPath (Join-Path $engineRoot $s) -PathType Leaf) { return $s }
    }
    return $null
}

$now = Get-Date
Write-Output ("=== agent probe @ {0} (window {1} min, active {2} min) ===" -f `
    $now.ToString('HH:mm:ss'), $WindowMinutes, $HotMinutes)
Write-Output ''
Write-Output '--- [1/2] dirty files recently modified (other lines mid-flight?) ---'

$rows = @()
foreach ($ln in (git status --short 2>$null)) {
    if (-not $ln -or $ln.Length -lt 4) { continue }
    $rel = $ln.Substring(3).Trim().Trim('"')
    if ($rel -match ' -> ') { $rel = ($rel -split ' -> ')[-1].Trim('"') }
    $res = Resolve-RepoFile $rel
    if (-not $res) { continue }
    $age = ($now - (Get-Item -LiteralPath (Join-Path $engineRoot $res)).LastWriteTime).TotalMinutes
    if ($age -gt $WindowMinutes -or $age -lt 0) { continue }
    $zone = ''
    if ($hotZoneFiles -contains $res) { $zone = '  [HOT-ZONE]' }
    else {
        foreach ($pre in $hotZonePrefixes) {
            if ($res.StartsWith($pre)) { $zone = '  [HOT-ZONE]'; break }
        }
    }
    $flag = 'recent'
    if ($age -le $HotMinutes) { $flag = 'ACTIVE' }
    $rows += [pscustomobject]@{ Age = [math]::Round($age, 1); Flag = $flag; File = $res; Zone = $zone }
}

if ($rows.Count -eq 0) {
    Write-Output '  (none) - no dirty file touched inside the window; safe to start.'
} else {
    foreach ($r in ($rows | Sort-Object Age)) {
        Write-Output ('  {0,6:N1} min  {1,-6}  {2}{3}' -f $r.Age, $r.Flag, $r.File, $r.Zone)
    }
    Write-Output ''
    Write-Output '  ACTIVE (<= active window) = someone is likely editing it RIGHT NOW.'
    Write-Output '  Touching an ACTIVE file, especially a HOT-ZONE, needs negotiation first.'
}

Write-Output ''
Write-Output '--- [2/2] instance restart cooldown (piggyback, do not double-restart) ---'
$cdDir = 'D:\chengjie-instances\.ops\restart_cooldown'
if (Test-Path $cdDir) {
    $any = $false
    foreach ($f in (Get-ChildItem $cdDir -Filter *.json)) {
        $any = $true
        $age = ($now - $f.LastWriteTime).TotalMinutes
        $hint = 'clear - normal restart discipline applies (batch your changes)'
        if ($age -lt 10) { $hint = 'IN COOLDOWN - restart will be refused; wait or verify hot-reload covers you' }
        elseif ($age -lt 30) { $hint = 'recent restart - your on-disk .py is LIKELY ALREADY LOADED (shared code root); verify via a read-only API probe before restarting again (FLAP window!)' }
        Write-Output ('  {0,-32} {1,7:N1} min ago  {2}' -f $f.Name, [math]::Round($age, 1), $hint)
    }
    if (-not $any) { Write-Output '  (no cooldown files yet)' }
} else {
    Write-Output ("  (cooldown dir not found: {0})" -f $cdDir)
}

Write-Output ''
Write-Output 'remember: template/i18n/shared-component saves go LIVE immediately (hot reload);'
Write-Output '          every intermediate save must be self-consistent (no half-wired buttons).'
exit 0
