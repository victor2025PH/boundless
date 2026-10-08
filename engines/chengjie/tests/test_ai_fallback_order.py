"""P1-2 主备 AI 真正可用（2026-10-08）：备用链顺序可配 + 启动探针 + /health 可观测。

钉住：
1. ``ai.fallback_order`` 解析（别名/去重/未知项/漏写补尾），缺省「池 → 本地」逐字节不变；
2. 顺序 ``[local, key_pool]``（智聊推荐：chatx → 第二推理端 → 云端池）在三个入口都生效：
   主链失败、熔断开路、云端冷却；
3. 主模型不可达时 replybus ``decide`` 仍能出话（走第二推理端），且耗时远低于 12s；
4. ``probe_backup_lanes`` 只打 ``models.list``（**绝不 chat**），``backup_status`` 冗余结论
   ok/unknown/down/none，全部不可达时 ERROR + 主机告警；不含地址/密钥；
5. ``/health`` 的 ``ai_stats.backup`` 带出状态。

全程假客户端注入，零真网络（不触 173:8001）。
"""
from __future__ import annotations

import asyncio
import logging
import time
from types import SimpleNamespace

import pytest
from fastapi import Request  # 模块级：from __future__ annotations 下 FastAPI 按模块全局解析注解

from src.ai.ai_client import AIClient, DEFAULT_FALLBACK_ORDER, parse_fallback_order
from src.utils import host_alert


class _Cfg:
    config_path = None
    config = {"web_admin": {"site_name": "T"}, "ai": {}}

    def get_ai_config(self):
        return {}


class _Msg:
    def __init__(self, content):
        self.content = content
        self.model_extra = {}


class _Resp:
    def __init__(self, content):
        self.choices = [SimpleNamespace(message=_Msg(content))]
        self.usage = SimpleNamespace(prompt_tokens=10, completion_tokens=5)


class _Fake:
    """AsyncOpenAI 替身：chat 调用计数；models.list 可设成功/失败/挂起。"""

    def __init__(self, *, fail=None, reply="ok", list_mode="ok",
                 base_url="http://lan-2:11434/v1"):
        self.fail, self.reply, self.base_url = fail, reply, base_url
        self.calls = 0
        self.list_calls = 0
        outer = self

        class _Completions:
            async def create(self, **kw):
                outer.calls += 1
                outer.last_kw = kw
                if outer.fail is not None:
                    raise outer.fail
                return _Resp(outer.reply)

        class _Models:
            async def list(self):
                outer.list_calls += 1
                if list_mode == "fail":
                    raise ConnectionError("refused")
                if list_mode == "hang":
                    await asyncio.sleep(30)
                return SimpleNamespace(data=[])

        self.chat = SimpleNamespace(completions=_Completions())
        if list_mode is not None:
            self.models = _Models()


def _client(primary, *, order=None, pool=None, local=None) -> AIClient:
    c = AIClient(_Cfg())
    c._use_openai_compat = True
    c._oa_client = primary
    c.model = "chatx"
    c.timeout = 5
    c._cb_enabled = False
    if order is not None:
        c._fallback_order = list(order)
    if pool is not None:
        c._pool_entries = [{"name": "cloud-1", "client": pool, "model": "m",
                            "label": "m @ cloud (cloud-1)", "bad_until": 0.0}]
    if local is not None:
        c._fb_client = local
        c._fb_model = "qwen-lan"
    return c


@pytest.fixture(autouse=True)
def _quiet_alerts(monkeypatch):
    seen = []
    monkeypatch.setattr(host_alert, "notify_key_failure", lambda *a, **k: True)
    monkeypatch.setattr(host_alert, "notify_cloud_outage", lambda *a, **k: True)
    monkeypatch.setattr(host_alert, "notify_host",
                        lambda title, message, **kw: seen.append((title, kw.get("key"))) or True)
    return seen


# ── 1. 解析 ─────────────────────────────────────────────────────────────────

def test_parse_default_is_legacy_pool_then_local():
    assert DEFAULT_FALLBACK_ORDER == ("key_pool", "local")
    assert parse_fallback_order(None) == (["key_pool", "local"], [])
    assert parse_fallback_order([]) == (["key_pool", "local"], [])


def test_parse_aliases_dedupe_unknown_and_fill_missing():
    assert parse_fallback_order(["local", "key_pool"]) == (["local", "key_pool"], [])
    assert parse_fallback_order("LAN, cloud") == (["local", "key_pool"], [])
    assert parse_fallback_order(["second", "local", "pool"]) == (["local", "key_pool"], [])
    order, unknown = parse_fallback_order(["local", "gemini"])
    assert order == ["local", "key_pool"] and unknown == ["gemini"]
    # 漏写的 lane 补尾：顺序可配，但不悄悄丢备用链
    assert parse_fallback_order(["local"])[0] == ["local", "key_pool"]
    assert parse_fallback_order(42)[0] == ["key_pool", "local"]


