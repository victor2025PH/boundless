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
