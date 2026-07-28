# -*- coding: utf-8 -*-
"""automation_mode 解析与 bootstrap 单测（Phase13 好友全自动）。"""
from unittest.mock import MagicMock

from src.inbox.automation_mode import (
    allows_direct_autosend,
    bootstrap_enabled_from_config,
    global_automation_mode_from_config,
    human_gate_skip_reason,
    maybe_bootstrap_automation_mode,
    resolve_automation_mode,
)


def _cfg(**ad_kw):
    return {"inbox": {"auto_draft": ad_kw}}


def test_global_mode_defaults_auto_ai():
    assert global_automation_mode_from_config({}) == "auto_ai"
    assert global_automation_mode_from_config(_cfg(automation_mode="review")) == "review"


def test_bootstrap_default_on_when_auto_ai():
    assert bootstrap_enabled_from_config(_cfg(automation_mode="auto_ai")) is True
    assert bootstrap_enabled_from_config(_cfg(
        automation_mode="auto_ai", bootstrap_automation_mode=False)) is False
    assert bootstrap_enabled_from_config(_cfg(automation_mode="review")) is False


def test_maybe_bootstrap_persists_auto_ai():
    store = MagicMock()
    store.get_automation_mode_if_set.return_value = None
    cid = "telegram:acc:123"
    mode = maybe_bootstrap_automation_mode(
        store, cid, _cfg(automation_mode="auto_ai"))
    assert mode == "auto_ai"
    store.set_automation_mode.assert_called_once_with(cid, "auto_ai")


def test_bootstrap_respects_explicit_mode():
    store = MagicMock()
    store.get_automation_mode_if_set.return_value = "manual"
    mode = maybe_bootstrap_automation_mode(
        store, "c1", _cfg(automation_mode="auto_ai"))
    assert mode == "manual"
    store.set_automation_mode.assert_not_called()


def test_resolve_without_bootstrap():
    store = MagicMock()
    store.get_automation_mode_if_set.return_value = None
    assert resolve_automation_mode(
        store, "c1", _cfg(automation_mode="auto_ai")) == "auto_ai"
    store.get_automation_mode_if_set.return_value = "review"
    assert resolve_automation_mode(
        store, "c1", _cfg(automation_mode="auto_ai")) == "review"


def test_maybe_bootstrap_records_stats():
    store = MagicMock()
    store.get_automation_mode_if_set.return_value = None
    cid = "messenger:acc:99"
    maybe_bootstrap_automation_mode(
        store, cid, _cfg(automation_mode="auto_ai"))
    from src.inbox.automation_mode_stats import metrics_snapshot
    snap = metrics_snapshot()
    assert snap["bootstrap_total"] >= 1
    assert snap["last"]["platform"] == "messenger"


def test_allows_direct_autosend_only_auto_ai():
    """坐席「手动/草稿我审」必须停 A 线直发与主动触达。"""
    assert allows_direct_autosend("auto_ai") is True
    assert allows_direct_autosend("AUTO_AI") is True
    assert allows_direct_autosend("manual") is False
    assert allows_direct_autosend("review") is False
    assert allows_direct_autosend("multi_choice") is False
    assert allows_direct_autosend("") is False
    assert allows_direct_autosend(None) is False


def test_human_gate_skip_reason():
    assert human_gate_skip_reason("manual") == "inbox_manual"
    assert human_gate_skip_reason("review") == "inbox_human_gate"
    assert human_gate_skip_reason("multi_choice") == "inbox_human_gate"
    assert human_gate_skip_reason("auto_ai") == ""
    assert human_gate_skip_reason("") == ""
