"""主动生活照分享（Phase16）纯函数门禁：``photo_share_gate`` + 统计。"""
from __future__ import annotations

from src.companion.proactive_stats import metrics_snapshot, record_photo
from src.companion.proactive_topic import photo_share_gate


def _cfg(**kw):
    base = {"enabled": True, "probability": 1.0, "min_intimacy": 20}
    base.update(kw)
    return base


def test_gate_disabled_by_default():
    assert photo_share_gate({}, mode="gentle_checkin", intimacy=99, rand01=0.0) is False
    assert photo_share_gate(
        {"enabled": False}, mode="gentle_checkin", intimacy=99, rand01=0.0) is False


def test_gate_mode_allowlist():
    ok = _cfg()
    assert photo_share_gate(ok, mode="gentle_checkin", intimacy=50, rand01=0.0)
    assert photo_share_gate(ok, mode="follow_up", intimacy=50, rand01=0.0)
    # 画像采集/付费预告/仪式问候默认不带自拍
    assert not photo_share_gate(ok, mode="ask_birthday", intimacy=50, rand01=0.0)
    assert not photo_share_gate(ok, mode="story_teaser", intimacy=50, rand01=0.0)
    assert not photo_share_gate(ok, mode="ritual_morning", intimacy=50, rand01=0.0)
    # 显式配置 modes 可放开
    custom = _cfg(modes=["ritual_morning"])
    assert photo_share_gate(custom, mode="ritual_morning", intimacy=50, rand01=0.0)
    assert not photo_share_gate(custom, mode="gentle_checkin", intimacy=50, rand01=0.0)


def test_gate_intimacy_threshold():
    assert not photo_share_gate(_cfg(), mode="follow_up", intimacy=10, rand01=0.0)
    assert photo_share_gate(_cfg(), mode="follow_up", intimacy=20, rand01=0.0)
    assert not photo_share_gate(  # intimacy 脏数据不放行
        _cfg(), mode="follow_up", intimacy="oops", rand01=0.0)


def test_gate_probability():
    half = _cfg(probability=0.5)
    assert photo_share_gate(half, mode="follow_up", intimacy=50, rand01=0.4)
    assert not photo_share_gate(half, mode="follow_up", intimacy=50, rand01=0.6)


def test_record_photo_metric():
    before = int(metrics_snapshot().get("photo_sent", 0))
    record_photo()
    assert metrics_snapshot()["photo_sent"] == before + 1
