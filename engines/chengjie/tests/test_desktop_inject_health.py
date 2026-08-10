"""桌面壳注入健康信标（D1b）后端模块单测。

覆盖 src/web/desktop_inject_health.py：分类纯函数（与渲染层 deriveInjectState 同口径）、
最新态存储（每账号去重/覆盖、ts 倒序、stale 标注、概览计数、软上限）。
"""

from __future__ import annotations

import time

from src.web.desktop_inject_health import (
    InjectHealthStore,
    MISMATCH_STATUSES,
    SELECTOR_KEYS,
    classify_inject_health,
    missing_selectors,
    selector_failure_breakdown,
)


# ── P9 逐选择器失配诊断（selector_failure_breakdown）─────────────────────────────
def test_selector_breakdown_empty():
    assert selector_failure_breakdown([]) == []
    assert selector_failure_breakdown(None) == []


def test_selector_breakdown_counts_and_sorted():
    alerts = [
        {"selectors": {"composer": True, "sendBtn": False, "bubble": False, "peerTitle": True}},
        {"selectors": {"composer": False, "sendBtn": False, "bubble": True, "peerTitle": True}},
        {"selectors": {"composer": True, "sendBtn": False, "bubble": True, "peerTitle": True}},
    ]
    out = selector_failure_breakdown(alerts)
    # sendBtn 抓空 3 次最多 → 置首；composer/bubble 各 1 次
    assert out[0] == {"key": "sendBtn", "missing": 3, "advisory": True}
    keys = {d["key"]: d["missing"] for d in out}
    assert keys == {"sendBtn": 3, "composer": 1, "bubble": 1}
    # advisory 只标注「抓空未必是故障」，不参与排序：影响面仍是唯一主序。
    assert [d["advisory"] for d in out] == [True, False, False]


def test_selector_breakdown_ignores_missing_and_nondict():
    # 缺字段（None）不计；非 dict 行跳过；只有明确 False 才算抓空
    alerts = [
        {"selectors": {"composer": False}},  # 仅 composer False
        {"selectors": None},
        "garbage",
        {"no_selectors": 1},
    ]
    assert selector_failure_breakdown(alerts) == [
        {"key": "composer", "missing": 1, "advisory": False}]


# ── 可信失效清单：弃用与硬事实矛盾的整表 ────────────────────────────────────
def test_missing_selectors_reads_explicit_false_only():
    from src.web.desktop_inject_health import missing_selectors
    assert missing_selectors({"selectors": {"composer": False}}) == ["composer"]
    assert missing_selectors({"selectors": None}) == []
    assert missing_selectors({}) == []


def test_missing_selectors_drops_self_contradicting_table():
    """入库把缺失键补成 False → 「没上报这张表」与「四个全坏」同形。

    数到了气泡还说 bubble 抓空，就说明那是补出来的默认值：整表弃用。宁可不给线索，
    不给错线索——照错线索去改一个没坏的选择器比没有线索更浪费人。
    """
    from src.web.desktop_inject_health import missing_selectors
    filled = {"composer": False, "sendBtn": False,
              "bubble": False, "peerTitle": False}
    assert missing_selectors({"bubbles": 7, "selectors": filled}) == []
    assert missing_selectors({"composer": True, "selectors": filled}) == []
    # 硬事实与表一致（页面真的整体对不上）→ 照实列出
    assert missing_selectors({"bubbles": 0, "selectors": filled}) == list(SELECTOR_KEYS)


def test_breakdown_ignores_contradicting_tables():
    """提取器类失配的账号（元素都在）不得在每个选择器键上各投一票。"""
    alerts = [{"status": "mismatch_text", "bubbles": 9,
               "selectors": {"composer": False, "sendBtn": False,
                             "bubble": False, "peerTitle": False}}]
    assert selector_failure_breakdown(alerts) == [
        {"key": "bubbleText", "missing": 1, "advisory": False}]


def test_classify_ok():
    assert classify_inject_health(
        {"supported": True, "composer": True, "bubbles": 5, "chatOpen": True}
    ) == "ok"


def test_classify_unsupported():
    assert classify_inject_health({"supported": False}) == "unsupported"


def test_classify_no_chat():
    assert classify_inject_health(
        {"supported": True, "composer": False, "bubbles": 0, "chatOpen": False}
    ) == "no_chat"


