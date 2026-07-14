# -*- coding: utf-8 -*-
"""autodraft bootstrap 接线单测（Phase13）。"""
from unittest.mock import MagicMock, patch

from src.inbox.autodraft_helpers import AutoDraftConfig, make_auto_draft_cb


def test_autodraft_bootstrap_persists_auto_ai():
    store = MagicMock()
    store.get_automation_mode_if_set.return_value = None
    ds = MagicMock()
    ds.auto_generate_draft.return_value = "d1"
    cfg = AutoDraftConfig(
        mode="auto_ai", min_len=0, skip=set(),
        platform_ceilings={}, skip_groups=False, enrich=False,
    )
    app_config = {"inbox": {"auto_draft": {
        "automation_mode": "auto_ai",
        "bootstrap_automation_mode": True,
    }}}
    cb = make_auto_draft_cb(
        cfg, ds, store, MagicMock(), MagicMock(), MagicMock(),
        app_config=app_config,
    )
    cb({"platform": "telegram", "conversation_id": "telegram:a:1"}, "hi")
    store.set_automation_mode.assert_called_once_with("telegram:a:1", "auto_ai")
    _, kw = ds.auto_generate_draft.call_args
    assert kw["automation_mode"] == "auto_ai"
