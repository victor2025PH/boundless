# -*- coding: utf-8 -*-
"""Q-26（#301 #302）会话「AI 会不会回」单一状态机 ``src.inbox.conv_state.compute``。

六源各一例（状态 / 文案键 / 动作）+ 优先级 + 只读（compute 全程零写）+ 档位收口
（人审档不出让位 / 作息外 / 语言噪音）。全部用内存假 store，不依赖时区与真边车。
"""
from __future__ import annotations

import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from src.inbox import ai_fail_marker, conv_state, l1_reason, risk_hold
from src.inbox.conv_state import ACTIONS, PRIORITY, STATES, TONE, compute
from src.integrations.protocol_autoreply import HANDOFF_TAG

_NY = "America/New_York"
CID = "messenger:acct1:peer9"


class _Store:
    """最小假 store：KV + 档位 + 标签 + handoff_meta + 失败留痕；记录每次写以证「只读」。"""

    def __init__(self, mode="auto_ai"):
        self.kv = {}
        self.mode = mode
        self.tags = []
        self.handoff_meta = {}
        self.failed = []
        self.writes = 0

    def get_app_setting(self, key, default=""):
        return self.kv.get(key, default)

    def set_app_setting(self, key, value, updated_by=""):
        self.writes += 1
        self.kv[key] = value

    def get_automation_mode(self, cid):
        return self.mode

    def get_conv_tags(self, cid):
        return list(self.tags)

    def get_handoff_meta(self, cid):
        return dict(self.handoff_meta)

    def recent_failed_outbound(self, cid, *, within_sec=86400.0, limit=10):
        return list(self.failed)[:limit]


class _Worker:
    def __init__(self, active=False, until=0.0, remaining=0.0, by="agent_sent"):
        self._st = {"active": active, "by": by, "since": time.time() - 30,
                    "until": until, "remaining": remaining, "draft_id": "d1"}

    def agent_yield_state(self, cid):
        return dict(self._st)


class _Health:
    def __init__(self, inbox=None, sessions=None):
        self._inbox = inbox or {}
        self._sessions = sessions or {}

    def dump(self):
        return {"inbox_health": self._inbox, "sessions": self._sessions}


@pytest.fixture(autouse=True)
def _clean_l1():
    l1_reason._REG.clear()
    yield
    l1_reason._REG.clear()


def _c(store, **kw):
    kw.setdefault("platform", "messenger")
    kw.setdefault("account_id", "acct1")
    kw.setdefault("health", _Health())
    kw.setdefault("config", {})
    return compute(store, CID, **kw)


# ── 契约 ─────────────────────────────────────────────────────────────

def test_contract_shape_and_enums():
    st = _c(_Store())
    for k in ("will_send", "state", "tone", "reason_code", "reason_text_key",
              "until_ts", "action", "params", "sources"):
        assert k in st, k
    assert st["state"] in STATES and st["action"] in ACTIONS
    assert set(PRIORITY) == set(STATES)
    assert set(TONE) == set(STATES) and set(TONE.values()) == {"ok", "warn", "danger", "muted"}
    # Q-30 B/C（2026-09-12）加两源：peer_guard（守卫硬拦拟稿）/ pacing_hold（拟人首回延迟）
    assert set(st["sources"]) == {"mode", "handoff", "sidecar", "off_hours",
                                  "agent_yield", "lang_plan", "ai_last_fail",
                                  "peer_guard", "pacing_hold"}


def test_auto_default_green():
    st = _c(_Store("auto_ai"))
    assert st["state"] == "auto" and st["will_send"] is True and st["tone"] == "ok"
    assert st["action"] == "none" and st["reason_text_key"] == "inbox.cs.auto"


def test_human_review_mode_grey():
    st = _c(_Store("review"))
    assert st["state"] == "human" and st["will_send"] is False and st["tone"] == "muted"
    assert st["params"]["effective"] == "review"


def test_manual_mode_grey_and_beats_everything():
    s = _Store("manual")
    s.tags = [HANDOFF_TAG]
    risk_hold.set(s, CID, "adult:high:xx", "xx", by="test")
    ai_fail_marker.mark(s, CID, reason="timeout", draft_id="d9")
    st = _c(s)
    assert st["state"] == "manual" and st["will_send"] is False and st["tone"] == "muted"


# ── 六源各一例 ────────────────────────────────────────────────────────

