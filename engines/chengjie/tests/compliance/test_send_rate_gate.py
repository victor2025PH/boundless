# -*- coding: utf-8 -*-
"""智安 · 发送限速闸：预热期硬上限（AI + 坐席）+ 脚本流量日上限 + 来源归因。

全部是纯判定 / 计数库单测（假 registry、假请求），不触达任何真实账号、不真发。
"""
from __future__ import annotations

import time

import pytest

from src.compliance import send_rate_gate as srg

_DAY = 86400.0


@pytest.fixture(autouse=True)
def _gate_on(tmp_path, monkeypatch):
    monkeypatch.delenv("ZHILIAO_SEND_RATE_GATE", raising=False)
    monkeypatch.setenv("ZHILIAO_SEND_RATE_DB", str(tmp_path / "send_rate_gate.db"))
    srg.reset_for_tests()
    yield
    srg.reset_for_tests()


class _Reg:
    def __init__(self, age_days=None, now=None):
        self._created = None if age_days is None else (now or time.time()) - age_days * _DAY

    def get(self, platform, account_id):
        return {} if self._created is None else {"created_at": self._created}


# ── 来源归因 ─────────────────────────────────────────────────────────────

def test_classify_origin_maps_guard_origins_to_sent_by():
    assert srg.classify_origin("auto") == "ai"
    assert srg.classify_origin("manual") == "agent"
    assert srg.classify_origin("script") == "script"
    assert srg.classify_origin("phone") == "phone"
    cfg = {"compliance": {"send_rate_gate": {"script_peers": ["639171234567"],
                                             "script_accounts": ["telegram:6834000252"]}}}
    assert srg.classify_origin("auto", chat_key="639171234567@s.whatsapp.net", config=cfg) == "script"
    assert srg.classify_origin("manual", platform="telegram", account_id="6834000252", config=cfg) == "script"
    assert srg.classify_origin("manual", platform="telegram", account_id="2015000339", config=cfg) == "agent"
    assert [srg.origin_to_sent_by(o) for o in ("auto", "manual", "script", "phone")] == \
        ["ai", "agent", "script", "phone"]


def test_script_scope_from_request_header_and_body():
    class _Req:
        def __init__(self, h):
            self.headers = h

    assert srg.in_script_scope() is False
    tok = srg.set_script_scope(False)
    assert srg.mark_script_from_request(_Req({"x-send-origin": "agent"}), {}) is False
    assert srg.in_script_scope() is False
    assert srg.mark_script_from_request(_Req({}), {"origin": "script"}) is True
    assert srg.in_script_scope() is True
    assert srg.classify_origin("manual") == "script"
    srg.reset_script_scope(tok)
    assert srg.in_script_scope() is False


# ── 预热期硬上限 ─────────────────────────────────────────────────────────

def test_warmup_cap_blocks_ai_and_agent_alike_and_audits():
    now = time.time()
    reg = _Reg(age_days=0, now=now)              # 新号当天：建议上限 = start_cap 2
    d1 = srg.check("telegram", "acc1", origin="auto", chat_key="u1", registry=reg, now=now)
    d2 = srg.check("telegram", "acc1", origin="manual", chat_key="u2", registry=reg, now=now + 1)
    assert d1["allowed"] and d2["allowed"] and d2["in_warmup"] and d2["cap"] == 2
    d3 = srg.check("telegram", "acc1", origin="auto", chat_key="u3", registry=reg, now=now + 2)
    d4 = srg.check("telegram", "acc1", origin="manual", chat_key="u3", registry=reg, now=now + 3)
    assert not d3["allowed"] and d3["reason"] == "warmup_cap" and d3["used"] == 2
    assert not d4["allowed"] and d4["reason"] == "warmup_cap" and d4["origin"] == "agent"
    rows = srg.get_store().audit_rows(action="blocked")
    assert [r["origin"] for r in rows] == ["agent", "ai"]
    assert all(r["reason"] == "warmup_cap" and r["cap"] == 2 for r in rows)
    # 别的号不受影响；24h 滚动窗过后恢复
    assert srg.check("telegram", "acc2", origin="auto", registry=reg, now=now)["allowed"]
    assert srg.check("telegram", "acc1", origin="auto", registry=reg, now=now + _DAY + 5)["allowed"]


