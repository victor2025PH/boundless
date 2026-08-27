"""LINE 接收循环退避监督器门禁（2026-08-27 06:11 宕机事故沉淀）。

okline ``stream(reconnect=True)`` 内建重连零退避——HMAC 签名桥崩坏后每秒上百轮
即抛即重试，是当日 stderr 风暴的共振源之一。修复＝worker 改一击语义
（``bot.run(reconnect=False)``）+ ``LineRecvSupervisor`` 决策重连节奏：
短命**失败**（异常）指数退避（5→10→20→…封顶 300s）；
连败达阈值放弃退出线程 → ``healthy()`` 变假 → 编排器既有监督循环接管。

**外加 09:30 回归事故的钉子**：LINE 网关的 SSE 本来就是 ~10s 发个 ``ping`` 后
正常关流等客户端重连（真机实测两个号皆然，含 token 健康的号）。首版只按存活
秒数判、门槛 60s，于是每次正常轮回都被记成失败 → 连败 6 次放弃 → 编排器重启 →
约 4 分钟一圈空转，两个号 ``inbound_count`` 恒 0（LINE 收消息全停 2 小时）。
故本文件同时守「干净结束的正常轮回**不得**被算失败」这条不变量。
"""

from __future__ import annotations

from pathlib import Path

from src.integrations.line_recv_supervisor import LineRecvSupervisor

_ENGINE_ROOT = Path(__file__).resolve().parents[1]


def test_healthy_attempt_resets_and_resumes_fast():
    sup = LineRecvSupervisor()
    sup.streak = 3
    v = sup.on_attempt_end(3600.0)
    assert v == {"sleep_sec": 1.0, "give_up": False, "streak": 0}


def test_short_failures_backoff_exponentially_then_give_up():
    sup = LineRecvSupervisor()
    sleeps = []
    for _ in range(5):
        v = sup.on_attempt_end(0.2)      # 签名桥崩坏＝即抛即返
        assert v["give_up"] is False
        sleeps.append(v["sleep_sec"])
    assert sleeps == [5.0, 10.0, 20.0, 40.0, 80.0]
    v6 = sup.on_attempt_end(0.2)
    assert v6["give_up"] is True and v6["sleep_sec"] == 0.0
    assert v6["streak"] == 6


def test_backoff_respects_cap():
    sup = LineRecvSupervisor(give_up_after=12)
    v = None
    for _ in range(9):
        v = sup.on_attempt_end(0.1)
    assert v is not None and v["sleep_sec"] == 300.0, "退避必须封顶（勿指数爆炸）"


def test_recovery_mid_streak_clears_counter():
    sup = LineRecvSupervisor()
    sup.on_attempt_end(0.5)
    sup.on_attempt_end(0.5)
    assert sup.streak == 2
    sup.on_attempt_end(120.0)            # 一次健康会话
    assert sup.streak == 0
    v = sup.on_attempt_end(0.5)          # 再失败从头退避
    assert v["sleep_sec"] == 5.0 and v["give_up"] is False


def test_clean_gateway_cycle_is_not_a_failure():
    """网关正常关流（~10s ping 后 clean return）＝正常轮回，不得计失败。

    这条就是 2026-08-27 07:19–09:30「放弃→重启」无限循环的回归钉。
    """
    sup = LineRecvSupervisor()
    sup.streak = 3                                    # 之前真失败过几轮
    v = sup.on_attempt_end(10.09, clean=True)         # 真机实测的轮回时长
    assert v == {"sleep_sec": 1.0, "give_up": False, "streak": 0}


def test_many_clean_cycles_never_give_up():
    """连续几百轮正常轮回也不许放弃（否则 LINE 收消息永久停摆）。"""
    sup = LineRecvSupervisor()
    for _ in range(500):
        v = sup.on_attempt_end(10.0, clean=True)
        assert v["give_up"] is False
    assert sup.streak == 0


def test_instant_clean_close_still_rate_capped():
    """干净结束但秒内即返＝病态空转：仍走退避（限速是风暴防线的硬要求）。"""
    sup = LineRecvSupervisor()
    v = sup.on_attempt_end(0.05, clean=True)
    assert v["sleep_sec"] == 5.0 and v["give_up"] is False and v["streak"] == 1


def test_exception_at_ten_seconds_is_still_a_failure():
    """同样 10s，抛异常就是失败——门槛按结束方式分流，别混成一个。"""
    sup = LineRecvSupervisor()
    v = sup.on_attempt_end(10.0, clean=False)
    assert v["streak"] == 1 and v["sleep_sec"] == 5.0


def test_worker_wiring_uses_one_shot_run_and_supervisor():
    """静态接线 ratchet：LINE worker 不得改回 okline 内建零退避重连。"""
    src = (_ENGINE_ROOT / "src" / "integrations"
           / "account_orchestrator.py").read_text(encoding="utf-8")
    assert "bot.run(reconnect=False)" in src, (
        "LINE receiver 退回 bot.run(reconnect=True)＝零退避风暴源回归")
    assert "LineRecvSupervisor" in src
    assert "bot.run(reconnect=True)" not in src


def test_worker_wiring_reports_clean_vs_exception():
    """静态接线 ratchet：漏传 ``clean=`` 就退回「正常轮回被当失败」的死循环。

    纯函数门禁测不到这一层——判据对了但调用方不传，行为照旧是坏的。
    """
    src = (_ENGINE_ROOT / "src" / "integrations"
           / "account_orchestrator.py").read_text(encoding="utf-8")
    assert "on_attempt_end(lived, clean=clean)" in src, (
        "receiver 必须如实上报本轮是否抛异常（clean=），否则网关的 ~10s "
        "正常轮回会被当成短命失败 → 连败放弃 → 编排器重启 → 无限循环")
    assert "recv_cycles" in src, "轮回次数要可读数（正常轮回不打日志）"
