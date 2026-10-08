"""智安集成修复：官方轨 STOP 软委托统一闸后，会话未入库也必须冻结（需人工 + 停联标签）。

#75/#76（official_stop_gate）× #66（compliance.stop_gate）合并后的语义冲突回归测试：
record_stop 对未入库会话只进名单（frozen=False），official_stop_gate 必须回落本地冻结。
只用临时库，不发任何消息。
"""
from __future__ import annotations

import pytest


@pytest.fixture()
def store(tmp_path):
    from src.inbox.store import InboxStore
    from src.integrations import protocol_bridge
    from src.integrations.shared import official_stop_gate as osg
    st = InboxStore(tmp_path / "inbox.db")
    protocol_bridge.register_inbox_store_getter(lambda: st)
    osg.reset_for_tests()
    yield st
    osg.reset_for_tests()
    protocol_bridge.register_inbox_store_getter(None)


def test_apply_stop_freezes_even_when_conversation_not_in_store(store):
    from src.inbox.normalizer import conv_id
    from src.inbox.stop_contact import frozen_reason
    from src.integrations.shared import official_stop_gate as osg
    assert osg._compliance() is not None  # 统一闸已合入
    out = osg.apply_stop("telegram", "bot1", "tg:user:42", hits=["stop"], store=store)
    assert out["compliance"] is True
    assert out["frozen"] is True
    assert frozen_reason(store, conv_id("telegram", "bot1", "tg:user:42"))
    assert osg.is_stopped("telegram", "bot1", "tg:user:42", store=store)


def test_apply_stop_is_idempotent(store):
    from src.integrations.shared import official_stop_gate as osg
    osg.apply_stop("whatsapp", "pn1", "wa:user:639170000001", hits=["stop"], store=store)
    out = osg.apply_stop("whatsapp", "pn1", "wa:user:639170000001", hits=["stop"], store=store)
    assert out["frozen"] is True
