# -*- coding: utf-8 -*-
"""业务助手装配清单一致性门禁（P1-2，2026-08-12）。

组件层单源早有门禁（test_copilot_shared_sync 双树字节一致），但「哪张卡、进哪个
tab、什么顺序、叫什么名」的**装配层**从没有单源——三套装配各自演化出实录漂移
（SOP 两端不同 tab、cp-draft 三个名字）。本门禁以 shared/copilot/panel-manifest.js
为唯一事实源，逐条对照两端 HTML 的真实装配：

1. 清单可解析（严格 JSON 字面量）且两树同步（文件集合由 sync 门禁兜底）；
2. 网页原生右栏（unified_inbox.html）装配序列 == 清单 web 投影（含 pinned 英雄卡）；
3. 统一 App（app.html，两棵树）装配序列 == 清单 app 投影（appCardId 别名归一）；
   → 任一端出现**未登记**的 data-cp-card 即红：新增卡片先登记清单（这里就是
     装配层的变更控制点；挂载新组件的同学看到本条红=补一行清单条目）；
4. 词典键真实存在：keyWeb 在服务端合并词典（zh+en）、keyApp 在 cp-i18n.js（zh+en）；
5. converged=true 的卡：两端 zh+en 标题显示值**逐字相等**（命名收敛的机器可查
   刻度；converged=false 是登记在案的待收敛债，pending 写明前置，防假收敛）；
6. hiddenBoth 卡（AI 对话分析）两端 section 都必须带 hidden——单边悄悄复活=漂移。
"""

import json
import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_MANIFEST_JS = _ROOT / "shared" / "copilot" / "panel-manifest.js"
_WEB_TPL = _ROOT / "src" / "web" / "templates" / "unified_inbox.html"
_APP_TREES = [
    _ROOT / "shared" / "copilot" / "app.html",
    _ROOT / "desktop" / "renderer" / "shared" / "copilot" / "app.html",
]
_CP_I18N = _ROOT / "shared" / "copilot" / "i18n" / "cp-i18n.js"

_MARK = re.compile(r'data-tab="([\w-]+)"|data-cp-card="([\w-]+)"')


def _manifest() -> dict:
    src = _MANIFEST_JS.read_text(encoding="utf-8")
    literal = src.split("var MANIFEST =", 1)[1].split("\n;", 1)[0]
    return json.loads(literal)


def _scan(region: str):
    """按出现顺序抽 (card_id, tab)：tab 取最近一次 data-tab 标记。"""
    out, tab = [], None
    for m in _MARK.finditer(region):
        if m.group(1):
            tab = m.group(1)
        else:
            out.append((m.group(2), tab))
    return out


def _web_assembly():
    html = _WEB_TPL.read_text(encoding="utf-8")
    side = html[html.index('id="info-sidebar"'):html.index('id="mob-plat-bar"')]
    body = side[side.index('id="ws-cp-body"'):]
    cards = _scan(body)
    if 'data-cp-card="hero"' in side[:side.index('id="ws-cp-body"')]:
        cards.insert(0, ("hero", "pinned"))
    return cards, side


def _app_assembly(path: Path):
    html = path.read_text(encoding="utf-8")
    region = html[html.index('id="cp-app"'):html.index('id="cp-acct-layer"')]
    return _scan(region), html


def test_manifest_parses_and_shape():
    m = _manifest()
    assert m.get("version") == 1
    assert [t["id"] for t in m["tabs"]] == ["reply", "customer", "tools"]
    ids = [c["id"] for c in m["cards"]]
    assert len(ids) == len(set(ids)), "清单卡片 id 重复"
    for c in m["cards"]:
        assert c["tab"] in ("reply", "customer", "tools", "pinned"), c["id"]
        assert c["surfaces"], c["id"]
        if not c.get("converged", False) and "app" in c["surfaces"] and "web" in c["surfaces"]:
            assert c.get("pending"), f"{c['id']}: converged=false 必须写明 pending 前置条件"


def test_web_assembly_matches_manifest():
    m = _manifest()
    expected = [(c["id"], c["tab"]) for c in m["cards"] if "web" in c["surfaces"]]
    actual, _side = _web_assembly()
    assert actual == expected, (
        "网页原生右栏装配 ≠ 清单 web 投影（新增卡先登记 shared/copilot/"
        f"panel-manifest.js；挪 tab/改顺序=先改清单）：\n  实际: {actual}\n  清单: {expected}")


