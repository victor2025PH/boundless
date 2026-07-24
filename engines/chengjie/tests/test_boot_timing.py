"""Phase 11：启动分阶段计时器 BootTimer 门禁。"""
from src.bootstrap.boot_timing import BootTimer


class _FakeClock:
    """可推进的假单调时钟。"""

    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t

    def advance(self, sec):
        self.t += sec


def test_mark_records_delta_and_cumulative():
    clk = _FakeClock()
    t = BootTimer(clock=clk)

    clk.advance(0.5)
    d1 = t.mark("config")
    clk.advance(2.0)
    d2 = t.mark("ai_client")
    clk.advance(8.0)
    d3 = t.mark("skill_manager")

    assert round(d1, 3) == 0.5
    assert round(d2, 3) == 2.0
    assert round(d3, 3) == 8.0

    phases = t.phases()
    assert [p[0] for p in phases] == ["config", "ai_client", "skill_manager"]
    # cumulative 单调递增
    cums = [p[2] for p in phases]
    assert cums == sorted(cums)
    assert round(cums[-1], 3) == 10.5


def test_slowest_and_summary():
    clk = _FakeClock()
    t = BootTimer(clock=clk)
    clk.advance(0.1)
    t.mark("config")
    clk.advance(8.1)
    t.mark("skill_manager")
    clk.advance(2.3)
    t.mark("web_app")

    assert t.slowest() == ("skill_manager", 8.1)

    s = t.summary()
    assert s["slowest_phase"] == "skill_manager"
    assert round(s["slowest_sec"], 1) == 8.1
    assert round(s["total_sec"], 1) == 10.5
    assert len(s["phases"]) == 3
    assert s["phases"][0]["name"] == "config"
    assert "cumulative_sec" in s["phases"][0]


def test_format_line_shape():
    clk = _FakeClock()
    t = BootTimer(clock=clk)
    clk.advance(0.1)
    t.mark("config")
    clk.advance(8.1)
    t.mark("skill_manager")

    line = t.format_line()
    assert line.startswith("boot phases:")
    assert "config=0.1s" in line
    assert "skill_manager=8.1s" in line
    assert "total=" in line
    assert "slowest=skill_manager" in line


def test_empty_timer_is_safe():
    clk = _FakeClock()
    t = BootTimer(clock=clk)
    assert t.slowest() == ("", 0.0)
    s = t.summary()
    assert s["phases"] == []
    assert s["slowest_phase"] == ""
    # format_line 不得抛
    assert t.format_line().startswith("boot phases:")


def test_bad_clock_never_raises():
    def _boom():
        raise RuntimeError("clock exploded")

    # 构造期时钟坏 → 让 mark/total 走异常分支而非崩溃
    t = BootTimer(clock=lambda: 0.0)
    t._clock = _boom  # 事后替换成坏钟，模拟运行期时钟异常
    # mark / total 都必须吞异常返回 0，绝不冒泡到启动流程
    assert t.mark("x") == 0.0
    assert t.total == 0.0
