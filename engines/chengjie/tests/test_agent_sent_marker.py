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
    assert not by["AI 说的"].get("sent_by") and not by["AI 又说"].get("sent_by")
    assert not by["客户"].get("sent_by")                    # 入站永不标
    # 幂等 / 无打点会话 / 空输入 / 无 store 全部软处理
    assert _mark_agent_sent(_req(store), CID, msgs) == 2
    assert _mark_agent_sent(_req(store), "other:x:y", [{"direction": "out", "ts": t, "text": "x"}]) == 0
    assert _mark_agent_sent(_req(store), CID, []) == 0
    assert _mark_agent_sent(_req(None), CID, msgs) == 0
    store.close()


def _ing_media(store, mid, text, ref, ts, mt="image"):
    store.ingest_message(InboxMessage(conversation_id=CID, platform_msg_id=mid, direction="out",
                                      text=text, media_type=mt, media_ref=ref, ts=ts))


def _rows(store):
    return {r["platform_msg_id"]: r for r in store.list_recent_messages(CID, limit=50)}


def test_sent_by_claim_mirror_first_then_send_record(tmp_path):
    """三期主路径：编排器先落镜像行，发送路由随后打点（带 text）→ 反向认领 sent_by=agent。"""
    store = InboxStore(tmp_path / "inbox.db")
    t = 1_757_000_000.0
    _ing(store, "ai0", "out", "你好呀", t - 400)                 # 同文本但在窗口外 → 不认
    _ing(store, "h1", "out", "你好呀", t)
    store.record_agent_send(CID, "a1", agent_name="小王", ts=t + 2, text="你好呀 ")   # 空白归一化
    rows = _rows(store)
    assert rows["h1"]["sent_by"] == "agent" and rows["ai0"]["sent_by"] == ""
    sends = store.list_agent_sends(CID)
    assert sends[0]["claimed_mid"] == rows["h1"]["message_id"] and sends[0]["text_hash"]
    # 同文本再来一条（AI 复读）→ 打点已认领，不再认
    _ing(store, "ai1", "out", "你好呀", t + 5)
    assert _rows(store)["ai1"]["sent_by"] == ""
    store.close()


def test_sent_by_claim_send_record_first_then_mirror(tmp_path):
    """边车镜像慢：打点先到、镜像行后落 → ingest 时正向认领；无 hash/ref 的老式打点不认。"""
    store = InboxStore(tmp_path / "inbox.db")
    t = 1_757_000_000.0
    store.record_agent_send(CID, "a1", agent_name="小王", ts=t, text="Good night")
    store.record_agent_send(CID, "a1", agent_name="小王", ts=t + 1)                  # 老式打点
    _ing(store, "h1", "out", "Good  night", t + 40)              # 镜像 40s 后到、空白略不同
    _ing(store, "ai1", "out", "Sleep well", t + 41)
    rows = _rows(store)
    assert rows["h1"]["sent_by"] == "agent" and rows["ai1"]["sent_by"] == ""
    # 窗口外的镜像不认
    store.record_agent_send(CID, "a1", agent_name="小王", ts=t + 1000, text="late")
    _ing(store, "h2", "out", "late", t + 1000 + 300)
    assert _rows(store)["h2"]["sent_by"] == ""
    store.close()


def test_sent_by_claim_media_by_ref_and_batch_path(tmp_path):
    from src.inbox.models import InboxConversation
    store = InboxStore(tmp_path / "inbox.db")
    t = 1_757_000_000.0
    ref = "/static/protocol_media/whatsapp/out_a.jpg"
    _ing_media(store, "h1", "", ref, t)                          # 无配文：只能靠 ref
    store.record_agent_send(CID, "a1", agent_name="小王", ts=t + 1, text="", media_ref=ref)
    assert _rows(store)["h1"]["sent_by"] == "agent"
    # ingest_batch 路径同样认领（打点先到）
    ref2 = "/static/protocol_media/whatsapp/out_b.mp4"
    store.record_agent_send(CID, "a1", agent_name="小王", ts=t + 10, text="看看", media_ref=ref2)
    conv = InboxConversation(conversation_id=CID, platform="whatsapp", account_id="17345893506",
                             chat_key="13308422244")
    store.ingest_batch(conv, [InboxMessage(conversation_id=CID, platform_msg_id="h2", direction="out",
                                           text="看看", media_type="video", media_ref=ref2, ts=t + 12)])
    rows = _rows(store)
    assert rows["h2"]["sent_by"] == "agent"
    # 入站行永不标
    store.ingest_message(InboxMessage(conversation_id=CID, platform_msg_id="c1", direction="in",
                                      text="看看", ts=t + 13))
    assert _rows(store)["c1"]["sent_by"] == ""
    store.close()


