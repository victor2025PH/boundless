"""本地窗口口径（2026-09-18 「脑子有点空」事故沉淀）。

事故：173:8001 是 vLLM ``max_model_len=24576``，但 ``ai.fallback`` 没配 ``num_ctx`` 时
AIClient 一律落 8192，再固定扣 ``max_tokens(4096)+128`` 出话预留 → prompt 只剩 3968，
人设一个块就把历史裁到 2 条；同一台机器在无限制路由却按 24576 封顶。本文件钉住：

1. ``resolve_local_num_ctx``：显式 num_ctx > ai.unrestricted.max_ctx > Ollama 8192 > 私网 24576；
2. ``fit_local_budget``：先收输出预留（到 512 地板）、再裁历史；装得下不动 max_tokens；
3. AIClient 装载后 ``_fb_num_ctx`` 与 conv_route ``endpoint_spec`` 对同一端点给同一个数；
4. 端到端：本地出话时实际下发的 ``max_tokens`` 被收缩、历史保留条数明显多于旧算法。
"""
from __future__ import annotations

from src.ai.ai_client import AIClient
from src.ai.vendor_params import (
    DEFAULT_OLLAMA_CTX, DEFAULT_PRIVATE_CTX, LOCAL_OUTPUT_RESERVE_FLOOR,
    fit_local_budget, resolve_local_num_ctx,
)
from src.ai import conv_route


# ── 1. 窗口解析 ────────────────────────────────────────────────────────────────

def test_resolve_explicit_num_ctx_wins():
    n, src = resolve_local_num_ctx(
        {"base_url": "http://192.168.0.173:8001", "num_ctx": 16384},
        {"unrestricted": {"max_ctx": 30000}})
    assert (n, src) == (16384, "ai.fallback.num_ctx")


def test_resolve_unrestricted_max_ctx_second():
    n, src = resolve_local_num_ctx(
        {"base_url": "http://192.168.0.173:8001"},
        {"unrestricted": {"max_ctx": 32768}})
    assert (n, src) == (32768, "ai.unrestricted.max_ctx")


def test_resolve_ollama_stays_conservative():
    n, src = resolve_local_num_ctx({"base_url": "http://192.168.0.176:11434"}, {})
    assert (n, src) == (DEFAULT_OLLAMA_CTX, "ollama_default")
    assert DEFAULT_OLLAMA_CTX == 8192


def test_resolve_vllm_private_defaults_to_24k():
    n, src = resolve_local_num_ctx({"base_url": "http://192.168.0.173:8001"}, {})
    assert (n, src) == (DEFAULT_PRIVATE_CTX, "private_default")
    assert DEFAULT_PRIVATE_CTX == 24_576


def test_resolve_never_raises_on_garbage():
    assert resolve_local_num_ctx(None, None)[0] == DEFAULT_PRIVATE_CTX
    assert resolve_local_num_ctx({"num_ctx": "abc"}, {"unrestricted": "x"})[0] == DEFAULT_PRIVATE_CTX
    assert resolve_local_num_ctx({"num_ctx": -5, "base_url": "http://10.0.0.1:11434"}, {})[0] == 8192


def test_resolve_matches_conv_route_endpoint_spec():
    """同一端点：主链/兜底口径 == 无限制路由口径（事故本质是两处不一致）。"""
    cfg = {"ai": {"fallback": {"enabled": True, "base_url": "http://192.168.0.173:8001",
                               "model": "chatx"}}}
    spec = conv_route.endpoint_spec(cfg)
    n, _ = resolve_local_num_ctx(cfg["ai"]["fallback"], cfg["ai"])
    assert spec["max_ctx"] == n == 24_576


# ── 2. 预算装配 ────────────────────────────────────────────────────────────────

def test_fit_keeps_max_tokens_when_prompt_fits():
    budget, mt = fit_local_budget(3000, 24_576, 4096)
    assert mt == 4096
    assert budget == 24_576 - 4096 - 128


def test_fit_shrinks_output_reserve_before_trimming_history():
    # 8192 窗口 + 7000 prompt：旧算法预算 3968 → 裁掉一半 prompt；新算法只把预留收到
    # 「刚好装下」(8192-7000-128=1064)，历史一条不裁
    budget, mt = fit_local_budget(7000, 8192, 4096)
    assert mt == 1064
    assert budget == 7000            # prompt 装得下，不裁历史
    # prompt 再涨到 7600 → 预留触地板 512，剩下的交给历史裁剪
    budget, mt = fit_local_budget(7600, 8192, 4096)
    assert mt == LOCAL_OUTPUT_RESERVE_FLOOR == 512
    assert budget == 8192 - 512 - 128


def test_fit_partial_shrink_only_as_needed():
    # 24576 窗口 + 21000 prompt：预留 = 24576-21000-128 = 3448（介于地板与 4096 之间）
    budget, mt = fit_local_budget(21_000, 24_576, 4096)
    assert mt == 3448
    assert budget == 24_576 - 3448 - 128 == 21_000


def test_fit_prompt_over_window_still_leaves_floor_for_output():
    budget, mt = fit_local_budget(30_000, 24_576, 4096)
    assert mt == 512
    assert budget == 24_576 - 512 - 128   # 调用方据此裁历史


def test_fit_tiny_window_floor_halves():
    # 窗口 700：地板收到 350，预算走 512 下限（与旧用例 test_fallback_trims_long_history 同值）
    budget, mt = fit_local_budget(5000, 700, 1024)
    assert mt == 350
    assert budget == 512


def test_fit_garbage_inputs_do_not_raise():
    assert fit_local_budget("x", "y", "z") == (0, 0)
    assert fit_local_budget(100, 0, 1024) == (0, 1024)
    assert fit_local_budget(100, 8192, 0)[1] == 512     # max_tokens=0 → 按地板送


