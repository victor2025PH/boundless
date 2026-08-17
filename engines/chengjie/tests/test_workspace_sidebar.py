# -*- coding: utf-8 -*-
"""工作台左侧导航（`_ws_sidebar.html`）门禁。

背景：四渠道设置页在「渠道中心融合」后从管理后台外壳迁进工作台外壳，而工作台
外壳只有顶栏 → 主管进渠道中心就整条丢失侧栏导航。侧栏遂搬进工作台壳，**默认关**
（收件箱是三栏满高布局，塞侧栏会挤坏），按页 opt-in。

钉四条不变量：
1. 渠道中心渲染出侧栏且当前渠道高亮；
2. 收件箱**不得**出现侧栏、`.ws-body` 不得被改成弹性布局（默认关的回归钉）；
3. 菜单单一事实源＝`nav_schema`——partial 里不许手写业务菜单 <a>；
4. 折叠按钮不用内联 on*（本仓「哑按钮」事故的高发写法）。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_PARTIAL = _ROOT / "src" / "web" / "templates" / "_ws_sidebar.html"
_SHELL = _ROOT / "src" / "web" / "templates" / "workspace_base.html"
_CHANNEL_TPL = _ROOT / "src" / "web" / "templates" / "workspace_channels.html"

_ASIDE_RE = re.compile(r'<aside class="wsb-side".*?</aside>', re.S)


def _aside(html: str) -> str:
    m = _ASIDE_RE.search(html)
    assert m, "侧栏 <aside class=\"wsb-side\"> 未渲染"
    return m.group(0)


# ── 1) 渠道中心：侧栏在场 + 当前渠道高亮 ────────────────────────────────
@pytest.mark.parametrize(
    "channel,path",
    [("telegram", "/workspace/channels/telegram"),
     ("line", "/workspace/channels/line"),
     ("messenger", "/workspace/channels/messenger"),
     ("whatsapp", "/workspace/channels/whatsapp")],
)
def test_channel_center_renders_sidebar(auth_client, channel, path):
    r = auth_client.get(path)
    assert r.status_code == 200, (channel, r.status_code)
    html = r.text
    assert 'id="ws-side"' in html, f"{channel}: 侧栏缺失"
    assert "ws-has-side" in html, f"{channel}: .ws-body 未切到带侧栏布局"
    # 侧栏必须在正文之前（壳序：aside → 正文容器）
    assert html.find('id="ws-side"') < html.find('<div class="chc-wrap"')
    # 本渠道那一项高亮（active 类落在自身 href 上）
    body = _aside(html)
    hit = re.search(r'<a href="%s"[^>]*class="active"' % re.escape(path), body)
    assert hit, f"{channel}: 侧栏未高亮当前渠道"


# ── 2) 收件箱回归钉：默认关，布局零改动 ─────────────────────────────────
def test_inbox_has_no_sidebar(auth_client):
    r = auth_client.get("/workspace")
    assert r.status_code == 200
    html = r.text
    assert 'id="ws-side"' not in html, "收件箱不该有侧栏（三栏满高布局会被挤坏）"
    # 只看 <main> 的 class 实参：ws-has-side 的样式规则本身在每页壳 CSS 里都在，
    # 裸搜字符串会永远命中（首版就这么误报过）。
    m = re.search(r'<main class="([^"]*)"', html)
    assert m, "收件箱 <main> 缺失"
    assert "ws-has-side" not in m.group(1), "收件箱的 .ws-body 不该被切成弹性布局"


# ── 3) 菜单单一事实源＝nav_schema ───────────────────────────────────────
def test_sidebar_items_come_from_nav_schema(auth_client):
    """每条菜单都得自报来源，且 schema 来源的 href 必须真在 NAV_ITEMS 里。

    来源标记（data-nav）三档：schema=nav_schema 菜单项 / domain=域 manifest 动态页
    （如 payment 域的 /channels）/ locked=档位锁定项跳会员中心。没有标记的 <a>
    就是有人手写进来的第二份菜单事实源。
    """
    from src.web.nav_schema import NAV_ITEMS

    body = _aside(auth_client.get("/workspace/channels/telegram").text)
    anchors = re.findall(r'<a ([^>]*?)>', body)
    assert anchors, "侧栏一个菜单项都没渲染"

    def _attr(tag: str, name: str) -> str:
        m = re.search(r'%s="([^"]*)"' % name, tag)
        return m.group(1) if m else ""

    untagged = [_attr(a, "href") for a in anchors if not _attr(a, "data-nav")]
    assert not untagged, f"侧栏出现未标来源的手写菜单：{untagged}"

    schema_paths = {str(it["path"]) for it in NAV_ITEMS.values() if it.get("path")}
    rendered = {_attr(a, "href") for a in anchors if _attr(a, "data-nav") == "schema"}
    assert rendered - schema_paths == set(), (
        f"侧栏 schema 项出现 NAV_ITEMS 外的路径：{rendered - schema_paths}")
    # 正向：四渠道 + 一个非渠道项都在（证明渲染的是真菜单而非空壳）
    assert "/workspace/channels/telegram" in rendered
    assert "/rpa-overview" in rendered


def test_partial_does_not_hardcode_menu_links():
    """partial 里除锁定项/模式切换外不许出现字面量业务链接。

    手写一条 <a href="/personas"> 就是第二份菜单事实源：schema 改了它不跟，
    于是后台侧栏与工作台侧栏开始漂移（本仓 base.html 顶部就写着「别在这里加 <a>」）。
    """
    txt = _PARTIAL.read_text(encoding="utf-8")
    literal = set(re.findall(r'<a[^>]+href="(/[^"{]*)"', txt))
    assert literal <= {"/membership"}, f"partial 手写了业务菜单链接：{literal}"
    assert "nav_groups" in txt and "nav_icons" in txt, "侧栏未消费 nav_schema 数据"


# ── 4) 哑按钮防线 + 开关接线 ────────────────────────────────────────────
def test_sidebar_uses_no_inline_handlers():
    txt = _PARTIAL.read_text(encoding="utf-8")
    assert not re.search(r"\son[a-z]+=", txt), (
        "侧栏用了内联 on* handler：必须挂 window 才可达，是本仓哑按钮事故高发写法")
    assert "addEventListener" in txt


def test_shell_opt_in_wiring():
    shell = _SHELL.read_text(encoding="utf-8")
    assert "{% if ws_sidebar %}{% include \"_ws_sidebar.html\" %}" in shell
    assert ".ws-body.ws-has-side{display:flex;}" in shell
    # 渠道中心是当前唯一开启方（顶层 set 才对父模板可见，写进 block 里不生效）
    chan = _CHANNEL_TPL.read_text(encoding="utf-8")
    assert re.search(r"^\{% set ws_sidebar = true %\}$", chan, re.M), (
        "ws_sidebar 必须在子模板顶层 set，放进 block 内父模板读不到")