def test_mark_agent_sent_trusts_column_and_fills_name(tmp_path):
    from src.inbox.normalizer import store_message_to_obj
    store = InboxStore(tmp_path / "inbox.db")
    t = 1_757_000_000.0
    _ing(store, "h1", "out", "人工精确认领", t)
    store.record_agent_send(CID, "a1", agent_name="小王", ts=t + 1, text="人工精确认领")
    _ing(store, "h2", "out", "老行只能时间匹配", t + 60)
    store.record_agent_send(CID, "a2", agent_name="老李", ts=t + 61)                # 无 hash → 未认领
    _ing(store, "ai", "out", "AI 说的", t + 120)
    msgs = [store_message_to_obj(r) for r in store.list_recent_messages(CID, limit=10)]
    by = {m["text"]: m for m in msgs}
    assert by["人工精确认领"]["sent_by"] == "agent"                # 列透传
    n = _mark_agent_sent(_req(store), CID, msgs)
    assert n == 1                                                # 只有老行是本次新标
    assert by["人工精确认领"]["agent_name"] == "小王"              # 认领打点的坐席名补上
    assert by["老行只能时间匹配"]["sent_by"] == "agent" and by["老行只能时间匹配"]["agent_name"] == "老李"
    assert "sent_by" not in by["AI 说的"] or by["AI 说的"]["sent_by"] == ""
    store.close()


def test_sent_by_tagged_rows_from_orchestrator_mirror(tmp_path):
    """四期：镜像行自带 sent_by（origin → ai/agent）→ INSERT 即落列；ai 行永不被打点认领，
    agent 行仍与打点连上（claimed）；入站/非法值归空。"""
    from src.inbox.models import InboxConversation
    store = InboxStore(tmp_path / "inbox.db")
    t = 1_757_000_000.0
    store.ingest_message(InboxMessage(conversation_id=CID, platform_msg_id="ai1", direction="out",
                                      text="同一句", ts=t, sent_by="ai"))
    store.ingest_message(InboxMessage(conversation_id=CID, platform_msg_id="h1", direction="out",
                                      text="同一句", ts=t + 1, sent_by="agent"))
    store.ingest_message(InboxMessage(conversation_id=CID, platform_msg_id="c1", direction="in",
                                      text="同一句", ts=t + 2, sent_by="agent"))       # 入站不认
    store.ingest_message(InboxMessage(conversation_id=CID, platform_msg_id="x1", direction="out",
                                      text="非法值", ts=t + 3, sent_by="robot"))
    rows = _rows(store)
    assert rows["ai1"]["sent_by"] == "ai" and rows["h1"]["sent_by"] == "agent"
    assert rows["c1"]["sent_by"] == "" and rows["x1"]["sent_by"] == ""
    # 路由随后打点（同文本）：反向认领必须连到 agent 行 h1，而不是 ai1
    store.record_agent_send(CID, "a1", agent_name="小王", ts=t + 2, text="同一句")
    sends = store.list_agent_sends(CID)
    assert sends[0]["claimed_mid"] == rows["h1"]["message_id"]
    assert _rows(store)["ai1"]["sent_by"] == "ai"
    # ingest_batch 路径同样落列
    conv = InboxConversation(conversation_id=CID, platform="whatsapp", account_id="17345893506",
                             chat_key="13308422244")
    store.ingest_batch(conv, [InboxMessage(conversation_id=CID, platform_msg_id="ai2", direction="out",
                                           text="自动语音", media_type="voice", media_ref="/v.ogg",
                                           ts=t + 10, sent_by="ai")])
    assert _rows(store)["ai2"]["sent_by"] == "ai"
    # 线程读路径：ai 行不进回落池（打点即使就近也不把它标成人工）
    from src.inbox.normalizer import store_message_to_obj
    store.record_agent_send(CID, "a2", agent_name="老李", ts=t + 10.5)               # 老式打点、无 hash
    msgs = [store_message_to_obj(r) for r in store.list_recent_messages(CID, limit=10)]
    assert _mark_agent_sent(_req(store), CID, msgs) == 1       # 只有无来源的 x1 可被就近回落
    by = {m["platform_msg_id"]: m for m in msgs}
    assert by["ai2"]["sent_by"] == "ai" and by["h1"]["agent_name"] == "小王"
    assert by["x1"]["sent_by"] == "agent" and by["ai1"]["sent_by"] == "ai"
    store.close()


