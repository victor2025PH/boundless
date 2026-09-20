# -*- coding: utf-8 -*-
"""#167 首次登录不该直接全自动（#63 / #156 之上）。

矩阵：{onboarding 未确认/已确认} × {bootstrap true/false} × {人设显式/默认}
+ 首登竞态（登录 upsert 早于 baseline）+ connected_at 未知。
"""
from __future__ import annotations

import time

import pytest

import src.inbox.account_mode_onboarding as amo
from src.inbox.automation_mode import (
    maybe_bootstrap_automation_mode,
    resolve_automation_mode,
)

NOW = time.time()


def _cfg(*, onboarding: bool, bootstrap: bool, default_persona: str = "lin_xiaoyu"):
    return {
        "inbox": {"auto_draft": {
            "automation_mode": "auto_ai",
            "bootstrap_automation_mode": bootstrap,
            "account_mode_onboarding": {"enabled": onboarding},
        }},
        "platform_login": {"default_persona_id": default_persona},
    }


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    p = tmp_path / "account_mode_onboarding.json"
    monkeypatch.setattr(amo, "_state_path", lambda: p)
    amo._reset_for_tests()
    yield
    amo._reset_for_tests()


class _FakeStore:
    def __init__(self, explicit=None):
        self.explicit = dict(explicit or {})
        self.set_calls = []

    def get_automation_mode_if_set(self, cid):
        return self.explicit.get(cid)

    def set_automation_mode(self, cid, mode, *, source=""):
        self.set_calls.append((cid, mode, source))
        self.explicit[cid] = mode

    def get_conversation(self, cid):
        return {"conversation_id": cid, "chat_type": "private"}


def _connected(ts_map):
    def fn(platform, account_id, *, now=None):
        return float(ts_map.get(f"{platform}:{account_id}", 0.0))
    return fn


class _Reg:
    def __init__(self, rows=None):
        self._rows = dict(rows or {})

    def get(self, platform, account_id):
        return self._rows.get(f"{platform}:{account_id}")


def _new_acct(monkeypatch, *, connected=None):
    amo.ensure_baseline(now=NOW - 3600)
    ts = NOW if connected is None else connected
    monkeypatch.setattr(amo, "_resolve_connected_at",
                        _connected({"whatsapp:acc1": ts}))


# ── 矩阵 ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("bootstrap", [True, False])
@pytest.mark.parametrize("explicit_persona", [True, False])
def test_unconfirmed_never_autosends(monkeypatch, bootstrap, explicit_persona):
    """未确认：无论 bootstrap / 人设，首条入站不真发（review、不落盘）。"""
    _new_acct(monkeypatch)
    monkeypatch.setattr(amo, "persona_is_user_explicit",
                        lambda *a, **k: explicit_persona)
    cfg = _cfg(onboarding=True, bootstrap=bootstrap)
    cid = "whatsapp:acc1:13308422244"
    st = _FakeStore()
    assert resolve_automation_mode(st, cid, cfg) == "review"
    assert maybe_bootstrap_automation_mode(st, cid, cfg) == "review"
    assert st.set_calls == []


@pytest.mark.parametrize("bootstrap", [True, False])
@pytest.mark.parametrize("explicit_persona", [True, False])
def test_confirmed_auto_ai_follows_decision(monkeypatch, bootstrap, explicit_persona):
    """已确认全自动：跟随决策；bootstrap 仍不落盘（账号层自身持久）。"""
    _new_acct(monkeypatch)
    amo.decide_account_mode("whatsapp", "acc1", "auto_ai", actor="seat")
    monkeypatch.setattr(amo, "persona_is_user_explicit",
                        lambda *a, **k: explicit_persona)
    cfg = _cfg(onboarding=True, bootstrap=bootstrap)
    cid = "whatsapp:acc1:13308422244"
    st = _FakeStore()
    assert resolve_automation_mode(st, cid, cfg) == "auto_ai"
    assert maybe_bootstrap_automation_mode(st, cid, cfg) == "auto_ai"
    assert st.set_calls == []


