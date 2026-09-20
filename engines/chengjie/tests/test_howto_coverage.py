# -*- coding: utf-8 -*-
"""小智「使用帮助」页面覆盖率棘轮（实施74 P1，2026-08-27）。

## 为什么需要这道门禁

小智的核心定位之一是**解决软件的使用问题**，而承载它的是 how-to 语料
（`howto_pack._HOWTO`）——`help_terms` 是名词解释、`nav_schema` 是页面入口，
只有 how-to 回答「我该怎么做 X」。

2026-08-27 实测：`nav_schema.NAV_ITEMS` 有 **40** 个页面，其中 **34 个**没有任何
how-to 条目。用户在这些页面上问「这个怎么用」，小智答不上来——不是模型不行，
是语料里压根没有。

更要命的是这笔债**只会越欠越多**：加一个新页面不会有任何机制提醒「配一条
how-to」，于是每次上新功能，覆盖率就自动下降一点。本门禁把它变成**棘轮**：
缺口只允许减少，不允许增加。新增页面要么同批配 how-to，要么显式抬 ceiling
并说明理由（后者会留在 diff 里被看见）。

## 为什么是棘轮而不是硬要求

硬要求（gap 必须为 0）会在落地当天就红，然后被人加豁免清单绕过——那等于没有。
棘轮承认历史债、只堵住增量，与仓里 `_ROUTE_CJK_CEILINGS`、inline-color ratchet
同族。
"""
from __future__ import annotations

from typing import Dict, Set, Tuple

# 当前缺口天花板：**只降不升**。补完一批 how-to 就把这个数字调低（并在 PR 里
# 让 diff 说话）。2026-08-27 基线 34 → 第一批补 12 条后 23 → 第二批补 13 条后 10。
HOWTO_GAP_CEILING = 10


def _nav_paths() -> Dict[str, str]:
    """{path: 中文标签}——只取有 path 的侧栏菜单项（命令面板额外项不计）。"""
    from src.web.nav_schema import NAV_ITEMS

    out: Dict[str, str] = {}
    for name, item in NAV_ITEMS.items():
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or "").strip()
        if path:
            out.setdefault(path, str(item.get("label_zh") or name))
    return out


def _howto_paths() -> Set[str]:
    from src.assistant.howto_pack import _HOWTO

    return {str(t[6]).strip() for t in _HOWTO if str(t[6]).strip()}


def _gap() -> Tuple[Dict[str, str], Set[str]]:
    nav = _nav_paths()
    covered = _howto_paths()
    return nav, {p for p in nav if p not in covered}


def test_howto_page_gap_not_above_ceiling():
    """没有操作指引的页面数不得超过天花板（棘轮：只降不升）。"""
    nav, gap = _gap()
    assert len(gap) <= HOWTO_GAP_CEILING, (
        f"没有 how-to 的页面 {len(gap)} 个 > 天花板 {HOWTO_GAP_CEILING}——"
        "新增页面请同批往 src/assistant/howto_pack.py 配一条操作指引"
        "（内容红线：必须是产品当前真实行为，先读代码/模板核实再写）。\n"
        + "\n".join(f"  {p}  {nav[p]}" for p in sorted(gap))
    )


def test_ceiling_is_not_stale():
    """天花板不得明显高于实际缺口——否则棘轮松掉，形同虚设。

    容差 0：补完就该顺手调低。这条红了说明「你补了 how-to 却忘了收紧棘轮」，
    修法是把 HOWTO_GAP_CEILING 改成当前实际值。
    """
    _nav, gap = _gap()
    assert len(gap) == HOWTO_GAP_CEILING, (
        f"实际缺口 {len(gap)} != 天花板 {HOWTO_GAP_CEILING}——"
        f"补完 how-to 请把 HOWTO_GAP_CEILING 调成 {len(gap)}（棘轮要收紧）"
    )


