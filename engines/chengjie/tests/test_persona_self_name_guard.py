"""错误自称名守卫门禁（2026-08-08「David Lin」事故回归网）。

实录：客户（显示名 David）质疑「Look like ai」，AI 答「I'm just David Lin, a real
guy…」——借对方名+自己姓氏缝合成新身份；3 分钟后被问「Who are you?」改口真名
Lin Xiaoyu，当场穿帮。本文件钉住：
  - 事故原话必须被 hard 拦截并可安全剥离；
  - 正确自称（真名/西名/拼音罗马化/含名昵称）绝不误伤；
  - 「我是认真的 / David's friend / 你以为我是大卫」等日常句零误报；
  - 分级判罚：置信不足只进 soft 观测，不动文本。
"""

from __future__ import annotations

import pytest

from src.utils.persona_guard import (
    build_self_name_allowlist,
    find_vocative_self_name,
    find_wrong_self_name,
    sanitize_self_name,
    strip_vocative_self_name,
)

_ALLOWED_CN = ["林小语"]
_ALLOWED_FULL = ["林小语", "Lin Xiaoyu", "Xiaoyu"]

try:
    import pypinyin  # noqa: F401
    _HAS_PINYIN = True
except ImportError:
    _HAS_PINYIN = False


# ── 事故原话回归 ────────────────────────────────────────────────────────────────

_INCIDENT_REPLY = (
    "Haha, I get that a lot—but nah, I'm just David Lin, a real guy who likes "
    "his messages short and sweet. What makes me sound like a robot to you? 😄"
)


def test_incident_borrowed_peer_name_is_hard_hit():
    hard, _soft = find_wrong_self_name(
        _INCIDENT_REPLY, _ALLOWED_FULL, peer_names=["David"])
    assert hard, "借对方名缝合自称必须 hard 命中"
    assert any("David" in h for h in hard)


def test_incident_reply_sanitized_keeps_rest():
    cleaned, hard, _ = sanitize_self_name(
        _INCIDENT_REPLY, _ALLOWED_FULL, peer_names=["David"])
    assert hard
    assert "David Lin" not in cleaned
    assert "robot" in cleaned, "非违规句必须保留"
    assert cleaned.strip(), "绝不返回空"


def test_incident_correct_self_intro_untouched():
    """18:45 的正确回答（真名罗马化）绝不能被拦——拦它=守卫杀死名字锁定。"""
    reply = ("Haha, I'm Lin Xiaoyu—your friendly neighborhood college student "
             "who's currently surviving on bubble tea 😄")
    cleaned, hard, _ = sanitize_self_name(
        reply, _ALLOWED_FULL, peer_names=["David"])
    assert not hard
    assert cleaned == reply


@pytest.mark.skipif(not _HAS_PINYIN, reason="pypinyin 未安装")
def test_pinyin_variants_admit_romanized_name_without_manual_alias():
    """人设只有中文名时，拼音变体自动放行「I'm Lin Xiaoyu」。"""
    allowed = build_self_name_allowlist({"name": "林小语"})
    norms = {a.replace(" ", "").lower() for a in allowed}
    assert "linxiaoyu" in norms and "xiaoyu" in norms
    reply = "I'm Lin Xiaoyu, nice to meet you!"
    _, hard, soft = sanitize_self_name(reply, allowed, peer_names=["David"])
    assert not hard and not soft


def test_allowlist_collects_western_names_fields():
    persona = {
        "name": "林小语",
        "names": {"full_western": "Sophie Lin", "english": "Sophie",
                  "nickname": "小语儿"},
    }
    allowed = build_self_name_allowlist(persona, ["林小语"])
    joined = " | ".join(allowed)
    assert "Sophie Lin" in joined and "小语儿" in joined
    hard, soft = find_wrong_self_name(
        "I'm Sophie, remember?", allowed, peer_names=["David"])
    assert not hard and not soft


# ── 借名/编名分级判罚 ───────────────────────────────────────────────────────────

def test_cjk_borrowed_peer_name_weak_pattern_is_hard():
    hard, _ = find_wrong_self_name("哈哈我是大卫呀", _ALLOWED_CN, peer_names=["大卫"])
    assert hard == ["大卫"]


