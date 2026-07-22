"""ThrottleFilter 门禁：折叠三方噪声、放行业务日志、折叠计数回填。"""

import logging

from src.utils.log_throttle import ThrottleFilter, build_throttle_filter


def _rec(name, msg, args=()):
    return logging.LogRecord(name, logging.WARNING, __file__, 1, msg, args, None)


class _Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_non_matching_logger_always_passes():
    clk = _Clock()
    f = ThrottleFilter(prefixes=["pyrogram"], window_sec=60, time_fn=clk)
    for _ in range(100):
        assert f.filter(_rec("src.integrations.foo", "hello")) is True


def test_matching_logger_folds_within_window():
    clk = _Clock()
    f = ThrottleFilter(prefixes=["pyrogram.connection"], window_sec=60, time_fn=clk)
    first = _rec("pyrogram.connection.connection", "Connection timed out")
    assert f.filter(first) is True          # 首条放行
    # 窗口内后续同类全部折叠
    for _ in range(9):
        assert f.filter(_rec("pyrogram.connection.connection",
                             "Connection timed out")) is False


def test_fold_count_appended_on_next_emit():
    clk = _Clock()
    f = ThrottleFilter(prefixes=["pyrogram"], window_sec=60, time_fn=clk)
    assert f.filter(_rec("pyrogram.connection.connection", "boom")) is True
    for _ in range(5):
        f.filter(_rec("pyrogram.connection.connection", "boom"))
    clk.t = 61  # 越过窗口
    r = _rec("pyrogram.connection.connection", "boom")
    assert f.filter(r) is True
    assert "5" in r.getMessage() and "折叠" in r.getMessage()


def test_digit_templates_fold_together():
    clk = _Clock()
    f = ThrottleFilter(prefixes=["pyrogram"], window_sec=60, time_fn=clk)
    assert f.filter(_rec("pyrogram.session.session", "cycle=125 scanned=30")) is True
    # 数字不同但模板相同 → 折叠
    assert f.filter(_rec("pyrogram.session.session", "cycle=999 scanned=1")) is False


def test_distinct_templates_tracked_separately():
    clk = _Clock()
    f = ThrottleFilter(prefixes=["pyrogram"], window_sec=60, time_fn=clk)
    assert f.filter(_rec("pyrogram.connection.connection", "timed out")) is True
    # 不同模板各有独立首条放行
    assert f.filter(_rec("pyrogram.connection.connection", "failed again")) is True


def test_build_disabled_returns_none():
    assert build_throttle_filter({"throttle": {"enabled": False}}) is None


def test_build_default_prefixes():
    f = build_throttle_filter(None)
    assert f is not None
    # 默认应折叠 pyrogram，不折叠 src
    assert f._matches("pyrogram.connection.connection") is True
    assert f._matches("src.integrations.x") is False
