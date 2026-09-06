# -*- coding: utf-8 -*-
"""P17 坐席「工作目标」UI 改版门禁——守住可发现性/交互/视觉/埋点的源码不变量。

后端 agenda/undo/won_meta 已有专测；本文件守**坐席 UI 层**不回退：
- 组件不再 403=整卡消失（可配置灰字提示）
- 埋点走既有 ui-event 通道
- 宿主英雄行 / tab accent / agenda chips / 深链 / 驱动草稿接线存在
- 图标走 data-cp-ic=target（非 emoji）
"""

from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_GOAL_JS = _REPO / "shared" / "copilot" / "components" / "cp-goal.js"
_DRAFT_JS = _REPO / "shared" / "copilot" / "components" / "cp-draft.js"
_INBOX = _REPO / "src" / "web" / "templates" / "unified_inbox.html"
_CSS = _REPO / "src" / "web" / "static" / "workspace" / "unified-inbox.css"
_CHROME = _REPO / "shared" / "copilot" / "sidebar-chrome.js"


@pytest.fixture(scope="module")
def goal_js() -> str:
    return _GOAL_JS.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def draft_js() -> str:
    return _DRAFT_JS.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def inbox_html() -> str:
    return _INBOX.read_text(encoding="utf-8")


def test_goal_component_disabled_hint_not_blind_hide(goal_js: str):
    assert "_showDisabledHint" in goal_js
    assert "inbox.goal.disabled_hint" in goal_js
    assert "console.debug" in goal_js
    # 默认仍可隐藏，但开启 hint 时出灰字
    assert "__forbidden" in goal_js


def test_goal_component_ui_funnel_uses_existing_beacon(goal_js: str):
    assert "/api/telemetry/ui-event" in goal_js
    for action in ("goal_card_expose", "goal_set_click", "goal_feedback_adopt",
                   "goal_feedback_reject", "goal_drive_draft"):
        assert action in goal_js, f"missing funnel action {action}"


def test_goal_component_interaction_upgrades(goal_js: str):
    assert "cp_goal_form_prefs_v1" in goal_js          # C1 偏好记忆
    assert "beat_undo" in goal_js                      # C2 撤销
    assert "cp-goal-drive-draft" in goal_js            # C3 驱动草稿
    assert "won_meta" in goal_js or "won_product" in goal_js  # C4 归因
    assert "celebrate" in goal_js                      # C5 里程碑庆祝
    assert "keydown" in goal_js and "data-prof-key" in goal_js  # C6 回车提交


def test_goal_component_visual_upgrades(goal_js: str):
    assert "gl-ms-cur" in goal_js                      # D2 当前里程碑常显
    assert "gl-sec" in goal_js and "direct" in goal_js  # D3/D5 分区+直说色
    assert "more_toggle" in goal_js                    # D4 放弃进溢出菜单
    assert "_emptyIllust" in goal_js                   # D6 空态插画
    assert "gl-badge.muted" in goal_js or "muted" in goal_js  # D7 终态降压


def test_inbox_host_discoverability(inbox_html: str):
    assert 'data-cp-ic="target"' in inbox_html
    assert 'id="cp-hero-goal"' in inbox_html
    assert "goal-agenda-filters" in inbox_html
    assert "_renderHeroGoal" in inbox_html
    assert "_updateCustomerTabBadge" in inbox_html
    assert "accent" in inbox_html
    assert "card" in inbox_html and "_handleGoalDeepLink" in inbox_html
    assert "cp-goal-drive-draft" in inbox_html
    assert "cp-goal.js?v=20260907b" in inbox_html  # 改 cp-goal.js 必须 bump 缓存戳（本断言随批次前移；M-7 A）


def test_goal_sprint_pace_ui(goal_js: str):
    """P0 2026-08-29 限时节奏：分段控件 / 倒计时顺延 / hold / 草稿 pace。"""
    assert "pace_cap" in goal_js
    assert 'data-act="pick_pace"' in goal_js
    assert "gl-pace" in goal_js
    assert 'data-act="extend_30m"' in goal_js
    assert 'data-ref="pace_line"' in goal_js
    assert "_sprintOk" in goal_js
    assert "goal_pick_pace" in goal_js
    assert "inbox.goal.form.arc_hint_sprint" in goal_js
    # P1：today 三拍短弧 + 达成信号提示行（只提示不自动结算）
    assert "arc_today_" in goal_js
    assert "gl-outcome" in goal_js
    assert "outcome_signal" in goal_js
    assert "inbox.goal.outcome.contact" in goal_js
    # P2：限时终局复盘（分钟级用时 + 拍数 chip，tooltip=最后一拍意图）
    assert "inbox.goal.done.dur" in goal_js
    assert "inbox.goal.term.beats" in goal_js


def test_goal_sprint_p3_ui(goal_js: str):
    """P3 2026-08-30 冲刺前端：倒计时活字 / 调度状态行+立即推进 / close 档 /
    力度选择（全力）/ 补录成交 / 插队回程票。改 cp-goal.js 冲刺区前先读
    CLAUDE 的冲刺主线段。"""
    # 倒计时活字（30s tick 原位刷新，不整卡重渲染）
    assert 'data-ref="sp_remain"' in goal_js
    assert "_armSprintTick" in goal_js
    # 调度状态行 + 手动加速（安全闸在路由，按钮只是入口）
    assert "gl-sprint-line" in goal_js
    assert 'data-act="sprint_nudge"' in goal_js
    assert "sprint_live" in goal_js
    assert "inbox.goal.sprint.nudge_btn" in goal_js
    assert "/sprint/nudge" in goal_js
    # close 收口档进力度全集 + 配色
    assert '"close"' in goal_js
    assert ".gl-pill.close" in goal_js
    # 力度选择（稳妥/全力 → params.sprint_mode）
    assert 'data-act="pick_force"' in goal_js
    assert "sprint_mode" in goal_js
    assert "inbox.goal.form.force" in goal_js
    # 疑似达成补录（expired→done 迁移的前端入口；空态也可达）
    assert 'data-act="revive_won"' in goal_js
    assert "inbox.goal.outcome.revive" in goal_js
    # 插队回程票（暂停长线→冲刺带 resume_goal_id）
    assert 'data-act="jump_sprint"' in goal_js
    assert "resume_goal_id" in goal_js
    # 文案与行为一致：推进器开关驱动限时档提示（caps.sprint_enabled）
    assert "sprint_enabled" in goal_js
    assert "_hint_engine" in goal_js
    # 3h chip（老板口径的经典冲刺窗）
    assert "[3, 4, 8]" in goal_js
    assert "beats_used" in goal_js and "last_beat_intent" in goal_js
    # P1（信号驱动）：买家信号条 + 直达限时表单 + 可关闭；画像陈旧 ⏳
    assert "gl-buysig" in goal_js
    assert 'data-act="open_form_sprint"' in goal_js
    assert 'data-act="signal_dismiss"' in goal_js
    assert "signal_hint" in goal_js
    assert "inbox.goal.slots.stale_t" in goal_js