def test_cjk_strong_pattern_unknown_name_is_hard():
    """「我叫〈非白名单名〉」＝确定性身份错误，无需 peer 锚点。"""
    hard, _ = find_wrong_self_name("我叫大卫，很高兴认识你", _ALLOWED_CN)
    assert hard == ["大卫"]


def test_latin_full_name_hard_only_when_allowlist_has_latin():
    """白名单没有拉丁变体（无西名+无拼音）时，拉丁全名只进 soft——
    防「罗马化正确自称」在白名单不完备的部署被误剥。"""
    hard, soft = find_wrong_self_name(
        "I'm Sarah Chen, by the way", _ALLOWED_CN, peer_names=[])
    assert not hard
    assert soft == ["Sarah Chen"]
    hard2, soft2 = find_wrong_self_name(
        "I'm Sarah Chen, by the way", _ALLOWED_FULL, peer_names=[])
    assert hard2 == ["Sarah Chen"] and not soft2


def test_single_token_unknown_latin_is_soft_only():
    """「I'm Batman」这类单词自称置信不足：只观测不动文本。"""
    reply = "Haha I'm Batman tonight 😎"
    cleaned, hard, soft = sanitize_self_name(reply, _ALLOWED_FULL)
    assert not hard and soft == ["Batman"]
    assert cleaned == reply


# ── 误伤反例网（全部必须零命中） ─────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "我是认真的，不骗你",
    "我是女生呀",
    "我是大卫的朋友",              # 关系描述不是自称
    "别叫我大卫，我不喜欢这个称呼",  # 否定/拒绝
    "我不是大卫",
    "你以为我是大卫吗？",          # 感知语境
    "I'm not David, silly",
    "I'm David's friend, we met last week",
    "You think I'm David? haha",
    "I'm Just Kidding around",     # 停用词 token
    "I'm So Sorry about that",
    "I'm fine, thanks!",
])
def test_no_false_positive(text):
    hard, _soft = find_wrong_self_name(
        text, _ALLOWED_FULL, peer_names=["David", "大卫"])
    assert hard == [], f"误伤: {text!r} -> {hard}"


@pytest.mark.parametrize("text", [
    "叫我小语就好啦",
    "我叫林小语，这都要问～",
    "call me Xiaoyu",
])
def test_own_name_variants_allowed(text):
    hard, soft = find_wrong_self_name(
        text, _ALLOWED_FULL, peer_names=["David"])
    assert hard == [] and soft == [], f"自家名被误报: {text!r}"


def test_never_returns_empty_even_if_whole_reply_violates():
    cleaned, hard, _ = sanitize_self_name("我叫大卫", _ALLOWED_CN, ["大卫"])
    assert hard
    assert cleaned.strip(), "整段违规也绝不返回空"
    assert "大卫" not in cleaned or cleaned == "我叫大卫"


def test_pure_function_never_raises_on_garbage():
    for bad in (None, "", 123, "我叫", "I'm "):
        cleaned, hard, soft = sanitize_self_name(str(bad or ""), None, None)
        assert isinstance(hard, list) and isinstance(soft, list)


# ── B74 金标（实施67，`_352` v1.054 实录「叫我。朋友都这么喊我。」）──────────────
#
# 场景：自建人设（张浩然）绑定断链/白名单不完备时，AI 正当自报家门「叫我浩然，
# 朋友都这么喊我」被 hard 档②（非白名单 CJK 强自称）命中；单句剥光后旧 inline
# 抹名兜底把名字抹空 → 「叫我，朋友都这么喊我。」残句 100% 穿帮。
# 修后不变量：非 borrowed 的 hard 剥光时保留原文（宁可漏拦不残剥）；
# borrowed（David Lin 借名缝合）的 inline 抹名兜底行为不回退。

_B74_REPLY = "叫我浩然，朋友都这么喊我。"


