# -*- coding: utf-8 -*-
"""驾驶舱入口显隐门禁（ui_visibility.cockpit，2026-08-16）。

老板决定：人工操作台（manual_console）既然藏了，驾驶舱跟着藏——该页主动作是
「一键接管/交还」，接管要跳原生页，而原生页整条链已随 manual_console 关闭。

钉住四条：

- **缺省隐藏**：新键进 UI_VISIBILITY_KEYS 且缺省 False（与其余六键同约定）；
- **顶栏入口 fail-hidden**：``workspace_base.html`` 的 ``/workspace/cockpit``
  链接必须整块包在 ``(ui_vis or {}).get('cockpit')`` 闸内。**缺键即隐藏**是
  刻意的——模板热更新先于重启上线，中间态里进程还不认识 cockpit 键，此时
  按钮就该已经不见（settings.html 的 ai_settings 走的是反向的「缺键回落显示」，
  因为那批不想在重启前就让卡片消失；两处语义不同，别照抄）；
- **藏而不废**：``/workspace/cockpit`` 与 ``/api/cockpit/overview`` 路由照旧，
  开发者页显隐清单收录 cockpit（藏了却无处可开＝把功能焊死）；
- **用量裁决不被误读**：入口藏了之后零点击是「进不来」不是「没人用」，
  ``cockpit_usage_report`` 必须在报告顶部打横幅（否则 8-28 满窗时会得出
  「没人用，砍掉」的错误结论——那正是这次隐藏造成的）。

新文件而非并进 test_ui_visibility.py：共享树多线并发下新文件＝零撞车。
"""
from __future__ import annotations

from pathlib import Path

_ENGINE_ROOT = Path(__file__).resolve().parents[1]
_TPL = _ENGINE_ROOT / "src" / "web" / "templates"

_NAV_HREF = 'href="/workspace/cockpit"'
_GATE = "{% if (ui_vis or {}).get('cockpit') %}"


def _read(rel: str) -> str:
    return (_TPL / rel).read_text(encoding="utf-8")


# ── 键契约 ───────────────────────────────────────────────────────────

def test_cockpit_key_defaults_hidden():
    from src.web.ui_visibility import DEFAULTS, UI_VISIBILITY_KEYS, resolve_ui_visibility

    assert "cockpit" in DEFAULTS, "驾驶舱显隐键丢失"
    assert DEFAULTS["cockpit"] is False
    assert "cockpit" in {k for k, _ in UI_VISIBILITY_KEYS}, "开发者页清单取不到该键"
    # 无配置段 / 坏配置 → 隐藏（fail-hidden）
    assert resolve_ui_visibility(None)["cockpit"] is False
    assert resolve_ui_visibility({})["cockpit"] is False
    assert resolve_ui_visibility({"ui_visibility": {}})["cockpit"] is False


def test_cockpit_key_can_be_reopened():
    from src.web.ui_visibility import is_visible, resolve_ui_visibility

    cfg = {"ui_visibility": {"cockpit": True}}
    assert resolve_ui_visibility(cfg)["cockpit"] is True
    assert is_visible(cfg, "cockpit") is True
    # 与 manual_console 正交：开驾驶舱不连带开人工操作台
    assert resolve_ui_visibility(cfg)["manual_console"] is False


# ── 顶栏入口 ─────────────────────────────────────────────────────────

def test_nav_entry_wrapped_in_visibility_gate():
    tpl = _read("workspace_base.html")
    assert tpl.count(_NAV_HREF) == 1, "顶栏驾驶舱链接数量变了，闸门断言需同步"
    gate = tpl.find(_GATE)
    assert gate >= 0, "顶栏驾驶舱入口未挂 ui_visibility.cockpit 闸"
    href = tpl.find(_NAV_HREF)
    endif = tpl.find("{% endif %}", gate)
    assert gate < href < endif, "驾驶舱链接不在闸内（闸开着＝按钮照旧显示）"


def test_nav_gate_is_fail_hidden_not_fail_shown():
    """缺键必须隐藏：写成 `'cockpit' not in ui_vis` 那种回落显示就是没藏成。

    模板热更新直上生产、而 .py 要等重启——中间态里进程没有 cockpit 键，
    此刻正是老板要求「不要再出现按钮」的时刻。
    """
    tpl = _read("workspace_base.html")
    seg_start = tpl.find(_GATE)
    seg = tpl[max(0, seg_start - 400):tpl.find(_NAV_HREF)]
    assert "'cockpit' not in" not in seg, "写成了缺键回落显示，重启前按钮仍在"


def test_no_other_cockpit_entry_left_in_templates():
    """全站模板不得留第二个未过闸的驾驶舱入口（藏一处露一处＝没藏）。"""
    offenders = []
    for p in _TPL.rglob("*.html"):
        src = p.read_text(encoding="utf-8", errors="replace")
        if _NAV_HREF not in src:
            continue
        if p.name != "workspace_base.html":
            offenders.append(p.name)
    assert not offenders, f"这些模板留了未过闸的驾驶舱入口：{offenders}"


# ── 藏而不废 ─────────────────────────────────────────────────────────

def test_page_and_api_routes_survive():
    """只藏入口，URL/API 不封（与 manual_console / matrix_nav 同哲学）。"""
    routes = (_ENGINE_ROOT / "src" / "web" / "routes"
              / "unified_inbox_workspace_pages_routes.py").read_text(encoding="utf-8")
    assert '"/workspace/cockpit"' in routes, "页面路由被误删（应只藏入口）"
    api = (_ENGINE_ROOT / "src" / "web" / "routes"
           / "cockpit_routes.py").read_text(encoding="utf-8")
    assert "/api/cockpit/overview" in api, "聚合 API 被误删（应只藏入口）"


def test_developer_page_lists_cockpit_toggle():
    tpl = _read("developer.html")
    assert "'ai_settings', 'cockpit'" in tpl, \
        "开发者页显隐清单未收录 cockpit（藏了却无处可开）"
    # 新键守卫：清单靠 `_k in _uiv` 过滤，重启前不渲染「点了报 404」的行
    assert "if _k in _uiv" in tpl, "新键渲染守卫丢失"


def test_developer_toggle_labels_bilingual():
    from src.web.i18n_packs.developer_page import EN, ZH

    for k in ("dv_uiv_k_cockpit", "dv_uiv_k_cockpit_d"):
        assert ZH.get(k), f"{k} 缺中文"
        assert EN.get(k), f"{k} 缺英文"


# ── 用量裁决不被误读 ─────────────────────────────────────────────────

def test_usage_report_banners_hidden_entry():
    from tools.cockpit_usage_report import CK_ENTRY_HIDDEN_DAY, render_text

    assert CK_ENTRY_HIDDEN_DAY, "入口隐藏日未登记，报告不会提醒读数人"
    rep = {"root": "r", "db": "d", "entry_hidden_since": CK_ENTRY_HIDDEN_DAY,
           "missing": True, "note": "无库"}
    out = "\n".join(render_text(rep))
    assert "入口已隐藏" in out and CK_ENTRY_HIDDEN_DAY in out
    # 库缺失分支同样要打横幅（那条 early-return 最容易漏）
    assert out.index("入口已隐藏") < out.index("无库")


def test_usage_report_banner_absent_when_entry_reopened():
    from tools.cockpit_usage_report import render_text

    out = "\n".join(render_text({"root": "r", "db": "d", "window_days": 30,
                                 "grand_total": 0, "totals": {}, "verdicts": []}))
    assert "入口已隐藏" not in out
