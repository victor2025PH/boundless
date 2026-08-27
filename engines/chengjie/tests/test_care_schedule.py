"""Phase O2：主动关怀待办持久层单测（:memory:）。

覆盖：add 往返 + 置信度阈值过滤 + 同主题去重 + add_from_text 接线 + list_due/pending +
状态流转（sent/skipped/cancel，仅 pending 可转）+ expire_overdue + count。
"""
from datetime import datetime

from src.contacts.care_commitment import CareCommitment
from src.contacts.care_schedule import CareScheduleStore

NOW = datetime(2026, 6, 17, 10, 0, 0).timestamp()


def _commit(due_offset_days=1.0, topic="面试", conf=0.85):
    due = NOW + due_offset_days * 86400
    return CareCommitment(
        due_at=due, event_at=due - 36000, topic=topic, sentiment="negative",
        anchor_text="明天", source_text="明天面试好紧张", confidence=conf,
    )


def _store():
    return CareScheduleStore(":memory:")


def test_add_and_list_pending():
    s = _store()
    rid = s.add_commitment(_commit(), contact_key="tg:u1", platform="telegram",
                           account_id="default", chat_key="u1")
    assert rid
    pend = s.list_pending()
    assert len(pend) == 1 and pend[0]["topic"] == "面试"
    assert pend[0]["status"] == "pending"
    assert s.count() == 1 and s.count(status="pending") == 1


def test_low_confidence_filtered():
    s = _store()
    rid = s.add_commitment(_commit(conf=0.5), contact_key="tg:u1")
    assert rid is None
    assert s.count() == 0


def test_dedup_same_topic_window():
    s = _store()
    a = s.add_commitment(_commit(due_offset_days=1.0, topic="面试"), contact_key="tg:u1")
    # 同 contact + 同主题 + due 邻近（1 天内）→ 去重
    b = s.add_commitment(_commit(due_offset_days=1.5, topic="面试"), contact_key="tg:u1")
    assert a and b is None
    assert s.count() == 1


def test_dedup_allows_different_topic():
    s = _store()
    a = s.add_commitment(_commit(topic="面试"), contact_key="tg:u1")
    b = s.add_commitment(_commit(topic="复查"), contact_key="tg:u1")
    assert a and b and a != b
    assert s.count() == 2


def test_dedup_allows_far_due():
    s = _store()
    a = s.add_commitment(_commit(due_offset_days=1.0, topic="面试"), contact_key="tg:u1")
    # 同主题但 due 相距 10 天（超 3 天窗口）→ 允许
    b = s.add_commitment(_commit(due_offset_days=11.0, topic="面试"), contact_key="tg:u1")
    assert a and b
    assert s.count() == 2


def test_dedup_scoped_per_contact():
    s = _store()
    a = s.add_commitment(_commit(topic="面试"), contact_key="tg:u1")
    b = s.add_commitment(_commit(topic="面试"), contact_key="tg:u2")
    assert a and b  # 不同 contact 不互相去重
    assert s.count() == 2


def test_far_future_due_rejected_at_capture():
    """B68（实施67 P2-i）：到期日在一年开外＝几乎必是日期解析错误（实锤：
    报告长文里的日期被抓成 2027 年约定）——捕获侧直接拦下不入库。
    锚 time.time()（捕获侧 sanity 的判定时钟），不锚测试固定 NOW。"""
    import time as _t
    s = _store()
    far_due = _t.time() + 400 * 86400
    c = CareCommitment(
        due_at=far_due, event_at=far_due - 36000, topic="检查",
        sentiment="neutral", anchor_text="2027年", source_text="2027年再检查",
        confidence=0.9,
    )
    assert s.add_commitment(c, contact_key="tg:u1") is None
    assert s.count() == 0
    # 一年内的正常约定不受影响
    ok_due = _t.time() + 5 * 86400
    c2 = CareCommitment(
        due_at=ok_due, event_at=ok_due - 3600, topic="复查",
        sentiment="neutral", anchor_text="下周", source_text="下周复查",
        confidence=0.9,
    )
    assert s.add_commitment(c2, contact_key="tg:u1")


def test_capture_cb_excludes_groups_and_bug_intake():
    """B68（实施67 P2-i）：正则捕获回调对群聊 contact 与 bug_intake 会话一律
    跳过（00:12 实锤 2 条报障群消息被捕成关怀约定，值守现场 cancel）。"""
    from src.contacts.care_capture import make_care_inbound_cb

    class _CM:
        config = {
            "companion": {"proactive_care": {"enabled": True, "capture": True}},
            "bug_intake": {"enabled": True, "groups": ["-100999"]},
        }

    s = _store()
    cb = make_care_inbound_cb(s, _CM())
    base = {"conversation_id": "telegram:a:x", "platform": "telegram",
            "account_id": "a", "chat_key": "x"}
    # 群聊 → 不捕
    cb({**base, "chat_type": "group"}, "明天面试好紧张")
    assert s.count() == 0
    # bug_intake 在册会话（私聊形态）→ 不捕
    cb({**base, "chat_key": "-100999", "chat_type": "private"},
       "明天面试好紧张")
    assert s.count() == 0
    # 正常私聊 → 照捕
    cb({**base, "chat_type": "private"}, "明天面试好紧张")
    assert s.count() == 1


