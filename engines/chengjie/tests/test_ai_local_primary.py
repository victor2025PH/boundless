"""本地优先 / 全本地主对话链（``ai.primary``，P1）。

守四条不变量：
1. **默认 cloud → 行为零变化**（既有 test_ai_client_chat_fallback 那套全部照旧）。
2. ``local`` 本地即主链；本地挂了才回落云端。
3. ``local_only`` **绝不把用户内容发往云端**——本地挂了直接不回复（隐私优先于可用性）。
   这是隐私敏感客户选它的全部理由，静默回落云端等于背叛该承诺。
4. **本地转正不得被报成「降级顶班」**：否则 degradation_snapshot 永久 degraded、
   坐席端天天挂假红条、告警长期误鸣。成功须记进主链 ok 时间戳 + tier=local_primary。

本文件不触网（假 AsyncOpenAI 注入）。
"""
from __future__ import annotations

from src.ai.ai_client import AIClient
from tests.test_ai_client_chat_fallback import _Cfg, _FakeChatClient


def _client(
    primary: _FakeChatClient | None,
    local: _FakeChatClient | None,
    mode: str,
) -> AIClient:
    c = AIClient(_Cfg())
    c._use_openai_compat = primary is not None
    c._oa_client = primary
    c.model = "deepseek-chat"
    c.timeout = 5
    c._cb_enabled = False
    if local is not None:
        c._fb_client = local
        c._fb_model = "qwen-local"
    c._primary_mode = mode
    return c


# ── 1. 配置解析 / 配错保护 ─────────────────────────────────────────────────

def test_default_mode_is_cloud():
    c = AIClient(_Cfg())
    assert c._primary_mode == "cloud"


async def test_local_without_endpoint_degrades_to_cloud(monkeypatch):
    """声明本地优先却没配 fallback 端点 → 退回 cloud（不把聊天变砖）。

    须走 ``_initialize_openai_compatible``（primary 解析只在该分支；
    ``__init__`` 只置默认 cloud）。
    """
    class _FakeOpenAI:
        def __init__(self, **kw):
            pass

    monkeypatch.setattr("src.ai.ai_client.AsyncOpenAI", _FakeOpenAI)

    class _C(_Cfg):
        def get_ai_config(self):
            return {
                "provider": "openai_compatible",
                "api_key": "sk-test",
                "base_url": "https://api.deepseek.com",
                "model": "deepseek-chat",
                "primary": "local_only",      # 故意不给 fallback
            }

    c = AIClient(_C())
    await c.initialize()
    assert c._primary_mode == "cloud"


async def test_illegal_mode_falls_back_to_cloud(monkeypatch):
    class _FakeOpenAI:
        def __init__(self, **kw):
            pass

    monkeypatch.setattr("src.ai.ai_client.AsyncOpenAI", _FakeOpenAI)

    class _C(_Cfg):
        def get_ai_config(self):
            return {
                "provider": "openai_compatible",
                "api_key": "sk-test",
                "base_url": "https://api.deepseek.com",
                "model": "deepseek-chat",
                "primary": "on-prem-maybe",
            }

    c = AIClient(_C())
    await c.initialize()
    assert c._primary_mode == "cloud"


async def test_local_mode_parsed_when_endpoint_present(monkeypatch):
    class _FakeOpenAI:
        def __init__(self, **kw):
            pass

    monkeypatch.setattr("src.ai.ai_client.AsyncOpenAI", _FakeOpenAI)

    class _C(_Cfg):
        def get_ai_config(self):
            return {
                "provider": "openai_compatible",
                "api_key": "sk-test",
                "base_url": "https://api.deepseek.com",
                "model": "deepseek-chat",
                "primary": "local",
                "fallback": {"enabled": True, "model": "qwen3:30b",
                             "base_url": "http://192.168.0.176:11434"},
            }

    c = AIClient(_C())
    await c.initialize()
    assert c._primary_mode == "local"


# ── 2. local：本地即主链，挂了才回落云端 ───────────────────────────────────

async def test_local_primary_serves_and_cloud_untouched():
    cloud = _FakeChatClient(reply="云端不该被调用")
    local = _FakeChatClient(reply="本地主模型回复")
    c = _client(cloud, local, "local")
    out = await c._generate_reply_openai_compat("在吗", context={"reply_lang": "zh"})
    assert out == "本地主模型回复"
    assert local.calls == 1
    assert cloud.calls == 0, "本地优先模式下云端不该被调用"


