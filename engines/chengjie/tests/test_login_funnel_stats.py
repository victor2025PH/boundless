"""扫码登录漏斗观测门禁。

核心断言是 ``stalled``：它就是这次 LINE 事故本该被自动喊出来的那个信号——
发起了若干次、一次都没成，等于该平台该方式的登录链路事实上不可用。
"""

from __future__ import annotations

from src.integrations.login_funnel_stats import (
    LoginFunnelStats, record_login_stage, get_login_funnel_stats,
)


def _row(stats: LoginFunnelStats, key: str) -> dict:
    return next(r for r in stats.dump()["rows"] if r["key"] == key)


def test_funnel_counts_each_stage():
    s = LoginFunnelStats()
    s.record("line", "protocol", "started")
    s.record("line", "protocol", "qr_shown")
    s.record("line", "protocol", "pin_issued")
    s.record("line", "protocol", "authorized", elapsed_ms=4200)
    r = _row(s, "line:protocol")
    assert (r["started"], r["qr_shown"], r["pin_issued"], r["authorized"]) == (1, 1, 1, 1)
    assert r["avg_authorized_ms"] == 4200


def test_stalled_flags_the_line_incident_shape():
    """PIN 发出去了却一次都没登上——事故当天的真实形态，看板必须自己变红。"""
    s = LoginFunnelStats()
    for _ in range(4):
        s.record("line", "protocol", "started")
        s.record("line", "protocol", "qr_shown")
        s.record("line", "protocol", "pin_issued")
        s.record("line", "protocol", "failed", reason_code="qr_expired")
    r = _row(s, "line:protocol")
    assert r["stalled"] is True
    assert r["pin_issued"] == 4 and r["authorized"] == 0
    assert r["reasons"] == {"qr_expired": 4}


def test_healthy_funnel_not_stalled():
    s = LoginFunnelStats()
    for _ in range(4):
        s.record("tg", "protocol", "started")
        s.record("tg", "protocol", "authorized", elapsed_ms=1000)
    assert _row(s, "tg:protocol")["stalled"] is False


def test_regression_after_past_success_is_stalled():
    """先能用、后彻底坏＝回归。累计口径（authorized>0）会漏掉它，而它比「从没通过」
    更隐蔽——所有人都默认那条链路还好着。判据必须是「距上次成功以来」。"""
    s = LoginFunnelStats()
    for _ in range(9):                       # 上周好好的
        s.record("messenger", "web", "started")
        s.record("messenger", "web", "authorized", elapsed_ms=3000)
    for _ in range(5):                       # 今天全挂
        s.record("messenger", "web", "started")
        s.record("messenger", "web", "failed", reason_code="checkpoint")

    r = _row(s, "messenger:web")
    assert r["authorized"] == 9              # 累计口径看着很健康
    assert r["started_since_success"] == 5 and r["failed_since_success"] == 5
    assert r["stalled"] is True              # 但它现在确实接不上


def test_success_clears_the_since_counters():
    """成功一次即闭嘴：看板与 watchdog 共用这一清零动作，不必各自维护基线。"""
    s = LoginFunnelStats()
    for _ in range(5):
        s.record("line", "protocol", "started")
        s.record("line", "protocol", "failed", reason_code="pin_timeout")
    assert _row(s, "line:protocol")["stalled"] is True

    s.record("line", "protocol", "started")
    s.record("line", "protocol", "authorized", elapsed_ms=2000)
    r = _row(s, "line:protocol")
    assert r["stalled"] is False and r["started_since_success"] == 0
    assert r["failed"] == 5                  # 累计计数不受影响，历史仍可查


def test_few_attempts_not_flagged():
    """样本太少不下结论，避免「新装机第一次没扫成」就报警。"""
    s = LoginFunnelStats()
    s.record("line", "protocol", "started")
    s.record("line", "protocol", "failed", reason_code="network")
    assert _row(s, "line:protocol")["stalled"] is False


def test_unknown_reason_code_folded():
    """脏码不得变成高基数维度，否则 Prometheus 标签会被撑爆。"""
    s = LoginFunnelStats()
    s.record("line", "protocol", "failed", reason_code="totally-made-up")
    assert _row(s, "line:protocol")["reasons"] == {"login_failed": 1}


def test_dirty_platform_key_sanitized():
    s = LoginFunnelStats()
    s.record("Line; DROP TABLE", "proto col", "started")
    assert _row(s, "unknown:unknown")["started"] == 1


