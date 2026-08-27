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
$sweepStart = Get-Date

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
    # Python 未定义名棘轮（2026-08-27 真活探针事故沉淀）。Step 0 above only proves a
    # dirty .py PARSES; the incident code parsed fine - health_watchdog's
    # _check_true_probes called Path(...) while the module had no pathlib import, the
    # NameError got swallowed by _tick's outer except at DEBUG level, and all four
    # true-probe domains silently stopped running for a whole restart cycle. The only
    # signal was a log line that STOPPED appearing. ~20 sibling _check_* methods share
    # that exact failure shape. Templates have had an inline-JS syntax gate for months
    # (one syntax error bricks the inbox); this is the Python-side equivalent.
    # Scope is deliberately just undefined names (pyflakes F821) - no full lint suite,
    # which would surface thousands of pre-existing warnings and get ignored.
    # Baseline 2026-08-27: src/ 23 hits / 12 files on first run - and 6 of those files
    # were REAL live defects the gate found on day one (4 missing imports, one encoding
    # corruption that had eaten a whole assignment, one nested helper reaching for a
    # nonexistent `request`), every one of them silently swallowed by an outer except.
    # All 6 fixed same day => ledger down to 13 hits / 6 files, all annotation-only.
    # tools/ and scripts/ have always been ZERO. pyflakes missing => SKIP, never a red.
    'tests/test_python_undefined_names.py',
    # 全站模板 Jinja 编译冒烟（2026-08-17 `){#` 事故沉淀）：任一模板编译失败=对应页
    # 热更新下此刻就是 500（unified_inbox=整个坐席台）。秒级、零上下文，放最前先爆。
    # 手跑同口径：python tools/template_compile_check.py
    'tests/test_template_jinja_compile.py',
    # 编码腐蚀门禁（2026-08-22 unified_inbox gb18030 乱码整存事故沉淀）：热更面
    # （模板/i18n包/共享组件/静态前端）不得出现 PUA/替换符/锟斤拷/非法 UTF-8——
    # 某编辑器用错编码保存一次=全站坐席界面裸奔乱码+恢复耗一条线一小时。秒级。
    'tests/test_template_encoding_guard.py',
    # 编码腐蚀指纹（2026-08-22 gb18030 事故沉淀）：热更面（模板/i18n包/共享组件/
    # 静态前端）不得出现 PUA/替换符/锟斤拷/非法 UTF-8——某编辑器一次错误编码保存
    # 把 unified_inbox 全文中文毁掉 3398 行并直上生产，恢复耗一条线一小时。秒级。
    'tests/test_template_encoding_guard.py',
    'tests/test_inbox_inline_handlers_exported.py',
    'tests/test_rpa_inline_handlers_exposed.py',
    # 哑图标门禁（2026-08-08）：图标名引用必须能在 ui_icons.js 注册表解析 + uiIcon 定义唯一
    # （unified_inbox 页内同名副本覆盖全局导致顶栏图标静默变空的实锤事故防复发）。
    'tests/test_ui_icon_registry.py',
    # 控件位 emoji 棘轮（2026-08-08 P2A）：工作台模板 emoji 计数只减不增，新控件走 SVG。
    'tests/test_workspace_emoji_ratchet.py',
    # i18n 端到端渲染（2026-08-27 补挂）：<html lang> 随语言、译表整包注入、日期一律
    # 走 wsFmt*（禁内联 Date+toLocale*，那用浏览器 locale 会无视应用语言设置）。
    # **它此前不在本清单里，于是烂了没人知道**——发现时 19 个失败：ui_locale 收口后
    # 断言过期（en 现为 en-US）×16，外加 _support.html 一处真回归（共享外壳里内联
    # 日期本地化，三个后台页一起中招）。补挂就是为了不再重演「无人看守 → 静默腐烂」。
    'tests/test_workspace_i18n_render.py',
    'tests/test_template_unique_ids.py',
    'tests/test_template_orphan_refs.py',
    # 跨作用域幽灵引用（2026-08-22 顶栏药丸全灭事故沉淀，IIFE 颗粒度）：A 作用域内
    # 定义、B 作用域裸调用、全站无 window 挂载 = 运行时必 ReferenceError 且被
    # promise .catch 吞掉零信号（_setPill/_ui 两例实锤，药丸/确认弹窗图标静默全灭）。
    'tests/test_template_cross_block_refs.py',
    # 编码完整性（2026-08-22 收件箱全站 500 事故沉淀）：UTF-8 严格解码 + 零 U+FFFD +
    # GBK 系乱码指纹全扫（模板/静态/i18n packs/copilot 双树）——热更新即生产，
    # 编辑器按 ANSI 误存一次 = 全坐席宕机，本门禁把损毁变成分钟级可见。
    'tests/test_encoding_integrity.py',
    # ops warn 卡「去处理」CTA 注册表：key 必须是真卡、目标必须有真路由（防静默孤儿/404）。
    'tests/test_ops_card_cta.py',
    # 模板 JS「无主标识符」门禁（2026-08-22 B39 workflows._TYPE_LABELS 删定义留引用
    # 整页崩事故沉淀；读取形态裸标识符全文件无声明痕迹=必然 ReferenceError）。
    'tests/test_template_undefined_identifiers.py',
    'tests/test_template_dynamic_dot_access.py',
    'tests/test_template_free_capture.py',
    'tests/test_template_dead_classes.py',
    'tests/test_static_js_free_capture.py',
    'tests/test_template_inline_js_syntax.py',
    'tests/test_template_jinja_comment_trap.py',
    # CSS 注释陷阱（2026-08-20 内测 B12 实锤）：注释正文里的「星号紧贴斜杠」（多见于
    # --a-*/--b 这类 token 枚举）提前闭合注释 → 真结束符成游离符 → 浏览器错误恢复把
    # 紧随其后的整条规则丢弃。花括号照样平衡、CSS 不报错，只是静默少一条规则：被吞的
    # .ws-ctx-menu(position:fixed) 让「更多操作/右键」菜单落到首屏外＝点了毫无反应。
    'tests/test_css_comment_trap.py',
    # 搁置弹窗键盘隔离（2026-08-20 内测 B18 实锤）：stopPropagation 不取消默认插入行为，
    # 映射外的键（傍晚只有 3 档 → 按 5）在焦点被右栏语音框持有时会原样落字。
    'tests/test_snooze_dialog_keys.py',
    # 全站模板真编译门禁（2026-08-17 事故沉淀）：comment-trap 抓「注释被静默吞段」
    # 形态，这条用生产同款 Environment 真编译抓「解析失败=页面 500」形态（当日实锤：
    # 额度横幅 CSS 写出 `){#` → unified_inbox 编译失败 → 工作台 500 十八分钟）。
    'tests/test_templates_jinja_compile.py',
    # 预算触顶「触发时可见」链路（2026-08-17）：默认 500 + spec 同步 + alert payload
    # 三元组/budget 段 + SSE 白名单 + 中央弹窗/结构化确认弹窗接线 + i18n 双语。
    'tests/test_budget_alert_popup.py',
    'tests/test_channel_page_render_integrity.py',
    'tests/test_template_inline_color_ratchet.py',
    # 主题调色板 WCAG 对比度契约（2026-08-04 白天模式可读性收口）：白天模式的
    # 亮值历史上是暗色收口时"字节级保留"的字面量，从未按白底校准（t3 白底 2.5:1、
    # 状态徽章 2.1~3.8:1）。这条把"哪个前景配哪个面 ≥ 多少"钉成两主题各测的
    # 可执行契约（含 rgba 层叠合成、var 链解析、债务表防"永久白名单"化）。
    'tests/test_theme_contrast_gate.py',
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
    # 输入控件主题三档门禁（2026-08-02）：① color/background 必须成对（.tag-add-input
    # 暗色隐形事故）；② workspace css 无 fallback 的 var() 必须解析（--bg-main 幽灵
    # token）；③ 全站模板 <style> 按**继承链**解析（--t1 跨页掩护 209 处两轮清零；
    # 静态资产松度经 tools/audit_template_var_links.py 实证零暴露）。
    'tests/test_input_theme_contrast.py',
    # URL 主题钉的两条不变量（静态一层；行为侧在 -Full 的 verify_theme_pin_isolation.py，
    # 实例没起时它 SKIP，而这三个读者全是热更新直上生产）：钉不许落进跨窗口共享键
    # cp_theme（2026-08-15「后台点了没反应」），且钉是初始默认不是锁——显式选档必须
    # 当场释放、释放标记只许进 sessionStorage（2026-08-16「亮色/暗色点了没反应」）。
    'tests/test_theme_pin_window_local.py',
    # 「忘 bump」探测：坐席前端面 mtime 比 ui-build.txt 新即红（修法 scripts/bump_ui_build.py；
    # 中批红=诚实状态，由收口方 bump）
    'tests/test_ui_build_freshness.py',
    'tests/test_i18n_coverage.py',
    'tests/test_copilot_shared_sync.py',
    'tests/test_copilot_theme_tokens.py',
    # copilot 词典键可解析 + /copilot 资源两宿主 ?v= 一致（2026-08-22 cp.app.h_nurture
    # 裸键事故沉淀；与 test_cp_app_i18n_keys 互锁，本条独有面=组件静态字面量+缓存戳）
    'tests/test_copilot_i18n_keys.py',
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
    # 实施72（2026-08-27 账号身份错乱事故）：登录身份决议层（登录位换人→隔离态：
    # 不补人设/自动化封顶 review/显式转正）+ 重连积压封顶 + 外机自有号对端封顶 +
    # 补收时间戳诚实化（合成 ts 打标→SLO 剔除/气泡「≈」/拟稿上下文防线）+
    # 回填合并语义（not_empty 拒收竞态=13/14 丢史的回归钉）。这些层横跨
    # session-status/effective_automation/persona_voice/store 四个多线热点，
    # 任何一线重构漏接都=身份安全防线静默失效，必须进 sweep 互验。
    'tests/test_account_identity.py',
    'tests/test_reconnect_backlog_and_fleet.py',
    'tests/test_messenger_history_backfill.py',
    'tests/test_failed_outbound_trace.py',
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
    # Fixture time bombs (2026-08-12): hardcoded calendar dates written into
    # *trend* data fixtures self-detonate when the consumer's now-anchored
    # window slides past them (duel_bench blew on 08-11 with NOBODY's change =
    # unowned red for two days). Anchor fixture dates to datetime.now().
    'tests/test_fixture_time_bombs.py',
    # CWD 相对路径风险（生产进程 CWD = 实例数据根，非引擎根）：被服务静态资产/
    # 代码根资源用相对路径 → 本地一切正常、只在真实部署才错位。2026-07-29 实锤：
    # 自身头像写进 <数据根>/src/web/static/... → 永久 404（且指纹去重不重下）。
    'tests/test_static_asset_paths.py',
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
    'tests/test_config_duplicate_keys.py',
    # ── 小智（AI 助手）门禁族（实施73/74，2026-08-27）──────────────────────
    # 五个文件此前都不在本清单里 = 别的线收口时跑不到，改语料/改路由的回归
    # 只有本线自己跑才发现。它们全是静态或纯确定性（BM25/无网络无模型），
    # 秒级完成，进 sweep 的代价可以忽略。
    #
    # 面板不变量：意图埋点（漏斗分母，删一枚就少一段真相且不报错）、画布令牌
    # 单源、入口行不得退回 dashed / hint 不得退回截断。
    'tests/test_assistant_beacons.py',
    # 报障闭环：报障别名必须在告警关注集里（不在=「点了报障没人收到」而自检
    # 判 healthy）、前端状态表必须等于后端 VALID_STATUSES、零命中必给报障入口。
    'tests/test_bug_report_loop.py',
    # 使用帮助覆盖率棘轮：没有 how-to 的页面数只降不升（新增页面不配操作指引
    # 就红），外加「不得把开发者密码写进帮助语料」这条安全不变量。
    'tests/test_howto_coverage.py',
    # 问答检索质量：BM25 命中率下限 + 阈值校准余量 + 诚实拒答率。语料一改就
    # 能立刻知道有没有把原本能答的问题搞丢（补关键词有交叉挤占，实测过）。
    'tests/test_assistant_qa_eval.py',
    # 「没依据」哨兵：命中时用户必须一个字都看不到、answered 必须记真话、
    # 哨兵出现在正文中间不得误伤。含端到端真跑路由两例。
    'tests/test_assistant_no_basis.py'
)