async def test_local_mode_falls_back_to_cloud_when_local_down():
    cloud = _FakeChatClient(reply="云端接手")
    local = _FakeChatClient(fail=True)
    c = _client(cloud, local, "local")
    out = await c._generate_reply_openai_compat("在吗", context={"reply_lang": "zh"})
    assert out == "云端接手"
    assert local.calls == 1 and cloud.calls >= 1


# ── 3. local_only：隐私红线，绝不回落云端 ─────────────────────────────────

async def test_local_only_never_calls_cloud_on_failure():
    cloud = _FakeChatClient(reply="绝不能出现")
    local = _FakeChatClient(fail=True)
    c = _client(cloud, local, "local_only")
    out = await c._generate_reply_openai_compat("在吗", context={"reply_lang": "zh"})
    assert cloud.calls == 0, "local_only 下用户内容绝不能发往云端"
    assert out is None                     # 本轮不回复（罐头兜底已移除）


async def test_local_only_empty_reply_also_stays_local():
    cloud = _FakeChatClient(reply="绝不能出现")
    local = _FakeChatClient(reply=None)    # 空答
    c = _client(cloud, local, "local_only")
    out = await c._generate_reply_openai_compat("在吗", context={"reply_lang": "zh"})
    assert cloud.calls == 0
    assert out is None


async def test_local_only_works_without_any_cloud_client():
    """全本地部署常根本没配云端 key（_oa_client=None）——仍须能出话。"""
    local = _FakeChatClient(reply="纯本地出话")
    c = _client(None, local, "local_only")
    out = await c._generate_reply_openai_compat("在吗", context={"reply_lang": "zh"})
    assert out == "纯本地出话"


async def test_no_cloud_and_local_down_returns_none():
    local = _FakeChatClient(fail=True)
    c = _client(None, local, "local")       # 非 only，但压根没云端可回落
    out = await c._generate_reply_openai_compat("在吗", context={"reply_lang": "zh"})
    assert out is None                      # 不抛、不出罐头话＝本轮不回复


# ── 4. 观测语义：本地转正 ≠ 降级顶班 ──────────────────────────────────────

async def test_local_primary_not_reported_as_degraded():
    local = _FakeChatClient(reply="本地主模型回复")
    c = _client(None, local, "local_only")
    await c._generate_reply_openai_compat("在吗", context={"reply_lang": "zh"})
    snap = c.degradation_snapshot()
    assert snap["degraded"] is False, "本地优先是正常运行，不该报降级"
    assert snap["mode"] == "primary"
    # 成功记进主链而非兜底时间戳
    assert c._last_primary_ok_ts > 0
    assert c._last_fb_ok_ts == 0


async def test_local_fallback_still_reported_as_degraded():
    """对照组：cloud 模式下本地兜底顶班仍须如实报降级（旧语义不许被我改坏）。"""
    cloud = _FakeChatClient(fail=True)
    local = _FakeChatClient(reply="兜底出话")
    c = _client(cloud, local, "cloud")
    await c._generate_reply_openai_compat("在吗", context={"reply_lang": "zh"})
    snap = c.degradation_snapshot()
    assert snap["degraded"] is True and snap["mode"] == "local"
    assert c._last_fb_ok_ts > 0


async def test_local_primary_skips_fallback_duty_counter(monkeypatch):
    """本地转正不得计入顶班计数（否则告警长期误鸣：2026-09-18 实锤）。

    两层都要钉：metrics_store.local_llm_fallback **和** get_stats().local_fallback_calls
    （看门狗 ``_check_local_fallback_duty`` 读的是后者）。
    """
    rec = []
    c = _client(None, _FakeChatClient(reply="ok"), "local_only")
    monkeypatch.setattr(
        c, "_record_local_fallback_metric",
        lambda ok, latency_ms=0.0: rec.append(ok))
    await c._generate_reply_openai_compat("在吗", context={"reply_lang": "zh"})
    assert rec == [], "as_primary 不该记顶班 metric"
    assert c._fb_calls == 0 and c._fb_ok == 0
    stats = c.get_stats()
    assert stats["local_fallback_calls"] == 0
    assert stats["local_fallback_ok"] == 0
    assert stats["primary_mode"] == "local_only"


async def test_local_fallback_still_counts_duty():
    """对照组：cloud 模式下本地兜底出话仍须进 local_fallback_*。"""
    cloud = _FakeChatClient(fail=True)
    local = _FakeChatClient(reply="兜底出话")
    c = _client(cloud, local, "cloud")
    await c._generate_reply_openai_compat("在吗", context={"reply_lang": "zh"})
    assert c._fb_calls == 1 and c._fb_ok == 1
    assert c.get_stats()["local_fallback_calls"] == 1
