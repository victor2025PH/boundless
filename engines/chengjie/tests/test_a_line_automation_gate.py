# -*- coding: utf-8 -*-
"""A 线（telegram companion）须尊重收件箱 automation_mode。

真机事故：坐席在统一收件箱切「手动 / AI草稿我审」，下拉已变，但 katie↔Carlin
仍全自动互聊——根因是 companion A 线直发从不读 conversation_settings。
本文件钉「解析 → 是否允许直发」契约；接线在 telegram_client / autodraft_helpers。
"""
from unittest.mock import MagicMock

from src.inbox.automation_mode import allows_direct_autosend, resolve_automation_mode
from src.inbox.normalizer import conv_id


def _cfg(mode="auto_ai"):
    return {"inbox": {"auto_draft": {"automation_mode": mode}}}


def test_carlin_katie_manual_must_block_direct():
    """截图会话：telegram:katie:<chat> 显式 manual → A 线/主动触达不得直发。"""
    store = MagicMock()
    store.get_automation_mode_if_set.return_value = "manual"
    cid = conv_id("telegram", "katie", "8921664288")
    mode = resolve_automation_mode(store, cid, _cfg("auto_ai"))
    assert mode == "manual"
    assert allows_direct_autosend(mode) is False


def test_review_must_block_direct_even_when_global_auto():
    store = MagicMock()
    store.get_automation_mode_if_set.return_value = "review"
    mode = resolve_automation_mode(
        store, conv_id("telegram", "katie", "1"), _cfg("auto_ai"))
    assert allows_direct_autosend(mode) is False


def test_unset_follows_global_auto_ai():
    """无显式档位时沿用全局 auto_ai → companion 默认仍全自动（零行为变更）。"""
    store = MagicMock()
    store.get_automation_mode_if_set.return_value = None
    mode = resolve_automation_mode(
        store, conv_id("telegram", "katie", "1"), _cfg("auto_ai"))
    assert mode == "auto_ai"
    assert allows_direct_autosend(mode) is True


def test_source_wiring_mentions_allows_direct_autosend():
    """防回归：A 线闸门源码须调用 allows_direct_autosend（勿再只改 UI）。"""
    from pathlib import Path
    src = Path("src/client/telegram_client.py").read_text(encoding="utf-8")
    assert "allows_direct_autosend" in src
    assert "resolve_automation_mode" in src
    assert "_mirror_inbox" in src