def test_goal_form_draft_survival_layer(goal_js: str, inbox_html: str):
    """P0 2026-08-04 草稿幸存层 + 编辑防打断：误触背板/宿主重喂 context/取数失败
    都不得丢已填内容。行为不变量由 fixture 门禁 tools/verify_goal_form_ui.py 在真
    浏览器里钉；本测只钉源码存在性，防「顺手清理」裸退。"""
    assert "cp_goal_form_draft_v1:" in goal_js       # sessionStorage 草稿键（按会话 id）
    assert 'data-act="ov_dismiss"' in goal_js        # 背板＝判脏动作，不再直连 form_back
    assert "_applyFormDraft" in goal_js              # 整块重渲染后的草稿回填
    assert "_formBaseline" in goal_js                # 判脏基准＝出厂快照
    assert "goal_ctx_deferred" in goal_js            # 编辑期挂起外部刷新（每编辑会话记一次）
    assert '"draft_clear"' in goal_js or "draft_clear" in goal_js  # 显式弃稿口
    assert "inbox.goal.form.draft_restored" in goal_js
    assert "inbox.goal.form.draft_saved_hint" in goal_js
    # 宿主侧：同会话重喂去抖（身份合并/peer 解析不再核爆右栏 8 组件）
    assert "_wsCpIdentFp" in inbox_html
    assert "cid===_wsCpCid" in inbox_html
    # P1（同日）：键盘/焦点体验 + ×语义分离 + 自动暂存微反馈 + 移动端底部抽屉
    assert "inbox.goal.form.close" in goal_js          # ×＝关闭整表单（与「返回」分离）
    assert "inbox.goal.form.autosave_note" in goal_js  # 常驻「自动暂存中」微反馈
    assert "_trapModalTab" in goal_js                  # Tab 焦点陷阱
    assert "e.ctrlKey || e.metaKey" in goal_js         # Ctrl/Cmd+Enter 创建
    assert "_focusModal" in goal_js                    # 初始聚焦/重建后焦点回位
    assert "max-width: 520px" in goal_js               # 窄屏底部抽屉


def test_panel_base_same_cid_refresh_no_flash():
    """P3 2026-08-05：基类 refresh 同会话不清屏（换会话仍清屏防串数据；无旧数据时
    保留 loading 反馈）。行为断言在 verify_goal_form_ui.py S20；这里钉源码防裸退。"""
    base = (_REPO / "shared" / "copilot" / "components" / "cp-panel-base.js")\
        .read_text(encoding="utf-8")
    assert "_renderedCid" in base
    assert "switching || !this._d" in base


def test_target_icon_registered():
    """goal 卡头 target 图标须在两端可解析。

    2026-08-08 SSOT 收敛后收件箱页不再内联图标表（uiIcon 走 /static/ui_icons.js
    全站注册表），断言随架构更新：页内保留 data-cp-ic="target" 用点 + 注册表有
    target 条目；独立壳（app.html/桌面）仍由 sidebar-chrome 子集表兜底。"""
    chrome = _CHROME.read_text(encoding="utf-8")
    assert "target:" in chrome
    inbox = _INBOX.read_text(encoding="utf-8")
    assert 'data-cp-ic="target"' in inbox
    lib = (_REPO / "src" / "web" / "static" / "ui_icons.js").read_text(encoding="utf-8")
    assert "target:" in lib


def test_goal_css_tokens_present():
    css = _CSS.read_text(encoding="utf-8")
    for sel in (".cp-hero-goal", ".goal-agenda-filters", ".goal-af-chip",
                ".conv-goal-mark", ".ws-cp-tab-badge.accent"):
        assert sel in css, f"missing CSS {sel}"


# ── Phase 2（同日晚）：窄屏入口 / 自动展开 / 驱动草稿防覆盖 / 深链时序 ──


def test_goal_quick_entry_narrow_screen(inbox_html: str):
    """U6：右栏收起时顶栏「工作目标」快捷芯片——按 featureOn+会话选中 控显，走内联 onclick 须挂 window。"""
    assert 'id="goal-quick-btn"' in inbox_html
    assert "_openGoalQuick" in inbox_html
    assert "_syncGoalQuickBtn" in inbox_html
    # 内联 onclick → 必须在全局暴露块登记（完整可达性由哑按钮门禁复核，这里钉暴露行存在）
    assert "\n  _openGoalQuick," in inbox_html


def test_goal_auto_expose_on_customer_tab(inbox_html: str):
    """待反馈时切「客户&关系」tab 自动展开目标卡——每会话仅一次（防打扰）。"""
    assert "_maybeAutoExposeGoal" in inbox_html
    assert "_goalAutoExposed" in inbox_html
    assert "goal_auto_expand" in inbox_html


