"""消息不变量 · 接管即静音（Sprint1）。

固定两条行为：
  1) 会话被显式降级为 manual（坐席接管）后，autosend 的统一出站闸门会取消/跳过残留 L2
     （防「接管前已入队的 L2 仍被投递」竞态）；auto_ai 会话不受影响照常发。
  2) 见 tests/test_protocol_autoreply.py::test_manual_conv_stands_down —— protocol 直发对 manual 让位。
"""
from src.inbox.autosend_worker import AutosendWorker
from src.inbox.store import InboxStore


class _FakeSvc:
    """最小 DraftService 替身：_store 为真 InboxStore，list_drafts 返回给定草稿。"""

    def __init__(self, store, drafts):
        self._store = store
        self._drafts = drafts
        self.resolved = []

    def list_drafts(self, status=None, limit=200):
        return list(self._drafts)

    def resolve_with_audit(self, draft_id, action, by=None):
        self.resolved.append(draft_id)
        return {"ok": True}


def _l2(cid, draft_id="d1"):
    return {"draft_id": draft_id, "autopilot_level": "L2", "status": "pending",
            "conversation_id": cid, "platform": "line", "account_id": "acc",
            "chat_key": "c1", "draft_text": "您好，已收到"}


def test_manual_conversation_l2_is_cancelled_not_sent(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    cid = "line:acc:c1"
    store.set_automation_mode(cid, "manual")  # 坐席接管
    svc = _FakeSvc(store, [_l2(cid)])
    w = AutosendWorker(draft_service=svc, config={})
    sent, errors, to_deliver = w._process_batch()
    assert sent == 0, "manual 会话的残留 L2 不应被自动发"
    assert svc.resolved == [], "manual 会话不应 resolve L2"
    assert w.total_skipped_mode == 1


def test_review_conversation_l2_is_cancelled_not_sent(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    cid = "line:acc:c2"
    store.set_automation_mode(cid, "review")  # 显式人审
    svc = _FakeSvc(store, [_l2(cid, "d2")])
    w = AutosendWorker(draft_service=svc, config={})
    sent, _, _ = w._process_batch()
    assert sent == 0 and w.total_skipped_mode == 1


def test_auto_ai_conversation_l2_still_sent(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    cid = "line:acc:c3"
    store.set_automation_mode(cid, "auto_ai")
    svc = _FakeSvc(store, [_l2(cid, "d3")])
    w = AutosendWorker(draft_service=svc, config={})
    sent, _, _ = w._process_batch()
    assert sent == 1, "auto_ai 会话的 L2 应照常自动发"
    assert svc.resolved == ["d3"]
    assert w.total_skipped_mode == 0


def test_unset_mode_l2_still_sent(tmp_path):
    """从未显式设置档位（None）→ 不干预，保持既有默认行为（照常发）。"""
    store = InboxStore(tmp_path / "inbox.db")
    cid = "line:acc:c4"
    svc = _FakeSvc(store, [_l2(cid, "d4")])
    w = AutosendWorker(draft_service=svc, config={})
    sent, _, _ = w._process_batch()
    assert sent == 1 and w.total_skipped_mode == 0
