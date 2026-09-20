# -*- coding: utf-8 -*-
"""坐席级字符用量账本（agent_char_usage）门禁。

钉住的不变量：
- 按 (username, 日, 类目) 增量聚合；月/日/单人三种读数口径一致；
- 未知类目归并 other，绝不撑出碎行；
- agent_quota_status 四态边界（unlimited / ok / warn>=80% / over）三个消费面共用；
- 总开关默认 **关**（新子系统默认关纪律）；未登录 / 开关关 → 零记账；
- daily_series 缺数据日补零、旧→新排序。
"""
import time

import pytest

from src.utils.agent_char_usage import (
    AgentCharUsageStore,
    agent_chars_enabled,
    agent_quota_status,
    configure_agent_char_usage,
    ensure_store_for_read,
    get_agent_char_usage_store,
    record_request_chars,
    reset_agent_char_usage_store,
)

# 2026-08-16 12:00 UTC 附近的一个固定锚点（避免跨月边界抖动用相对天数推）
_ANCHOR = time.mktime((2026, 8, 16, 12, 0, 0, 0, 0, 0)) - time.timezone


@pytest.fixture(autouse=True)
def _isolated_singleton():
    reset_agent_char_usage_store()
    yield
    reset_agent_char_usage_store()


def _store() -> AgentCharUsageStore:
    return AgentCharUsageStore(":memory:")


def test_record_and_month_totals_aggregate_by_user_and_category():
    s = _store()
    s.record("zuoxi01", "tts", 100, now=_ANCHOR)
    s.record("zuoxi01", "tts", 50, now=_ANCHOR)
    s.record("zuoxi01", "translation", 30, now=_ANCHOR - 86400)
    s.record("zuoxi02", "translation", 7, now=_ANCHOR)
    totals = s.month_totals("2026-08")
    assert totals["zuoxi01"]["total"] == 180
    assert totals["zuoxi01"]["by_category"] == {"tts": 150, "translation": 30}
    assert totals["zuoxi02"]["total"] == 7
    # 上月口径互不串月
    assert s.month_totals("2026-07") == {}


def test_record_ignores_empty_user_and_nonpositive_chars():
    s = _store()
    s.record("", "tts", 100, now=_ANCHOR)
    s.record("a", "tts", 0, now=_ANCHOR)
    s.record("a", "tts", -5, now=_ANCHOR)
    assert s.month_totals("2026-08") == {}


def test_unknown_category_normalized_to_other():
    s = _store()
    s.record("a", "TTS ", 10, now=_ANCHOR)        # 大小写/空白归一
    s.record("a", "voice_xxx", 5, now=_ANCHOR)    # 未知类目 → other
    s.record("a", "", 5, now=_ANCHOR)             # 空 → other
    cats = s.month_totals("2026-08")["a"]["by_category"]
    assert cats == {"tts": 10, "other": 10}


def test_day_totals_and_usage_for_single_user_view():
    s = _store()
    s.record("a", "tts", 40, now=_ANCHOR)
    s.record("a", "translation", 10, now=_ANCHOR)
    s.record("a", "tts", 99, now=_ANCHOR - 3 * 86400)
    s.record("b", "tts", 5, now=_ANCHOR)
    day = time.strftime("%Y-%m-%d", time.gmtime(_ANCHOR))
    assert s.day_totals(day) == {"a": 50, "b": 5}
    u = s.usage_for("a", now=_ANCHOR)
    assert u["month_total"] == 149
    assert u["today_total"] == 50
    assert u["by_category"] == {"tts": 139, "translation": 10}
    # 未知用户零值不抛
    assert s.usage_for("ghost", now=_ANCHOR)["month_total"] == 0


def test_daily_series_fills_zero_days_old_to_new():
    s = _store()
    s.record("a", "tts", 20, now=_ANCHOR)
    s.record("b", "translation", 10, now=_ANCHOR - 2 * 86400)
    series = s.daily_series(4, now=_ANCHOR)
    assert len(series) == 4
    assert [d["total"] for d in series] == [0, 10, 0, 20]
    assert series[-1]["day"] == time.strftime("%Y-%m-%d", time.gmtime(_ANCHOR))
    assert series[1]["by_category"] == {"translation": 10}


