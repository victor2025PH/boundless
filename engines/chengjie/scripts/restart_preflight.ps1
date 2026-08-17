# restart_preflight.ps1 - one-command GO/NO-GO check before a shared-tree restart.
# ASCII-only output (PS5.1 GBK decode lesson).
#
# Why this exists (2026-08-06, distilled from the conn-starvation batch restart):
# a restart on the shared code root loads EVERY line's on-disk .py - the safe
# sequence is five separate checks that until tonight lived only in operator
# memory (probe quiet -> syntax preflight -> app assembly -> -Advise -> judge).
# Any forgotten step turns a routine restart into a 2 AM boot failure. This tool
# chains them read-only and prints the exact restart command only on GO.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File scripts\restart_preflight.ps1
#     [-Instance zhiliao]      target instance (default zhiliao)
#     [-SkipActiveCheck]       accept recent dirty-file activity (it is YOURS)
#     [-ExtraTests a.py,b.py]  extra pytest files (e.g. sibling batch tests)
#
# Read-only: never restarts, never writes. Exit 0 = GO, 1 = NO-GO.

param(
    [string]$Instance = 'zhiliao',
    [switch]$SkipActiveCheck,
    [string[]]$ExtraTests = @()
)

$ErrorActionPreference = 'Continue'
$engineRoot = Split-Path -Parent $PSScriptRoot
Set-Location $engineRoot
$fails = @()

Write-Output ("=== restart preflight for '{0}' @ {1} ===" -f $Instance, (Get-Date).ToString('HH:mm:ss'))

