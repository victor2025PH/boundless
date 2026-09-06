# -*- coding: utf-8 -*-
"""#221 顶栏胶囊 / 主题 / 溢出 / 徽标（M-4 B，2026-09-06 证据包 4KEJ63 / Y8Z47B / 7E3XKR）。

事故：桌面壳 webview 地址 /workspace?lang=zh&theme=dark；顶栏「自动 (1)」胶囊是
href="/workspace" 整页链接 → 点一下整页重载、丢 ?theme= 按 auto 渲染成浅色、掐断进行中的
loadChats/loadThread；徽标改成「旗舰版（厂商自营）」后右侧元素串溢出窗口。

修法**不推翻**主题窗口级钉子不变量（workspace_base.html L22–34：?theme= 只作用本窗口、
绝不写 localStorage(cp_theme)）：站内导航一律带上当前 ?theme= 与 lang=（wsNav / wsNavUrl +
window 捕获相位链接改写 + __wsGoHome 默认 url 补齐）；胶囊改页内筛选
（window.wsFilterAutoConversations，行为定义归 M-2 E）；徽标改回「旗舰版」、override 措辞进
悬浮（D-M8）；顶栏右侧收缩规则（药丸组先让位 → 徽标文字→图标 → 用户名）。

本文件钉四件事：① 助手存在且不落盘；② wsNavUrl 语义（node 跑模板源码）；③ 裸
location.href= 只减不增（工作台家族模板 ratchet）；④ 胶囊 / 徽标 / 收缩规则的 DOM 契约。
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TPL = REPO / "src" / "web" / "templates"
WB = (TPL / "workspace_base.html").read_text(encoding="utf-8")


def _block(src: str, start_marker: str, end_marker: str) -> str:
    i = src.index(start_marker)
    j = src.index(end_marker, i)
    return src[i:j]


# ── ① 助手存在，且不推翻窗口级钉子不变量 ─────────────────────────────────────

def test_ws_nav_helper_defined_and_theme_pin_invariant_intact():
    assert "window.wsNavUrl=window.wsNavUrl||navUrl;" in WB
    assert "window.wsNav=window.wsNav||function(url){ location.href=window.wsNavUrl(url); };" in WB
    # window 捕获相位（先于 _win_unique 的 document 捕获拦截器）改写同源 <a href>
    assert re.search(r"window\.addEventListener\(\s*'click'\s*,\s*function\(ev\)\{[\s\S]*?wsNavUrl\(href\)[\s\S]*?\}\s*,\s*true\s*\)", WB), \
        "缺 window 捕获相位链接改写"
    # __wsGoHome 默认 url 补齐（包一层，不改 _win_unique）
    assert "w.__wsNavWrapped=true;" in WB and "window.__wsGoHome=w;" in WB
    # 不变量原文仍在（别推翻）：钉是窗口级、绝不写 cp_theme、释放走 sessionStorage
    head = WB[:12000]
    assert "窗口级" in head and "cp_theme_pin_off" in head and "cpReleaseThemePin" in head
    # wsNav 助手块自身不碰 localStorage（带 theme 的做法是「原样携带」，不是「持久化」）
    blk = _block(WB, "站内导航主题安全助手 wsNav", "外观引擎升为全工作台装载")
    assert not re.search(r"localStorage\s*\.\s*(setItem|removeItem)|localStorage\[", blk), \
        "wsNav 块不得写 localStorage——那会把窗口级钉子升成 profile 级"
    assert "sessionStorage.setItem" not in blk


# ── ② wsNavUrl 语义（node 跑模板里的真实源码） ───────────────────────────────

_NODE = shutil.which("node")


def _run_navurl(cases, search):
    blk = _block(WB, "(function(){\n  var CARRY=['theme','lang'];", "</script>")
    harness = r"""
    const _URL = URL, _USP = URLSearchParams;
    global.location = { search: %s, href: 'http://127.0.0.1:18799/workspace' + %s, origin: 'http://127.0.0.1:18799', pathname: '/workspace' };
    global.window = { addEventListener(){}, __wsGoHome: function(o){ return o; } };
    %s
    const cases = %s;
    const out = {};
    for (const c of cases) out[c] = window.wsNavUrl(c);
    out.__gohome = window.__wsGoHome({cid: 'telegram:acc:x'}).url;
    out.__gohome_plain = window.__wsGoHome({}).url;
    process.stdout.write(JSON.stringify(out));
    """ % (json.dumps(search), json.dumps(search), blk, json.dumps(cases))
    r = subprocess.run([_NODE, "-e", harness], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


@pytest.mark.skipif(not _NODE, reason="node 不在 PATH")
def test_navurl_carries_theme_and_lang_only_when_present():
    cases = ["/workspace", "/workspace/drafts", "/x?a=1#h", "/workspace?theme=light",
             "https://other.example/x", "javascript:void(0)", "#top", ""]
    out = _run_navurl(cases, "?lang=zh&theme=dark")
    assert out["/workspace"] == "/workspace?theme=dark&lang=zh"
    assert out["/workspace/drafts"] == "/workspace/drafts?theme=dark&lang=zh"
    assert out["/x?a=1#h"] == "/x?a=1&theme=dark&lang=zh#h"
    assert out["/workspace?theme=light"] == "/workspace?theme=light&lang=zh"   # 目标已带 theme 不覆盖
    assert out["https://other.example/x"] == "https://other.example/x"          # 外链不动
    assert out["javascript:void(0)"] == "javascript:void(0)"
    assert out["#top"] == "#top"
    assert out[""] == ""
    # __wsGoHome 默认 url 补齐（深链 ?conv= 不丢）
    assert out["__gohome"] == "/workspace?conv=telegram%3Aacc%3Ax&theme=dark&lang=zh"
    assert out["__gohome_plain"] == "/workspace?theme=dark&lang=zh"


@pytest.mark.skipif(not _NODE, reason="node 不在 PATH")
def test_navurl_noop_without_pin_params():
    out = _run_navurl(["/workspace", "/workspace/drafts?x=1"], "")
    assert out["/workspace"] == "/workspace"
    assert out["/workspace/drafts?x=1"] == "/workspace/drafts?x=1"
    assert out["__gohome_plain"] == "/workspace"


# ── ③ 裸导航 ratchet（工作台家族模板；只减不增） ─────────────────────────────

_BARE = re.compile(r"""location\.href\s*=\s*['"]""")

# 2026-09-06 基线：workspace_base 全部改走 wsNav（0）；unified_inbox 4 处不在 M-4 的四个
# 改动位内（M-2 / M-3 热区，且已由 window 捕获相位链接改写覆盖 <a> 导航）——留给后续批次
# 收口，天花板只许往下调。其余为工作台子页既有量。
_CEILINGS = {
    "workspace_base.html": 0,
    "unified_inbox.html": 4,
    "workspace_dashboard.html": 1,
    "contact360.html": 2,
    "queue_monitor.html": 1,
    "escalation_log.html": 1,
    "_win_unique.html": 1,     # _navWithFeedback(url)：url 由 __wsGoHome 包装层补齐 theme/lang
}


def test_bare_location_href_not_above_ceiling():
    offenders = []
    for name, cap in _CEILINGS.items():
        n = len(_BARE.findall((TPL / name).read_text(encoding="utf-8", errors="replace")))
        if n > cap:
            offenders.append(f"{name}: {n} > {cap}")
    assert not offenders, (
        "新增裸 location.href= 导航会丢 ?theme=/lang=（桌面壳窗口级钉子）——请改用 wsNav(url)：\n  "
        + "\n  ".join(offenders))


def test_bare_location_href_ledger_not_stale():
    stale = [f"{name}: 实际 {len(_BARE.findall((TPL / name).read_text(encoding='utf-8', errors='replace')))} < 天花板 {cap}"
             for name, cap in _CEILINGS.items()
             if len(_BARE.findall((TPL / name).read_text(encoding="utf-8", errors="replace"))) < cap]
    assert not stale, "裸导航已减少，请把天花板收紧：\n  " + "\n  ".join(stale)


# ── ④ 胶囊 / 徽标 / 收缩规则 DOM 契约 ────────────────────────────────────────

def test_auto_pill_is_in_page_filter_not_full_reload():
    m = re.search(r'<a class="ws-pill l2auto" id="ws-l2auto"([^>]*)>', WB)
    assert m, "缺 #ws-l2auto 胶囊"
    attrs = m.group(1)
    assert 'onclick="return window.wsAutoPillClick&&window.wsAutoPillClick(event)"' in attrs
    assert 'href="/workspace"' in attrs, "href 保留给 CmdK 采集与无 JS 兜底"
    # 处理器：页内钩子优先；缺席才 __wsGoHome 去重 + wsNav 带 theme
    fn = _block(WB, "window.wsAutoPillClick=function(ev){", "function _l2AutoBump(dc){")
    assert "ev.preventDefault()" in fn
    assert "typeof window.wsFilterAutoConversations==='function'" in fn
    assert "window.wsFilterAutoConversations();" in fn
    assert "if(!(window.__wsGoHome&&window.__wsGoHome({}))) wsNav('/workspace');" in fn
    assert "location.href" not in fn


def test_plan_badge_short_label_and_override_in_tooltip():
    from jinja2 import Environment
    blk = _block(WB, "{% if plan_badge %}", "{% endif %}\n  <!-- 用户菜单")
    blk += "{% endif %}"
    env = Environment()
    i18n = {"mb_plan_flagship": "旗舰版", "mb_plan_flagship_override": "旗舰版（厂商自营）",
            "base.plan_badge_t": "当前授权档位 · 点击进入会员中心",
            "base.plan_badge_ro_t": "当前系统授权档位"}
    pb = {"plan": "flagship", "label_key": "mb_plan_flagship_override", "source": "override"}
    for role in ("master", "agent"):
        out = env.from_string(blk).render(plan_badge=pb, i18n=i18n, user_role=role)
        lb = re.search(r'<span class="ws-plan-lb">([^<]*)</span>', out).group(1)
        assert lb == "旗舰版", lb                               # D-M8：正文改回短档名
        assert "厂商自营" not in lb
        assert 'data-plan-full="旗舰版（厂商自营）"' in out          # override 措辞进悬浮
        assert re.search(r'title="旗舰版（厂商自营） · ', out)
        assert 'data-ui-icon="crown"' in out                       # 窄屏图标态
    # 无 override（真授权）：悬浮也只写「旗舰版」
    pb2 = {"plan": "flagship", "label_key": "mb_plan_flagship", "source": "license"}
    out2 = env.from_string(blk).render(plan_badge=pb2, i18n=i18n, user_role="master")
    assert 'data-plan-full="旗舰版"' in out2 and "厂商自营" not in out2


def test_topbar_shrink_rules_present():
    # 药丸组容器 + 三档收缩：≤1280 药丸组让位 / ≤1100 徽标文字→图标 / ≤1024 用户名收起
    assert '<span class="ws-pills" id="ws-pills">' in WB
    for pid in ("ws-sla", "ws-l4-badge", "ws-l2auto", "ws-followup", "ws-quota"):
        assert f'id="{pid}"' in _block(WB, '<span class="ws-pills" id="ws-pills">', "</span>\n  <!-- P24"), pid
    assert re.search(r"@media \(max-width:1280px\)\{ \.ws-pills\{min-width:0;overflow:hidden;flex-shrink:1;\} \}", WB)
    assert re.search(r"@media \(max-width:1100px\)\{ \.ws-plan-pill \.ws-plan-lb\{display:none;\} \.ws-plan-pill \.ws-plan-ic\{display:inline-flex;\}", WB)
    assert re.search(r"@media \(max-width:1024px\)\{ \.ws-user-name\{display:none;\} \}", WB)
    # 头像按钮最小宽度 + 永不收缩
    assert re.search(r"\.ws-user-btn\{[^}]*min-width:34px;", WB)
    assert re.search(r"\.ws-user-wrap\{position:relative;flex-shrink:0;\}", WB)
    # 主题开关在头像下拉（三档分段控件），顶栏不另设开关
    assert 'id="ws-theme-seg"' in WB
    assert WB.index('id="ws-user-menu"') < WB.index('id="ws-theme-seg"')


def test_pill_tooltip_keys_bilingual_and_say_filter():
    from src.web.i18n_packs.workspace_shell import EN, ZH
    for d in (ZH, EN):
        assert d["base.pill.l2auto_t"] and d["base.pill.l2auto_dyn"]
    assert "筛出全自动会话" in ZH["base.pill.l2auto_t"] and "打开收件箱" not in ZH["base.pill.l2auto_t"]
    assert "filter" in EN["base.pill.l2auto_t"].lower() and "open the inbox" not in EN["base.pill.l2auto_t"]
