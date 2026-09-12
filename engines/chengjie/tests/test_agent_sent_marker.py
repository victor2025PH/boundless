# -*- coding: utf-8 -*-
"""接力记忆二期（2026-09-12）：出站气泡「人工」角标——agent_sends 打点与出站行时间就近匹配。"""
from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

from src.inbox.models import InboxMessage
from src.inbox.store import InboxStore
from src.web.routes.unified_inbox_aggregate import AGENT_SENT_MATCH_SEC, _mark_agent_sent

CID = "whatsapp:17345893506:13308422244"


def _req(store):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(inbox_store=store)))


def _ing(store, mid, direction, text, ts):
    store.ingest_message(InboxMessage(conversation_id=CID, platform_msg_id=mid,
                                      direction=direction, text=text, ts=ts))


def test_store_list_agent_sends_filters_and_orders(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    store.record_agent_send(CID, "a1", agent_name="小王", ts=200.0)
    store.record_agent_send(CID, "a1", agent_name="小王", ts=100.0)
    store.record_agent_send("other:x:y", "a2", agent_name="别会话", ts=150.0)
    rows = store.list_agent_sends(CID)
    assert [r["ts"] for r in rows] == [100.0, 200.0] and rows[0]["agent_name"] == "小王"
    assert [r["ts"] for r in store.list_agent_sends(CID, since_ts=150.0)] == [200.0]
    assert [r["ts"] for r in store.list_agent_sends(CID, until_ts=150.0)] == [100.0]
    assert store.list_agent_sends("") == []
    store.close()


def test_mark_agent_sent_nearest_one_to_one_within_window(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    t = 1_757_000_000.0
    _ing(store, "ai1", "out", "AI 说的", t + 0)             # 无打点 → 不标
    _ing(store, "c1", "in", "客户", t + 10)
    _ing(store, "h1", "out", "人工第一句", t + 20)
    _ing(store, "h2", "out", "人工第二句", t + 23)          # 两条出站挨得近：各配各的打点
    _ing(store, "ai2", "out", "AI 又说", t + 200)           # 打点在窗口外 → 不标
    store.record_agent_send(CID, "a1", agent_name="小王", ts=t + 19.0)
    store.record_agent_send(CID, "a1", agent_name="小王", ts=t + 22.5)
    store.record_agent_send(CID, "a1", agent_name="小王", ts=t + 200 + AGENT_SENT_MATCH_SEC + 5)
    msgs = [dict(r) for r in store.list_recent_messages(CID, limit=20)]
    n = _mark_agent_sent(_req(store), CID, msgs)
    assert n == 2
    by = {m["text"]: m for m in msgs}
    assert by["人工第一句"]["sent_by"] == "agent" and by["人工第一句"]["agent_name"] == "小王"
    assert by["人工第二句"]["sent_by"] == "agent"
    assert "sent_by" not in by["AI 说的"] and "sent_by" not in by["AI 又说"]
    assert "sent_by" not in by["客户"]                      # 入站永不标
    # 幂等 / 无打点会话 / 空输入 / 无 store 全部软处理
    assert _mark_agent_sent(_req(store), CID, msgs) == 2
    assert _mark_agent_sent(_req(store), "other:x:y", [{"direction": "out", "ts": t, "text": "x"}]) == 0
    assert _mark_agent_sent(_req(store), CID, []) == 0
    assert _mark_agent_sent(_req(None), CID, msgs) == 0
    store.close()


def test_thread_route_and_template_wired():
    root = Path(__file__).resolve().parents[1] / "src"
    rt = (root / "web" / "routes" / "unified_inbox_read_routes.py").read_text(encoding="utf-8", errors="ignore")
    assert "_mark_agent_sent(request, cid, out_msgs)" in rt
    html = (root / "web" / "templates" / "unified_inbox.html").read_text(encoding="utf-8", errors="ignore")
    assert "m.sent_by||'')==='agent'" in html and "inbox.msg.agent_sent_t" in html
    assert "${_agentChip}" in html
    # 接力摘要预览条：setMode 里有 note 走 _handoffToast，函数已定义且用了 i18n 键
    assert "function _handoffToast(" in html and "_handoffToast(tip, _hfNote" in html
    assert "inbox.mode.handoff_view" in html and "inbox.mode.handoff_title" in html
    css = (root / "web" / "static" / "workspace" / "unified-inbox.css").read_text(encoding="utf-8", errors="ignore")
    assert ".ms-ext-chip.agent-chip" in css and ".hf-toast" in css
    # CSS 缓存戳已随样式新增 bump（不钉具体值：同日多线共用同一个戳）
    m = re.search(r'unified-inbox\.css\?v=([\w-]+)', html)
    assert m and m.group(1) not in ("20260912q21b",)