def test_distinct_key_cap():
    s = LoginFunnelStats()
    for i in range(80):
        s.record(f"p{i}", "protocol", "started")
    assert len(s.dump()["rows"]) <= 41  # _MAX_KEYS + __other__
    assert s.overflow > 0


def test_overflow_does_not_leak_side_tables():
    """超限折叠进 __other__ 后，旁挂表也必须按生效 key 落键。

    否则主表封顶、``_since`` / ``_reasons`` / ``_auth_ms_*`` 却按原始脏 key 无限
    长，正好绕过 ``_MAX_KEYS`` 想防的内存撑爆；而且那些条目在 dump 里永远读不到
    （只按 ``_funnel`` 的 key 查），是纯泄漏。
    """
    s = LoginFunnelStats()
    for i in range(200):
        s.record(f"p{i}", "protocol", "failed", reason_code="network")
        s.record(f"p{i}", "protocol", "authorized", elapsed_ms=1000)
    cap = 41  # _MAX_KEYS + __other__
    assert len(s._since) <= cap
    assert len(s._reasons) <= cap
    assert len(s._auth_ms_sum) <= cap
    assert len(s._auth_ms_n) <= cap
    # 折叠后的计数不能丢：__other__ 行照样得记到 reasons/耗时
    other = _row(s, "__other__")
    assert other["failed"] > 0 and other["avg_authorized_ms"] == 1000
    assert other["reasons"].get("network", 0) > 0


def test_unknown_stage_ignored():
    s = LoginFunnelStats()
    s.record("line", "protocol", "not_a_stage")
    assert s.dump()["rows"] == []


def test_failed_elapsed_not_averaged_in():
    """失败耗时由超时预算主导，混进均值会把它钉死在超时值上。"""
    s = LoginFunnelStats()
    s.record("line", "protocol", "failed", reason_code="pin_timeout", elapsed_ms=240000)
    s.record("line", "protocol", "authorized", elapsed_ms=3000)
    assert _row(s, "line:protocol")["avg_authorized_ms"] == 3000


def test_prom_exposition_shape():
    s = LoginFunnelStats()
    s.record("line", "protocol", "pin_issued")
    s.record("line", "protocol", "failed", reason_code="pin_timeout")
    out = s.dump_prom()
    assert 'login_funnel_total{platform="line",mode="protocol",stage="pin_issued"} 1' in out
    assert 'login_funnel_failed_total{platform="line",mode="protocol",reason="pin_timeout"} 1' in out
    assert out.endswith("\n")


def test_prom_exposes_since_success_criterion():
    """看板/watchdog/外部告警必须读同一个判据数，否则会出现「Prom 静默但看板标红」。"""
    s = LoginFunnelStats()
    for _ in range(3):
        s.record("messenger", "web", "started")
    out = s.dump_prom()
    assert ('login_funnel_since_success{platform="messenger",mode="web",'
            'stage="started"} 3') in out
    # 成功一次即清零——这正是累计 counter 判不出回归、而本指标能判的原因
    s.record("messenger", "web", "authorized", elapsed_ms=1000)
    out2 = s.dump_prom()
    assert ('login_funnel_since_success{platform="messenger",mode="web",'
            'stage="started"} 0') in out2
    assert ('login_funnel_total{platform="messenger",mode="web",'
            'stage="started"} 3') in out2  # 累计值不受影响


def test_module_entrypoint_never_raises():
    """观测绝不能影响登录本身——任何脏输入都只能被吞掉。"""
    record_login_stage(None, None, None)  # type: ignore[arg-type]
    record_login_stage("line", "protocol", "started")
    assert get_login_funnel_stats() is get_login_funnel_stats()


def test_readout_wired_into_metrics_endpoints():
    """读出端接线门禁（本次功能测试发现的缺口教训）。

    2026-07-25：模块埋点全接、纯函数门禁全绿，但 ``dump()``/``dump_prom()`` 两个读出端
    从没被任何 ``/metrics`` 路由调用——漏斗在进程里累积却对外不可见，恰好复刻了它本要
    根治的「没人在看」。静态断言 drafts_routes 同时接了 JSON 与 Prometheus 两侧，
    任一侧再被摘掉即红。
    """
    import pathlib

    src = (pathlib.Path(__file__).resolve().parents[1]
           / "src" / "web" / "routes" / "drafts_routes.py").read_text(encoding="utf-8")
    assert "get_login_funnel_stats().dump()" in src, "JSON 侧 /api/workspace/metrics 未接登录漏斗"
    assert "get_login_funnel_stats().dump_prom()" in src, "Prometheus /metrics 未接登录漏斗"
