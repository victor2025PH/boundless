"""Token 钱包水位主动告警（HealthWatchdog._check_token_wallet，quotawall v2 P2-4）。

语义：enforce 生效且注过资的钱包 耗尽点名（tok_out 判定同源复用
quota_state.resolve_quota_state，与前端额度墙 tok 变体绝不各算一套）/ 有月度含量
分母时临近提醒 / 回充后给告过警的部署补恢复通知；影子记账（enforce=False）、
未启用（enabled=False）、从未注资（funded 守卫）一律静默。重提去抖交给
notify_host 按 key 冷却（这里只验证发起面）。
"""

import types

from src.inbox.health_watchdog import HealthWatchdog
from src.utils import host_alert


class _CM:
    def __init__(self, config):
        self.config = config


def _watchdog(tw=None):
    cfg = {"health_watchdog": {"token_wallet_remind": tw}} if tw is not None else {}
    app = types.SimpleNamespace(state=types.SimpleNamespace())
    return HealthWatchdog(app=app, config_manager=_CM(cfg), interval_sec=60)


def _patch_wallet(monkeypatch, **w):
    base = {"enabled": True, "enforce": True, "balance": 0,
            "active_granted": 1000, "expired_lost": 0, "monthly": 1000}
    base.update(w)
    import src.licensing.token_ledger as tl
    monkeypatch.setattr(tl, "wallet_snapshot",
                        lambda *a, **kw: dict(base))


def _capture(monkeypatch):
    seen = []
    monkeypatch.setattr(
        host_alert, "notify_host",
        lambda title, msg, *, key="", cooldown_sec=0.0:
            seen.append((title, msg, key)) or True)
    return seen


def test_exhausted_alerts_with_degrade_wording(monkeypatch):
    seen = _capture(monkeypatch)
    _patch_wallet(monkeypatch, balance=0)
    wd = _watchdog()
    wd._check_token_wallet(now=1000.0)
    assert len(seen) == 1
    title, msg, key = seen[0]
    assert title == "Token 钱包已耗尽" and key == "token_wallet:exceeded"
    assert "降级" in msg and "不断线" in msg and "会员中心" in msg
    assert wd.total_token_wallet_alerts == 1
    # 10min 稀疏节流：紧接着的 tick 不重复读数
    wd._check_token_wallet(now=1300.0)
    assert len(seen) == 1


def test_shadow_mode_is_silent_even_at_zero(monkeypatch):
    seen = _capture(monkeypatch)
    _patch_wallet(monkeypatch, balance=0, enforce=False)
    wd = _watchdog()
    wd._check_token_wallet(now=1000.0)
    assert seen == []


def test_unfunded_and_disabled_are_silent(monkeypatch):
    seen = _capture(monkeypatch)
    # 从未注资（funded 守卫，与 resolve_quota_state 同源）：不谎报用尽
    _patch_wallet(monkeypatch, balance=0, active_granted=0,
                  expired_lost=0, monthly=0)
    wd = _watchdog()
    wd._check_token_wallet(now=1000.0)
    assert seen == []
    # 未启用 token_ledger
    _patch_wallet(monkeypatch, balance=0, enabled=False)
    wd2 = _watchdog()
    wd2._check_token_wallet(now=1000.0)
    assert seen == []
    # 巡检开关关
    _patch_wallet(monkeypatch, balance=0)
    wd3 = _watchdog(tw={"enabled": False})
    wd3._check_token_wallet(now=1000.0)
    assert seen == []


def test_near_warn_needs_monthly_denominator(monkeypatch):
    seen = _capture(monkeypatch)
    # monthly=1000、剩 100（消耗 90% ≥ 85%）→ 临近提醒
    _patch_wallet(monkeypatch, balance=100, monthly=1000)
    wd = _watchdog()
    wd._check_token_wallet(now=1000.0)
    assert len(seen) == 1 and seen[0][2] == "token_wallet:warn"
    assert "100" in seen[0][1] and "降级" in seen[0][1]
    # 纯充值包部署（monthly=0）余额再低也不做百分比预警（没有分母）
    seen.clear()
    _patch_wallet(monkeypatch, balance=5, monthly=0)
    wd2 = _watchdog()
    wd2._check_token_wallet(now=1000.0)
    assert seen == []


def test_recovery_only_after_alerted(monkeypatch):
    seen = _capture(monkeypatch)
    wd = _watchdog()
    # 一直健康：不发任何东西（尤其不发「恢复」）
    _patch_wallet(monkeypatch, balance=900, monthly=1000)
    wd._check_token_wallet(now=1000.0)
    assert seen == []
    # 耗尽 → 充值回升 → 恢复通知一次
    _patch_wallet(monkeypatch, balance=0)
    wd._check_token_wallet(now=2000.0)
    assert len(seen) == 1
    _patch_wallet(monkeypatch, balance=5000, monthly=1000)
    wd._check_token_wallet(now=3000.0)
    assert len(seen) == 2
    assert seen[1][0] == "Token 钱包已恢复" and seen[1][2] == "token_wallet:recover"
    # 恢复后保持健康：不再重复报平安
    wd._check_token_wallet(now=4000.0)
    assert len(seen) == 2


def test_exhausted_trigger_parity_with_quota_state(monkeypatch):
    """看门狗「耗尽」发起面必须与 resolve_quota_state 的 tok_out 完全一致。

    敢改任何一边的判定（enabled/enforce/funded/balance）都会在这里先红——
    「看板说降级、告警不响」或反过来都是比没有告警更糟的分叉。
    """
    from src.licensing.quota_state import resolve_quota_state
    shapes = [
        dict(enabled=True, enforce=True, balance=0,
             active_granted=100, expired_lost=0, monthly=100),   # out
        dict(enabled=True, enforce=True, balance=0,
             active_granted=0, expired_lost=50, monthly=0),      # out（历史注资）
        dict(enabled=True, enforce=True, balance=1,
             active_granted=100, expired_lost=0, monthly=0),     # 有余额
        dict(enabled=True, enforce=False, balance=0,
             active_granted=100, expired_lost=0, monthly=100),   # 影子
        dict(enabled=False, enforce=True, balance=0,
             active_granted=100, expired_lost=0, monthly=100),   # 未启用
        dict(enabled=True, enforce=True, balance=0,
             active_granted=0, expired_lost=0, monthly=0),       # 从未注资
    ]
    for i, w in enumerate(shapes):
        seen = _capture(monkeypatch)
        _patch_wallet(monkeypatch, **w)
        wd = _watchdog()
        wd._check_token_wallet(now=1000.0 + i)
        expect_out = resolve_quota_state(
            quota=None, level="ok", wallet=w)["tok"]["out"]
        fired = any(k == "token_wallet:exceeded" for _, _, k in seen)
        assert fired == expect_out, f"shape#{i} 分叉：{w}"


def test_status_snapshot_exposes_counter(monkeypatch):
    _capture(monkeypatch)
    _patch_wallet(monkeypatch, balance=0)
    wd = _watchdog()
    wd._check_token_wallet(now=1000.0)
    assert wd.status_snapshot()["total_token_wallet_alerts"] == 1
