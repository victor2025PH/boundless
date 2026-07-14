"""Phase17：主动触达分形态（photo/voice/text）A/B 回执数据链门禁。

写入端：proactive_topic._send 成功后 record_outreach(batch_id=proactive_topic:<kind>)；
读取端：outreach_response_stats 按批次算回复率。此处验证 store 层数据链路闭合
（写入 → 触达后入站 → 回复率口径），路由只是薄封装。
"""
from __future__ import annotations

import time

from src.inbox.models import InboxConversation, InboxMessage
from src.inbox.store import InboxStore


def _mk_store(tmp_path):
    return InboxStore(tmp_path / "inbox.db")


def _conv(cid: str) -> InboxConversation:
    return InboxConversation(
        conversation_id=cid, platform="telegram", account_id="acc",
        chat_key=cid.split(":")[-1], display_name="c")


def test_kind_ab_batches_roundtrip(tmp_path):
    st = _mk_store(tmp_path)
    now = time.time() - 3600.0
    # photo 触达：1 回复 / 1 未回复
    st.record_outreach("telegram:acc:1", batch_id="proactive_topic:photo",
                       platform="telegram", account_id="acc",
                       note="gentle_checkin", ts=now)
    st.record_outreach("telegram:acc:2", batch_id="proactive_topic:photo",
                       platform="telegram", account_id="acc",
                       note="follow_up", ts=now)
    # text 触达：1 回复
    st.record_outreach("telegram:acc:3", batch_id="proactive_topic:text",
                       platform="telegram", account_id="acc",
                       note="gentle_checkin", ts=now)
    # conv1 与 conv3 在触达后回复了（ingest_batch=会话+入站消息一并落库）
    st.ingest_batch(_conv("telegram:acc:1"), [InboxMessage(
        conversation_id="telegram:acc:1", platform_msg_id="m1",
        direction="in", text="哇好看", ts=now + 600)])
    st.ingest_batch(_conv("telegram:acc:3"), [InboxMessage(
        conversation_id="telegram:acc:3", platform_msg_id="m2",
        direction="in", text="在呢", ts=now + 1200)])
    photo = st.outreach_response_stats(
        "proactive_topic:photo", response_window_days=3.0)
    text = st.outreach_response_stats(
        "proactive_topic:text", response_window_days=3.0)
    voice = st.outreach_response_stats(
        "proactive_topic:voice", response_window_days=3.0)
    assert photo["sent"] == 2 and photo["responded"] == 1
    assert photo["response_rate"] == 0.5
    assert text["sent"] == 1 and text["responded"] == 1
    assert voice["sent"] == 0  # 未发过 → 空批次口径稳定
