"""主对话 LLM 容灾（ai.fallback）：云主模型不可达/熔断开路 → 本地模型出真话。

背景：聊天生成原是 DeepSeek 云单点，断网/云故障时只剩 canned 占位句
（「在的，请您稍等一下～」）。本地兜底后由 LAN Ollama 出真话。
2026-08-15 起全链失败**不再回罐头句**（「可以不回复，不能乱回复」——8/13、
8/15 两起罐头客服腔刷屏事故）：返回 None＝本轮不回复。本文件不触网。
"""
from __future__ import annotations

import time

from src.ai.ai_client import AIClient


class _Cfg:
    config_path = None
    config = {"web_admin": {"site_name": "T"}, "ai": {}}

    def get_ai_config(self):
        return {}


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


class _FakeChatClient:
    """最小 AsyncOpenAI.chat.completions 替身：fail=True 恒抛；reply=None 回空答。"""

    def __init__(self, *, fail: bool = False, reply: str | None = "ok"):
        self.fail = fail
        self.reply = reply
        self.calls = 0
        outer = self

        class _Completions:
            async def create(self, **kw):
                outer.calls += 1
                outer.last_kw = kw
                if outer.fail:
                    raise RuntimeError("cloud down")
                return _Resp(outer.reply)

        class _Chat:
            completions = _Completions()

        self.chat = _Chat()


def _client(primary: _FakeChatClient, fallback: _FakeChatClient | None) -> AIClient:
    c = AIClient(_Cfg())
    c._use_openai_compat = True
    c._oa_client = primary
    c.model = "deepseek-chat"
    c.timeout = 5
    c._cb_enabled = False   # 熔断态默认关（initialize() 才会设，此处直构）
    if fallback is not None:
        c._fb_client = fallback
        c._fb_model = "qwen-local"
    return c


async def test_primary_ok_no_fallback():
    primary = _FakeChatClient(reply="你好呀")
    fb = _FakeChatClient(reply="本地回复")
    c = _client(primary, fb)
    out = await c._generate_reply_openai_compat("在吗", context={"reply_lang": "zh"})
    assert out == "你好呀"
    assert primary.calls == 1 and fb.calls == 0
    assert c._fb_calls == 0


async def test_primary_down_falls_to_local():
    primary = _FakeChatClient(fail=True)
    fb = _FakeChatClient(reply="本地兜底的真话")
    c = _client(primary, fb)
    out = await c._generate_reply_openai_compat("在吗", context={"reply_lang": "zh"})
    assert out == "本地兜底的真话"
    assert primary.calls == 2          # 主链两次尝试后才轮到兜底
    assert fb.calls == 1
    assert c._fb_calls == 1 and c._fb_ok == 1
    # 兜底调用带上了主链同款 messages（人设/上下文不丢）+ 目标语钉子并入首位 system
    # （2026-08-15 起：Qwen3 系模板拒绝非打头 system，钉子不再作末位独立消息）
    assert fb.last_kw["model"] == "qwen-local"
    assert {"role": "user", "content": "在吗"} in fb.last_kw["messages"]
    _msgs = fb.last_kw["messages"]
    assert _msgs[0]["role"] == "system" and "Reply strictly in" in _msgs[0]["content"]
    assert all(m["role"] != "system" for m in _msgs[1:])
    assert _msgs[-1]["role"] == "user"


async def test_breaker_open_goes_straight_to_local():
    primary = _FakeChatClient(reply="不该被调用")
    fb = _FakeChatClient(reply="熔断期本地出话")
    c = _client(primary, fb)
    c._cb_enabled = True
    c._cb_open_until = time.time() + 60
    out = await c._generate_reply_openai_compat("在吗", context={"reply_lang": "zh"})
    assert out == "熔断期本地出话"
    assert primary.calls == 0          # 开路语义保住：主模型免打扰
    assert fb.calls == 1


async def test_breaker_open_without_fallback_returns_none():
    """熔断开路且无本地兜底 → None＝本轮不回复（罐头兜底已移除）。"""
    primary = _FakeChatClient(reply="不该被调用")
    c = _client(primary, None)
    c._cb_enabled = True
    c._cb_open_until = time.time() + 60
    out = await c._generate_reply_openai_compat("在吗", context={"reply_lang": "zh"})
    assert out is None
    assert primary.calls == 0


