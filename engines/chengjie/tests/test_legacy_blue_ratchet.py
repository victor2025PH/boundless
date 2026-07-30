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
    # 2026-07-30 逐点审毕，25 处转 14 留 11：info toast(.tk-toast.info) 1 +
    # 套餐阶梯徽章(.ws-plan-basic，蓝/紫/金阶梯刻意非品牌) 3 + 深蓝信息横幅
    # (#dbeafe on --th-bg-blue8，语义蓝族非品牌桥) 1 + JS 类型调色板
    # (message=蓝/contact=紫/note=青 categorical + 通知类型图标 + toast 默认色) 6
    "src/web/templates/workspace_base.html": 11,
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
    # 16 转 1 留 15（调色板重镇）：平台色表 PC web=蓝 1 + 头像渐变 12 组池 2 +
    # 账号 8 色池 1 + 说话人分离 6 色 1 + info toast/模式提示 5 + 命名语义令牌
    # --xl-info-*（回复预览蓝框）2 / --bdg-info-*（信息徽章）2 + 冷却 pill info 底 1
    "src/web/templates/unified_inbox.html": 15,
    # 10 转 3 留 7：info toast 2 + KPI 卡色 2 + 权重刻度（橙≥5/蓝≥2/灰）1 +
    # 发送队列五态（amber/蓝/绿/红/灰）1 + 选择条 on-indigo 浅蓝文字 1
    "src/web/templates/_channel_body_messenger.html": 7,
    # 8 转 5 留 3：交接状态 .st-acknowledged 蓝（多态族）1 + 同族确认按钮
    # .btn-ack 及 hover 2
    "src/web/templates/ops/mobile_handoffs.html": 3,
    # 7 转 2 留 5：意图调色板（purchase绿/support红/inquiry蓝/greeting灰）
    # CSS+JS 4 + KPI 卡图标 tint 1
    "src/web/templates/_channel_body_whatsapp.html": 5,
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
