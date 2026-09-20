"""嵌入多端点双活（ai.embedding_base_urls）：按序尝试 + 端点冷却 + 全败才全局熔断。

背景：嵌入原是单端点（140 Ollama bge-m3），140 挂 → 记忆向量召回/翻译语义闸门全部
静默降级。双活后 176（同有 bge-m3）兜底；本文件不触网（假 AsyncOpenAI 客户端注入）。
"""
from __future__ import annotations

import pytest

from src.ai.ai_client import AIClient


class _Cfg:
    config_path = None
    config = {"web_admin": {"site_name": "T"}, "ai": {}}

    def get_ai_config(self):
        return {}


class _FakeEmbClient:
    """最小 AsyncOpenAI.embeddings 替身：fail=True 恒抛；否则回单位向量。"""

    def __init__(self, *, fail: bool = False, vec=None):
        self.fail = fail
        self.vec = vec or [0.1, 0.2]
        self.calls = 0
        outer = self

        class _Emb:
            async def create(self, *, model, input):  # noqa: A002 - SDK 形参名
                outer.calls += 1
                if outer.fail:
                    raise RuntimeError("endpoint down")

                class _D:
                    embedding = outer.vec

                class _R:
                    data = [_D() for _ in input]

                return _R()

        self.embeddings = _Emb()


def _client_with(pairs) -> AIClient:
    c = AIClient(_Cfg())
    c._use_openai_compat = True
    c._embedding_model = "bge-m3"
    c._oa_embed_clients = list(pairs)
    return c


async def test_failover_to_second_endpoint():
    bad = _FakeEmbClient(fail=True)
    good = _FakeEmbClient(vec=[1.0, 0.0])
    c = _client_with([("http://a/v1", bad), ("http://b/v1", good)])
    out = await c.embed(["你好"])
    assert out == [[1.0, 0.0]]
    assert bad.calls == 1 and good.calls == 1
    # 坏端点进冷却 → 立刻再 embed 一次应直接走好端点（坏端点不再被打）
    assert c._embed_url_bad_until.get("http://a/v1", 0) > 0
    out2 = await c.embed(["再见"])
    assert out2 == [[1.0, 0.0]]
    assert bad.calls == 1          # 冷却中排到队尾，好端点先成功
    assert good.calls == 2
    # 单点故障被端点冷却吸收，不触发全局熔断
    assert c._embed_fail_streak == 0
    assert c._embed_unreachable_until == 0.0


async def test_all_endpoints_down_counts_one_global_failure():
    b1, b2 = _FakeEmbClient(fail=True), _FakeEmbClient(fail=True)
    c = _client_with([("http://a/v1", b1), ("http://b/v1", b2)])
    out = await c.embed(["你好"])
    assert out == []
    assert b1.calls == 1 and b2.calls == 1
    # 全端点失败 = 全局 streak 只 +1（不是每端点 +1）
    assert c._embed_fail_streak == 1


async def test_global_breaker_opens_after_streak_and_success_resets():
    bad = _FakeEmbClient(fail=True)
    c = _client_with([("http://a/v1", bad)])
    for _ in range(c._EMBED_FAIL_THRESHOLD):
        c._embed_url_bad_until.clear()   # 绕过端点冷却，模拟跨冷却窗的连续失败
        assert await c.embed(["x"]) == []
    assert c._embed_unreachable_until > 0
    # 熔断窗口内直接短路返回空，不再打端点
    calls_before = bad.calls
    assert await c.embed(["x"]) == []
    assert bad.calls == calls_before
    # 恢复后成功一次 → streak/熔断全清
    c._embed_unreachable_until = 0.0
    c._embed_url_bad_until.clear()
    bad.fail = False
    assert await c.embed(["x"]) == [[0.1, 0.2]]
    assert c._embed_fail_streak == 0


async def test_ordered_puts_cooling_endpoint_last():
    a, b = _FakeEmbClient(), _FakeEmbClient()
    c = _client_with([("http://a/v1", a), ("http://b/v1", b)])
    assert [u for u, _ in c._embed_clients_ordered()] == ["http://a/v1", "http://b/v1"]
    c._mark_embed_url_bad("http://a/v1")
    assert [u for u, _ in c._embed_clients_ordered()] == ["http://b/v1", "http://a/v1"]


async def test_no_dedicated_endpoints_falls_back_to_chat_client():
    chat = _FakeEmbClient(vec=[0.5])
    c = _client_with([])
    c._oa_client = chat
    out = await c.embed(["hi"])
    assert out == [[0.5]]


def test_init_parses_embedding_base_urls(monkeypatch):
    """embedding_base_urls 列表 / embedding_base_url 逗号串两种写法都应展开成多客户端。"""
    import asyncio

    calls = {}

    class _FakeAsyncOpenAI:
        def __init__(self, *, api_key, base_url, timeout, **kw):
            calls.setdefault("urls", []).append(base_url)

    import src.ai.ai_client as mod
    monkeypatch.setattr(mod, "AsyncOpenAI", _FakeAsyncOpenAI)
    monkeypatch.setattr(mod, "OPENAI_SDK_AVAILABLE", True)

    c = AIClient(_Cfg())
    c.timeout = 5
    c.model = "m"

    async def _ok():
        return True
    monkeypatch.setattr(c, "_test_openai_connection", _ok)

    ok = asyncio.run(c._initialize_openai_compatible(
        {"base_url": "http://chat:1/v1",
         "embedding_base_urls": ["http://e1:11434", "http://e2:11434/"]},
        "k"))
    assert ok is True
    assert [u for u, _ in c._oa_embed_clients] == [
        "http://e1:11434/v1", "http://e2:11434/v1"]
    assert c._oa_embed_client is c._oa_embed_clients[0][1]