def test_held_needs_human_tag_red_ack():
    s = _Store("auto_ai")
    s.tags = [HANDOFF_TAG]
    s.handoff_meta = {"reason": "adult:high:裸照", "ts": time.time() - 120, "source": "adult_grader",
                      "level": "high", "category": "adult", "hits": ["裸照"]}
    st = _c(s)
    assert st["state"] == "held" and st["tone"] == "danger" and st["will_send"] is False
    assert st["action"] == "ack"
    assert st["params"]["category"] == "adult" and st["params"]["level"] == "high"
    assert st["params"]["hit"] == "裸照"
    assert st["reason_text_key"] == "inbox.cs.held"


def test_held_risk_hold_without_tag():
    s = _Store("auto_ai")
    risk_hold.set(s, CID, "privacy", "身份证", by="test")
    st = _c(s)
    assert st["state"] == "held" and st["action"] == "ack"
    assert st["params"]["hit"] == "身份证"
    assert st["params"]["tagged"] is False


def test_sidecar_pin_required_confirm_pin():
    s = _Store("auto_ai")
    h = _Health(inbox={"messenger:acct1": {"hint_code": "e2ee_pin_required"}})
    st = _c(s, health=h)
    assert st["state"] == "sidecar_down" and st["tone"] == "danger"
    assert st["action"] == "confirm_pin"
    assert st["reason_text_key"] == "inbox.cs.sidecar_down.pin"
    assert st["reason_code"] == "e2ee_pin_required"


def test_sidecar_send_fail_retry_with_message_id():
    s = _Store("auto_ai")
    s.failed = [{"message_id": "m77", "ts": time.time() - 60,
                 "fail_reason": "composer_detached", "text": "hello"},
                {"message_id": "m76", "ts": time.time() - 120,
                 "fail_reason": "composer_detached", "text": "hi"}]
    st = _c(s)
    assert st["state"] == "sidecar_down" and st["action"] == "retry"
    assert st["reason_text_key"] == "inbox.cs.sidecar_down.send_fail"
    assert st["params"]["message_id"] == "m77" and st["params"]["n"] == 2
    assert st["params"]["code"] == "composer_detached"


def test_sidecar_send_fail_ignores_non_sidecar_reason():
    s = _Store("auto_ai")
    s.failed = [{"message_id": "m1", "ts": time.time(), "fail_reason": "quota", "text": ""}]
    assert _c(s)["state"] == "auto"


def test_off_hours_amber_with_until():
    s = _Store("auto_ai")
    cfg = {"inbox": {"work_schedule": {
        "enabled": True, "timezone": _NY,
        "default": {"workdays": [1, 2, 3, 4, 5, 6, 7], "start": "09:00", "end": "23:00"},
        "edge_jitter_min": 0}}}
    now = datetime(2026, 9, 10, 3, 0, tzinfo=ZoneInfo(_NY)).timestamp()
    st = _c(s, config=cfg, now=now)
    assert st["state"] == "off_hours" and st["tone"] == "warn" and st["will_send"] is False
    assert st["until_ts"] and st["until_ts"] > now
    assert st["params"]["until_hhmm"] == "09:00"
    assert st["action"] == "none"


def test_deferred_agent_yield_amber_resume():
    s = _Store("auto_ai")
    w = _Worker(active=True, until=time.time() + 23, remaining=23.4)
    st = _c(s, worker=w)
    assert st["state"] == "deferred" and st["tone"] == "warn"
    assert st["will_send"] is True  # 会回，只是在等
    assert st["action"] == "resume" and st["params"]["remaining_sec"] == 23
    assert st["until_ts"] == pytest.approx(w._st["until"])


def test_lang_unknown_via_l1_reason_l1_variant():
    s = _Store("auto_ai")
    l1_reason.note(CID, "lang_unknown")
    st = _c(s)
    assert st["state"] == "lang_unknown" and st["tone"] == "warn"
    assert st["will_send"] is False
    assert st["reason_text_key"] == "inbox.cs.lang_unknown_l1"
    assert st["params"]["via"] == "l1_reason"


def test_lang_unknown_via_lang_plan_fallback_variant():
    s = _Store("auto_ai")
    s.kv["conv_lang_plan:" + CID] = '{"decided_by": "persona_fallback", "reply_lang": "en"}'
    st = _c(s)
    assert st["state"] == "lang_unknown" and st["will_send"] is True
    assert st["reason_text_key"] == "inbox.cs.lang_unknown"
    assert st["params"]["reply_lang"] == "en"


