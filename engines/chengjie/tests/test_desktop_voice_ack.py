"""P0-6（2026-09-19）：桌面桥 kind=voice 命令的 ack 处置——镜像出站行 + 指标 + 能力型失败回落文字。"""
from types import SimpleNamespace

import pytest

from src.inbox import desktop_voice_ack as dva
from src.inbox.desktop_outbound import DesktopOutboundQueue


def _allow(*a, **k):
    return False, ""


def _q():
    return DesktopOutboundQueue(":memory:")


def _voice_item(q, **over):
    r = q.enqueue("wechat", "wx1", "wx:name:张三", "念稿文本", kind="voice", guard=_allow,
                  media_url="/static/outbound/wechat/wx1/v.ogg", media_ref=r"C:\tmp\v.ogg",
                  duration_ms=8600, inbox_text="念稿文本", sender_name="Claire", **over)
    assert r["enqueued"]
    return q.get(r["id"])


class _Store:
    pass


# ── 解析 ──────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("err,kind,stage,reason", [
    ("guard:record:voice_not_ready", "guard", "record", "voice_not_ready"),
    ("guard:play:mic_switch_failed:boom", "guard", "play", "mic_switch_failed:boom"),
    ("guard:cancel:finish_record_failed", "guard", "cancel", "finish_record_failed"),
    ("policy:tier_forbids_kind", "policy", "policy", "tier_forbids_kind"),
    ("", "", "", ""),
    ("weird", "other", "", "weird"),
])
def test_parse_ack_error(err, kind, stage, reason):
    assert dva.parse_ack_error(err) == {"kind": kind, "stage": stage, "reason": reason}


@pytest.mark.parametrize("err,expect", [
    ("guard:record:voice_not_ready", True),
    ("guard:record:media_unavailable", True),
    ("guard:play:x", True),
    ("guard:title:mismatch", False),
    ("guard:send:x", False),
    ("guard:echo:x", False),
    ("guard:cancel:x", False),
    ("policy:daily_cap", False),
    ("policy:voice_daily_cap", True),
    ("policy:voice_per_peer_daily_cap", True),
    ("policy:min_gap", False),
    ("", False),
])
def test_should_fallback_to_text(err, expect):
    assert dva.should_fallback_to_text(err) is expect


def test_voice_quota_denial_falls_back_to_text_keeping_reply_group(monkeypatch):
    """P2 语音配额只限「用语音」：驱动回 policy:voice_* → 同稿改发文字，组号带过去（分条节奏不断）。"""
    import src.inbox.voice_autosend as va
    fb = []
    monkeypatch.setattr(va, "record_voice_fallback", lambda r: fb.append(r))
    q = _q()
    it = _voice_item(q, reply_group="vg-abc")
    assert it["reply_group"] == "vg-abc"
    q.pull("wechat", "wx1")
    q.ack(it["id"], ok=False, error="policy:voice_per_peer_daily_cap")
    out = dva.handle_voice_ack(q, it, ok=False, error="policy:voice_per_peer_daily_cap", config={}, registry=None)
    assert out["reason"] == "driver_voice_per_peer_daily_cap" and fb == ["driver_voice_per_peer_daily_cap"]
    assert out["fallback"]["enqueued"] is True
    items = q.pull("wechat", "wx1")
    assert len(items) == 1 and items[0]["kind"] == "text" and items[0]["text"] == "念稿文本"
    assert items[0]["reply_group"] == "vg-abc"


# ── 成功：镜像 + 指标 ────────────────────────────────────────────────────────
def test_ok_mirrors_out_row_and_records_sent(monkeypatch):
    import src.inbox.voice_autosend as va
    import src.integrations.protocol_bridge as pb
    sent = []
    monkeypatch.setattr(va, "record_voice_sent", lambda dur, synth_meta=None: sent.append((dur, synth_meta)))
    ingested = []

    def _ingest(store, **kw):
        ingested.append(kw)
        return "conv-1"

    monkeypatch.setattr(pb, "ingest_incoming", _ingest)
    q = _q()
    it = _voice_item(q)
    q.ack(it["id"], ok=True)
    out = dva.handle_voice_ack(q, it, ok=True, store=_Store(), now=1000.0, extra={"echo": "语音9秒"})
    assert out["handled"] is True and out["mirrored"] is True and out["conversation_id"] == "conv-1"
    assert len(ingested) == 1
    row = ingested[0]
    assert row["direction"] == "out" and row["media_type"] == "voice"
    assert row["media_ref"] == "/static/outbound/wechat/wx1/v.ogg"
    assert row["text"] == "念稿文本" and row["sender_name"] == "Claire"
    assert row["source"]["sent_by"] == "ai" and row["source"]["sender_name"] == "Claire"
    assert row["source"]["voice_duration_ms"] == 8600 and row["source"]["bridge"] == "wechat_pc"
    assert row["ts"] == 1000.0 and row["msg_id"] == ""
    assert sent == [(8600, {"provider": "desktop_bridge", "persona_id": "Claire", "audio_duration_ms": 8600})]
    # 成功不回落
    assert q.pull("wechat", "wx1") == []