def test_classify_mismatch_composer():
    # 会话已开（有气泡）但抓不到输入框 → 输入框选择器失配
    assert classify_inject_health(
        {"supported": True, "composer": False, "bubbles": 3, "chatOpen": True}
    ) == "mismatch_composer"


def test_classify_mismatch_bubble():
    # 会话已开 + 有输入框，但抓不到任何气泡 → 气泡选择器失配
    assert classify_inject_health(
        {"supported": True, "composer": True, "bubbles": 0, "chatOpen": True}
    ) == "mismatch_bubble"


def test_classify_empty_record():
    assert classify_inject_health({}) == "no_chat"


# ── 提取器失效（元素在、内容抓不出）───────────────────────────────────────────────
# 官方改版的常见形态是内层结构微调：bubble 照样命中而 bubbleText/mid 提取全空 →
# 翻译按钮一个都不出现、消息一条都不回流，而只数元素存在性的旧判据会报 ok。
# 这批用例里**不该告警**的边界比该告警的更重要：误报一次运维就再也不信这盏灯了。
def _ex_rec(**kw):
    base = {"supported": True, "composer": True, "bubbles": 5, "chatOpen": True}
    base.update(kw)
    return base


def test_classify_mismatch_text_when_nothing_decorated():
    # 抓到 5 条气泡，逐条都试过、一条都没提出正文/媒体 → 正文提取器塌了
    assert classify_inject_health(_ex_rec(
        extract={"decorated": 0, "unresolved": 5})) == "mismatch_text"


def test_classify_mismatch_ingest_when_no_key_resolved():
    # 正文提取正常（有装饰），但本轮要回流的 4 条全拿不到 (mid, peerId) → 同步静默中断
    assert classify_inject_health(_ex_rec(
        extract={"decorated": 5, "unresolved": 0,
                 "ingest_tried": 4, "ingest_keyed": 0})) == "mismatch_ingest"


def test_classify_text_failure_outranks_ingest():
    # 正文提取塌了必然连带 ingest 拿不到内容 → 报根因，别让运营去校准 mid
    assert classify_inject_health(_ex_rec(
        extract={"decorated": 0, "unresolved": 5,
                 "ingest_tried": 5, "ingest_keyed": 0})) == "mismatch_text"


def test_classify_accepts_camel_case_extract():
    # 上报侧是 camelCase；本函数既吃原始上报也吃库内规范记录
    assert classify_inject_health(_ex_rec(
        extract={"decorated": 5, "ingestTried": 3, "ingestKeyed": 0})) == "mismatch_ingest"


def test_classify_ok_without_extract_field():
    # 旧版桌面壳不上报 extract → 绝不能因为「老客户端没这字段」就报失配
    assert classify_inject_health(_ex_rec()) == "ok"
    assert classify_inject_health(_ex_rec(extract=None)) == "ok"
    assert classify_inject_health(_ex_rec(extract="garbage")) == "ok"


def test_classify_ok_when_steady_state_has_nothing_to_ingest():
    # 长会话稳定态：全都推过了 → tried=keyed=0。分母为 0 必须判 ok，
    # 否则「一切正常」会被误判成「键提取全废」——这是本判据最容易写错的地方。
    assert classify_inject_health(_ex_rec(
        extract={"decorated": 200, "unresolved": 0,
                 "ingest_tried": 0, "ingest_keyed": 0})) == "ok"


def test_classify_ok_when_only_some_bubbles_unresolved():
    # 混合会话（贴纸/通话记录等本就提不出内容）：只要有一条提取成功就说明提取器活着
    assert classify_inject_health(_ex_rec(
        extract={"decorated": 4, "unresolved": 1})) == "ok"


def test_classify_ok_when_partially_keyed():
    # 部分拿到键 → 提取器活着（个别气泡没 mid 是常态，如日期分隔/系统提示）
    assert classify_inject_health(_ex_rec(
        extract={"decorated": 5, "ingest_tried": 4, "ingest_keyed": 1})) == "ok"


def test_classify_extract_not_consulted_when_no_bubbles():
    # 空会话（bubbles=0）先被 mismatch_bubble 接住，不会因 unresolved=0 误入 extract 档
    assert classify_inject_health(_ex_rec(
        bubbles=0, extract={"decorated": 0, "unresolved": 0})) == "mismatch_bubble"


