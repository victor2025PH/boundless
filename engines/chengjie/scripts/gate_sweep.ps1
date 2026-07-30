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
    'tests/test_template_jinja_comment_trap.py',
    'tests/test_channel_page_render_integrity.py',
    'tests/test_template_inline_color_ratchet.py',
    # 品牌令牌桥的两个静默失效面（都不报错、不变红，只是颜色/交互悄悄不对）：
    # ① th-brand-bridge.css 靠「排在 theme-tokens.css 之后、同特异性后者胜」才能把
    #    --th-*-brand* 覆盖成品牌智连蓝，<head> 一被重排就退回旧紫蓝 #5b7cf6；
    # ② base.html 若漏定义 --accent，派生页（personas 等）无 fallback 的
    #    var(--accent) 整条声明失效 → 选中态没有强调色、.pcard-focused 的 outline
    #    不显示（键盘焦点框看不见，可访问性缺陷）。
    'tests/test_brand_token_bridge.py',
    # 裸 periwinkle 旧品牌蓝清零（2026-07-30 117 处收口为 color-mix(var(--p|--tk-brand|--bl-growth))；
    # 分类器单一事实源 tools/audit_legacy_blues.py，var() fallback / 生成物 / 注释豁免）
    'tests/test_legacy_blue_ratchet.py',
    'tests/test_i18n_coverage.py',
    'tests/test_copilot_shared_sync.py',
    'tests/test_copilot_theme_tokens.py',
    'tests/test_admin_route_inventory.py',
    'tests/test_ops_overview.py',
    'tests/test_duel_bench_status.py',
    # CWD 相对路径风险（生产进程 CWD = 实例数据根，非引擎根）：被服务静态资产/
    # 代码根资源用相对路径 → 本地一切正常、只在真实部署才错位。2026-07-29 实锤：
    # 自身头像写进 <数据根>/src/web/static/... → 永久 404（且指纹去重不重下）。
    'tests/test_static_asset_paths.py'
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

    # Multi-window coordinator is PURE FRONTEND logic (localStorage primary slot +
    # storage events + overlay). The static gates above can only prove "the handler
    # is exposed on window", NOT "mutual exclusion between two tabs actually holds",
    # and template edits are hot-reloaded straight to production. So verify it in a
    # real browser (read-only: opens two /workspace tabs, clicks the overlay button,
    # sends nothing). Missing playwright or an unreachable instance => SKIP, exit 0.
    Write-Output ''
    Write-Output '=== -Full: multi-window coordinator, real browser (tools\verify_multiwin_ui.py) ==='
    python (Join-Path $engineRoot 'tools\verify_multiwin_ui.py')
    if ($LASTEXITCODE -ne 0) { $code = $LASTEXITCODE }

    # Account rail scoped view (2026-07-29 incident class): "click account X, see
    # account X" is a user-level invariant spanning drawer CTA / rail chips / overview
    # dropdown / scoped fetch. Static gates cannot prove it; hot-reloaded templates
    # ship straight to production. Read-only; missing playwright / instance => SKIP.
    Write-Output ''
    Write-Output '=== -Full: account rail scoped view, real browser (tools\verify_account_rail_ui.py) ==='
    python (Join-Path $engineRoot 'tools\verify_account_rail_ui.py')
    if ($LASTEXITCODE -ne 0) { $code = $LASTEXITCODE }

    # Inbox density budget (2026-07-30). The agent stares at a 300px-wide list;
    # every extra chrome row above it hides one more conversation. Such regressions
    # are SILENT - nothing errors, nothing turns red, a screenshot still "looks
    # fine", there is simply one less conversation visible. Two real defects were
    # found that way the same day (saved-views bar 32px while holding zero views,
    # filter-summary 25px merely restating the already-highlighted tab), each only
    # because someone measured by hand. This pins the heights AND the "hide it only
    # when it adds nothing" invariants so the next added row gets named by a gate.
    # Read-only; opens one ALREADY-READ conversation so no unread state is touched.
    # Missing playwright / unreachable instance => SKIP exit 0.
    Write-Output ''
    Write-Output '=== -Full: inbox density budget, real browser (tools\verify_inbox_density.py) ==='
    python (Join-Path $engineRoot 'tools\verify_inbox_density.py')
    if ($LASTEXITCODE -ne 0) { $code = $LASTEXITCODE }

    # Inbox identity bar (2026-07-30). Persona outreach: the last glance before send
    # must name the effective persona. Conversation-level override already lived in
    # /api/persona/effective + cp-persona, but #identity-bar used to read account-level
    # accountMeta only - leaving inbox.ident.bar_conv an orphan i18n key. Async upgrade
    # + TTL cache closed that gap; this gate pins it. Override path is route-mocked
    # (never writes production bindings). Read-only otherwise (already-read rows only).
    # Missing playwright / unreachable instance => still runs static source wiring,
    # SKIP-equivalent exit 0 when wiring is intact.
    Write-Output ''
    Write-Output '=== -Full: inbox identity bar, real browser (tools\verify_inbox_identity.py) ==='
    python (Join-Path $engineRoot 'tools\verify_inbox_identity.py')
    if ($LASTEXITCODE -ne 0) { $code = $LASTEXITCODE }
}

if ($code -eq 0) {
    Write-Output 'gate sweep: ALL GREEN'
} else {
    Write-Output ("gate sweep: FAILURES (exit {0}) - a red gate in a file you did not touch usually means ANOTHER line broke it; report, do not silently fix inside their active edit window." -f $code)
}
exit $code