def test_ok_without_store_still_records_metric(monkeypatch):
    import src.inbox.voice_autosend as va
    sent = []
    monkeypatch.setattr(va, "record_voice_sent", lambda dur, synth_meta=None: sent.append(dur))
    q = _q()
    it = _voice_item(q)
    out = dva.handle_voice_ack(q, it, ok=True, store=None)
    assert out["mirrored"] is False and sent == [8600]


def test_mirror_failure_is_swallowed(monkeypatch):
    import src.integrations.protocol_bridge as pb

    def _boom(store, **kw):
        raise RuntimeError("db locked")

    monkeypatch.setattr(pb, "ingest_incoming", _boom)
    q = _q()
    it = _voice_item(q)
    out = dva.handle_voice_ack(q, it, ok=True, store=_Store())
    assert out["handled"] is True and out["mirrored"] is False


# ── 失败：能力型回落文字 / 守卫型不回落 ────────────────────────────────────────
def test_capability_failure_falls_back_to_text(monkeypatch):
    import src.inbox.voice_autosend as va
    fb = []
    monkeypatch.setattr(va, "record_voice_fallback", lambda r: fb.append(r))
    q = _q()
    it = _voice_item(q, conversation_id="conv-9")
    q.pull("wechat", "wx1")
    q.ack(it["id"], ok=False, error="guard:record:voice_not_ready")
    out = dva.handle_voice_ack(q, it, ok=False, error="guard:record:voice_not_ready", config={}, registry=None)
    assert out["reason"] == "driver_record" and fb == ["driver_record"]
    assert out["fallback"]["enqueued"] is True
    items = q.pull("wechat", "wx1")
    assert len(items) == 1
    t = items[0]
    assert t["kind"] == "text" and t["text"] == "念稿文本" and t["chat_key"] == "wx:name:张三"
    assert t["conversation_id"] == "conv-9" and t["media_url"] == "" and t["duration_ms"] == 0


def test_mic_busy_failure_falls_back_to_text_with_own_reason(monkeypatch):
    """P2-3：坐席正在用麦 → 同稿改发文字；指标原因单列 driver_mic_busy，且不入语音断档台账。"""
    import src.inbox.voice_autosend as va
    from src.ai.voice_outage import is_capability_skip
    fb = []
    monkeypatch.setattr(va, "record_voice_fallback", lambda r: fb.append(r))
    q = _q()
    it = _voice_item(q)
    q.pull("wechat", "wx1")
    err = "guard:record:mic_busy:Zoom.exe,Teams.exe"
    q.ack(it["id"], ok=False, error=err)
    assert dva.should_fallback_to_text(err) is True
    out = dva.handle_voice_ack(q, it, ok=False, error=err, config={}, registry=None)
    assert out["reason"] == "driver_mic_busy" and fb == ["driver_mic_busy"]
    assert out["fallback"]["enqueued"] is True
    assert q.pull("wechat", "wx1")[0]["kind"] == "text"
    assert is_capability_skip("driver_mic_busy") and is_capability_skip("driver_voice_daily_cap")
    assert not is_capability_skip("driver_record")


def test_guard_freeze_failure_does_not_fall_back(monkeypatch):
    import src.inbox.voice_autosend as va
    fb = []
    monkeypatch.setattr(va, "record_voice_fallback", lambda r: fb.append(r))
    q = _q()
    it = _voice_item(q)
    q.pull("wechat", "wx1")
    q.ack(it["id"], ok=False, error="guard:cancel:finish_record_failed")
    out = dva.handle_voice_ack(q, it, ok=False, error="guard:cancel:finish_record_failed")
    assert out["reason"] == "driver_cancel" and "fallback" not in out and fb == ["driver_cancel"]
    assert q.pull("wechat", "wx1") == []


