# -*- coding: utf-8 -*-
"""回复策略用户版收起（L-4 D，#212，D-L2，2026-09-06）门禁。

AMTKXC 实录：回复策略＝大模型调参面板（Temperature / Max Tokens / 上下文轮数 / 意图映射）
对用户版直出，1.0.74 只折叠了参数。钉住：client 形态整页 l4-locked + 横幅；partner /
internal 原面板；侧栏入口已剔（A 项）。三档预设映射底层参数属「待老板」，本单不做。
"""
from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader

REPO = Path(__file__).resolve().parents[1]
TPL_DIR = REPO / "src" / "web" / "templates"


def test_template_wiring():
    tpl = (TPL_DIR / "strategies.html").read_text(encoding="utf-8")
    assert '{% include "_client_tier_upgrading.html" %}' in tpl
    assert '<div id="st-root" class="{% if ui_client_hide %}l4-locked{% endif %}">' in tpl
    # 收起容器必须包住参数卡与意图映射（两处研发面），闭合在 <script> 之前
    body = tpl.split("<script>\nfunction markDirty", 1)[0]
    assert 'class="st-grid"' in body and 'id="map-adv"' in body
    assert body.rstrip().endswith("</div>"), "st-root 未在脚本前闭合"


def test_nav_hides_strategies_for_client():
    from src.web.nav_schema import CLIENT_HIDDEN_ITEM_IDS, get_nav_context
    assert "strategies" in CLIENT_HIDDEN_ITEM_IDS
    ctx = get_nav_context({"ui_visibility": {"flavor": "client"}})
    paths = {it.get("path") for g in ctx["nav_groups"] for it in g["items"] if isinstance(it, dict)}
    assert "/strategies" not in paths
    # 自动回复设置（面向用户的档位页）仍在
    assert "/reply-settings" in paths


def test_locked_render_hides_panel_but_keeps_banner():
    env = Environment(loader=FileSystemLoader(str(TPL_DIR)))
    src = (TPL_DIR / "strategies.html").read_text(encoding="utf-8")
    # 只取 content 块里 include + 容器开头两行做渲染（整页依赖 base.html 上下文）
    lines = [l for l in src.splitlines()
             if "_client_tier_upgrading.html" in l or 'id="st-root"' in l]
    t = env.from_string("\n".join(lines))
    out = t.render(ui_client_hide=True, i18n={})
    assert 'data-l4-upgrading="1"' in out and 'class="l4-locked"' in out
    out2 = t.render(ui_client_hide=False, i18n={})
    assert "l4-upgrading" not in out2 and 'class=""' in out2
