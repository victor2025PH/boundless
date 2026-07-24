"""3-tier persona resolution tests.

Covers:
- get_persona: account-profile > chat-binding > domain-default
  （2026-07-24 双号串话修复：账号已绑人设时优先账号人设，跳过 peer-global
  chat_binding；无账号人设时 chat_binding 仍生效）
- load_profiles_from_config: reads personas.profiles[].id
- get_persona_by_id / upsert_profile / delete_profile
- get_all_chat_bindings returns full dicts (bug-fix regression)
- format_persona_block & build_system_prompt respect account_persona_id
- get_persona_name with account_persona_id
"""
import copy
import pytest
from src.utils.persona_manager import PersonaManager


def _pm() -> PersonaManager:
    PersonaManager.reset()
    return PersonaManager.get_instance()


# ── Profile store ─────────────────────────────────────────────

def test_load_profiles_from_config_basic():
    pm = _pm()
    cfg = {
        "personas": {
            "profiles": [
                {"id": "warm", "name": "温柔版", "role": "伴侣"},
                {"id": "pro",  "name": "专业版", "role": "助手"},
            ]
        }
    }
    n = pm.load_profiles_from_config(cfg)
    assert n == 2
    assert pm.get_persona_by_id("warm") == {"id": "warm", "name": "温柔版", "role": "伴侣"}
    assert pm.get_persona_by_id("pro")  == {"id": "pro",  "name": "专业版", "role": "助手"}


def test_load_profiles_skips_missing_id():
    pm = _pm()
    cfg = {"personas": {"profiles": [{"name": "no-id"}, {"id": "", "name": "empty-id"}]}}
    n = pm.load_profiles_from_config(cfg)
    assert n == 0
    assert pm.list_profile_ids() == []


def test_load_profiles_safe_when_no_personas_key():
    pm = _pm()
    assert pm.load_profiles_from_config({}) == 0
    assert pm.load_profiles_from_config({"personas": {}}) == 0
    assert pm.load_profiles_from_config({"personas": {"profiles": []}}) == 0


def test_upsert_and_delete_profile():
    pm = _pm()
    pm.upsert_profile("x", {"id": "x", "name": "X"})
    assert pm.get_persona_by_id("x") == {"id": "x", "name": "X"}
    assert pm.delete_profile("x") is True
    assert pm.get_persona_by_id("x") is None
    assert pm.delete_profile("x") is False  # already gone


def test_list_profile_ids():
    pm = _pm()
    pm.upsert_profile("a", {"name": "A"})
    pm.upsert_profile("b", {"name": "B"})
    assert set(pm.list_profile_ids()) == {"a", "b"}


# ── 3-tier get_persona ────────────────────────────────────────

def test_tier1_account_profile_takes_priority():
    """2026-07-24 双号串话修复：账号已绑人设 → 优先账号人设（跳过 chat_binding）。

    同一客户找 Katie/Jason 两号时，旧「chat_binding 最高」会强制两号共用
    同一 peer-global 绑定 → 女号说男声、内容互串。
    """
    pm = _pm()
    pm.set_domain_persona({"name": "Domain"})
    pm.upsert_profile("acc_p", {"name": "Account"})
    pm.bind_chat_persona("chat99", {"name": "ChatSpecific"})
    p = pm.get_persona("chat99", "acc_p")
    assert p["name"] == "Account"
    # 无账号人设（单号运营绑会话）→ chat_binding 仍最高优先
    assert pm.get_persona("chat99")["name"] == "ChatSpecific"


def test_tier2_account_profile_used_when_no_chat_binding():
    pm = _pm()
    pm.set_domain_persona({"name": "Domain"})
    pm.upsert_profile("acc_p", {"name": "AccountPersona"})
    p = pm.get_persona("chat99", "acc_p")
    assert p["name"] == "AccountPersona"


def test_tier3_domain_fallback_when_no_chat_and_no_account_profile():
    pm = _pm()
    pm.set_domain_persona({"name": "Domain"})
    p = pm.get_persona("chat99", "nonexistent_profile")
    assert p["name"] == "Domain"


