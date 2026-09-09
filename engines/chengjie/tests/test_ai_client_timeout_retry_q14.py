"""Q-14 #262 A/B（D-Q10）：ai.timeout 默认 60 对齐官网网关 55s 预算；连接类错误重试 1 次、
读超时不重试；prompt token 预算（历史 → few-shot → 注入长度）；主链失败落
``[ai] fail conv=… reason=…`` 一行并可由起草侧 ``pop_last_fail`` 消费。本文件不触网。
"""
from __future__ import annotations

import logging

import httpx
import pytest

from src.ai.ai_client import AIClient


class _Cfg:
    config_path = None
    config = {"web_admin": {"site_name": "T"}, "ai": {}}

    def __init__(self, ai=None):
        self._ai = dict(ai or {})

    def get_ai_config(self):
        return dict(self._ai)


class _Msg:
    def __init__(self, content):
        self.content = content
        self.model_extra = {}


class _Choice:
    def __init__(self, content):
        self.message = _Msg(content)
        self.finish_reason = "stop"


class _Usage:
    prompt_tokens = 10
    completion_tokens = 5


class _Resp:
    def __init__(self, content):
        self.choices = [_Choice(content)]
        self.usage = _Usage()


class _FakeChatClient:
    """AsyncOpenAI.chat.completions 替身：``errors`` 依次抛出（None = 正常回答）。"""

    def __init__(self, *, errors=(), reply: str = "ok"):
        self.errors = list(errors)
        self.reply = reply
        self.calls = 0
        self.last_kw = None
        self.base_url = "https://bd2026.cc/api/ai/v1"
        outer = self

        class _Completions:
            async def create(self, **kw):
                outer.calls += 1
                outer.last_kw = kw
                if outer.errors:
                    err = outer.errors.pop(0)
                    if err is not None:
                        raise err
                return _Resp(outer.reply)

        class _Chat:
            completions = _Completions()

        self.chat = _Chat()


def _client(primary: _FakeChatClient) -> AIClient:
    c = AIClient(_Cfg())
    c._use_openai_compat = True
    c._oa_client = primary
    c.model = "deepseek-v4-flash"
    c._cb_enabled = False
    return c


def _read_timeout() -> Exception:
    """openai SDK 的 APITimeoutError 因果链上挂 httpx.ReadTimeout（读侧超时）。"""
    try:
        from openai import APITimeoutError
        e = APITimeoutError(request=httpx.Request("POST", "https://x/v1/chat/completions"))
    except Exception:  # pragma: no cover - SDK 缺席时退化
        e = TimeoutError("Request timed out.")
    e.__cause__ = httpx.ReadTimeout("read timed out")
    return e


def _connect_error() -> Exception:
    try:
        from openai import APIConnectionError
        e = APIConnectionError(request=httpx.Request("POST", "https://x/v1/chat/completions"))
    except Exception:  # pragma: no cover
        e = ConnectionError("Connection error.")
    e.__cause__ = httpx.ConnectError("connection refused")
    return e


# ── 默认值 ────────────────────────────────────────────────────────────────────

def test_default_timeout_is_60_and_config_key_respected():
    """缺席键 → 60（≥ 网关 55s）；显式写 30 的存量照旧 30（基线只补缺席键）。"""
    c = AIClient(_Cfg())
    assert c.timeout == 60
    assert c._prompt_budget_tokens == 6000


async def test_initialize_reads_timeout_default_60(monkeypatch):
    import src.ai.ai_client as mod

    class _FakeAsyncOpenAI:
        def __init__(self, **kw):
            self.kw = kw
            self.base_url = kw.get("base_url", "")

    monkeypatch.setattr(mod, "AsyncOpenAI", _FakeAsyncOpenAI)
    monkeypatch.setattr(mod, "OPENAI_SDK_AVAILABLE", True)

    async def _ok():
        return True

    c = AIClient(_Cfg({"provider": "openai_compatible", "base_url": "http://h/v1", "api_key": "k"}))
    monkeypatch.setattr(c, "_test_openai_connection", _ok)
    assert await c.initialize() is True
    assert c.timeout == 60
    assert c._oa_client.kw["max_retries"] == 0
    to = c._oa_client.kw["timeout"]
    assert getattr(to, "read", None) == 60.0 and getattr(to, "connect", None) == 5.0

    c2 = AIClient(_Cfg({"provider": "openai_compatible", "base_url": "http://h/v1",
                        "api_key": "k", "timeout": 30, "prompt_budget_tokens": 0}))
    monkeypatch.setattr(c2, "_test_openai_connection", _ok)
    assert await c2.initialize() is True
    assert c2.timeout == 30 and c2._prompt_budget_tokens == 0


