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
    # 「忘 bump」探测：坐席前端面 mtime 比 ui-build.txt 新即红（修法 scripts/bump_ui_build.py；
    # 中批红=诚实状态，由收口方 bump）
    'tests/test_ui_build_freshness.py',
    'tests/test_i18n_coverage.py',
    'tests/test_copilot_shared_sync.py',
    'tests/test_copilot_theme_tokens.py',
    'tests/test_admin_route_inventory.py',
    'tests/test_ops_overview.py',
    # Messenger web sign-in is a CROSS-LANGUAGE contract with no compile-time guard:
    # Python provider f-strings the sidecar routes, Node server.js registers them via
    # Express. Rename either side and the other 404s SILENTLY - "Messenger access does
    # not work" reappears with nothing red, nothing thrown. This pins the sign-in +
    # relogin routes (the access core) so a drift during the turnkey refactor gets
    # named instead of shipping a silent break. Pure static text scan, zero side effect.
    # WhatsApp Baileys sidecar is structurally identical (same login trio + a reconnect
    # route); same silent-404 risk, same gate shape (shared core in tests/_sidecar_contract.py).
    'tests/test_messenger_sidecar_contract.py',
    'tests/test_whatsapp_sidecar_contract.py',
    # Alert alias coverage: every publish("*_alert") in src must have a NAMED subscribe
    # alias in _EVENT_ALIASES, else ops configuring a webhook can never receive it (alerts
    # into the void - the whole point of wiring the alert egress). The e2e _EMITTED_ALERTS
    # table is hand-maintained and measured drifted (9 real alerts missing); this scans
    # real source publish sites instead. Pure static, zero side effect.
    'tests/test_alert_alias_coverage.py',
    # Alert e2e delivery + completeness: each source *_alert has an end-to-end webhook
    # delivery case, and test_source_alerts_all_have_e2e_payload pins source==table both
    # ways (漏登记/死条目 both red) reusing the alias-coverage scanner as the single
    # source-of-truth. Closes the hand-maintained-table drift for good.
    'tests/test_alert_delivery_e2e.py',
    'tests/test_duel_bench_status.py',
    # CWD 相对路径风险（生产进程 CWD = 实例数据根，非引擎根）：被服务静态资产/
    # 代码根资源用相对路径 → 本地一切正常、只在真实部署才错位。2026-07-29 实锤：
    # 自身头像写进 <数据根>/src/web/static/... → 永久 404（且指纹去重不重下）。
    'tests/test_static_asset_paths.py',
    # 「升级安装拿不到新功能」类（2026-07-31，104 实机实锤：同一安装包全新装四平台
    # 扫码全可用、升级装 LINE/WA/Messenger 全灰「未启用」，重装也不修——数据目录在
    # %APPDATA%，覆盖安装不动它）。根因＝种子 config.desktop.min.yaml 只在配置文件
    # **不存在**时播种一次，产品决策却写在种子里 → 存量用户永远停在旧开关。
    # 三层守（全静态/纯函数，零副作用）：
    #   defaults —— 默认表 ↔ 种子双向 + 四平台读取函数三态接线 + **字面直读旁路守卫**
    #               （messenger 与 telegram 两次都是「表里声明了、读取函数漏接」，
    #                手工核实已证明不可靠，故改源码扫描自我强制）；
    #   upgrade  —— 用 104 真实陈旧配置形态验 diagnose_mode 的判定（弹窗真正消费的那层）；
    #   coverage —— 类别级：种子里为 true 的每个开关必须被两套机制之一覆盖，否则
    #               「加个开关但谁都不管」会静默通过其余所有门禁（＝104 原始形态）。
    # 冻结产物那一层不在 pytest 内，走 desktop: npm run smoke:upgrade（陈旧数据目录
    # 起打包后端，一次验读时解析 + 启动补齐）。
    'tests/test_platform_login_defaults.py',
    'tests/test_platform_login_upgrade.py',
    'tests/test_seed_switch_upgrade_coverage.py'
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

    # Draft-approval predictive badges (2026-07-30). Before an agent clicks Send, the
    # draft card must predict "sending as-is gets blocked" - age chip, block chip, a
    # greyed-out Send, Edit flagged as the way out. Backend _approve_block_reason has 8
    # cross-asserts on the DECISION; the last mile (does the frontend faithfully render
    # that approve_blocked field into grey + the right chip) had no browser guard - pure
    # frontend, hot-reloaded to production. The product invariant is "prediction must
    # match the guard exactly". Draft list is route-mocked with three states (''/age/
    # replied); never writes production. Read-only otherwise (already-read row only).
    Write-Output ''
    Write-Output '=== -Full: draft-approval predictive badges, real browser (tools\verify_inbox_draft_review.py) ==='
    python (Join-Path $engineRoot 'tools\verify_inbox_draft_review.py')
    if ($LASTEXITCODE -ne 0) { $code = $LASTEXITCODE }

    # Brand token RENDERED values (2026-07-30 brand unification close-out). Static
    # gates prove links/literals; they cannot prove the browser computes growth-blue
    # accents or actually fetches the Montserrat woff2 (lazy @font-face + the
    # Electron file:// relative-path 404 class). Read-only: /login render, desktop
    # renderer via file://, /workspace computed styles only. Missing playwright /
    # unreachable instance => SKIP exit 0.
    Write-Output ''
    Write-Output '=== -Full: brand token rendered values, real browser (tools\verify_brand_ui.py) ==='
    python (Join-Path $engineRoot 'tools\verify_brand_ui.py')
    if ($LASTEXITCODE -ne 0) { $code = $LASTEXITCODE }

    # Copilot write-channel CSRF credential (2026-07-31 persona-switch incident).
    # The write path used to depend on a host-page fetch patch + the Referer header;
    # any privacy setting / proxy stripping Referer killed every copilot write with a
    # silent generic error for TWO WEEKS. Client now self-carries X-CSRF-Token - but
    # that is pure frontend behavior, hot-reloaded straight to production. This gate
    # proves in a real browser: header present in native mode, writes still succeed in
    # the incident environment (iframe + Referer/Origin stripped), bare writes get a
    # structured 403 code=csrf. Probe conversation key only (bind then unbind).
    # Missing playwright / unreachable instance => SKIP exit 0.
    Write-Output ''
    Write-Output '=== -Full: copilot write-channel CSRF, real browser (tools\verify_copilot_write_csrf.py) ==='
    python (Join-Path $engineRoot 'tools\verify_copilot_write_csrf.py')
    if ($LASTEXITCODE -ne 0) { $code = $LASTEXITCODE }

    # Persona failtip recovery paths (2026-07-31 P3). P1 split "always retry" into
    # per-HTTP-status text + way-out buttons + state re-assertion; the pure classifier
    # is pinned by a node gate (cp-persona-failtype.test.js), but that cannot prove the
    # classification actually RENDERS into the right shadow-DOM buttons/copy. cp-persona
    # is a hot-reloaded Web Component. This assembles it with a fail-only fake client and
    # asserts: 401 -> [fail-reload]+re-assert, 403 csrf -> [fail-reload], 404 -> [fail-refresh],
    # network -> [fail-retry,fail-refresh] with NO state re-assertion. Zero side effect
    # (fake client never hits real endpoints; probe conv key). Missing playwright /
    # unreachable instance => SKIP exit 0.
    Write-Output ''
    Write-Output '=== -Full: persona failtip recovery paths, real browser (tools\verify_persona_failtip.py) ==='
    python (Join-Path $engineRoot 'tools\verify_persona_failtip.py')
    if ($LASTEXITCODE -ne 0) { $code = $LASTEXITCODE }
}

if ($code -eq 0) {
    Write-Output 'gate sweep: ALL GREEN'
} else {
    Write-Output ("gate sweep: FAILURES (exit {0}) - a red gate in a file you did not touch usually means ANOTHER line broke it; report, do not silently fix inside their active edit window." -f $code)
}
exit $code