def test_extract_statuses_are_alertable():
    # 新状态必须进失配集，否则持续告警/概览计数看不见它们（建了灯却不亮）
    assert "mismatch_text" in MISMATCH_STATUSES
    assert "mismatch_ingest" in MISMATCH_STATUSES


def test_store_normalizes_and_classifies_extract():
    store = InjectHealthStore()
    out = store.record({"platform": "telegram", "account_id": "tg1", "supported": True,
                        "composer": True, "bubbles": 9, "chatOpen": True,
                        "extract": {"decorated": 0, "unresolved": 9,
                                    "ingestTried": 9, "ingestKeyed": 0}})
    assert out["status"] == "mismatch_text"
    # 库内统一 snake_case，camelCase 上报被归一
    assert out["extract"] == {"decorated": 0, "unresolved": 9,
                              "ingest_tried": 9, "ingest_keyed": 0}
    assert store.persistent_mismatches(0.0)[0]["account_id"] == "tg1"


def test_breakdown_attributes_extract_failures_by_status():
    # 提取器失效在 selectors 布尔表里全绿（元素确实在），只能按 status 反推该校准哪个字段
    alerts = [
        {"status": "mismatch_text",
         "selectors": {"composer": True, "sendBtn": True, "bubble": True, "peerTitle": True}},
        {"status": "mismatch_text",
         "selectors": {"composer": True, "sendBtn": True, "bubble": True, "peerTitle": True}},
        {"status": "mismatch_ingest",
         "selectors": {"composer": True, "sendBtn": True, "bubble": True, "peerTitle": True}},
    ]
    out = selector_failure_breakdown(alerts)
    assert out == [{"key": "bubbleText", "missing": 2, "advisory": False},
                   {"key": "mid", "missing": 1, "advisory": False}]


def test_record_dedups_by_account():
    store = InjectHealthStore()
    store.record({"platform": "instagram", "account_id": "ig1", "supported": True,
                  "composer": True, "bubbles": 2, "chatOpen": True})
    store.record({"platform": "instagram", "account_id": "ig1", "supported": True,
                  "composer": False, "bubbles": 2, "chatOpen": True})
    rows = store.latest()
    assert len(rows) == 1
    assert rows[0]["status"] == "mismatch_composer"  # 最新一条覆盖


def test_record_multiple_accounts_sorted_desc():
    store = InjectHealthStore()
    store.record({"platform": "x", "account_id": "a", "supported": True,
                  "composer": True, "bubbles": 1, "chatOpen": True, "ts": 100})
    store.record({"platform": "x", "account_id": "b", "supported": True,
                  "composer": True, "bubbles": 1, "chatOpen": True, "ts": 200})
    rows = store.latest()
    assert [r["account_id"] for r in rows] == ["b", "a"]  # ts 倒序


def test_no_key_not_stored_but_classified():
    store = InjectHealthStore()
    rec = store.record({"supported": True, "composer": True, "bubbles": 1, "chatOpen": True})
    assert rec["status"] == "ok"
    assert store.latest() == []  # 无主键不入库


def test_stale_flag():
    store = InjectHealthStore()
    store.record({"platform": "zalo", "account_id": "z1", "supported": True,
                  "composer": True, "bubbles": 1, "chatOpen": True, "ts": time.time() - 200})
    rows = store.latest(stale_after=90.0)
    assert rows[0]["stale"] is True
    rows2 = store.latest(stale_after=None)
    assert "stale" not in rows2[0]


def test_summary_counts():
    store = InjectHealthStore()
    store.record({"platform": "instagram", "account_id": "1", "supported": True,
                  "composer": True, "bubbles": 1, "chatOpen": True})  # ok
    store.record({"platform": "instagram", "account_id": "2", "supported": True,
                  "composer": False, "bubbles": 1, "chatOpen": True})  # mismatch_composer
    store.record({"platform": "x", "account_id": "3", "supported": True,
                  "composer": True, "bubbles": 0, "chatOpen": True})  # mismatch_bubble
    s = store.summary()
    assert s["total"] == 3
    assert s["mismatch"] == 2
    assert s["ok"] == 1


