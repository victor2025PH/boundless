# -*- coding: utf-8 -*-
"""智安 · A 线（sender.py）外发接发送限速闸。

全部是替身对象 + 临时计数库：不连 Telegram、不真发、告警通道打桩。
"""
from __future__ import annotations

import asyncio
import logging
import types

import pytest

from src.client.sender import TelegramSenderMixin
from src.compliance import send_rate_gate as srg


@pytest.fixture(autouse=True)
def _gate_on(tmp_path, monkeypatch):
    monkeypatch.delenv("ZHILIAO_SEND_RATE_GATE", raising=False)
    monkeypatch.setenv("ZHILIAO_SEND_RATE_DB", str(tmp_path / "send_rate_gate.db"))
    srg.reset_for_tests()
    # 新号当天：建议上限 = start_cap（缺省 2）
    monkeypatch.setattr(srg, "_age_days", lambda *a, **k: 0.0)
    yield
    srg.reset_for_tests()


@pytest.fixture()
def alerts(monkeypatch):
    got = []
    from src.integrations.shared import send_guard
    monkeypatch.setattr(send_guard, "notify_send_blocked",
                        lambda p, a, r: got.append((p, a, r)))
    return got


class _Sent:
    id = 4242


class _FakeClient:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))
        return _Sent()


class _S(TelegramSenderMixin):
    def __init__(self, cfg=None):
        self.config = types.SimpleNamespace(config=cfg or {})
        self.account_id = "7000000001"
        self.proxy_id = ""
        self.logger = logging.getLogger("test.aline_rate_gate")
        self.client = _FakeClient()


def _counts(acct="7000000001"):
    import time
    return srg.get_store().counts_by_origin("telegram", acct, since=time.time() - 86400)


def test_autoreply_blocked_after_warmup_cap_and_audited(alerts):
    s = _S()
    assert s._presend_blocked(is_autoreply=True, peer=111) is False
    assert s._presend_blocked(is_autoreply=True, peer=111) is False
    assert s._presend_blocked(is_autoreply=True, peer=111) is True
    assert _counts().get("ai") == 2          # 拦下的那条不计数
    rows = srg.get_store().audit_rows(action="blocked")
    assert rows and rows[0]["reason"] == srg.REASON_WARMUP
    assert alerts and alerts[0][2] == "send_rate_gate:warmup_cap"


def test_other_aline_sends_share_the_same_bucket(alerts):
    s = _S()
    assert s._presend_blocked(peer=1) is False           # 主动触达 / 关怀 / 形象照
    assert s._presend_blocked(is_autoreply=True, peer=2) is False
    assert s._presend_blocked(peer=3) is True
    assert _counts().get("ai") == 2


def test_script_account_counts_as_script(alerts):
    s = _S({"compliance": {"send_rate_gate": {"script_accounts": ["telegram:7000000001"],
                                              "script_daily_cap": 1, "warmup_block": False}}})
    assert s._presend_blocked(is_autoreply=True, peer=9) is False
    assert s._presend_blocked(is_autoreply=True, peer=9) is True
    assert _counts().get("script") == 1
    assert alerts[-1][2] == "send_rate_gate:script_daily_cap"


def test_gate_disabled_by_config_or_env(monkeypatch, alerts):
    s = _S({"compliance": {"send_rate_gate": {"enabled": False}}})
    for _ in range(5):
        assert s._presend_blocked(is_autoreply=True, peer=1) is False
    monkeypatch.setenv("ZHILIAO_SEND_RATE_GATE", "off")
    s2 = _S()
    for _ in range(5):
        assert s2._presend_blocked(peer=1) is False
    assert alerts == []


def test_rate_gate_false_skips_check_and_count(alerts):
    s = _S()
    for _ in range(5):
        assert s._presend_blocked(peer=1, rate_gate=False) is False
    assert _counts() == {}


def test_send_message_counts_but_return_id_path_does_not(alerts):
    """send_message（A 线主动发）计数并受闸；send_message_return_id（编排器 worker，已过 send_guard）不重复。"""
    s = _S()
    ok, mid = asyncio.run(s.send_message_return_id(5, "hi"))
    assert ok is True
    assert _counts() == {}
    assert asyncio.run(s.send_message(5, "a")) is True
    assert asyncio.run(s.send_message(5, "b")) is True
    assert asyncio.run(s.send_message(5, "c")) is False        # 预热上限 2
    assert [t for _, t in s.client.sent] == ["hi", "a", "b"]   # 被拦的没碰 client


def test_return_id_only_used_by_orchestrator_worker():
    """rate_gate=False 的前提：send_message_return_id 只有编排器 TelegramCompanionWorker 调用。"""
    from pathlib import Path
    src = Path(__file__).resolve().parents[2] / "src"
    users = sorted(str(f.relative_to(src)).replace("\\", "/") for f in src.rglob("*.py")
                   if "send_message_return_id" in f.read_text(encoding="utf-8", errors="ignore"))
    assert set(users) <= {"client/sender.py", "client/telegram_client.py",
                          "integrations/telegram_companion_worker.py"}, users


def test_exempt_peer_still_subject_to_rate_gate(alerts):
    s = _S({"companion_send_gate": {"enabled": True, "exempt_peers": ["777"]}})
    from src.skills.companion_send_gate import gate_enabled, peer_exempt
    if not (gate_enabled(s.config.config) and peer_exempt(s.config.config, "777")):
        pytest.skip("companion_send_gate 配置口径不同，豁免分支未命中")
    assert s._presend_blocked(is_autoreply=True, peer=777) is False
    assert s._presend_blocked(is_autoreply=True, peer=777) is False
    assert s._presend_blocked(is_autoreply=True, peer=777) is True