def test_embed_clients_fast_fail_construction(monkeypatch):
    """嵌入客户端必须「连接快败 + 关 SDK 内建重试」（2026-08-01 生产实锤钉住）。

    旧构造缺 connect 超时且吃 SDK 默认 2 重试：176 宕机时一次 embed = 3 次 TCP
    连接尝试 × Windows ~21s ≈ 65s（[smart_reply] gen=68269 实测），端点 60s 冷却
    过期后每分钟再吃一轮。主/兜底客户端 2026-07 已修同款，嵌入是第三处遗漏——
    本门禁按构造参数钉死：max_retries=0 + connect 短超时 + 读超时有界。"""
    import asyncio

    built = []

    class _FakeAsyncOpenAI:
        def __init__(self, *, api_key, base_url, timeout, max_retries=2, **kw):
            built.append({"base_url": base_url, "timeout": timeout,
                          "max_retries": max_retries})

    import src.ai.ai_client as mod
    monkeypatch.setattr(mod, "AsyncOpenAI", _FakeAsyncOpenAI)
    monkeypatch.setattr(mod, "OPENAI_SDK_AVAILABLE", True)

    c = AIClient(_Cfg())
    c.timeout = 60
    c.model = "m"

    async def _ok():
        return True
    monkeypatch.setattr(c, "_test_openai_connection", _ok)

    ok = asyncio.run(c._initialize_openai_compatible(
        {"base_url": "http://chat:1/v1",
         "embedding_base_urls": ["http://e1:11434", "http://e2:11434"]},
        "k"))
    assert ok is True
    emb = [b for b in built if "e1" in b["base_url"] or "e2" in b["base_url"]]
    assert len(emb) == 2
    for b in emb:
        assert b["max_retries"] == 0, "嵌入客户端必须关 SDK 内建重试（调用方自有端点轮询）"
        to = b["timeout"]
        try:
            import httpx
            assert isinstance(to, httpx.Timeout), "应为 httpx.Timeout（带 connect 分量）"
            assert to.connect is not None and float(to.connect) <= 5.0, \
                "connect 必须 ≤5s 快败（死端点代价从 ~65s 收到 ~5s）"
            assert to.read is not None and float(to.read) <= 30.0, \
                "读超时须有界（bge-m3 热态亚秒级，别继承 60s 大读超时）"
        except ImportError:
            assert float(to) <= 30.0


# ── 熔断日志可读性（B126 排障教训，2026-08-28）───────────────────────────────
# 端点回整页 HTML（网关缺路由的 404 页 / 反代 504 页）时，SDK 把整页塞进异常，
# 于是每条熔断记录都是几 KB 的 `<!DOCTYPE html>…`：诊断包被撑大，而「这是网页
# 不是 JSON」这个关键判据反倒要人工翻。压成一行 + 点名端点。

def test_embed_exc_brief_strips_html_and_truncates():
    html = ('Error code: 404 - <!DOCTYPE html><html lang="zh-CN"><head>'
            '<title>404</title></head><body><div>' + "x" * 4000 + "</div></body></html>")
    brief = AIClient._embed_exc_brief(Exception(html))
    assert len(brief) <= AIClient._EMBED_EXC_BRIEF_CHARS + 1
    assert "HTML" in brief, "必须点明「响应体是网页」——这就是路由缺失的判据"
    assert "<html" not in brief.lower() and "<div" not in brief.lower(), "标签须剥掉"


def test_embed_exc_brief_keeps_connection_errors_readable():
    brief = AIClient._embed_exc_brief(OSError("[WinError 10060] connect timed out"))
    assert "OSError" in brief and "10060" in brief
    assert "HTML" not in brief, "连接类失败不该被误标成网页响应"
    assert AIClient._embed_exc_brief(None) == "unknown"


async def test_embed_failure_log_names_the_endpoints(caplog):
    """熔断那条 WARNING 必须带端点清单：诊断包里才分得清「哪个端点在坏」。"""
    import logging

    bad_a = _FakeEmbClient(fail=True)
    bad_b = _FakeEmbClient(fail=True)
    c = _client_with([("http://e1:11434/v1", bad_a), ("http://e2:11434/v1", bad_b)])
    with caplog.at_level(logging.WARNING, logger=c.logger.name):
        for _ in range(AIClient._EMBED_FAIL_THRESHOLD):
            c._embed_url_bad_until.clear()   # 每轮都真打两个端点
            assert await c.embed(["你好"]) == []
    warned = [r.getMessage() for r in caplog.records if "熔断" in r.getMessage()]
    assert warned, "连败达阈值必须打一条 WARNING"
    assert "e1:11434" in warned[-1] and "e2:11434" in warned[-1]
