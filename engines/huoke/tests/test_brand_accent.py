# -*- coding: utf-8 -*-
"""品牌强调色对齐钉（2026-07-30，无界品牌统一 Phase-1）。

后台交互强调色已从 Tailwind 蓝对齐到无界智连蓝（platform/brand/tokens.json 的
--bl-growth 阶）：dark #1e8cf2(500) / light #0d76d9(600)。大多数规则走
var(--accent)，改定义点即全站生效——本测试钉住定义点，防止将来有人把
Tailwind 蓝写回去（视觉「差不多对」所以极易漏审）。

**刻意保留**的旧蓝（勿「顺手清零」）：stat-card.blue 卡片族、badge/ts/tb-running
运行态族、log INFO / toast.info / anomaly info / oc-dlg info 语义蓝、
funnel-colors-* 序列色，以及 JS 里全部状态/阶段/等级/图表调色板——它们是
多色家族的一臂，一臂跟品牌变量其余臂字面量会破坏体系（判据沉淀见
chengjie tests/test_legacy_blue_ratchet.py 头注）。
"""
from pathlib import Path

CSS = Path(__file__).resolve().parents[1] / "src" / "host" / "static" / "css" / "dashboard.css"


def test_accent_tokens_are_boundless_growth():
    text = CSS.read_text(encoding="utf-8")
    assert "--accent: #1e8cf2;" in text, (
        "dark 主题 --accent 应为无界智连蓝 #1e8cf2（growth-500）——"
        "勿写回 Tailwind #3b82f6"
    )
    assert "--accent: #0d76d9;" in text, (
        "light 主题 --accent 应为 #0d76d9（growth-600）——勿写回 #2563eb"
    )
    assert "--accent-glow: rgba(30,140,242,.25);" in text
    assert "--accent-glow: rgba(13,118,217,.18);" in text


def test_no_tailwind_accent_definitions():
    """定义点不得回退（其余散点是刻意保留的调色板/语义色，不在此检查范围）。"""
    text = CSS.read_text(encoding="utf-8")
    assert "--accent: #3b82f6" not in text
    assert "--accent: #2563eb" not in text