def test_onboarding_off_bootstrap_still_auto_ai(monkeypatch):
    """功能关＝#63 旧行为：bootstrap 可把全局 auto_ai 落盘。"""
    amo.ensure_baseline(now=NOW - 3600)
    monkeypatch.setattr(amo, "_resolve_connected_at",
                        _connected({"whatsapp:acc1": NOW}))
    cfg = _cfg(onboarding=False, bootstrap=True)
    cid = "whatsapp:acc1:peer"
    st = _FakeStore()
    assert maybe_bootstrap_automation_mode(st, cid, cfg) == "auto_ai"
    assert st.set_calls == [(cid, "auto_ai", "bootstrap")]


# ── 首登竞态 / 未知接入时刻 ───────────────────────────────────────────────

def test_login_before_baseline_is_pending(monkeypatch):
    """登录 upsert 早于首次入站冻结 baseline 几分钟 → 仍待确认（#167 竞态）。"""
    amo.ensure_baseline(now=NOW)
    monkeypatch.setattr(amo, "_resolve_connected_at",
                        _connected({"whatsapp:acc1": NOW - 600}))
    cfg = _cfg(onboarding=True, bootstrap=True)
    cid = "whatsapp:acc1:peer"
    assert amo.account_mode_for("whatsapp", "acc1", cfg) == "review"
    st = _FakeStore()
    assert maybe_bootstrap_automation_mode(st, cid, cfg) == "review"
    assert st.set_calls == []


def test_unknown_connected_at_is_pending(monkeypatch):
    amo.ensure_baseline(now=NOW - 3600)
    monkeypatch.setattr(amo, "_resolve_connected_at", _connected({}))
    cfg = _cfg(onboarding=True, bootstrap=True)
    assert amo.account_mode_for("whatsapp", "ghost", cfg) == "review"
    st = _FakeStore()
    assert maybe_bootstrap_automation_mode(
        st, "whatsapp:ghost:peer", cfg) == "review"
    assert st.set_calls == []


def test_stock_account_default_persona_caps_review(monkeypatch):
    """存量未确认 + 默认人设 → #167 最后一道：不真发。"""
    amo.ensure_baseline(now=NOW - 3600)
    monkeypatch.setattr(amo, "_resolve_connected_at",
                        _connected({"whatsapp:old1": NOW - 30 * 86400}))
    monkeypatch.setattr(amo, "persona_is_user_explicit",
                        lambda *a, **k: False)
    cfg = _cfg(onboarding=True, bootstrap=True)
    cid = "whatsapp:old1:peer"
    st = _FakeStore()
    assert resolve_automation_mode(st, cid, cfg) == "review"
    assert maybe_bootstrap_automation_mode(st, cid, cfg) == "review"
    assert st.set_calls == []


def test_stock_account_explicit_persona_keeps_global(monkeypatch):
    """存量未确认 + 用户显式人设：回落全局（不打扰已在跑的号），但不落盘。"""
    amo.ensure_baseline(now=NOW - 3600)
    monkeypatch.setattr(amo, "_resolve_connected_at",
                        _connected({"whatsapp:old1": NOW - 30 * 86400}))
    monkeypatch.setattr(amo, "persona_is_user_explicit",
                        lambda *a, **k: True)
    cfg = _cfg(onboarding=True, bootstrap=True)
    cid = "whatsapp:old1:peer"
    st = _FakeStore()
    assert resolve_automation_mode(st, cid, cfg) == "auto_ai"
    assert maybe_bootstrap_automation_mode(st, cid, cfg) == "auto_ai"
    assert st.set_calls == []


def test_persona_is_user_explicit_registry_and_auto_attach():
    """显式绑定 vs 自动补默认。"""
    cfg = _cfg(onboarding=True, bootstrap=True)
    cfg["platform_login"]["auto_attach_default_persona"] = True
    reg = _Reg({"whatsapp:a": {"meta": {"persona_id": "lin_xiaoyu"}}})
    assert amo.persona_is_user_explicit(
        "whatsapp", "a", cfg, registry=reg) is False
    reg2 = _Reg({"whatsapp:a": {"meta": {"persona_id": "chen_mo"}}})
    assert amo.persona_is_user_explicit(
        "whatsapp", "a", cfg, registry=reg2) is True
    assert amo.persona_is_user_explicit(
        "whatsapp", "missing", cfg, registry=_Reg()) is False
