# -*- coding: utf-8 -*-
"""#146（2026-09-02，钧）：手机删除消息 → 工作台同步删 + **关联记忆一并清理**。

「残留错误内容会进 AI 记忆、影响后面每一轮回复」——B87 只做了镜像软删且刻意留了
记忆（「AI 记得但不主动提」）。本批三条腿：
  ① 情景记忆：被删消息正文 ↔ ``source_quote``（五件套溯源）精确反查删（hits==1 才删）；
  ② A 线上下文：``_conversation_history`` 剔匹配 user 条 + 命中的 ``last_message`` 清空；
  ③ B 线历史：``list_recent_messages`` 默认口径剔 peer 软删行（见 test_peer_delete_sync）。
另：协议多开的 TelegramProtocolWorker 此前**根本没接**删除事件（B87 只接了 A 线 client）
——接线自证钉源码。
"""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

from src.inbox import peer_delete_purge as pdp
from src.inbox.models import InboxConversation, InboxMessage
from src.inbox.store import InboxStore
from src.utils.context_store import ContextStore, make_context_key
from src.utils.episodic_memory_store import EpisodicMemoryStore


def _seed(store: InboxStore, cid: str, pmid: str, text: str, ts: float,
          *, direction="in", account="acct", chat_key=""):
    conv = InboxConversation(
        conversation_id=cid, platform="telegram", account_id=account,
        chat_key=chat_key or cid.rsplit(":", 1)[-1], display_name="客户")
    store.ingest_batch(conv, [InboxMessage(
        conversation_id=cid, platform_msg_id=pmid, text=text, ts=ts,
        direction=direction)])


# ── 纯函数 ──────────────────────────────────────────────────────────────────
def test_normalize_quote_strips_media_desc_and_collapses_ws():
    q = pdp.normalize_quote("  我住在  北京\n哦 ")
    assert q == "我住在 北京 哦"
    assert pdp.normalize_quote("x" * 500) == "x" * 200
    # 识图描述不进记忆抽取 → 反查也不带它
    assert "图片内容" not in pdp.normalize_quote("[图片内容] 一只猫\n我叫小明")


def test_quotes_match_first_200_and_min_len():
    assert pdp.quotes_match("我住在北京哦", "我住在北京哦")
    assert pdp.quotes_match("a" * 250, "a" * 300)      # 双方都截 200
    assert not pdp.quotes_match("嗯", "嗯")            # 太短没有唯一性
    assert not pdp.quotes_match("我住在北京", "我住在上海")


def test_split_conversation_id_keeps_colon_in_chat_key():
    assert pdp.split_conversation_id("telegram:acct:555") == ("telegram", "acct", "555")
    assert pdp.split_conversation_id("line:U1:Ux:y") == ("line", "U1", "Ux:y")
    assert pdp.split_conversation_id("bad") == ("", "", "")


def test_key_has_component_not_substring():
    assert pdp.key_has_component("telegram:acct:555", "555")
    assert pdp.key_has_component("acct:555", "555")
    assert pdp.key_has_component("555", "555")
    assert pdp.key_has_component("-100777_555", "555")       # 群键 chat_user
    assert not pdp.key_has_component("telegram:acct:15550", "555")   # 纯子串不认
    assert not pdp.key_has_component("", "555") and not pdp.key_has_component("x", "")


def test_inbound_rows_filters_outbound_and_short():
    rows = [
        {"direction": "in", "text": "我住在北京哦", "conversation_id": "telegram:a:1"},
        {"direction": "out", "text": "好的记住了北京", "conversation_id": "telegram:a:1"},
        {"direction": "in", "text": "嗯", "conversation_id": "telegram:a:1"},
        "坏形状",
    ]
    out = pdp.inbound_rows(rows)
    assert [r["_quote"] for r in out] == ["我住在北京哦"]


