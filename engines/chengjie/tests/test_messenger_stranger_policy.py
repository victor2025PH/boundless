"""Messenger 陌生人「消息请求」入站策略门禁。

产品口径（用户拍板）：陌生人首次来讯（Messenger「消息请求」）——
  · 「可能认识 / general」类 → 进收件箱且**允许人设自动回**（不强改档位，走全局默认 auto_ai）。
  · 「垃圾 / spam」类           → **只进收件箱、不自动回**（落库前预置 automation_mode=manual）。

关键不变量：auto-draft(System Z) 在 ingest_incoming 内部即触发，故 spam 会话必须在
**落库前**就把档位预置成 manual（否则回调按默认 auto_ai 已生成草稿 → 可能被 autosend）；
且预置仅在坐席未显式设过档位时进行，尊重人工覆盖。另：avatar_url 须透传落库（会话列表显真头像）。
"""
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.inbox.store import InboxStore
from src.inbox.normalizer import conv_id
from src.web.routes.unified_inbox_account_routes import register_account_routes


def _client(tmp_path):
    app = FastAPI()
    register_account_routes(app, api_auth=lambda request: None, config_manager=None)
    app.state.inbox_store = InboxStore(tmp_path / "inbox.db")
    return TestClient(app), app.state.inbox_store


def test_spam_request_forced_manual_no_autoreply(tmp_path):
    c, store = _client(tmp_path)
    cid = conv_id("messenger", "acc1", "stranger_spam")
    r = c.post("/api/internal/protocol/ingest", json={
        "platform": "messenger", "account_id": "acc1",
        "chat_key": "stranger_spam", "name": "Sketchy Sender",
        "text": "click this link to win $$$", "ts": 1780000000.0,
        "direction": "in", "is_request": True, "request_category": "spam",
    })
    assert r.status_code == 200, r.text
    assert r.json().get("conversation_id") == cid
    # 垃圾请求：进收件箱（消息落库）但档位被预置为 manual → 不自动回。
    assert store.count_messages(cid) == 1
    assert store.get_automation_mode_if_set(cid) == "manual"
    store.close()


def test_general_request_left_default_and_avatar_stored(tmp_path):
    c, store = _client(tmp_path)
    cid = conv_id("messenger", "acc1", "stranger_ok")
    avatar = "/static/protocol_media/messenger/avatars/stranger_ok.jpg"
    r = c.post("/api/internal/protocol/ingest", json={
        "platform": "messenger", "account_id": "acc1",
        "chat_key": "stranger_ok", "name": "Maybe Friend",
        "text": "hi there!", "ts": 1780000001.0, "direction": "in",
        "is_request": True, "request_category": "general",
        "avatar_url": avatar,
    })
    assert r.status_code == 200, r.text
    # 「可能认识」请求：预置 auto_ai → 人设自动 AI 回（本部署全局默认档位未设=review，
    # 若不预置则只出人审草稿、不真发；故必须显式预置成 auto_ai 才兑现「自动回」策略）。
    assert store.get_automation_mode_if_set(cid) == "auto_ai"
    # 头像透传落库（会话列表显真头像，兑现最初诉求）。
    conv = store.get_conversation(cid) or {}
    assert conv.get("avatar_url") == avatar
    store.close()


def test_spam_respects_operator_manual_override_precedence(tmp_path):
    """坐席已把该会话显式设成 auto_ai 时，spam 请求不得反向覆盖（尊重人工决策）。"""
    c, store = _client(tmp_path)
    cid = conv_id("messenger", "acc1", "known_but_spammy")
    store.set_automation_mode(cid, "auto_ai")
    r = c.post("/api/internal/protocol/ingest", json={
        "platform": "messenger", "account_id": "acc1",
        "chat_key": "known_but_spammy", "name": "VIP",
        "text": "promo", "ts": 1780000002.0, "direction": "in",
        "is_request": True, "request_category": "spam",
    })
    assert r.status_code == 200, r.text
    assert store.get_automation_mode_if_set(cid) == "auto_ai"  # 未被 spam 策略覆盖
    store.close()


