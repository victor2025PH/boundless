# -*- coding: utf-8 -*-
"""收件箱左栏平台常驻清单 × 后端实现集 一致性门禁（2026-08-07 IG/Zalo 点亮沉淀）。

背景：Instagram/Zalo 的后端（webhook 入站镜像 / 官方出站 worker / 接入向导）2026-07
就完工了，但工作台左栏 ``FIXED_PLATS`` 一直只列 4 个老平台——「接了后端、面板不亮」
这种断层不报错、不变红，直到有人问「怎么看不到新软件的 logo」才被人肉发现。
本门禁把两边钉成同一事实源的两个投影：

1. 后端实现了账号形态的平台（``platform_readiness._IMPLEMENTED_MODES``）必须
   全部出现在 ``FIXED_PLATS``——新平台完工却忘改面板 → 红；
2. ``FIXED_PLATS`` 不得出现后端没实现的平台——图标库早备好了 X/Signal/WeChat/
   Viber 的 logo，提前摆上面板＝给用户一个点不通的入口 → 也红；
3. 常驻平台必须配齐 ``PLAT_DESC``（hover 卡与空态说明）、``PLAT_NOTE``（账号抽屉
   空态的接入方式一行小字）、``PC``/``PN``（品牌色/显示名）——缺任何一项都是
   「亮了但残缺」的静默缺陷（hover 空白 / 空态少说明 / 图标退灰色兜底）。

web 刻意不在清单内：它是服务端原生渠道（无账号登录形态，不在 _IMPLEMENTED_MODES），
左栏按「有账号或有会话」条件渲染，与常驻语义不同。
"""
from __future__ import annotations

import re
from pathlib import Path

from src.integrations.platform_readiness import _IMPLEMENTED_MODES

_TEMPLATE = (Path(__file__).resolve().parents[1]
             / "src" / "web" / "templates" / "unified_inbox.html")


def _template_text() -> str:
    return _TEMPLATE.read_text(encoding="utf-8")


def _fixed_plats(src: str) -> list:
    m = re.search(r"const FIXED_PLATS\s*=\s*\[([^\]]*)\]", src)
    assert m, "unified_inbox.html 里找不到 FIXED_PLATS 定义（被重命名/删除？）"
    return re.findall(r"'([a-z_]+)'", m.group(1))


def _js_object_keys(src: str, name: str) -> set:
    """提取 ``const <name> = {...}`` 字面量的键集合（单层，够本模板用）。"""
    m = re.search(r"const %s\s*=\s*\{(.*?)\};" % re.escape(name), src, re.S)
    assert m, f"unified_inbox.html 里找不到 {name} 定义"
    return set(re.findall(r"(?:^|[,{\s])([a-zA-Z_]\w*)\s*:", m.group(1)))


def test_fixed_plats_match_implemented_backends():
    """常驻清单 == 后端实现集（双向：漏亮 / 虚亮 都拦）。"""
    src = _template_text()
    fixed = _fixed_plats(src)
    implemented = {p for p, _mode in _IMPLEMENTED_MODES}
    missing = implemented - set(fixed)
    assert not missing, (
        f"后端已实现的平台没进左栏常驻清单（面板看不见）：{sorted(missing)}；"
        "改 unified_inbox.html 的 FIXED_PLATS 并补 PLAT_DESC/PLAT_NOTE/PC/PN")
    phantom = set(fixed) - implemented
    assert not phantom, (
        f"左栏常驻了后端未实现的平台（点不通的入口）：{sorted(phantom)}；"
        "先在 platform_readiness._IMPLEMENTED_MODES 登记真实实现再上面板")


def test_fixed_plats_have_complete_ui_metadata():
    """每个常驻平台必须配齐 hover 描述 / 接入说明 / 品牌色 / 显示名。"""
    src = _template_text()
    fixed = set(_fixed_plats(src))
    for table in ("PLAT_DESC", "PLAT_NOTE", "PC", "PN"):
        keys = _js_object_keys(src, table)
        gap = fixed - keys
        assert not gap, f"{table} 缺常驻平台条目：{sorted(gap)}"


def test_fixed_plats_order_stable():
    """老平台相对顺序不漂移（坐席肌肉记忆；新平台只许追加在尾部）。"""
    fixed = _fixed_plats(_template_text())
    legacy = [p for p in fixed if p in ("telegram", "line", "whatsapp", "messenger")]
    assert legacy == ["telegram", "line", "whatsapp", "messenger"], (
        f"老平台相对顺序变了：{legacy}（会打乱坐席肌肉记忆，新平台请追加尾部）")