$missing = @($gates | Where-Object { -not (Test-Path (Join-Path $engineRoot $_)) })
if ($missing.Count -gt 0) {
    Write-Output 'WARN: gate files missing (renamed? update scripts/gate_sweep.ps1):'
    $missing | ForEach-Object { Write-Output ("  - " + $_) }
    $gates = @($gates | Where-Object { Test-Path (Join-Path $engineRoot $_) })
}

Write-Output ("=== [1/2] gate sweep: {0} gate files ===" -f $gates.Count)
# Parallel sweep (2026-08-06): the serial run crossed 9 min (672 tests, growing
# as every line registers more gates) - a per-wrap-up cost paid by ALL lines.
# These same gate files already run under xdist (-n auto) in the daily full
# regression, so they are parallel-safe by construction. Production discipline
# mirrors regression.ps1: instance listening => this process drops to
# BelowNormal (pytest children inherit) + workers capped at 4 (8-core box must
# not starve live agents); otherwise -n auto. --timeout stops one hung worker
# from stalling the whole sweep (pytest-timeout is a repo dependency).
$prodUp = @(Get-NetTCPConnection -LocalPort 18799, 18899 -State Listen `
    -ErrorAction SilentlyContinue).Count -gt 0
if ($prodUp) {
    try { (Get-Process -Id $PID).PriorityClass = 'BelowNormal' } catch {}
    Write-Output '  [sweep] production instance online -> BelowNormal + workers capped at 4'
}
$nWorkers = if ($prodUp) { '4' } else { 'auto' }
# Tee stdout so the red-ledger below can parse FAILED lines; stderr stays
# untouched (PS5.1 wraps piped stderr into red ErrorRecords = console noise).
python -m pytest @gates -n $nWorkers -q --tb=line --timeout=90 --timeout-method=thread | Tee-Object -Variable sweepLines
$code = $LASTEXITCODE

# --- transient-red auto-requeue (2026-08-12 evening) -------------------------
# Sweeps on the shared tree scan files WHILE sibling lines are mid-save; twice
# today a half-written file turned a gate red for exactly one scan (ui-build
# freshness + fixture time-bomb at 21:08 - both green on manual re-run minutes
# later, but they still cost a triage round and polluted the red ledger).
# A red that heals on immediate re-run is TRANSIENT: re-run just the failed
# gate FILES once, serial, and let the SECOND pass be authoritative - a real
# red stays red on pass 2 (mixed files re-prove their real reds too), and the
# ledger below reads the authoritative pass. No FAILED lines parsed (collection
# error / hang) => no re-run, first verdict stands honestly.
if ($code -ne 0) {
    $failedFiles = @($sweepLines | ForEach-Object { "$_" } |
        Where-Object { $_ -match '^FAILED\s+\S' } |
        ForEach-Object { (($_ -replace '^FAILED\s+', '').Split(' ')[0].Split(':')[0]).Trim() } |
        Sort-Object -Unique |
        Where-Object { $_ -and (Test-Path (Join-Path $engineRoot $_)) })
    if ($failedFiles.Count -gt 0) {
        Write-Output ''
        Write-Output ("--- transient check: re-running {0} failed gate file(s) once (mid-save scans heal; real reds stay red) ---" -f $failedFiles.Count)
        python -m pytest @failedFiles -q --tb=line --timeout=90 --timeout-method=thread | Tee-Object -Variable rerunLines
        if ($LASTEXITCODE -eq 0) {
            Write-Output '  re-run GREEN: first-pass reds were transient (sibling mid-save scan); verdict flips to green.'
            $code = 0
            $script:transientHealed = $true
        } else {
            Write-Output '  re-run still RED: reds are real (not mid-save transients).'
        }
        $sweepLines = $rerunLines   # red ledger below reads the authoritative pass
    }
}

# --- shared red ledger (2026-08-12) -----------------------------------------
# Root cause of the "5 pre-existing reds" era (08-11..12): every line's wrap-up
# said "other owners, pre-existing" and nobody claimed a fix - reds had no AGE
# and no owner anywhere, so they just sat (one was a fixture time bomb that
# NOBODY broke). This ledger pins first-seen per failed gate test in the shared
# .ops state dir, prints age on every sweep, and names >48h reds as unclaimed
# debt. Scope = the [1/2] gate list only (same deterministic set for every
# line); -Full extras never enter the ledger. Entries auto-clear when their
# test goes green. Fail-open: a missing/corrupt ledger never breaks the sweep.
$ledgerPath = 'D:\chengjie-instances\.ops\gate_reds.json'
try {
    $curReds = @($sweepLines | ForEach-Object { "$_" } |
        Where-Object { $_ -match '^FAILED\s+\S' } |
        ForEach-Object { ($_ -replace '^FAILED\s+', '').Split(' ')[0].Trim() } |
        Sort-Object -Unique)
    $ledger = @{}
    if (Test-Path $ledgerPath) {
        $rawLedger = Get-Content $ledgerPath -Raw -Encoding UTF8 | ConvertFrom-Json
        foreach ($pp in $rawLedger.PSObject.Properties) { $ledger[$pp.Name] = [string]$pp.Value }
    }
    # fixed = test file ran in this sweep and its entry is no longer red
    foreach ($k in @($ledger.Keys)) {
        $kFile = $k.Split(':')[0]
        if (($gates -contains $kFile) -and (-not ($curReds -contains $k))) {
            $ledger.Remove($k) | Out-Null
        }
    }
    $nowIso = (Get-Date).ToString('s')
    foreach ($r in $curReds) {
        if (-not $ledger.ContainsKey($r)) { $ledger[$r] = $nowIso }
    }
    $opsDir = Split-Path -Parent $ledgerPath
    if (Test-Path $opsDir) {
        ($ledger | ConvertTo-Json) | Set-Content -Path $ledgerPath -Encoding UTF8
    }
    if ($curReds.Count -gt 0) {
        Write-Output ''
        Write-Output '--- red ledger: first-seen age per failed gate (shared .ops state) ---'
        foreach ($r in $curReds) {
            $ageTxt = '   new'
            $tag = ''
            if ($ledger.ContainsKey($r)) {
                $ageH = ((Get-Date) - [datetime]$ledger[$r]).TotalHours
                $ageTxt = ('{0,6:N1} h' -f [math]::Round($ageH, 1))
                if ($ageH -gt 48) { $tag = '   <<< UNCLAIMED >48h: fix it, or claim it on the intent board (agent_probe -Intent)' }
            }
            Write-Output ('  {0}  {1}{2}' -f $ageTxt, $r, $tag)
        }
    }
} catch {
    Write-Output ('  (red ledger unavailable, sweep unaffected: {0})' -f $_.Exception.Message)
}

if ($Full) {
    Write-Output ''
    Write-Output '=== -Full: whole-suite regression (scripts\regression.ps1) ==='
    powershell -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'regression.ps1')
    if ($LASTEXITCODE -ne 0) { $code = $LASTEXITCODE }

    # Template var() static-asset linkage audit (2026-08-02): gate tier-3 treats
    # static/shared token definitions as globally reachable; this read-only audit
    # closes the loop by checking the defining file is actually referenced by the
    # host chain (UNLINKED/PARTIAL => fail). Baseline: 6 refs, all OK-linked.
    Write-Output ''
    Write-Output '=== -Full: template var() static linkage audit (tools\audit_template_var_links.py --strict) ==='
    python (Join-Path $engineRoot 'tools\audit_template_var_links.py') --strict
    if ($LASTEXITCODE -ne 0) { $code = $LASTEXITCODE }

    # Local-first delivery profile (P2 2026-08-06): provision render -> real
    # config.example.yaml copy -> ConfigManager deep-merge -> golive/config_check
    # local_only verdicts. The pytest gates pin each layer with synthetic configs;
    # this tool is the only place the REAL example config flows through the whole
    # chain (catches example-config drift breaking the delivery profile). Offline
    # by default: ~2s, zero network, zero GPU, temp dir self-cleans. The --live
    # leg (real local LLM reply) is operator-run only, never wired here.
    Write-Output ''
    Write-Output '=== -Full: local-first delivery profile, offline (tools\verify_local_first_delivery.py) ==='
    python (Join-Path $engineRoot 'tools\verify_local_first_delivery.py')
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

    # Cockpit page (2026-08-14 P0/P1 revamp): KPI single-source (fresh/stale split,
    # takeover-overdue exempt from backlog fold), first-visit hint memory, category
    # filter chips with self-heal, caps feature-probe (old backend w/o resolve =>
    # zero "Done" buttons), empty-state ROI line, readable-name fallback (raw cid
    # never on screen). All data faces are route-mocked in the browser (resolve POST
    # intercepted = zero production writes; sendBeacon stubbed = zero telemetry
    # pollution). Missing playwright / unreachable instance => SKIP exit 0.
    Write-Output ''
    Write-Output '=== -Full: cockpit page, real browser (tools\verify_cockpit_ui.py) ==='
    python (Join-Path $engineRoot 'tools\verify_cockpit_ui.py')
    if ($LASTEXITCODE -ne 0) { $code = $LASTEXITCODE }

    # Account rail scoped view (2026-07-29 incident class): "click account X, see
    # account X" is a user-level invariant spanning drawer CTA / rail chips / overview
    # dropdown / scoped fetch. Static gates cannot prove it; hot-reloaded templates
    # ship straight to production. Read-only; missing playwright / instance => SKIP.
    Write-Output ''
    Write-Output '=== -Full: account rail scoped view, real browser (tools\verify_account_rail_ui.py) ==='
    python (Join-Path $engineRoot 'tools\verify_account_rail_ui.py')
    if ($LASTEXITCODE -ne 0) { $code = $LASTEXITCODE }

    # Connection-starvation backend fixes (2026-08-05): avatar origin breaker +
    # SSE silent-prime are backend .py, dormant until an instance restart loads
    # them (shared tree: whoever restarts next carries the batch). This probe
    # self-detects load state via the cooldown metrics key: not loaded => SKIP
    # exit 0; loaded => asserts no replay flood + no 20s avatar hangs. Wiring it
    # here closes the "restart happens but nobody re-verifies" gap.
    Write-Output ''
    Write-Output '=== -Full: connection-starvation fixes, live probe (tools\verify_conn_fixes.py) ==='
    python (Join-Path $engineRoot 'tools\verify_conn_fixes.py')
    if ($LASTEXITCODE -ne 0) { $code = $LASTEXITCODE }

    # Platform-switch thread sync (2026-08-05 incident class): clicking another
    # platform on the left rail must not leave the previous platform's conversation
    # sitting in the center pane (agent types into the wrong customer = missend
    # risk). Close-on-mismatch + desktop per-platform restore are PURE FRONTEND
    # (_syncThreadToScope); hot-reloaded templates ship straight to production.
    # Read-only (already-read rows only); missing playwright / instance => SKIP.
    Write-Output ''
    Write-Output '=== -Full: platform-switch thread sync, real browser (tools\verify_plat_thread_sync.py) ==='
    python (Join-Path $engineRoot 'tools\verify_plat_thread_sync.py')
    if ($LASTEXITCODE -ne 0) { $code = $LASTEXITCODE }

    # Channel-center four-page alignment (2026-08-02, P1-P3 vocabulary/skeleton
    # unification): five-segment tab order, shared KPI slots, pause tiers, the
    # relocated Messenger main JS actually executing (window.saveConfig), shared
    # funnel pane, rpa-card floor / mr-panel zero, mustache-leak + pageerror nets.
    # Static vocab gates prove the SOURCE; this proves the RENDERED page - and
    # hot-reloaded templates ship straight to production. Read-only; missing
    # playwright / unreachable instance => SKIP exit 0.
    Write-Output ''
    Write-Output '=== -Full: channel-center alignment, real browser (tools\verify_channel_alignment.py) ==='
    python (Join-Path $engineRoot 'tools\verify_channel_alignment.py')
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

    # White-label reskin stress (2026-08-23). Runtime-overrides the --bl-growth brand
    # family (equivalent to an OEM changing tokens.json) then computed-style-scans for
    # elements still rendering the OLD brand blue = hardcoded leaks that would ship as
    # "old blue patches inside the customer's brand". First run found 481 leaks from
    # just 4 css declarations (--ps alone fed 472 knowledge chips); all cleared same
    # night via color-mix(token) - this pin keeps the count at zero. Canvas charts
    # (analytics Chart.js) are a known blind spot, ledgered in the tool docstring.
    # Read-only; missing playwright / unreachable instance => SKIP exit 0.
    Write-Output ''
    Write-Output '=== -Full: white-label reskin stress, real browser (tools\verify_brand_reskin.py) ==='
    python (Join-Path $engineRoot 'tools\verify_brand_reskin.py')
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

    # Composer language-mismatch warning bar (2026-08-15 D batch). Foreign-language
    # conversation + agent types Chinese + outbound translation off => Chinese goes
    # out verbatim. #lang-warn-bar warns (never blocks send) with a one-click
    # "enable auto-translate" fix reusing the _onXlateChange single write path.
    # Truth table of _langWarnCheck is tested in a real browser; behavior section
    # route-mocks chats language=en (zero production writes). Missing playwright /
    # unreachable instance => static source wiring only, SKIP-equivalent exit 0.
    Write-Output ''
    Write-Output '=== -Full: composer language-mismatch warning, real browser (tools\verify_composer_langwarn.py) ==='
    python (Join-Path $engineRoot 'tools\verify_composer_langwarn.py')
    if ($LASTEXITCODE -ne 0) { $code = $LASTEXITCODE }

    # Sticker panel (2026-08-17 sticker-pack mainline). sticker-panel.js is an
    # EXTERNAL static component (wraps wsEmojiOpen, injects the Emoji|Stickers
    # tabs, all clicks via data-attr delegation) - template static gates cannot
    # see delegated handlers living in an external JS file, and static assets
    # hot-serve straight to production. Proves: assets loaded, emoji popover
    # still opens (no regression), flag ON => tabs injected + grid/CTA + manage
    # modal open/close, flag OFF => NO tabs injected (feature truly dark), zero
    # ReferenceError. Read-only: opens panels only, never sends a sticker.
    # Missing playwright / unreachable instance => SKIP exit 0.
    Write-Output ''
    Write-Output '=== -Full: sticker panel, real browser (tools\verify_sticker_ui.py) ==='
    python (Join-Path $engineRoot 'tools\verify_sticker_ui.py')
    if ($LASTEXITCODE -ne 0) { $code = $LASTEXITCODE }

    # Conversation-translation popover P0 (2026-08-16): quick toggle is a real
    # two-state switch (agent-language inbound + auto outbound, click again = off),
    # _agentLang locale resolution (vi-VN browser -> vi), same-language honesty
    # hint, and the defaults manager modal (inline editors + effective-chain pills,
    # no selected-conversation dependency). Behavior route-mocks chats language
    # (zero production writes). Missing playwright / unreachable => static wiring
    # only, SKIP-equivalent exit 0.
    Write-Output ''
    Write-Output '=== -Full: conversation translation popover P0, real browser (tools\verify_xlate_ui.py) ==='
    python (Join-Path $engineRoot 'tools\verify_xlate_ui.py')
    if ($LASTEXITCODE -ne 0) { $code = $LASTEXITCODE }

    # Voice "more" menu + one-click clone-bind modal (2026-08-03). Browser <audio>
    # native menu cannot be extended, so a self-built menu (clone / speed / download /
    # transcribe / quote) + a clone-bind modal drive /api/voice/enroll. Static gates
    # only prove the handlers are wired + i18n keys exist; they cannot prove the menu
    # pops, the modal prefills defaults, or the consent gate blocks a submit. This gate
    # locates a voice conversation via a read-only inbox.db query (mode=ro) + history
    # backfill, then verifies all of the above. Read-only: never checks the consent box,
    # never sends /api/voice/enroll. Missing playwright / unreachable instance / no
    # token / no voice conversation => SKIP exit 0.
    Write-Output ''
    Write-Output '=== -Full: voice clone menu+modal, real browser (tools\verify_voice_clone_ui.py) ==='
    python (Join-Path $engineRoot 'tools\verify_voice_clone_ui.py')
    if ($LASTEXITCODE -ne 0) { $code = $LASTEXITCODE }

    # Cases page (2026-08-03 case-center overhaul): a real JS app now - customer
    # text must render inert (the page once concatenated raw Telegram messages into
    # innerHTML = XSS into the ops console), the 30s poll must not eat an in-progress
    # note, claim buttons must adapt to old/new backend rows, closed cards collapse,
    # empty state must teach what opens a case. All case data is route-mocked
    # (zero production writes; close modal is open/cancel only). Static gates prove
    # keys/wiring, only a browser proves the rendered behavior; hot-reloaded
    # templates ship straight to production. Missing playwright / instance => SKIP.
    Write-Output ''
    Write-Output '=== -Full: cases page, real browser (tools\verify_cases_ui.py) ==='
    python (Join-Path $engineRoot 'tools\verify_cases_ui.py')
    if ($LASTEXITCODE -ne 0) { $code = $LASTEXITCODE }

    # Inbox translate speedup batch (2026-08-09): engine dropdown wired to window,
    # batch endpoint reachable through a browser session (cookie auth), skeleton
    # CSS shipped under the new cache stamp, zero ReferenceError on load. Static
    # gates prove exposure/keys; only a browser proves the rendered pipeline, and
    # hot-reloaded templates ship straight to production. Read-only apart from two
    # tiny cached test translations. Missing playwright / instance => SKIP exit 0.
    Write-Output ''
    Write-Output '=== -Full: inbox translate batch + engine dropdown, real browser (tools\verify_xlate_batch_ui.py) ==='
    python (Join-Path $engineRoot 'tools\verify_xlate_batch_ui.py')
    if ($LASTEXITCODE -ne 0) { $code = $LASTEXITCODE }

    # Message right-click translate group + multi-lang compare popup + lightbox
    # OCR panel (2026-08-18 P0/P1). Pure-frontend interactions on hot-reloaded
    # templates; menu typing per message kind / language-chip state machine /
    # panel<->bubble sync are only provable in a real browser. Fully hermetic:
    # chats/thread route-mocked to a synthetic conversation, compare & media
    # translate stub-fulfilled (zero engine cost, zero GPU, zero prod writes).
    # Missing playwright / unreachable instance => SKIP exit 0.
    Write-Output ''
    Write-Output '=== -Full: message ctx translate + lightbox panel, real browser (tools\verify_xlate_ctx_ui.py) ==='
    python (Join-Path $engineRoot 'tools\verify_xlate_ctx_ui.py')
    if ($LASTEXITCODE -ne 0) { $code = $LASTEXITCODE }

    # Inline-script syntax gate for hot-reloaded workspace templates (2026-08-18).
    # A single JS syntax error in unified_inbox.html bricks the whole inbox THE
    # MOMENT the file is saved (Jinja auto_reload); none of the text-level static
    # gates parse JS. node --check over all inline <script> blocks (Jinja masked).
    # Missing node => SKIP exit 0.
    Write-Output ''
    Write-Output '=== -Full: hot-template inline JS syntax (tools\verify_template_js_syntax.py) ==='
    python (Join-Path $engineRoot 'tools\verify_template_js_syntax.py')
    if ($LASTEXITCODE -ne 0) { $code = $LASTEXITCODE }

    # Theme contrast RENDERED values (2026-08-04 light-mode readability close-out).
    # The static gate pins palette VALUES; only a browser proves what agents SEE -
    # cascade overrides, component opacity, a page missing the token CSS, a more
    # specific rule hardcoding text back. Samples computed styles of the high-touch
    # text nodes on /knowledge in BOTH themes (effective bg composited up the
    # ancestor chain, element opacity folded in), pins the disabled-card badge and
    # no-raw-kb2_-keys. Read-only. Missing playwright / instance => SKIP exit 0.
    Write-Output ''
    Write-Output '=== -Full: theme contrast rendered values, real browser (tools\verify_theme_contrast_ui.py) ==='
    python (Join-Path $engineRoot 'tools\verify_theme_contrast_ui.py')
    if ($LASTEXITCODE -ne 0) { $code = $LASTEXITCODE }

    # Workspace chrome theme consistency (2026-08-17 "light mode still has dark
    # rail/toolbox" close-out). Three independently-regressable mechanisms only a
    # real browser proves: chrome token'd top bar / nav rail flipping per theme
    # (dark leg pinned to the historical literals = zero-regression promise), the
    # App(iframe) right panel following the host via the cp-cmd set-theme bridge,
    # and cpSetTheme being the working governed write path (bare cp_theme writes
    # get overruled by the appearance engine). Read-only; records the admin's
    # prior night.mode and restores it after (theme prefs roam server-side).
    # Missing playwright / unreachable instance / no token => SKIP exit 0.
    Write-Output ''
    Write-Output '=== -Full: workspace chrome theme consistency, real browser (tools\verify_ws_chrome_theme.py) ==='
    python (Join-Path $engineRoot 'tools\verify_ws_chrome_theme.py')
    if ($LASTEXITCODE -ne 0) { $code = $LASTEXITCODE }

    # Reply-settings page (2026-08-02 readability overhaul): P0 pinned the inline
    # mode-box regression / base input-width leak / t3-tiny-CJK contrast floor /
    # visited-purple links; P1 added preview playback, the advanced <details> group
    # and anchor nav. Templates hot-reload straight to production, so a real browser
    # must re-prove these. Read-only (client-side playback only, no config writes).
    # Missing playwright / unreachable instance => SKIP exit 0.
    Write-Output ''
    Write-Output '=== -Full: reply settings page, real browser (tools\verify_reply_settings_ui.py) ==='
    python (Join-Path $engineRoot 'tools\verify_reply_settings_ui.py')
    if ($LASTEXITCODE -ne 0) { $code = $LASTEXITCODE }

    # Contacts pane (2026-08-01 incident class): "entered the address book, could
    # not get back" - the old icon-toggle was the ONLY exit and vanished when the
    # list header collapsed. This pins the four exit paths (Esc / X / chat-side
    # action auto-exit / cmdk entry under collapsed header), the never-spoke filter
    # + icebreaker CTA loop, and the ops asset-card deep link. Read-only: never
    # presses sync (POST) nor icebreaker generate (LLM); clicking a contact only
    # synthesizes a client-side placeholder conversation. Waits use polling=100 -
    # default rAF polling gets throttled in aged headless pages (fake 18.8s reads).
    # Missing playwright / unreachable instance => SKIP exit 0.
    Write-Output ''
    Write-Output '=== -Full: inbox contacts pane, real browser (tools\verify_inbox_contacts.py) ==='
    python (Join-Path $engineRoot 'tools\verify_inbox_contacts.py')
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

    # Six admin boards: sidebar mount + light/dark readability (2026-08-18 unified
    # chrome batch). Static gates prove "{% set ws_sidebar = true %} exists" but not
    # that the sidebar actually renders/highlights (context starvation collapses it
    # silently via `or []` guards), nor that both themes stay readable (usage /
    # agent-perf were hardcoded-light x theme-flipping ink = dark mode unreadable
    # while every static gate stayed green). Also pins queue_monitor theme-follow
    # (boss decision 2026-08-18, replaced the always-dark big-screen) via
    # light-vs-dark wrap background diff, plus zero uncaught page JS errors.
    # Read-only GETs + window-scoped ?theme= pin (never touches agent prefs).
    # Missing playwright / unreachable instance / no token => SKIP exit 0.
    Write-Output ''
    Write-Output '=== -Full: admin boards sidebar + dual-theme readability, real browser (tools\verify_boards_ui.py) ==='
    python (Join-Path $engineRoot 'tools\verify_boards_ui.py')
    if ($LASTEXITCODE -ne 0) { $code = $LASTEXITCODE }

    # Ops hidden-capability panel (2026-08-18 P0-P4 series: footer text-wall ->
    # collapsed panel + reason groups + popover + toolbar entry). Pure frontend
    # state machine hot-reloaded straight to production; static gates pin ids/
    # functions/assert-strings but cannot prove expand/popover/outside-close, nor
    # that filtering a hidden card's title no longer lies "no match" (the fixed
    # bug). Read-only: expand/collapse/filter only, never clicks card actions.
    # Zero-hidden instances auto-skip scenarios 2-7 (panel may vanish entirely).
    # Missing playwright / unreachable instance / no token => SKIP exit 0.
    Write-Output ''
    Write-Output '=== -Full: ops hidden-capability panel, real browser (tools\verify_ops_hidden_ui.py) ==='
    python (Join-Path $engineRoot 'tools\verify_ops_hidden_ui.py')
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

    # Unified copilot App shell (2026-08-12 P0-P2 accounts/assembly/polish series).
    # Default surface of the desktop shell right panel + target of web app-mode
    # rollout; pure frontend hot-reloaded straight to production. Pins the three
    # bug classes only a real browser catches from this series: hero crushed to
    # 2px (flex min-height:auto=0 under overflow:hidden - DOM-presence gates are
    # blind to geometry), hidden attr overridden by display:inline-flex (accounts
    # icon "hidden" yet visible), applyI18n textContent-overwrite eating in-button
    # icons. Also: accounts overlay open/close, hostAccounts negotiation, tab
    # switching closes the overlay, no raw cp.* i18n keys. Read-only (fake cid
    # probe renders err/empty skeletons; never generates/sends/archives).
    # Missing playwright / unreachable instance / no token => SKIP exit 0.
    Write-Output ''
    Write-Output '=== -Full: unified copilot app shell, real browser (tools\verify_copilot_app_ui.py) ==='
    python (Join-Path $engineRoot 'tools\verify_copilot_app_ui.py')
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

    # Goal form draft survival + edit-hold (2026-08-04 P0). The modal used to lose
    # everything typed on a stray backdrop click, and ANY host context re-feed
    # (poll identity merge / peer resolve / tab refocus) rebuilt the shadow DOM
    # mid-edit. Fix = input snapshot (sessionStorage per conversation) + reapply
    # after rebuild + deferring same-cid refreshes while the form is open. All of
    # that is lifecycle-race behavior no static gate can see. Same fixture style
    # as verify_goal_card_ui.py (file:// page, fake fetch, zero telemetry).
    # Only missing playwright => SKIP exit 0.
    Write-Output ''
    Write-Output '=== -Full: goal form draft survival + edit-hold, fixture browser (tools\verify_goal_form_ui.py) ==='
    python (Join-Path $engineRoot 'tools\verify_goal_form_ui.py')
    if ($LASTEXITCODE -ne 0) { $code = $LASTEXITCODE }

    # /welcome first-run wizard (WP-2 2026-08-16): five-step state machine +
    # persona_create_wizard modal hosted on a non-personas page + resume/skip
    # persistence + send-to-self payload contract (chat_key='me'). All lifecycle
    # behavior static gates cannot see; same fixture style as verify_goal_form_ui
    # (file:// page, offline-rendered Jinja content, fake fetch, zero instance
    # dependency). Missing playwright => SKIP exit 0.
    Write-Output ''
    Write-Output '=== -Full: welcome onboarding wizard, fixture browser (tools\verify_onboarding_ui.py) ==='
    python (Join-Path $engineRoot 'tools\verify_onboarding_ui.py')
    if ($LASTEXITCODE -ne 0) { $code = $LASTEXITCODE }

    # Messenger 应用内登录（表单中继）渲染器 (2026-08-13): connect_relay.js 的 render 薄壳——
    # form 态同步骤不清屏（登录表单被 2.5s 轮询清空是致命 UX，cp-goal 草稿幸存事故同类）、
    # data-relay-field 收值、2FA/密码错/成功各态、driveTick 编排（注入 stub fetchJson）。
    # 同 verify_goal_form_ui.py 的夹具风格（file:// 内联组件、假 fetch、零实例依赖）。
    # 缺 playwright => SKIP exit 0。
    Write-Output ''
    Write-Output '=== -Full: messenger relay login renderer, fixture browser (tools\verify_relay_ui.py) ==='
    python (Join-Path $engineRoot 'tools\verify_relay_ui.py')
    if ($LASTEXITCODE -ne 0) { $code = $LASTEXITCODE }

    # 接入向导失败态自愈 (P0 2026-08-13): cred_invalid 失败卡——hosted 倒计时归零
    # **自动重试**（封顶 2 轮转 exhausted）、self 就地修正表单、诊断短码渲染、
    # phone_* 字段级错误、手机号链归零只解锁**绝不自动重发短信**、失败遮罩无转圈
    # （「转圈+报错并存」回归钉）。两个状态机的协同静态门禁证不了；telegram 的
    # modes/start/cancel/preflight/diag-upload 全 page.route 合成——零真实登录、
    # 零池容量消耗。实例不可达 => 静态接线仍守，浏览器段 SKIP exit 0。
    Write-Output ''
    Write-Output '=== -Full: connect wizard fail-state self-heal, real browser (tools\verify_connect_ui.py) ==='
    python (Join-Path $engineRoot 'tools\verify_connect_ui.py')
    if ($LASTEXITCODE -ne 0) { $code = $LASTEXITCODE }

    # Goal outcomes report page (P2 2026-08-09): KPI/matrix/trend/heat rendering,
    # per-row send modal, checkbox batch bar, outreach preview (stops before real
    # send), deep-link filter presets, 403 disabled guidance. Live instance +
    # page.route synthetic responses (zero production writes, zero data deps);
    # page route not yet loaded (pre-restart) => live section SKIP, static wiring
    # still gates. Missing playwright / instance down => SKIP exit 0.
    Write-Output ''
    Write-Output '=== -Full: goal outcomes report page, real browser (tools\verify_goal_report_ui.py) ==='
    python (Join-Path $engineRoot 'tools\verify_goal_report_ui.py')
    if ($LASTEXITCODE -ne 0) { $code = $LASTEXITCODE }

    # URL theme-pin window isolation (2026-08-15 incident). `?theme=dark` pins ONE
    # embedded webview; cp_theme is the profile-WIDE governance key. Leaking the pin
    # into cp_theme made the pinned window re-broadcast dark on every applyTheme and
    # revert the admin day/night switch within ~150ms in a sibling window of the same
    # partition -- reported as "the backend day/night toggle does nothing", while the
    # click handler fired, telemetry counted it, and every single-page gate stayed
    # green. The defect only exists with two windows sharing localStorage, and the
    # three readers (workspace_base head, unified_inbox head, appearance.js) all
    # hot-reload straight to production, so it needs a real two-tab browser gate.
    # Read-only: two logged-in tabs, one theme-toggle click, DOM/localStorage reads.
    # Missing playwright / unreachable instance / no token => SKIP exit 0.
    Write-Output ''
    Write-Output '=== -Full: URL theme-pin window isolation, real browser (tools\verify_theme_pin_isolation.py) ==='
    python (Join-Path $engineRoot 'tools\verify_theme_pin_isolation.py')
    if ($LASTEXITCODE -ne 0) { $code = $LASTEXITCODE }

    # cp-voice state machine (P0 2026-08-05): preview clear/regenerate/stale-guard,
    # in-flight mutex (double-click = double send to a customer), idempotency key,
    # post-send reset, epoch guard across conversation switches. All interaction
    # timing no static gate can see. Same fixture style (file:// + real component
    # + stub client object, zero instance deps, zero TTS burn). Missing playwright
    # => SKIP exit 0.
    Write-Output ''
    Write-Output '=== -Full: right-rail voice clone/send state machine, fixture browser (tools\verify_cp_voice_ui.py) ==='
    python (Join-Path $engineRoot 'tools\verify_cp_voice_ui.py')
    if ($LASTEXITCODE -ne 0) { $code = $LASTEXITCODE }

    # Messenger in-app login (form-relay) driveTick state machine (B64 二期):
    # submit acknowledgement / verifying escalation / password eye / Enter-submit /
    # checkpoint+locked screenshot views. Fixture browser (stub fetchJson, zero
    # sidecar/instance/network). Missing playwright => SKIP exit 0.
    Write-Output ''
    Write-Output '=== -Full: messenger in-app login relay state machine, fixture browser (tools\verify_connect_relay_ui.py) ==='
    python (Join-Path $engineRoot 'tools\verify_connect_relay_ui.py')
    if ($LASTEXITCODE -ne 0) { $code = $LASTEXITCODE }

    # Song Studio page (impl58 P3-2): request-desk card flow + humanized fail
    # reasons (no raw JSON) + voice-match dots + feat-probe button hiding on old
    # backends + mini-player. Read-only (never clicks approve/reject/toggles).
    # Missing playwright / unreachable instance / no token => SKIP exit 0.
    Write-Output ''
    Write-Output '=== -Full: song studio page, real browser (tools\verify_singing_ui.py) ==='
    python (Join-Path $engineRoot 'tools\verify_singing_ui.py')
    if ($LASTEXITCODE -ne 0) { $code = $LASTEXITCODE }

    # Toolbox smart-nurture card revamp (2026-08-22 cp.app.h_nurture raw-key incident
    # close-out): applyI18n missing-key keeps inline fallback (N0 pins the incident),
    # 3-step engine stepper transitions, go_live inline confirm, shadow plan list,
    # dirty-lit save, viewer read-only, probe two-click armed state. All interaction
    # timing no static gate can see. Same fixture style (file:// + real dict + real
    # component + stub client, zero instance deps, zero writes). Missing playwright
    # => SKIP exit 0.
    Write-Output ''
    Write-Output '=== -Full: toolbox smart-nurture card revamp, fixture browser (tools\verify_nurture_ui.py) ==='
    python (Join-Path $engineRoot 'tools\verify_nurture_ui.py')
    if ($LASTEXITCODE -ne 0) { $code = $LASTEXITCODE }

    # Quota wall v2 (2026-08-21): scheduler dedupe / action-trigger via machine
    # header events / mutex with the daily upsell modal / in-wall voucher redeem /
    # buy->credit-watch auto-recover / running-low bar with daily snooze. All
    # runtime scheduler behavior in workspace_base inline JS (hot-reloads straight
    # to production); the fixture extracts the REAL template segment at run time
    # (single source - the gate always tests current code, never a stale copy).
    # Missing playwright => SKIP exit 0.
    Write-Output ''
    Write-Output '=== -Full: quota wall v2 scheduler + recharge loop, fixture browser (tools\verify_quota_wall_ui.py) ==='
    python (Join-Path $engineRoot 'tools\verify_quota_wall_ui.py')
    if ($LASTEXITCODE -ne 0) { $code = $LASTEXITCODE }

    # AI assistant ball (P0/P1 2026-08-20): the whole ball/panel DOM is built at
    # runtime by shared/assistant/assistant-ball.js - template static gates are
    # blind to it, and the shared component hot-reloads straight to production.
    # 22 assertions: ball->panel, chip Q&A (JSON events render), feedback POST,
    # report-hint prefill, report submit + my-tickets, err/429/retry, Esc, mic
    # capability-hide, screenshot annotate layer, legacy help-ball absorption,
    # both shells. Same fixture style (file:// + real component + stub fetch,
    # zero instance deps, zero LLM burn). First run caught a dead chip button.
    # Missing playwright => SKIP exit 0.
    Write-Output ''
    Write-Output '=== -Full: AI assistant ball, fixture browser (tools\verify_assistant_ui.py) ==='
    python (Join-Path $engineRoot 'tools\verify_assistant_ui.py')
    if ($LASTEXITCODE -ne 0) { $code = $LASTEXITCODE }

    # Sidebar resize perf rebuild (P0 2026-08-17): pointer-capture drag that must
    # survive crossing an iframe/<webview> (the old stick-and-jump bug), full-window
    # shield mount/cleanup, rAF-coalesced width writes, min/max clamps, dblclick
    # reset via compat mouse chain, keyboard a11y resize, and the desktop CSS
    # transition-exemption + max-width alignment pins. Real mouse input via CDP --
    # capture semantics cannot be proven by static gates or synthetic dispatch.
    # Missing playwright => SKIP exit 0.
    Write-Output ''
    Write-Output '=== -Full: copilot sidebar resize drag, fixture browser (tools\verify_sidebar_resize.py) ==='
    python (Join-Path $engineRoot 'tools\verify_sidebar_resize.py')
    if ($LASTEXITCODE -ne 0) { $code = $LASTEXITCODE }

    # Guided onboarding tour engine (P1 2026-08-07): spotlight positioning,
    # auto-skip on missing targets (never trap the user), click-through overlay,
    # Esc/done exits, centered no-target step, EN zero-CJK on visible surface.
    # Pure front-end behavior no static gate can prove; same fixture style
    # (real Jinja render + real i18n, zero instance deps). Missing playwright
    # => SKIP exit 0.
    Write-Output ''
    Write-Output '=== -Full: guided onboarding tour engine, fixture browser (tools\verify_guided_tour_ui.py) ==='
    python (Join-Path $engineRoot 'tools\verify_guided_tour_ui.py')
    if ($LASTEXITCODE -ne 0) { $code = $LASTEXITCODE }

    # Workflows page UX contract (2026-08-09 batch): in-page dark variable group
    # (no more piebald dark mode), tab memory + #hash deep-link, disabled-chain
    # edit must NOT resurrect it (PUT carries enabled=0), running pill, header
    # value bar + funnel bars (P2). All hot-reloaded template JS/CSS that static
    # gates cannot prove; block-level real Jinja render + apiFetch mock fixture
    # (zero instance deps). First run already caught an invisible-bar bug
    # (span fill without display:block => width:0). Missing playwright => SKIP.
    Write-Output ''
    Write-Output '=== -Full: workflows page UX contract, fixture browser (tools\verify_workflows_ui.py) ==='
    python (Join-Path $engineRoot 'tools\verify_workflows_ui.py')
    if ($LASTEXITCODE -ne 0) { $code = $LASTEXITCODE }

    # Connect-modal official-channel guidance state (2026-08-10). IG/Zalo used to
    # render "needs your credentials" as grey "coming soon" (= feature does not
    # exist), and the explanation card fell back to Telegram-only instructions.
    # The fix (code registration + cfg badge + guidance card + auto-open +
    # ?channel= deeplink + recheck-no-rewizard) is all RENDERED behavior that
    # static gates cannot prove, and the modal lives in a hot-reloaded template.
    # Modes API is route-mocked (zero production writes; works whether or not IG
    # is actually configured). switch_off variant auto-SKIPs until the sibling
    # line's modal-side consumption lands. Missing playwright / instance => SKIP.
    Write-Output ''
    Write-Output '=== -Full: connect modal official-channel guidance, real browser (tools\verify_connect_modal_ui.py) ==='
    python (Join-Path $engineRoot 'tools\verify_connect_modal_ui.py')
    if ($LASTEXITCODE -ne 0) { $code = $LASTEXITCODE }

    # Setup wizard deeplink + webhook reach strip (2026-08-10, other half of the
    # official-channel onboarding loop): ?channel= arrival must auto-expand the
    # channel card + official-API fold + highlight/scroll (else the deeplink is
    # decorative), and the handshake strip must render verdicts and re-render on
    # poll. webhook-status endpoint is route-mocked (works before the backend
    # route's activating restart; arbitrary verdicts testable). NEVER clicks save
    # (that writes real config). Missing playwright / instance => SKIP exit 0.
    Write-Output ''
    Write-Output '=== -Full: setup wizard deeplink + reach strip, real browser (tools\verify_setup_wizard_ui.py) ==='
    python (Join-Path $engineRoot 'tools\verify_setup_wizard_ui.py')
    if ($LASTEXITCODE -ne 0) { $code = $LASTEXITCODE }

    # Shell-fusion page half (2026-08-11 rail fusion). The desktop rail became a
    # top tab strip; "open official web app" sank into the account drawer, unread
    # badge + tab/inject-health state flow through the inbox-preload bridge. The
    # page half (bridge-detect render / click drives bridge / state push rewrites
    # entry semantics / pure-browser zero-change) is hot-reloaded template JS no
    # static gate can prove; the shell half is pinned by desktop boot-invariants.
    # Bridge is STUBBED (works with no real shell present); read-only otherwise.
    # Missing playwright / unreachable instance => SKIP exit 0.
    Write-Output ''
    Write-Output '=== -Full: shell-fusion drawer entry + state push, real browser (tools\verify_shell_fusion_ui.py) ==='
    python (Join-Path $engineRoot 'tools\verify_shell_fusion_ui.py')
    if ($LASTEXITCODE -ne 0) { $code = $LASTEXITCODE }
}

# --- shared sweep record (2026-08-12 evening) --------------------------------
# preflight consumes this as ADVISORY context ("what did the gate face look
# like last time anyone swept?") before a tree-loading restart. Deliberately
# NOT a GO/NO-GO input: a red here may be another line's ledgered debt - the
# red ledger already tracks ownership/age; blocking B's restart on A's red
# would be overreach. Fail-open: a broken record never breaks the sweep.
try {
    $recPath = 'D:\chengjie-instances\.ops\last_sweep.json'
    if (Test-Path (Split-Path -Parent $recPath)) {
        $passedN = 0
        foreach ($l in $sweepLines) {
            if ("$l" -match '(\d+) passed') { $passedN = [int]$matches[1] }
        }
        $rec = [ordered]@{
            ts               = (Get-Date).ToString('s')
            verdict          = if ($code -eq 0) { 'green' } else { 'red' }
            passed           = $passedN
            failed           = @($curReds | Where-Object { $_ })
            transient_healed = [bool]$script:transientHealed
            full             = [bool]$Full
            duration_sec     = [int]((Get-Date) - $sweepStart).TotalSeconds
        }
        ($rec | ConvertTo-Json) | Set-Content -Path $recPath -Encoding UTF8
    }
} catch {
    Write-Output ('  (sweep record unavailable, sweep unaffected: {0})' -f $_.Exception.Message)
}

if ($code -eq 0) {
    Write-Output 'gate sweep: ALL GREEN'
} else {
    Write-Output ("gate sweep: FAILURES (exit {0}) - a red gate in a file you did not touch usually means ANOTHER line broke it; report, do not silently fix inside their active edit window." -f $code)
    # Attribution hints (2026-08-06): triaging 4 red gates by hand took ~15 min
    # (run each gate, read the assertion, cross-check who touched what). Print the
    # raw materials right here - recently modified dirty files + the declared-intent
    # board (agent_probe -Intent) - so "which line likely owns this red" is one glance.
    Write-Output ''
    Write-Output '--- attribution hints: dirty files modified in the last 60 min ---'
    $now2 = Get-Date
    $hintRows = @()
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
        $fage = ($now2 - (Get-Item -LiteralPath $p).LastWriteTime).TotalMinutes
        if ($fage -lt 0 -or $fage -gt 60) { continue }
        $hintRows += ('  {0,6:N1} min  {1}' -f [math]::Round($fage, 1), $rel)
    }
    if ($hintRows.Count -eq 0) {
        Write-Output '  (none - reds are likely older than 60 min; try agent_probe -WindowMinutes 240)'
    } else {
        $hintRows | Select-Object -First 25 | ForEach-Object { Write-Output $_ }
        if ($hintRows.Count -gt 25) { Write-Output ('  (+{0} more - bulk-touch batch?)' -f ($hintRows.Count - 25)) }
    }
    $intDir = 'D:\chengjie-instances\.ops\agent_intents'
    if (Test-Path $intDir) {
        Write-Output '--- attribution hints: declared intents (agent_probe -Intent) ---'
        foreach ($f in (Get-ChildItem $intDir -Filter *.txt | Sort-Object LastWriteTime -Descending)) {
            $ih = ($now2 - $f.LastWriteTime).TotalHours
            if ($ih -gt 24) { continue }
            $body = (Get-Content -LiteralPath $f.FullName -Encoding UTF8 | Select-Object -Skip 1) -join ' '
            Write-Output ('  {0,5:N1} h   {1}' -f [math]::Round($ih, 1), $body)
        }
    }
}
exit $code