async def test_local_also_down_returns_none():
    """主链+本地全灭 → None，绝不再出「在的，有什么可以帮您的？」式罐头句。"""
    primary = _FakeChatClient(fail=True)
    fb = _FakeChatClient(fail=True)
    c = _client(primary, fb)
    out = await c._generate_reply_openai_compat("在吗", context={"reply_lang": "zh"})
    assert out is None
    assert fb.calls == 1
    assert c._fb_calls == 1 and c._fb_ok == 0


async def test_local_empty_reply_returns_none():
    primary = _FakeChatClient(fail=True)
    fb = _FakeChatClient(reply="")
    c = _client(primary, fb)
    out = await c._generate_reply_openai_compat("在吗", context={"reply_lang": "zh"})
    assert out is None
    assert c._fb_calls == 1 and c._fb_ok == 0


def test_metrics_store_local_llm_fallback_counter():
    """record_local_llm_fallback 计数进 snapshot().local_llm_fallback（/api/bot-metrics 读它）。"""
    from src.monitoring import metrics_store as _msmod
    _msmod.MetricsStore._instance = None
    try:
        ms = _msmod.get_metrics_store()
        ms.record_local_llm_fallback(True)
        ms.record_local_llm_fallback(False)
        snap = ms.snapshot()
        assert snap["local_llm_fallback"] == {"calls": 2, "ok": 1}
    finally:
        _msmod.MetricsStore._instance = None


def test_init_parses_fallback_config(monkeypatch):
    """ai.fallback 配置齐备时初始化出兜底客户端；缺 model/base_url 或未启用则不建。"""
    import asyncio

    built = []

    class _FakeAsyncOpenAI:
        def __init__(self, **kw):
            built.append(kw)

    import src.ai.ai_client as mod
    monkeypatch.setattr(mod, "AsyncOpenAI", _FakeAsyncOpenAI)
    monkeypatch.setattr(mod, "OPENAI_SDK_AVAILABLE", True)

    def _mk(fb_cfg):
        built.clear()
        c = AIClient(_Cfg())
        c.timeout = 5
        c.model = "m"

        async def _ok():
            return True
        monkeypatch.setattr(c, "_test_openai_connection", _ok)
        ok = asyncio.run(c._initialize_openai_compatible(
            {"base_url": "http://chat:1/v1", "fallback": fb_cfg}, "k"))
        assert ok is True
        return c

    c = _mk({"enabled": True, "base_url": "http://176:11434",
             "model": "qwen3:30b", "timeout": 33})
    assert c._fb_client is not None and c._fb_model == "qwen3:30b"
    fb_kw = built[-1]
    assert fb_kw["base_url"] == "http://176:11434/v1"
    assert fb_kw["max_retries"] == 0
    assert c._fb_extra_body == {
        "options": {"think": False},
        # 2026-08-15 主链 27B@64k 换代：vLLM 直答档随 think:false 一并下发
        "chat_template_kwargs": {"enable_thinking": False},
    }
    # Ollama 端点（:11434）→ 走原生 /api/chat（keep_alive/think 才被尊重）
    assert c._fb_native_base == "http://176:11434"
    assert c._fb_keep_alive == "30m" and c._fb_timeout == 33.0
    # num_ctx 缺省 8192（2026-07-14：runner 默认 -c 4096 会 400 拒长对话 prompt）
    assert c._fb_num_ctx == 8192

    c_ctx = _mk({"enabled": True, "base_url": "http://176:11434",
                 "model": "qwen3:30b", "num_ctx": 16384})
    assert c_ctx._fb_num_ctx == 16384

    c2 = _mk({"enabled": False, "base_url": "http://176:11434", "model": "x"})
    assert c2._fb_client is None

    c3 = _mk({"enabled": True, "model": "x"})   # 缺 base_url
    assert c3._fb_client is None

    # 非 Ollama 端口（如 vLLM :8000）→ 维持 /v1 OpenAI 兼容路径
    c4 = _mk({"enabled": True, "base_url": "http://gpu:8000", "model": "x"})
    assert c4._fb_client is not None and c4._fb_native_base is None