def test_goal_drive_draft_uses_set_directive(inbox_html: str, goal_js: str, draft_js: str):
    """P22：驱动草稿走 setDirective，绝不写 composer（旧版空输入框预填意图原文
    → 误发风险 + 生成引擎根本收不到指令）。
    P28：opener 已吃 instruction+目标注入 → setDirective 不得再强制切回 reply。
    2026-08-13 钉子收窄：`!ta.value.trim()` 只在 **drive-draft 处理器**内禁用——
    send-gate 线的「发送被拦后恢复输入框」在别的 handler 里合法使用同一模式，
    全文级负向钉会把无关线的正当代码误伤成红。"""
    assert "setDirective" in inbox_html
    assert "ta.value=String(intent)" not in inbox_html
    _at = inbox_html.find("cp-goal-drive-draft', function")
    assert _at >= 0, "drive-draft 宿主监听器不见了"
    _handler = inbox_html[_at:_at + 2400]
    assert "!ta.value.trim()" not in _handler   # 处理器内绝不预填 composer
    assert 'this._mode === "opener") this._setMode("reply")' not in draft_js
    assert "_goalBadgeHtml" in draft_js
    assert "goal_applied" in draft_js
    assert "slots_progress" in goal_js or "_renderSlotsProgress" in goal_js
    assert "slot_toggle" in goal_js and "switch_discovery" in goal_js
    assert "discovery" in goal_js  # KIND_ORDER / gk-discovery


def test_goal_deep_link_defers_when_conv_param(inbox_html: str):
    """?conv=&card=goal 同行时不许先弹「请先选择会话」——消费延后到会话打开。"""
    assert "hasConv" in inbox_html
    assert "if(!hasConv) _tryConsumeGoalDeepLink()" in inbox_html


# ── P18（2026-07-31）：「会用」层——场景化表单 / 三步引导 / 反馈闭环 ──────────


def test_goal_form_scenario_cards(goal_js: str):
    """表单从「模板下拉」升级为场景卡片：每模板一句人话说明；custom 收进
    「进阶」入口且不参与默认预选（实锤：新手停在自定义模板写不出推进方向）。"""
    assert "pick_tmpl" in goal_js and "pick_custom" in goal_js
    assert "inbox.goal.tmpl_desc." in goal_js
    assert 'prefs.template !== "custom"' in goal_js
    assert "goal_form_pick_scenario" in goal_js and "goal_form_pick_custom" in goal_js


def test_goal_form_situational_recommend(goal_js: str):
    """沉默 ≥72h 推荐「沉默唤回」——宿主经 ctx.goalHint.silentHours 喂数，
    组件只在高置信时贴推荐标（拿不准不猜）。"""
    assert "goalHint" in goal_js
    assert "engagement_reactivate" in goal_js
    assert "inbox.goal.form.rec_silent" in goal_js


def test_goal_autonomy_copy_honest(goal_js: str):
    """auto 档诚实注解：caps.bridge_enabled=false 时如实注明「不会自己主动发
    消息」；caps 缺失（旧后端未重启）不注解不猜——文案与行为一致是硬原则。"""
    assert "_autonomyNote" in goal_js
    assert "bridge_enabled" in goal_js
    assert "inbox.goal.autonomy.auto_note_off" in goal_js


def test_goal_autonomy_auto_primary(goal_js: str):
    """2026-08-12 运营方针「建目标以全自动为主，只观察不作主推」三不变量：
    ① 表单缺省 auto 且偏好记忆不回放 observe（实录：一次选了只观察被 prefs
    粘住，之后三个目标全默认 observe → 坐席以为目标功能整体失效）；
    ② 推荐徽标挂 auto 卡（防「顺手」改回 suggest）；
    ③ 参与度卡展示序 auto 优先、observe 垫底（后端 autonomy_levels 仅成员集）。"""
    assert 'prefs.autonomy !== "observe"' in goal_js      # observe 不粘偏好
    assert '? prefs.autonomy : "auto"' in goal_js          # 缺省回落 auto
    # #166（2026-09-05）唯一例外：引擎在当前节奏下**不能真出手**（sprint 关 / care
    # dry_run / 平台白名单 / bridge 关…）时推荐徽标让给 suggest、缺省不落灰掉的
    # auto——「全自动为主」的前提是全自动真的会动；引擎能出手时口径不变
    assert 'const recOn = autoOff ? lvl === "suggest" : lvl === "auto"' in goal_js
    assert 'if (engDef && !engDef.on && prefAuto === "auto") prefAuto = "suggest"' in goal_js
    assert "{ auto: 0, suggest: 1, observe: 2 }" in goal_js  # 展示序前端定


def test_goal_created_next_hint(goal_js: str):
    """建目标后一次性「接下来会发生什么」提示（按自治档取文案，可手动关掉）。"""
    assert "_createdHintAutonomy" in goal_js
    assert "inbox.goal.created_next." in goal_js
    assert "hint_dismiss" in goal_js


def test_goal_progress_timeline(goal_js: str):
    """「AI 做了什么」进展时间线：M-7 A（#236）起懒取 /api/goals/{id}/beats 每一拍清单
    （主动发出 / 回复带方向 / 被拦下 + 原因 / 投递真相 / 跳到消息），不再读 goal_actions
    拍史——修「卡片写已推进 2 拍，会话里一条目标消息都看不见」。"""
    assert "prog_toggle" in goal_js
    assert "goal_progress_open" in goal_js
    assert "_loadProgress" in goal_js
    assert '"/beats"' in goal_js
    assert "inbox.goal.beatk." in goal_js
    assert "inbox.goal.beatst." in goal_js
    assert "inbox.goal.blocked." in goal_js
    assert 'data-act="beat_jump"' in goal_js
    assert "cp-goal-jump-message" in goal_js
    # 状态行「已推进 N 拍」可点开清单；「今天被拦 N 次」并排
    assert 'class="gl-beats-btn" data-act="prog_toggle"' in goal_js
    assert "inbox.goal.sprint.blocked_today" in goal_js


def test_goal_templates_endpoint_exposes_caps():
    routes = (_REPO / "src" / "web" / "routes" / "goal_routes.py").read_text(encoding="utf-8")
    assert "bridge_enabled" in routes
    assert "proactive_enabled" in routes