def test_warmup_cap_ramps_with_age_and_stops_after_ramp():
    now = time.time()
    reg7 = _Reg(age_days=7, now=now)             # 2 + (15-2)*7/14 = 8
    for i in range(8):
        assert srg.check("telegram", "a7", origin="auto", registry=reg7, now=now + i)["allowed"]
    d = srg.check("telegram", "a7", origin="manual", registry=reg7, now=now + 9)
    assert not d["allowed"] and d["cap"] == 8
    reg_old = _Reg(age_days=30, now=now)         # 预热期满：本闸不管（业务额度归既有闸）
    for i in range(40):
        assert srg.check("telegram", "old", origin="auto", registry=reg_old, now=now + i)["allowed"]
    unknown = _Reg(age_days=None)                # 天龄未知：不臆造预热期
    for i in range(10):
        assert srg.check("telegram", "unk", origin="auto", registry=unknown, now=now + i)["allowed"]


def test_preview_does_not_count_and_phone_counts_separately():
    now = time.time()
    reg = _Reg(age_days=0, now=now)
    for i in range(5):
        assert srg.check("whatsapp", "w1", origin="auto", registry=reg, now=now + i,
                         record=False)["allowed"]
    for i in range(5):                            # 手机端外发：不拦、不占预热桶
        srg.record_external("whatsapp", "w1", chat_key=f"p{i}", ts=now + i)
        assert srg.check("whatsapp", "w1", origin="phone", registry=reg, now=now + i)["allowed"]
    assert srg.check("whatsapp", "w1", origin="auto", registry=reg, now=now + 6)["allowed"]
    snap = srg.snapshot("whatsapp", "w1", registry=reg, now=now + 7)
    assert snap["by_origin"] == {"phone": 10, "ai": 1} and snap["cap"] == 2 and snap["in_warmup"]


# ── 脚本流量日上限 ───────────────────────────────────────────────────────

def test_script_daily_cap_any_age():
    now = time.time()
    cfg = {"compliance": {"send_rate_gate": {"script_daily_cap": 3,
                                             "script_peers": ["tester-bot"]}}}
    reg = _Reg(age_days=60, now=now)
    for i in range(3):
        assert srg.check("telegram", "6834", origin="auto", chat_key="tester-bot",
                         config=cfg, registry=reg, now=now + i)["allowed"]
    d = srg.check("telegram", "6834", origin="auto", chat_key="tester-bot", config=cfg,
                  registry=reg, now=now + 4)
    assert not d["allowed"] and d["reason"] == "script_daily_cap" and d["origin"] == "script"
    # 真实客户不受脚本上限影响
    assert srg.check("telegram", "6834", origin="auto", chat_key="real-customer", config=cfg,
                     registry=reg, now=now + 5)["allowed"]


def test_script_traffic_also_eats_warmup_bucket():
    now = time.time()
    cfg = {"compliance": {"send_rate_gate": {"script_daily_cap": 50}}}
    reg = _Reg(age_days=0, now=now)
    assert srg.check("telegram", "n1", origin="script", config=cfg, registry=reg, now=now)["allowed"]
    assert srg.check("telegram", "n1", origin="script", config=cfg, registry=reg, now=now + 1)["allowed"]
    d = srg.check("telegram", "n1", origin="manual", config=cfg, registry=reg, now=now + 2)
    assert not d["allowed"] and d["reason"] == "warmup_cap"


# ── 开关 ────────────────────────────────────────────────────────────────

def test_kill_switch_config_and_env(monkeypatch):
    now = time.time()
    reg = _Reg(age_days=0, now=now)
    off = {"compliance": {"send_rate_gate": {"enabled": False}}}
    for i in range(5):
        assert srg.check("telegram", "k1", config=off, registry=reg, now=now + i)["reason"] == "disabled"
    nowarm = {"compliance": {"send_rate_gate": {"warmup_block": False}}}
    for i in range(5):
        assert srg.check("telegram", "k2", config=nowarm, registry=reg, now=now + i)["allowed"]
    monkeypatch.setenv("ZHILIAO_SEND_RATE_GATE", "off")
    assert srg.enabled({}) is False


def test_custom_warmup_curve_from_companion_send_gate_cfg():
    now = time.time()
    cfg = {"companion_send_gate": {"target_cap": 30, "warmup_start_cap": 5, "warmup_ramp_days": 10}}
    reg = _Reg(age_days=0, now=now)
    for i in range(5):
        assert srg.check("line", "l1", config=cfg, registry=reg, now=now + i)["allowed"]
    assert srg.check("line", "l1", config=cfg, registry=reg, now=now + 6)["cap"] == 5