def test_min_yaml_timeout_60():
    from pathlib import Path
    import yaml
    p = Path(__file__).resolve().parent.parent / "config" / "config.desktop.min.yaml"
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    assert int(data["ai"]["timeout"]) >= 60
    assert int(data["ai"]["prompt_budget_tokens"]) > 0


# ── 错误分类 ──────────────────────────────────────────────────────────────────

def test_classify_errors():
    assert AIClient._classify_ai_error(_read_timeout()) == "timeout"
    assert AIClient._classify_ai_error(_connect_error()) == "connect"
    assert AIClient._classify_ai_error(httpx.ReadTimeout("x")) == "timeout"
    assert AIClient._classify_ai_error(httpx.ConnectTimeout("x")) == "connect"
    assert AIClient._classify_ai_error(ConnectionResetError("reset")) == "connect"

    class _E(Exception):
        status_code = 502

    assert AIClient._classify_ai_error(_E("Bad gateway")) == "gateway_5xx"
    assert AIClient._classify_ai_error(RuntimeError("cloud down")) == "other"
    assert AIClient._should_retry_ai_error(_read_timeout()) is False
    assert AIClient._should_retry_ai_error(_connect_error()) is True


# ── 三例验收：60s 读超时不重试 / 连接错误重试 1 次 / 读超时不重试 ──────────────

async def test_read_timeout_not_retried_returns_none_and_logs_fail(caplog):
    primary = _FakeChatClient(errors=[_read_timeout()])
    c = _client(primary)
    ctx = {"reply_lang": "zh", "conversation_id": "telegram:acc:123", "request_id": "r1"}
    with caplog.at_level(logging.ERROR):
        out = await c._generate_reply_openai_compat("在吗", context=ctx)
    assert out is None
    assert primary.calls == 1            # 读超时：只打一枪
    line = next(r.getMessage() for r in caplog.records if "[ai] fail" in r.getMessage())
    assert "conv=telegram:acc:123" in line and "reason=timeout" in line
    assert "attempt=1" in line and "latency_ms=" in line
    rec = c.pop_last_fail("telegram:acc:123")
    assert rec and rec["reason"] == "timeout" and rec["attempt"] == 1
    assert c.pop_last_fail("telegram:acc:123") is None     # 一次性


async def test_connect_error_retried_once_then_ok(monkeypatch):
    import src.ai.ai_client as mod

    async def _nosleep(_s):
        return None

    monkeypatch.setattr(mod.asyncio, "sleep", _nosleep)
    primary = _FakeChatClient(errors=[_connect_error(), None], reply="好呀")
    c = _client(primary)
    out = await c._generate_reply_openai_compat(
        "在吗", context={"reply_lang": "zh", "conversation_id": "tg:a:1"})
    assert out == "好呀"
    assert primary.calls == 2            # 连接错误：重试 1 次救回
    assert c.pop_last_fail("tg:a:1") is None   # 成功即清


async def test_connect_error_twice_gives_up_with_reason(monkeypatch, caplog):
    import src.ai.ai_client as mod

    async def _nosleep(_s):
        return None

    monkeypatch.setattr(mod.asyncio, "sleep", _nosleep)
    primary = _FakeChatClient(errors=[_connect_error(), _connect_error()])
    c = _client(primary)
    with caplog.at_level(logging.ERROR):
        out = await c._generate_reply_openai_compat(
            "在吗", context={"reply_lang": "zh", "conversation_id": "tg:a:2"})
    assert out is None and primary.calls == 2
    line = next(r.getMessage() for r in caplog.records if "[ai] fail" in r.getMessage())
    assert "reason=connect" in line and "attempt=2" in line


async def test_timeout_after_connect_retry_stops(monkeypatch):
    """第一枪连接错误 → 重试；第二枪读超时 → 到此为止（不会有第三枪）。"""
    import src.ai.ai_client as mod

    async def _nosleep(_s):
        return None

    monkeypatch.setattr(mod.asyncio, "sleep", _nosleep)
    primary = _FakeChatClient(errors=[_connect_error(), _read_timeout()])
    c = _client(primary)
    out = await c._generate_reply_openai_compat("在吗", context={"reply_lang": "zh"})
    assert out is None and primary.calls == 2
    rec = c.pop_last_fail()
    assert rec and rec["reason"] == "timeout"


