# -*- coding: utf-8 -*-
"""P3 2026-08-03：存量话术回填工具（tools/care_backfill_sent_text.py）。

覆盖：dry-run 只报告不写 / apply 只补空快照 / care 指纹不符不回填 /
队列行已清理如实 skipped / 幂等（二跑零候选）。
"""
import time
from pathlib import Path

from src.contacts.care_commitment import CareCommitment
from src.contacts.care_schedule import CareScheduleStore
from src.integrations.shared.deferred_outbox import DeferredOutboxStore


def _tool():
    import importlib.util
    p = Path(__file__).resolve().parents[1] / "tools" / "care_backfill_sent_text.py"
    spec = importlib.util.spec_from_file_location("care_backfill_sent_text", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _pending(care, *, contact, topic):
    due = time.time() - 10
    c = CareCommitment(due_at=due, event_at=due, topic=topic, sentiment="neutral",
                       anchor_text="x", source_text="s", confidence=1.0)
    return care.add_commitment(c, contact_key=contact, platform="telegram",
                               chat_key="1", min_confidence=0.0,
                               dedup_window_days=0.0)


def test_backfill_dry_run_then_apply_idempotent(tmp_path):
    cfg = tmp_path / "config"
    cfg.mkdir()
    care = CareScheduleStore(cfg / "care_schedule.db")
    dof = DeferredOutboxStore(cfg / "deferred_outbox.db")

    # ① 可回填：队列行在、指纹对上、快照为空
    s1 = _pending(care, contact="a", topic="面试")
    d1 = dof.enqueue(platform="telegram", account_id="x", chat_key="1",
                     reply_text="历史话术一", defer_until=0,
                     extra={"care": True, "care_id": s1})
    assert care.mark_sent(s1, note=f"deferred:{d1}")
    # ② 指纹不符：不回填（宁可缺，不挂错）
    s2 = _pending(care, contact="b", topic="复查")
    d2 = dof.enqueue(platform="telegram", account_id="x", chat_key="2",
                     reply_text="别人的话术", defer_until=0,
                     extra={"care": True, "care_id": 999999})
    assert care.mark_sent(s2, note=f"deferred:{d2}")
    # ③ 已有快照：不是候选、绝不覆盖
    s3 = _pending(care, contact="c", topic="体检")
    assert care.mark_sent(s3, note="deferred:777", sent_text="已有快照")
    # ④ 队列行已被保留期清理：候选但 skipped
    s4 = _pending(care, contact="d", topic="生日")
    assert care.mark_sent(s4, note="deferred:888")
    care.close()
    dof.close()

    m = _tool()
    rep = m.backfill_root(tmp_path, apply=False)
    assert rep["candidates"] == 3          # s1 + s2 + s4（s3 有快照不进候选）
    assert rep["filled"] == 1 and rep["skipped"] == 2

    # dry-run 不落盘
    care_chk = CareScheduleStore(cfg / "care_schedule.db")
    assert care_chk.get(s1)["sent_text"] == ""
    care_chk.close()

    rep2 = m.backfill_root(tmp_path, apply=True)
    assert rep2["filled"] == 1 and rep2["skipped"] == 2
    care2 = CareScheduleStore(cfg / "care_schedule.db")
    rows = {r["id"]: r for r in care2.list_history(status="sent", limit=10)}
    assert rows[s1]["sent_text"] == "历史话术一"
    assert rows[s2]["sent_text"] == ""
    assert rows[s3]["sent_text"] == "已有快照"
    assert rows[s4]["sent_text"] == ""
    care2.close()

    # 幂等：再跑一遍，s1 已有快照不再进候选
    rep3 = m.backfill_root(tmp_path, apply=True)
    assert rep3["candidates"] == 2 and rep3["filled"] == 0


def test_backfill_missing_dbs_soft_skip(tmp_path):
    m = _tool()
    rep = m.backfill_root(tmp_path, apply=True)
    assert rep.get("skip_reason") == "db_missing"
    assert rep["filled"] == 0
