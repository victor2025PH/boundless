"""P3-1：节点「已恢复在线」推送：同通道、带离线时长、抖动限频。"""
from __future__ import annotations

import logging

from src.fleet import offline_alert as oa

NOW = 1_800_000_000.0


def _n(nid, state, last_seen, status="active", **kw):
    d = {"node_id": nid, "state": state, "status": status, "last_seen": last_seen, "label": nid.upper(),
         "host_name": "PC-" + nid, "group_name": "机房A"}
    d.update(kw)
    return d


def _rec():
    sent = []
    return sent, (lambda text, nid, deb: sent.append((nid, text, deb)))


def test_recovered_message_carries_offline_duration(caplog):
    off, rec = [], []
    al = oa.OfflineAlerter(10, notify=lambda t, n, d: off.append((n, t)),
                           recover_notify=lambda t, n, d: rec.append((n, t, d)))
    caplog.set_level(logging.INFO)
    al.check([_n("a", "offline", NOW - 15 * 60)], now=NOW)
    assert len(off) == 1 and rec == []
    al.check([_n("a", "online", NOW + 30 * 60)], now=NOW + 30 * 60)
    assert len(rec) == 1 and rec[0][0] == "a"
    assert "已恢复在线" in rec[0][1] and "离线约 45 分钟" in rec[0][1] and "「A」" in rec[0][1]
    assert "机房A" in rec[0][1] and rec[0][2] == 600
    assert "fleet node_offline_recovered node=a offline_min=45" in caplog.text
    assert len(off) == 1


def test_never_alerted_node_gets_no_recovery_message():
    sent, fn = _rec()
    al = oa.OfflineAlerter(10, notify=fn)
    al.check([_n("a", "offline", NOW - 5 * 60)], now=NOW)          # 不到 10 分钟，没告警
    al.check([_n("a", "online", NOW + 60)], now=NOW + 60)
    assert sent == []


def test_flapping_node_recovery_is_rate_limited(caplog):
    off, rec = [], []
    al = oa.OfflineAlerter(10, notify=lambda t, n, d: off.append(n),
                           recover_notify=lambda t, n, d: rec.append(n), recover_min_interval_sec=600)
    caplog.set_level(logging.INFO)
    t = NOW
    al.check([_n("a", "offline", t - 11 * 60)], now=t)
    al.check([_n("a", "online", t + 60)], now=t + 60)              # 第一次恢复：推
    al.check([_n("a", "offline", t + 60)], now=t + 12 * 60)        # 又离线 11 分钟：离线照常告警
    al.check([_n("a", "online", t + 13 * 60)], now=t + 13 * 60)    # 12 分钟后又恢复：已过 10 分钟，推
    al.check([_n("a", "offline", t + 13 * 60)], now=t + 24 * 60)
    al.check([_n("a", "online", t + 25 * 60)], now=t + 25 * 60)    # 距上次恢复 12 分钟：推
    assert off == ["a", "a", "a"] and rec == ["a", "a", "a"]

    rec.clear()
    al2 = oa.OfflineAlerter(1, notify=lambda *a: None, recover_notify=lambda t, n, d: rec.append(n))
    for k in range(5):                                             # 1 分钟阈值下快速抖动 5 次（每轮 2 分钟）
        base = NOW + k * 120
        al2.check([_n("b", "offline", base - 61)], now=base)
        al2.check([_n("b", "online", base + 60)], now=base + 60)
    assert rec == ["b"]                                            # 10 分钟内只推一次
    assert "suppressed=True" in caplog.text


def test_recovery_between_ticks_is_detected():
    rec = []
    al = oa.OfflineAlerter(10, notify=lambda *a: None, recover_notify=lambda t, n, d: rec.append(t))
    al.check([_n("a", "offline", NOW - 20 * 60)], now=NOW)
    # 两次巡检之间回来过又刚掉：state=offline 但最后心跳比告警时新
    al.check([_n("a", "offline", NOW + 30)], now=NOW + 60)
    assert len(rec) == 1 and "离线约 20 分钟" in rec[0]


def test_revoked_or_deleted_node_is_cleared_silently():
    rec = []
    al = oa.OfflineAlerter(10, notify=lambda *a: None, recover_notify=lambda t, n, d: rec.append(n))
    al.check([_n("a", "offline", NOW - 20 * 60), _n("b", "offline", NOW - 20 * 60)], now=NOW)
    al.check([_n("a", "offline", NOW - 20 * 60, status="revoked")], now=NOW + 60)
    assert rec == [] and al._alerted == {}


def test_recover_notify_failure_is_logged(caplog):
    def boom(*a):
        raise RuntimeError("tg down")
    al = oa.OfflineAlerter(10, notify=lambda *a: None, recover_notify=boom)
    caplog.set_level(logging.WARNING)
    al.check([_n("a", "offline", NOW - 20 * 60)], now=NOW)
    al.check([_n("a", "online", NOW + 60)], now=NOW + 60)
    assert "node_recovered notify failed node=a" in caplog.text


def test_default_channels_use_ops_alert_with_distinct_kinds(monkeypatch):
    calls = []
    import src.ops.ops_alert as ops
    monkeypatch.setattr(ops, "notify", lambda kind, text, **kw: calls.append((kind, text, kw)) or True)
    al = oa.OfflineAlerter(10)
    al.check([_n("c", "offline", NOW - 20 * 60)], now=NOW)
    al.check([_n("c", "online", NOW + 60)], now=NOW + 60)
    assert [c[0] for c in calls] == ["fleet_node_offline", "fleet_node_recovered"]
    assert calls[1][2]["account_id"] == "c" and calls[1][2]["source"] == "fleet-controller"
    assert calls[1][2]["reason"] == "recovered" and "已恢复在线" in calls[1][1]
