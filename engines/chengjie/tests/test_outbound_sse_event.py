# -*- coding: utf-8 -*-
"""P2-2 出站镜像 SSE 事件（outbound_message）门禁。

链路：orch.send/send_media、官方渠道 inbox_mirror、A 线 TG 陪伴镜像、web 坐席出站
  → emit_incoming/ingest_incoming(direction="out") → 新插入才 publish("outbound_message")
  → SSE 白名单放行 → unified_inbox 前端「刷新线程/列表预览但不加未读」
  → 选中会话兜底轮询 10s→30s（P1-3 分档表随之更新）。

不变量：
  1) 出站新插入 → 恰好一条 outbound_message（payload 带 conversation_id/preview/direction）；
  2) 同 msg_id 二次落库（编排器镜像 vs worker fromMe 回显）→ 不重复发事件；
  3) 入站路径行为不变（inbox_message，绝不发 outbound_message）；
  4) SSE 白名单登记 + 前端消费三件套（handler / bumpUnread:false / 30s 档）静态可见；
  5) web 通道出站同样切到 outbound_message。
"""
from __future__ import annotations

import pathlib
import types

import pytest

from src.inbox.store import InboxStore
from src.integrations import protocol_bridge as pb
from src.integrations.shared.event_bus import get_event_bus

_REPO = pathlib.Path(__file__).resolve().parents[1]
_INBOX_TPL = _REPO / "src/web/templates/unified_inbox.html"


@pytest.fixture()
def bus_queue():
    bus = get_event_bus()
    q = bus.subscribe()
    yield q
    bus.unsubscribe(q)


def _drain(q, etype: str):
    """取队列里指定类型的事件（跳过其他测试/组件顺带发布的无关事件）。"""
    out = []
    while True:
        try:
            evt = q.get_nowait()
        except Exception:
            break
        if evt.get("type") == etype:
            out.append(evt)
    return out


# ── 后端：发布语义 ─────────────────────────────────────────────────────────────

def test_outbound_ingest_publishes_event(tmp_path, bus_queue):
    store = InboxStore(tmp_path / "inbox.db")
    cid = pb.ingest_incoming(
        store, platform="telegram", account_id="acc1", chat_key="123",
        name="Alice", text="AI 自动回复内容", ts=100, msg_id="out-1",
        direction="out",
    )
    evts = _drain(bus_queue, "outbound_message")
    assert len(evts) == 1
    d = evts[0]["data"]
    assert d["conversation_id"] == cid == "telegram:acc1:123"
    assert d["direction"] == "out"
    assert d["platform"] == "telegram"
    assert d["chat_key"] == "123"
    assert "AI 自动回复内容" in d["preview"]


def test_outbound_duplicate_msgid_publishes_once(tmp_path, bus_queue):
    """编排器镜像与 worker fromMe 回显同 msg_id 落同键：第二次无新插入 → 不再发事件。"""
    store = InboxStore(tmp_path / "inbox.db")
    for _ in range(2):
        pb.ingest_incoming(
            store, platform="whatsapp", account_id="a", chat_key="999",
            text="reply", ts=50, msg_id="dup-1", direction="out",
        )
    assert len(_drain(bus_queue, "outbound_message")) == 1


def test_inbound_ingest_keeps_inbox_message_semantics(tmp_path, bus_queue):
    """入站：inbox_message 照发（publish_events=True 路径），绝不发 outbound_message。"""
    store = InboxStore(tmp_path / "inbox.db")
    pb.ingest_incoming(
        store, platform="telegram", account_id="acc1", chat_key="321",
        name="Bob", text="客户来消息", ts=100, msg_id="in-1", direction="in",
    )
    assert len(_drain(bus_queue, "inbox_message")) == 1
    assert _drain(bus_queue, "outbound_message") == []


def test_outbound_media_only_message_publishes(tmp_path, bus_queue):
    """无文本纯媒体出站（语音/图片）也应发事件（预览用占位符）。"""
    store = InboxStore(tmp_path / "inbox.db")
    pb.ingest_incoming(
        store, platform="telegram", account_id="acc1", chat_key="555",
        text="", ts=100, msg_id="m-9", direction="out",
        media_type="photo", media_ref="/static/outbound/x.jpg",
    )
    evts = _drain(bus_queue, "outbound_message")
    assert len(evts) == 1
    assert evts[0]["data"]["preview"]  # 占位符（如「[图片]」）非空


def test_web_channel_outbound_uses_new_type(bus_queue):
    """web 通道坐席出站：_publish_inbox_event(direction=out) → outbound_message。"""
    from src.web.routes.web_chat_routes import _publish_inbox_event
    svc = types.SimpleNamespace(account_id="web-main")
    _publish_inbox_event("web:web-main:v1", svc, "v1", "hello", direction="out")
    evts = _drain(bus_queue, "outbound_message")
    assert len(evts) == 1
    assert evts[0]["data"]["platform"] == "web"
    # 入站方向仍走 inbox_message
    _publish_inbox_event("web:web-main:v1", svc, "v1", "hi", direction="in")
    assert len(_drain(bus_queue, "inbox_message")) == 1


# ── SSE 白名单 + 前端消费（静态接线） ─────────────────────────────────────────

def test_sse_whitelist_includes_outbound():
    from src.web.routes.unified_inbox_realtime_routes import _SSE_EVENT_TYPES
    assert "outbound_message" in _SSE_EVENT_TYPES
    assert "inbox_message" in _SSE_EVENT_TYPES


def test_outbound_not_in_notif_center():
    """出站事件不该进 P24 通知中心（坐席不需要被自己/AI 的出站打扰）。"""
    import src.web.routes.unified_inbox_realtime_routes as rt
    notif = getattr(rt, "_NOTIF_EVENT_TYPES", None)
    if notif is not None:
        assert "outbound_message" not in notif


def test_frontend_consumes_outbound_event():
    txt = _INBOX_TPL.read_text(encoding="utf-8", errors="replace")
    # 1) SSE handler 分支存在
    assert "t==='outbound_message'" in txt
    # 2) 列表补丁不加未读
    assert "_patchChatInList(data, {bumpUnread:false})" in txt
    # 3) 选中会话 SSE 健康档已放宽到 30s（出站事件补位后才允许）
    assert "sel?30000:45000" in txt
    # 4) _patchChatInList 支持 opts（bumpUnread 语义落地而非只传参）
    assert "opts.bumpUnread!==false" in txt
