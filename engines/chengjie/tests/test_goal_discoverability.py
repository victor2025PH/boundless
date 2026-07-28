# -*- coding: utf-8 -*-
"""「工作目标」可发现性门禁（运营实录：坐席端的工作目标/工作计划按钮找不到）。

根因之一＝全站零登记：命令面板、悬浮词典、侧栏都搜不到「工作目标/工作计划/营销目标」，
而术语三分裂（坐席端=工作目标 / 配置与 ops=营销目标 / 运营口语=工作计划）让搜哪个都空。
策略是**不改名**、只让同义词搜得到，本文件守住这条策略的三个不变量：

1. 命令面板有一条深链 ``/workspace?card=goal``（面板专属项，不在侧栏重复占位），
   且在**简洁模式**可见——全角色默认档位是简洁模式，漏了 simple 标记等于对坐席隐身。
2. 按面板**真实的匹配逻辑**（``name`` + ``keys`` 子串，见 base.html renderCmdList），
   搜「工作目标」「工作计划」「营销目标」「goal」「milestone」都命中这一条。
3. 悬浮词典登记 6 条（三个同义词互相指认 + 今日拍/自治档/里程碑），中英齐备；
   且全部 nav 项的 ``help`` 都指向真实存在的词条（防悬空引用）。
"""

import pytest

from src.web.help_terms import HELP_TERMS
from src.web.nav_schema import (CMD_EXTRA_ITEMS, NAV_ICONS, NAV_ITEMS,
                                _cmd_items, get_nav_context)
from src.web.web_i18n import get_translations

# 另一条工作流在统一收件箱实现该 query param：自动切「客户&关系」tab + 展开目标卡
GOAL_DEEP_LINK = "/workspace?card=goal"

# cmd_keys 必含的说法（坐席端/ops/口语三套中文 + 英文）——逐个断言，
# 将来谁删一个都要单独红，不给「整串被改短」留静默空间。
REQUIRED_CMD_KEYS = (
    "工作目标", "工作计划", "营销目标", "目标", "计划", "推进", "里程碑", "今日拍",
    "goal", "goals", "plan", "milestone", "agenda",
)

# 用户可能敲进面板的说法 → 必须命中目标入口（真正的用户侧不变量）
USER_QUERIES = ("工作目标", "工作计划", "营销目标", "goal", "milestone")

# 新增的 6 条悬浮词条
GOAL_HELP_KEYS = ("work_goal", "work_plan", "marketing_goal",
                  "goal_beat_today", "goal_autonomy", "goal_milestone")


def _palette_rows(lang: str = "zh"):
    """复刻 base.html 的面板行：name = i18n(label_key) 回落 label_zh，keys = cmd_keys。"""
    i18n = get_translations(lang)
    rows = []
    for it in get_nav_context()["nav_cmd_items"]:
        rows.append({
            "name": i18n.get(it.get("label_key", ""), it.get("label_zh", "")),
            "url": it.get("path", ""),
            "keys": it.get("cmd_keys", "") or "",
            "simple": bool(it.get("simple")),
        })
    return rows


def _palette_search(q: str, lang: str = "zh", ui_mode: str = "simple"):
    """复刻 renderCmdList：先按 ui_mode 过滤 simple，再对 name/keys 做小写子串匹配。"""
    ql = q.lower()
    rows = [r for r in _palette_rows(lang) if r["simple"] or ui_mode != "simple"]
    return [r for r in rows
            if ql in r["name"].lower() or ql in r["keys"].lower()]


def _goal_row():
    hits = [r for r in _palette_rows() if r["url"] == GOAL_DEEP_LINK]
    assert len(hits) == 1, f"命令面板应有且仅有一条 {GOAL_DEEP_LINK} 入口，实得 {hits}"
    return hits[0]