# --- [1/5] other lines mid-flight? (10 min dirty activity + intent board) ---
Write-Output ''
Write-Output '--- [1/5] shared-tree activity (a restart loads EVERY line\''s on-disk code) ---'
$now = Get-Date
$active = @()
foreach ($ln in (git status --short 2>$null)) {
    if (-not $ln -or $ln.Length -lt 4) { continue }
    $rel = $ln.Substring(3).Trim().Trim('"')
    if ($rel -match ' -> ') { $rel = ($rel -split ' -> ')[-1].Trim('"') }
    $p = Join-Path $engineRoot $rel
    if (-not (Test-Path -LiteralPath $p -PathType Leaf)) {
        if ($rel.StartsWith('engines/chengjie/')) {
            $p = Join-Path $engineRoot $rel.Substring('engines/chengjie/'.Length)
        }
        if (-not (Test-Path -LiteralPath $p -PathType Leaf)) { continue }
    }
    $age = ($now - (Get-Item -LiteralPath $p).LastWriteTime).TotalMinutes
    if ($age -ge 0 -and $age -le 10) { $active += ('  {0,5:N1} min  {1}' -f [math]::Round($age, 1), $rel) }
}
if ($active.Count -gt 0) {
    $active | Select-Object -First 15 | ForEach-Object { Write-Output $_ }
    if ($SkipActiveCheck) {
        Write-Output '  ACTIVE files present but -SkipActiveCheck given (operator says they are theirs).'
    } else {
        Write-Output '  NO-GO: files changed in the last 10 min - a sibling may be mid-save.'
        Write-Output '         wait for quiet, or re-run with -SkipActiveCheck if these are YOUR saves.'
        $fails += 'active-files'
    }
} else {
    Write-Output '  quiet (no dirty file touched in 10 min).'
}
$intDir = 'D:\chengjie-instances\.ops\agent_intents'
if (Test-Path $intDir) {
    Write-Output '  declared intents (piggyback batches - are they complete?):'
    foreach ($f in (Get-ChildItem $intDir -Filter *.txt | Sort-Object LastWriteTime -Descending)) {
        $ih = ($now - $f.LastWriteTime).TotalHours
        if ($ih -gt 24) { continue }
        $body = (Get-Content -LiteralPath $f.FullName -Encoding UTF8 | Select-Object -Skip 1) -join ' '
        Write-Output ('    {0,5:N1} h   {1}' -f [math]::Round($ih, 1), $body)
    }
    # Piggyback triage (2026-08-12): the raw dump above answers "who is working",
    # not the restart question "whose on-disk state do I load?". Bucket by the
    # marker words lines already use in intents:
    #   riders    piggyback / AWAITS RESTART  -> declared ready; name them in -Reason
    #             (a LANDED intent can still carry AWAITS RESTART - it stays a rider)
    #   in-flight no LANDED, no marker        -> their half-done .py loads TOO;
    #                                            confirm with that line before GO
    #   silent    zero restart debt / landed  -> nothing to load, safe to ignore
    # [pre-restart] suffix (stale-intent hygiene, same session): the intent file
    # predates the latest restart - any declared .py debt was already loaded then;
    # weigh it accordingly and nudge the owner to refresh or -Done.
    $lastRestartTs = $null
    $cdProbe = Get-ChildItem 'D:\chengjie-instances\.ops\restart_cooldown' -Filter *.json -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if ($cdProbe) { $lastRestartTs = $cdProbe.LastWriteTime }
    $riders = @(); $inflight = @(); $silentN = 0
    foreach ($f in (Get-ChildItem $intDir -Filter *.txt | Sort-Object LastWriteTime -Descending)) {
        $ih = ($now - $f.LastWriteTime).TotalHours
        if ($ih -gt 24) { continue }
        $body = (Get-Content -LiteralPath $f.FullName -Encoding UTF8 | Select-Object -Skip 1) -join ' '
        $short = if ($body.Length -gt 140) { $body.Substring(0, 140) + '...' } else { $body }
        $preTag = ''
        if ($lastRestartTs -and ($f.LastWriteTime -lt $lastRestartTs)) { $preTag = ' [pre-restart]' }
        $row = ('    {0,5:N1} h {1} {2}' -f [math]::Round($ih, 1), $preTag, $short)
        if ($body -match 'zero restart debt|no \.py (in src )?touched') { $silentN++ }
        elseif ($body -match 'piggyback|AWAITS RESTART|rides (the )?next restart') { $riders += $row }
        elseif ($body -match '^\s*LANDED') { $silentN++ }
        else { $inflight += $row }
    }
    Write-Output '  piggyback triage (who does this restart actually load?):'
    if ($riders.Count) {
        Write-Output '   riders - declared ready, name them in -Reason:'
        $riders | ForEach-Object { Write-Output $_ }
    }
    if ($inflight.Count) {
        Write-Output '   in-flight - NOT declared ready; their on-disk .py loads too, confirm before GO:'
        $inflight | ForEach-Object { Write-Output $_ }
    }
    if ((-not $riders.Count) -and (-not $inflight.Count)) {
        Write-Output '   (no riders, no in-flight batches - clean load)'
    }
    if ($silentN) { Write-Output ('   ({0} landed / zero-restart-debt intents omitted)' -f $silentN) }
    Write-Output '   [pre-restart] = declared before the latest restart (debt likely already loaded;'
    Write-Output '                   owner should refresh the intent or -Done it).'
}

# --- [2/5] dirty .py syntax preflight (hard gate here, unlike gate_sweep's warn) ---
Write-Output ''
Write-Output '--- [2/5] dirty .py syntax preflight (half-saved sibling file = boot failure) ---'
$repoRoot = (git rev-parse --show-toplevel 2>$null)
$listFile = Join-Path $env:TEMP 'restart_preflight_dirty_py.txt'
git -C $repoRoot status --porcelain -- '*.py' |
    ForEach-Object { ($_ -replace '^..\s+', '').Trim().Trim('"') } |
    Where-Object { $_ -like '*.py' } |
    ForEach-Object { Join-Path $repoRoot $_ } |
    Where-Object { Test-Path $_ } |
    Set-Content -Path $listFile -Encoding utf8
