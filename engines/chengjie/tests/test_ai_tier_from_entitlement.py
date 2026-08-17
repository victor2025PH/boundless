"""会员权益 → ai_tier 自动进档（P4，把 P3 分层从「可配」变「在用」）。

选档级联（_apply_tier_overrides）：显式 ``context.ai_tier`` → ``entitlement.tier``
（档名与 ``ai.tiers`` 对齐才采纳）→ 默认档。守：
1. vip 权益自动吃到 vip 档（端到端到本地链换模型）；
2. 显式 ai_tier 永远压过权益档；
3. 权益档名未配置 / 无权益 / tiers 关 → 全部旧行为；
4. skill_manager 权益懒加载门控：story **或** ai.tiers 任一启用即解析
   （旧口径只认 story——分级部署不开剧情就永远拿不到权益，P3 白配）。
"""
from __future__ import annotations

import logging
from types import SimpleNamespace

from src.ai.ai_client import AIClient
from tests.test_ai_client_chat_fallback import _Cfg, _FakeChatClient

# ── AIClient 侧：选档级联 ────────────────────────────────────────


def _client(local: _FakeChatClient, *, tiers: dict | None) -> AIClient:
    c = AIClient(_Cfg())
    c._use_openai_compat = False
    c._oa_client = None
    c.model = "deepseek-chat"
    c.timeout = 5
    c._cb_enabled = False
    c._fb_client = local
    c._fb_model = "qwen-local-14b"
    c._primary_mode = "local_only"
    if tiers is not None:
        c._tiers_enabled = True
        c._tiers_default = "normal"
        c._tiers = tiers
    return c


_TIERS = {"normal": {}, "vip": {"local_model": "qwen-local-30b"}}


async def test_vip_entitlement_auto_selects_vip_tier():
    local = _FakeChatClient(reply="ok")
    c = _client(local, tiers=dict(_TIERS))
    await c.generate_reply("在吗", context={
        "reply_lang": "zh",
        "entitlement": {"tier": "vip", "grants": ["priority_reply"]},
    })
    assert local.last_kw["model"] == "qwen-local-30b", "VIP 权益应自动进 vip 档"


async def test_explicit_ai_tier_beats_entitlement():
    local = _FakeChatClient(reply="ok")
    c = _client(local, tiers=dict(_TIERS))
    await c.generate_reply("在吗", context={
        "reply_lang": "zh", "ai_tier": "normal",
        "entitlement": {"tier": "vip"},
    })
    assert local.last_kw["model"] == "qwen-local-14b", "显式 ai_tier 必须压过权益档"


async def test_unconfigured_entitlement_tier_falls_to_default():
    local = _FakeChatClient(reply="ok")
    c = _client(local, tiers=dict(_TIERS))
    await c.generate_reply("在吗", context={
        "reply_lang": "zh", "entitlement": {"tier": "svip"},   # 未配 svip 档
    })
    assert local.last_kw["model"] == "qwen-local-14b"


async def test_no_entitlement_and_disabled_tiers_unchanged():
    local = _FakeChatClient(reply="ok")
    c = _client(local, tiers=dict(_TIERS))
    await c.generate_reply("在吗", context={"reply_lang": "zh"})
    assert local.last_kw["model"] == "qwen-local-14b"

    local2 = _FakeChatClient(reply="ok")
    c2 = _client(local2, tiers=None)           # tiers 未启用
    await c2.generate_reply("在吗", context={
        "reply_lang": "zh", "entitlement": {"tier": "vip"}})
    assert local2.last_kw["model"] == "qwen-local-14b", "tiers 关时权益不得改行为"


