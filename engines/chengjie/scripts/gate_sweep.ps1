# gate_sweep.ps1 - one-command cross-line gate sweep for the shared worktree.
# ASCII-only output (PS5.1 GBK decode lesson).
#
# Protocol item 4 (AGENTS.md/CLAUDE.md "multi-agent shared-worktree protocol"):
# before wrapping up, every agent line runs the BROAD gates - not just its own
# domain tests. Both cross-line bugs on 2026-07-28 (dead button `_openGoalQuick`,
# dark-theme token gaps) were caught by someone ELSE running broad scans.
#
# Scope = gates that catch one line breaking another:
#   - frontend wiring   (inline handlers / dup ids / orphan refs / dynamic dot /
#                        inline js syntax / inline color ratchet)
#   - i18n contracts    (window.T keys resolve, bilingual coverage, sealed pages)
#   - shared components (web vs desktop byte parity, theme token completeness)
#   - route contracts   (admin route inventory, ops overview)
#
# Usage:  powershell -ExecutionPolicy Bypass -File scripts\gate_sweep.ps1
#         -Full     also run the whole test suite afterwards (delegates to
#                   scripts\regression.ps1, ~15-20 min on a busy box)
#         -NoSyntaxScan  skip the dirty-.py syntax preflight
# Exit code: pytest's (0 = all green).
#
# Step 0 is a dirty-.py SYNTAX preflight, and it deliberately only WARNS.
# Why it exists: the code root is shared, so ANY restart of the production
# instance loads every line's already-saved .py - a sibling caught mid-save
# (half-written file) turns your routine restart into a boot failure, and the
# restart script's dirty gate only distinguishes hot-reloadable vs must-restart,
# it never checks that the .py actually parses. Measured 2026-07-28: 207 dirty
# .py in the tree at once, so "eyeball it" is not an option.
# Why warn and not fail: a file being half-saved right now is legitimate - the
# signal you want is "do not restart this minute", not "your work is broken".

param(
    [switch]$Full,
    [switch]$NoSyntaxScan
)

$ErrorActionPreference = 'Continue'
$engineRoot = Split-Path -Parent $PSScriptRoot
Set-Location $engineRoot

if (-not $NoSyntaxScan) {
    Write-Output '=== [0/2] dirty .py syntax preflight (warn-only) ==='
    try {
        $repoRoot = (git rev-parse --show-toplevel 2>$null)
        if ($LASTEXITCODE -ne 0 -or -not $repoRoot) { throw 'not a git repo' }
        $listFile = Join-Path $env:TEMP 'gate_sweep_dirty_py.txt'
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
        bad.append((p, str(e).splitlines()[0][:160]))
print('  checked %d dirty .py, syntax errors: %d' % (len(paths), len(bad)))
for p, e in bad:
    print('  WARN unparseable (another line mid-save? DO NOT restart yet): %s' % p)
    print('       %s' % e)
"@ $listFile
    } catch {
        Write-Output ('  skipped syntax preflight: {0}' -f $_.Exception.Message)
    }
    Write-Output ''
}

$gates = @(
    'tests/test_inbox_inline_handlers_exported.py',
    'tests/test_rpa_inline_handlers_exposed.py',
    'tests/test_template_unique_ids.py',
    'tests/test_template_orphan_refs.py',
    'tests/test_template_dynamic_dot_access.py',
    'tests/test_template_inline_js_syntax.py',
    'tests/test_template_inline_color_ratchet.py',
    'tests/test_i18n_coverage.py',
    'tests/test_copilot_shared_sync.py',
    'tests/test_copilot_theme_tokens.py',
    'tests/test_admin_route_inventory.py',
    'tests/test_ops_overview.py',
    'tests/test_duel_bench_status.py'
)

$missing = @($gates | Where-Object { -not (Test-Path (Join-Path $engineRoot $_)) })
if ($missing.Count -gt 0) {
    Write-Output 'WARN: gate files missing (renamed? update scripts/gate_sweep.ps1):'
    $missing | ForEach-Object { Write-Output ("  - " + $_) }
    $gates = @($gates | Where-Object { Test-Path (Join-Path $engineRoot $_) })
}

Write-Output ("=== [1/2] gate sweep: {0} gate files ===" -f $gates.Count)
python -m pytest @gates -q --tb=line
$code = $LASTEXITCODE

if ($Full) {
    Write-Output ''
    Write-Output '=== -Full: whole-suite regression (scripts\regression.ps1) ==='
    powershell -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'regression.ps1')
    if ($LASTEXITCODE -ne 0) { $code = $LASTEXITCODE }
}

if ($code -eq 0) {
    Write-Output 'gate sweep: ALL GREEN'
} else {
    Write-Output ("gate sweep: FAILURES (exit {0}) - a red gate in a file you did not touch usually means ANOTHER line broke it; report, do not silently fix inside their active edit window." -f $code)
}
exit $code