# ── ① 情景记忆反查删 ─────────────────────────────────────────────────────────
def test_episodic_delete_by_source_quotes(tmp_path: Path):
    st = EpisodicMemoryStore(tmp_path / "bot.db")
    key = "telegram:acct:555"
    quote = "我住在北京哦，下周搬家"
    rid = st.add_fact(key, "用户住在北京", "llm", source="ai_inferred",
                      source_quote=quote, source_ts=time.time())
    st.add_fact(key, "用户养了一只猫", "llm", source="ai_inferred",
                source_quote="我家猫今天很乖", source_ts=time.time())
    # 同 chat_key 别的账号键：也算这个客户的记忆（组件匹配）
    st.add_fact("acct2:555", "用户喜欢喝茶", "heuristic", source="user_stated",
                source_quote=quote)
    # 纯子串键（15550）不动
    st.add_fact("telegram:acct:15550", "别人的事实", "llm", source_quote=quote)
    # 复发事实（hits=2）不删——客户在别处又说过
    st.add_fact(key, "用户是设计师", "llm", source="ai_inferred", source_quote=quote)
    assert st.add_fact(key, "用户是设计师", "llm", source="ai_inferred") is None  # hits→2
    assert rid is not None

    n = st.delete_by_source_quotes("555", [pdp.normalize_quote(quote)])
    assert n == 2
    left = {r["content"] for r in st.list_rows(limit=50)}
    assert left == {"用户养了一只猫", "别人的事实", "用户是设计师"}
    # 幂等 / 空参
    assert st.delete_by_source_quotes("555", [quote]) == 0
    assert st.delete_by_source_quotes("", [quote]) == 0
    assert st.delete_by_source_quotes("555", []) == 0


def test_purge_episodic_groups_by_chat_key(tmp_path: Path):
    st = EpisodicMemoryStore(tmp_path / "bot.db")
    st.add_fact("telegram:acct:555", "用户住在北京", "llm", source_quote="我住在北京哦")
    st.add_fact("telegram:acct:777", "用户住在上海", "llm", source_quote="我住在上海哦")
    rows = [
        {"conversation_id": "telegram:acct:555", "direction": "in", "text": "我住在北京哦"},
        {"conversation_id": "telegram:acct:777", "direction": "out", "text": "我住在上海哦"},  # 出站不算
    ]
    assert pdp.purge_episodic(st, rows) == 1
    assert {r["content"] for r in st.list_rows(limit=10)} == {"用户住在上海"}
    assert pdp.purge_episodic(None, rows) == 0


# ── ② A 线上下文历史 ─────────────────────────────────────────────────────────
def test_purge_context_history_and_last_message(tmp_path: Path):
    cs = ContextStore(tmp_path / "bot.db", ttl_days=30)
    key = make_context_key("555", "acct")
    ctx = cs.get(key)
    ctx["_conversation_history"] = [
        {"role": "user", "content": "我住在北京哦"},
        {"role": "assistant", "content": "北京好地方"},
        {"role": "user", "content": "今天好累"},
    ]
    ctx["last_message"] = "我住在北京哦"
    cs.mark_dirty(key)
    cs.flush(key)

    rows = [{"conversation_id": "telegram:acct:555", "direction": "in", "text": "我住在北京哦"}]
    assert pdp.purge_context_history(cs, rows) == 1
    ctx2 = cs.peek(key)
    assert [m["content"] for m in ctx2["_conversation_history"]] == ["北京好地方", "今天好累"]
    assert ctx2["last_message"] == ""
    # 不凭空建 ctx：另一个会话没有上下文 → peek 为 None、不落盘
    assert cs.peek(make_context_key("999", "acct")) is None
    assert pdp.purge_context_history(
        cs, [{"conversation_id": "telegram:acct:999", "direction": "in", "text": "凭空的消息"}]) == 0
    assert cs.peek(make_context_key("999", "acct")) is None
    # 落盘后重开仍是清理后的形态
    cs2 = ContextStore(tmp_path / "bot.db", ttl_days=30)
    assert cs2.peek(key)["last_message"] == ""


def test_context_store_peek_loads_persisted_without_creating(tmp_path: Path):
    cs = ContextStore(tmp_path / "bot.db", ttl_days=30)
    assert cs.peek("nobody") is None
    assert cs.peek("") is None
    cs.get("someone")["topic"] = "t"
    cs.mark_dirty("someone")
    cs.flush("someone")
    cs2 = ContextStore(tmp_path / "bot.db", ttl_days=30)
    assert cs2.peek("someone")["topic"] == "t"


# ── 编排入口 + bridge 端到端 ─────────────────────────────────────────────────
def test_purge_for_deleted_rows_orchestrates_both(tmp_path: Path):
    st = EpisodicMemoryStore(tmp_path / "bot.db")
    cs = ContextStore(tmp_path / "bot.db", ttl_days=30)
    st.add_fact("telegram:acct:555", "用户住在北京", "llm", source_quote="我住在北京哦")
    key = make_context_key("555", "acct")
    cs.get(key)["_conversation_history"] = [{"role": "user", "content": "我住在北京哦"}]
    cs.mark_dirty(key)
    cs.flush(key)
    sm = SimpleNamespace(_episodic_store=st, _context_store=cs)
    rows = [{"conversation_id": "telegram:acct:555", "direction": "in", "text": "我住在北京哦"}]
    assert pdp.purge_for_deleted_rows(sm, rows) == {"memory": 1, "context": 1}
    assert pdp.purge_for_deleted_rows(None, rows) == {"memory": 0, "context": 0}
    assert pdp.purge_for_deleted_rows(sm, []) == {"memory": 0, "context": 0}