def test_selectors_normalized():
    store = InjectHealthStore()
    rec = store.record({"platform": "messenger", "account_id": "m1", "supported": True,
                        "composer": True, "bubbles": 1, "chatOpen": True,
                        "selectors": {"bubble": True, "composer": True}})
    assert rec["selectors"] == {"bubble": True, "composer": True, "sendBtn": False, "peerTitle": False}


def test_soft_cap_evicts_oldest():
    store = InjectHealthStore(cap=2)
    store.record({"platform": "p", "account_id": "old", "supported": True,
                  "composer": True, "bubbles": 1, "chatOpen": True, "ts": 1})
    store.record({"platform": "p", "account_id": "mid", "supported": True,
                  "composer": True, "bubbles": 1, "chatOpen": True, "ts": 2})
    store.record({"platform": "p", "account_id": "new", "supported": True,
                  "composer": True, "bubbles": 1, "chatOpen": True, "ts": 3})
    ids = {r["account_id"] for r in store.latest()}
    assert "old" not in ids and len(ids) == 2


def test_mismatch_statuses_constant():
    assert "mismatch_composer" in MISMATCH_STATUSES
    assert "mismatch_bubble" in MISMATCH_STATUSES


# ── #4 失配持续告警升级：跃迁追踪 / 持续时长 / 历史 ──────────────────────────
def _rec(store, status_kwargs, ts):
    base = {"platform": "instagram", "account_id": "ig1", "supported": True}
    base.update(status_kwargs)
    base["ts"] = ts
    return store.record(base)


def test_mismatch_since_set_on_entry_and_persists_across_substatus():
    store = InjectHealthStore()
    _rec(store, {"composer": True, "bubbles": 1, "chatOpen": True}, 100)   # ok
    r1 = _rec(store, {"composer": False, "bubbles": 1, "chatOpen": True}, 110)  # mismatch_composer
    assert r1["mismatch_since"] == 110
    # 切到另一种失配子状态（composer→bubble）→ mismatch_since 不重置（持续在失配）
    r2 = _rec(store, {"composer": True, "bubbles": 0, "chatOpen": True}, 150)  # mismatch_bubble
    assert r2["mismatch_since"] == 110


def test_mismatch_since_cleared_on_recovery():
    store = InjectHealthStore()
    _rec(store, {"composer": False, "bubbles": 1, "chatOpen": True}, 100)  # mismatch
    r = _rec(store, {"composer": True, "bubbles": 1, "chatOpen": True}, 120)  # ok 恢复
    assert r["mismatch_since"] is None
    # 再次失配 → 重新计起点（非沿用旧的）
    r2 = _rec(store, {"composer": False, "bubbles": 1, "chatOpen": True}, 200)
    assert r2["mismatch_since"] == 200


def test_latest_reports_mismatch_secs():
    store = InjectHealthStore()
    _rec(store, {"composer": False, "bubbles": 1, "chatOpen": True}, 1000)  # mismatch@1000
    rows = store.latest(now=1180)
    assert rows[0]["mismatch_secs"] == 180


def test_persistent_mismatches_threshold():
    store = InjectHealthStore()
    _rec(store, {"composer": False, "bubbles": 1, "chatOpen": True}, 1000)  # mismatch@1000
    # 阈值 300s：t=1200 时未到 → 空；t=1400 时已过 → 命中
    assert store.persistent_mismatches(300, now=1200) == []
    hit = store.persistent_mismatches(300, now=1400)
    assert len(hit) == 1 and hit[0]["account_id"] == "ig1"
    assert hit[0]["mismatch_secs"] == 400


def test_summary_persistent_count():
    store = InjectHealthStore()
    _rec(store, {"composer": False, "bubbles": 1, "chatOpen": True}, 1000)
    s = store.summary(persist_sec=300, now=1400)
    assert s["persistent_mismatch"] == 1
    # 未给 persist_sec → 不含该键（向后兼容）
    assert "persistent_mismatch" not in store.summary()


def test_recovery_excludes_from_persistent():
    store = InjectHealthStore()
    _rec(store, {"composer": False, "bubbles": 1, "chatOpen": True}, 1000)
    _rec(store, {"composer": True, "bubbles": 1, "chatOpen": True}, 1500)  # 恢复
    assert store.persistent_mismatches(300, now=2000) == []


