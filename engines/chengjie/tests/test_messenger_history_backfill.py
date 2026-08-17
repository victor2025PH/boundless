# -*- coding: utf-8 -*-
"""Messenger 历史回填闭环契约（P1，对齐 Telegram「接入即有上下文」）。

两条链路：
1. ``/api/internal/protocol/thread-history``——messenger-web 首连回填（登录后对
   top-N 线程各读末尾若干条推上来）。核心不变量：
   - **只对空会话写入**（Messenger DOM 无稳定消息 id，跨次回填没法按 id 去重；
     「空会话才收」让重启/重连后的重复回填天然幂等）；
   - 空会话判定必须走 ``list_recent_messages``（``get_oldest_message`` 只认带
     platform_msg_id 的消息，messenger 恒 None → 判空恒真 → 护栏形同虚设）；
   - 历史语义 = ``ingest_thread``：不触发自动回复/SSE、unread 恒 0；
   - 消息缺 ts → 按数组序回推（只保序）。
2. ``/api/platforms/messenger/{acct}/history``——会话内「拉更早」。核心不变量：
   - 与库内已有消息按（方向, 归一化文本）去重，只收带文本的；
   - 新写入的 ts 全部排在库内最早消息**之前**（顺序正确优先于时间真实性）；
   - 全部重复 → no_more；未启用 → protocol_disabled；node 404 → account_offline。
"""

from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

import src.web.routes.unified_inbox_account_routes as uar
from src.inbox.store import InboxStore


def _client(tmp_path, *, messenger_enabled: bool = True):
    app = FastAPI()
    cfg = {"platform_login": {"messenger": {"web_enabled": messenger_enabled,
                                            "web_url": "http://127.0.0.1:8791"}}}
    uar.register_account_routes(app, api_auth=lambda request: None,
                                config_manager=SimpleNamespace(config=cfg))
    store = InboxStore(tmp_path / "inbox.db")
    app.state.inbox_store = store
    return TestClient(app), store


def _backfill(client, **overrides):
    payload = {
        "platform": "messenger", "account_id": "6158", "chat_key": "777",
        "name": "Bea", "avatar_url": "http://a/b.jpg", "ts": 1750000100,
        "messages": [
            {"direction": "in", "sender": "Bea", "text": "hi"},
            {"direction": "out", "sender": "你", "text": "hello"},
            {"direction": "in", "sender": "Bea", "text": "are you there?"},
        ],
    }
    payload.update(overrides)
    return client.post("/api/internal/protocol/thread-history", json=payload)


CID = "messenger:6158:777"


# ── thread-history（首连回填）────────────────────────────────────────────────

def test_backfill_writes_empty_conversation(tmp_path):
    c, store = _client(tmp_path)
    r = _backfill(c)
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "inserted": 3}
    msgs = store.list_recent_messages(CID, limit=10)
    assert [m["text"] for m in msgs] == ["hi", "hello", "are you there?"]
    assert [m["direction"] for m in msgs] == ["in", "out", "in"]
    # ts 缺失 → 按数组序回推：严格递增（保序）
    tss = [float(m["ts"]) for m in msgs]
    assert tss == sorted(tss) and len(set(tss)) == 3
    conv = store.get_conversation(CID)
    assert conv is not None
    assert int(conv.get("unread") or 0) == 0        # 历史绝不制造「新消息」观感
    assert conv.get("display_name") == "Bea"
    assert conv.get("avatar_url") == "http://a/b.jpg"


def test_backfill_refuses_non_empty_conversation(tmp_path):
    c, store = _client(tmp_path)
    assert _backfill(c).json()["inserted"] == 3
    # 再来一次（模拟服务重启后重建回填队列）→ 空会话护栏拒收，零重复
    r2 = _backfill(c)
    assert r2.json() == {"ok": True, "inserted": 0, "reason": "not_empty"}
    assert len(store.list_recent_messages(CID, limit=50)) == 3


def test_backfill_empty_guard_works_without_platform_msg_id(tmp_path):
    """判空必须认「无 id 的 messenger 消息」——get_oldest_message 会漏判。"""
    c, store = _client(tmp_path)
    _backfill(c)
    # 无 msg_id 的旧路径：get_oldest_message 恒 None；护栏仍应靠 list_recent 生效
    assert store.get_oldest_message(CID) is None
    assert _backfill(c).json()["reason"] == "not_empty"