def test_b74_self_intro_unlisted_name_never_leaves_stub():
    """`_352` 金标：白名单没有人设名时，自介句绝不能被抹成「叫我。」残句。"""
    cleaned, hard, _soft = sanitize_self_name(
        _B74_REPLY, ["顾嘉"], peer_names=["Steven"])
    assert hard, "非白名单 CJK 强自称仍须 hard 命中（观测语义保留）"
    assert cleaned == _B74_REPLY, "非 borrowed 剥光→保留原文，绝不残剥"
    assert "叫我，" not in cleaned and "叫我。" not in cleaned


def test_b74_multi_sentence_still_strips_whole_sentence():
    """多句时按句剥依旧成立（防真幻觉名）：违规句整句掉，好句保留。"""
    text = "叫我浩然。今天天气不错，出去走走吧。"
    cleaned, hard, _ = sanitize_self_name(text, ["顾嘉"], peer_names=["Steven"])
    assert hard
    assert "浩然" not in cleaned
    assert "天气不错" in cleaned


def test_b74_resolved_name_in_allowlist_not_flagged():
    """白名单同源修复面：生成时 resolved name 进白名单后，自报家门不命中。"""
    cleaned, hard, _ = sanitize_self_name(
        _B74_REPLY, ["顾嘉", "张浩然"], peer_names=["Steven"])
    assert not hard, "白名单含人设名（含去姓变体）→ 「叫我浩然」是合法自称"
    assert cleaned == _B74_REPLY


def test_b74_borrowed_inline_fallback_not_regressed():
    """David Lin 金标不回退：borrowed（借对方名）单句剥光仍走 inline 抹名。"""
    text = "I'm just David Lin, a real guy."
    cleaned, hard, _ = sanitize_self_name(
        text, ["Lin Xiaoyu", "Xiaoyu"], peer_names=["David"])
    assert hard
    assert "David Lin" not in cleaned, "借名缝合必须被抹（残句两害相权取其轻）"
    assert cleaned.strip(), "绝不返回空"


def test_b74_english_self_intro_unlisted_kept_whole():
    """英文自介同理：call me + 非白名单名，单句剥光→保留原文不残剥。"""
    text = "Call me Rachel, that's what my friends say."
    cleaned, hard, _ = sanitize_self_name(
        text, ["Lin Xiaoyu"], peer_names=["Steven"])
    # 单词名按分级规则最多进 soft（拉丁全名档要求 ≥2 词），文本必须原样。
    assert cleaned == text
    assert "Call me ," not in cleaned


# ── prompt 侧「对方身份」声明（与守卫同一事故的源头缓解） ────────────────────────

def _cfg(domain: str = "conversion"):
    class _Cfg:
        config_path = None
        config = {"domain": domain, "web_admin": {"site_name": "T"}, "ai": {},
                  "inbox": {"reply_style": {"bubbles": {"enabled": False}}}}

        def get_ai_config(self):
            return {}

    return _Cfg()


def test_prompt_declares_peer_identity_in_companion_domain():
    from src.ai.ai_client import AIClient
    client = AIClient(_cfg())
    out = client._build_context_prompt({
        "channel": "telegram",
        "_resolved_persona_name": "林小语",
        "_peer_display_name": "David",
    })
    assert "对方身份" in out and "David" in out
    assert "对方的名字永远不是你的名字" in out


def test_prompt_peer_identity_absent_outside_companion():
    from src.ai.ai_client import AIClient
    client = AIClient(_cfg(domain="general"))
    out = client._build_context_prompt({
        "channel": "telegram",
        "_resolved_persona_name": "林小语",
        "_peer_display_name": "David",
    })
    assert "对方身份" not in out


# ── B42 称呼混淆守卫（2026-08-22 _236 实录「you're not that old, Steven」）────
#
# 镜像方向：AI 用**自己的**人设名呼叫客户（呼格）。上面的守卫抓「拿对方名自称」，
# 这组抓「拿自己名喊对方」——两个方向合起来才是完整的身份锚。

_VOC_INCIDENT = "Haha you're not that old, Steven! Age is just a number anyway."