def test_coalesce_system_head_folds_trailing_pin():
    """2026-08-15 实锤回归钉：末位语言钉子（system）在 Qwen3 系模板下 400
    「System message must be at the beginning」→ local_only 客户整轮无回复。
    合并后：单条打头 system、内容全保留、非 system 顺序不变。"""
    from src.ai.ai_client import AIClient
    msgs = [
        {"role": "system", "content": "人设+记忆"},
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "嗨"},
        {"role": "user", "content": "在吗"},
        {"role": "system", "content": "Reply strictly in Chinese only."},
    ]
    out = AIClient._coalesce_system_head(msgs)
    assert [m["role"] for m in out] == ["system", "user", "assistant", "user"]
    assert "人设+记忆" in out[0]["content"]
    assert "Reply strictly in Chinese" in out[0]["content"]
    assert out[0]["content"].index("人设+记忆") < out[0]["content"].index("Reply strictly")
    assert out[-1]["content"] == "在吗"


def test_coalesce_system_head_noop_when_already_normal():
    from src.ai.ai_client import AIClient
    normal = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    assert AIClient._coalesce_system_head(normal) == normal
    no_sys = [{"role": "user", "content": "u"}]
    assert AIClient._coalesce_system_head(no_sys) == no_sys
    assert AIClient._coalesce_system_head([]) == []


def test_coalesce_system_head_folds_mid_conversation_system():
    """任何未来的中途 system 注入（提示/守卫）同样被收口，不再依赖模板宽容。"""
    from src.ai.ai_client import AIClient
    msgs = [
        {"role": "system", "content": "A"},
        {"role": "user", "content": "u1"},
        {"role": "system", "content": "B"},
        {"role": "user", "content": "u2"},
    ]
    out = AIClient._coalesce_system_head(msgs)
    assert [m["role"] for m in out] == ["system", "user", "user"]
    assert out[0]["content"] == "A\n\nB"


async def test_fallback_uses_native_api_chat_when_ollama(monkeypatch):
    """_fb_native_base 设定时兜底走原生 /api/chat（带 keep_alive），不走 /v1 客户端。"""
    primary = _FakeChatClient(fail=True)
    fb = _FakeChatClient(reply="不该走 /v1")
    c = _client(primary, fb)
    c._fb_native_base = "http://176:11434"
    c._fb_keep_alive = "30m"
    seen = {}

    async def _fake_native(messages, *, max_tokens, temperature, model=""):
        seen["messages"] = messages
        seen["max_tokens"] = max_tokens
        return "原生口出话", 7, 3

    monkeypatch.setattr(c, "_fb_native_chat", _fake_native)
    out = await c._generate_reply_openai_compat("在吗", context={"reply_lang": "zh"})
    assert out == "原生口出话"
    assert fb.calls == 0                       # /v1 客户端未被打
    # 语言钉子仍然带上——2026-08-15 起并入首位 system（Qwen3 系模板拒非打头 system）
    assert seen["messages"][0]["role"] == "system"
    assert "Reply strictly in" in seen["messages"][0]["content"]
    assert all(m["role"] != "system" for m in seen["messages"][1:])
    assert c._fb_ok == 1


async def test_fb_native_payload_carries_num_ctx(monkeypatch):
    """原生 /api/chat 请求体必须带 options.num_ctx——runner 默认 -c 4096，
    真实事故（2026-07-14）：4161-4292 tokens 的长对话 prompt 被 400
    「exceeds the available context size」拒答，兜底在最有价值的会话上必挂。"""
    import src.ai.ai_client as mod

    c = AIClient(_Cfg())
    c._fb_model = "qwen-local"
    c._fb_native_base = "http://176:11434"
    c._fb_keep_alive = "30m"
    c._fb_num_ctx = 8192
    captured = {}

    class _FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"message": {"content": "ok"}, "prompt_eval_count": 1, "eval_count": 1}

    class _FakeAsyncClient:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None):
            captured["url"] = url
            captured["payload"] = json
            return _FakeResp()

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)
    out, _pt, _ct = await c._fb_native_chat(
        [{"role": "user", "content": "在吗"}], max_tokens=512, temperature=0.7)
    assert out == "ok"
    assert captured["payload"]["options"]["num_ctx"] == 8192
    assert captured["payload"]["keep_alive"] == "30m"
    # num_ctx=0 显式关闭 → 不下发（维持端侧默认）
    c._fb_num_ctx = 0
    await c._fb_native_chat(
        [{"role": "user", "content": "在吗"}], max_tokens=512, temperature=0.7)
    assert "num_ctx" not in captured["payload"]["options"]