def test_tier3_domain_fallback_when_no_account_persona_id():
    pm = _pm()
    pm.set_domain_persona({"name": "Domain"})
    pm.upsert_profile("acc_p", {"name": "AccountPersona"})
    p = pm.get_persona("chat99")  # no account_persona_id
    assert p["name"] == "Domain"


def test_hardcoded_default_when_no_domain():
    pm = _pm()
    p = pm.get_persona("chat99", "")
    assert p["name"] == "Assistant"  # global hardcoded default


def test_account_profile_beats_chat_binding_when_both_exist():
    """双号串话修复：chat 绑定与账号 profile 并存 → 账号 profile 赢。"""
    pm = _pm()
    pm.bind_chat_persona("42", {"name": "ChatBound"})
    pm.upsert_profile("profile_a", {"name": "AccountLevel"})
    pm.set_domain_persona({"name": "DomainLevel"})
    assert pm.get_persona("42", "profile_a")["name"] == "AccountLevel"
    # 账号人设 id 悬空（profile 已删）→ 回落 chat_binding，不黑洞
    assert pm.get_persona("42", "missing_profile")["name"] == "ChatBound"


# ── get_all_chat_bindings returns dicts (bug-fix regression) ──

def test_get_all_chat_bindings_returns_full_dicts():
    pm = _pm()
    pm.bind_chat_persona("c1", {"id": "prof_a", "name": "Alice", "role": "companion"})
    pm.bind_chat_persona("c2", {"name": "Bob"})
    bindings = pm.get_all_chat_bindings()
    assert isinstance(bindings, dict)
    assert isinstance(bindings["c1"], dict), "value must be full persona dict"
    assert bindings["c1"]["name"] == "Alice"
    assert bindings["c1"]["id"] == "prof_a"
    assert bindings["c2"]["name"] == "Bob"


def test_get_all_chat_bindings_returns_copies():
    """Mutations to returned dict don't affect internal state."""
    pm = _pm()
    pm.bind_chat_persona("cx", {"name": "Original"})
    b = pm.get_all_chat_bindings()
    b["cx"]["name"] = "Mutated"
    assert pm.get_persona("cx")["name"] == "Original"


# ── format_persona_block / build_system_prompt with account_persona_id ──

def test_format_persona_block_uses_account_persona_id():
    pm = _pm()
    pm.set_domain_persona({"name": "DomainName"})
    pm.upsert_profile("voice_persona", {"name": "VoicePersonaName", "role": "R"})
    block = pm.format_persona_block("", account_persona_id="voice_persona", detail="full")
    assert "VoicePersonaName" in block
    assert "DomainName" not in block


def test_format_persona_block_account_id_beats_chat_binding():
    pm = _pm()
    pm.bind_chat_persona("chat7", {"name": "ChatName", "role": "R"})
    pm.upsert_profile("p", {"name": "ProfileName", "role": "R"})
    block = pm.format_persona_block("chat7", account_persona_id="p")
    assert "ProfileName" in block
    assert "ChatName" not in block
    # 无 account_persona_id → chat_binding 供块（单号路径不回归）
    block2 = pm.format_persona_block("chat7")
    assert "ChatName" in block2


def test_build_system_prompt_with_account_persona_id():
    pm = _pm()
    pm.set_domain_persona({"name": "DomainD"})
    pm.upsert_profile("acc", {"name": "AccName", "role": "Acc role"})
    prompt = pm.build_system_prompt(chat_id="", account_persona_id="acc")
    assert "AccName" in prompt


# ── get_persona_name ──────────────────────────────────────────

def test_get_persona_name_with_account_persona_id():
    pm = _pm()
    pm.set_domain_persona({"name": "DomainN"})
    pm.upsert_profile("p", {"name": "ProfileN"})
    assert pm.get_persona_name("", "p") == "ProfileN"
    assert pm.get_persona_name("", "") == "DomainN"