@pytest.mark.parametrize("path", _APP_TREES, ids=lambda p: str(p.parent.parent.name))
def test_app_assembly_matches_manifest(path):
    m = _manifest()
    expected = [(c.get("appCardId", c["id"]), c["tab"])
                for c in m["cards"] if "app" in c["surfaces"]]
    actual, _html = _app_assembly(path)
    assert actual == expected, (
        "统一 App 装配 ≠ 清单 app 投影（新增卡先登记 shared/copilot/"
        f"panel-manifest.js）：\n  实际: {actual}\n  清单: {expected}\n  文件: {path}")


def _cp_i18n_values(key: str):
    """cp-i18n.js 里某键的 (zh, en)：reg(zh,en) 结构下首次出现=zh、第二次=en。"""
    src = _CP_I18N.read_text(encoding="utf-8")
    vals = re.findall(r'"%s":\s*"((?:[^"\\]|\\.)*)"' % re.escape(key), src)
    return [json.loads(f'"{v}"') for v in vals]


def _web_dicts():
    from src.web.web_i18n import get_translations
    return get_translations("zh"), get_translations("en")


def test_i18n_keys_exist_on_both_sides():
    m = _manifest()
    zh, en = _web_dicts()
    missing = []
    for item in m["tabs"] + m["cards"]:
        kw, ka = item.get("keyWeb"), item.get("keyApp")
        if kw and (kw not in zh or kw not in en):
            missing.append(f"web:{kw}")
        if ka:
            vals = _cp_i18n_values(ka)
            if len(vals) < 2:
                missing.append(f"app:{ka}（cp-i18n 需 zh+en 两处定义，现 {len(vals)}）")
    assert not missing, "清单引用的词典键缺失：\n  " + "\n  ".join(missing)


def test_converged_cards_have_identical_titles():
    """converged=true ＝「同物同名」已达成：两端 zh 与 en 显示值逐字相等。
    改任何一端的卡名而不同步另一端（或不改清单）→ 此处红。"""
    m = _manifest()
    by_id = {c["id"]: c for c in m["cards"]}
    zh, en = _web_dicts()
    diffs = []
    for c in m["cards"]:
        if not c.get("converged"):
            continue
        kw = c.get("keyWeb") or (by_id.get(c.get("webHome", ""), {}) or {}).get("keyWeb")
        ka = c.get("keyApp")
        if not kw or not ka:
            continue
        vals = _cp_i18n_values(ka)
        if len(vals) < 2:
            diffs.append(f"{c['id']}: cp-i18n 缺 {ka}")
            continue
        if zh.get(kw) != vals[0]:
            diffs.append(f"{c['id']}: zh 「{zh.get(kw)}」(web:{kw}) ≠ 「{vals[0]}」(app:{ka})")
        if en.get(kw) != vals[1]:
            diffs.append(f"{c['id']}: en 「{en.get(kw)}」(web:{kw}) ≠ 「{vals[1]}」(app:{ka})")
    assert not diffs, "converged 卡两端标题不一致（同物必须同名）：\n  " + "\n  ".join(diffs)


def test_hidden_both_cards_stay_hidden():
    m = _manifest()
    hidden_ids = [c for c in m["cards"] if c.get("hiddenBoth")]
    assert hidden_ids, "清单 hiddenBoth 集不应为空（AI 对话分析下线是运营决策）"
    _cards, side = _web_assembly()
    for c in hidden_ids:
        cid = c["id"]
        sec = re.search(r'<section[^>]*data-cp-card="%s"[^>]*>' % re.escape(cid), side)
        assert sec and " hidden" in sec.group(0), f"web {cid} 卡应保持 hidden（下线决策）"
        for path in _APP_TREES:
            _a, html = _app_assembly(path)
            sec2 = re.search(r'<section[^>]*data-cp-card="%s"[^>]*>' % re.escape(
                c.get("appCardId", cid)), html)
            assert sec2 and " hidden" in sec2.group(0), f"{path} {cid} 卡应保持 hidden"
