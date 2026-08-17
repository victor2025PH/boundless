"""出站口头禅账本 → 多样性提示门禁（2026-08-02）。

不变量：
- 超限检测按**消息条数**计：笑词（哈哈家族）/句尾语气（～呀啦呢哦嘛）/
  重复开头（前 6 字符组 ≥head_limit）/场景词（调用方词表）/中文 bigram 兜底；
- 生产语料样例（连续「哈哈」、「呀～」收尾、「抹茶」复读、发图配文九连同款
  开头）必须被抓到；干净语料零误报；
- hint 文案含关键要素（次数/样本词/换笑法/换收尾/别提词/别用开头），
  en 版可用，空 overused 返回 ""；
- ``parse_variety_cfg`` 全默认值防御式解析（enabled 默认 False，本仓惯例）。
"""
from src.ai.reply_variety import (
    build_variety_hint,
    collect_overused,
    extract_persona_words,
    parse_variety_cfg,
)

# 生产实录风味语料（近 14 天：哈哈 327/1106、几乎每条 ～/呀/啦 收尾、
# 「翻到一张之前拍的给你看」×9、抹茶/咖啡/手冲高频）
PROD_WINDOW = [
    "哈哈真的假的呀～",
    "我今天又去那家抹茶店啦",
    "哈哈哈你也太逗了吧",
    "翻到一张之前拍的给你看",
    "抹茶拿铁yyds 哈哈",
    "翻到一张之前拍的给你看哦",
    "今天手冲咖啡有点苦呢",
    "哈哈 好呀好呀",
    "翻到一张之前拍的给你看呀",
    "刚做完瑜伽 累死啦～",
    "抹茶冰淇淋也好吃哦",
    "嗯嗯 明天见啦",
]


# ── collect_overused：各类超限 ──────────────────────────────────────────────
def test_laugh_family_counted_per_message():
    got = collect_overused(PROD_WINDOW)
    assert "laugh" in got
    # 哈哈×4 条（同条里多个笑词只算 1 条）
    assert got["laugh"]["count"] == 4
    assert got["laugh"]["sample"] == "哈哈"


def test_laugh_variants_hhh_lol():
    msgs = ["hhhh that's funny", "lol ok", "笑不出来", "lolll again"]
    got = collect_overused(msgs, laugh_limit=2)
    assert got.get("laugh", {}).get("count") == 3


def test_tail_particle_overuse():
    got = collect_overused(PROD_WINDOW)
    assert "tail" in got
    # 呀～/啦/呢/哦/呀/啦～/哦/啦 → 明显超默认 tail_limit=3
    assert got["tail"]["count"] >= 6
    assert got["tail"]["sample"] in "～~呀啦呢哦嘛哟喔"


def test_tail_allows_trailing_emoji_and_bang():
    msgs = ["好呀😊", "走啦！", "来嘛~", "平常收尾。"]
    got = collect_overused(msgs, tail_limit=3, laugh_limit=99,
                           head_limit=99, keyword_limit=99)
    assert got["tail"]["count"] == 3


def test_repeated_head_group():
    got = collect_overused(PROD_WINDOW)
    heads = {h["head"]: h["count"] for h in got.get("heads", [])}
    # 「翻到一张之前拍的给你看」×3 → 前 6 字符「翻到一张之前」成组
    assert heads.get("翻到一张之前") == 3


def test_scene_words_from_caller():
    got = collect_overused(PROD_WINDOW, scene_words=["抹茶", "咖啡", "便利店"])
    scene = {d["word"]: d["count"] for d in got.get("scene", [])}
    assert scene.get("抹茶") == 3          # 三条提到抹茶
    assert "便利店" not in scene           # 没出现的词不报


def test_bigram_fallback_without_scene_words():
    # 不给 scene_words → bigram 兜底也要能抓到「抹茶」家族复读
    got = collect_overused(PROD_WINDOW, keyword_limit=3)
    kws = [d["word"] for d in got.get("keywords", [])]
    assert any("抹茶" in w for w in kws)