async def test_cloud_accounting_gets_entitlement_tier():
    """云链成本按 context.ai_tier 记账——自动进档必须回写，否则 VIP 记成 default。"""
    from src.ai.llm_cost import get_llm_cost

    cloud = _FakeChatClient(reply="云端回复")
    c = AIClient(_Cfg())
    c._use_openai_compat = True
    c._oa_client = cloud
    c.model = "tier-audit-cloud-p4"        # 唯一名防并跑测试串味
    c.timeout = 5
    c._cb_enabled = False
    c._primary_mode = "cloud"
    c._tiers_enabled = True
    c._tiers_default = "normal"
    c._tiers = {"normal": {}, "vip": {}}
    ctx = {"reply_lang": "zh", "entitlement": {"tier": "vip"}}
    out = await c.generate_reply("在吗", context=ctx)
    assert out == "云端回复"
    assert ctx.get("ai_tier") == "vip", "自动进档须回写 context 供记账/下游"
    rows = [r for r in (get_llm_cost().dump().get("rows") or [])
            if r.get("model") == "tier-audit-cloud-p4"]
    assert rows and rows[0]["tier"] == "vip"


async def test_default_tier_not_written_back():
    """落默认档不回写——保住既有 default 记账口径（标签语义零变化）。"""
    local = _FakeChatClient(reply="ok")
    c = _client(local, tiers=dict(_TIERS))
    ctx = {"reply_lang": "zh"}
    await c.generate_reply("在吗", context=ctx)
    assert "ai_tier" not in ctx


# ── skill_manager 侧：权益懒加载门控（story 或 ai.tiers）─────────


def _sm(*, story: bool, tiers: bool):
    from src.skills.skill_manager import SkillManager as _SMcls

    class _SM:
        _story_cfg = _SMcls._story_cfg
        _ensure_entitlement = _SMcls._ensure_entitlement

        def __init__(self):
            cfg = {
                "companion": {"story": {"enabled": story}},
                "ai": {"tiers": {"enabled": tiers, "default": "normal",
                                 "vip": {"local_model": "big"}}},
            }
            self.config = SimpleNamespace(config=cfg)
            self.logger = logging.getLogger("test_tier_ent")

    return _SM()


def test_ensure_entitlement_resolves_when_only_tiers_enabled():
    from src.utils.companion_context import (
        reset_relationship_providers, set_relationship_providers,
    )
    reset_relationship_providers()
    try:
        set_relationship_providers(
            entitlement_resolver=lambda ck: {"tier": "vip", "grants": []})
        sm = _sm(story=False, tiers=True)
        ctx: dict = {}
        sm._ensure_entitlement("tg:acc:u1", ctx)
        assert ctx.get("entitlement", {}).get("tier") == "vip", \
            "分级启用（剧情关）也必须解析权益，否则 P3 分层永远吃不到档"
    finally:
        reset_relationship_providers()


def test_ensure_entitlement_still_skipped_when_both_disabled():
    from src.utils.companion_context import (
        reset_relationship_providers, set_relationship_providers,
    )
    reset_relationship_providers()
    try:
        set_relationship_providers(
            entitlement_resolver=lambda ck: {"tier": "vip", "grants": []})
        sm = _sm(story=False, tiers=False)
        ctx: dict = {}
        sm._ensure_entitlement("tg:acc:u1", ctx)
        assert "entitlement" not in ctx, "两开关全关＝零开销旧行为"
    finally:
        reset_relationship_providers()


# ── B 线静态接线（P5）───────────────────────────────────────────


def test_inbox_draft_chain_wires_entitlement():
    """generate_inbox_draft 必须在生成前懒解析权益——否则 VIP 在人审/自动发
    草稿链永远进不了档（P4 级联只消费 context，谁也不会替它补）。

    函数体巨大且依赖重，端到端构造不现实 → 静态接线门禁（repo 先例：
    test_human_deliver 的「静态接线不得被 if 包住」）。
    """
    import inspect

    from src.skills.skill_manager import SkillManager
    src = inspect.getsource(SkillManager.generate_inbox_draft)
    assert "self._ensure_entitlement(user_id, user_context)" in src, \
        "B 线权益接线丢失（P5）——草稿链会员分档会静默失效"
