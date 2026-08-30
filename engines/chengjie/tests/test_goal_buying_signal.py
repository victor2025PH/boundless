# -*- coding: utf-8 -*-
"""买家信号 → 限时目标提示 门禁（P1 2026-08-29）。

业界基准：signal 出现后 24–48h 内行动的约见率 8–15%，过窗即凉。本功能只做
**提示**（无活跃目标的会话近窗在问价 → 目标卡出「趁热开今天收口」行），
不自动建目标、不进 prompt、不发消息。误报会让坐席不再信提示——负例优先。
"""

from __future__ import annotations

import time

import pytest
# Request 必须在模块全局可见：本文件开了 `from __future__ import annotations`，
# 注解按字符串延迟解析（按函数 __globals__ 找名字）——只在 helper 里局部导入
# 会让 FastAPI 把 `request: Request` 当成名为 request 的查询参数（422）。
from fastapi import FastAPI, Request

from src.companion.goals.buying_signal import (
    detect_buying_signal,
    scan_recent_inbound,
)


@pytest.mark.parametrize("text", [
    "这个多少钱？",
    "怎么收费的",
    "怎么买啊",
    "付款方式有哪些",
    "发个链接我看看",
    "在哪里买",
    "有没有优惠",
    "便宜点行不行",
    "how much is it?",
    "what's the price",
    "how do i subscribe",
    "send me the link please",
    "any discount?",
])
def test_detect_positive(text):
    assert detect_buying_signal(text) != ""


@pytest.mark.parametrize("text", [
    "",
    None,
    "今天天气不错",
    "我最近在忙项目",
    "你吃饭了吗",
    "那家餐厅有点贵",              # 「贵」泛化闲聊不算——词表只认购买动作
    "much better now",
    "价值观很重要",                 # 「价格」词内子串不误命中（价值≠价格）
])
def test_detect_negative(text):
    assert detect_buying_signal(text) == ""


def test_scan_recent_inbound_window_and_direction():
    now = time.time()
    msgs = [
        {"direction": "out", "content": "报价我发你了", "ts": now - 60},   # 我方说的不算
        {"direction": "in", "content": "怎么买？", "ts": now - 120},
        {"direction": "in", "content": "多少钱", "ts": now - 3 * 86400},  # 过窗即凉
    ]
    hit = scan_recent_inbound(msgs, now=now)
    assert hit is not None
    assert hit["kind"] == "buying" and hit["v"] == "怎么买"
    assert abs(hit["ts"] - (now - 120)) < 1
    # 全部过窗 / 空表 / 坏行 → None
    assert scan_recent_inbound(
        [{"direction": "in", "content": "多少钱", "ts": now - 3 * 86400}],
        now=now) is None
    assert scan_recent_inbound([], now=now) is None
    assert scan_recent_inbound([{"bad": "row"}], now=now) is None
    assert scan_recent_inbound(None, now=now) is None


def test_scan_picks_latest_hit():
    now = time.time()
    msgs = [
        {"direction": "in", "content": "多少钱", "ts": now - 3600},
        {"direction": "in", "content": "怎么付款", "ts": now - 60},
    ]
    hit = scan_recent_inbound(msgs, now=now)
    assert hit["v"] == "怎么付款"


# ── 路由接线：无目标 + 近窗信号 → signal_hint；有目标 → 不附 ────────────────


class _FakeInbox:
    def __init__(self, msgs):
        self._msgs = msgs

    def list_recent_messages(self, conversation_id, limit=30):
        return list(self._msgs)


def _client_with_inbox(monkeypatch, msgs):
    """复用 test_goal_routes 的自建 app 模式 + 假 inbox 经 protocol_bridge。"""
    from types import SimpleNamespace

    from starlette.testclient import TestClient

    import src.integrations.protocol_bridge as pb
    from src.web.routes.goal_routes import register_goal_routes

    inbox = _FakeInbox(msgs)
    monkeypatch.setattr(pb, "_inbox_store_getter", lambda: inbox)
    cfg = {"companion": {"goals": {"enabled": True, "db_path": ":memory:"}}}
    app = FastAPI()

    def auth_dep(request: Request) -> None:
        request.scope["session"] = {"role": "", "user": "tester"}

    register_goal_routes(
        app, auth_dep, SimpleNamespace(config=cfg, config_path=None))
    return TestClient(app)


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    import src.integrations.protocol_bridge as pb
    import src.utils.companion_context as cc
    from src.companion.goals.store import reset_goal_store

    monkeypatch.setattr(cc, "_REL_PROVIDERS", {})
    monkeypatch.setattr(pb, "_inbox_store_getter", None)
    reset_goal_store()
    yield
    reset_goal_store()


def test_route_attaches_hint_only_without_goal(monkeypatch):
    now = time.time()
    client = _client_with_inbox(
        monkeypatch,
        [{"direction": "in", "content": "这个怎么收费？", "ts": now - 300}])
    conv = "telegram:a1:sig1"
    d = client.get(
        f"/api/goals/for-conversation?conversation_id={conv}").json()
    assert d["goal"] is None
    sig = d.get("signal_hint")
    assert sig and sig["kind"] == "buying" and sig["v"] == "怎么收费"
    # 建了目标 → 活跃路径不附提示（会话已有推进节奏，重复提示=噪音）
    r = client.post("/api/goals", json={
        "template": "custom", "conversation_id": conv, "pace": "today",
        "params": {"note": "今天收口"}, "deadline_days": 8 / 24.0})
    assert r.status_code == 200
    d2 = client.get(
        f"/api/goals/for-conversation?conversation_id={conv}").json()
    assert d2["goal"] is not None
    assert "signal_hint" not in d2


def test_route_no_hint_when_no_signal_or_no_inbox(monkeypatch):
    now = time.time()
    client = _client_with_inbox(
        monkeypatch,
        [{"direction": "in", "content": "晚安", "ts": now - 300}])
    d = client.get(
        "/api/goals/for-conversation?conversation_id=telegram:a1:sig2").json()
    assert "signal_hint" not in d

    import src.integrations.protocol_bridge as pb
    monkeypatch.setattr(pb, "_inbox_store_getter", None)   # 无 inbox 也不崩
    d2 = client.get(
        "/api/goals/for-conversation?conversation_id=telegram:a1:sig3").json()
    assert d2["goal"] is None and "signal_hint" not in d2