def test_goal_i18n_dynamic_keys_bilingual():
    """动态拼键（tmpl_desc.<id> / created_next.<lvl> / beat.<status>）静态键门禁
    扫不到，这里显式钉双语齐备——模板注册表加新模板时此测试会点名补文案。"""
    import importlib

    goals_pack = importlib.import_module("src.web.i18n_packs.goals")
    from src.companion.goals.templates import AUTONOMY_LEVELS, TEMPLATES

    for tid in TEMPLATES:
        for lang in (goals_pack.ZH, goals_pack.EN):
            assert f"inbox.goal.tmpl_desc.{tid}" in lang, f"missing tmpl_desc {tid}"
    for lvl in AUTONOMY_LEVELS:
        for lang in (goals_pack.ZH, goals_pack.EN):
            assert f"inbox.goal.created_next.{lvl}" in lang, f"missing created_next {lvl}"
    for st in ("planned", "consumed", "sent", "skipped", "blocked"):
        for lang in (goals_pack.ZH, goals_pack.EN):
            assert f"inbox.goal.beat.{st}" in lang, f"missing beat {st}"


def test_inbox_host_tour_and_feedback(inbox_html: str):
    """宿主：cp-fill/cp-action-done 出用户可见反馈（修「点了没反馈」）+
    goalHint 喂沉默时长。三步引导（cp-tour）已于 2026-08-31 随新手引导整体
    退役——此处反向钉住不复活（防复活总门禁见 test_onboarding_retired.py）。"""
    assert "_cpTourStart" not in inbox_html
    assert "_cpTourMaybeAuto" not in inbox_html
    assert "inbox.cp.fill_toast" in inbox_html
    assert "inbox.cp.exec_ok" in inbox_html
    assert "goalHint" in inbox_html


def test_tour_css_tokens_absent():
    """cp-tour 样式随功能退役清除（残留=死 CSS，且暗示有人把引导加回来了）。"""
    css = _CSS.read_text(encoding="utf-8")
    for sel in (".cp-tour-veil{", ".cp-tour-hl{", ".cp-tour-pop{"):
        assert sel not in css, f"retired CSS resurfaced: {sel}"


# ── P19（2026-07-31 深夜）：反馈率 0% 实锤后的复合按钮 + 本周成果面 ──────────


def test_goal_adopt_and_draft_composite(goal_js: str):
    """「采纳并拟稿」合并坐席最常见两连击（上线基线实锤：30 拍 0 反馈）；
    采纳成功（服务端权威落账）才驱动草稿；已采纳态保留拟稿入口不断路。"""
    assert "beat_adopt_draft" in goal_js
    assert "goal_feedback_adopt_draft" in goal_js
    assert "inbox.goal.act.adopt_only" in goal_js
    assert "return true" in goal_js and "return false" in goal_js


def test_inbox_host_weekly_wins_chip(inbox_html: str):
    """本周成果 chip：正向数据才显示（0 不示众——空数字是反激励），
    10min 轮询；403/异常一律静默隐藏。"""
    assert 'id="goal-af-wins"' in inbox_html
    assert "_refreshGoalWins" in inbox_html
    assert "/api/goals/report?days=7" in inbox_html
    assert "inbox.goal.wins.week" in inbox_html


def test_weekly_wins_css_present():
    css = _CSS.read_text(encoding="utf-8")
    assert ".goal-af-wins" in css


def test_goal_wins_i18n_bilingual():
    import importlib

    goals_pack = importlib.import_module("src.web.i18n_packs.goals")
    for key in ("inbox.goal.wins.week", "inbox.goal.wins.active_only",
                "inbox.goal.wins.t", "inbox.goal.act.adopt_only",
                "inbox.goal.act.adopt_draft", "inbox.goal.act.adopt_draft_t"):
        assert key in goals_pack.ZH and key in goals_pack.EN, key


# ── P20（2026-08-01）：画像区排版收口 + 缺口 chips 动作化 + 产品行升级 ────────


def test_profile_header_layout_no_squeeze(goal_js: str):
    """P0-1 竖排根因回归钉：旧版画像头部单行 flex 硬塞「标题+双固定宽 bar+按钮」
    ≈304px，默认 300px 侧栏内容区只有 ~236px → 溢出后 CJK 逐字换行成竖排。
    收口不变量：bar 不许回到固定宽、头部容器必须可换行、完成度独立成行。"""
    assert "width:52px" not in goal_js          # bar 固定宽是挤压根因
    assert ".gl-fillrow" in goal_js             # 完成度独立行
    hd = goal_js.split(".gl-prof-hd {", 1)[1].split("}", 1)[0]
    assert "flex-wrap:wrap" in hd
    bar = goal_js.split(".gl-fillbar .bar {", 1)[1].split("}", 1)[0]
    assert "flex:1 1" in bar                    # bar 弹性伸缩
    btn = goal_js.split(".gl-prof-hd button {", 1)[1].split("}", 1)[0]
    assert "white-space:nowrap" in btn          # 「补录」按钮不许竖排


def test_profile_zero_state_hides_bars(goal_js: str):
    """P0-3：关系/商机双零时不渲染 0% 完成度条（空数字是反激励——与 P19
    本周成果 chip「双零整条隐藏」同哲学）；冷启动由空态文案+缺口 chips 承担。"""
    assert "hasFill" in goal_js


def test_profile_gap_chips_actionable(goal_js: str):
    """P0-2：缺口 chip 从死 span 变 button（旧版无 data-act 点了没反应，可供性
    错位）——点击展开「拟稿去问/我来补录」；拟稿经 cp-goal-drive-draft →
    setDirective → smart-reply.instruction。"""
    assert '<span class="gl-chip miss">' not in goal_js
    assert 'data-act="slot_menu"' in goal_js
    assert 'data-act="slot_ask"' in goal_js
    assert 'data-act="slot_fill"' in goal_js
    for beacon in ("goal_slot_menu", "goal_slot_ask",
                   "goal_slot_fill", "goal_slot_edit"):
        assert beacon in goal_js, f"missing funnel action {beacon}"
    assert "inbox.goal.profile.ask_intent" in goal_js