def test_bigram_not_duplicating_reported_scene_words():
    got = collect_overused(PROD_WINDOW, scene_words=["抹茶"])
    kws = [d["word"] for d in got.get("keywords", [])]
    assert not any("抹茶" in w or w in "抹茶" for w in kws)


def test_clean_window_no_flags():
    msgs = ["今天去了美术馆", "晚上吃的泰国菜很辣", "周末打算爬山"]
    assert collect_overused(msgs) == {}


def test_empty_and_garbage_inputs():
    assert collect_overused([]) == {}
    assert collect_overused(["", "   ", None]) == {}


def test_limits_zero_disable_category():
    got = collect_overused(PROD_WINDOW, laugh_limit=0, tail_limit=0,
                           head_limit=0, scene_limit=0, keyword_limit=0)
    assert got == {}


# ── build_variety_hint ──────────────────────────────────────────────────────
def test_hint_contains_key_elements_zh():
    over = collect_overused(PROD_WINDOW, scene_words=["抹茶"])
    hint = build_variety_hint(over, lang="zh")
    assert "表达多样性" in hint and "硬约束" in hint
    assert "哈哈" in hint and "4 条" in hint          # 次数事实
    assert "收尾" in hint                              # 换收尾语气
    assert "抹茶" in hint and "不要再提到" in hint     # 列出的词
    assert "开头别再用" in hint and "翻到一张之前" in hint
    assert "机器人" in hint


def test_hint_english_variant():
    over = collect_overused(PROD_WINDOW, scene_words=["抹茶"])
    hint = build_variety_hint(over, lang="en")
    assert "Expression variety" in hint
    assert "laugh" in hint and "抹茶" in hint
    assert "bot" in hint


def test_hint_empty_overused_returns_empty():
    assert build_variety_hint({}) == ""
    assert build_variety_hint(None) == ""


def test_hint_max_items_caps_categories():
    over = collect_overused(PROD_WINDOW, scene_words=["抹茶"])
    # 4 个类别全超限 → max_items=1 只留最高优先的笑词项
    hint = build_variety_hint(over, lang="zh", max_items=1)
    assert "哈哈" in hint
    assert "收尾" not in hint and "开头" not in hint


# ── extract_persona_words ───────────────────────────────────────────────────
def test_extract_persona_words_from_tastes_and_scenes():
    persona = {
        "tastes": {"likes": ["抹茶拿铁", "手冲咖啡", "Barolo红酒"]},
        "selfie_scenes": ["beach promenade", "深夜便利店门口"],
    }
    words = extract_persona_words(persona)
    assert "抹茶拿铁" in words and "手冲咖啡" in words
    assert "红酒" in words
    assert any("便利店" in w for w in words)


def test_extract_persona_words_defensive():
    assert extract_persona_words(None) == []
    assert extract_persona_words({}) == []
    assert extract_persona_words({"tastes": "not-a-dict"}) == []


# ── parse_variety_cfg ───────────────────────────────────────────────────────
def test_cfg_defaults():
    cfg = parse_variety_cfg({})
    assert cfg["enabled"] is False       # 新子系统默认关（本仓惯例）
    assert cfg["window"] == 12
    assert cfg["max_items"] == 4
    assert cfg["laugh_limit"] == 2
    assert cfg["tail_limit"] == 3
    assert cfg["head_limit"] == 2
    assert cfg["scene_limit"] == 2
    assert cfg["keyword_limit"] == 3


def test_cfg_overrides_and_garbage():
    cfg = parse_variety_cfg({
        "ai": {"reply_variety": {
            "enabled": True, "window": 20, "laugh_limit": "not-int",
            "tail_limit": 5,
        }},
    })
    assert cfg["enabled"] is True
    assert cfg["window"] == 20
    assert cfg["laugh_limit"] == 2       # 坏值回默认
    assert cfg["tail_limit"] == 5
    # 非 dict 输入不抛
    assert parse_variety_cfg(None)["enabled"] is False
    assert parse_variety_cfg({"ai": "oops"})["window"] == 12