def test_policy_denial_does_not_fall_back(monkeypatch):
    import src.inbox.voice_autosend as va
    monkeypatch.setattr(va, "record_voice_fallback", lambda r: None)
    q = _q()
    it = _voice_item(q)
    q.pull("wechat", "wx1")
    q.ack(it["id"], ok=False, error="policy:tier_forbids_kind")
    out = dva.handle_voice_ack(q, it, ok=False, error="policy:tier_forbids_kind")
    assert out["reason"] == "driver_policy" and "fallback" not in out
    assert q.pull("wechat", "wx1") == []


def test_fallback_goes_through_guard(monkeypatch):
    import src.inbox.voice_autosend as va
    import src.inbox.desktop_outbound as do
    monkeypatch.setattr(va, "record_voice_fallback", lambda r: None)
    monkeypatch.setattr(do, "_default_guard", lambda *a, **k: (True, "kill_switch:global"))
    q = _q()
    it = _voice_item(q)
    out = dva.handle_voice_ack(q, it, ok=False, error="guard:play:x")
    assert out["fallback"]["enqueued"] is False and out["fallback"]["blocked"] == "kill_switch:global"


def test_non_voice_item_not_handled():
    q = _q()
    r = q.enqueue("wechat", "wx1", "c", "文字", guard=_allow)
    assert dva.handle_voice_ack(q, q.get(r["id"]), ok=True) == {"handled": False}
    assert dva.handle_voice_ack(q, None, ok=True) == {"handled": False}


# ── 路由集成：ack 端点首次命中才处置 ────────────────────────────────────────────
def test_route_ack_dispatches_voice_once(monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import src.inbox.desktop_outbound as do
    import src.web.routes.unified_inbox_desktop_routes as routes

    q = _q()
    monkeypatch.setattr(do, "get_desktop_outbound_queue", lambda: q)
    calls = []
    import src.inbox.desktop_voice_ack as _dva
    monkeypatch.setattr(_dva, "handle_voice_ack",
                        lambda queue, item, **kw: calls.append((item["id"], kw["ok"], kw["error"], kw.get("extra"))) or {"handled": True})

    app = FastAPI()
    app.state.inbox_store = _Store()
    app.state.config_manager = SimpleNamespace(config={})
    routes.register_desktop_routes(app, api_auth=lambda: None)
    c = TestClient(app)
    it = _voice_item(q)
    q.pull("wechat", "wx1")
    r = c.post("/api/desktop/outbound/ack", json={"id": it["id"], "ok": True, "delivered_as": "voice", "echo": "语音9秒"})
    assert r.status_code == 200, r.text
    assert r.json()["acked"] is True and r.json()["voice"] == {"handled": True}
    assert calls == [(it["id"], True, "", {"delivered_as": "voice", "echo": "语音9秒"})]
    # 重复 ack 不再命中 → 不重复处置（不双镜像 / 不双回落）
    r2 = c.post("/api/desktop/outbound/ack", json={"id": it["id"], "ok": True})
    assert r2.json()["acked"] is False and "voice" not in r2.json()
    assert len(calls) == 1
    # 文字命令 ack 不进语音处置
    t = q.enqueue("wechat", "wx1", "c", "文字", guard=_allow)
    q.pull("wechat", "wx1")
    r3 = c.post("/api/desktop/outbound/ack", json={"id": t["id"], "ok": True})
    assert r3.json()["acked"] is True and "voice" not in r3.json() and len(calls) == 1


def test_inbox_failr_maps_voice_reasons_before_generic_other():
    """工作台失败气泡：麦占用 / 语音配额必须走人话，不能掉进 inbox.failr.other 裸码。"""
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "src" / "web" / "templates" / "unified_inbox.html").read_text(
        encoding="utf-8")
    fn = src.split("function _failReasonText", 1)[1].split("window._failReasonText", 1)[0]
    for needle in ("mic_busy", "voice_daily_cap", "voice_per_peer",
                   "inbox.failr.voice_mic_busy", "inbox.failr.voice_daily_cap", "inbox.failr.voice_per_peer"):
        assert needle in fn, needle
    assert fn.find("if(low.indexOf('mic_busy')") < fn.find("if(low.indexOf('send_gate:daily_cap')")