def test_bridge_report_deleted_purges_memory_and_sse(tmp_path: Path, monkeypatch):
    """端到端：TG 删除事件 → 软删 + 记忆清理 + SSE 带清理计数。"""
    import src.integrations.protocol_bridge as pb
    from src.integrations.shared import event_bus as eb
    monkeypatch.setattr(eb, "_bus", None, raising=False)

    store = InboxStore(tmp_path / "inbox.db")
    cid = "telegram:acct:555"
    _seed(store, cid, "1001", "我住在北京哦", 1.0)
    _seed(store, cid, "1002", "今天好累", 2.0)
    monkeypatch.setattr(pb, "get_inbox_store", lambda: store)

    st = EpisodicMemoryStore(tmp_path / "bot.db")
    cs = ContextStore(tmp_path / "bot.db", ttl_days=30)
    st.add_fact(cid, "用户住在北京", "llm", source="ai_inferred", source_quote="我住在北京哦")
    st.add_fact(cid, "用户今天很累", "llm", source="ai_inferred", source_quote="今天好累")
    sm = SimpleNamespace(_episodic_store=st, _context_store=cs)
    pb.register_deleted_memory_purger(lambda rows: pdp.purge_for_deleted_rows(sm, rows))
    try:
        n = pb.report_deleted_messages("telegram", "acct", ["1001"])
    finally:
        pb.register_deleted_memory_purger(None)
    assert n == 1
    # 镜像：UI 与业务口径都不见了；记忆：只删了被删那句抽出的事实
    assert [m["text"] for m in store.list_recent_messages(cid, limit=10)] == ["今天好累"]
    assert {r["content"] for r in st.list_rows(limit=10)} == {"用户今天很累"}

    from src.integrations.shared.event_bus import get_event_bus
    evts = [e for e in get_event_bus().recent_events(50) if e["type"] == "messages_deleted"]
    assert evts and evts[-1]["data"]["memory_purged"] == 1
    assert evts[-1]["data"]["op"] == "peer_delete"


def test_bridge_survives_purger_crash(tmp_path: Path, monkeypatch):
    """清理钩子抛异常不得拖垮软删（软删是主职责，记忆清理是 best-effort）。"""
    import src.integrations.protocol_bridge as pb
    store = InboxStore(tmp_path / "inbox.db")
    _seed(store, "telegram:acct:555", "1001", "我住在北京哦", 1.0)
    monkeypatch.setattr(pb, "get_inbox_store", lambda: store)

    def _boom(_rows):
        raise RuntimeError("boom")

    pb.register_deleted_memory_purger(_boom)
    try:
        assert pb.report_deleted_messages("telegram", "acct", ["1001"]) == 1
    finally:
        pb.register_deleted_memory_purger(None)
    assert store.list_recent_messages("telegram:acct:555", limit=10) == []


# ── 接线自证 ─────────────────────────────────────────────────────────────────
def test_orchestrator_tg_worker_listens_for_deletes():
    """协议多开 TelegramProtocolWorker 必须接 UpdateDeleteMessages / Channel 版
    （B87 只接了 A 线 client，钧的手机删消息事件此前没人听）。"""
    src = (Path(__file__).resolve().parents[1] / "src" / "integrations"
           / "account_orchestrator.py").read_text(encoding="utf-8")
    seg = src[src.index("def _wire_receipts"):src.index("def _backfill_limit")]
    assert "raw.types.UpdateDeleteMessages" in seg
    assert "raw.types.UpdateDeleteChannelMessages" in seg
    assert "report_deleted_messages(" in seg


def test_web_layer_registers_memory_purger():
    src = (Path(__file__).resolve().parents[1] / "src" / "web" / "routes"
           / "unified_inbox_account_routes.py").read_text(encoding="utf-8")
    assert "register_deleted_memory_purger(_purge_deleted)" in src
    assert "purge_for_deleted_rows(resolve_skill_manager(None, app), rows)" in src
