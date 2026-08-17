"""本地档分层路由（P3）：``local_model`` 覆写流进本地链。

背景实证（2026-08-06 本地交付验收）：全人设 prompt（1686 tokens）在 .173 14B 上
56s——本地档的产品化靠**按会话分级换模型**（同端点 14B 常暖接长尾、30B 按需接
高价值），不是全员换大模型。守：
1. ``ai.tiers.<tier>.local_model`` 经既有 _apply_tier_overrides 流入本地主链；
2. 调用方显式 ``strategy_overrides.local_model`` 无 tiers 也生效；
3. 不给覆写 → 旧行为（ai.fallback.model）逐字节不变；
4. 成本记账记**实际所用模型**（分层效果可分模型对比）；
5. 云挂兜底路径同样吃覆写（同一会话降级不换档）。
"""
from __future__ import annotations

from src.ai.ai_client import AIClient
from src.ai.llm_cost import get_llm_cost
from tests.test_ai_client_chat_fallback import _Cfg, _FakeChatClient


def _client(
    primary: _FakeChatClient | None,
    local: _FakeChatClient,
    mode: str,
    *,
    tiers: dict | None = None,
) -> AIClient:
    c = AIClient(_Cfg())
    c._use_openai_compat = primary is not None
    c._oa_client = primary
    c.model = "deepseek-chat"
    c.timeout = 5
    c._cb_enabled = False
    c._fb_client = local
    c._fb_model = "qwen-local-14b"
    c._primary_mode = mode
    if tiers is not None:
        c._tiers_enabled = True
        c._tiers_default = "normal"
        c._tiers = tiers
    return c


async def test_tier_local_model_reaches_local_primary_chain():
    local = _FakeChatClient(reply="大模型回复")
    c = _client(None, local, "local_only",
                tiers={"normal": {}, "vip": {"local_model": "qwen-local-30b"}})
    out = await c.generate_reply("在吗", context={"reply_lang": "zh", "ai_tier": "vip"})
    assert out == "大模型回复"
    assert local.last_kw["model"] == "qwen-local-30b"


async def test_default_tier_keeps_fallback_model():
    local = _FakeChatClient(reply="常暖模型回复")
    c = _client(None, local, "local_only",
                tiers={"normal": {}, "vip": {"local_model": "qwen-local-30b"}})
    out = await c.generate_reply("在吗", context={"reply_lang": "zh"})
    assert out == "常暖模型回复"
    assert local.last_kw["model"] == "qwen-local-14b", "无分档信号必须走配置默认模型"


async def test_explicit_strategy_override_without_tiers():
    local = _FakeChatClient(reply="ok")
    c = _client(None, local, "local_only")     # tiers 未启用
    await c.generate_reply("在吗", context={"reply_lang": "zh"},
                           strategy_overrides={"local_model": "qwen-local-30b"})
    assert local.last_kw["model"] == "qwen-local-30b"


async def test_llm_cost_records_actual_model():
    local = _FakeChatClient(reply="ok")
    c = _client(None, local, "local_only")
    marker = "tier-audit-model-p3"
    await c.generate_reply("在吗", context={"reply_lang": "zh"},
                           strategy_overrides={"local_model": marker})
    rows = get_llm_cost().dump().get("rows") or []
    mine = [r for r in rows if r.get("model") == marker]
    assert mine and mine[0]["tier"] == "local_primary", \
        "记账必须记实际所用模型，否则分层效果在看板不可见"


async def test_cloud_down_fallback_honors_tier_local_model():
    cloud = _FakeChatClient(fail=True)
    local = _FakeChatClient(reply="兜底大模型")
    c = _client(cloud, local, "cloud",
                tiers={"normal": {}, "vip": {"local_model": "qwen-local-30b"}})
    out = await c.generate_reply("在吗", context={"reply_lang": "zh", "ai_tier": "vip"})
    assert out == "兜底大模型"
    assert local.last_kw["model"] == "qwen-local-30b", \
        "云挂降级到本地时同一会话不该丢分档"
