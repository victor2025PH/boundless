# -*- coding: utf-8 -*-
"""Q-24（#298 P0）Messenger 网页代发链止血——Python 侧契约门禁。

覆盖：边车七码 → http_error_fields / MessengerWebWorker.send / 路由结构化 502 字段；
连败铃铛（session-status send_stuck → sys_status）；失败留痕改标 / 近期失败读取；
reply_diagnosis sidecar_send_fail；出站媒体上下文标签；update_message_text 方向闸；
i18n 三语词条齐全。Node 侧对应 services/messenger-web/send_chain.test.js。
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.integrations.messenger_web_login import (
    SIDECAR_SEND_FAIL_CODES, http_error_fields, sidecar_fail_code,
)

DETACHED = "elementHandle.click: Element is not attached to the DOM"


class _Resp:
    def __init__(self, status: int, body):
        self.status_code = status
        self._body = body

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class _HttpErr(Exception):
    def __init__(self, status: int, body):
        super().__init__(f"Server error '{status}' for url http://x/send")
        self.response = _Resp(status, body)


# ── 七码归一 ──────────────────────────────────────────────────────────────────

def test_seven_codes_frozen():
    assert sorted(SIDECAR_SEND_FAIL_CODES) == sorted([
        "composer_detached", "thread_not_found", "e2ee_pin_pending", "call_overlay",
        "send_backoff", "login_expired", "upload_failed"])


@pytest.mark.parametrize("reason,detail,expect", [
    ("not_logged_in", "", "login_expired"),
    ("e2ee_pin_prompt", "", "e2ee_pin_pending"),
    ("", "e2ee recovery pin prompt on screen", "e2ee_pin_pending"),
    ("account_blocked", "", "send_backoff"),
    ("send_backoff", "", "send_backoff"),
    ("", "Ongoing call", "call_overlay"),
    ("exception", DETACHED, "composer_detached"),   # DXAPAX：含 attach 字样也不能落 upload_failed
    ("file_chooser_timeout", "", "upload_failed"),
    ("page_not_rendered", "", "thread_not_found"),
    ("", "page.goto: net::ERR_CONNECTION_RESET", "thread_not_found"),
    ("", "", "composer_detached"),
])
def test_sidecar_fail_code_mapping(reason, detail, expect):
    assert sidecar_fail_code(reason, detail) == expect


def test_http_error_fields_reads_new_code_contract():
    ex = _HttpErr(500, {"ok": False, "code": "composer_detached", "reason": "exception",
                        "detail": DETACHED, "retry_after_ms": 0, "retries": 3,
                        "reason_code": "exception", "error": DETACHED})
    f = http_error_fields(ex)
    assert f["code"] == "composer_detached"
    assert f["retries"] == 3
    assert f["status"] == 500
    assert DETACHED[:20] in f["sidecar_detail"]
    assert f["reason_code"] == "exception"
    assert "exception" in f["detail"]


def test_http_error_fields_old_sidecar_body_normalizes_code():
    # 老边车：只有 reason_code / error，无 code → Python 侧归一
    ex = _HttpErr(429, {"ok": False, "reason_code": "send_backoff", "retry_after_ms": 40000,
                        "error": "consecutive failures"})
    f = http_error_fields(ex)
    assert f["code"] == "send_backoff"
    assert f["retry_after_ms"] == 40000
    ex2 = _HttpErr(500, {"error": DETACHED})
    assert http_error_fields(ex2)["code"] == "composer_detached"


def test_http_error_fields_non_json_body_is_safe():
    f = http_error_fields(_HttpErr(502, ValueError("no json")))
    assert f["code"] == ""
    assert f["retries"] == 0
    f2 = http_error_fields(RuntimeError("plain"))
    assert f2["code"] == "" and f2["status"] == 0


# ── MessengerWebWorker.send / send_media 透传 ─────────────────────────────────

def _worker():
    from src.integrations.account_orchestrator import MessengerWebWorker
    w = MessengerWebWorker({"account_id": "acc1"}, {})
    w._session_unhealthy = lambda: False  # type: ignore[assignment]
    w._base = lambda: "http://127.0.0.1:1"  # type: ignore[assignment]
    return w


def test_worker_send_failure_carries_sidecar_code(monkeypatch):
    import src.integrations.messenger_web_login as mwl

    async def _boom(url, payload, **kw):
        raise _HttpErr(500, {"ok": False, "code": "composer_detached", "reason": "exception",
                             "detail": DETACHED, "retries": 2})
    monkeypatch.setattr(mwl, "_post_json", _boom)
    monkeypatch.setattr("src.integrations.platform_session_health.note_send_auth_failure",
                        lambda *a, **k: None, raising=False)
    res = asyncio.run(_worker().send("1000123", "hello"))
    assert res["delivered"] is False
    assert res["sidecar_code"] == "composer_detached"
    assert res["error_kind"] == "composer_detached"
    assert res["sidecar_retries"] == 2
    assert DETACHED[:15] in res["sidecar_detail"]


def test_worker_send_2xx_ok_false_still_coded(monkeypatch):
    import src.integrations.messenger_web_login as mwl

    async def _soft(url, payload, **kw):
        return {"ok": False, "delivered": False, "reason": "bubble_fail_marker",
                "code": "thread_not_found", "detail": "messenger marked the message as failed"}
    monkeypatch.setattr(mwl, "_post_json", _soft)
    res = asyncio.run(_worker().send("1000123", "hello"))
    assert res["delivered"] is False
    assert res["sidecar_code"] == "thread_not_found"


def test_worker_send_media_failure_is_structured_not_raised(monkeypatch, tmp_path):
    import src.integrations.messenger_web_login as mwl

    async def _boom(url, payload, **kw):
        raise _HttpErr(502, {"ok": False, "code": "upload_failed", "reason": "attach_failed",
                             "detail": "file chooser did not open", "retries": 1})
    monkeypatch.setattr(mwl, "_post_json", _boom)
    monkeypatch.setattr("src.integrations.platform_session_health.note_send_auth_failure",
                        lambda *a, **k: None, raising=False)
    p = tmp_path / "a.jpg"
    p.write_bytes(b"\xff\xd8\xff")
    res = asyncio.run(_worker().send_media("1000123", media_path=str(p), media_type="image"))
    assert res["delivered"] is False
    assert res["sidecar_code"] == "upload_failed"


def test_worker_send_success_unchanged(monkeypatch):
    import src.integrations.messenger_web_login as mwl

    async def _ok(url, payload, **kw):
        return {"ok": True, "delivered": True, "message_id": "m1", "verified": True}
    monkeypatch.setattr(mwl, "_post_json", _ok)
    res = asyncio.run(_worker().send("1000123", "hello"))
    assert res == {"delivered": True, "message_id": "m1", "error": ""}


# ── 路由层结构化 502 字段 ───────────────────────────────────────────────────────

def test_sidecar_fail_fields_shape():
    from src.inbox.send_failure_class import classify_send_failure, sidecar_fail_fields
    d = sidecar_fail_fields({"delivered": False, "sidecar_code": "composer_detached",
                             "sidecar_retries": 2, "retry_after_ms": 40000,
                             "sidecar_detail": DETACHED, "sidecar_reason": "exception"})
    assert d["code"] == "sidecar_send_fail"
    assert d["sidecar_code"] == "composer_detached"
    assert d["retries"] == 2
    assert d["retry_after_sec"] == 40
    assert sidecar_fail_fields({"delivered": False, "error": "x"}) == {}
    assert sidecar_fail_fields({"delivered": False, "sidecar_code": "bogus"}) == {}
    # 人话分类不掉兜底：新码进既有五类
    assert classify_send_failure("", "thread_not_found") == "channel"
    assert classify_send_failure("", "login_expired") == "session"
    assert classify_send_failure("", "e2ee_pin_pending") == "e2ee_pin"
    assert classify_send_failure("", "composer_detached") == "channel"


# ── 连败铃铛 ──────────────────────────────────────────────────────────────────

def test_send_stuck_detail_parse_and_bell_dedupe():
    from src.inbox.sidecar_send_alert import (
        is_send_stuck_status, parse_send_stuck_detail, push_send_stuck_bell,
    )
    assert is_send_stuck_status("send_stuck") and is_send_stuck_status("SEND_RECOVERED")
    assert not is_send_stuck_status("authorized")
    d = parse_send_stuck_detail("composer_detached|jid=1000123|streak=2|preview=hello there")
    assert d == {"code": "composer_detached", "jid": "1000123", "streak": "2",
                 "preview": "hello there", "tries": ""}
    d2 = parse_send_stuck_detail("jid=1000123|tries=1")
    assert d2["code"] == "" and d2["jid"] == "1000123" and d2["tries"] == "1"

    app = SimpleNamespace(state=SimpleNamespace(notif_queue=[]))
    it = push_send_stuck_bell(app, platform="messenger", account_id="acc1", status="send_stuck",
                              detail="composer_detached|jid=1000123|streak=2|preview=hi")
    assert it and it["type"] == "sys_status"
    assert it["data"]["id"].startswith("ms_send_stuck:messenger:acc1:1000123")
    assert it["data"]["sidecar_code"] == "composer_detached"
    assert "Messenger" in it["data"]["text"]
    assert it["data"]["conversation_id"] == "messenger:acc1:1000123"
    # 同会话 recovered 覆盖 stuck（铃铛不堆叠）
    it2 = push_send_stuck_bell(app, platform="messenger", account_id="acc1", status="send_recovered",
                               detail="jid=1000123|tries=1")
    assert it2["data"]["level"] == "info"
    ids = [n["data"]["id"] for n in app.state.notif_queue]
    assert ids.count(it["data"]["id"]) == 1
    assert push_send_stuck_bell(app, platform="messenger", account_id="acc1",
                                status="authorized", detail="") is None


# ── 留痕 / 读取 / 诊断 ─────────────────────────────────────────────────────────

@pytest.fixture
def store(tmp_path: Path):
    from src.inbox.store import InboxStore
    s = InboxStore(tmp_path / "inbox.db")
    yield s
    try:
        s._conn.close()
    except Exception:
        pass


def test_failed_row_resent_by_text_and_recent_failed(store):
    cid = "messenger:acc1:1000123"
    mid = store.record_failed_outbound(cid, "hello", reason="composer_detached")
    assert mid
    rows = store.recent_failed_outbound(cid)
    assert len(rows) == 1 and rows[0]["fail_reason"] == "composer_detached"
    assert store.mark_failed_outbound_resent_by_text(cid, "other text") == ""
    assert store.mark_failed_outbound_resent_by_text(cid, "hello") == mid
    assert store.recent_failed_outbound(cid) == []
    # 二次改标无行可改
    assert store.mark_failed_outbound_resent_by_text(cid, "hello") == ""


def test_reply_diagnosis_sidecar_send_fail_finding(store):
    from src.inbox.reply_diagnosis import diagnose_conversation
    cid = "messenger:acc1:1000123"
    store.record_failed_outbound(cid, "hello", reason="composer_detached")
    store.record_failed_outbound(cid, "hello again", reason=DETACHED)
    out = diagnose_conversation(store, {}, platform="messenger", account_id="acc1",
                                chat_key="1000123")
    f = [x for x in out["findings"] if x["code"] == "sidecar_send_fail"]
    assert len(f) == 1
    assert f[0]["level"] == "warn"
    assert f[0]["params"]["code"] == "composer_detached"
    assert f[0]["params"]["n"] == 2
    assert out["sidecar_send_fail"]["n"] == 2
    # 非 messenger / 无留痕 → 无该 finding
    out2 = diagnose_conversation(store, {}, platform="whatsapp", account_id="acc1",
                                 chat_key="1000123")
    assert not [x for x in out2["findings"] if x["code"] == "sidecar_send_fail"]


def test_update_message_text_by_media_ref_skips_outbound(store):
    from src.inbox.models import InboxMessage
    cid = "messenger:acc1:1000123"
    now = time.time()
    store.ingest_message(InboxMessage(conversation_id=cid, platform_msg_id="o1", direction="out",
                                      text="", media_type="image", media_ref="/static/x/o.jpg",
                                      ts=now))
    store.ingest_message(InboxMessage(conversation_id=cid, platform_msg_id="i1", direction="in",
                                      text="", media_type="image", media_ref="/static/x/i.jpg",
                                      ts=now + 1))
    assert store.update_message_text(cid, text="客户发的图：一只猫", media_ref="/static/x/o.jpg") is False
    assert store.update_message_text(cid, text="客户发的图：一只猫", media_ref="/static/x/i.jpg") is True
    # 显式 message_id 定位不受方向闸影响（调用方明确知道在改哪条）
    rows = store._conn.execute("SELECT message_id, direction, text FROM messages WHERE conversation_id=?",
                               (cid,)).fetchall()
    by_dir = {r["direction"]: r for r in rows}
    assert by_dir["out"]["text"] == ""
    assert by_dir["in"]["text"].startswith("客户发的图")


def test_normalize_history_labels_outbound_media():
    from src.inbox.persona_reply import normalize_history
    hist, last_in = normalize_history([
        {"direction": "in", "text": "", "media_type": "image", "media_ref": "/a.jpg"},
        {"direction": "out", "text": "", "media_type": "image", "media_ref": "/b.jpg"},
        {"direction": "out", "text": "", "media_type": "video", "media_ref": "/c.mp4"},
        {"direction": "in", "text": "好看吗"},
    ])
    assert hist[0]["role"] == "user" and "我方" not in hist[0]["content"]
    # 空正文出站媒体仍占位（否则这一轮从历史消失）；占位是纯表情替身、形态挂带外 media
    # 字段——assistant 内容里不再有任何方括号系统标签可供模型照抄（#332）
    assert hist[1] == {"role": "assistant", "content": "📷", "media": "image"}
    assert hist[2]["role"] == "assistant" and hist[2]["content"] == "🎬"
    assert hist[2]["media"] == "video"
    for r in hist:
        if r["role"] == "assistant":
            assert "[" not in r["content"], r
    assert last_in == "好看吗"


# ── i18n ──────────────────────────────────────────────────────────────────────

def test_i18n_pack_covers_seven_codes_in_three_langs():
    from src.web.i18n_packs import messenger_sidecar_q24 as pk
    for code in SIDECAR_SEND_FAIL_CODES:
        k = f"inbox.ms.fail.{code}"
        assert k in pk.ZH and k in pk.EN and k in pk.ZH_HANT, k
    for k in ("inbox.ms.fail.retried", "inbox.ms.fail.next_phone", "inbox.ms.fail.retry_btn",
              "inbox.ms.bell.send_stuck", "inbox.ms.bell.send_recovered",
              "inbox.ms.bell.send_stuck_final", "inbox.ms.pin_bar", "inbox.ms.external_out",
              "inbox.ms.media_out_label", "inbox.diag.sidecar_send_fail"):
        assert k in pk.ZH and k in pk.EN and k in pk.ZH_HANT, k
    assert set(pk.ZH) == set(pk.EN) == set(pk.ZH_HANT)
    assert pk.ZH["inbox.ms.pin_bar"] == "Messenger 需在手机确认 PIN 后才能同步"