# ── P22（2026-08-01）：采纳并拟稿语义接通 + 目标管理入口 ──────────────────


def test_p22_drive_emits_instruction_with_push(goal_js: str):
    """采纳并拟稿必须带 instruction + pushLevel（力度规则进坐席指令，防陪伴日硬推销）。"""
    assert "_emitDriveDraft" in goal_js
    assert "inbox.goal.drive_instruction" in goal_js
    assert "inbox.goal.drive_rule." in goal_js   # 动态拼键 drive_rule.{none|soft|direct}
    assert "instruction:" in goal_js
    assert "pushLevel:" in goal_js


def test_p22_goal_management_surface(goal_js: str):
    """有目标时「设定目标」不再失踪：⋯ 菜单有换方向；AI 自建徽标；自治档可点切。"""
    assert 'data-act="redirect"' in goal_js
    assert "inbox.goal.act.redirect" in goal_js
    assert "AUTO_ORIGIN" in goal_js
    assert "inbox.goal.origin.auto" in goal_js
    assert 'data-act="autonomy_cycle"' in goal_js
    assert "inbox.goal.push.none_tip" in goal_js


def test_p22_draft_directive_api(draft_js: str):
    draft = draft_js
    assert "setDirective" in draft
    assert "clearDirective" in draft
    assert "payload.instruction" in draft
    assert "dirchip" in draft
    assert "cp.draft.directive_label" in draft
    # P28：opener 已吃 instruction+目标注入 → 禁止再强制切回 reply（旧 P22.1 翻车点）
    assert 'this._mode === "opener") this._setMode("reply")' not in draft
    assert "P28" in draft  # 注释钉住设计决策
    assert "summary" in draft
    # P23：目标/来源随 payload 走（服务端落 drive_draft 耐久事件 + 样本归属）
    assert "payload.goal_id" in draft
    assert "payload.instruction_source" in draft
    # P28：goal_applied 坐席可见
    assert "_goalBadgeHtml" in draft
    assert "cp.draft.goal_on" in draft or "goal_on" in (
        _REPO / "shared" / "copilot" / "i18n" / "cp-i18n.js"
    ).read_text(encoding="utf-8")


def test_p23_host_forwards_source(inbox_html: str):
    """宿主把 drive-draft 的 source（beat/hero/slot）透传给 setDirective——
    断了它 drive_draft 事件全归 '-'，周审看不出哪个入口在被用。"""
    assert "source:String(det.source||'')" in inbox_html


def test_p22_1_hero_one_click_draft(goal_js: str, inbox_html: str):
    """英雄卡一键拟稿：降发现成本；与卡内「采纳并拟稿」同指令拼装。"""
    assert "driveDraftFromToday" in goal_js
    assert "goal_hero_draft" in goal_js
    assert "hg-draft" in inbox_html
    assert "data-hg-draft" in inbox_html
    assert "driveDraftFromToday" in inbox_html
    # 宿主缺 instruction 时用 intent+push 回拼（旧事件/英雄卡容错）
    assert "drive_instruction" in inbox_html
    # i18n P0（2026-08-19）：summary 展示态优先英文变体（intentDisplay），
    # 指令 text 保持中文权威口径——断言随行为前移
    assert "summary:(String(det.intentDisplay||'').trim()||intent)" in inbox_html


def test_p22_drive_i18n_bilingual():
    import importlib
    goals_pack = importlib.import_module("src.web.i18n_packs.goals")
    for key in (
        "inbox.goal.drive_instruction",
        "inbox.goal.drive_rule.none",
        "inbox.goal.drive_rule.soft",
        "inbox.goal.drive_rule.direct",
        "inbox.goal.push.none_tip",
        "inbox.goal.origin.auto",
        "inbox.goal.act.redirect",
        "inbox.goal.redirect_confirm",
        "inbox.goal.autonomy_cycle_t",
        "inbox.goal.autonomy_saved",
        "inbox.goal.drive_switched",
        "inbox.goal.hero.draft",
        "inbox.goal.hero.draft_t",
        "inbox.goal.profile.ask_draft_label",
    ):
        assert key in goals_pack.ZH and key in goals_pack.EN, key


def test_profile_filled_chips_editable_and_form_grouped(goal_js: str):
    """P1：已填 chip 点击进补录并聚焦对应字段（来源标注收进 tooltip 省宽度）；
    补录表单按轨分组 + 建议问法当 placeholder（空输入框不再让人猜该填什么）。"""
    assert 'data-act="slot_edit"' in goal_js
    assert "gl-pf-group" in goal_js
    assert "placeholder=" in goal_js
    assert "inbox.goal.profile.src." in goal_js   # 来源仍可见（tooltip）
    assert "focus()" in goal_js


def test_products_pitch_visible_price_aligned(goal_js: str):
    """P1：产品 pitch 从 hover title 提为常显副行（触屏/桌面壳无 hover 也可达
    ——那是坐席的现成话术）；价格右对齐独立列；外链带 ↗ 线稿 + noopener。"""
    assert "gl-prod-pitch" in goal_js
    assert "gl-prod-price" in goal_js
    assert 'rel="noopener"' in goal_js


def test_profile_ask_i18n_bilingual():
    """ask.<slot key> 是动态拼键（静态键门禁扫不到）——显式钉：profile_slots
    注册表每个槽位的建议问法 zh+en 齐备；registry 增槽时此测试点名补文案。"""
    import importlib

    goals_pack = importlib.import_module("src.web.i18n_packs.goals")
    from src.companion.goals.profile_slots import SLOTS

    for s in SLOTS:
        for lang in (goals_pack.ZH, goals_pack.EN):
            assert f"inbox.goal.profile.ask.{s['key']}" in lang, \
                f"missing ask.{s['key']}"
    for key in ("inbox.goal.profile.ask_lead", "inbox.goal.profile.ask_intent",
                "inbox.goal.profile.ask_btn", "inbox.goal.profile.fill_btn",
                "inbox.goal.profile.miss_t", "inbox.goal.profile.edit_t"):
        for lang in (goals_pack.ZH, goals_pack.EN):
            assert key in lang, key