# ── 1. 命令面板入口 ──────────────────────────────────────────────────────────
def test_goal_deep_link_registered_in_command_palette():
    """面板专属项（CMD_EXTRA_ITEMS）里有目标深链，且经 _cmd_items 流进 nav_cmd_items。"""
    item = CMD_EXTRA_ITEMS["work_goal"]
    assert item["path"] == GOAL_DEEP_LINK, "深链必须是 /workspace?card=goal（与收件箱侧参数约定一致）"
    assert item["icon"] in NAV_ICONS, "图标名须在 NAV_ICONS 里（否则渲染空 svg）"
    assert item.get("help"), "面板项应关联悬浮词条，点不进去时也能看懂是什么"

    paths = [d.get("path") for d in _cmd_items()]
    assert GOAL_DEEP_LINK in paths, "CMD_EXTRA_ITEMS 未流进命令面板项（_cmd_items 断线）"
    # 侧栏已有 /workspace 行，深链不得在侧栏重复占位
    ctx = get_nav_context()
    side = [it.get("path") for g in ctx["nav_groups"] for it in g["items"]
            if isinstance(it, dict)]
    side += [it.get("path") for it in ctx["nav_simple_core"] + ctx["nav_simple_more"]
             if isinstance(it, dict)]
    assert GOAL_DEEP_LINK not in side, "目标深链只进命令面板，不在侧栏重复一行 /workspace"


def test_goal_palette_label_bilingual():
    """label_key 中英齐备（面板 name 直显，缺英文会漏中文给英文用户）。"""
    key = CMD_EXTRA_ITEMS["work_goal"]["label_key"]
    for lang in ("zh", "en"):
        val = get_translations(lang).get(key)
        assert val, f"i18n[{lang}] 缺键 {key}"
        assert val != key, f"i18n[{lang}][{key}] 回落成裸键名"
    assert get_translations("zh")[key] == CMD_EXTRA_ITEMS["work_goal"]["label_zh"], \
        "label_zh 兜底文案应与 zh 词条一致（i18n 缺失时面板不该显示另一套说法）"


def test_goal_entry_visible_in_simple_mode():
    """全角色默认简洁模式 → 面板项必须带 simple 标记，否则坐席根本看不到（本次事故重现点）。"""
    assert _goal_row()["simple"] is True
    assert _palette_search("工作目标", ui_mode="simple"), "简洁模式下搜「工作目标」空结果"


@pytest.mark.parametrize("token", REQUIRED_CMD_KEYS)
def test_goal_cmd_keys_contain_every_synonym(token):
    assert token in _goal_row()["keys"], f"cmd_keys 缺同义词 {token!r}"


@pytest.mark.parametrize("query", USER_QUERIES)
@pytest.mark.parametrize("ui_mode", ("simple", "full"))
@pytest.mark.parametrize("lang", ("zh", "en"))
def test_palette_search_finds_goal_entry(query, ui_mode, lang):
    """真实匹配逻辑下搜任一说法都能到达目标卡（不只是「字段里有这个词」）。"""
    urls = [r["url"] for r in _palette_search(query, lang=lang, ui_mode=ui_mode)]
    assert GOAL_DEEP_LINK in urls, \
        f"{ui_mode}/{lang} 模式搜 {query!r} 未命中目标入口，实得 {urls}"


# ── 2. 悬浮词典 ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize("key", GOAL_HELP_KEYS)
def test_goal_help_terms_complete(key):
    """6 条词条中英齐备 + 带操作路径（test_i18n_coverage 只强制 en/desc_en，这里更严）。"""
    term = HELP_TERMS.get(key)
    assert term, f"HELP_TERMS 缺词条 {key}"
    for field in ("zh", "en", "desc", "desc_en", "usage", "usage_en"):
        assert str(term.get(field) or "").strip(), f"词条 {key} 的 {field} 为空"


