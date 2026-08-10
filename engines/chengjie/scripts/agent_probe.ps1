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
#         [-Intent "theme (files/areas)"]   register/refresh what YOUR line works on
#         [-Intent "theme" -Done]           clear it when finished
# Exit code is always 0 (advisory tool, never blocks).
#
# Intent board (2026-08-05 lesson): two agent lines built near-identical
# verification tools for the same incident within the hour, only a code
# comment prevented double-shipping. mtime probing shows WHERE lines touch,
# not WHAT they pursue - so declare intent up front. ASCII themes preferred
# (PS5.1 GBK console). Entries expire after 24h automatically.

param(
    [int]$HotMinutes = 10,
    [int]$WindowMinutes = 30,
    [string]$Intent = '',
    [switch]$Done
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

# --- intent registration (before probing, so the board reflects this run) ---
$intentDir = 'D:\chengjie-instances\.ops\agent_intents'
if ($Intent) {
    New-Item -ItemType Directory -Path $intentDir -Force | Out-Null
    $slug = (($Intent.ToLower() -replace '[^a-z0-9]+', '-').Trim('-'))
    if ($slug.Length -gt 48) { $slug = $slug.Substring(0, 48) }
    if (-not $slug) { $slug = 'unnamed' }
    $f = Join-Path $intentDir ($slug + '.txt')
    if ($Done) {
        Remove-Item -LiteralPath $f -Force -ErrorAction SilentlyContinue
        Write-Output ("intent cleared: {0}" -f $slug)
        Write-Output '  (siblings no longer wait on this batch; clear stale intents the same way)'
        Write-Output '  did you sweep? scripts\gate_sweep.ps1 catches cross-gate misses'
        Write-Output '  (hand-picked test files skip ratchets like inline-color / ui-build freshness)'
    } else {
        # UTF8 file content; console output stays ASCII-safe elsewhere
        ("{0}`n{1}" -f $now.ToString('yyyy-MM-dd HH:mm'), $Intent) |
            Out-File -LiteralPath $f -Encoding UTF8 -Force
        Write-Output ("intent registered/refreshed: {0}" -f $slug)
        Write-Output '  when finished: re-run with -Done so restart_preflight / siblings stop waiting'
        Write-Output '  before restart: scripts\restart_preflight.ps1  (GO/NO-GO; never restarts itself)'
    }
    Write-Output ''
}

Write-Output ("=== agent probe @ {0} (window {1} min, active {2} min) ===" -f `
    $now.ToString('HH:mm:ss'), $WindowMinutes, $HotMinutes)
Write-Output ''
Write-Output '--- [1/3] dirty files recently modified (other lines mid-flight?) ---'

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
Write-Output '--- [2/3] declared intents (what each line PURSUES, not just where it types) ---'
if (Test-Path $intentDir) {
    $shown = $false
    foreach ($f in (Get-ChildItem $intentDir -Filter *.txt | Sort-Object LastWriteTime -Descending)) {
        $iage = ($now - $f.LastWriteTime).TotalHours
        if ($iage -gt 24) { Remove-Item -LiteralPath $f.FullName -Force -ErrorAction SilentlyContinue; continue }
        $body = (Get-Content -LiteralPath $f.FullName -Encoding UTF8 | Select-Object -Skip 1) -join ' '
        Write-Output ('  {0,5:N1} h   {1}' -f [math]::Round($iage, 1), $body)
        $shown = $true
    }
    if (-not $shown) { Write-Output '  (none declared in the last 24h)' }
} else {
    Write-Output '  (none declared yet)'
}
Write-Output '  declare yours:  scripts\agent_probe.ps1 -Intent "theme (files/areas)"   [-Done to clear]'
Write-Output '  before restart: scripts\restart_preflight.ps1   (chains probe quiet + syntax + Advise + cooldown)'

Write-Output ''
Write-Output '--- [3/3] instance restart cooldown (piggyback, do not double-restart) ---'
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