def test_recent_events_records_transitions():
    store = InjectHealthStore()
    _rec(store, {"composer": True, "bubbles": 1, "chatOpen": True}, 100)   # ok（首次）
    _rec(store, {"composer": True, "bubbles": 1, "chatOpen": True}, 110)   # ok（不变→不记）
    _rec(store, {"composer": False, "bubbles": 1, "chatOpen": True}, 120)  # →mismatch_composer
    _rec(store, {"composer": True, "bubbles": 1, "chatOpen": True}, 130)   # →ok
    evs = store.recent_events()
    # 最新在前：ok(130) ← mismatch(120) ← ok(100)；中间不变那条不记
    assert [e["status"] for e in evs] == ["ok", "mismatch_composer", "ok"]
    assert evs[0]["from"] == "mismatch_composer"


# ── 告警出口：due_reminders 节流 / 陈旧防线 / 恢复清零 ─────────────────────────
def _mm(store, ts, *, platform="instagram", acct="ig1", composer=False):
    """记一条失配（或正常）上报。"""
    return store.record({"platform": platform, "account_id": acct,
                         "supported": True, "composer": composer,
                         "bubbles": 1, "chatOpen": True, "ts": ts})


def test_due_reminders_respects_min_age():
    store = InjectHealthStore()
    _mm(store, 1000)
    # 心跳持续上报（ts 在动，mismatch_since 不动）——未到首提龄不该出
    _mm(store, 1100)
    assert store.due_reminders(min_age_sec=300, interval_sec=3600, now=1100) == {}
    _mm(store, 1400)
    due = store.due_reminders(min_age_sec=300, interval_sec=3600, now=1400)
    assert len(due) == 1
    row = next(iter(due.values()))
    assert row["mismatch_secs"] == 400 and row["first_reminder"] is True


def test_due_reminders_throttles_until_interval():
    store = InjectHealthStore()
    _mm(store, 1000)
    _mm(store, 1400)
    assert store.due_reminders(min_age_sec=300, interval_sec=3600, now=1400)
    # 心跳每 30s 一条新记录：若节流戳没被 record 继承，这里每次都会重发
    for ts in (1430, 1460, 1490):
        _mm(store, ts)
        assert store.due_reminders(min_age_sec=300, interval_sec=3600, now=ts) == {}
    # 过了重提间隔 → 再出一条，且不再算首提
    _mm(store, 5100)
    again = store.due_reminders(min_age_sec=300, interval_sec=3600, now=5100)
    assert len(again) == 1
    assert next(iter(again.values()))["first_reminder"] is False


def test_due_reminders_skips_stale_reports():
    """壳已关/账号已卸：最后那条失配记录永远留在库里，催人修一个没在跑的 webview。"""
    store = InjectHealthStore()
    _mm(store, 1000)
    assert store.due_reminders(min_age_sec=300, interval_sec=3600,
                              stale_after=120, now=2000) == {}
    # 心跳恢复（有人真在用）→ 立刻可催
    _mm(store, 2000)
    assert store.due_reminders(min_age_sec=300, interval_sec=3600,
                              stale_after=120, now=2000)


def test_due_reminders_clears_after_recovery():
    store = InjectHealthStore()
    _mm(store, 1000)
    _mm(store, 1400)
    store.due_reminders(min_age_sec=300, interval_sec=3600, now=1400)
    _mm(store, 1500, composer=True)          # 恢复
    _mm(store, 2000)                          # 再次坏
    # 恢复清零 → 重新走首提计时（2000 起算，min_age 300 未到）
    assert store.due_reminders(min_age_sec=300, interval_sec=3600, now=2100) == {}
    _mm(store, 2400)
    due = store.due_reminders(min_age_sec=300, interval_sec=3600, now=2400)
    assert next(iter(due.values()))["first_reminder"] is True


def test_due_reminders_per_account_independent():
    store = InjectHealthStore()
    _mm(store, 1000, acct="ig1")
    _mm(store, 1000, acct="ig2")
    _mm(store, 1400, acct="ig1")
    _mm(store, 1400, acct="ig2")
    due = store.due_reminders(min_age_sec=300, interval_sec=3600, now=1400)
    assert len(due) == 2
    # 取走后各自独立节流，互不牵连
    _mm(store, 1430, acct="ig1")
    assert store.due_reminders(min_age_sec=300, interval_sec=3600, now=1430) == {}
