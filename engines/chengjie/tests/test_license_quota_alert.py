"""P4c 授权字符额度水位主动告警（HealthWatchdog._check_license_quota）。

语义：临近（≥warn_pct）提醒 / 触顶升级（独立 key 不被 warn 冷却压住）/ 兑换回落后
给告过警的部署补恢复通知；无授权/不限量（included=0）天然静默；重提去抖交给
notify_host 按 key 冷却（这里只验证发起面）。
"""

import types

from src.inbox.health_watchdog import HealthWatchdog
from src.utils import host_alert


class _CM:
    def __init__(self, config):
        self.config = config


def _watchdog(qr=None):
    cfg = {"health_watchdog": {"quota_remind": qr}} if qr is not None else {}
    app = types.SimpleNamespace(state=types.SimpleNamespace())
    return HealthWatchdog(app=app, config_manager=_CM(cfg), interval_sec=60)


def _patch_quota(monkeypatch, **q):
    base = {"allowed": True, "exceeded": False, "enforce": False,
            "used": 0, "included": 0, "included_base": 0, "topup_chars": 0,
            "remaining": None, "lic_id": "L"}
    base.update(q)
    import src.licensing.quota_store as qs
    monkeypatch.setattr(qs, "check_license_quota", lambda **kw: dict(base))


def _capture(monkeypatch):
    seen = []
    monkeypatch.setattr(
        host_alert, "notify_host",
        lambda title, msg, *, key="", cooldown_sec=0.0:
            seen.append((title, msg, key)) or True)
    return seen


def test_warn_threshold_alerts_and_points_to_membership(monkeypatch):
    seen = _capture(monkeypatch)
    _patch_quota(monkeypatch, used=90, included=100)
    wd = _watchdog()
    wd._check_license_quota(now=1000.0)
    assert len(seen) == 1
    title, msg, key = seen[0]
    assert title == "字符额度即将用尽" and key == "license_quota:warn"
    assert "兑换加量包" in msg and "90" in msg
    assert wd.total_license_quota_alerts == 1
    # 10min 稀疏节流：紧接着的 tick 不重复读数
    wd._check_license_quota(now=1300.0)
    assert len(seen) == 1


def test_exceeded_escalates_with_enforce_wording(monkeypatch):
    seen = _capture(monkeypatch)
    _patch_quota(monkeypatch, used=100, included=100, exceeded=True, enforce=True)
    wd = _watchdog()
    wd._check_license_quota(now=1000.0)
    assert seen[0][0] == "字符额度已用尽"
    assert seen[0][2] == "license_quota:exceeded"
    assert "已被限制" in seen[0][1]
    # enforce 关：如实说明仍在放行
    seen.clear()
    _patch_quota(monkeypatch, used=100, included=100, exceeded=True, enforce=False)
    wd2 = _watchdog()
    wd2._check_license_quota(now=1000.0)
    assert "仍在放行" in seen[0][1]


def test_recovery_only_after_alerted(monkeypatch):
    seen = _capture(monkeypatch)
    wd = _watchdog()
    # 一直健康：不发任何东西（尤其不发「恢复」）
    _patch_quota(monkeypatch, used=10, included=100)
    wd._check_license_quota(now=1000.0)
    assert seen == []
    # 触警 → 兑换加量包回落 → 恢复通知一次
    _patch_quota(monkeypatch, used=90, included=100)
    wd._check_license_quota(now=2000.0)
    assert len(seen) == 1
    _patch_quota(monkeypatch, used=90, included=200, topup_chars=100)
    wd._check_license_quota(now=3000.0)
    assert len(seen) == 2
    assert seen[1][0] == "字符额度已恢复" and seen[1][2] == "license_quota:recover"
    # 恢复后保持健康：不再重复报平安
    wd._check_license_quota(now=4000.0)
    assert len(seen) == 2


def test_unlimited_and_disabled_are_silent(monkeypatch):
    seen = _capture(monkeypatch)
    _patch_quota(monkeypatch, used=999, included=0)  # 不限量授权
    wd = _watchdog()
    wd._check_license_quota(now=1000.0)
    assert seen == []
    _patch_quota(monkeypatch, used=100, included=100, exceeded=True)
    wd_off = _watchdog(qr={"enabled": False})
    wd_off._check_license_quota(now=1000.0)
    assert seen == []


def test_custom_warn_pct(monkeypatch):
    seen = _capture(monkeypatch)
    _patch_quota(monkeypatch, used=60, included=100)
    wd = _watchdog(qr={"enabled": True, "warn_pct": 50})
    wd._check_license_quota(now=1000.0)
    assert len(seen) == 1 and seen[0][2] == "license_quota:warn"


def test_status_snapshot_exposes_counter(monkeypatch):
    _patch_quota(monkeypatch, used=90, included=100)
    _capture(monkeypatch)
    wd = _watchdog()
    wd._check_license_quota(now=1000.0)
    assert wd.status_snapshot()["total_license_quota_alerts"] == 1
