# -*- coding: utf-8 -*-
"""本地链 prompt-inspect 留痕 + 前缀缓存读数（2026-09-18 N4）。

此前只有云主链调 ``_trace_prompt``：173 实际收到什么、vLLM prefix cache 命中多少，
从来没人量——「人设块分层值不值」没有读数。钉住：
- 本地出话后 prompt_trace 有一条 purpose=local_primary/local_fallback 的留痕，
  messages 是**裁剪后实发**的那份，budget 带 num_ctx / max_tokens_sent / lane=local；
- usage 里 ``prompt_tokens_details.cached_tokens`` 被读成 cache_hit_tokens，进 by_lane 统计；
- 云链留痕 purpose 为空 → lane=cloud，两边不混；
- 本地返回空也留痕（ok=False），便于查「173 回了空」。
"""
from __future__ import annotations

from src.ai import prompt_trace
from src.ai.ai_client import AIClient


class _FakeChatClient:
    def __init__(self, *, fail=False, reply="ok", prompt_tokens=1200, cached=900):
        self.fail = fail
        self.reply = reply
        self.last_kw = None
        self.base_url = "http://192.168.0.173:8001/v1"
        outer = self

        class _Msg:
            def __init__(self, content):
                self.content = content
                self.model_extra = {}

        class _Choice:
            def __init__(self, content):
                self.message = _Msg(content)

        class _Det:
            cached_tokens = cached

        class _Usage:
            # 类体里 ``prompt_tokens = prompt_tokens`` 会把名字变成类局部，
            # RHS 在赋值前查找 → NameError。用构造器吃外层形参。
            def __init__(self, pt, details):
                self.prompt_tokens = pt
                self.completion_tokens = 30
                self.prompt_tokens_details = details

        class _Resp:
            def __init__(self, content):
                self.choices = [_Choice(content)]
                self.usage = _Usage(prompt_tokens, _Det())

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
    c._fb_num_ctx = 24_576
    c.max_tokens = 1024
    return c


async def test_local_fallback_records_prompt_trace_with_cache_hit():
    prompt_trace.reset()
    fb = _FakeChatClient(reply="出话", prompt_tokens=1200, cached=900)
    c = _client(_FakeChatClient(fail=True), fb)
    hist = [{"role": "user", "content": "历史一"}, {"role": "assistant", "content": "答一"}]
    out = await c._generate_reply_openai_compat(
        "最新消息", context={"reply_lang": "zh", "request_id": "r-1"}, conversation_history=hist)
    assert out == "出话"
    ents = prompt_trace.list_entries(limit=5)
    loc = [e for e in ents if e.get("purpose") == "local_fallback"]
    assert loc, ents
    e = loc[0]
    assert e["model"] == "chatx" and e["host"].startswith("192.168.0.173")
    assert e["usage"]["prompt_tokens"] == 1200 and e["usage"]["cache_hit_tokens"] == 900
    assert e["budget"]["num_ctx"] == 24_576 and e["budget"]["lane"] == "local"
    assert e["budget"]["max_tokens_sent"] == fb.last_kw["max_tokens"]
    assert e["budget"]["billed_prompt"] == 1200
    full = prompt_trace.get_entry(e["seq"])
    # 留的是实发 messages：system 合并成首位单条，历史与当前消息都在
    assert full["system"] and "历史一" in "\n".join(m["content"] for m in full["messages"])
    cs = prompt_trace.cache_stats()
    assert cs["by_lane"]["local_fallback"]["hit_ratio"] == 0.75
    assert "cloud" not in cs["by_lane"]                  # 云链失败没出话 → 不计
    prompt_trace.reset()


async def test_local_primary_purpose_and_empty_reply_traced():
    prompt_trace.reset()
    fb = _FakeChatClient(reply="", prompt_tokens=800, cached=0)
    c = _client(_FakeChatClient(fail=True), fb)
    out = await c._try_local_fallback_chat(
        [{"role": "system", "content": "人设"}, {"role": "user", "content": "在吗"}],
        0.7, 512, {"reply_lang": "zh"}, "r-2", as_primary=True)
    assert out is None
    ents = prompt_trace.list_entries(limit=5)
    assert ents and ents[0]["purpose"] == "local_primary" and ents[0]["ok"] is False
    # 失败不进缓存统计
    assert prompt_trace.cache_stats()["calls"] == 0
    prompt_trace.reset()