def test_backfill_stores_synth_msg_id(tmp_path):
    """P3：worker 带 synth msg_id → 落 platform_msg_id（表情/撤回挂点）。"""
    c, store = _client(tmp_path)
    r = _backfill(c, messages=[
        {"direction": "in", "sender": "Bea", "text": "hi", "msg_id": "m_abc1111111111111"},
        {"direction": "out", "sender": "你", "text": "hello", "msg_id": "m_def2222222222222"},
    ])
    assert r.json()["inserted"] == 2
    msgs = store.list_recent_messages(CID, limit=10)
    assert [m.get("platform_msg_id") for m in msgs] == [
        "m_abc1111111111111", "m_def2222222222222",
    ]
    # 有 id 后 get_oldest_message 可用；空会话护栏仍拒第二次回填
    assert store.get_oldest_message(CID) is not None
    assert _backfill(c).json()["reason"] == "not_empty"


def test_backfill_missing_fields_and_empty_messages(tmp_path):
    c, _ = _client(tmp_path)
    assert c.post("/api/internal/protocol/thread-history",
                  json={"platform": "messenger"}).json()["ok"] is False
    r = _backfill(c, messages=[])
    assert r.json()["reason"] == "no_messages"
    # 全部无正文无媒体 → 同样 no_messages（不落空壳）
    r = _backfill(c, messages=[{"direction": "in", "text": ""}])
    assert r.json()["reason"] == "no_messages"


def test_backfill_keeps_provided_ts_and_media(tmp_path):
    c, store = _client(tmp_path)
    r = _backfill(c, messages=[
        {"direction": "in", "text": "old", "ts": 1749990000},
        {"direction": "in", "text": "", "media_type": "image",
         "media_ref": "/static/protocol_media/messenger/x.jpg"},
    ])
    assert r.json()["inserted"] == 2
    msgs = store.list_recent_messages(CID, limit=10)
    assert float(msgs[0]["ts"]) == 1749990000.0     # 显式 ts 保留
    assert msgs[1]["media_type"] == "image"          # 无正文媒体照收


# ── /history messenger 分支（拉更早）────────────────────────────────────────

def _pull(client, count: int = 50):
    return client.post("/api/platforms/messenger/6158/history",
                       json={"chat_key": "777", "count": count})


def _fake_node(monkeypatch, messages, calls=None):
    async def fake_post(url, payload, timeout=20.0):
        if calls is not None:
            calls.append((url, dict(payload)))
        return {"ok": True, "messages": messages}
    monkeypatch.setattr(
        "src.integrations.messenger_web_login._post_json", fake_post)


def test_pull_history_dedups_and_orders_before_earliest(tmp_path, monkeypatch):
    c, store = _client(tmp_path)
    _backfill(c)  # 库内已有 hi / hello / are you there?
    earliest = min(float(m["ts"]) for m in store.list_recent_messages(CID, limit=10))
    calls = []
    _fake_node(monkeypatch, [
        {"direction": "in", "sender": "Bea", "text": "much older 1"},
        {"direction": "out", "sender": "你", "text": "much older 2"},
        {"direction": "in", "sender": "Bea", "text": "hi"},   # 与库内重复 → 滤掉
    ], calls)
    r = _pull(c)
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "requested": 50, "inserted": 2}
    assert calls and calls[0][0].endswith("/accounts/6158/thread-history")
    msgs = store.list_recent_messages(CID, limit=20)
    texts = [m["text"] for m in msgs]
    assert texts.count("hi") == 1                    # 去重生效
    assert texts[:2] == ["much older 1", "much older 2"]  # 排在最前
    pulled_ts = [float(m["ts"]) for m in msgs[:2]]
    assert all(t < earliest for t in pulled_ts)      # 全部在库内最早之前
    assert pulled_ts == sorted(pulled_ts)


def test_pull_history_all_duplicates_reports_no_more(tmp_path, monkeypatch):
    c, store = _client(tmp_path)
    _backfill(c)
    _fake_node(monkeypatch, [
        {"direction": "in", "text": "HI  "},   # 大小写/空白归一后仍是重复
        {"direction": "out", "text": "hello"},
    ])
    r = _pull(c)
    assert r.json()["ok"] is False
    assert r.json()["reason"] == "no_more"
    assert len(store.list_recent_messages(CID, limit=20)) == 3


