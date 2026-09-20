"""双协议号同 peer 隔离：ContextStore 键 + 账号人设优先于 peer chat_binding。"""

from src.ai.spoken_variant import stash_spoken_variant, take_spoken_variant
from src.utils.context_store import make_context_key
from src.utils.persona_manager import PersonaManager


def test_make_context_key_scopes_by_account():
    assert make_context_key("5433982810", "") == "5433982810"
    assert make_context_key("5433982810", "default") == "5433982810"
    assert make_context_key("5433982810", "8244899900") == "8244899900:5433982810"
    assert make_context_key("5433982810", "8755679833") == "8755679833:5433982810"
    assert (
        make_context_key("5433982810", "8244899900")
        != make_context_key("5433982810", "8755679833")
    )


def test_spoken_variant_scoped_by_account():
    """同书面文双号各暂存各的口语版，互不 take 走。"""
    written = "今天天气不错"
    stash_spoken_variant(written, "今天天气真不错嘿", scope="8244899900")
    stash_spoken_variant(written, "今天天气挺好啊", scope="8755679833")
    assert take_spoken_variant(written, scope="8244899900") == "今天天气真不错嘿"
    assert take_spoken_variant(written, scope="8755679833") == "今天天气挺好啊"
    assert take_spoken_variant(written, scope="8244899900") is None


def test_account_persona_beats_peer_global_chat_binding():
    """Katie=lin_xiaoyu 不得被 peer→chen_mo 全局绑定盖成男声。"""
    PersonaManager.reset()
    pm = PersonaManager.get_instance()
    pm.upsert_profile(
        "lin_xiaoyu",
        {"id": "lin_xiaoyu", "name": "林小雨", "gender": "female"},
        _track_history=False,
    )
    pm.upsert_profile(
        "chen_mo",
        {"id": "chen_mo", "name": "陈默（阿默）", "gender": "male"},
        _track_history=False,
    )
    pm.bind_chat_persona_by_profile_id("5433982810", "chen_mo")

    p_acct, tier_acct = pm.get_persona_with_tier(
        "5433982810", account_persona_id="lin_xiaoyu")
    assert tier_acct == "account_profile"
    assert p_acct["id"] == "lin_xiaoyu"

    p_bare, tier_bare = pm.get_persona_with_tier("5433982810", account_persona_id="")
    assert tier_bare == "chat_binding"
    assert p_bare["id"] == "chen_mo"

    PersonaManager.reset()
