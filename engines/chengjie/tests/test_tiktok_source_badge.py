"""TK-3 E3：TikTok 会话来源徽标（官方 / 真机 / 网页）+ 个人号「消息请求 · 对方未回」。"""
from __future__ import annotations

from typing import Any, Dict, List

from src.integrations import tiktok_source_badge as sb

T0 = 1_800_000_000.0


class _Reg:
    def __init__(self, modes: Dict[str, str]):
        self.modes, self.calls = modes, 0

    def get(self, platform, account_id):
        self.calls += 1
        m = self.modes.get(account_id)
        return {"mode": m} if m else None


class _Store:
    def __init__(self, last_in: Dict[str, float]):
        self.last_in = last_in

    def get_conversation(self, cid):
        return {"last_in_ts": self.last_in[cid]} if cid in self.last_in else None


def test_source_for_rules():
    assert sb.source_for("tiktok:comment:u1", "") == "personal_rpa"
    assert sb.source_for("tiktok:user:b", "official") == "official"
    assert sb.source_for("tiktok:user:b", "personal_rpa") == "personal_rpa"
    assert sb.source_for("tiktok:user:b", "web") == "web"
    assert sb.source_for("tiktok:user:b", "shop") == "" and sb.source_for("tiktok:user:b", "") == ""


def test_annotate_by_account_mode_and_never_replied_only_for_personal():
    reg = _Reg({"acc-off": "official", "acc-rpa": "personal_rpa", "acc-web": "web"})
    store = _Store({"c1": 0.0, "c2": T0, "c3": 0.0, "c4": 0.0})
    chats: List[Dict[str, Any]] = [
        {"platform": "tiktok", "account_id": "acc-off", "chat_key": "tiktok:user:a", "conversation_id": "c1"},
        {"platform": "tiktok", "account_id": "acc-rpa", "chat_key": "tiktok:user:b", "conversation_id": "c2"},
        {"platform": "tiktok", "account_id": "acc-rpa", "chat_key": "tiktok:user:c", "conversation_id": "c3"},
        {"platform": "tiktok", "account_id": "acc-web", "chat_key": "tiktok:user:d", "conversation_id": "c4"},
        {"platform": "tiktok", "account_id": "acc-unknown", "chat_key": "tiktok:comment:u9", "conversation_id": "c9"},
        {"platform": "tiktok", "account_id": "acc-unknown", "chat_key": "tiktok:user:z", "conversation_id": "c10"},
        {"platform": "whatsapp", "account_id": "wa", "chat_key": "123", "conversation_id": "w1"},
    ]
    n = sb.annotate_tiktok_sources(chats, registry=reg, store=store)
    assert n == 5 and reg.calls == 4  # 每账号只查一次注册表；非 tiktok 不碰
    assert chats[0]["tiktok_source"] == "official" and "peer_never_replied" not in chats[0]  # 官方不打「对方未回」
    assert chats[1]["tiktok_source"] == "personal_rpa" and "peer_never_replied" not in chats[1]  # 有入站
    assert chats[2]["tiktok_source"] == "personal_rpa" and chats[2]["peer_never_replied"] is True
    assert chats[3]["tiktok_source"] == "web" and chats[3]["peer_never_replied"] is True
    assert chats[4]["tiktok_source"] == "personal_rpa"  # 评论线索恒真机，mode 未知也打
    assert "tiktok_source" not in chats[5] and "tiktok_source" not in chats[6]
    assert sb.annotate_tiktok_sources([{"platform": "line", "chat_key": "x"}], registry=reg, store=store) == 0


def test_chats_api_marks_huoke_dm_and_clears_after_peer_replies(auth_client, app, tmp_path, monkeypatch):
    """真路由：huoke 首触回显（只有 out）→ 列表「真机 + 对方未回」；对方回一条 → 「对方未回」消失、来源仍真机。"""
    from src.integrations import protocol_bridge as pb
    from src.integrations import tiktok_huoke_bridge as hb
    from src.inbox.store import InboxStore
    import asyncio
    store = InboxStore(tmp_path / "inbox_badge.db")
    app.state.inbox_store = store
    st = hb.TikTokHuokeStateStore(":memory:")
    monkeypatch.setattr(hb, "get_state_store", lambda *_a, **_k: st)
    cfg = {"tiktok": {"huoke_bridge": {"enabled": True}}}
    acc, dev, chat = "acc-badge", "dev-badge", "tiktok:user:buyer_x"
    from src.integrations.account_registry import get_account_registry
    reg = get_account_registry()  # conftest 已隔离到 tmp
    hb.bind_device({"device_id": dev, "account_id": acc, "timezone": "Asia/Manila"}, config=cfg, state=st, now=T0, registry=reg)
    assert (reg.get("tiktok", acc) or {}).get("mode") == "personal_rpa"
    emit = lambda m: pb.ingest_incoming(store, **m)

    async def _noop(_m):
        return None

    status, res = asyncio.run(hb.ingest_dm({"device_id": dev, "account_id": acc, "messages": [
        {"msg_id": "o1", "peer_username": "buyer_x", "text": "Hi! saw you liked our video", "direction": "out", "ts": T0}]},
        config=cfg, state=st, now=T0, emit=emit, auto_reply=_noop, registry=reg))
    assert status == 200 and res["echo"] == 1
    r = auth_client.get("/api/unified-inbox/chats", params={"platform": "tiktok", "limit": 30})
    mine = [c for c in (r.json().get("chats") or []) if c.get("chat_key") == chat]
    assert mine and mine[0]["tiktok_source"] == "personal_rpa" and mine[0].get("peer_never_replied") is True, mine
    status, res = asyncio.run(hb.ingest_dm({"device_id": dev, "account_id": acc, "messages": [
        {"msg_id": "i1", "peer_username": "buyer_x", "text": "how much?", "direction": "in", "ts": T0 + 60}]},
        config=cfg, state=st, now=T0 + 60, emit=emit, auto_reply=_noop, registry=reg))
    assert status == 200 and res["accepted"] == 1
    r = auth_client.get("/api/unified-inbox/chats", params={"platform": "tiktok", "limit": 30})
    mine = [c for c in (r.json().get("chats") or []) if c.get("chat_key") == chat]
    assert mine and mine[0]["tiktok_source"] == "personal_rpa" and not mine[0].get("peer_never_replied"), mine
    # 词包三语齐平；模板函数与 CSS 类在位
    from src.web.i18n_packs import tiktok_source_badge as pk
    assert set(pk.ZH) == set(pk.EN) == set(pk.ZH_HANT) and "非官方" in pk.ZH["inbox.tt.src_rpa_t"]
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[1] / "src" / "web"
    html = (root / "templates" / "unified_inbox.html").read_text(encoding="utf-8")
    css = (root / "static" / "workspace" / "unified-inbox.css").read_text(encoding="utf-8")
    assert "function _convTikTokTags(c)" in html and "${_convTikTokTags(c)}" in html and "c.tiktok_source||''" in html
    assert ".conv-src-chip.personal_rpa" in css and ".conv-req-chip.pending" in css
