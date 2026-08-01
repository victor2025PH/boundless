"""告警链路自检纯函数门禁（2026-08-01）。

钉住 :mod:`src.integrations.alert_link_audit` 的三条不变量：
  1. 别名级覆盖（配了通道 ≠ 覆盖高价值别名；``all`` 通配；``enabled:false`` 不算数）；
  2. ``healthy`` = 有启用通道 且 关注别名零未覆盖；
  3. 诱饵文件检测（引擎根有、数据根无 → 命中；同根 → 不命中）。
"""

from __future__ import annotations

from src.integrations.alert_link_audit import (
    HIGH_VALUE_ALIASES,
    WEBHOOKS_REL,
    audit_alert_link,
    channel_covers,
    default_focus_aliases,
    detect_orphan_config,
)


def test_focus_includes_high_value_aliases():
    focus = default_focus_aliases()
    for a in HIGH_VALUE_ALIASES:
        assert a in focus, f"{a} 必须在关注别名里"
    # 去重：无重复
    assert len(focus) == len(set(focus))


def test_empty_is_not_healthy():
    a = audit_alert_link([])
    assert a["channels_enabled"] == 0
    assert a["has_any_channel"] is False
    assert a["healthy"] is False
    # 0 通道时所有关注别名都未覆盖
    assert set(a["uncovered"]) == set(a["focus_aliases"])


def test_channel_covers_direct_and_all():
    assert channel_covers({"events": ["draft_backlog"]}, "draft_backlog") is True
    assert channel_covers({"events": ["all"]}, "host_alert") is True
    assert channel_covers({"events": ["report"]}, "draft_backlog") is False
    assert channel_covers({"events": []}, "host_alert") is False
    assert channel_covers({}, "host_alert") is False


def test_partial_coverage_reports_uncovered():
    # 一个只订阅 draft_backlog 的通道：其余高价值别名仍未覆盖
    hooks = [{"name": "tg", "format": "telegram", "events": ["draft_backlog"]}]
    a = audit_alert_link(hooks, focus_aliases=list(HIGH_VALUE_ALIASES))
    assert a["has_any_channel"] is True
    assert a["per_alias"]["draft_backlog"] == ["tg"]
    assert "human_deliver" in a["uncovered"]
    assert "host_alert" in a["uncovered"]
    assert a["healthy"] is False


def test_all_alias_covers_everything():
    hooks = [{"name": "catch-all", "format": "json", "events": ["all"]}]
    a = audit_alert_link(hooks, focus_aliases=list(HIGH_VALUE_ALIASES))
    assert a["uncovered"] == []
    assert a["healthy"] is True
    for alias in HIGH_VALUE_ALIASES:
        assert a["per_alias"][alias] == ["catch-all"]


def test_disabled_channel_does_not_count():
    hooks = [{"name": "off", "format": "telegram",
              "events": ["all"], "enabled": False}]
    a = audit_alert_link(hooks, focus_aliases=list(HIGH_VALUE_ALIASES))
    assert a["channels_total"] == 1
    assert a["channels_enabled"] == 0
    assert a["channels_disabled"] == 1
    assert a["healthy"] is False
    assert set(a["uncovered"]) == set(HIGH_VALUE_ALIASES)


def test_full_coverage_is_healthy():
    hooks = [
        {"name": "tg-ops", "format": "telegram",
         "events": ["draft_backlog", "human_deliver", "host_alert"]},
    ]
    a = audit_alert_link(hooks, focus_aliases=list(HIGH_VALUE_ALIASES))
    assert a["healthy"] is True
    assert a["covered_count"] == 3
    assert a["formats"] == {"telegram": 1}


def test_orphan_detected_engine_has_data_missing(tmp_path):
    engine = tmp_path / "engine"
    data = tmp_path / "data"
    (engine / "config").mkdir(parents=True)
    (data / "config").mkdir(parents=True)
    (engine / WEBHOOKS_REL).write_text("[]", encoding="utf-8")
    # 数据根不写文件 → 命中诱饵
    orphan = detect_orphan_config(engine, data)
    assert orphan is not None
    assert orphan["engine_bytes"] >= 0
    assert str(engine) in orphan["engine_file"]


def test_orphan_none_when_data_has_file(tmp_path):
    engine = tmp_path / "engine"
    data = tmp_path / "data"
    (engine / "config").mkdir(parents=True)
    (data / "config").mkdir(parents=True)
    (engine / WEBHOOKS_REL).write_text("[]", encoding="utf-8")
    (data / WEBHOOKS_REL).write_text("[]", encoding="utf-8")
    assert detect_orphan_config(engine, data) is None


def test_orphan_none_when_same_root(tmp_path):
    # 引擎根 == 数据根（开发机/CI）→ 无诱饵问题
    (tmp_path / "config").mkdir(parents=True)
    (tmp_path / WEBHOOKS_REL).write_text("[]", encoding="utf-8")
    assert detect_orphan_config(tmp_path, tmp_path) is None