def test_howto_paths_point_at_real_pages():
    """how-to 的 path 必须指向真实页面（防打错字导致「带我去」跳空）。

    刻意放行**子页面**（如 /workspace/drafts）——它们不在侧栏但确实存在；
    判据＝以某个 nav 路径为前缀，或本身就是 nav 路径。
    """
    nav = set(_nav_paths())
    # 已核实存在、但不在侧栏的子页面白名单（新增前请先确认页面真的能打开）
    known_subpages = {"/dashboard", "/workspace/drafts"}
    bad = []
    for p in sorted(_howto_paths()):
        if p in nav or p in known_subpages:
            continue
        if any(p.startswith(n.rstrip("/") + "/") for n in nav if n != "/"):
            continue
        bad.append(p)
    assert not bad, (
        f"how-to 的 path 指向了未知页面：{bad}——"
        "「带我去」会跳到不存在的地址；请核对 nav_schema 或加进 known_subpages"
    )


def test_howto_entries_are_wellformed():
    """每条 how-to 都要有中英标题正文与关键词，slug 唯一。

    裸缺英文＝英文用户看到空白；缺关键词＝BM25 检索基本命不中，条目等于白写。
    """
    from src.assistant.howto_pack import _HOWTO

    slugs = [t[0] for t in _HOWTO]
    dup = {s for s in slugs if slugs.count(s) > 1}
    assert not dup, f"how-to slug 重复：{sorted(dup)}（id 冲突会互相覆盖）"

    for slug, title, title_en, content, content_en, keywords, _path in _HOWTO:
        for field, val in (("title", title), ("title_en", title_en),
                           ("content", content), ("content_en", content_en),
                           ("keywords", keywords)):
            assert str(val or "").strip(), f"how-to {slug!r} 的 {field} 为空"
        assert len(content) >= 20, f"how-to {slug!r} 正文过短，答不了实际问题"


def test_howto_never_leaks_the_developer_password():
    """帮助语料**不得**包含开发者工具的二次密码。

    背景（2026-08-27 写 `/developer` 条目时的真实取舍）：核实页面行为时会读到
    `developer_page_routes._DEV_PASSWORD` 的明文。写帮助最"顺手"的做法就是
    告诉用户密码是什么——但 how-to 语料是**给所有登录用户看的**（坐席、只读
    观察员都能问小智），而这道门恰恰是用来把他们挡在 API Key / Bot 行为 /
    全站显隐之外的。写进去等于把门拆了。

    正确写法＝「需要时请向技术支持索取」。本门禁从常量读真值来比对，
    所以将来改了密码它自动跟随，不会因为硬编码而失效。
    """
    from src.assistant.howto_pack import _HOWTO

    try:
        from src.web.routes.developer_page_routes import _DEV_PASSWORD
    except Exception:  # 模块重构/改名 → 跳过而不是假绿
        import pytest

        pytest.skip("developer_page_routes._DEV_PASSWORD 不可导入")

    pw = str(_DEV_PASSWORD or "").strip()
    if not pw:
        return
    for slug, title, title_en, content, content_en, keywords, _p in _HOWTO:
        blob = " ".join((title, title_en, content, content_en, keywords))
        assert pw not in blob, (
            f"how-to {slug!r} 泄露了开发者密码——帮助语料对所有登录用户可见，"
            "写「向技术支持索取」，不要写密码本身"
        )


def test_channel_howtos_do_not_claim_onboarding_on_channel_page():
    """渠道页 how-to **不得**说「在渠道页扫码接入账号」。

    2026-08-27 核实实况：四个 `/workspace/channels/*` 页共用「真机矩阵」壳，
    **本身不负责接入**，统一引导到坐席工作台的账号抽屉
    （`/workspace?drawer=1&connect={channel}`）。写反了就是把用户指到一个
    没有该功能的页面——助手编造功能是本语料包的一票否决项。
    """
    from src.assistant.howto_pack import _HOWTO

    for slug, _t, _te, content, _ce, _kw, path in _HOWTO:
        if not path.startswith("/workspace/channels/"):
            continue
        assert ("账号抽屉" in content or "工作台" in content), (
            f"渠道 how-to {slug!r} 未指向工作台账号抽屉——"
            "渠道页本身不接入账号，指错地方比不写更糟"
        )
