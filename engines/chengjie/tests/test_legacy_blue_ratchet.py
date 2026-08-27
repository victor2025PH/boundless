# -*- coding: utf-8 -*-
"""裸 periwinkle 旧品牌蓝清零门禁（2026-07-30，品牌统一 P0 收口）。

periwinkle `#5b7cf6` / `rgba(91,124,246,…)` 是 base.html 壳换品牌前的旧主色，
**只当过品牌强调色**（从来不是语义信息蓝）——主色变量切智连蓝后，任何裸写的
periwinkle 都是品牌债务：旁边 var(--p) 的文字已是智连蓝，裸 tint 底纹还是旧紫蓝，
形成肉眼可见色差却不报错。2026-07-30 已用 `tools/audit_legacy_blues.py --fix`
把 117 处收口成 `color-mix(in srgb, var(--p|--tk-brand|--bl-growth,#1e8cf2) N%,
transparent)`（模板 <script> 块 / kb_report.py 走 #1e8cf2 字面量——canvas 上下文
CSS 变量不解析）。本门禁防回潮。

合法保留（分类器豁免，勿「顺手清零」）：
  * ``var(--th-x, 旧蓝)`` fallback —— inline_color_ratchet 要求 fallback ≡
    theme-tokens.css 亮值，实际渲染值由 th-brand-bridge 接管；
  * ``theme-tokens.css`` —— codemod 生成物；
  * 注释里提到旧色的文档。

Tailwind 蓝族（#3b82f6/#2563eb…）**不设全局门禁**：那族语义混杂（信息蓝是合法
语义色），全局硬门禁会误伤正常业务改动。但**逐点人工审完的文件**进天花板台账
`_TAILWIND_CEILINGS`（只许降不许升 + not-stale 反向钉，与 inline_color_ratchet
的 `_INLINE_COLOR_CEILINGS` 同一idiom）：
  * 全品牌文件（unified-inbox.css）＝ 0，机械收口走
    ``tools/audit_legacy_blues.py --fix-tailwind``（注册表 TAILWIND_BRAND_JUDGED）；
  * 混杂文件（workspace_base.html）＝ 判定后保留的语义色数量，新增裸蓝即红。
未进台账的文件只出现在 D 桶报告里，不拦。

分类器单一事实源在 tools/audit_legacy_blues.py（与 test_static_asset_paths
复用 audit_relative_paths 同一模式）。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from tools.audit_legacy_blues import (  # noqa: E402
    PERI_HEX,
    PERI_RGBA,
    TAILWIND_BRAND_JUDGED,
    _in_spans,
    _mask_spans,
    classify,
)

# ── 已判定文件的裸 Tailwind 蓝天花板（只许降不许升） ─────────────────────
# 数字 = 逐点人工判定后**保留**的合法语义色数量。0 = 全品牌文件（已机械收口）。
_TAILWIND_CEILINGS = {
    # 2026-07-30 全文件审毕：100% 品牌 tint（选中态/未读 pill/焦点环/闪烁），
    # 93 处经 --fix-tailwind 收口为 var(--accent)/growth 阶/color-mix
    "src/web/static/workspace/unified-inbox.css": 0,
    # 外观个性化引擎（2026-08-04）：主题预设「智连蓝」bundle 的数据值——JS 要拿
    # hex 字面量算 WCAG 对比度（守卫）再写入 --bbl-out-bg，var() 无法参与运算；
    # 与 CSS 侧回落值 var(--bbl-out-bg,var(--bl-growth-600,#2563eb)) 同源。
    # 属「语义调色板数据」而非样式硬编码，新增预设若引入 Tailwind 蓝需同步调数。
    "src/web/static/workspace/appearance.js": 1,
    # 2026-07-30 逐点审毕，25 处转 14 留 11：info toast(.tk-toast.info) 1 +
    # 套餐阶梯徽章(.ws-plan-basic，蓝/紫/金阶梯刻意非品牌) 3 + 深蓝信息横幅
    # (#dbeafe on --th-bg-blue8，语义蓝族非品牌桥) 1 + JS 类型调色板
    # (message=蓝/contact=紫/note=青 categorical + 通知类型图标 + toast 默认色) 6
    # + 2026-08-17 顶栏随主题(P0-c)：.ws-plan-basic 亮档皮肤 3（rgba(59,130,246,
    #   .12/.40) 底/描边 + #1d4ed8 ink）——同一「蓝/紫/金阶梯」多色家族的亮色臂，
    #   与暗档 3 处同判据保留字面量（一臂跟品牌变量其余臂字面量会破坏阶梯一致性）
    # 2026-08-27 实施75 蓝条退役：ws-restartcool 顶部横幅（含 --th-bg-blue8 渐变端点
    # 的 Tailwind 蓝字面量）随 DOM 摘除，14→13。
    "src/web/templates/workspace_base.html": 13,
    # ── 判据（后续文件沿用）：蓝色是「多色家族的一臂」（同类名族里有 ≥2 个
    # 其它颜色臂：档位梯/状态调色板/类型章/阶段色）→ 留字面量（一臂跟变量
    # 其余臂字面量会破坏体系一致性）；蓝色「独立」当强调/active/hover/tint
    # → 转品牌令牌。 ──────────────────────────────────────────────
    # 19 转 11 留 8：L0-L4 档位梯 .dr-card.l1/.badge-l1（红/琥珀/绿/蓝/灰）3 +
    # intel 徽章族 .dr-intel-intent（intent蓝/emotion绿/risk黄红）3 +
    # JS toast 调色板默认蓝 1 + dr-suggest 顶边 #bfdbfe（随邻近 --th-*-blue*
    # 语义蓝族，非品牌桥辖区）1
    "src/web/templates/draft_review.html": 8,
    # 18 转 7 留 11：tag-green/gray/blue 命名三件套 3 + 「灰=出厂/蓝=已定制/
    # 绿=已发布/琥珀=导入」四色收敛 .src-runtime/.src-studio 6 + 素材池三态
    # .pma-pool-generic（keyword绿/generic蓝/none灰）2
    "src/web/templates/personas.html": 11,
    # 10 转 7 留 3：badge-template绿/badge-task蓝 1 + badge-running蓝/completed绿 1 +
    # exec-step-dot done绿/active蓝 1（均为状态对）
    "src/web/templates/workflows.html": 3,
    # 9 转 5 留 4：渠道章 .ch-web 1 + 消息方向 itl-dot in蓝/out绿 1 +
    # 事件类型点 .stl-dot.stage_sync 2
    "src/web/templates/contact360.html": 4,
    # 9 转 6 留 3：客户阶段调色板 .s-ENGAGED（INITIAL灰/ENGAGED蓝）1 +
    # 时间线点型 .tl-dot.regular 1 + JS 阶段色表 ENGAGED:'#3b82f6' 1
    "src/web/templates/ops/contacts.html": 3,
    # 8 转 0 留 8（全语义文件，诚实结论=不动）：关系阶段调色板
    # stranger灰/friend蓝/close紫/soulmate粉（CSS 渐变/条/章 + 两份 JS 色表）
    # + 分数档 score-pill.s-low 蓝
    "src/web/templates/ai_studio.html": 8,
    # 身份化 P0（2026-08-02）：头像渐变 8 组确定性色池 _EM_GRADS，#3b82f6 为
    # 多色渐变池一臂（与 unified_inbox 头像池同判据：一臂跟变量其余臂字面量
    # 会破坏池子一致性）
    "src/web/templates/episodic_memory.html": 1,
    # 16 转 1 留 15（调色板重镇）：平台色表 PC web=蓝 1 + 头像渐变 12 组池 2 +
    # 账号 8 色池 1 + 说话人分离 6 色 1 + info toast/模式提示 5 + 命名语义令牌
    # --xl-info-*（回复预览蓝框）2 / --bdg-info-*（信息徽章）2 + 冷却 pill info 底 1
    # 2026-08-02 收紧 15→14（08-01 批次减了一处，ratchet 只降不升）
    # 2026-08-10 14→15（清账线按 git diff 归因后代记，owner=接管体检线）：
    # `_toast(...,'#2563eb')` 接管恢复 toast 的信息蓝——与本台账 crm-widgets.js
    # 「toast 语义调色板 info:[...]」同构判据＝语义留；该线在途（.py 待重启装载），
    # owner 若改用 toast 色板常量可回收本处 +1。
    # 2026-08-17 15→16（msgops 线按 HEAD diff 归因后代记，owner=acct-dock 线）：
    # `_toast(window.T('inbox.dock.kbd_hint'),'#2563eb')` 键盘提示 toast 信息蓝——
    # 与上一条 08-10 接管体检线同构判据（toast info 语义色）＝语义留；owner 在途，
    # 若改用 toast 色板常量可回收本处 +1。
    # 2026-08-17 额度批回收 1：预算救济 toast 成功色从 var(--th-bg-blue6,#2563eb)
    # 改语义绿 #16a34a（toast 色板常量口径），16 → 15。
    "src/web/templates/unified_inbox.html": 15,
    # 10 转 3 留 7 → P2-3（2026-08-02）主 IIFE 外迁 static/messenger/messenger_rpa.js
    # 带走 JS 侧 3 处（在下方 JS 条目续记），模板余 4：KPI 卡色 2 + 选择条 on-indigo 1 +
    # dc-tab/dc-bar-cell 品牌蓝回落 1
    "src/web/templates/_channel_body_messenger.html": 4,
    # ↑ 外迁续记：权重刻度（橙≥5/蓝≥2/灰）+ 发送队列五态 + info toast（生成期 JS 串）
    "src/web/static/messenger/messenger_rpa.js": 3,
    # 8 转 5 留 3：交接状态 .st-acknowledged 蓝（多态族）1 + 同族确认按钮
    # .btn-ack 及 hover 2
    "src/web/templates/ops/mobile_handoffs.html": 3,
    # 7 转 2 留 4 → P3-1（2026-08-02）图标 SVG 化回收意图卡蓝图标盒 1，现存 CSS+JS 3
    "src/web/templates/_channel_body_whatsapp.html": 3,
    # 6 转 0 留 6（全语义）：策略卡身份渐变 S2=蓝青 1 + TG 范围章及图例点 4 +
    # 绿蓝双色装饰横幅 1
    "src/web/templates/strategies.html": 6,
    # 6 转 4 留 2：语言徽章 .tm-badge.zh 蓝（语言色族）2
    "src/web/templates/template_mgmt.html": 2,
    # 6 转 4 留 2：头像渐变池 CHC_AV_GRAD 2（--ps/--sp 主色派生令牌已接
    # color-mix(var(--tk-brand))，active 页签蓝臂同）
    "src/web/templates/workspace_channels.html": 2,
    # 6 转 6 留 0：返回链/active pill/双序列图表（同色相深浅对整体平移
    # growth-300/600，图例点随之）——全品牌
    "src/web/templates/workspace_usage.html": 0,
    # 5 转 0 留 5（全语义）：待发状态 sent=蓝 1 + 语言锁徽章 2 + 时间线 run=蓝 1 +
    # 发送队列五态 processing=蓝 1
    "src/web/templates/_channel_body_line.html": 5,
    # ── 2026-07-30 尾批（22 文件 46 处审毕，转 21 留 25）───────────────
    # 3 转 1 留 2 → 2026-08-18 暗色令牌归队批再收 1：分数刻度（≥90/≥75/≥60/其余）
    # **四臂全族**转 --tk-emerald/brand/amber/danger（全族齐转不破「一臂跟变量」判据，
    # 且暗色自适配）；仅留 QA 档 B 蓝 tint rgba(59,130,246,.16) 1——六色家族
    # (A绿/B蓝/C琥珀/D橙/F红/NA灰) 一臂，六臂 ink 已全部令牌化、tint 统一字面量。
    "src/web/templates/agent_perf.html": 1,
    # 3 转 0 留 3（既定决策不动）：--blue/--violet 命名语义变量 1 +
    # 套餐阶梯徽章 .plan-badge-basic（蓝/紫/金）2
    "src/web/templates/base.html": 3,
    # 3 转 0 留 3：审计动作 .act-approved 蓝（多态族）2 + L 档 .lv-L1 1
    "src/web/templates/draft_audit_page.html": 3,
    # 3 转 3 留 0：.gl-act 行动 chip（文字已 var(--tk-brand)，tint 收口）
    "src/web/templates/golive_checklist.html": 0,
    # 3 转 3 留 0：置信值强调 + 导航链接/active（与 ops/contacts 同构）
    "src/web/templates/ops/merge_reviews.html": 0,
    # 3 转 3 留 0：坐席头像底 + 查看/确认重分配按钮
    "src/web/templates/queue_monitor.html": 0,
    # 3 转 1 留 2：品牌色输入 placeholder 示例改 #1e8cf2（诚实示例）；
    # 留 = 伴随 --th-ink-blue6 语义蓝族的 chip tint（--th 蓝族非品牌桥辖区）
    "src/web/templates/settings.html": 2,
    # 3 转 2 留 1：L0-L4 等级色表 1；sparkline 默认色/当前行高亮已收口
    "src/web/templates/workspace_dashboard.html": 1,
    # 3 转 3 留 0：「未指派人设」提示 chip（JS 注入 style，var 可解析）
    "src/web/static/js/persona_studio_core.js": 0,
    # 3 转 0 留 3：toast 语义调色板表 info:[...]（success/warn/error 并列）
    "src/web/static/workspace/crm-widgets.js": 3,
    # 2 转 2 留 0：badge/login 框 tint（文字已 var(--tk-brand)）
    "src/web/templates/setup_wizard.html": 0,
    # 2 转 0 留 2：策略卡身份渐变 + 分层卡 moderate=蓝青（tier 族）
    "src/web/templates/strategy_analytics.html": 2,
    # 2 转 0 留 2：平台色表 web=蓝（wechat绿/viber紫 并列）+ web 图标底
    "src/web/static/platform_icons.js": 2,
    # 2 转 0 留 2：头像渐变池（多组渐变 categorical）；shared 双树勿单边改
    "shared/copilot/components/cp-accounts.js": 2,
    # 1 处语义留（2026-08-17 登记，rail/滚动条批次首扫点名的存量债）：人设身份
    # 8 色盘 PERSONA_DISC_PALETTE 的蓝臂——与宿主 unified_inbox._acctColor 色盘
    # **逐字节同构**（哈希+色盘同构是跨区域同人同色的契约，改任何一边都会让
    # 右栏色盘 ≠ 底部身份条圆点），categorical 一臂非品牌强调；shared 双树勿单边改
    "shared/copilot/components/cp-persona.js": 1,
    # 1 转 0 留 1：漏斗系列色 handoff_rate=蓝（多系列并列）
    "src/web/templates/_rpa_shared_funnel.html": 1,
    # 1 转 0 留 1：意图调色板 inquiry=蓝
    "src/web/templates/_rpa_shared_scripts.html": 1,
    # 各 1 转 0 留 1：渠道章 .ch-web 蓝（与 contact360 同构）
    "src/web/templates/contacts_list.html": 1,
    "src/web/templates/tasks.html": 1,
    # 1 转 0 留 1：状态对 bad=琥珀/ok=浅蓝
    "src/web/templates/dashboard.html": 1,
    # 1 转 1 留 0：下一步引导框 tint（文字已 var(--tk-brand)）
    "src/web/templates/kb_cold_start.html": 0,
    # 1 转 1 留 0：当前套餐列高亮 → color-mix(var(--blue))（留在页面 --blue
    # 语义族内、变量驱动）
    "src/web/templates/membership.html": 0,
    # 1 转 0 留 1：动作调色板 enable=蓝（advance绿/downgrade红 并列）
    "src/web/templates/rpa_overview.html": 1,
    # 2026-08-02 补审（08-01 批次落盘后首扫，5 处全语义留）：分数档
    # .score-pill.s-low 蓝（high绿/mid?/zero灰 档位梯）2 + 关系阶段调色板
    # .stage-tag.t-friend（stranger灰/friend蓝/close紫/soulmate粉）2 +
    # JS 色表 _REL_STAGE_COLORS.friend 1 —— 与 ai_studio.html 同构判据
    "src/web/templates/analytics.html": 5,
    # 2026-08-10 登记（清账线代记，owner=goal 报表线，意向已标 DEPLOYED 未跑广域
    # 门禁）：9 处按判据分类＝语义留 2（.st-active 蓝=「进行中」状态档，与 done绿/
    # failed红/expired橙 并列的档位调色板）+ 品牌用途应令牌化 7（.gr-act:hover 2 +
    # .gr-chip 3 + .gr-btn-primary 2）——后者随该页暗色适配一起归 owner 线收口
    # （与 inline_color 台账同一条目注释互引）。数值取自门禁实测。
    # 2026-08-20 收紧 9 → 1（owner 线已把 .gr-act:hover / .gr-chip / .gr-btn-primary
    # 那 7 处品牌用途令牌化，实测只剩 1 处）：棘轮只降不升，这行不跟着降就等于门禁
    # 给回了 8 个格的退化空间（清账线代记，非本线业务改动）。
    "src/web/templates/goal_report.html": 1,
}


def test_no_bare_periwinkle_anywhere():
    """模板 / 静态 CSS·JS / shared·desktop 入口 / kb_report 里裸 periwinkle 必须为 0。"""
    bare = classify()["bare_periwinkle"]
    assert not bare, (
        "出现裸 periwinkle 旧品牌蓝（品牌已切智连蓝 #1e8cf2，裸紫蓝=与主色变量"
        "肉眼色差）。修法：python tools/audit_legacy_blues.py --fix，"
        "或手工换成 color-mix(in srgb, var(--p,#1e8cf2) N%, transparent) / "
        "var(--p,#1e8cf2)（canvas/图表上下文用 #1e8cf2 字面量）：\n  "
        + "\n  ".join(f"{f}:{ln}  {snip}" for f, ln, snip in bare[:20])
    )


def test_judged_files_tailwind_ceiling():
    """已判定文件：裸 Tailwind 蓝 ≤ 台账天花板；低于天花板须收紧（防台账虚高）。"""
    counts = classify()["tailwind_bare"]
    over, stale = [], []
    for rel, ceiling in _TAILWIND_CEILINGS.items():
        actual = counts.get(rel, 0)
        if actual > ceiling:
            over.append(f"{rel}: 实际 {actual} > 天花板 {ceiling}")
        elif actual < ceiling:
            stale.append(f"{rel}: 实际 {actual} < 天花板 {ceiling}（请收紧台账）")
    assert not over, (
        "已判定文件新增了裸 Tailwind 蓝。该文件的既有蓝已逐点定性（品牌→令牌 / "
        "语义→留），新增裸蓝要么该写 var(--tk-brand)/growth 阶，要么是新语义色"
        "（更新台账数字并在注释里写清理由）：\n  " + "\n  ".join(over)
    )
    assert not stale, (
        "台账虚高会吞掉倒退空间（与 inline_color_ratchet 同规则）：\n  "
        + "\n  ".join(stale)
    )


def test_no_unledgered_tailwind_files():
    """全量台账制（2026-07-30 尾批收口后升格）：任何文件出现裸 Tailwind 蓝都必须在台账里。

    39 个存量文件已全部逐点审毕（品牌→令牌化，语义→带理由入账）。新文件裸写
    Tailwind 蓝只有两条正路：品牌用途写 var(--tk-brand)/var(--p)/growth 阶 +
    color-mix；真语义调色板（状态/类型/平台/档位）则在 _TAILWIND_CEILINGS 加一行
    并注明理由。没有第三种「先裸写再说」。
    """
    counts = classify()["tailwind_bare"]
    unlisted = {rel: n for rel, n in counts.items() if rel not in _TAILWIND_CEILINGS}
    assert not unlisted, (
        "台账外文件出现裸 Tailwind 蓝（#3b82f6/#2563eb/#dbeafe…）。品牌强调请写 "
        "var(--tk-brand,#1e8cf2)/growth 阶/color-mix；确属语义调色板（多色家族的"
        "一臂）则在 _TAILWIND_CEILINGS 登记并写明理由：\n  "
        + "\n  ".join(f"{k}: {v} 处" for k, v in sorted(unlisted.items()))
    )


def test_brand_judged_registry_consistent():
    """注册表（全品牌可机械收口）文件必须在台账里钉 0——两处口径不一致=门禁自相矛盾。"""
    for rel in TAILWIND_BRAND_JUDGED:
        assert _TAILWIND_CEILINGS.get(rel) == 0, (
            f"{rel} 在 TAILWIND_BRAND_JUDGED（全品牌）却未在天花板台账钉 0"
        )


def test_detectors_actually_detect():
    """探测器自证：掩码既要豁免合法形态，也不能放过真裸写（防「永远绿的摆设」）。"""
    # 1) 裸写必须能命中（hex 与 rgba 两种形态）
    assert PERI_HEX.search("border-color:#5b7cf6;")
    assert PERI_RGBA.search("background:rgba(91,124,246,.12)")
    assert PERI_RGBA.search("background:rgba( 91 , 124 , 246 , 0.4 )")

    # 2) var() fallback（含嵌套括号）必须被掩码豁免
    s = "color:var(--th-bg-brand08,rgba(91,124,246,.08));"
    spans = _mask_spans(s)
    m = PERI_RGBA.search(s)
    assert m and _in_spans(m.start(), spans), "var fallback 里的 periwinkle 应被豁免"

    # 3) 双层嵌套（渐变里再套 rgba）也要整段豁免——平衡括号扫描的存在理由
    s2 = "background:var(--g,linear-gradient(135deg,rgba(91,124,246,.5),#fff));"
    spans2 = _mask_spans(s2)
    m2 = PERI_RGBA.search(s2)
    assert m2 and _in_spans(m2.start(), spans2), "嵌套 fallback 应整段豁免"

    # 4) 注释文档提到旧色不算违规；注释外裸写必须算
    s3 = "/* 原 #5b7cf6 已废弃 */ .x{color:#5b7cf6}"
    spans3 = _mask_spans(s3)
    hits = [m for m in PERI_HEX.finditer(s3) if not _in_spans(m.start(), spans3)]
    assert len(hits) == 1, "应只命中注释外那一处"
