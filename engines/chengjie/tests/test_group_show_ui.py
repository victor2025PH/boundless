# -*- coding: utf-8 -*-
"""群脉导播台（group_show）前端不变量门禁。

这一页的引导层全是**静态可判**的接线，而它们坏掉的样子都不报错、只是「悄悄没反应」：

- 步骤条是纯锚点 `href="#gs-sec-x"`。目标 id 一旦被改名/删掉，点了**什么都不会发生**——
  没有报错、没有红条，运营只会觉得「这按钮坏了」。所以锚点↔id 必须成对钉住。
- 悬浮词典的 `data-help="k"` 是**查 TERM_DICT 的键**（base.html `showTip` 里 `if(!d)return`）：
  键名写错 → 悬停静默无提示，肉眼与「本来就没提示」完全无法区分。
- `.gs-metrics` 曾被定义两次（grid 与 flex 同名同优先级），后者全局顶掉前者，自然度卡的
  三指标网格一直没按预期排。改名成 `.gs-nat-metrics` 修好后，必须防它再被合回同一个类名。
- 软广 chip 曾与 warn chip 几乎同色（都是琥珀）。软广是**强度**不是告警，本页 ok/warn/hard
  已占掉红/琥珀/绿的状态语义，强度色一旦漂回琥珀，运营会把「重软广」读成「报错」。
- 骨架屏容器带 `aria-busy="true"`：没有 JS 接手填充的话，读屏用户会被永远告知「还在加载」。

一律只做静态检查：不起浏览器、不连实例、不读库，CI 与本机同一个结论。
"""
import re
from pathlib import Path

from src.web.help_terms import HELP_TERMS
from src.web.i18n_packs.group_show import EN, ZH

_TPL = (Path(__file__).resolve().parents[1]
        / "src" / "web" / "templates" / "group_show.html")
_HTML = _TPL.read_text(encoding="utf-8")

_SCRIPT_RE = re.compile(r"<script\b.*?</script>", re.S | re.I)
_STYLE_RE = re.compile(r"<style\b.*?</style>", re.S | re.I)

_SCRIPT = "\n".join(_SCRIPT_RE.findall(_HTML))
_STYLE = "\n".join(_STYLE_RE.findall(_HTML))
_BODY = _STYLE_RE.sub("", _SCRIPT_RE.sub("", _HTML))

#: 有 section 锚点但**刻意**不在六步流程里的区块（演出矩阵属风控读数，不是上手步骤）。
#: 新增区块若也不进步骤条，必须登记在此并写明理由——否则默认按「忘了接导航」判红。
_ANCHORS_WITHOUT_STEP = {"gs-sec-exp"}


def _ids() -> set:
    return set(re.findall(r"""\bid\s*=\s*["']([^"']+)["']""", _HTML))


def test_stepper_anchors_have_real_targets():
    """每个 `href="#gs-sec-*"` 都要有同名 id：否则点了静默无反应。"""
    ids = _ids()
    dangling = sorted({a for a in re.findall(r'href="#(gs-sec-[^"]+)"', _HTML)
                       if a not in ids})
    assert not dangling, f"步骤条锚点指向不存在的区块（点了不会跳）: {dangling}"


def test_every_section_anchor_is_navigable():
    """反向钉：加了区块锚点却没接进步骤条，等于用户永远走不到它。"""
    linked = set(re.findall(r'href="#(gs-sec-[^"]+)"', _HTML))
    declared = {i for i in _ids() if i.startswith("gs-sec-")}
    orphan = sorted(declared - linked - _ANCHORS_WITHOUT_STEP)
    assert not orphan, (
        f"区块有锚点但步骤条里没有入口: {orphan}；"
        "确属刻意请登记进 _ANCHORS_WITHOUT_STEP 并写明理由")


def test_anchors_without_step_allowlist_not_stale():
    """白名单不许过期：登记的锚点必须仍然存在且仍未被接进导航。"""
    declared = {i for i in _ids() if i.startswith("gs-sec-")}
    linked = set(re.findall(r'href="#(gs-sec-[^"]+)"', _HTML))
    for a in sorted(_ANCHORS_WITHOUT_STEP):
        assert a in declared, f"{a} 已不存在，请从 _ANCHORS_WITHOUT_STEP 移除"
        assert a not in linked, f"{a} 已接进步骤条，请从 _ANCHORS_WITHOUT_STEP 移除"


def test_sticky_stepper_and_guide_targets_exist():
    """吸顶/记忆逻辑引用的 DOM 必须真的在页面上。"""
    ids = _ids()
    assert "gs-guide" in ids, "引导条 id 丢了 → gsInitGuide 静默失效（折叠记忆没了）"
    assert "gs-stepper" in _HTML, "步骤条容器类名丢了 → 吸顶与高亮全部静默失效"
    assert "function gsInitNav" in _SCRIPT
    assert "gs-stuck" in _STYLE, "吸顶紧凑态样式缺失 → 吸顶后步骤条会占掉半屏"