def test_goal_help_terms_cross_reference_synonyms():
    """三个同义词条互相指认——搜到任一个都能知道另两个是同一个东西。"""
    zh_of = {k: HELP_TERMS[k]["zh"] for k in ("work_goal", "work_plan", "marketing_goal")}
    assert zh_of == {"work_goal": "工作目标", "work_plan": "工作计划",
                     "marketing_goal": "营销目标"}
    cross = {
        "work_goal": ("工作计划", "营销目标"),
        "work_plan": ("工作目标", "营销目标"),
        "marketing_goal": ("工作目标", "工作计划"),
    }
    for key, others in cross.items():
        desc = HELP_TERMS[key]["desc"]
        for other in others:
            assert other in desc, f"词条 {key} 的 desc 未提到同义词「{other}」"
    cross_en = {
        "work_goal": ("work plan", "marketing goal"),
        "work_plan": ("work goal", "marketing goal"),
        "marketing_goal": ("work goal", "work plan"),
    }
    for key, others in cross_en.items():
        desc_en = HELP_TERMS[key]["desc_en"].lower()
        for other in others:
            assert other in desc_en, f"词条 {key} 的 desc_en 未提到同义词 {other!r}"


def test_goal_help_terms_point_to_the_card():
    """usage 必须给出到卡的点击路径（「找不到」的直接解药）。"""
    for key in ("work_goal", "work_plan"):
        usage = HELP_TERMS[key]["usage"]
        assert "客户&关系" in usage and "工作目标" in usage, f"词条 {key} 的 usage 未给出点击路径"
    assert "工作目标" in HELP_TERMS["marketing_goal"]["usage"], \
        "营销目标词条应把人指到坐席端那张可操作的卡"


def test_goal_help_terms_share_onscreen_vocabulary():
    """词条 zh 与屏幕标签同词——matchTerm 是精确相等匹配，用词一漂移 tooltip 就静默脱钩。

    只断言「包含」而非全等：另一条工作流若给标题加括注（如「工作目标（工作计划）」）
    属合规改动，不该被本门禁误红；但换成另一套词汇（如改叫「销售目标」）必须红。
    """
    zh = get_translations("zh")
    for key, label_key in (("work_goal", "inbox.goal.title"),
                           ("goal_autonomy", "inbox.goal.form.autonomy"),
                           ("goal_beat_today", "ov2_goal_beats"),
                           ("goal_milestone", "ov2_goal_col_milestone")):
        label = zh.get(label_key, "")
        assert label, f"i18n 缺屏幕标签 {label_key}"
        assert HELP_TERMS[key]["zh"] in label, \
            f"词条 {key}（{HELP_TERMS[key]['zh']}）与屏幕标签 {label_key}（{label}）用词不一致"


def test_goal_help_terms_describe_real_behavior():
    """描述须落在真实语义上（自治档三档 / 推进力度三级 / 驳回降档），防写成想象功能。"""
    autonomy = HELP_TERMS["goal_autonomy"]["desc"]
    for level in ("只观察", "顺势建议", "自动推进"):
        assert level in autonomy, f"自治档词条缺档位「{level}」"
    beat = HELP_TERMS["goal_beat_today"]["desc"]
    for level in ("陪伴日", "顺势", "可直说"):
        assert level in beat, f"今日拍词条缺推进力度「{level}」"
    assert "驳回" in beat and "降档" in beat, "今日拍词条应说明驳回会回流降档"


# ── 3. 悬空引用护栏 ─────────────────────────────────────────────────────────
def test_no_dangling_help_reference_in_nav_schema():
    """所有 nav / 面板项的 help 都必须存在于 HELP_TERMS（写错 key = tooltip 静默不弹）。"""
    dangling = {}
    for name, table in (("NAV_ITEMS", NAV_ITEMS), ("CMD_EXTRA_ITEMS", CMD_EXTRA_ITEMS)):
        for item_id, item in table.items():
            help_key = item.get("help")
            if help_key and help_key not in HELP_TERMS:
                dangling[f"{name}.{item_id}"] = help_key
    assert not dangling, f"nav 项 help 指向不存在的词条: {dangling}"