def test_vocative_incident_hit_and_stripped():
    hits = find_vocative_self_name(_VOC_INCIDENT, ["Steven"], ["Nicks"])
    assert hits == ["Steven"]
    cleaned, hits2 = strip_vocative_self_name(_VOC_INCIDENT, ["Steven"], ["Nicks"])
    assert hits2 == ["Steven"]
    assert "Steven" not in cleaned
    # 句子本体保留（剥名不剥句——整句剥会把有效回复杀掉）
    assert "not that old" in cleaned and "Age is just a number" in cleaned


def test_vocative_leading_form_hit():
    cleaned, hits = strip_vocative_self_name(
        "Steven, 你怎么看这件事？", ["Steven"], ["尼克"])
    assert hits == ["Steven"]
    assert "Steven" not in cleaned and "你怎么看" in cleaned


def test_vocative_cjk_trailing_form_hit():
    cleaned, hits = strip_vocative_self_name(
        "你还年轻着呢，史蒂文。放宽心啦。", ["史蒂文"], ["尼克斯"])
    assert hits == ["史蒂文"]
    assert "史蒂文" not in cleaned and "你还年轻" in cleaned and "放宽心" in cleaned


def test_vocative_self_intro_not_flagged():
    # 自我介绍/报名字绝不误伤（含怪写法「name is, Steven」）
    assert find_vocative_self_name("I'm Steven, nice to meet you.",
                                   ["Steven"], ["Nicks"]) == []
    assert find_vocative_self_name("My name is, Steven.",
                                   ["Steven"], ["Nicks"]) == []
    assert find_vocative_self_name("我是史蒂文，很高兴认识你。",
                                   ["史蒂文"], ["尼克"]) == []


def test_vocative_peer_actually_named_same_skipped():
    # 客户真叫 Steven → 称呼合法，不拦
    assert find_vocative_self_name(_VOC_INCIDENT, ["Steven"], ["Steven"]) == []
    assert find_vocative_self_name(_VOC_INCIDENT, ["Steven"], ["Steven Wu"]) == []


def test_vocative_unknown_peer_now_judged():
    # #96（0830 Steven 四连报实锤）改判：对方名未知 → **照判**。
    # 旧「不判」语义让 B 线（无 peer 名管道）恰好裸奔；prompt 契约本就是
    # 「不知道对方叫什么就不用名字」，守卫按同一契约执行。
    assert find_vocative_self_name(_VOC_INCIDENT, ["Steven"], []) == ["Steven"]
    assert find_vocative_self_name(_VOC_INCIDENT, ["Steven"], None) == ["Steven"]


_VOC_INCIDENT_96 = (
    "That sunset was unreal, wish you were there to judge it with me, Steven."
)


def test_vocative_incident_96_stripped_without_peer():
    # #96 事故原句（原图 833）：B 线拿不到 peer 名也必须拦
    cleaned, hits = strip_vocative_self_name(_VOC_INCIDENT_96, ["Steven"], None)
    assert hits == ["Steven"]
    assert "Steven" not in cleaned
    assert "wish you were there" in cleaned


def test_vocative_peer_name_address_untouched():
    # 用对方的名字称呼对方＝正常行为
    assert find_vocative_self_name("Nice point, Nicks!", ["Steven"], ["Nicks"]) == []


def test_vocative_never_returns_empty():
    cleaned, hits = strip_vocative_self_name(", Steven", ["Steven"], ["Nicks"])
    assert hits and cleaned == ", Steven"   # 剥空回原文（调用方记日志）


def test_vocative_mention_mid_sentence_not_flagged():
    # 句中普通提及（非呼格）不拦：谈论自己名字是合法内容
    assert find_vocative_self_name(
        "Steven is my English name btw.", ["Steven"], ["Nicks"]) == []


def test_prompt_anchors_mirror_direction():
    """B42 prompt 侧：身份锚必须写死「绝不能用自己的名字称呼对方」镜像方向。"""
    from src.ai.ai_client import AIClient
    client = AIClient(_cfg())
    out = client._build_context_prompt({
        "channel": "telegram",
        "_resolved_persona_name": "林小语",
        "_peer_display_name": "David",
    })
    assert "绝不能用它称呼对方" in out or "绝不能用你自己的名字称呼对方" in out
    assert "对对方的称呼只能来自客户资料" in out
