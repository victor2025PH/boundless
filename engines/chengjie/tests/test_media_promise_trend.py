"""Phase22c：出站媒体承诺兑现率按日落库（MediaPromiseTrendStore + choke-point 钩子）。"""
from __future__ import annotations

import pytest

from src.inbox import media_promise_trend_store as mpts


@pytest.fixture(autouse=True)
def _reset_store():
    mpts.reset_media_promise_trend_store()
    yield
    mpts.reset_media_promise_trend_store()


def test_add_and_daily_fulfill_rate():
    st = mpts.MediaPromiseTrendStore(":memory:")
    t = 1_000_000.0
    st.add(detected=5, fulfilled=3, retracted=1, now=t)
    st.add(fulfilled=1, retracted=1, now=t)          # 同日累加
    rows = st.daily(days=1, now=t)
    assert len(rows) == 1
    r = rows[0]
    assert r["detected"] == 5 and r["fulfilled"] == 4 and r["retracted"] == 2
    # 兑现率 = 4/(4+2) = 0.6667
    assert r["fulfill_rate"] == pytest.approx(0.6667, abs=1e-3)


def test_daily_backfills_zero_and_none_rate():
    st = mpts.MediaPromiseTrendStore(":memory:")
    t = 2_000_000.0
    st.add(detected=1, now=t)                          # 只有 detected，无兑现/落空
    rows = st.daily(days=3, now=t)
    assert len(rows) == 3
    assert rows[0]["detected"] == 0 and rows[0]["fulfill_rate"] is None  # 补零天
    assert rows[-1]["detected"] == 1
    assert rows[-1]["fulfill_rate"] is None            # 无结果 → None（前端断点，不画 0）


def test_prune_old_days():
    st = mpts.MediaPromiseTrendStore(":memory:")
    now = 1_700_000_000.0                               # 2023（Win 不支持负时间戳）
    st.add(detected=1, now=now - 100 * 86400)          # 100 天前
    st.add(detected=1, now=now)
    removed = st.prune(retention_days=90, now=now)
    assert removed == 1


def test_disabled_record_is_noop():
    # 未 configure → record 恒 no-op，get 返 None
    assert mpts.get_media_promise_trend_store() is None
    mpts.record_media_promise_trend("detected")        # 不抛
    assert mpts.get_media_promise_trend_store() is None


def test_configure_and_choke_point_hook():
    """image_autosend.record_promise_event 单一 choke point → 趋势库自动累加。"""
    from src.inbox import image_autosend as ia
    mpts.configure_media_promise_trend_store(enabled=True, db_path=":memory:")
    ia.record_promise_event("detected")
    ia.record_promise_event("fulfilled")
    ia.record_promise_event("fulfilled_async")          # 也归 fulfilled
    ia.record_promise_event("retracted")
    ia.record_promise_event("fulfill_failed")           # 也归 retracted
    ia.record_promise_event("offer_accept")
    ia.record_promise_event("fulfill_scheduled")        # 中间态 → 不入账
    st = mpts.get_media_promise_trend_store()
    rows = st.daily(days=1)
    r = rows[0]
    assert r["detected"] == 1
    assert r["fulfilled"] == 2                           # fulfilled + fulfilled_async
    assert r["retracted"] == 2                           # retracted + fulfill_failed
    assert r["offer_accept"] == 1


def test_unknown_event_ignored():
    mpts.configure_media_promise_trend_store(enabled=True, db_path=":memory:")
    mpts.record_media_promise_trend("bogus_event")
    st = mpts.get_media_promise_trend_store()
    rows = st.daily(days=1)
    assert rows[0]["detected"] == 0 and rows[0]["fulfilled"] == 0