def test_ingest_chain_passes_sent_by_from_source():
    from src.inbox.ingest import _msg_from_obj
    m = {"message_id": "m1", "direction": "out", "text": "hi", "ts": 1.0,
         "source": {"sent_by": "ai"}}
    assert _msg_from_obj("c1", m, direction="out").sent_by == "ai"
    m2 = {"message_id": "m2", "direction": "out", "text": "hi", "ts": 1.0, "source": {}}
    assert _msg_from_obj("c1", m2, direction="out").sent_by == ""
    from src.integrations.account_orchestrator import _sent_by_for_origin
    assert _sent_by_for_origin("manual") == "agent" and _sent_by_for_origin("auto") == "ai"
    assert _sent_by_for_origin(None) == "ai"
    root = Path(__file__).resolve().parents[1] / "src"
    orch = (root / "integrations" / "account_orchestrator.py").read_text(encoding="utf-8", errors="ignore")
    assert orch.count('"sent_by": _sent_by_for_origin(origin)') == 2         # 文本 + 媒体镜像两处
    tpl = (root / "web" / "templates" / "unified_inbox.html").read_text(encoding="utf-8", errors="ignore")
    assert "ms-ext-chip ai-chip" in tpl and "inbox.msg.ai_sent_t" in tpl
    from src.web.i18n_packs import inbox_workspace as iw
    for d in (iw.ZH, iw.EN):
        assert "inbox.msg.ai_sent" in d and "inbox.msg.ai_sent_t" in d


def test_send_routes_pass_text_and_media_ref():
    root = Path(__file__).resolve().parents[1] / "src"
    rt = (root / "web" / "routes" / "unified_inbox_send_routes.py").read_text(encoding="utf-8", errors="ignore")
    assert "_mark_send(cid, text," in rt
    assert "parts=(list(_bubble_parts[:_bub_sent]) if _bubble_parts else None)" in rt   # 四期：分条逐段打点
    assert "for _pt in _texts:" in rt
    assert "text=caption, media_ref=url" in rt and "text=spoken_text, media_ref=url" in rt


def test_split_send_each_part_claims_its_own_row(tmp_path):
    """四期：分条发送逐段打点 → 每段镜像行都按自己的 hash 精确认领（不再只有首段命中）。"""
    store = InboxStore(tmp_path / "inbox.db")
    t = 1_757_000_000.0
    parts = ["第一段话", "第二段话", "第三段话"]
    for i, p in enumerate(parts):                       # 编排器逐条镜像（RPA 形态：无 sent_by）
        _ing(store, f"p{i}", "out", p, t + i)
    _ing(store, "ai", "out", "AI 插了一句", t + 1.5)     # 夹在中间、时间上比第三段更近打点
    for p in parts:                                      # 路由逐段打点（_mark_send parts=）
        store.record_agent_send(CID, "a1", agent_name="小王", ts=t + 3, text=p)
    rows = _rows(store)
    assert all(rows[f"p{i}"]["sent_by"] == "agent" for i in range(3))
    assert rows["ai"]["sent_by"] == ""
    assert sorted(s["claimed_mid"] for s in store.list_agent_sends(CID)) == \
        sorted(rows[f"p{i}"]["message_id"] for i in range(3))
    store.close()


def test_thread_route_and_template_wired():
    root = Path(__file__).resolve().parents[1] / "src"
    rt = (root / "web" / "routes" / "unified_inbox_read_routes.py").read_text(encoding="utf-8", errors="ignore")
    assert "_mark_agent_sent(request, cid, out_msgs)" in rt
    html = (root / "web" / "templates" / "unified_inbox.html").read_text(encoding="utf-8", errors="ignore")
    assert "_sentBy==='agent'" in html and "inbox.msg.agent_sent_t" in html
    assert "${_agentChip}" in html
    # 接力摘要预览条：setMode 里有 note 走 _handoffToast，函数已定义且用了 i18n 键
    assert "function _handoffToast(" in html and "_handoffToast(tip, _hfNote" in html
    assert "inbox.mode.handoff_view" in html and "inbox.mode.handoff_title" in html
    css = (root / "web" / "static" / "workspace" / "unified-inbox.css").read_text(encoding="utf-8", errors="ignore")
    assert ".ms-ext-chip.agent-chip" in css and ".hf-toast" in css
    # CSS 缓存戳已随样式新增 bump（不钉具体值：同日多线共用同一个戳）
    m = re.search(r'unified-inbox\.css\?v=([\w-]+)', html)
    assert m and m.group(1) not in ("20260912q21b",)