def test_trim_messages_under_budget_untouched():
    msgs = [
        {"role": "system", "content": "人设"},
        {"role": "user", "content": "在吗"},
    ]
    out = AIClient._trim_messages_to_budget(msgs, 10_000)
    assert out == msgs


def test_trim_messages_drops_oldest_keeps_system_and_tail():
    """超预算时丢最旧历史；首部 system（人设/记忆）、末尾语言钉子与最后一轮消息保住。"""
    msgs = [{"role": "system", "content": "人设" * 50}]
    for i in range(30):
        msgs.append({"role": "user", "content": f"旧消息{i}" + "字" * 60})
        msgs.append({"role": "assistant", "content": f"旧回复{i}" + "字" * 60})
    msgs.append({"role": "user", "content": "最新的问题"})
    msgs.append({"role": "system", "content": "Reply strictly in Chinese only."})

    budget = 800
    out = AIClient._trim_messages_to_budget(msgs, budget)
    assert len(out) < len(msgs)
    assert out[0]["role"] == "system" and out[0]["content"].startswith("人设")
    assert out[-1]["content"].startswith("Reply strictly")
    assert out[-2] == {"role": "user", "content": "最新的问题"}
    assert sum(AIClient._estimate_msg_tokens(m["content"]) for m in out) <= budget
    # 丢的是最旧的：旧消息0 一定先没
    assert all("旧消息0" not in str(m.get("content")) for m in out)


def test_trim_messages_truncates_system_as_last_resort():
    """只剩保护消息仍超预算 → 截 system 尾部，绝不让整包被 400 拒掉。"""
    msgs = [
        {"role": "system", "content": "指" * 3000},
        {"role": "user", "content": "在吗"},
    ]
    out = AIClient._trim_messages_to_budget(msgs, 500)
    assert len(out) == 2
    assert len(out[0]["content"]) < 3000
    # 原 messages 不被原地污染
    assert len(msgs[0]["content"]) == 3000


async def test_fallback_trims_long_history_before_send(monkeypatch):
    """端到端：长历史兜底调用前被裁进预算（修 400 事故的第二重保险）。"""
    primary = _FakeChatClient(fail=True)
    fb = _FakeChatClient(reply="不走 /v1")
    c = _client(primary, fb)
    c._fb_native_base = "http://176:11434"
    c._fb_num_ctx = 700          # 压小预算逼出裁剪
    seen = {}

    async def _fake_native(messages, *, max_tokens, temperature, model=""):
        seen["messages"] = messages
        return "出话", 7, 3

    monkeypatch.setattr(c, "_fb_native_chat", _fake_native)
    hist = []
    for i in range(40):
        hist.append({"role": "user", "content": f"历史{i}" + "字" * 40})
        hist.append({"role": "assistant", "content": f"答{i}" + "字" * 40})
    out = await c._generate_reply_openai_compat(
        "最新消息", context={"reply_lang": "zh"}, conversation_history=hist)
    assert out == "出话"
    sent = seen["messages"]
    budget = 700 - c.max_tokens - 128
    # max_tokens 默认 1024 > num_ctx 700 → 预算走 512 下限
    budget = max(512, budget)
    assert sum(AIClient._estimate_msg_tokens(m["content"]) for m in sent) <= budget
    # 结构不变量（2026-08-15 契约）：system 只在首位（Qwen3 系模板安全）；
    # 极限预算下 last-resort 可截空 system 尾部（钉子牺牲换出话）——钉子存在性
    # 由常规预算用例（primary_down / native_api_chat）钉住，此处不重复要求。
    assert sent[0]["role"] == "system"
    assert all(m["role"] != "system" for m in sent[1:])
    assert any(m.get("content") == "最新消息" for m in sent)
