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
    # Meta Graph API 版本到期（2026-08-03 实锤：告警通道的 Messenger 端点钉着
    # v19.0，而它 2026-05-21 就被 Meta 移除了——已死 74 天无人知晓；同期三个 Meta
    # 集成各写各的版本常量 v25/v21/v19）。版本过期不在部署期报错，只在真发请求那刻
    # 4xx，而告警渠道平时零流量＝出事当口才发现发不出去。本门禁把版本收成单一事实
    # 源，并在离官方停用日 <60 天时硬失败（时间驱动，到点自己变红）。
    'tests/test_meta_graph_version.py',
    # 外部 API 版本生命周期（2026-08-03，上面那条的泛化）：v19.0 不是孤例——同一次
    # 排查发现 shopify_connector 钉着 2024-01，官方 2025-01-16 就到期了，**已死 564
    # 天**。且 Shopify 的失败模式更阴：过期不报错，静默 fall-forward 到别的版本，
    # 破坏性变更被悄悄应用而日志里一个字都没有。本门禁把「钉了外部版本 + 有公布死期」
    # 收成一张登记表，守三条：源码零散落字面量 / 每个 pin 离死期 >60 天 /
    # 登记表自己不许漏登记（否则只是把原 bug 搬到上一层）。
    'tests/test_external_api_lifecycle.py',
    # 幽灵 SQL（2026-08-01 双实锤：conversation_meta.claimed_by 让 churn-risks/
    # agent-qa-stats 全部署 500 两个月；messenger_rpa_runs.reply_lang 让 Messenger
    # 语言分布被 except 吞成静默恒空）。列名写错不在导入期/测试期报错，只在真实
    # 调用时炸——本门禁对 src|scripts|tools 全部完整 SQL 字面量做真 schema EXPLAIN
    # 预编译（DDL 自采联合库 + 四大 store 真实例），预编译期判定对吞异常免疫。
    'tests/test_sql_phantom_columns.py',
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
    'tests/test_seed_switch_upgrade_coverage.py',
    # LINE 媒体能力契约（2026-07-31）：编排器判「该号能不能发媒体」用的是
    # `hasattr(worker,"send_media")`，所以 LineProtocolWorker 是**按开关条件绑定**
    # 那个属性、而不是写成普通方法。谁顺手「简化」成 `async def send_media`，就等于
    # 把自拍/相册/克隆语音/命理 K 线在 LINE 上一次性全部放开（默认关的意义没了）。
    # 这类「读代码看不出、改了也不报错」的契约正是要靠门禁互验的东西。
    'tests/test_line_media.py',
    # 平台能力矩阵（2026-07-31）：docs/平台能力矩阵.md 由代码结构生成，这条钉住
    # 「生成物 == 代码」+「矩阵判据 == 编排器运行时那套 hasattr」+「开关说明是活的」
    # +「新 worker 不会漏进矩阵」。缘起＝当时代码里有三处能力注释是错的/已过期，
    # 手写能力清单必然漂移，所以改成生成 + 门禁钉。
    'tests/test_platform_matrix.py',
    # A 线出站富媒体镜像（2026-07-31）：pyrogram 直发的图/语音此前只往收件箱写一行
    # 纯文字占位 → 坐席看不到自家人设发出去的图，按 media_type 统计出站媒体的口径
    # 把整条 A 线漏掉（实测 TG 868 条出站只数出 1 条，实际文本占位 166 条）。
    # 这条钉住三个接线不变量：发图发布 canonical 原图（不是发完即删的去重副本）、
    # 语音发布夹在「发送→删文件」窗口内、两条语音路径共用同一文案函数。
    'tests/test_outbound_media_mirror.py',
    # P0-198 出站保真三防线接线（2026-07-31，客户机实录：中文原样发给英文客户 /
    # 同义改写三连发 / WhatsApp 灰色感叹号 / Signal 私钥进日志）：语言错配 409、
    # 近重复 409、Baileys ephemeral 三出站口、libsignal 脱敏、worker HOLD——
    # 全是「写了守卫没挂线」高发区，行为单测盖不住接线层。纯静态扫描。
    'tests/test_outbound_leak_guard_wiring.py',
    # P1-198 副驾体验收口接线（2026-07-31）：工坊 opener 模式分发、情绪状态行、
    # 回访任务诚实化 + 可关闭 flag、英雄卡收敛到客户&关系 tab、人设账号级默认
    # legacy 入口——双树（shared ↔ desktop/renderer/shared）关键锚点都断言，
    # 防「只改一棵树」的半吊子同步。纯静态扫描。
    'tests/test_p1_copilot_wiring.py',
    # companion 号发媒体能力（2026-07-31）：与 LINE 那条同型的「按开关条件绑定
    # send_media」契约——写成普通方法就一次性放开坐席手动发送 + B 线自动发媒体 +
    # 主动触达语音三条链（最后一条属运营决策）。另钉「走内层 pyrogram、不委派会
    # 二次镜像的 A 线 send_photo」，否则一次发送在坐席台留两行。
    'tests/test_companion_media_capability.py',
    # P2-198 直出模式 + 诊断包接线（2026-07-31）：gloss 双产线挂点、路由透传、
    # 对照区块**必须只读**（带填入按钮=复活 P0 修掉的中文泄漏）、诊断包路由/
    # 清单/settings 卡探活自洽、i18n 双语。纯静态扫描。
    'tests/test_p2_direct_output_wiring.py',
    # 配置重复键（2026-07-31 事故后补）：PyYAML 对重复键静默取最后一个，手插一个
    # 同名块会把前面整块丢掉——实测把 platform_login.telegram 整段清空，重启后
    # 5 个 TG 号无 worker 离线约 2 分钟。解析成功、启动正常、日志无异常，只有这条
    # 查得出。含「本机实例配置必须无重复键」（无实例部署的机器自动跳过）。
    # 同一检查已接进 restart_instance.ps1 前置闸门（真拦，见 -Advise 输出）。
    'tests/test_config_duplicate_keys.py'
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

    # AI thinking panel three-state machine (2026-08-01). Click "AI reply" must pop
    # loading INSTANTLY (skeleton/phase/elapsed/cancel), swap to ready in place, fail
    # to in-place retry; cancel must release the button; minimize pill must survive.
    # Pure frontend hot-reloaded to production; static gates cannot prove state flow.
    # Fully route-mocked (thread msgs / held smart-reply / beacon sink / empty drafts)
    # - burns no LLM, writes nothing, pollutes no funnel counters.
    Write-Output ''
    Write-Output '=== -Full: AI thinking panel three-state, real browser (tools\verify_dpick_panel.py) ==='
    python (Join-Path $engineRoot 'tools\verify_dpick_panel.py')
    if ($LASTEXITCODE -ne 0) { $code = $LASTEXITCODE }

    # Churn Alerts board (2026-08-01 RH revamp close-out). Capability probe ->
    # tri-state render -> dual-source tabs -> save-first queue -> in-page send:
    # all pure frontend + conditionally-registered endpoints, hot-reloaded straight
    # to production. This gate's FIRST run caught a 2-month-old phantom-column 500
    # (conversation_meta.claimed_by) that every static gate missed. Read-only:
    # never clicks generate/send/mark-sent (no LLM burn, no reactivation ledger
    # writes). Missing playwright / unreachable instance / no token => SKIP exit 0.
    Write-Output ''
    Write-Output '=== -Full: churn alerts board, real browser (tools\verify_relations_ui.py) ==='
    python (Join-Path $engineRoot 'tools\verify_relations_ui.py')
    if ($LASTEXITCODE -ne 0) { $code = $LASTEXITCODE }

    # Proactive-care page (2026-08-01 P0-P3 revamp). Health lights -> hero/run mode
    # switch -> contact picker (load/filter/select/restore) -> topic+time chips ->
    # pending cards vs audit table view switch -> no raw cs2_ i18n keys. All pure
    # frontend grown 176->700+ lines in one day, hot-reloaded straight to production;
    # its FIRST run caught the onboarding modal intercepting every click. Read-only:
    # never clicks schedule/send-now/cancel/preview (no LLM burn, no state writes).
    # Missing playwright / unreachable instance / no token => SKIP exit 0.
    Write-Output ''
    Write-Output '=== -Full: proactive care page, real browser (tools\verify_care_ui.py) ==='
    python (Join-Path $engineRoot 'tools\verify_care_ui.py')
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

    # Goal-card narrow-width layout + profile interaction chain (2026-08-01 P21).
    # The P20 vertical-stacking incident shipped precisely because no gate rendered
    # the component at the default 300px rail - static gates pin CSS source text,
    # not computed shadow-DOM layout. Deliberately NOT the login-to-instance style:
    # a self-contained file:// fixture page loads the real component files with a
    # fake fetch + fake sendBeacon, so it runs with the instance down, cannot
    # inflate the goal_slot_* ui-event funnel under review, and is immune to a
    # sibling mid-save on cp-i18n.js. Only missing playwright => SKIP exit 0.
    Write-Output ''
    Write-Output '=== -Full: goal card narrow-width + interaction, fixture browser (tools\verify_goal_card_ui.py) ==='
    python (Join-Path $engineRoot 'tools\verify_goal_card_ui.py')
    if ($LASTEXITCODE -ne 0) { $code = $LASTEXITCODE }
}

if ($code -eq 0) {
    Write-Output 'gate sweep: ALL GREEN'
} else {
    Write-Output ("gate sweep: FAILURES (exit {0}) - a red gate in a file you did not touch usually means ANOTHER line broke it; report, do not silently fix inside their active edit window." -f $code)
}
exit $code
