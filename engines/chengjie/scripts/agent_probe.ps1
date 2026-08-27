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
# ---- [1/4] index occupancy -------------------------------------------------
# 2026-08-27: blocked TWICE in one day on this. The git index is a SINGLE
# shared slot for the whole worktree - while any line has files staged, nobody
# else can stage/commit without either waiting or sweeping up the other line's
# files by accident (the exact way one batch got mixed into another earlier).
# Unlike dirty files (advisory - you can work around them), a held index is a
# HARD blocker, so it goes first. mtime of .git/index is only a HINT for "how
# long" (plumbing refreshes it), so the age is labelled as such rather than
# dressed up as precision - the staged list itself is the reliable part.
Write-Output '--- [1/4] git index occupancy (staged = every other line is blocked) ---'
$stagedNames = @(git diff --cached --name-only 2>$null | Where-Object { $_ })
if ($stagedNames.Count -eq 0) {
    Write-Output '  index FREE - safe to stage/commit'
} else {
    $idxFile = Join-Path (git rev-parse --git-dir 2>$null) 'index'
    $ageTxt = 'unknown'
    if (Test-Path $idxFile) {
        $mins = [math]::Round(($now - (Get-Item $idxFile).LastWriteTime).TotalMinutes, 1)
        $ageTxt = ("~{0} min (hint only)" -f $mins)
    }
    # Deliberately does NOT claim whose staging it is - the probe cannot tell, and
    # "your own forgotten staging" is exactly the case protocol rule 1 targets.
    Write-Output ("  !! index HELD (yours or another line's): {0} file(s), last touched {1}" -f `
        $stagedNames.Count, $ageTxt)
    foreach ($n in ($stagedNames | Select-Object -First 8)) { Write-Output ("     {0}" -f $n) }
    if ($stagedNames.Count -gt 8) {
        Write-Output ("     ... and {0} more" -f ($stagedNames.Count - 8))
    }
    Write-Output '  protocol: stage -> commit (or git reset) within a few minutes.'
    Write-Output '  do NOT git add -A / git commit -a now: you would sweep their files into your commit.'
    Write-Output '  tools/stage_hunks.py already refuses to run while foreign files are staged.'
}

Write-Output ''
Write-Output '--- [2/4] dirty files recently modified (other lines mid-flight?) ---'

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
Write-Output '--- [3/4] declared intents (what each line PURSUES, not just where it types) ---'
# Stale-intent hygiene (2026-08-12): intents declared BEFORE the latest instance
# restart have had their on-disk .py (if any) loaded already - the declared
# "in-flight / awaits restart" state is likely stale, polluting the preflight
# piggyback triage (measured: ~half of the in-flight bucket was overnight
# batches loaded by the 12:08/12:40 restarts, never -Done'd). Tag, never delete:
# the board is human speech; the machine only points at it.
$lastRestartTs = $null
$cdProbeDir = 'D:\chengjie-instances\.ops\restart_cooldown'
if (Test-Path $cdProbeDir) {
    $newest = Get-ChildItem $cdProbeDir -Filter *.json |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if ($newest) { $lastRestartTs = $newest.LastWriteTime }
}
if (Test-Path $intentDir) {
    $shown = $false
    $staleSeen = $false
    foreach ($f in (Get-ChildItem $intentDir -Filter *.txt | Sort-Object LastWriteTime -Descending)) {
        $iage = ($now - $f.LastWriteTime).TotalHours
        if ($iage -gt 24) { Remove-Item -LiteralPath $f.FullName -Force -ErrorAction SilentlyContinue; continue }
        $body = (Get-Content -LiteralPath $f.FullName -Encoding UTF8 | Select-Object -Skip 1) -join ' '
        $staleTag = ''
        if ($lastRestartTs -and ($f.LastWriteTime -lt $lastRestartTs)) {
            $staleTag = '   [pre-restart: state may be stale - refresh or -Done]'
            $staleSeen = $true
        }
        Write-Output ('  {0,5:N1} h   {1}{2}' -f [math]::Round($iage, 1), $body, $staleTag)
        $shown = $true
    }
    if (-not $shown) { Write-Output '  (none declared in the last 24h)' }
    if ($staleSeen) {
        Write-Output '  [pre-restart] = declared before the latest restart; any .py debt is already'
        Write-Output '                  loaded - owner should refresh (-Intent same theme) or -Done.'
    }
} else {
    Write-Output '  (none declared yet)'
}
Write-Output '  declare yours:  scripts\agent_probe.ps1 -Intent "theme (files/areas)"   [-Done to clear]'
Write-Output '  before restart: scripts\restart_preflight.ps1   (chains probe quiet + syntax + Advise + cooldown)'

Write-Output ''
Write-Output '--- [4/4] instance restart cooldown (piggyback, do not double-restart) ---'
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

# --- warn-only template compile smoke (2026-08-17 `){#` incident) ---
# Hot reload means a broken template save is a LIVE 500 the moment it lands;
# gates only catch it when someone runs gates (~25 min window that day).
# Probing runs at every line's kickoff, so check here too: silent when healthy,
# loud when broken, never blocks (exit 0 contract intact), errors swallowed.
try {
    $tcOut = & python tools/template_compile_check.py 2>&1
    if ($LASTEXITCODE -ne 0 -and $tcOut) {
        Write-Output ''
        Write-Output '--- [!] BROKEN TEMPLATE(S) ON DISK: those pages are 500 in production RIGHT NOW ---'
        $tcOut | Where-Object { $_ -match '^\s{2}\S' } | ForEach-Object { Write-Output ('  ' + $_) }
        Write-Output '  fix before anything else (usual culprit: CSS `){#id` read as Jinja comment -> `){ #id`);'
        Write-Output '  re-run once to rule out a mid-save transient; detail: python tools/template_compile_check.py'
    }
} catch { }

Write-Output ''
Write-Output 'remember: template/i18n/shared-component saves go LIVE immediately (hot reload);'
Write-Output '          every intermediate save must be self-consistent (no half-wired buttons).'
exit 0