def test_normal_inbound_not_treated_as_request(tmp_path):
    """非请求（普通好友入站）不受策略影响：不预置 manual。"""
    c, store = _client(tmp_path)
    cid = conv_id("messenger", "acc1", "friend")
    r = c.post("/api/internal/protocol/ingest", json={
        "platform": "messenger", "account_id": "acc1",
        "chat_key": "friend", "name": "Bestie", "text": "在吗",
        "ts": 1780000003.0, "direction": "in",
    })
    assert r.status_code == 200, r.text
    assert store.get_automation_mode_if_set(cid) is None
    store.close()


# ── 2026-08-11 请求可视化：is_request 落库 → chats 透传 → 出站自动清 → 显式处置 ──


def test_request_flag_persisted_and_passed_through(tmp_path):
    """ingest 携带 is_request → conversations 列落库 + store_row_to_chat 透传（前端徽章依据）。"""
    from src.inbox.normalizer import store_row_to_chat
    c, store = _client(tmp_path)
    cid = conv_id("messenger", "acc1", "stranger_vis")
    r = c.post("/api/internal/protocol/ingest", json={
        "platform": "messenger", "account_id": "acc1",
        "chat_key": "stranger_vis", "name": "New Lead",
        "text": "hello?", "ts": 1780000010.0, "direction": "in",
        "is_request": True, "request_category": "general",
    })
    assert r.status_code == 200, r.text
    row = store.get_conversation(cid) or {}
    assert int(row.get("is_request") or 0) == 1
    assert row.get("request_category") == "general"
    chat = store_row_to_chat(row)
    assert chat["is_request"] is True
    assert chat["request_category"] == "general"
    # 普通好友入站不带标记（缺省 0/空 → 前端零徽章）
    c.post("/api/internal/protocol/ingest", json={
        "platform": "messenger", "account_id": "acc1",
        "chat_key": "friend2", "name": "F", "text": "hi",
        "ts": 1780000011.0, "direction": "in",
    })
    row2 = store.get_conversation(conv_id("messenger", "acc1", "friend2")) or {}
    assert int(row2.get("is_request") or 0) == 0
    assert store_row_to_chat(row2)["is_request"] is False
    store.close()


def test_outbound_message_clears_request_flag(tmp_path):
    """回复即接受：出站消息一落库自动撤请求标记；入站不清；重复出站幂等。"""
    from src.inbox.models import InboxMessage
    c, store = _client(tmp_path)
    cid = conv_id("messenger", "acc1", "stranger_reply")
    c.post("/api/internal/protocol/ingest", json={
        "platform": "messenger", "account_id": "acc1",
        "chat_key": "stranger_reply", "name": "Lead",
        "text": "hi", "ts": 1780000020.0, "direction": "in",
        "is_request": True, "request_category": "general",
    })
    assert int((store.get_conversation(cid) or {}).get("is_request") or 0) == 1
    # 又一条入站：标记仍在（客户连发不算处置）
    c.post("/api/internal/protocol/ingest", json={
        "platform": "messenger", "account_id": "acc1",
        "chat_key": "stranger_reply", "name": "Lead",
        "text": "u there?", "ts": 1780000021.0, "direction": "in",
        "is_request": True, "request_category": "general",
    })
    assert int((store.get_conversation(cid) or {}).get("is_request") or 0) == 1
    # 出站镜像落库 → 标记自动清（任何发送路径共用 ingest_message 这一写点）
    assert store.ingest_message(InboxMessage(
        conversation_id=cid, direction="out", text="你好，很高兴认识",
        ts=1780000022.0)) is True
    row = store.get_conversation(cid) or {}
    assert int(row.get("is_request") or 0) == 0
    assert row.get("request_category") == ""
    store.close()


