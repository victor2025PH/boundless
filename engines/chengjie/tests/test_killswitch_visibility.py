"""急停可见化（P0 2026-08-23）：归因 / 就地解除数据面 / 审计落账。

实录事故：横幅文案写死「运营手动冻结外发」，而置位来源有两种（人工 API/面板
vs ban_signal 自动风控处置）——自动冻结被错误归因成人工操作，且无倒计时、
无解除入口、无历史审计。本门禁钉住四件事：

1. ``freeze_source`` 纯函数：auto/manual 判定 + cause 残端提取；
2. ``KillSwitch.blocking_record`` / 模块级 ``active_record``：与 ``is_blocked``
   同一判定链、TTL 惰性清理、单例缺失 fail-open；
3. ``send_gate_snapshot`` 附 ``kill`` 段（scope/source/cause/actor/expires_at），
   未冻结时不带该键（旧后端兼容＝前端 feat 探测）；
4. 置位/解除/自动置位 → ops_events 审计行（此前 clear 即删行，历史查无对证）。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import src.ops.kill_switch as ks_mod
from src.ops.kill_switch import KillSwitch, active_record, freeze_source


@pytest.fixture
def fresh_ks(tmp_path, monkeypatch):
    ks = KillSwitch(tmp_path / "runtime_flags.db")
    monkeypatch.setattr(ks_mod, "_singleton", ks)
    return ks


@pytest.fixture
def ops_store(tmp_path):
    from src.ops import ops_events as oe
    oe.reset_ops_event_store()
    st = oe.get_ops_event_store(str(tmp_path / "ops_events.db"))
    yield st
    oe.reset_ops_event_store()


# ── freeze_source 纯函数 ─────────────────────────────────────────────────────

def test_freeze_source_auto_by_actor():
    assert freeze_source("ban_signal", "auto_pause:PeerFlood") == ("auto", "PeerFlood")


def test_freeze_source_auto_by_reason_prefix():
    # reason 前缀本身就是 G2 写入形态，actor 异常丢失也不误判成人工
    assert freeze_source("", "auto_ban:UserDeactivated") == ("auto", "UserDeactivated")


def test_freeze_source_actor_case_insensitive_no_colon():
    src, cause = freeze_source("BAN_SIGNAL", "whatever")
    assert src == "auto" and cause == "whatever"


def test_freeze_source_manual_keeps_reason():
    assert freeze_source("boss", "内容事故灭火") == ("manual", "内容事故灭火")


def test_freeze_source_empty_is_manual():
    assert freeze_source(None, None) == ("manual", "")


# ── blocking_record / active_record ─────────────────────────────────────────

def test_blocking_record_returns_full_rec(fresh_ks):
    fresh_ks.set("account:telegram:1", reason="auto_pause:PeerFlood",
                 actor="ban_signal", ttl_sec=3600)
    rec = fresh_ks.blocking_record("telegram", "1")
    assert rec is not None
    assert rec["scope"] == "account:telegram:1"
    assert rec["reason"] == "auto_pause:PeerFlood"
    assert rec["actor"] == "ban_signal"
    assert float(rec["expires_at"]) > 0


def test_blocking_record_global_wins_chain(fresh_ks):
    fresh_ks.set("account:telegram:1", reason="acct")
    fresh_ks.set("global", reason="all-stop", actor="boss")
    rec = fresh_ks.blocking_record("telegram", "1")
    assert rec["scope"] == "global"


def test_blocking_record_ttl_expired_lazy_clean(fresh_ks):
    fresh_ks.set("account:telegram:2", reason="r", ttl_sec=10, now=1000.0)
    assert fresh_ks.blocking_record("telegram", "2", now=1011.0) is None
    assert fresh_ks.status(now=1011.0) == []


def test_blocking_record_none_when_clear(fresh_ks):
    assert fresh_ks.blocking_record("telegram", "9") is None


def test_active_record_fail_open_without_singleton(monkeypatch):
    monkeypatch.setattr(ks_mod, "_singleton", None)
    assert active_record("telegram", "1") is None


def test_active_record_reads_singleton(fresh_ks):
    fresh_ks.set("platform:whatsapp", reason="平台灭火", actor="admin")
    rec = active_record("whatsapp", "any")
    assert rec and rec["scope"] == "platform:whatsapp"


# ── send_gate_snapshot 附 kill 段 ────────────────────────────────────────────

def _snap(platform, account_id):
    from src.inbox.send_gate_status import send_gate_snapshot
    return send_gate_snapshot(platform, account_id, "",
                              config={}, registry=SimpleNamespace())


def test_snapshot_carries_auto_kill(fresh_ks):
    fresh_ks.set("account:telegram:9", reason="auto_pause:PeerFlood",
                 actor="ban_signal", ttl_sec=3600)
    snap = _snap("telegram", "9")
    assert snap and snap["blocked"] is True
    assert snap["reason"] == "kill_switch:account:telegram:9"
    k = snap["kill"]
    assert k["scope"] == "account:telegram:9"
    assert k["source"] == "auto" and k["cause"] == "PeerFlood"
    assert k["actor"] == "ban_signal"
    assert isinstance(k["expires_at"], float) and k["expires_at"] > 0


def test_snapshot_carries_manual_kill_no_ttl(fresh_ks):
    fresh_ks.set("global", reason="内容事故", actor="boss")
    snap = _snap("whatsapp", "x")
    k = snap["kill"]
    assert k["scope"] == "global" and k["source"] == "manual"
    assert k["cause"] == "内容事故" and k["actor"] == "boss"
    assert k["expires_at"] is None


def test_snapshot_none_when_not_blocked(fresh_ks):
    # 无冻结 + 反封号闸门未启用 → 快照 None（旧契约不变）
    assert _snap("telegram", "77") is None


def test_snapshot_no_kill_key_for_non_killswitch_reasons(fresh_ks, monkeypatch):
    # 非急停拦因（如授权）不得带 kill 段——前端按键名分族，串键=文案错族
    import src.inbox.send_gate_status as sgs
    monkeypatch.setattr(
        "src.integrations.shared.send_guard.send_blocked",
        lambda *a, **k: (True, "license_readonly"))
    snap = sgs.send_gate_snapshot("telegram", "1", "",
                                  config={}, registry=SimpleNamespace())
    assert snap["blocked"] is True and "kill" not in snap


# ── 审计落账 ─────────────────────────────────────────────────────────────────

def test_scope_parts_pure():
    from src.web.routes.ops_killswitch_routes import scope_parts
    assert scope_parts("account:telegram:123") == ("telegram", "123")
    assert scope_parts("platform:whatsapp") == ("whatsapp", "")
    assert scope_parts("global") == ("global", "")
    assert scope_parts("junk") == ("global", "")


def test_manual_audit_helper_records(ops_store):
    from src.web.routes.ops_killswitch_routes import _audit_killswitch
    _audit_killswitch("kill_switch_set", "account:telegram:5",
                      actor="boss", reason="灭火", detail="ttl_sec=7200")
    rows = ops_store.recent(account_id="5")
    assert rows and rows[0]["kind"] == "kill_switch_set"
    assert rows[0]["platform"] == "telegram"
    assert rows[0]["reason"] == "灭火"
    assert "actor=boss" in rows[0]["detail"]
    assert "ttl_sec=7200" in rows[0]["detail"]


def test_manual_audit_survives_store_absence(monkeypatch):
    # 审计降级绝不阻断急停操作
    from src.web.routes import ops_killswitch_routes as r
    monkeypatch.setattr("src.ops.ops_events.get_ops_event_store",
                        lambda *a, **k: None)
    r._audit_killswitch("kill_switch_clear", "global", actor="x")  # 不抛即过


def test_ban_signal_auto_freeze_audited(ops_store):
    from src.ops.ban_signal import apply_action

    class _KS:
        def set(self, *a, **k):
            return {}

    out = apply_action("telegram", "7",
                       {"kind": "pause", "cooldown_sec": 60, "reason": "PeerFlood"},
                       kill_switch=_KS())
    assert out["applied"] == "pause"
    rows = ops_store.recent(account_id="7")
    assert rows and rows[0]["kind"] == "kill_switch_set"
    assert rows[0]["reason"] == "auto_pause:PeerFlood"
    assert "actor=ban_signal" in rows[0]["detail"]


def test_ban_signal_ban_audited_permanent(ops_store):
    from src.ops.ban_signal import apply_action

    class _KS:
        def set(self, *a, **k):
            return {}

    apply_action("telegram", "8", {"kind": "ban", "reason": "UserDeactivated"},
                 kill_switch=_KS())
    rows = ops_store.recent(account_id="8")
    assert rows and rows[0]["reason"] == "auto_ban:UserDeactivated"
    assert "ttl_sec=0" in rows[0]["detail"]


# ── status_snapshot（ai-runtime-status 顶栏摘要的数据面）────────────────────

def test_status_snapshot_fail_open_without_singleton(monkeypatch):
    from src.ops.kill_switch import status_snapshot
    monkeypatch.setattr(ks_mod, "_singleton", None)
    assert status_snapshot() == []


def test_status_snapshot_lists_active(fresh_ks):
    from src.ops.kill_switch import status_snapshot
    fresh_ks.set("global", reason="灭火", actor="boss")
    fresh_ks.set("account:telegram:3", reason="auto_pause:PeerFlood",
                 actor="ban_signal", ttl_sec=60)
    snap = status_snapshot()
    assert [i["scope"] for i in snap][0] == "global"   # global 排最前（status 契约）
    assert len(snap) == 2


# ── 价值周报「风控防护」段（P2：审计数据 → 周价值读数）──────────────────────

def test_safety_window_splits_auto_manual_lifts(ops_store):
    from src.ops.value_report import _safety_window
    ops_store.record("kill_switch_set", account_id="1", platform="telegram",
                     reason="auto_pause:PeerFlood", ts=1000.0)
    ops_store.record("kill_switch_set", account_id="2", platform="telegram",
                     reason="auto_ban:UserDeactivated", ts=1001.0)
    ops_store.record("kill_switch_set", account_id="", platform="global",
                     reason="内容事故", ts=1002.0)
    ops_store.record("kill_switch_clear", account_id="", platform="global",
                     reason="", ts=1003.0)
    ops_store.record("kill_switch_set", account_id="9", platform="telegram",
                     reason="auto_pause:X", ts=2000.0)   # 窗外
    w = _safety_window(ops_store, 900.0, 1500.0)
    assert w == {"auto_freezes": 2, "manual_freezes": 1, "lifts": 1}


def test_safety_window_store_error_soft_zero():
    from src.ops.value_report import _safety_window

    class _Boom:
        @property
        def _lock(self):
            raise RuntimeError("x")

    assert _safety_window(_Boom(), 0, 1) == {
        "auto_freezes": 0, "manual_freezes": 0, "lifts": 0}


def test_weekly_lines_include_safety_when_present():
    from src.ops.value_report import weekly_value_lines
    tw = {"safety": {"auto_freezes": 3, "manual_freezes": 1, "lifts": 2}}
    lw = {"safety": {"auto_freezes": 1, "manual_freezes": 0, "lifts": 0}}
    lines = weekly_value_lines(tw, lw)
    assert any("风控防护" in ln and "急停 4 次" in ln
               and "自动风控 3 次" in ln and "人工解除 2 次" in ln
               for ln in lines)


def test_weekly_lines_no_safety_line_when_absent():
    from src.ops.value_report import weekly_value_lines
    assert not any("风控防护" in ln for ln in weekly_value_lines({}, {}))