def test_add_from_text():
    s = _store()
    ids = s.add_from_text("明天面试好紧张", contact_key="tg:u1", platform="telegram", now=NOW)
    assert len(ids) == 1
    # 无锚点 → 不入库
    assert s.add_from_text("今天好累", contact_key="tg:u1", now=NOW) == []


def test_list_due():
    s = _store()
    s.add_commitment(_commit(due_offset_days=1.0, topic="面试"), contact_key="tg:u1")
    s.add_commitment(_commit(due_offset_days=5.0, topic="复查"), contact_key="tg:u1")
    # now+2 天：只有第一条到期
    due = s.list_due(now=NOW + 2 * 86400)
    assert len(due) == 1 and due[0]["topic"] == "面试"


def test_mark_sent_only_pending():
    s = _store()
    rid = s.add_commitment(_commit(), contact_key="tg:u1")
    assert s.mark_sent(rid) is True
    assert s.count(status="sent") == 1 and s.count(status="pending") == 0
    # 已 sent 不能再转
    assert s.mark_sent(rid) is False
    assert s.mark_skipped(rid) is False
    row = s.list_recent(status="sent")[0]
    assert row["sent_at"] is not None


def test_mark_skipped_and_cancel():
    s = _store()
    r1 = s.add_commitment(_commit(topic="面试"), contact_key="tg:u1")
    r2 = s.add_commitment(_commit(topic="复查"), contact_key="tg:u1")
    assert s.mark_skipped(r1, note="无上下文") is True
    assert s.cancel(r2) is True
    assert s.count(status="skipped") == 1 and s.count(status="cancelled") == 1


def test_expire_overdue():
    s = _store()
    # due 在很久以前
    old = CareCommitment(due_at=NOW - 10 * 86400, event_at=NOW - 10 * 86400,
                         topic="面试", sentiment="neutral", anchor_text="x",
                         source_text="y", confidence=0.85)
    rid = s.add_commitment(old, contact_key="tg:u1")
    assert rid
    n = s.expire_overdue(now=NOW, grace_days=1.0)
    assert n == 1
    assert s.count(status="expired") == 1 and s.count(status="pending") == 0


def test_expire_keeps_recent_pending():
    s = _store()
    s.add_commitment(_commit(due_offset_days=1.0), contact_key="tg:u1")
    # 未到期项不应被 expire
    assert s.expire_overdue(now=NOW, grace_days=1.0) == 0
    assert s.count(status="pending") == 1


def test_bring_forward_makes_due():
    s = _store()
    rid = s.add_commitment(_commit(due_offset_days=3.0), contact_key="tg:u1")
    # 提前前：3 天后到期，now 时不 due
    assert len(s.list_due(now=NOW)) == 0
    assert s.bring_forward(rid, now=NOW) is True
    assert len(s.list_due(now=NOW)) == 1


def test_bring_forward_only_pending():
    s = _store()
    rid = s.add_commitment(_commit(), contact_key="tg:u1")
    s.cancel(rid)
    # 非 pending → 不可提前
    assert s.bring_forward(rid, now=NOW) is False


def test_list_by_contact_and_count_pending():
    s = _store()
    s.add_commitment(_commit(topic="面试"), contact_key="tg:u1")
    s.add_commitment(_commit(topic="复查"), contact_key="tg:u1")
    s.add_commitment(_commit(topic="生日"), contact_key="tg:u2")
    assert len(s.list_by_contact("tg:u1")) == 2
    assert len(s.list_by_contact("tg:u2")) == 1
    assert len(s.list_by_contact("tg:nope")) == 0
    assert s.count_pending_by_contact("tg:u1") == 2
    assert s.count_pending_by_contact("tg:u2") == 1
    # 取消一条后 pending 计数下降，list_by_contact(status=pending) 也下降
    one = s.list_by_contact("tg:u1")[0]
    s.cancel(one["id"])
    assert s.count_pending_by_contact("tg:u1") == 1
    assert len(s.list_by_contact("tg:u1", status="pending")) == 1
    assert len(s.list_by_contact("tg:u1")) == 2  # 全状态仍 2


def test_pending_counts_by_contacts_batch():
    s = _store()
    s.add_commitment(_commit(topic="面试"), contact_key="tg:u1")
    s.add_commitment(_commit(topic="复查"), contact_key="tg:u1")
    s.add_commitment(_commit(topic="生日"), contact_key="tg:u2")
    counts = s.pending_counts_by_contacts(["tg:u1", "tg:u2", "tg:u3"])
    assert counts == {"tg:u1": 2, "tg:u2": 1}  # u3 无 pending → 不出现
    assert s.pending_counts_by_contacts([]) == {}