def test_initialize_reads_fallback_order(monkeypatch):
    import src.ai.ai_client as mod

    class _FakeAsyncOpenAI:
        def __init__(self, **kw):
            self.base_url = kw.get("base_url")
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=None))

    monkeypatch.setattr(mod, "AsyncOpenAI", _FakeAsyncOpenAI)
    monkeypatch.setattr(mod, "OPENAI_SDK_AVAILABLE", True)
    c = AIClient(_Cfg())
    c.timeout = 5

    async def _ok():
        return True
    monkeypatch.setattr(c, "_test_openai_connection", _ok)
    cfg = {"base_url": "http://chatx.lan:8001/v1", "api_key": "x" * 12, "model": "chatx",
           "fallback": {"enabled": True, "base_url": "http://lan-2:11434", "model": "qwen-lan"},
           "fallback_order": ["local", "key_pool"]}
    assert asyncio.run(c._initialize_openai_compatible(cfg, cfg["api_key"])) is True
    assert c._fallback_order == ["local", "key_pool"]
    assert c.get_stats()["fallback_order"] == ["local", "key_pool"]


# ── 2. 三个入口按顺序走 ─────────────────────────────────────────────────────

async def test_default_order_unchanged_pool_first():
    primary = _Fake(fail=Exception("Connection error."))
    pool, local = _Fake(reply="云池"), _Fake(reply="第二推理端")
    c = _client(primary, pool=pool, local=local)
    out = await c._generate_reply_openai_compat("hi", context={"reply_lang": "zh"})
    assert out == "云池" and local.calls == 0


async def test_local_first_order_primary_down():
    primary = _Fake(fail=Exception("Connection error."))
    pool, local = _Fake(reply="云池"), _Fake(reply="第二推理端出话")
    c = _client(primary, order=["local", "key_pool"], pool=pool, local=local)
    out = await c._generate_reply_openai_compat("在吗", context={"reply_lang": "zh"})
    assert out == "第二推理端出话"
    assert primary.calls == 2 and local.calls == 1 and pool.calls == 0
    # 第二推理端拿到同一份 messages（人设/上下文不丢）
    assert {"role": "user", "content": "在吗"} in local.last_kw["messages"]


async def test_local_first_falls_through_to_pool():
    primary = _Fake(fail=Exception("Connection error."))
    local = _Fake(fail=Exception("lan down"))
    pool = _Fake(reply="云池兜底")
    c = _client(primary, order=["local", "key_pool"], pool=pool, local=local)
    out = await c._generate_reply_openai_compat("hi", context={"reply_lang": "zh"})
    assert out == "云池兜底" and local.calls == 1 and pool.calls == 1


async def test_breaker_open_respects_order():
    primary = _Fake(reply="不该被调")
    pool, local = _Fake(reply="云池"), _Fake(reply="本地")
    c = _client(primary, order=["local", "key_pool"], pool=pool, local=local)
    c._cb_enabled = True
    c._cb_open_until = time.time() + 60
    out = await c._generate_reply_openai_compat("hi", context={"reply_lang": "zh"})
    assert out == "本地" and primary.calls == 0 and pool.calls == 0


async def test_cloud_cooling_respects_order(monkeypatch):
    primary = _Fake(reply="不该被调")
    pool, local = _Fake(reply="云池"), _Fake(reply="本地")
    c = _client(primary, order=["local", "key_pool"], pool=pool, local=local)
    monkeypatch.setattr(c, "_lane_should_skip", lambda lane: lane == "cloud")
    noted = []
    monkeypatch.setattr(c, "_lane_note_ok", lambda lane: noted.append(lane))
    out = await c._generate_reply_openai_compat("hi", context={"reply_lang": "zh"})
    assert out == "本地" and primary.calls == 0 and pool.calls == 0
    assert "local" in noted and "pool" not in noted


async def test_all_down_returns_none_no_canned():
    c = _client(_Fake(fail=Exception("x")), order=["local", "key_pool"],
                pool=_Fake(fail=Exception("y")), local=_Fake(fail=Exception("z")))
    assert await c._generate_reply_openai_compat("hi", context={"reply_lang": "zh"}) is None


# ── 3. decide 端到端：主模型不可达仍出话 ─────────────────────────────────────