def test_request_setters_idempotent(tmp_path):
    """mark/clear setter 幂等；clear 对非请求会话零影响。"""
    c, store = _client(tmp_path)
    cid = conv_id("messenger", "acc1", "s1")
    c.post("/api/internal/protocol/ingest", json={
        "platform": "messenger", "account_id": "acc1",
        "chat_key": "s1", "name": "S", "text": "yo",
        "ts": 1780000030.0, "direction": "in",
    })
    store.mark_conversation_request(cid, "spam")
    store.mark_conversation_request(cid, "spam")
    assert (store.get_conversation(cid) or {}).get("request_category") == "spam"
    store.clear_conversation_request(cid)
    store.clear_conversation_request(cid)
    assert int((store.get_conversation(cid) or {}).get("is_request") or 0) == 0
    store.close()


def test_request_action_route_validates_and_clears_on_accept(tmp_path, monkeypatch):
    """代理路由：参数校验 400；accept 成功（mock worker）→ 本地标记被清。"""
    import src.integrations.messenger_web_login as mwl
    c, store = _client(tmp_path)
    cid = conv_id("messenger", "acc1", "stranger_btn")
    c.post("/api/internal/protocol/ingest", json={
        "platform": "messenger", "account_id": "acc1",
        "chat_key": "stranger_btn", "name": "Lead",
        "text": "hi", "ts": 1780000040.0, "direction": "in",
        "is_request": True, "request_category": "general",
    })
    # 参数校验
    r = c.post("/api/platforms/messenger/acc1/request-action",
               json={"action": "accept"})
    assert r.status_code == 400
    r = c.post("/api/platforms/messenger/acc1/request-action",
               json={"chat_key": "stranger_btn", "action": "report"})
    assert r.status_code == 400
    # worker 打桩：accept 成功
    calls = {}

    async def _fake_post(url, payload, timeout=30.0):
        calls["url"] = url
        calls["payload"] = payload
        return {"ok": True, "action": "accept", "was_request": True}

    monkeypatch.setattr(mwl, "_post_json", _fake_post)
    r = c.post("/api/platforms/messenger/acc1/request-action",
               json={"chat_key": "stranger_btn", "action": "accept"})
    assert r.status_code == 200, r.text
    assert r.json().get("ok") is True
    assert calls["payload"] == {"jid": "stranger_btn", "action": "accept",
                                "confirm": False}
    assert int((store.get_conversation(cid) or {}).get("is_request") or 0) == 0
    store.close()


def test_request_action_decline_probe_keeps_flag(tmp_path, monkeypatch):
    """decline 未 confirm（worker 返回 probe）→ 未真删，本地标记保留。"""
    import src.integrations.messenger_web_login as mwl
    c, store = _client(tmp_path)
    cid = conv_id("messenger", "acc1", "stranger_keep")
    c.post("/api/internal/protocol/ingest", json={
        "platform": "messenger", "account_id": "acc1",
        "chat_key": "stranger_keep", "name": "Lead",
        "text": "hi", "ts": 1780000050.0, "direction": "in",
        "is_request": True, "request_category": "spam",
    })

    async def _fake_post(url, payload, timeout=30.0):
        return {"ok": True, "action": "decline", "probe": True,
                "button_found": True}

    monkeypatch.setattr(mwl, "_post_json", _fake_post)
    r = c.post("/api/platforms/messenger/acc1/request-action",
               json={"chat_key": "stranger_keep", "action": "decline"})
    assert r.status_code == 200
    assert int((store.get_conversation(cid) or {}).get("is_request") or 0) == 1
    # confirm 真删成功 → 标记清除
    async def _fake_post2(url, payload, timeout=30.0):
        assert payload["confirm"] is True
        return {"ok": True, "action": "decline", "deleted": True}

    monkeypatch.setattr(mwl, "_post_json", _fake_post2)
    r = c.post("/api/platforms/messenger/acc1/request-action",
               json={"chat_key": "stranger_keep", "action": "decline",
                     "confirm": True})
    assert r.status_code == 200
    assert r.json().get("deleted") is True
    assert int((store.get_conversation(cid) or {}).get("is_request") or 0) == 0
    store.close()