def test_pull_history_disabled_and_offline(tmp_path, monkeypatch):
    c, _ = _client(tmp_path, messenger_enabled=False)
    assert _pull(c).json()["reason"] == "protocol_disabled"

    c2, _ = _client(tmp_path.joinpath("b"))

    class _Resp:
        status_code = 404

    async def fake_404(url, payload, timeout=20.0):
        exc = Exception("404")
        exc.response = _Resp()
        raise exc
    monkeypatch.setattr(
        "src.integrations.messenger_web_login._post_json", fake_404)
    assert _pull(c2).json()["reason"] == "account_offline"


def test_pull_history_empty_conversation_uses_now_anchor(tmp_path, monkeypatch):
    """占位会话（目录同步建的、零消息）也能拉更早——锚点回落当前时刻。"""
    c, store = _client(tmp_path)
    _fake_node(monkeypatch, [{"direction": "in", "text": "first ever"}])
    r = _pull(c)
    assert r.json()["inserted"] == 1
    msgs = store.list_recent_messages(CID, limit=5)
    assert msgs[0]["text"] == "first ever"


def test_pull_history_dedups_by_msg_id(tmp_path, monkeypatch):
    """P3：同 msg_id 即使文案微调也不重复灌库（id 优先于文本归一）。"""
    c, store = _client(tmp_path)
    _backfill(c, messages=[
        {"direction": "in", "text": "hi", "msg_id": "m_same000000000001"},
    ])
    _fake_node(monkeypatch, [
        {"direction": "in", "text": "hi!", "msg_id": "m_same000000000001"},  # 同 id
        {"direction": "in", "text": "brand new", "msg_id": "m_new0000000000002"},
    ])
    r = _pull(c)
    assert r.json()["inserted"] == 1
    texts = [m["text"] for m in store.list_recent_messages(CID, limit=10)]
    assert texts.count("hi") == 1
    assert "brand new" in texts
    assert "hi!" not in texts


# ── 目录同步的 unread 保护（P1 潜伏 bug 回归钉）─────────────────────────────

def test_directory_sync_without_unread_key_keeps_real_unread(tmp_path):
    """目录推送不带 unread 键时**绝不能**把 ingest 累计的真实未读清零。

    upsert_conversation 的 unread 是无条件覆盖语义；Telegram/WA 的目录行带云端
    真值从未踩到，Messenger 目录每 5 分钟推一轮、DOM 抓不到可靠计数——缺键按 0
    传的话，坐席的未读徽章会被周期性抹平（且极难归因）。
    """
    _, store = _client(tmp_path)
    from src.inbox.ingest import ingest_collected_chats
    ingest_collected_chats(store, [{
        "conversation_id": CID, "platform": "messenger", "account_id": "6158",
        "chat_key": "777", "name": "Bea", "last_msg": "hi", "last_ts": 1750000000,
        "unread": 3,
        "messages": [{"text": "hi", "ts": 1750000000, "direction": "in"}],
    }], publish_events=False)
    assert int(store.get_conversation(CID)["unread"] or 0) >= 1
    before = int(store.get_conversation(CID)["unread"] or 0)
    # 目录推送（无 unread 键）→ 未读保留；显式带 unread → 按传入值
    n = store.upsert_protocol_chats("messenger", "6158", [
        {"jid": "777", "name": "Bea Gaston", "avatar_url": "http://a/new.jpg"},
        {"jid": "888", "name": "New Peer"},
    ])
    assert n == 2
    after = store.get_conversation(CID)
    assert int(after["unread"] or 0) == before          # 未读未被清零
    assert after["display_name"] == "Bea Gaston"        # 名字/头像照常更新
    assert int(store.get_conversation("messenger:6158:888")["unread"] or 0) == 0
    n = store.upsert_protocol_chats("messenger", "6158", [
        {"jid": "777", "name": "Bea Gaston", "unread": 0},
    ])
    assert n == 1
    assert int(store.get_conversation(CID)["unread"] or 0) == 0  # 显式 0 仍可清