# ── P24（2026-08-03）：建目标两步向导 + 场景设置弹层 + 参数人话控件 ──────────


def test_p24_wizard_two_steps(goal_js: str):
    """一页堆全 → 两步：第一步分组场景卡，第二步场景专属设置弹层（fixed 居中，
    逃离 236px 窄栏）；背板点击/Esc/×/「返回」四路同回第一步；点击面板空白
    不许误关（modal_noop 挡事件委托冒泡到背板）。"""
    assert "_renderFormStep1" in goal_js
    assert "_renderFormModal" in goal_js
    assert "_formStep" in goal_js
    assert 'data-act="form_back"' in goal_js
    assert "goal_form_back" in goal_js
    assert '"Escape"' in goal_js
    assert "gl-ov" in goal_js and "gl-modal" in goal_js
    assert 'role="dialog"' in goal_js and 'aria-modal="true"' in goal_js
    assert 'data-act="modal_noop"' in goal_js


def test_p24_step1_grouped_cards_custom_highlight(goal_js: str):
    """场景卡按 kind 分组（转化/关系/唤回，类别色条+线稿图标）；「自定义目标」
    从 11px 灰色下划线文字链升级为独立高亮卡（violet 描边 + 进阶 pill）。"""
    assert "KIND_ORDER" in goal_js
    assert "inbox.goal.form.grp." in goal_js
    assert "_kindIcon" in goal_js
    assert "gl-scen-custom" in goal_js
    assert "gl-adv-pill" in goal_js
    assert "inbox.goal.form.adv_pill" in goal_js
    for tok in ("--cp-goal-conv", "--cp-goal-rel", "--cp-goal-eng",
                "--cp-goal-disc", "--cp-goal-sprint", "--cp-violet"):
        assert tok in goal_js, f"missing token ref {tok}"
    assert '"discovery"' in goal_js  # KIND_ORDER 含摸底组
    # 旧「进阶」下划线文字链退役（被高亮卡取代）
    assert 'class="gl-adv"' not in goal_js
    # P18 决策保留：偏好里存了 custom 也不参与预选/「上次」徽标
    assert 'prefs.template !== "custom"' in goal_js


def test_p24_unlock_picker_catalog(goal_js: str):
    """解锁项＝价目表下拉（名称 · 价格），原始 ID 不再直接示人；「聊天里怎么
    称呼它」自动跟随所选项、坐席手改过即不再覆盖；「自定义…」高级手填带
    格式说明（item_id 是 ledger 自动判达成的功能字段，填错=静默失效）。"""
    assert "/api/monetize/catalog" in goal_js
    assert "_loadCatalog" in goal_js
    assert 'data-chg="unlock_sel"' in goal_js
    assert "__custom__" in goal_js
    assert "_autoFillLabel" in goal_js
    assert "_labelTouched" in goal_js
    assert "goal_form_unlock_custom" in goal_js
    assert "inbox.goal.form.unlock_custom_id_help" in goal_js
    assert 'data-chg="tier_sel"' in goal_js
    # pickers 优先（包2 后端 templates 响应），catalog 直拉是旧后端回落
    assert "unlock_items" in goal_js and "site_products" in goal_js


def test_p24_param_widgets(goal_js: str):
    """参数控件注册表：阶段下拉中文化 / 亲密度滑杆 / 备注示例 chips /
    自动继承参数折叠进「高级」；未注册参数回落通用输入框（未来新模板
    零前端改动也能用）。"""
    assert "_paramControl" in goal_js
    assert "_genericParamHtml" in goal_js
    assert "inbox.goal.stage." in goal_js
    assert 'type="range"' in goal_js
    assert "note_example" in goal_js and "goal_note_example" in goal_js
    assert "ADV_PARAMS" in goal_js
    assert "inbox.goal.form.adv_params" in goal_js
    assert "inbox.goal.param_label." in goal_js
    assert "inbox.goal.param_help." in goal_js


def test_p24_arc_preview_and_autonomy_cards(goal_js: str):
    """「AI 会怎么推进」节奏预览（milestones + push_curve，旧后端缺 curve 时
    只显示里程碑名不猜力度）+ 参与度三张单选卡 + 主动触达信息气泡（中性
    说明替代橙色警告——未开启是状态不是错误）。"""
    assert "_arcHtml" in goal_js
    assert "push_curve" in goal_js
    assert "inbox.goal.form.arc_title" in goal_js
    assert "gl-auto-card" in goal_js
    assert 'data-act="pick_autonomy"' in goal_js
    assert "gl-note-info" in goal_js
    assert "_autonomyNoteFull" in goal_js
    assert "inbox.goal.form.auto_note_help" in goal_js
    # 创建链契约不变：仍从隐藏 autonomy input 取值
    assert 'data-ref="autonomy"' in goal_js


def test_p24_backend_pickers_and_push_curve():
    """包2 后端：templates 响应带 pickers 枚举源（解锁项/会员档/官网产品/
    阶段词表，逐段软失败）；push_curve 随模板形状导出；conversion_unlock
    默认值中性化（bazi_reading/八字详批 曾与官网产品业务错位）。"""
    routes = (_REPO / "src" / "web" / "routes" / "goal_routes.py").read_text(encoding="utf-8")
    assert "_pickers" in routes
    assert "unlock_items" in routes and "site_products" in routes
    assert '"stages"' in routes

    from src.companion.goals.templates import list_templates

    shapes = {t["id"]: t for t in list_templates()}
    assert shapes["conversion_unlock"]["push_curve"], "push_curve 须随模板形状导出"
    p = {x["key"]: x for x in shapes["conversion_unlock"]["params"]}
    assert p["item_id"]["default"] == ""
    assert p["item_label"]["default"] == ""
    assert "ID" not in p["item_id"]["label_zh"]