# ── 接入：编排器统一出站护栏 send_guard ────────────────────────────────

def test_send_guard_blocks_warmup_overflow_for_auto_and_manual():
    from src.integrations.shared.send_guard import send_blocked
    reg = _Reg(age_days=0)
    assert send_blocked("telegram", "g1", config={}, registry=reg, chat_key="c1") == (False, "")
    assert send_blocked("telegram", "g1", config={}, registry=reg, chat_key="c2",
                        origin="manual") == (False, "")
    # 预判（notify=False）只算不写
    assert send_blocked("telegram", "g1", config={}, registry=reg, notify=False)[0] is True
    assert send_blocked("telegram", "g1", config={}, registry=reg, chat_key="c3") == \
        (True, "send_rate_gate:warmup_cap")
    assert send_blocked("telegram", "g1", config={}, registry=reg, chat_key="c3",
                        origin="manual") == (True, "send_rate_gate:warmup_cap")
    rows = srg.get_store().audit_rows(action="blocked")
    assert len(rows) == 2 and {r["origin"] for r in rows} == {"ai", "agent"}


def test_send_guard_script_scope_counts_as_script():
    from src.integrations.shared.send_guard import send_blocked
    cfg = {"compliance": {"send_rate_gate": {"script_daily_cap": 1}}}
    reg = _Reg(age_days=40)
    tok = srg.set_script_scope(True)
    try:
        assert send_blocked("telegram", "s1", config=cfg, registry=reg, origin="manual") == (False, "")
        assert send_blocked("telegram", "s1", config=cfg, registry=reg, origin="manual") == \
            (True, "send_rate_gate:script_daily_cap")
    finally:
        srg.reset_script_scope(tok)
    assert send_blocked("telegram", "s1", config=cfg, registry=reg, origin="manual") == (False, "")


def test_check_error_fails_open_and_counts(monkeypatch):
    """闸门坏掉仍放行，并累计次数。日上限保持 20，不改成拦截。"""
    def boom(*_a, **_k):
        raise RuntimeError("db")

    monkeypatch.setattr(srg, "get_store", boom)
    d = srg.check("telegram", "a1", origin="agent")
    assert d["allowed"] is True and d["reason"] == "error" and d["degraded"] is True
    assert d["degraded_count"] == 1
    d2 = srg.check("telegram", "a1", origin="agent")
    assert d2["allowed"] is True and d2["degraded_count"] == 2
    assert srg.degraded_count() == 2
    assert srg.DEFAULTS["script_daily_cap"] == 20
    srg.reset_for_tests()
    assert srg.degraded_count() == 0


def test_degraded_count_persists_across_reopen(tmp_path, monkeypatch):
    """fail-open 次数写在限速库里。关掉进程内单例再读，次数还在。上限仍是 20。"""
    db = tmp_path / "send_rate_gate.db"
    monkeypatch.setenv("ZHILIAO_SEND_RATE_DB", str(db))
    srg.reset_for_tests()

    def boom(*_a, **_k):
        raise RuntimeError("logic")

    monkeypatch.setattr(srg, "classify_origin", boom)
    try:
        d = srg.check("telegram", "a1", origin="agent")
        assert d["allowed"] is True and d["degraded"] is True
        assert d["degraded_count"] == 1
        path = str(db)
        with srg._INST_LOCK:
            inst = srg._INSTANCES.pop(path, None)
        if inst is not None:
            inst.close()
        srg._DEGRADED_COUNT = 0
        assert srg.degraded_count() == 1
        fresh = srg.SendRateStore(path)
        try:
            assert fresh.degraded_total() == 1
            assert fresh.bump_degraded() == 2
        finally:
            fresh.close()
        assert srg.DEFAULTS["script_daily_cap"] == 20
    finally:
        srg.reset_for_tests()
        assert srg.degraded_count() == 0


def test_inbox_send_route_marks_script_header():
    from src.web.routes.unified_inbox_send_routes import _mark_script_send

    class _Req:
        headers = {"x-send-origin": "script"}

    tok = srg.set_script_scope(False)
    try:
        assert _mark_script_send(_Req(), {"platform": "telegram"}) is True
        assert srg.in_script_scope() is True
    finally:
        srg.reset_script_scope(tok)