def test_quota_status_levels_and_boundaries():
    assert agent_quota_status(123, 0)["level"] == "unlimited"
    assert agent_quota_status(0, -1)["level"] == "unlimited"
    assert agent_quota_status(79, 100)["level"] == "ok"
    assert agent_quota_status(80, 100)["level"] == "warn"     # 80% 边界含
    assert agent_quota_status(100, 100)["level"] == "warn"    # 用满未超 = warn
    assert agent_quota_status(101, 100)["level"] == "over"
    st = agent_quota_status(50, 200)
    assert st["pct"] == 25 and st["ratio"] == 0.25
    # 脏输入不抛
    assert agent_quota_status(None, "x")["level"] == "unlimited"


def test_enabled_flag_defaults_off():
    assert agent_chars_enabled(None) is False
    assert agent_chars_enabled({}) is False
    assert agent_chars_enabled({"usage": {}}) is False
    assert agent_chars_enabled({"usage": {"agent_chars": {}}}) is False
    assert agent_chars_enabled({"usage": {"agent_chars": {"enabled": True}}}) is True
    assert agent_chars_enabled({"usage": "garbage"}) is False


class _FakeReq:
    """最小 request 假体：session + app.state.config_manager.config。"""

    def __init__(self, username="", enabled=True):
        self.session = {"username": username} if username else {}

        class _O:  # 简易命名空间
            pass

        cm = _O()
        cm.config = {"usage": {"agent_chars": {"enabled": enabled}}}
        state = _O()
        state.config_manager = cm
        app = _O()
        app.state = state
        self.app = app


def test_record_request_chars_writes_for_logged_in_agent():
    s = _store()
    configure_agent_char_usage(store=s)
    record_request_chars(_FakeReq("zuoxi01"), "tts", 42)
    month = time.strftime("%Y-%m", time.gmtime())
    assert s.month_totals(month)["zuoxi01"]["total"] == 42


def test_record_request_chars_noop_when_disabled_or_anonymous():
    s = _store()
    configure_agent_char_usage(store=s)
    record_request_chars(_FakeReq("zuoxi01", enabled=False), "tts", 42)  # 开关关
    record_request_chars(_FakeReq(""), "tts", 42)                        # 未登录
    record_request_chars(_FakeReq("zuoxi01"), "tts", 0)                  # 零字符
    record_request_chars(object(), "tts", 42)                            # 非法 request 不抛
    month = time.strftime("%Y-%m", time.gmtime())
    assert s.month_totals(month) == {}


def test_record_named_chars_same_guards_as_request_path():
    from src.utils.agent_char_usage import record_named_chars

    s = _store()
    configure_agent_char_usage(store=s)
    on = {"usage": {"agent_chars": {"enabled": True}}}
    record_named_chars("zuoxi02", "translation", 33, config=on)
    record_named_chars("", "translation", 33, config=on)            # 空用户名
    record_named_chars("zuoxi02", "translation", 33, config={})     # 开关关
    record_named_chars("zuoxi02", "translation", -1, config=on)     # 非正数
    month = time.strftime("%Y-%m", time.gmtime())
    assert s.month_totals(month) == {
        "zuoxi02": {"total": 33, "by_category": {"translation": 33}}}


def test_ensure_store_for_read_respects_flag():
    # 开关关：不建库（沿用已注入的 store 也只读返回，不新建）
    assert ensure_store_for_read({}) is None
    s = _store()
    configure_agent_char_usage(store=s)
    assert ensure_store_for_read({}) is s  # 已有 store 就复用（面板可显历史）
    assert ensure_store_for_read({"usage": {"agent_chars": {"enabled": True}}}) is s


def test_singleton_configure_and_reset():
    assert get_agent_char_usage_store() is None
    s = _store()
    configure_agent_char_usage(store=s)
    assert get_agent_char_usage_store() is s
    reset_agent_char_usage_store()
    assert get_agent_char_usage_store() is None