def test_p24_i18n_dynamic_keys_bilingual():
    """P24 动态拼键（grp.<kind> / stage.<stage> / param_label|param_help
    注册面 / note_ex{i}）静态键门禁扫不到——显式钉 zh+en 齐备。"""
    import importlib

    goals_pack = importlib.import_module("src.web.i18n_packs.goals")
    from src.companion.goals.templates import STAGE_ORDER, TEMPLATES

    kinds = {str(t["kind"]) for t in TEMPLATES.values() if t["kind"] != "custom"}
    for k in kinds:
        for lang in (goals_pack.ZH, goals_pack.EN):
            assert f"inbox.goal.form.grp.{k}" in lang, f"missing grp.{k}"
    for s in STAGE_ORDER:
        for lang in (goals_pack.ZH, goals_pack.EN):
            assert f"inbox.goal.stage.{s}" in lang, f"missing stage.{s}"
    for key in (
        "inbox.goal.form.step1_lead", "inbox.goal.form.adv_pill",
        "inbox.goal.form.last_used", "inbox.goal.form.back",
        "inbox.goal.form.arc_title", "inbox.goal.form.arc_hint",
        "inbox.goal.form.params_title", "inbox.goal.form.summary",
        "inbox.goal.form.days_help", "inbox.goal.form.days_help_today",
        "inbox.goal.form.days_help_session", "inbox.goal.form.pace",
        "inbox.goal.form.pace.natural", "inbox.goal.form.pace.today",
        "inbox.goal.form.pace.session", "inbox.goal.form.pace_line",
        "inbox.goal.form.pace.natural_hint", "inbox.goal.form.pace.today_hint",
        "inbox.goal.form.pace.session_hint",
        "inbox.goal.form.horizon_h", "inbox.goal.form.horizon_min",
        "inbox.goal.form.chip_today_end", "inbox.goal.form.arc_hint_sprint",
        "inbox.goal.form.arc_session_1", "inbox.goal.form.arc_session_2",
        "inbox.goal.form.arc_session_3", "inbox.goal.form.arc_today_1",
        "inbox.goal.form.arc_today_2", "inbox.goal.form.arc_today_3",
        "inbox.goal.form.note_ex1_chip",
        "inbox.goal.hold.pace_cap", "inbox.goal.remaining",
        "inbox.goal.extend_30m", "inbox.goal.extend_2h",
        "inbox.goal.outcome.contact", "inbox.goal.outcome.confirm",
        "inbox.goal.done.dur", "inbox.goal.term.beats",
        "inbox.goal.signal.buying", "inbox.goal.signal.open_btn",
        "inbox.goal.signal.dismiss", "inbox.goal.slots.stale_t",
        "err.goals.pace_not_allowed", "inbox.goal.form.auto_note_help",
        "inbox.goal.form.adv_params", "inbox.goal.form.per_month",
        "inbox.goal.form.item_label_ph", "inbox.goal.form.unlock_custom_opt",
        "inbox.goal.form.unlock_custom_id", "inbox.goal.form.unlock_custom_id_ph",
        "inbox.goal.form.unlock_custom_id_help", "inbox.goal.form.product_auto_opt",
        "inbox.goal.form.intimacy_lo", "inbox.goal.form.intimacy_mid",
        "inbox.goal.form.intimacy_hi", "inbox.goal.form.note_ph_custom",
        "inbox.goal.form.note_ph_reactivate", "inbox.goal.form.note_ex_t",
        "inbox.goal.form.note_ex1", "inbox.goal.form.note_ex2",
        "inbox.goal.form.note_ex3",
        "inbox.goal.form.note_ex1_chip", "inbox.goal.form.note_ex2_chip",
        "inbox.goal.form.note_ex3_chip",
        "inbox.goal.param_label.conversion_unlock.item_id",
        "inbox.goal.param_help.conversion_unlock.item_id",
        "inbox.goal.param_label.conversion_unlock.item_label",
        "inbox.goal.param_help.conversion_unlock.item_label",
        "inbox.goal.param_label.conversion_subscribe.tier",
        "inbox.goal.param_help.conversion_subscribe.tier",
        "inbox.goal.param_label.conversion_subscribe.item_label",
        "inbox.goal.param_help.conversion_subscribe.item_label",
        "inbox.goal.param_label.relationship_stage.target_stage",
        "inbox.goal.param_help.relationship_stage.target_stage",
        "inbox.goal.param_label.relationship_intimacy.target_score",
        "inbox.goal.param_help.relationship_intimacy.target_score",
        "inbox.goal.param_label.engagement_reactivate.note",
        "inbox.goal.param_help.engagement_reactivate.note",
        "inbox.goal.param_label.acquire_and_convert.product_id",
        "inbox.goal.param_help.acquire_and_convert.product_id",
        "inbox.goal.param_help.acquire_and_convert.note",
        "inbox.goal.param_help.retention_expand.note",
        "inbox.goal.param_label.custom.note",
        "inbox.goal.param_help.custom.note",
        "inbox.goal.param_label.profile_discovery.slots",
        "inbox.goal.form.rec_discovery",
        "inbox.goal.slots.title",
    ):
        for lang in (goals_pack.ZH, goals_pack.EN):
            assert key in lang, key


def test_p24_goal_kind_tokens_defined_both_themes():
    """--cp-goal-* 类别色必须明暗双表齐平（test_copilot_theme_tokens 管全量
    集合一致，这里显式钉三个新类别色到位，防「只进 light 不进 dark」）。"""
    for name in ("theme-light.css", "theme-dark.css"):
        css = (_REPO / "shared" / "copilot" / name).read_text(encoding="utf-8")
        for tok in ("--cp-goal-conv:", "--cp-goal-rel:", "--cp-goal-eng:",
                    "--cp-goal-disc:", "--cp-goal-sprint:"):
            assert tok in css, f"{name} missing {tok}"


