# -*- coding: utf-8 -*-
"""P-2 A / F（#259 #252，2026-09-08）：回填历史与自聊会话「落库不触发自动化」。

事故链（H3BAJD）：WA 边车登录后把 messaging-history.set 里每个会话的最后一条历史当新入站
推给后端 → ingest_collected_chats 发 new_inbound 回调 → auto_generate_draft 3 秒内对 6 个
老会话起草（含昨晚说「Never write me again」的客户）。ZH3ZQ5 同链（占位会话拉历史 ON_DEMAND）。
A7PB2F / H3BAJD①：自聊会话 whatsapp:17345893728:17345893728 入箱并触发自动化。

钉住：① backfill=1 的入站**必须落库**（历史要看得见）但回调零调用、unread=0、不发事件；
② 同一会话随后的真实入站照常触发；③ 自聊会话同口径；④ 边车 / TG 回填打标静态钉。
"""
from __future__ import annotations

import re
import time
from pathlib import Path

from src.inbox.ingest import ingest_collected_chats
from src.inbox.normalizer import (
    is_backfill_source, is_self_chat, normalize_chat, store_row_to_chat,
)
from src.inbox.store import InboxStore
from src.integrations.protocol_bridge import ingest_incoming

_ROOT = Path(__file__).resolve().parents[1]
_PLAT, _ACCT, _CK = "whatsapp", "447546050758", "447349041791"
_CID = f"{_PLAT}:{_ACCT}:{_CK}"


def _cb_recorder(store):
    calls = []
    store.register_new_inbound_cb(lambda conv, text: calls.append((conv, text)))
    return calls


def test_backfill_inbound_persists_but_fires_no_callback(tmp_path):
    st = InboxStore(tmp_path / "inbox.db")
    calls = _cb_recorder(st)
    old_ts = time.time() - 86400 * 90   # 6 月无消息的老会话
    cid = ingest_incoming(
        st, platform=_PLAT, account_id=_ACCT, chat_key=_CK, name="Sinue",
        text="Never write me again, please", ts=old_ts, msg_id="h1",
        direction="in", backfill=True, backfill_source="history_set",
    )
    assert cid == _CID
    rows = st.list_recent_messages(_CID, limit=5)
    assert len(rows) == 1 and rows[0]["text"].startswith("Never write me again"), \
        "回填必须落库——不能为了不触发而丢消息"
    assert calls == [], "回填历史触发了 new_inbound 回调（= 登录群发的根因未堵）"
    conv = st.get_conversation(_CID) or {}
    assert int(conv.get("unread") or 0) == 0, "历史不算未读"
    st.close()


def test_real_inbound_after_backfill_still_triggers(tmp_path):
    st = InboxStore(tmp_path / "inbox.db")
    calls = _cb_recorder(st)
    ingest_incoming(st, platform=_PLAT, account_id=_ACCT, chat_key=_CK,
                    text="old hello", ts=time.time() - 86400 * 10, msg_id="h1",
                    direction="in", backfill=True, backfill_source="resync")
    assert calls == []
    ingest_incoming(st, platform=_PLAT, account_id=_ACCT, chat_key=_CK,
                    text="hey are you there", ts=time.time(), msg_id="m2",
                    direction="in")
    assert len(calls) == 1 and calls[0][1] == "hey are you there"
    assert len(st.list_recent_messages(_CID, limit=5)) == 2
    conv = st.get_conversation(_CID) or {}
    assert int(conv.get("unread") or 0) == 1
    st.close()


def test_backfill_flag_survives_source_dict_path(tmp_path):
    """A 线 / 桥把 backfill 塞进 source（而非关键字）同样生效。"""
    st = InboxStore(tmp_path / "inbox.db")
    calls = _cb_recorder(st)
    chat = normalize_chat(
        platform="telegram", platform_name="Telegram", account_id="7331682688",
        account_label="a", chat_key="5433982810", name="x", last_msg="hi",
        last_ts=time.time() - 3600, unread=1,
        source={"backfill": 1, "backfill_source": "tg_dialogs", "id": "1"},
    )
    n = ingest_collected_chats(st, [chat], publish_events=True)
    assert n == 1
    assert calls == []
    assert is_backfill_source(chat["last_message"]["source"])
    st.close()


def test_self_chat_detection_and_quiet(tmp_path):
    assert is_self_chat("whatsapp", "17345893728", "17345893728")
    assert is_self_chat("whatsapp", "17345893728", "17345893728:0")
    assert is_self_chat("telegram", "7331682688", "7331682688")
    assert not is_self_chat("whatsapp", "17345893728", "12134989840")
    assert not is_self_chat("whatsapp", "default", "default")
    assert not is_self_chat("line", "u1", "u1")          # LINE 无自聊语义
    assert is_self_chat("line", "u1", "u2", {"self_chat": 1})

    st = InboxStore(tmp_path / "inbox.db")
    calls = _cb_recorder(st)
    cid = ingest_incoming(st, platform="whatsapp", account_id="17345893728",
                          chat_key="17345893728", text="买牛奶", ts=time.time(),
                          msg_id="s1", direction="in")
    assert cid == "whatsapp:17345893728:17345893728"
    assert len(st.list_recent_messages(cid, limit=5)) == 1
    assert calls == [], "自聊会话触发了自动化"
    row = st.get_conversation(cid) or {}
    chat = store_row_to_chat(row)
    assert chat["self_chat"] is True and chat["unread"] == 0
    # 普通客户行不带 self_chat
    ingest_incoming(st, platform="whatsapp", account_id="17345893728",
                    chat_key="12134989840", text="hi", ts=time.time(), msg_id="c1")
    other = store_row_to_chat(st.get_conversation("whatsapp:17345893728:12134989840") or {})
    assert other["self_chat"] is False
    st.close()


def test_sidecar_and_tg_backfill_flag_wiring_static():
    js = (_ROOT / "services" / "whatsapp-baileys" / "server.js").read_text(encoding="utf-8")
    assert "async function pushWaMessage(entry, msg, skipEmpty, backfillSource)" in js
    assert re.search(r'pushWaMessage\(entry, msg, true, _bfSrc\)', js)
    assert 'onDemand ? "resync" : "history_set"' in js
    assert 'backfill: backfillSource ? true : undefined' in js
    assert '_upType === "notify" ? "" : ("upsert_" + _upType)' in js
    pb = (_ROOT / "src" / "integrations" / "protocol_bridge.py").read_text(encoding="utf-8")
    assert 'payload["backfill"] = True' in pb and '"tg_dialogs"' in pb
    rt = (_ROOT / "src" / "web" / "routes" / "unified_inbox_account_routes.py").read_text(
        encoding="utf-8")
    assert "backfill=_backfill," in rt and "and not _backfill:" in rt