def test_naturalness_metrics_grid_not_collided_again():
    """`.gs-metrics` 只能有一份定义；自然度用独立类名，别再合回去。"""
    assert _STYLE.count(".gs-metrics{") == 1, (
        ".gs-metrics 又被定义了多次——同名同优先级会互相顶掉，"
        "自然度三指标网格会再次静默失效")
    assert ".gs-nat-metrics{" in _STYLE
    assert 'class="gs-nat-metrics"' in _SCRIPT, "自然度渲染没用独立类名，网格会被 flex 顶掉"


def test_soft_chip_reads_as_intensity_not_warning():
    """软广＝强度梯度，不能漂回琥珀（那是 warn 的语义）。"""
    base = [ln for ln in _STYLE.splitlines() if ".gs-chip.soft{" in ln]
    assert base, "找不到 .gs-chip.soft 基础样式"
    assert "amber" not in base[0], (
        "软广 chip 又用回琥珀色，会和 warn 撞语义——重软广会被读成报错")
    for tier in ("s1", "s2", "s3"):
        assert f".gs-chip.soft.{tier}{{" in _STYLE, f"软广分级 {tier} 缺样式"
    emitted = set(re.findall(r"return v >= \d+ \? ' (s\d)'", _SCRIPT))
    assert {"s3"} <= emitted or "s3" in _SCRIPT, "gsSoftTier 没有产出分级类名"


def test_template_gs_i18n_keys_defined_in_both_langs():
    """模板里 `(i18n or {}).get('gs_*')` 的键必须 zh+en 齐备（英文用户不该看到裸键）。"""
    used = {k for k in re.findall(r"\(i18n or \{\}\)\.get\(\s*'([^']+)'", _HTML)
            if k.startswith("gs_")}
    assert used, "没扫到任何 gs_ 词条，正则可能失效了"
    assert not sorted(used - set(ZH)), f"缺 zh 词条: {sorted(used - set(ZH))}"
    assert not sorted(used - set(EN)), f"缺 en 词条: {sorted(used - set(EN))}"


def test_data_help_keys_resolve_in_help_terms():
    """`data-help` 是词典键：键名写错 → 悬停静默无提示，和「没做提示」无法区分。"""
    keys = sorted(set(re.findall(r'data-help="([^"]+)"', _HTML)))
    assert keys, "导播台一个 data-help 都没有，悬浮解释接线丢了"
    missing = [k for k in keys if k not in HELP_TERMS]
    assert not missing, f"data-help 指向不存在的词条（悬停无反应）: {missing}"


def test_help_terms_for_this_page_are_bilingual():
    """本页词条必须 zh/en/desc/desc_en 齐备，否则英文界面悬停出中文或空白。"""
    bad = []
    for k in sorted(k for k in HELP_TERMS if k.startswith("gs_")):
        d = HELP_TERMS[k]
        if not (d.get("zh") and d.get("en") and d.get("desc") and d.get("desc_en")):
            bad.append(k)
    assert not bad, f"导播台词条字段不齐: {bad}"


def test_skeleton_containers_are_js_managed_and_cleared():
    """带 aria-busy 的骨架容器必须有 JS 接手，且有摘掉 busy 的落幕路径。"""
    assert "function gsBusyOff" in _SCRIPT, "骨架屏没有落幕函数，读屏会一直说「加载中」"
    busy_ids = sorted(set(re.findall(
        r'id="([^"]+)"[^>]*aria-busy="true"', _BODY)))
    assert busy_ids, "没扫到骨架容器，骨架屏接线可能被删了"
    for i in busy_ids:
        assert f"'{i}'" in _SCRIPT or f'"{i}"' in _SCRIPT, (
            f"{i} 挂了 aria-busy 但脚本里从没提到它 → 永远停在加载态")
    assert _SCRIPT.count("gsBusyOff(") >= len(busy_ids), (
        "gsBusyOff 调用次数少于骨架容器数，可能有容器停在加载态")


def test_wide_tables_wrapped_for_narrow_screens():
    """静态宽表都要套横向滚动容器，否则窄屏撑破页面。"""
    assert ".gs-tw{" in _STYLE, "缺 .gs-tw 滚动容器样式"
    tables = _BODY.count('<table class="gs-table')
    wrappers = _BODY.count('class="gs-tw"')
    assert wrappers >= tables, (
        f"有 {tables} 张静态宽表但只有 {wrappers} 个滚动容器，窄屏会溢出")
