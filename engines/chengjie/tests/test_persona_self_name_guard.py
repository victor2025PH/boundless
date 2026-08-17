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
    find_wrong_self_name,
    sanitize_self_name,
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
