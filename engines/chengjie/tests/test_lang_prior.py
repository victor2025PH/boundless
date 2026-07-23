"""新会话初始语言先验（lang_prior）门禁。

场景（2026-07-23 「新 WA 好友第一句回日语/中文」事故的文本侧收尾）：
新好友第一条消息 "Hi"/emoji 语言中性 → 决策链落空到 default=zh，给
菲律宾/泰国客户回中文首句。lang_prior 在**全链落空时**按账号配置 /
WA 国码供给首句语言；任何真实语言证据/请求/历史都优先（先验≠覆写）。

覆盖：
  - 纯函数：phone_lang_hint（JID 兼容/群聊排除/国码最长前缀/覆写）、
    initial_lang_hint（开关/优先级链/平台白名单/坏配置软失败）。
  - 接线：skill_manager 3b default（真实 SkillManager 走 process_message，
    与 test_lang_e2e_acceptance 同款最小基建）；persona_reply 的
    _initial_lang_default（conversation_id 解析 account）。
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
import yaml

from src.ai.lang_prior import initial_lang_hint, phone_lang_hint
from src.hooks.registry import HookRegistry


# ── 纯函数：phone_lang_hint ─────────────────────────────────────


def test_phone_cc_basic_markets():
    assert phone_lang_hint("639270135480") == "en"      # 菲律宾
    assert phone_lang_hint("66812345678") == "th"       # 泰国
    assert phone_lang_hint("84912345678") == "vi"       # 越南
    assert phone_lang_hint("6281234567890") == "id"     # 印尼
    assert phone_lang_hint("819012345678") == "ja"      # 日本
    assert phone_lang_hint("85291234567") == "zh"       # 香港（3 位码先于 86）
    assert phone_lang_hint("8613800138000") == "zh"     # 大陆
    assert phone_lang_hint("13051234567") == "en"       # 北美 1
    assert phone_lang_hint("79161234567") == "ru"       # 俄罗斯 7
    assert phone_lang_hint("5511987654321") == "pt"     # 巴西
    assert phone_lang_hint("971501234567") == "ar"      # 阿联酋


def test_phone_cc_jid_suffix_and_plus():
    assert phone_lang_hint("639270135480@s.whatsapp.net") == "en"
    assert phone_lang_hint("639270135480:12@s.whatsapp.net") == "en"
    assert phone_lang_hint("+639270135480") == "en"


def test_phone_cc_rejects_non_phone():
    assert phone_lang_hint("") == ""
    assert phone_lang_hint("john_doe") == ""
    assert phone_lang_hint("12345") == ""                       # 太短（<7 位）
    assert phone_lang_hint("120363041234567890@g.us") == ""     # WA 群 JID（18 位超限）
    assert phone_lang_hint("99912345678") == ""                 # 未知国码


def test_phone_cc_overrides_win_and_sanitize():
    assert phone_lang_hint("639270135480", overrides={"63": "fil"}) == "fil"
    assert phone_lang_hint("639270135480", overrides={"+63": "fil"}) == "fil"
    # 坏值忽略 → 回内置表
    assert phone_lang_hint("639270135480", overrides={"63": ""}) == "en"
    assert phone_lang_hint("639270135480", overrides={"abc": "th"}) == "en"


# ── 纯函数：initial_lang_hint 优先级链 ──────────────────────────


def _cfg(**lang_prior):
    return {"lang_prior": {"enabled": True, **lang_prior}}


def test_hint_disabled_or_missing_returns_empty():
    assert initial_lang_hint(platform="whatsapp", chat_key="639270135480",
                             config={}) == ""
    assert initial_lang_hint(
        platform="whatsapp", chat_key="639270135480",
        config={"lang_prior": {"enabled": False}}) == ""


def test_hint_account_exact_beats_platform_beats_phone():
    cfg = _cfg(account_defaults={"whatsapp:63988": "th", "whatsapp": "ja"})
    assert initial_lang_hint(platform="whatsapp", account_id="63988",
                             chat_key="639270135480", config=cfg) == "th"
    assert initial_lang_hint(platform="whatsapp", account_id="other",
                             chat_key="639270135480", config=cfg) == "ja"
    # 无账号配置 → 国码推断
    assert initial_lang_hint(platform="whatsapp", account_id="other",
                             chat_key="639270135480", config=_cfg()) == "en"


def test_hint_phone_platform_whitelist_blocks_telegram_ids():
    # tg 数字 user_id 形似电话号码（54xxx… 会被误判阿根廷）→ 白名单外不推断
    assert initial_lang_hint(platform="telegram", chat_key="5433982810",
                             config=_cfg()) == ""
    # 运营显式加白 → 才推断
    assert initial_lang_hint(
        platform="telegram", chat_key="639270135480",
        config=_cfg(phone_platforms=["telegram"])) == "en"


def test_hint_country_overrides_passthrough():
    assert initial_lang_hint(
        platform="whatsapp", chat_key="639270135480",
        config=_cfg(country_overrides={"63": "fil"})) == "fil"


def test_hint_malformed_config_soft_fails():
    assert initial_lang_hint(platform="whatsapp", chat_key="639270135480",
                             config={"lang_prior": "oops"}) == ""
    assert initial_lang_hint(
        platform="whatsapp", chat_key="639270135480",
        config=_cfg(account_defaults="oops", phone_platforms="oops")) == ""
    assert initial_lang_hint(config=None) == ""


# ── 接线：persona_reply._initial_lang_default ───────────────────


def test_persona_reply_initial_default_parses_account_from_cid():
    from src.inbox.persona_reply import _initial_lang_default

    class _CM:
        config = _cfg(account_defaults={"whatsapp:63988": "th"})

    class _State:
        config_manager = _CM()

    class _App:
        state = _State()

    assert _initial_lang_default(
        _App(), "whatsapp", "639270135480",
        "whatsapp:63988:639270135480") == "th"
    # cid 缺失 → account 空 → 平台/国码链
    assert _initial_lang_default(_App(), "whatsapp", "639270135480", "") == "en"
    # 未启用 → 回落 zh（维持旧行为）
    class _CM2:
        config = {}

    class _State2:
        config_manager = _CM2()

    class _App2:
        state = _State2()

    assert _initial_lang_default(_App2(), "whatsapp", "639270135480", "") == "zh"


# ── 接线：skill_manager 3b default（真实 process_message 全链）──


class _FakeAIClient:
    model = "fake-model"

    def __init__(self):
        self.reply_count = 0

    async def generate_reply_with_intent(self, *args, **kwargs):
        self.reply_count += 1
        return f"回复{self.reply_count}-{uuid.uuid4().hex}"

    async def chat(self, *args, **kwargs):
        return "no"

    def _detect_message_language(self, text: str) -> str:
        from src.ai.lang_policy import classify_evidence
        return classify_evidence(text)[0] or "zh"

    async def embed(self, texts):
        return [[0.0] * 8 for _ in texts]

    async def embed_with_fallback(self, texts):
        return [[0.0] * 8 for _ in texts]


async def _make_sm(tmp_path: Path, *, lang_prior: dict | None = None):
    from src.skills.skill_manager import GreetingSkill, SkillManager
    from src.utils.config_manager import ConfigManager

    cfg = {
        "telegram": {"api_id": "1", "api_hash": "x", "phone_number": "+1"},
        "ai": {"api_key": "k"},
        "skills": {
            "enabled": [],
            "cooldown": {"global": 0, "per_user": 0,
                         "per_content": 0, "per_chat_user": 0},
        },
        "intent": {"keywords": {}, "patterns": {}},
        "reply": {},
        "context_store": {"ttl_days": 30},
        "memory": {
            "enabled": True,
            "db_path": str(tmp_path / "episodic.db"),
            "vector": {"enabled": False},
            "extract": {"enabled": False},
        },
    }
    if lang_prior is not None:
        cfg["lang_prior"] = lang_prior
    (tmp_path / "config.yaml").write_text(
        yaml.dump(cfg, allow_unicode=True), encoding="utf-8")
    (tmp_path / "templates.yaml").write_text("greeting: hi\n", encoding="utf-8")
    (tmp_path / "exchange_rates.yaml").write_text("channels: {}\n", encoding="utf-8")
    cm = ConfigManager(str(tmp_path / "config.yaml"))
    await cm.load()
    ai = _FakeAIClient()
    sm = SkillManager(cm, ai)
    sm.skills["greeting"] = GreetingSkill(cm, ai)
    return sm


@pytest.fixture(autouse=True)
def _clean_hook_registry():
    HookRegistry.reset()
    yield
    HookRegistry.reset()


async def _say(sm, user_id: str, text: str, **ctx_extra):
    ctx = {"chat_id": "639270135480", "platform": "whatsapp",
           "account_id": "63988"}
    ctx.update(ctx_extra)
    reply = await sm.process_message(text, user_id, ctx)
    assert reply, f"process_message 未产出回复: {text!r}"
    return reply


async def test_sm_neutral_first_message_uses_phone_prior(tmp_path):
    """WA 菲律宾号首条 "Hi 👋"（中性零证据）→ 先验 en；后续强中文证据即让位。"""
    sm = await _make_sm(tmp_path, lang_prior={"enabled": True})
    uid = "whatsapp:63988:639270135480"

    await _say(sm, uid, "Hi 👋")
    uc = sm._get_user_context(uid)
    assert uc.get("reply_lang") == "en"

    # 中性追问 → prev_lang=en 粘住（先验只供给首轮 default，不再参与）
    await _say(sm, uid, "ok ok")
    assert sm._get_user_context(uid).get("reply_lang") == "en"

    # 客户真写中文（强证据）→ 立即跟随，先验彻底让位
    await _say(sm, uid, "你好呀，可以说中文吗，今天有点忙")
    assert sm._get_user_context(uid).get("reply_lang") == "zh"


async def test_sm_prior_disabled_keeps_zh_default(tmp_path):
    """未开 lang_prior → 行为与历史一致：中性首条回落 zh。"""
    sm = await _make_sm(tmp_path)
    uid = "whatsapp:63988:639270135481"
    await _say(sm, uid, "Hi 👋", chat_id="639270135481")
    assert sm._get_user_context(uid).get("reply_lang") == "zh"


async def test_sm_account_default_beats_phone_cc(tmp_path):
    """账号级配置（whatsapp → ja）优先于国码推断（63 → en）。"""
    sm = await _make_sm(tmp_path, lang_prior={
        "enabled": True, "account_defaults": {"whatsapp": "ja"}})
    uid = "whatsapp:63988:639270135482"
    await _say(sm, uid, "Hi 👋", chat_id="639270135482")
    assert sm._get_user_context(uid).get("reply_lang") == "ja"


async def test_sm_strong_evidence_first_message_ignores_prior(tmp_path):
    """首条就是强证据（成句英文/中文）→ 走 detected，先验不参与。"""
    sm = await _make_sm(tmp_path, lang_prior={
        "enabled": True, "account_defaults": {"whatsapp": "ja"}})
    uid = "whatsapp:63988:639270135483"
    await _say(sm, uid, "今天上班好累啊，晚上想吃火锅", chat_id="639270135483")
    assert sm._get_user_context(uid).get("reply_lang") == "zh"
