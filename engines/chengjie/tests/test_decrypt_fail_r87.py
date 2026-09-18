# -*- coding: utf-8 -*-
"""#279 P2-2：Bad MAC 占位入站可见 + 会话头黄条；不触发自动起草。"""
from __future__ import annotations

import re
from pathlib import Path

from src.inbox.conv_state import compute
from src.inbox.decrypt_fail_marker import KEY_PREFIX, LIST_PREVIEW, clear, get, mark

_ROOT = Path(__file__).resolve().parents[1]
CID = "whatsapp:acc:66800000001"


class _KV:
    def __init__(self):
        self.kv = {}

    def get_app_setting(self, key, default=""):
        return self.kv.get(key, default)

    def set_app_setting(self, key, value, updated_by=""):
        if value == "":
            self.kv.pop(key, None)
        else:
            self.kv[key] = value
        return True

    def get_conversation(self, cid):
        return {"conversation_id": cid, "platform": "whatsapp"}

    def get_automation_mode(self, cid):
        return "auto_ai"

    def get_automation_mode_if_set(self, cid):
        return "auto_ai"

    def get_automation_mode_meta(self, cid):
        return {"mode": "auto_ai", "source": "human", "updated_at": 0.0}


def test_marker_roundtrip():
    st = _KV()
    assert get(CID, store=st) is None
    rec = mark(CID, store=st, ts=1_000.0)
    assert rec["n"] == 1 and KEY_PREFIX + CID in st.kv
    rec2 = mark(CID, store=st, ts=1_010.0)
    assert rec2["n"] == 2 and rec2["first_ts"] == 1_000.0
    assert get(CID, store=st, now=1_020.0)["n"] == 2
    assert get(CID, store=st, now=1_000.0 + 25 * 3600) is None
    assert clear(CID, store=st) is True
    assert get(CID, store=st, now=1_020.0) is None


def test_conv_state_note_does_not_change_state():
    st = _KV()
    mark(CID, store=st, ts=1_700_000_000.0)
    out = compute(st, CID, platform="whatsapp", account_id="acc", now=1_700_000_010.0)
    kinds = [n.get("kind") for n in (out.get("notes") or [])]
    assert "decrypt_fail" in kinds
    n = next(x for x in out["notes"] if x["kind"] == "decrypt_fail")
    assert n["text_key"] == "inbox.cs.note.decrypt_fail" and n["n"] == 1
    assert out["state"] != "held"


def test_template_pack_and_sidecar_guard():
    html = (_ROOT / "src" / "web" / "templates" / "unified_inbox.html").read_text(
        encoding="utf-8", errors="ignore")
    fn = html[html.index("function _csRenderNotes("):html.index("window._csNoteAction=")]
    assert "decrypt_fail" in fn and "inbox.cs.note.decrypt_fail" in fn
    js = (_ROOT / "services" / "whatsapp-baileys" / "server.js").read_text(encoding="utf-8")
    chunk = js.split("async function pushWaMessage")[1][:2500]
    assert "decryptFailIngestFields" in chunk
    assert "decrypt_fail: decryptFields" in js
    assert "if (!msg || !msg.message) return false;" not in chunk[:500]
    from src.web.i18n_packs import decrypt_fail_r87 as P
    assert set(P.ZH) == set(P.EN) == set(P.ZH_HANT)
    ph = lambda s: set(re.findall(r"\{(\w+)\}", s))  # noqa: E731
    for k in P.ZH:
        assert ph(P.ZH[k]) == ph(P.EN[k]) == ph(P.ZH_HANT[k]), k
    from src.web.i18n_packs import collect_all
    zh, _en, _extras = collect_all()
    assert zh["inbox.cs.note.decrypt_fail"] == P.ZH["inbox.cs.note.decrypt_fail"]


def test_decrypt_fail_counts_unread_but_skips_autodraft(tmp_path):
    import time
    from src.inbox.store import InboxStore
    from src.integrations.protocol_bridge import ingest_incoming

    plat, acct, ck = "whatsapp", "447546050758", "447349041791"
    cid = f"{plat}:{acct}:{ck}"
    st = InboxStore(tmp_path / "inbox.db")
    calls = []
    st.register_new_inbound_cb(lambda conv, text: calls.append(text))
    got = ingest_incoming(
        st, platform=plat, account_id=acct, chat_key=ck, name="Cam",
        text="[无法解密的消息 · 会话将自动重建]", ts=time.time(), msg_id="bm1",
        direction="in", decrypt_fail=True,
    )
    assert got == cid
    rows = st.list_recent_messages(cid, limit=3)
    assert rows and "无法解密" in rows[0]["text"]
    assert calls == []
    conv = st.get_conversation(cid) or {}
    assert int(conv.get("unread") or 0) == 1
    assert str(conv.get("last_text") or "") == LIST_PREVIEW
    assert get(cid, store=st) is not None
    st.close()
