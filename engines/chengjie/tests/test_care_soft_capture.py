"""实施84 P0-3：软承诺捕获层（无时间锚的「下次/等我忙完」）门禁。

重点是**不该捕**的路径（宁缺毋滥）：问句、敷衍语、短文本、带时间锚的句子
（归硬约定层）；以及捕获接线的配置闸（默认关）与 per-contact pending 上限。
"""
from datetime import datetime
from types import SimpleNamespace

from src.contacts.care_capture import make_care_inbound_cb
from src.contacts.care_commitment import (
    CareCommitment,
    extract_commitments,
    extract_soft_commitments,
)
from src.contacts.care_schedule import CareScheduleStore

NOW = datetime(2026, 6, 17, 10, 0, 0).timestamp()


# ── 纯函数 ──────────────────────────────────────────────────────────────────
def test_soft_basic_capture():
    out = extract_soft_commitments("等我忙完这阵就去找你玩", now=NOW)
    assert len(out) == 1
    c = out[0]
    assert c.anchor_text.startswith("soft:")
    assert c.confidence == 0.55           # 无主题词 → 摘要兜底档
    assert abs(c.due_at - (NOW + 72 * 3600)) < 5


def test_soft_topic_lexicon_raises_confidence():
    out = extract_soft_commitments("下次陪你一起去旅行", now=NOW)
    assert len(out) == 1
    assert out[0].topic == "旅行"
    assert out[0].confidence == 0.65


def test_soft_due_hours_configurable():
    out = extract_soft_commitments("回头给你看照片哈", now=NOW, due_hours=24)
    assert len(out) == 1
    assert abs(out[0].due_at - (NOW + 24 * 3600)) < 5


def test_soft_question_not_captured():
    assert extract_soft_commitments("下次什么时候来看我？", now=NOW) == []
    assert extract_soft_commitments("你回头有空吗?", now=NOW) == []


def test_soft_brushoff_not_captured():
    assert extract_soft_commitments("行吧，下次再说吧", now=NOW) == []
    assert extract_soft_commitments("好的好的下次一定", now=NOW) == []


def test_soft_short_text_not_captured():
    assert extract_soft_commitments("下次哈", now=NOW) == []


def test_soft_yields_to_hard_anchor():
    """句中有可解析时间锚 → 软层让位（硬层已产出，绝不双捕）。"""
    text = "明天考完试，回头给你打电话"
    assert len(extract_commitments(text, now=NOW)) == 1   # 硬层接住
    assert extract_soft_commitments(text, now=NOW) == []  # 软层让位


def test_soft_no_marker_not_captured():
    assert extract_soft_commitments("今天好累啊不想动", now=NOW) == []


# ── 捕获接线（配置闸 + 上限）────────────────────────────────────────────────
def _cm(soft_enabled=True, max_pending=2):
    return SimpleNamespace(config={
        "companion": {"proactive_care": {
            "enabled": True, "capture": True,
            "capture_soft": {
                "enabled": soft_enabled,
                "due_hours": 72,
                "min_confidence": 0.55,
                "max_pending_per_contact": max_pending,
            },
        }},
    })


def _conv(ck="tg:u1"):
    return {"conversation_id": ck, "platform": "telegram",
            "account_id": "default", "chat_key": "u1",
            "chat_type": "private"}


def test_capture_soft_lands_row_when_enabled():
    s = CareScheduleStore(":memory:")
    cb = make_care_inbound_cb(s, _cm())
    cb(_conv(), "等我忙完这阵就去找你玩")
    rows = s.list_pending()
    assert len(rows) == 1
    assert float(rows[0]["confidence"]) == 0.55


def test_capture_soft_disabled_by_default_config_shape():
    s = CareScheduleStore(":memory:")
    cb = make_care_inbound_cb(s, _cm(soft_enabled=False))
    cb(_conv(), "等我忙完这阵就去找你玩")
    assert s.count(status="pending") == 0


def test_capture_soft_respects_max_pending_guard():
    s = CareScheduleStore(":memory:")
    # 预置 2 条 pending（达到上限）
    for i in range(2):
        s.add_commitment(
            CareCommitment(due_at=NOW + 3600 * (i + 1),
                           event_at=NOW + 3600 * (i + 1), topic=f"t{i}",
                           sentiment="neutral", anchor_text="x",
                           source_text="y", confidence=0.9),
            contact_key="tg:u1", platform="telegram", chat_key="u1",
            min_confidence=0.0, dedup_window_days=0.0)
    cb = make_care_inbound_cb(s, _cm(max_pending=2))
    cb(_conv(), "等我忙完这阵就去找你玩")
    assert s.count(status="pending") == 2  # 上限拦下，软捕获未入


def test_capture_hard_anchor_still_wins_over_soft():
    s = CareScheduleStore(":memory:")
    cb = make_care_inbound_cb(s, _cm())
    cb(_conv(), "我明天面试，等我考完再约你")
    rows = s.list_pending()
    assert len(rows) == 1
    assert rows[0]["topic"] == "面试"  # 硬层产物（软层让位）