# ── 3. AIClient 装载 ───────────────────────────────────────────────────────────

class _Cfg:
    config_path = None

    def __init__(self, ai):
        self.config = {"web_admin": {"site_name": "T"}, "ai": ai}

    def get_ai_config(self):
        return self.config["ai"]


def _mk(monkeypatch, fb: dict, extra_ai: dict | None = None) -> AIClient:
    """走真实装载路径 ``_initialize_openai_compatible``（兜底块就在那里解析）。"""
    import asyncio
    import src.ai.ai_client as mod

    class _FakeAsyncOpenAI:
        def __init__(self, **kw):
            pass

    monkeypatch.setattr(mod, "AsyncOpenAI", _FakeAsyncOpenAI)
    monkeypatch.setattr(mod, "OPENAI_SDK_AVAILABLE", True)
    ai = {"base_url": "http://chat:1/v1", "fallback": fb}
    if extra_ai:
        ai.update(extra_ai)
    c = AIClient(_Cfg(ai))
    c.timeout = 5
    c.model = "m"

    async def _ok():
        return True
    monkeypatch.setattr(c, "_test_openai_connection", _ok)
    assert asyncio.run(c._initialize_openai_compatible(ai, "k")) is True
    return c


def test_client_vllm_endpoint_no_longer_defaults_to_8192(monkeypatch):
    c = _mk(monkeypatch, {"enabled": True, "base_url": "http://192.168.0.173:8001", "model": "chatx"})
    assert c._fb_client is not None
    assert c._fb_num_ctx == 24_576


def test_client_ollama_endpoint_keeps_8192(monkeypatch):
    c = _mk(monkeypatch, {"enabled": True, "base_url": "http://192.168.0.176:11434", "model": "qwen3:30b"})
    assert c._fb_num_ctx == 8192


def test_client_honours_unrestricted_max_ctx(monkeypatch):
    c = _mk(monkeypatch, {"enabled": True, "base_url": "http://192.168.0.173:8001", "model": "chatx"},
            {"unrestricted": {"max_ctx": 32_768}})
    assert c._fb_num_ctx == 32_768


# ── 4. 端到端：出话预留收缩 + 历史保留 ────────────────────────────────────────

class _FakeChatClient:
    def __init__(self, *, fail=False, reply="ok"):
        self.fail = fail
        self.reply = reply
        self.last_kw = None
        outer = self

        class _Msg:
            def __init__(self, content):
                self.content = content
                self.model_extra = {}

        class _Choice:
            def __init__(self, content):
                self.message = _Msg(content)

        class _Usage:
            prompt_tokens = 10
            completion_tokens = 5

        class _Resp:
            def __init__(self, content):
                self.choices = [_Choice(content)]
                self.usage = _Usage()

        class _Completions:
            async def create(self, **kw):
                outer.last_kw = kw
                if outer.fail:
                    raise RuntimeError("cloud down")
                return _Resp(outer.reply)

        class _Chat:
            completions = _Completions()

        self.chat = _Chat()


def _client(primary, fallback):
    class _C:
        config_path = None
        config = {"web_admin": {"site_name": "T"}, "ai": {}}

        def get_ai_config(self):
            return {}

    c = AIClient(_C())
    c._use_openai_compat = True
    c._oa_client = primary
    c.model = "deepseek-chat"
    c.timeout = 5
    c._cb_enabled = False
    c._fb_client = fallback
    c._fb_model = "chatx"
    return c


async def test_local_send_shrinks_max_tokens_and_keeps_history():
    """8192 窗口、max_tokens=4096、~6k token 的 prompt：旧算法预算 3968 → 历史被裁掉大半；
    新算法下发 max_tokens 收到 512、历史全部保留。"""
    primary = _FakeChatClient(fail=True)
    fb = _FakeChatClient(reply="出话")
    c = _client(primary, fb)
    c._fb_num_ctx = 8192
    c.max_tokens = 4096
    c.max_conversation_history = 200      # 深度档抬高历史条数（否则默认 10 条先裁掉）
    hist = []
    for i in range(30):
        hist.append({"role": "user", "content": f"历史{i}" + "字" * 70})
        hist.append({"role": "assistant", "content": f"答{i}" + "字" * 70})
    # 60 条 × ~80 token ≈ 4.8k + 人设 ≈ 6k prompt：旧算法预算 3968 → 必裁历史
    out = await c._generate_reply_openai_compat(
        "最新消息", context={"reply_lang": "zh"}, conversation_history=hist)
    assert out == "出话"
    kw = fb.last_kw
    sent = kw["messages"]
    est = sum(AIClient._estimate_msg_tokens(m["content"]) for m in sent)
    old_budget = 8192 - 4096 - 128
    assert est > old_budget                      # 旧算法下这包一定被裁
    # 预留收缩，且 prompt + max_tokens 仍不越窗（vLLM 400 的硬约束）
    assert kw["max_tokens"] < 4096
    assert kw["max_tokens"] >= 512
    assert est + kw["max_tokens"] + 128 <= 8192
    # 历史保留：30 轮 60 条全在
    kept_hist = [m for m in sent if m["role"] in ("user", "assistant") and m["content"].startswith(("历史", "答"))]
    assert len(kept_hist) == 60


async def test_local_send_keeps_max_tokens_when_room():
    primary = _FakeChatClient(fail=True)
    fb = _FakeChatClient(reply="出话")
    c = _client(primary, fb)
    c._fb_num_ctx = 24_576
    c.max_tokens = 2048
    out = await c._generate_reply_openai_compat(
        "在吗", context={"reply_lang": "zh"}, conversation_history=[])
    assert out == "出话"
    assert fb.last_kw["max_tokens"] == 2048
