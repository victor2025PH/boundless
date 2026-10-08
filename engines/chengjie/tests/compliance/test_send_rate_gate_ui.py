# -*- coding: utf-8 -*-
"""智安 · 发送限速闸拦截的专门文案（中 / 英 / 繁）+ 「几点恢复」。

纯判定 / 文案单测：假 registry、假 request，不触达任何真实账号、不真发。
"""
from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.compliance import send_rate_gate as srg

_DAY = 86400.0
_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _gate_on(tmp_path, monkeypatch):
    monkeypatch.delenv("ZHILIAO_SEND_RATE_GATE", raising=False)
    monkeypatch.setenv("ZHILIAO_SEND_RATE_DB", str(tmp_path / "send_rate_gate.db"))
    srg.reset_for_tests()
    yield
    srg.reset_for_tests()


class _Reg:
    def __init__(self, age_days, now):
        self._created = now - age_days * _DAY

    def get(self, platform, account_id):
        return {"created_at": self._created}


def _fill(now, reg, n=2, origin="auto"):
    for i in range(n):
        assert srg.check("telegram", "acc1", origin=origin, chat_key=f"u{i}", registry=reg,
                         now=now - 3600 * (n - i))["allowed"]


def test_frees_at_is_when_oldest_counted_send_ages_out():
    now = time.time()
    reg = _Reg(0, now)                        # 新号当天：建议上限 2
    _fill(now, reg, 2)                         # now-2h、now-1h 各一条
    info = srg.block_info("telegram", "acc1", origin="manual", registry=reg, now=now)
    assert info and info["reason"] == "warmup_cap" and info["used"] == 2 and info["cap"] == 2
    assert info["frees_at"] == pytest.approx(now - 2 * 3600 + _DAY, abs=1)
    # 未拦 → None
    assert srg.block_info("telegram", "acc2", origin="manual", registry=reg, now=now) is None


def test_frees_at_script_bucket_uses_script_events_only():
    now = time.time()
    cfg = {"compliance": {"send_rate_gate": {"script_daily_cap": 1, "warmup_block": False}}}
    assert srg.check("telegram", "acc9", origin="auto", chat_key="x", config=cfg, now=now - 7200)["allowed"]
    assert srg.check("telegram", "acc9", origin="script", chat_key="y", config=cfg, now=now - 3600)["allowed"]
    info = srg.block_info("telegram", "acc9", origin="script", config=cfg, now=now)
    assert info["reason"] == "script_daily_cap"
    assert info["frees_at"] == pytest.approx(now - 3600 + _DAY, abs=1)


def test_reason_family_mapping():
    from src.inbox.send_gate_status import blocked_reason_key
    assert blocked_reason_key("send_rate_gate:warmup_cap") == "rate_warmup"
    assert blocked_reason_key("send_rate_gate:script_daily_cap") == "rate_script"
    assert blocked_reason_key("send_gate:daily_cap") == "quota"


def test_snapshot_carries_rate_and_frees_at():
    from src.inbox.send_gate_status import send_gate_snapshot
    now = time.time()
    reg = _Reg(0, now)
    _fill(now, reg, 2)
    snap = send_gate_snapshot("telegram", "acc1", "u9", config={}, registry=reg, now=now)
    assert snap["blocked"] and snap["reason"] == "send_rate_gate:warmup_cap"
    assert snap["rate"]["used"] == 2 and snap["rate"]["cap"] == 2
    assert snap["frees_at"] == pytest.approx(now - 2 * 3600 + _DAY, abs=1)


@pytest.mark.parametrize("lang,needle,resume", [
    ("zh", "预热期", "恢复"),
    ("en", "warming up", "resume"),
    ("zh_hant", "預熱期", "恢復"),
])
def test_409_message_is_specific_and_says_when(lang, needle, resume):
    from src.web.routes.unified_inbox_send_routes import _send_blocked_exc
    now = time.time()
    req = SimpleNamespace(state=SimpleNamespace(ui_lang=lang), app=SimpleNamespace(state=SimpleNamespace()))
    snap = {"blocked": True, "reason": "send_rate_gate:warmup_cap", "quota": None,
            "frees_at": now + 3600, "rate": {"reason": "warmup_cap", "used": 2, "cap": 2,
                                             "frees_at": now + 3600, "origin": "agent"}}
    exc = _send_blocked_exc(req, "telegram", "acc1", "u1", reason="send_rate_gate:warmup_cap", snap=snap)
    det = exc.detail
    assert exc.status_code == 409 and det["code"] == "send_blocked"
    assert needle in det["message"] and resume in det["message"]
    assert time.strftime("%H:%M", time.localtime(now + 3600)) in det["message"]
    assert det["rate"]["cap"] == 2 and det["frees_at"] == pytest.approx(now + 3600)
    assert "{" not in det["message"], "占位符必须全部填上"


def test_409_without_frees_at_falls_back_to_rolling_window_text():
    from src.web.routes.unified_inbox_send_routes import _send_blocked_exc
    req = SimpleNamespace(state=SimpleNamespace(ui_lang="zh"), app=SimpleNamespace(state=SimpleNamespace()))
    snap = {"blocked": True, "reason": "send_rate_gate:script_daily_cap", "quota": None,
            "rate": {"reason": "script_daily_cap", "used": 20, "cap": 20, "frees_at": None}}
    det = _send_blocked_exc(req, "telegram", "acc1", "u1", reason="send_rate_gate:script_daily_cap",
                            snap=snap).detail
    assert "20/20" in det["message"] and "24 小时" in det["message"]


def test_i18n_keys_present_in_zh_en_hant():
    from src.web.i18n_packs import inbox_send_rate_gate as pack
    keys = set(pack.ZH)
    assert keys == set(pack.EN) == set(pack.ZH_HANT)
    for k in ("err.inbox.send_blocked_rate_warmup", "err.inbox.send_blocked_rate_script",
              "inbox.gate.rate_warmup", "inbox.gate.rate_script", "inbox.gate.rate_frees",
              "inbox.failr.rate_warmup"):
        assert k in keys
    from src.web.web_i18n import get_translations
    assert get_translations("zh_hant").get("inbox.gate.rate_frees") == "預計 {time} 恢復"


def test_frontend_banner_and_fail_reason_wired():
    html = (_ROOT / "src" / "web" / "templates" / "unified_inbox.html").read_text(encoding="utf-8")
    assert "fam='rate_warmup'" in html and "fam='rate_script'" in html
    assert "inbox.gate.rate_frees" in html and "rate:(det&&det.rate)||null" in html
    i_rate = html.index("send_rate_gate:warmup')>=0")
    i_daily = html.index("low.indexOf('send_gate:daily_cap')>=0")
    assert i_rate < i_daily, "限速闸短语必须在 warmup_cap 通配之前"