async def test_empty_reply_twice_reason_empty(caplog):
    primary = _FakeChatClient(reply="")
    c = _client(primary)
    with caplog.at_level(logging.ERROR):
        out = await c._generate_reply_openai_compat("在吗", context={"reply_lang": "zh"})
    assert out is None and primary.calls == 2
    line = next(r.getMessage() for r in caplog.records if "[ai] fail" in r.getMessage())
    assert "reason=empty" in line


# ── prompt 预算 ───────────────────────────────────────────────────────────────

def _msgs_long():
    sys_txt = "【人设】" + "设" * 800
    sys_txt += "\n\n【参考对话示例】\n客户：在吗\n你：在呀" + "例" * 600
    sys_txt += "\n\n【目标注入】" + "注" * 600
    msgs = [{"role": "system", "content": sys_txt}]
    for i in range(20):
        msgs.append({"role": "user", "content": f"旧{i}" + "字" * 100})
        msgs.append({"role": "assistant", "content": f"答{i}" + "字" * 100})
    msgs.append({"role": "user", "content": "最新的问题"})
    return msgs


def test_prompt_budget_untouched_when_under():
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    out, st = AIClient._trim_prompt_to_budget(msgs, 6000)
    assert out == msgs and st["hist"] == 0 and st["fewshot"] == 0 and st["inject_chars"] == 0


def test_prompt_budget_off_when_zero():
    msgs = _msgs_long()
    out, _ = AIClient._trim_prompt_to_budget(msgs, 0)
    assert out is msgs


def test_prompt_budget_trims_history_first():
    msgs = _msgs_long()
    total = sum(AIClient._estimate_msg_tokens(m["content"]) for m in msgs)
    budget = total - 1500              # 只需丢几轮历史即可
    out, st = AIClient._trim_prompt_to_budget(msgs, budget)
    assert st["hist"] > 0 and st["fewshot"] == 0 and st["inject_chars"] == 0
    assert out[0]["content"] == msgs[0]["content"]          # system 原样
    assert out[-1] == {"role": "user", "content": "最新的问题"}
    assert all("旧0" not in str(m["content"]) for m in out)  # 最旧先丢
    assert sum(AIClient._estimate_msg_tokens(m["content"]) for m in out) <= budget
    assert len(msgs) == 42 and len(msgs[0]["content"]) > 2000   # 原 messages 不被污染


def test_prompt_budget_then_fewshot_then_inject():
    msgs = _msgs_long()
    sys_tokens = AIClient._estimate_msg_tokens(msgs[0]["content"])
    # 预算比 system 还小：历史全丢 → few-shot 段丢 → 还超 → 截注入尾部
    budget = sys_tokens - 900
    out, st = AIClient._trim_prompt_to_budget(msgs, budget)
    assert st["hist"] == 40
    assert st["fewshot"] == 1
    assert "【参考对话示例】" not in out[0]["content"]
    assert out[0]["content"].startswith("【人设】")            # 人设主体保住
    assert st["inject_chars"] > 0
    assert out[-1]["content"] == "最新的问题"
    assert [m["role"] for m in out] == ["system", "user"]


async def test_prompt_budget_applied_before_send_and_logged(caplog):
    primary = _FakeChatClient(reply="ok")
    c = _client(primary)
    c._prompt_budget_tokens = 900
    hist = []
    for i in range(30):
        hist.append({"role": "user", "content": f"历史{i}" + "字" * 60})
        hist.append({"role": "assistant", "content": f"答{i}" + "字" * 60})
    with caplog.at_level(logging.INFO):
        out = await c._generate_reply_openai_compat(
            "最新消息", context={"reply_lang": "zh"}, conversation_history=hist)
    assert out == "ok"
    sent = primary.last_kw["messages"]
    assert sum(AIClient._estimate_msg_tokens(m["content"]) for m in sent) <= 900
    assert sent[-1]["content"] == "最新消息"
    line = next(r.getMessage() for r in caplog.records if "[ai] prompt_tokens=" in r.getMessage())
    assert "trimmed=hist:" in line and "budget=900" in line


def test_fewshot_part_detection():
    assert AIClient._is_fewshot_part("【参考对话示例】\n客户：…")
    assert AIClient._is_fewshot_part("Few-shot examples:\n…")
    assert not AIClient._is_fewshot_part("【后台人设定位 · 须遵守】\n…")
    assert not AIClient._is_fewshot_part("")
