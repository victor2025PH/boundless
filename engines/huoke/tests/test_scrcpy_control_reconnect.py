"""P2-③ scrcpy control socket 二阶段重连 + reconnect_control 单测.

不起真 scrcpy server (太重). 我们只测 _connect_control_once / reconnect_control
的纯逻辑分支:
- happy: 真 listen 一个 TCP port, _connect_control_once 应连上 + sanity probe pass
- fail: 没人 listen, _connect_control_once 应快速失败返 False (不抛)
- reconnect_control: 关闭旧 sock + 调 _connect_control + 返回 has_control
"""
from __future__ import annotations

import socket
import threading
import time

import pytest

from src.host.scrcpy_manager import ScrcpySession


def _spawn_listener(port: int, accept_count: int = 5) -> threading.Thread:
    """起一个临时 TCP listener, accept 后立刻 close (模拟 scrcpy server)."""
    def _serve():
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            srv.bind(("127.0.0.1", port))
            srv.listen(8)
            srv.settimeout(3.0)
            for _ in range(accept_count):
                try:
                    conn, _ = srv.accept()
                    # 保持 socket 活一小会让 client probe 成功
                    time.sleep(0.05)
                    conn.close()
                except socket.timeout:
                    break
        finally:
            try:
                srv.close()
            except Exception:
                pass
    th = threading.Thread(target=_serve, daemon=True)
    th.start()
    return th


def _free_port() -> int:
    """系统分配空闲端口."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_connect_control_once_happy(monkeypatch):
    """有人 listen 时, _connect_control_once 应连上, sanity probe pass, has_control=True."""
    port = _free_port()
    _spawn_listener(port, accept_count=2)
    time.sleep(0.1)  # 让 listener 起来

    # 不真启动 scrcpy server, 直接构造 session
    sess = ScrcpySession.__new__(ScrcpySession)
    sess.device_id = "DEVICE_TEST"
    sess.port = port
    sess._control_socket = None
    sess.has_control = False

    ok = sess._connect_control_once(attempts=2, delay=0.1, label="test_phase")
    assert ok is True
    assert sess.has_control is True
    assert sess._control_socket is not None

    # cleanup
    try:
        sess._control_socket.close()
    except Exception:
        pass


def test_connect_control_once_no_listener_returns_false():
    """没人 listen 时, 多次重试都失败, 返 False, has_control 保持 False, 不抛异常."""
    port = _free_port()  # 空闲但没起 listener
    sess = ScrcpySession.__new__(ScrcpySession)
    sess.device_id = "DEVICE_TEST"
    sess.port = port
    sess._control_socket = None
    sess.has_control = False

    start = time.time()
    ok = sess._connect_control_once(attempts=2, delay=0.1, label="test_no_listener")
    elapsed = time.time() - start

    assert ok is False
    assert sess.has_control is False
    assert sess._control_socket is None
    # 2 attempts × 0.1s delay 应在 ~3s 内 (timeout 5 + delay 0.1)
    assert elapsed < 12, f"took too long: {elapsed:.1f}s"


def test_reconnect_control_closes_old_socket(monkeypatch):
    """reconnect_control 应先关闭旧 socket, 再走 _connect_control 重新协商."""
    port = _free_port()
    _spawn_listener(port, accept_count=3)
    time.sleep(0.1)

    sess = ScrcpySession.__new__(ScrcpySession)
    sess.device_id = "DEVICE_TEST"
    sess.port = port
    sess._control_socket = None
    sess.has_control = False
    sess._ctrl_lock = threading.Lock()

    # 模拟先有一个旧 control socket
    old_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    old_sock.bind(("127.0.0.1", 0))
    sess._control_socket = old_sock
    sess.has_control = True

    # reconnect 应关闭旧, 起新
    ok = sess.reconnect_control()
    # 旧 socket fd 应已关闭 (close 后 send 抛 OSError)
    with pytest.raises(OSError):
        old_sock.send(b"x")

    assert ok is True
    assert sess.has_control is True
    assert sess._control_socket is not None
    assert sess._control_socket is not old_sock

    try:
        sess._control_socket.close()
    except Exception:
        pass


def test_reconnect_control_fallback_to_video_only_no_listener(monkeypatch):
    """reconnect_control 没人 listen 时, has_control=False 但不抛异常.

    2026-05-04 修: 旧版调真 socket connect 走 _connect_control hardcoded
    5+3 attempts × 5s timeout + 5s wait = ~50s, 全 suite pytest --timeout=20
    触发 stack trace 卡死 (Stage 0 baseline + Stage D 验证都中招).

    fix: mock _connect_control_once 永远返 False 模拟 no-listener, 跳过
    实际 socket. 保留 reconnect_control 的契约断言 (关旧 sock + 调
    _connect_control + 返 has_control). 真 socket 行为已由
    test_connect_control_once_no_listener_returns_false 用 attempts=2 +
    delay=0.1 验证 (那里 elapsed<12s 不卡).
    """
    # mock _connect_control_once 立即返 False, 跳真 socket 50s 卡顿
    monkeypatch.setattr(
        ScrcpySession, "_connect_control_once",
        lambda self, attempts, delay, label: False,
    )
    # 跳 _connect_control 内 phase1→phase2 之间 5s sleep
    monkeypatch.setattr(time, "sleep", lambda *a, **k: None)

    sess = ScrcpySession.__new__(ScrcpySession)
    sess.device_id = "DEVICE_TEST"
    sess.port = 1  # 任意 port (不会真用 _connect_control_once 已 mock)
    sess._control_socket = None
    sess.has_control = False
    sess._ctrl_lock = threading.Lock()

    ok = sess.reconnect_control()
    assert ok is False
    assert sess.has_control is False


def test_two_phase_uses_both_attempts(monkeypatch):
    """_connect_control 调 phase1 失败后等 5s 再 phase2.

    用 monkeypatch 替换 time.sleep + _connect_control_once 验证调用顺序.
    """
    sess = ScrcpySession.__new__(ScrcpySession)
    sess.device_id = "DEVICE_TEST"
    sess.port = 1  # any
    sess._control_socket = None
    sess.has_control = False

    calls = []
    sleep_durations = []

    def fake_once(attempts, delay, label):
        calls.append((attempts, delay, label))
        return False  # 两阶段都失败

    def fake_sleep(s):
        sleep_durations.append(s)

    monkeypatch.setattr(sess, "_connect_control_once", fake_once)
    monkeypatch.setattr("src.host.scrcpy_manager.time.sleep", fake_sleep)

    sess._connect_control()

    # 应调 phase1 + phase2
    assert len(calls) == 2
    assert calls[0][2] == "phase1"
    assert calls[1][2] == "phase2"
    # phase1 5×0.3, phase2 3×1.0
    assert calls[0][0] == 5 and calls[0][1] == 0.3
    assert calls[1][0] == 3 and calls[1][1] == 1.0
    # phase1 失败 → 等 5s → phase2
    assert 5.0 in sleep_durations
    assert sess.has_control is False
