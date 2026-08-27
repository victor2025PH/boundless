# -*- coding: utf-8 -*-
"""ops 总览 warn 卡「去处理」CTA 注册表门禁（2026-08-22 P2）。

背景：焦点档（只看重点）默认后，红黄灯卡被推到首屏，「所以我点哪」成为下一瓶颈。
``OPS_CARD_CTA`` 把可行动卡映射到处理页；本门禁钉三件事：

  1. 注册表里的每个 key 必须存在于 ``OPS_CARDS``（卡改名/删除时 CTA 不得变成孤儿——
     孤儿映射不会报错，只是按钮永远不出现＝静默失效，正是本仓最恨的缺陷形态）；
  2. 每个目标必须是站内绝对路径（以 / 开头，不带协议——防手滑写外链/相对路径）；
  3. 每个目标路径必须真的有页面路由（扫 src/web 的 @app.get 声明），防「点了 404」。

i18n 键 ``ov2_cta_fix``/``ov2_cta_fix_t`` 由 test_i18n_coverage 家族守双语齐备。
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TPL = ROOT / "src" / "web" / "templates" / "ops_overview.html"


def _template() -> str:
    return TPL.read_text(encoding="utf-8", errors="replace")


def _card_keys(src: str) -> set:
    i = src.find("OPS_CARDS = [")
    assert i > 0, "OPS_CARDS 注册表没找到（结构变更需同步本门禁）"
    j = src.find("];", i)
    return set(re.findall(r"key:'([^']+)'", src[i:j]))


def _cta_map(src: str) -> dict:
    i = src.find("var OPS_CARD_CTA = {")
    assert i > 0, "OPS_CARD_CTA 注册表没找到"
    j = src.find("};", i)
    return dict(re.findall(r"(\w+):\s*'([^']+)'", src[i:j]))


def _page_routes() -> set:
    routes: set = set()
    for f in (ROOT / "src" / "web").rglob("*.py"):
        s = f.read_text(encoding="utf-8", errors="replace")
        for m in re.finditer(r"@app\.get\(\"(/[^\"]*)\"", s):
            routes.add(m.group(1))
    return routes


def test_cta_keys_are_real_cards():
    src = _template()
    keys = _card_keys(src)
    cta = _cta_map(src)
    assert cta, "CTA 注册表为空＝功能被整体拆除，若是刻意请同步删本门禁"
    orphans = [k for k in cta if k not in keys]
    assert not orphans, f"CTA 映射指向不存在的卡 key（卡改名/删除后忘了同步）：{orphans}"


def test_cta_targets_are_internal_pages():
    cta = _cta_map(_template())
    bad = {k: v for k, v in cta.items()
           if not v.startswith("/") or v.startswith("//") or ":" in v}
    assert not bad, f"CTA 目标必须是站内绝对路径：{bad}"
    routes = _page_routes()
    missing = {k: v for k, v in cta.items() if v not in routes}
    assert not missing, f"CTA 目标路径没有对应页面路由（点了就是 404）：{missing}"