python -c @"
import py_compile, sys
sys.stdout.reconfigure(encoding='utf-8')
paths = [l.strip() for l in open(sys.argv[1], encoding='utf-8-sig') if l.strip()]
bad = []
for p in paths:
    try:
        py_compile.compile(p, cfile=None, doraise=True)
    except Exception as e:
        bad.append((p, str(e).splitlines()[0][:150]))
print('  checked %d dirty .py, syntax errors: %d' % (len(paths), len(bad)))
for p, e in bad:
    print('  BAD %s' % p)
    print('      %s' % e)
sys.exit(1 if bad else 0)
"@ $listFile
if ($LASTEXITCODE -ne 0) { $fails += 'py-syntax' }

# --- [3/5] app assembly + optional sibling batch tests ---
Write-Output ''
Write-Output '--- [3/5] app assembly test (create_app + full route registry) ---'
$testFiles = @('tests/test_admin_route_inventory.py') + @($ExtraTests | Where-Object { $_ })
python -m pytest @testFiles -q --tb=line
if ($LASTEXITCODE -ne 0) { $fails += 'assembly-tests' }

# --- [4/5] restart_instance -Advise (dirty gate / config dup keys / port state) ---
Write-Output ''
Write-Output '--- [4/5] restart_instance -Advise ---'
$adviseScript = Join-Path $engineRoot '..\..\deploy\instances\restart_instance.ps1'
if (-not (Test-Path $adviseScript)) {
    $adviseScript = 'D:\workspace\boundless\deploy\instances\restart_instance.ps1'
}
if (Test-Path $adviseScript) {
    powershell -ExecutionPolicy Bypass -File $adviseScript -Instance $Instance -Advise 2>&1 |
        ForEach-Object { Write-Output ('  ' + $_) }
} else {
    Write-Output '  NO-GO: restart_instance.ps1 not found (deploy tree moved?)'
    $fails += 'advise-missing'
}

# --- [5/5] manual-restart cooldown (>=30 min between manual restarts) ---
Write-Output ''
Write-Output '--- [5/5] manual restart interval ---'
$cd = Join-Path 'D:\chengjie-instances\.ops\restart_cooldown' ("last_restart_{0}.json" -f $Instance)
if (Test-Path $cd) {
    $ageMin = ((Get-Date) - (Get-Item $cd).LastWriteTime).TotalMinutes
    Write-Output ('  last restart {0:N1} min ago' -f $ageMin)
    if ($ageMin -lt 30) {
        Write-Output '  NO-GO: inside the 30 min manual-restart interval (watchdog self-heal exempt).'
        $fails += 'cooldown'
    }
} else {
    Write-Output '  (no cooldown ledger yet)'
}

# --- advisory: restart batching windows (P0-3 2026-08-12; informational, never NO-GO) ---
# Ledger baseline: 8.4 restarts/day avg, 88% dev batches - every one is a 15-30s
# seat-visible outage window. Non-emergency batches should converge on three daily
# windows; emergencies and watchdog self-heal are exempt by design.
Write-Output ''
Write-Output '--- advisory: restart batching windows (04:00 / 12:30 / 22:30, +/-45 min) ---'
$winDefs = @(@(4, 0), @(12, 30), @(22, 30))
$nowT = Get-Date
$inWin = $false
$bestNext = $null
foreach ($w in $winDefs) {
    $t = Get-Date -Hour $w[0] -Minute $w[1] -Second 0
    foreach ($cand in @($t.AddDays(-1), $t, $t.AddDays(1))) {
        $d = ($cand - $nowT).TotalMinutes
        if ([math]::Abs($d) -le 45) { $inWin = $true }
        if ($d -gt 0 -and ((-not $bestNext) -or ($cand -lt $bestNext))) { $bestNext = $cand }
    }
}
if ($inWin) {
    Write-Output '  inside a batching window - good time to load accumulated batches.'
} else {
    Write-Output ('  OUTSIDE batching windows; next window at {0:HH:mm}.' -f $bestNext)
    Write-Output '  non-emergency batches should ride a window (each restart = 15-30s seat outage);'
    Write-Output '  emergency fixes / watchdog self-heal exempt - this note never blocks GO.'
}