# ── 2026-08-18：摸底置顶 + 场景卡基准线 + 完成通知可见化 + 终局卡升级 ────────


def test_goal_discovery_first_funnel_order(goal_js: str):
    """KIND_ORDER＝经营漏斗序（信息摸底置顶）——组序是入口引导，不只是排版：
    摸底产出的画像反哺后面转化目标的选品/报价。回退成「转化第一」即红。"""
    at = goal_js.find("KIND_ORDER = [")
    assert at > 0
    seg = goal_js[at:at + 120]
    assert seg.find('"discovery"') > 0
    assert seg.find('"discovery"') < seg.find('"conversion"')
    # 组头升级（色点+计数）与摸底组「从这里开始」引导
    assert "gl-grp-dot" in goal_js and "gl-grp-n" in goal_js
    assert "inbox.goal.form.grp_start_hint" in goal_js


def test_goal_scenario_card_benchmark(goal_js: str):
    """场景卡 30 天基准线：一次 report 取齐（_loadBenchAll），organic≥3 才显示
    ——选场景时就建立「达成率/几天见效」预期，不必等到改期限才看见。"""
    assert "_loadBenchAll" in goal_js
    assert "inbox.goal.form.bench_card" in goal_js
    assert "gl-scen-bench" in goal_js


def test_goal_notify_visibility_row(goal_js: str):
    """完成通知状态行（达成后会通知谁）：特性探测 /api/goals/notify-status——
    端点缺席（旧后端/重启窗未到）=整行隐藏绝不裸奔；数据到达走 DOM 注入
    （_fillNotifyRow）不整块重渲染（active 卡可能开着期限/成交表单，重渲染
    抢焦点）。未订阅时「去接通」复用宿主 wsAlertlinkOpen（坐席侧无该函数
    按钮天然不渲染）。"""
    assert "/api/goals/notify-status" in goal_js
    assert "_notifyRowHtml" in goal_js and "_fillNotifyRow" in goal_js
    assert "notify_connect" in goal_js and "wsAlertlinkOpen" in goal_js
    for key in ("inbox.goal.notify.push_on", "inbox.goal.notify.push_off",
                "inbox.goal.notify.scan_off", "inbox.goal.notify.connect"):
        assert key in goal_js, f"missing i18n key {key}"
    # P2：定向副本升格（push_agent && self_bound → 「管理员 + 你」）
    assert "inbox.goal.notify.push_on_agent" in goal_js
    assert "self_bound" in goal_js
    # P3：通知行内自助绑定迷你表单（保存并测试；缓存失效后行升格）
    assert "notify_bind_open" in goal_js and "_nbSave" in goal_js
    assert "/api/workspace/my-notify-binding" in goal_js
    assert "/api/workspace/my-notify-binding/test" in goal_js
    assert "NOTIFY_STATUS.at = 0" in goal_js   # 绑定成功必须失效缓存重探
    for key in ("inbox.goal.notify.bind_btn", "inbox.goal.notify.bind_save",
                "inbox.goal.notify.bind_ok"):
        assert key in goal_js, f"missing i18n key {key}"


def test_goal_terminal_done_card_upgrade(goal_js: str):
    """终局卡：达成金带（gl-term-done）+ 完成方式人话（order/manual/auto，
    原始 result 串降级 tooltip）+ 用时 chips + 48h 跟进提示 + 转化类达成
    「接续留存目标」一键预填（draft 机制复用，人工成交场景补自动链缺口）。"""
    assert "gl-term-done" in goal_js
    assert "_doneKindLabel" in goal_js
    assert "inbox.goal.done.kind." in goal_js
    assert "inbox.goal.done.next_hint" in goal_js
    assert "chain_retention" in goal_js
    assert '"retention_expand"' in goal_js
    # P2：完成推送回执 chip（服务端 notified 键驱动；缺键=不渲染）
    assert "gl-done-pushed" in goal_js
    assert "inbox.goal.done.pushed" in goal_js
    assert "g.notified === true" in goal_js


def test_goal_new_i18n_keys_bilingual():
    """2026-08-18 新键 zh/en 双语齐平（pack 门禁管碰撞，这里钉键存在）。"""
    from src.web.i18n_packs import goals as goals_pack
    for key in (
        "inbox.goal.form.grp_start_hint", "inbox.goal.form.bench_card",
        "inbox.goal.done.days", "inbox.goal.done.kind.order",
        "inbox.goal.done.kind.manual", "inbox.goal.done.kind.auto",
        "inbox.goal.done.next_hint", "inbox.goal.done.chain_btn",
        "inbox.goal.notify.push_on", "inbox.goal.notify.push_on_t",
        "inbox.goal.notify.push_off", "inbox.goal.notify.push_off_t",
        "inbox.goal.notify.scan_off", "inbox.goal.notify.scan_off_t",
        "inbox.goal.notify.connect",
        "inbox.goal.notify.push_on_agent", "inbox.goal.notify.push_on_agent_t",
        "inbox.goal.done.pushed", "inbox.goal.done.pushed_t",
        "inbox.goal.notify.bind_btn", "inbox.goal.notify.bind_hint",
        "inbox.goal.notify.bind_ph", "inbox.goal.notify.bind_save",
        "inbox.goal.notify.bind_busy", "inbox.goal.notify.bind_bad",
        "inbox.goal.notify.bind_ok", "inbox.goal.notify.bind_test_fail",
    ):
        for lang in (goals_pack.ZH, goals_pack.EN):
            assert key in lang, key


def test_goal_mirror_tree_synced():
    """cp-goal.js 双树（shared ↔ desktop/renderer）字节级同步——桌面壳跑旧
    组件＝坐席看到的与网页端不是同一个面板。"""
    mirror = (_REPO / "desktop" / "renderer" / "shared" / "copilot"
              / "components" / "cp-goal.js")
    assert mirror.is_file()
    assert mirror.read_bytes() == _GOAL_JS.read_bytes()