def test_ai_fail_red_retry_with_draft():
    s = _Store("auto_ai")
    ai_fail_marker.mark(s, CID, reason="timeout", draft_id="d42", latency_ms=30000)
    st = _c(s)
    assert st["state"] == "ai_fail" and st["tone"] == "danger" and st["will_send"] is False
    assert st["action"] == "retry" and st["params"]["draft_id"] == "d42"
    assert st["reason_code"] == "timeout" and st["params"]["hhmm"]


def test_ai_fail_without_draft_has_no_action():
    s = _Store("auto_ai")
    ai_fail_marker.mark(s, CID, reason="gateway")
    st = _c(s)
    assert st["state"] == "ai_fail" and st["action"] == "none"


# ── 优先级 ───────────────────────────────────────────────────────────

def _everything(mode="auto_ai"):
    s = _Store(mode)
    s.tags = [HANDOFF_TAG]
    s.handoff_meta = {"reason": "needs_human", "ts": time.time()}
    s.failed = [{"message_id": "m1", "ts": time.time(), "fail_reason": "thread_not_found", "text": ""}]
    l1_reason.note(CID, "lang_unknown")
    ai_fail_marker.mark(s, CID, reason="timeout", draft_id="d1")
    return s


def test_priority_chain():
    cfg = {"inbox": {"work_schedule": {
        "enabled": True, "timezone": _NY,
        "default": {"workdays": [1, 2, 3, 4, 5, 6, 7], "start": "09:00", "end": "23:00"},
        "edge_jitter_min": 0}}}
    now = datetime(2026, 9, 10, 3, 0, tzinfo=ZoneInfo(_NY)).timestamp()
    w = _Worker(active=True, until=now + 20, remaining=20)
    h = _Health(inbox={"messenger:acct1": {"hint_code": "e2ee_pin_required"}})

    s = _everything()
    assert _c(s, worker=w, config=cfg, now=now, health=h)["state"] == "held"
    s.tags = []; s.handoff_meta = {}
    assert _c(s, worker=w, config=cfg, now=now, health=h)["state"] == "sidecar_down"
    s.failed = []
    assert _c(s, worker=w, config=cfg, now=now)["state"] == "off_hours"
    assert _c(s, worker=w, now=now)["state"] == "deferred"
    assert _c(s, now=now)["state"] == "lang_unknown"
    l1_reason._REG.clear()
    assert _c(s, now=now)["state"] == "ai_fail"
    ai_fail_marker.clear(s, CID)
    assert _c(s, now=now)["state"] == "auto"


def test_review_mode_skips_auto_only_sources():
    """人审档：让位 / 作息外 / 语言未知是全自动专属噪音 → 不出；held / sidecar / ai_fail 仍出。"""
    cfg = {"inbox": {"work_schedule": {
        "enabled": True, "timezone": _NY,
        "default": {"workdays": [1, 2, 3, 4, 5, 6, 7], "start": "09:00", "end": "23:00"},
        "edge_jitter_min": 0}}}
    now = datetime(2026, 9, 10, 3, 0, tzinfo=ZoneInfo(_NY)).timestamp()
    s = _Store("review")
    l1_reason.note(CID, "lang_unknown")
    w = _Worker(active=True, until=now + 20, remaining=20)
    st = _c(s, worker=w, config=cfg, now=now)
    assert st["state"] == "human"
    assert st["sources"]["agent_yield"] is None and st["sources"]["off_hours"] is None
    assert st["sources"]["lang_plan"] is None
    ai_fail_marker.mark(s, CID, reason="timeout", draft_id="d1")
    assert _c(s, worker=w, config=cfg, now=now)["state"] == "ai_fail"


# ── 只读 / 容错 ───────────────────────────────────────────────────────

def test_compute_is_read_only():
    s = _everything()
    before = s.writes
    _c(s, worker=_Worker(active=True, until=time.time() + 5, remaining=5))
    assert s.writes == before


def test_compute_never_raises_on_broken_store():
    class _Broken:
        def __getattr__(self, name):
            def _boom(*a, **k):
                raise RuntimeError("boom")
            return _boom

    st = compute(_Broken(), CID, platform="messenger", account_id="acct1", health=_Health())
    assert st["state"] in STATES
    st2 = compute(None, "", health=_Health())
    assert st2["state"] == "human"


# ── 路由接线（静态）──────────────────────────────────────────────────

def test_read_routes_wire_conv_state():
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "src" / "web" / "routes"
           / "unified_inbox_read_routes.py").read_text(encoding="utf-8")
    assert '@app.get("/api/unified-inbox/conv-state")' in src
    assert 'resp["conv_state"] = _cs' in src
    assert "from src.inbox.conv_state import compute" in src