def test_replybus_decide_still_answers_when_primary_unreachable():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from src.web.routes.replybus_routes import register_replybus_routes

    primary = _Fake(fail=ConnectionError("chatx unreachable"))
    local = _Fake(reply="Hello po! Andito ako, ano maitutulong ko?")
    ai = _client(primary, order=["local", "key_pool"], local=local)

    sm = SimpleNamespace(
        ai_client=ai,
        _recognize_intent=lambda text: "general",
        get_strategy_for_intent=lambda intent, uid: ({}, None),
    )
    app = FastAPI()

    def _auth(request: Request):
        return True

    register_replybus_routes(app, api_auth=_auth, config_manager=None)
    app.state.skill_manager = sm
    app.state.ai_client = ai
    msg = {"platform": "whatsapp", "account": "acct_test", "external_id": "wa:639000000000",
           "text": "hello po", "msg_id": "m1", "session_id": "s1",
           "context_hint": {"lang": "en"}}
    t0 = time.time()
    r = TestClient(app).post("/api/replybus/decide", json={"message": msg})
    elapsed = time.time() - t0
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["action"] in ("draft", "send"), body
    assert "Andito ako" in body["text"]
    # 语言守卫可能再借第二推理端改写一次（期望语种与回复不符时），故 >=1
    assert local.calls >= 1 and primary.calls >= 1
    assert elapsed < 12.0


# ── 4. 探针与状态 ────────────────────────────────────────────────────────────

async def test_probe_only_lists_models_never_chats():
    local = _Fake(reply="should not be used")
    c = _client(_Fake(), order=["local", "key_pool"], local=local)
    st = await c.probe_backup_lanes()
    assert local.list_calls == 1 and local.calls == 0
    assert st["lanes"]["local"]["reachable"] is True
    snap = c.backup_status()
    assert snap["redundancy"] == "ok"
    assert snap["order"] == ["primary", "local", "key_pool"]
    assert [ln["lane"] for ln in snap["lanes"]] == ["local", "key_pool"]
    assert snap["lanes"][1]["configured"] is False


async def test_probe_all_down_alerts(caplog, _quiet_alerts):
    local = _Fake(list_mode="fail")
    c = _client(_Fake(), order=["local", "key_pool"], local=local)
    caplog.set_level(logging.ERROR)
    await c.probe_backup_lanes()
    snap = c.backup_status()
    assert snap["redundancy"] == "down"
    assert snap["lanes"][0]["error"] == "ConnectionError"
    assert any(k == "ai_backup_down" for _t, k in _quiet_alerts)
    assert any("备用链全部不可达" in r.getMessage() for r in caplog.records)


async def test_probe_timeout_is_bounded():
    c = _client(_Fake(), local=_Fake(list_mode="hang"))
    t0 = time.time()
    await c.probe_backup_lanes(timeout=0.2, alert=False)
    assert time.time() - t0 < 2.0
    assert c.backup_status()["lanes"][1]["reachable"] is False   # 缺省序 local 在第二位


async def test_recent_success_beats_failed_probe():
    local = _Fake(list_mode="fail")
    c = _client(_Fake(), local=local)
    await c.probe_backup_lanes(alert=False)
    assert c.backup_status()["redundancy"] == "down"
    c._last_fb_ok_ts = time.time()
    assert c.backup_status()["redundancy"] == "ok"


async def test_no_backup_configured_is_none_and_warns(caplog, _quiet_alerts):
    c = _client(_Fake())
    caplog.set_level(logging.WARNING)
    await c.probe_backup_lanes()
    assert c.backup_status()["redundancy"] == "none"
    assert not _quiet_alerts            # 单点只记 WARNING，不弹窗
    assert any("单点" in r.getMessage() for r in caplog.records)


async def test_probe_without_models_api_is_unknown():
    c = _client(_Fake(), local=_Fake(list_mode=None))
    await c.probe_backup_lanes(alert=False)
    assert c.backup_status()["redundancy"] == "unknown"


async def test_local_mode_probe_skipped():
    local = _Fake()
    c = _client(_Fake(), local=local)
    c._primary_mode = "local"
    st = await c.probe_backup_lanes()
    assert "skipped" in st and local.list_calls == 0
    assert c.backup_status()["redundancy"] == "n/a"
    c._primary_mode = "local_only"
    assert c.backup_status()["redundancy"] == "none"


def test_backup_status_has_no_urls_or_keys():
    c = _client(_Fake(), local=_Fake(), pool=_Fake())
    c._pool_entries[0]["api_key_marker"] = "sk-should-not-leak"
    blob = repr(c.backup_status())
    for needle in ("http", "11434", "lan-2", "sk-", "cloud-1"):
        assert needle not in blob, needle


# ── 5. /health ───────────────────────────────────────────────────────────────

def test_health_exposes_backup_status(app, client):
    ai = _client(_Fake(), order=["local", "key_pool"], local=_Fake())
    app.state.ai_client = ai
    r = client.get("/health")
    assert r.status_code == 200
    backup = r.json()["ai_stats"]["backup"]
    assert backup["order"] == ["primary", "local", "key_pool"]
    assert backup["redundancy"] in ("unknown", "ok")
    assert "http" not in repr(backup)


def test_health_ignores_non_aiclient_state_double(app, client):
    from unittest.mock import MagicMock
    app.state.ai_client = MagicMock()
    r = client.get("/health")
    assert r.status_code == 200   # 替身不冒充 AIClient，/health 不因序列化 500