# --- advisory: last gate sweep (shared .ops record; informational, never NO-GO) ---
# A restart loads the whole tree; "what did the gate face look like last time
# anyone swept?" is context the operator should see. Deliberately advisory:
# a red sweep may be another line's ledgered debt (red ledger tracks age/owner);
# blocking this line's restart on that would be overreach.
Write-Output ''
Write-Output '--- advisory: last gate sweep (shared .ops record) ---'
$swPath = 'D:\chengjie-instances\.ops\last_sweep.json'
if (Test-Path $swPath) {
    try {
        $sw = Get-Content $swPath -Raw -Encoding UTF8 | ConvertFrom-Json
        $swAgeH = ((Get-Date) - [datetime]$sw.ts).TotalHours
        $swLine = ('  {0:N1} h ago  {1}  ({2} passed{3}{4})' -f [math]::Round($swAgeH, 1),
            ([string]$sw.verdict).ToUpper(), $sw.passed,
            $(if ($sw.full) { ', full' } else { '' }),
            $(if ($sw.transient_healed) { ', transient-healed' } else { '' }))
        Write-Output $swLine
        if (("$($sw.verdict)" -eq 'red') -and @($sw.failed).Count -gt 0) {
            @($sw.failed) | Select-Object -First 5 | ForEach-Object { Write-Output ('    red: ' + $_) }
            Write-Output '    (ownership/age in the red ledger; a sibling debt does not block YOUR restart)'
        }
        if ($swAgeH -gt 12) {
            Write-Output '  record is >12h old - consider a fresh gate_sweep before loading the tree.'
        }
    } catch {
        Write-Output ('  (sweep record unreadable: {0})' -f $_.Exception.Message)
    }
} else {
    Write-Output '  (no sweep record yet - gate_sweep writes it from 2026-08-12 on)'
}

# --- verdict ---
Write-Output ''
if ($fails.Count -eq 0) {
    Write-Output 'preflight: GO - all five checks green. Restart with:'
    Write-Output ('  powershell -ExecutionPolicy Bypass -File deploy\instances\restart_instance.ps1 -Instance {0} -Reason "batch: <what you are loading; name piggybacked sibling batches>"' -f $Instance)
    Write-Output 'after restart: gate_sweep -Full auto-verifies (verify_conn_fixes rides in it).'
    exit 0
} else {
    Write-Output ('preflight: NO-GO - failed checks: {0}' -f ($fails -join ', '))
    Write-Output ''
    Write-Output 'next steps (mechanism cross-refs):'
    if ($fails -contains 'active-files') {
        Write-Output '  active-files  -> wait for quiet, or -SkipActiveCheck if YOUR saves;'
        Write-Output '                  declare -Intent so siblings know what you are loading:'
        Write-Output '                  scripts\agent_probe.ps1 -Intent "batch: <theme>"'
    }
    if ($fails -contains 'py-syntax') {
        Write-Output '  py-syntax     -> fix BAD files listed in [2/5] (or ask the owning line);'
        Write-Output '                  half-saved .py on shared root = boot failure after restart.'
    }
    if ($fails -contains 'assembly-tests') {
        Write-Output '  assembly-tests-> create_app/route inventory red; do NOT restart until green.'
    }
    if ($fails -contains 'cooldown') {
        Write-Output '  cooldown      -> inside 30 min manual interval; piggyback verify instead:'
        Write-Output '                  your on-disk .py was likely already loaded by the last restart.'
    }
    if ($fails -contains 'advise-missing') {
        Write-Output '  advise-missing-> deploy\instances\restart_instance.ps1 path broken; fix tree first.'
    }
    Write-Output '  always        -> scripts\agent_probe.ps1 (dirty + intents + cooldown snapshot)'
    Write-Output '                  -Advise alone is not enough on a shared tree; this preflight is.'
    exit 1
}
