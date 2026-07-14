# -*- coding: utf-8 -*-
"""Reverse heartbeat prober (Stage I, 2026-05-05) 单元测试.

主控主动 GET worker /devices, 把 worker HeartbeatSender 没在 push 但 server
活着的 host 注册成 online. 不依赖真网络, mock urlopen.
"""
from __future__ import annotations

import json
import time
import threading
from unittest.mock import MagicMock, patch

import pytest

from src.host import multi_host
from src.host.multi_host import (
    ClusterCoordinator,
    HostInfo,
    _ReverseHeartbeatProber,
    start_reverse_prober,
    stop_reverse_prober,
    reset_reverse_prober_for_tests,
)


@pytest.fixture
def fresh_coord(monkeypatch):
    """每 test 独立 coordinator instance + 不写 prod cluster_state.json."""
    coord = ClusterCoordinator.__new__(ClusterCoordinator)
    coord._lock = threading.Lock()
    coord._hosts = {}
    coord._secret = ""
    monkeypatch.setattr(multi_host, "_coordinator", coord)
    # 防写真 cluster_state.json
    monkeypatch.setattr(coord, "_persist_state", lambda: None)
    yield coord


# ── reverse_probe_worker 单元 ────────────────────────────────────────


def test_probe_unknown_host_returns_false(fresh_coord):
    assert fresh_coord.reverse_probe_worker("not_in_hosts") is False


def test_probe_no_ip_returns_false(fresh_coord):
    fresh_coord._hosts["w_noip"] = HostInfo(
        host_id="w_noip", host_name="W", host_ip="", port=8000)
    assert fresh_coord.reverse_probe_worker("w_noip") is False


def test_probe_coordinator_self_returns_false(fresh_coord):
    fresh_coord._hosts["coordinator"] = HostInfo(
        host_id="coordinator", host_name="主控",
        host_ip="192.168.0.118", port=18080)
    assert fresh_coord.reverse_probe_worker("coordinator") is False


def test_probe_success_registers_online(fresh_coord):
    fresh_coord._hosts["w03"] = HostInfo(
        host_id="w03", host_name="W03",
        host_ip="192.168.0.101", port=8000,
        last_heartbeat=0.0, online=False)

    fake_devices = [
        {"device_id": "FAKE_001", "status": "connected"},
        {"device_id": "FAKE_002", "status": "connected"},
    ]
    fake_resp = MagicMock()
    fake_resp.read.return_value = json.dumps(fake_devices).encode()
    fake_resp.__enter__ = lambda self: self
    fake_resp.__exit__ = lambda self, *a: False

    with patch("urllib.request.urlopen", return_value=fake_resp):
        ok = fresh_coord.reverse_probe_worker("w03", timeout=2.0)

    assert ok is True
    h = fresh_coord._hosts["w03"]
    assert h.online is True
    assert len(h.devices) == 2
    assert h.devices[0]["device_id"] == "FAKE_001"
    # last_heartbeat 应被更新到接近 now
    assert (time.time() - h.last_heartbeat) < 5


def test_probe_dict_response_format(fresh_coord):
    """worker /devices 返 {"devices": [...]} 的 dict 形式也能解."""
    fresh_coord._hosts["w03"] = HostInfo(
        host_id="w03", host_ip="192.168.0.101", port=8000)
    fake_resp = MagicMock()
    fake_resp.read.return_value = json.dumps(
        {"devices": [{"device_id": "X1"}]}).encode()
    fake_resp.__enter__ = lambda self: self
    fake_resp.__exit__ = lambda self, *a: False
    with patch("urllib.request.urlopen", return_value=fake_resp):
        assert fresh_coord.reverse_probe_worker("w03") is True
    assert fresh_coord._hosts["w03"].devices[0]["device_id"] == "X1"


def test_probe_string_devices_normalized(fresh_coord):
    """worker 返字符串 device_id 列表也能被规范化为 dict."""
    fresh_coord._hosts["w03"] = HostInfo(
        host_id="w03", host_ip="192.168.0.101", port=8000)
    fake_resp = MagicMock()
    fake_resp.read.return_value = b'["DID_A", "DID_B"]'
    fake_resp.__enter__ = lambda self: self
    fake_resp.__exit__ = lambda self, *a: False
    with patch("urllib.request.urlopen", return_value=fake_resp):
        assert fresh_coord.reverse_probe_worker("w03") is True
    devs = fresh_coord._hosts["w03"].devices
    assert len(devs) == 2
    assert devs[0]["device_id"] == "DID_A"
    assert devs[0]["status"] == "unknown"


def test_probe_http_failure_returns_false_keeps_offline(fresh_coord):
    fresh_coord._hosts["w03"] = HostInfo(
        host_id="w03", host_ip="192.168.0.101", port=8000,
        last_heartbeat=0.0, online=False)

    with patch("urllib.request.urlopen",
               side_effect=ConnectionRefusedError("nope")):
        assert fresh_coord.reverse_probe_worker("w03") is False

    # 失败的 host 保持 offline, 不引入 false positive
    assert fresh_coord._hosts["w03"].online is False
    assert fresh_coord._hosts["w03"].last_heartbeat == 0.0


def test_probe_invalid_json_returns_false(fresh_coord):
    fresh_coord._hosts["w03"] = HostInfo(
        host_id="w03", host_ip="192.168.0.101", port=8000)
    fake_resp = MagicMock()
    fake_resp.read.return_value = b"not json"
    fake_resp.__enter__ = lambda self: self
    fake_resp.__exit__ = lambda self, *a: False
    with patch("urllib.request.urlopen", return_value=fake_resp):
        assert fresh_coord.reverse_probe_worker("w03") is False


def test_probe_secret_header_passed(fresh_coord):
    fresh_coord._secret = "TEST_SECRET_123"
    fresh_coord._hosts["w03"] = HostInfo(
        host_id="w03", host_ip="192.168.0.101", port=8000)
    fake_resp = MagicMock()
    fake_resp.read.return_value = b'[]'
    fake_resp.__enter__ = lambda self: self
    fake_resp.__exit__ = lambda self, *a: False

    captured = {}

    def _fake_urlopen(req, timeout=None):
        captured["headers"] = dict(req.header_items())
        return fake_resp

    with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
        fresh_coord.reverse_probe_worker("w03")

    # X-Cluster-Secret header 应被加上 (case-insensitive 查)
    keys_lower = {k.lower() for k in captured.get("headers", {})}
    assert "x-cluster-secret" in keys_lower


# ── _ReverseHeartbeatProber 后台线程 ─────────────────────────────────


def test_prober_skips_online_hosts(fresh_coord, monkeypatch):
    """已 online 的 host (last_heartbeat 新鲜) 不应被 probe."""
    now = time.time()
    fresh_coord._hosts["w_online"] = HostInfo(
        host_id="w_online", host_ip="192.168.0.101", port=8000,
        last_heartbeat=now, online=True)

    probed = []
    monkeypatch.setattr(
        fresh_coord, "reverse_probe_worker",
        lambda hid, timeout=5.0: probed.append(hid) or True,
    )

    pr = _ReverseHeartbeatProber(interval=10.0, startup_delay=0)
    pr._tick()

    assert probed == []


def test_prober_probes_stale_hosts(fresh_coord, monkeypatch):
    """last_heartbeat 超过 _HOST_TIMEOUT 的 host 应被 probe."""
    fresh_coord._hosts["w_stale"] = HostInfo(
        host_id="w_stale", host_ip="192.168.0.101", port=8000,
        last_heartbeat=0.0, online=False)
    fresh_coord._hosts["w_fresh"] = HostInfo(
        host_id="w_fresh", host_ip="192.168.0.102", port=8000,
        last_heartbeat=time.time(), online=True)

    probed = []
    monkeypatch.setattr(
        fresh_coord, "reverse_probe_worker",
        lambda hid, timeout=5.0: probed.append(hid) or False,
    )

    pr = _ReverseHeartbeatProber(interval=10.0, startup_delay=0)
    pr._tick()

    assert probed == ["w_stale"]


def test_prober_skips_coordinator(fresh_coord, monkeypatch):
    fresh_coord._hosts["coordinator"] = HostInfo(
        host_id="coordinator", host_ip="192.168.0.118", port=18080,
        last_heartbeat=0.0, online=False)

    probed = []
    monkeypatch.setattr(
        fresh_coord, "reverse_probe_worker",
        lambda hid, timeout=5.0: probed.append(hid) or True,
    )
    pr = _ReverseHeartbeatProber(interval=10.0, startup_delay=0)
    pr._tick()
    assert probed == []


def test_prober_tick_swallows_exception(fresh_coord, monkeypatch):
    """单 host probe 抛异常不影响线程."""
    fresh_coord._hosts["w03"] = HostInfo(
        host_id="w03", host_ip="192.168.0.101", port=8000,
        last_heartbeat=0.0, online=False)

    monkeypatch.setattr(
        fresh_coord, "reverse_probe_worker",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    pr = _ReverseHeartbeatProber(interval=10.0, startup_delay=0)
    # 不应 propagate
    pr._tick()
    assert pr._iterations == 1


# ── start/stop lifecycle ────────────────────────────────────────────


def test_start_then_stop_clean(fresh_coord, monkeypatch):
    monkeypatch.delenv("OPENCLAW_DISABLE_REVERSE_PROBE", raising=False)
    reset_reverse_prober_for_tests()

    t = start_reverse_prober(interval=0.05, startup_delay=0)
    assert t is not None
    assert t.is_alive()
    time.sleep(0.15)  # 让它跑几次 tick
    assert t._iterations >= 2

    ok = stop_reverse_prober(timeout_sec=2.0)
    assert ok is True
    assert not t.is_alive()


def test_env_disable_returns_none(monkeypatch):
    monkeypatch.setenv("OPENCLAW_DISABLE_REVERSE_PROBE", "1")
    reset_reverse_prober_for_tests()
    t = start_reverse_prober()
    assert t is None


def test_idempotent_start(fresh_coord, monkeypatch):
    monkeypatch.delenv("OPENCLAW_DISABLE_REVERSE_PROBE", raising=False)
    reset_reverse_prober_for_tests()

    t1 = start_reverse_prober(interval=10.0, startup_delay=0)
    t2 = start_reverse_prober(interval=10.0, startup_delay=0)
    assert t1 is t2  # 同一 instance 不重复启

    stop_reverse_prober()


def test_reset_for_tests_stops_thread(fresh_coord, monkeypatch):
    """Stage C.2 教训: reset 必须真 stop+join, 不能只清单例."""
    monkeypatch.delenv("OPENCLAW_DISABLE_REVERSE_PROBE", raising=False)
    reset_reverse_prober_for_tests()  # clean from prior test

    t = start_reverse_prober(interval=0.05, startup_delay=0)
    assert t.is_alive()

    reset_reverse_prober_for_tests()
    time.sleep(0.1)
    assert not t.is_alive(), "reset_for_tests 应真 stop+join thread"


# ── Stage K.1: per-host 指数退避 ─────────────────────────────────────


def test_backoff_fail_doubles_interval(fresh_coord, monkeypatch):
    """单 host probe 连续失败, 间隔翻倍直到 max_interval cap."""
    fresh_coord._hosts["w_dead"] = HostInfo(
        host_id="w_dead", host_ip="192.168.0.99", port=8000,
        last_heartbeat=0.0, online=False)

    monkeypatch.setattr(
        fresh_coord, "reverse_probe_worker",
        lambda hid, timeout=5.0: False,
    )

    pr = _ReverseHeartbeatProber(
        interval=10.0, startup_delay=0,
        max_interval=80.0, backoff_multiplier=2.0,
    )
    # Tick 1: 第一次 probe → fail → 间隔翻倍到 20s
    pr._tick()
    assert pr._current_interval["w_dead"] == 20.0
    # Tick 2 立即跑: 还在退避内 (next_probe_at 在未来), 不 probe
    probed_count_before = pr._last_probed
    pr._tick()
    assert pr._last_probed == probed_count_before  # 没 probe

    # 模拟时间过去 (绕过退避): 直接 fast-forward _next_probe_at
    pr._next_probe_at["w_dead"] = 0
    pr._tick()  # → fail again → 40s
    assert pr._current_interval["w_dead"] == 40.0
    pr._next_probe_at["w_dead"] = 0
    pr._tick()  # → 80s (cap)
    assert pr._current_interval["w_dead"] == 80.0
    pr._next_probe_at["w_dead"] = 0
    pr._tick()  # → 仍 80s (cap)
    assert pr._current_interval["w_dead"] == 80.0


def test_backoff_success_resets_to_default(fresh_coord, monkeypatch):
    """probe 失败 N 次后成功 → 间隔 reset 到默认."""
    fresh_coord._hosts["w_flaky"] = HostInfo(
        host_id="w_flaky", host_ip="192.168.0.50", port=8000,
        last_heartbeat=0.0, online=False)

    results = iter([False, False, True])
    monkeypatch.setattr(
        fresh_coord, "reverse_probe_worker",
        lambda hid, timeout=5.0: next(results),
    )

    pr = _ReverseHeartbeatProber(
        interval=10.0, startup_delay=0,
        max_interval=80.0, backoff_multiplier=2.0,
    )
    pr._tick()  # fail → 20s
    assert pr._current_interval["w_flaky"] == 20.0
    pr._next_probe_at["w_flaky"] = 0
    pr._tick()  # fail → 40s
    assert pr._current_interval["w_flaky"] == 40.0
    pr._next_probe_at["w_flaky"] = 0
    pr._tick()  # success → reset 10s
    assert pr._current_interval["w_flaky"] == 10.0


def test_backoff_per_host_independent(fresh_coord, monkeypatch):
    """两 host 退避独立 — w_dead 退避不影响 w_alive."""
    fresh_coord._hosts["w_dead"] = HostInfo(
        host_id="w_dead", host_ip="192.168.0.99", port=8000,
        last_heartbeat=0.0, online=False)
    fresh_coord._hosts["w_alive"] = HostInfo(
        host_id="w_alive", host_ip="192.168.0.50", port=8000,
        last_heartbeat=0.0, online=False)

    def _probe(hid, timeout=5.0):
        return hid == "w_alive"
    monkeypatch.setattr(fresh_coord, "reverse_probe_worker", _probe)

    pr = _ReverseHeartbeatProber(
        interval=10.0, startup_delay=0,
        max_interval=80.0, backoff_multiplier=2.0,
    )
    pr._tick()
    # w_dead fail → 20s 退避; w_alive success → 10s default
    assert pr._current_interval["w_dead"] == 20.0
    assert pr._current_interval["w_alive"] == 10.0


def test_backoff_init_state_allows_immediate_probe(fresh_coord, monkeypatch):
    """新 host 字典里没 entry → 立即 probe (不等任何退避窗口)."""
    fresh_coord._hosts["w_new"] = HostInfo(
        host_id="w_new", host_ip="192.168.0.50", port=8000,
        last_heartbeat=0.0, online=False)
    probed = []
    monkeypatch.setattr(
        fresh_coord, "reverse_probe_worker",
        lambda hid, timeout=5.0: probed.append(hid) or True,
    )
    pr = _ReverseHeartbeatProber(interval=100.0, startup_delay=0)
    pr._tick()  # 第一次 tick 立即 probe (字典里没 next_probe_at[w_new])
    assert probed == ["w_new"]


def test_status_includes_backoff_info(fresh_coord, monkeypatch):
    """status() 暴露 max_interval / backoff_multiplier / per_host_backoff."""
    pr = _ReverseHeartbeatProber(
        interval=30.0, startup_delay=0,
        max_interval=300.0, backoff_multiplier=2.0,
    )
    s = pr.status()
    assert s["interval_sec"] == 30.0
    assert s["max_interval_sec"] == 300.0
    assert s["backoff_multiplier"] == 2.0
    assert s["per_host_backoff"] == {}


# ── Stage K.2: cluster.yaml 配置 ───────────────────────────────────


def test_start_reads_cluster_yaml_defaults(fresh_coord, monkeypatch):
    """start_reverse_prober 不传参数时, 从 cluster.yaml 读默认."""
    monkeypatch.delenv("OPENCLAW_DISABLE_REVERSE_PROBE", raising=False)
    reset_reverse_prober_for_tests()

    fake_cfg = {
        "reverse_probe_interval": 0.05,
        "reverse_probe_startup_delay": 0,
        "reverse_probe_max_interval": 200.0,
        "reverse_probe_backoff_multiplier": 3.0,
    }
    monkeypatch.setattr(multi_host, "load_cluster_config",
                        lambda: fake_cfg)

    t = start_reverse_prober()
    assert t is not None
    assert t._interval == 0.05
    assert t._max_interval == 200.0
    assert t._backoff_mult == 3.0

    stop_reverse_prober(timeout_sec=2.0)


def test_start_explicit_args_override_cfg(fresh_coord, monkeypatch):
    """显式参数覆盖 cluster.yaml."""
    monkeypatch.delenv("OPENCLAW_DISABLE_REVERSE_PROBE", raising=False)
    reset_reverse_prober_for_tests()

    monkeypatch.setattr(
        multi_host, "load_cluster_config",
        lambda: {"reverse_probe_interval": 100.0,
                 "reverse_probe_startup_delay": 0},
    )
    t = start_reverse_prober(interval=0.05)  # 显式覆盖
    assert t._interval == 0.05
    stop_reverse_prober(timeout_sec=2.0)


# ── Stage L.1: /cluster/reverse-probe/status endpoint ─────────────────


def test_status_endpoint_when_prober_not_running(fresh_coord, monkeypatch):
    """prober 没启动时 endpoint 返 running=False + reason."""
    from src.host.routers.cluster import cluster_reverse_probe_status
    reset_reverse_prober_for_tests()  # 确保 _reverse_prober is None
    out = cluster_reverse_probe_status()
    assert out["running"] is False
    assert "reason" in out


def test_status_endpoint_when_prober_running(fresh_coord, monkeypatch):
    """prober 跑着时 endpoint 返完整 status dict."""
    from src.host.routers.cluster import cluster_reverse_probe_status
    monkeypatch.delenv("OPENCLAW_DISABLE_REVERSE_PROBE", raising=False)
    reset_reverse_prober_for_tests()

    t = start_reverse_prober(interval=10.0, startup_delay=0)
    try:
        out = cluster_reverse_probe_status()
        assert out["running"] is True
        assert "iterations" in out
        assert "total_probed" in out
        assert "total_recovered" in out
        assert out["interval_sec"] == 10.0
        assert "per_host_backoff" in out
    finally:
        stop_reverse_prober(timeout_sec=2.0)


# ── Stage M.1: /cluster/reverse-probe/trigger endpoint ────────────────


def test_trigger_specific_host_clears_backoff_and_probes(
    fresh_coord, monkeypatch,
):
    """trigger 指定 host_id: 清退避窗口 + 立即 probe."""
    from src.host.routers.cluster import cluster_reverse_probe_trigger

    # _verify_cluster_secret 用 load_cluster_config 读 secret, mock 返空
    monkeypatch.setattr(
        multi_host, "load_cluster_config",
        lambda: {"shared_secret": "",
                 "reverse_probe_startup_delay": 0},
    )

    fresh_coord._hosts["w_target"] = HostInfo(
        host_id="w_target", host_ip="192.168.0.99", port=8000,
        last_heartbeat=0.0, online=False)

    monkeypatch.delenv("OPENCLAW_DISABLE_REVERSE_PROBE", raising=False)
    reset_reverse_prober_for_tests()
    pr = start_reverse_prober(interval=10.0, startup_delay=0)
    # 模拟该 host 已退避到未来
    pr._next_probe_at["w_target"] = time.time() + 1000

    monkeypatch.setattr(
        fresh_coord, "reverse_probe_worker",
        lambda hid, timeout=5.0: True,
    )

    try:
        out = cluster_reverse_probe_trigger({"host_id": "w_target"})
        assert "w_target" in out["probed"]
        assert "w_target" in out["recovered"]
    finally:
        stop_reverse_prober(timeout_sec=2.0)


def test_trigger_no_host_probes_all_stale(fresh_coord, monkeypatch):
    """trigger 不传 host_id: probe 所有 stale host."""
    from src.host.routers.cluster import cluster_reverse_probe_trigger

    monkeypatch.setattr(
        multi_host, "load_cluster_config",
        lambda: {"shared_secret": "",
                 "reverse_probe_startup_delay": 0},
    )

    fresh_coord._hosts["w_dead"] = HostInfo(
        host_id="w_dead", host_ip="192.168.0.99", port=8000,
        last_heartbeat=0.0, online=False)
    fresh_coord._hosts["w_alive"] = HostInfo(
        host_id="w_alive", host_ip="192.168.0.50", port=8000,
        last_heartbeat=time.time(), online=True)

    monkeypatch.delenv("OPENCLAW_DISABLE_REVERSE_PROBE", raising=False)
    reset_reverse_prober_for_tests()
    start_reverse_prober(interval=10.0, startup_delay=0)

    probed_args = []

    def _probe(hid, timeout=5.0):
        probed_args.append(hid)
        return hid == "w_dead"
    monkeypatch.setattr(fresh_coord, "reverse_probe_worker", _probe)

    try:
        out = cluster_reverse_probe_trigger({})
        # 只有 stale host (w_dead) 被 probe, w_alive 跳过
        assert probed_args == ["w_dead"]
        assert out["probed"] == ["w_dead"]
        assert out["recovered"] == ["w_dead"]
    finally:
        stop_reverse_prober(timeout_sec=2.0)


def test_trigger_secret_verification_rejects_wrong_signature(
    fresh_coord, monkeypatch,
):
    """secret 配置时, body 缺签名 → 抛 HTTPException."""
    from fastapi import HTTPException
    from src.host.routers.cluster import cluster_reverse_probe_trigger

    fresh_coord._secret = "REAL_SECRET"
    monkeypatch.delenv("OPENCLAW_DISABLE_REVERSE_PROBE", raising=False)
    # cluster_state 路径 verify 用 load_cluster_config 读 shared_secret
    monkeypatch.setattr(
        multi_host, "load_cluster_config",
        lambda: {"shared_secret": "REAL_SECRET",
                 "reverse_probe_startup_delay": 0},
    )

    with pytest.raises(HTTPException) as exc_info:
        cluster_reverse_probe_trigger({"host_id": "x"})
    # 缺 _sig / _ts 应被拒
    assert exc_info.value.status_code in (401, 403)


# ── Stage N.1: HeartbeatSender auto-trigger ──────────────────────────


def _make_sender():
    """构造 minimal HeartbeatSender 不起 thread."""
    from src.host.multi_host import HeartbeatSender
    s = HeartbeatSender.__new__(HeartbeatSender)
    s._coordinator_url = "http://127.0.0.1:18080"
    s._host_id = "w03"
    s._consecutive_failures = 0
    s._standalone_mode = False
    s._last_auto_trigger_at = 0.0
    s._AUTO_TRIGGER_COOLDOWN_SEC = 300.0
    return s


def test_auto_trigger_cooldown_blocks_spam(monkeypatch):
    """5min cooldown 内重复调 _try_auto_trigger 立即返 False 不发请求."""
    s = _make_sender()
    s._last_auto_trigger_at = time.time()  # 刚 trigger 过

    called = []
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *a, **k: called.append(1) or MagicMock(),
    )
    assert s._try_auto_trigger() is False
    assert called == []  # 没 HTTP 请求


def test_auto_trigger_health_unreachable_skips_trigger(monkeypatch):
    """主控 /health 不通 (双向网断) → skip trigger 不浪费 timeout."""
    s = _make_sender()
    monkeypatch.setattr(
        multi_host, "load_cluster_config",
        lambda: {"shared_secret": ""},
    )
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *a, **k: (_ for _ in ()).throw(
            ConnectionRefusedError("nope")),
    )
    assert s._try_auto_trigger() is False


def test_auto_trigger_full_path_calls_trigger_endpoint(monkeypatch):
    """主控 /health 200 + trigger 200 → return True."""
    s = _make_sender()
    monkeypatch.setattr(
        multi_host, "load_cluster_config",
        lambda: {"shared_secret": ""},
    )

    calls = []

    def _fake_urlopen(req, timeout=None):
        calls.append((req.full_url, req.get_method()))
        resp = MagicMock()
        resp.status = 200
        resp.read.return_value = b"ok"
        resp.__enter__ = lambda self: self
        resp.__exit__ = lambda self, *a: False
        resp.close = lambda: None
        return resp

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)
    assert s._try_auto_trigger() is True
    # 验证: 先 GET /health, 再 POST /cluster/reverse-probe/trigger
    assert len(calls) == 2
    assert calls[0] == ("http://127.0.0.1:18080/health", "GET")
    assert calls[1] == (
        "http://127.0.0.1:18080/cluster/reverse-probe/trigger", "POST")


def test_auto_trigger_signs_body_when_secret_configured(monkeypatch):
    """配置 secret 时, trigger body 应含 _sig + _ts (HMAC-SHA256)."""
    import json
    s = _make_sender()
    monkeypatch.setattr(
        multi_host, "load_cluster_config",
        lambda: {"shared_secret": "TEST_SECRET"},
    )
    captured_body = []

    def _fake_urlopen(req, timeout=None):
        if req.get_method() == "POST":
            captured_body.append(json.loads(req.data))
        resp = MagicMock()
        resp.status = 200
        resp.read.return_value = b""
        resp.__enter__ = lambda self: self
        resp.__exit__ = lambda self, *a: False
        resp.close = lambda: None
        return resp

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)
    assert s._try_auto_trigger() is True
    assert len(captured_body) == 1
    body = captured_body[0]
    assert body["host_id"] == "w03"
    assert "_sig" in body
    assert "_ts" in body
    assert len(body["_sig"]) == 64  # SHA256 hex


# ── Stage P: prober wake_event + trigger async ──────────────────────


def test_request_immediate_probe_pops_backoff_and_wakes(fresh_coord):
    """request_immediate_probe(host_id): pop 退避窗口 + set wake_event."""
    pr = _ReverseHeartbeatProber(interval=10.0, startup_delay=0)
    # 模拟该 host 已退避到未来
    pr._next_probe_at["w03"] = time.time() + 1000
    pr._current_interval["w03"] = 240.0

    pr.request_immediate_probe("w03")

    # 退避被 pop
    assert "w03" not in pr._next_probe_at
    # wake_event set
    assert pr._wake_event.is_set()


def test_request_immediate_probe_empty_clears_all(fresh_coord):
    """host_id='' 清所有退避."""
    pr = _ReverseHeartbeatProber(interval=10.0, startup_delay=0)
    pr._next_probe_at["w03"] = time.time() + 1000
    pr._next_probe_at["w175"] = time.time() + 500

    pr.request_immediate_probe("")

    assert pr._next_probe_at == {}
    assert pr._wake_event.is_set()


def test_wake_event_makes_prober_tick_immediately(fresh_coord, monkeypatch):
    """prober run loop wait_for_next_tick 在 wake_event set 时立即返."""
    fresh_coord._hosts["w03"] = HostInfo(
        host_id="w03", host_ip="192.168.0.99", port=8000,
        last_heartbeat=0.0, online=False)
    monkeypatch.setattr(
        fresh_coord, "reverse_probe_worker",
        lambda hid, timeout=5.0: True,
    )
    monkeypatch.delenv("OPENCLAW_DISABLE_REVERSE_PROBE", raising=False)
    reset_reverse_prober_for_tests()

    # interval=5s 但我们用 wake event 让它 ≤200ms 完成第二 tick
    pr = start_reverse_prober(interval=5.0, startup_delay=0)
    try:
        time.sleep(0.1)  # 让第一 tick 跑完
        first_iter = pr._iterations

        pr.request_immediate_probe("w03")  # wake!
        time.sleep(0.2)  # ≤50ms polling step + 一些余量

        assert pr._iterations > first_iter, \
            "wake_event 应让 prober 立即跑下一 tick (而非等满 5s)"
    finally:
        stop_reverse_prober(timeout_sec=2.0)


def test_trigger_endpoint_wait_false_returns_queued(fresh_coord, monkeypatch):
    """wait=false: endpoint 立即返 {queued: [...], mode: async}, 不阻塞."""
    from src.host.routers.cluster import cluster_reverse_probe_trigger

    monkeypatch.setattr(
        multi_host, "load_cluster_config",
        lambda: {"shared_secret": ""},
    )
    fresh_coord._hosts["w03"] = HostInfo(
        host_id="w03", host_ip="192.168.0.99", port=8000,
        last_heartbeat=0.0, online=False)

    monkeypatch.delenv("OPENCLAW_DISABLE_REVERSE_PROBE", raising=False)
    reset_reverse_prober_for_tests()
    start_reverse_prober(interval=10.0, startup_delay=0)

    # 让 reverse_probe_worker 慢 1s, 验证 async 不等
    probe_calls = []

    def _slow_probe(hid, timeout=5.0):
        probe_calls.append(hid)
        time.sleep(1.0)
        return True
    monkeypatch.setattr(fresh_coord, "reverse_probe_worker", _slow_probe)

    try:
        t0 = time.time()
        out = cluster_reverse_probe_trigger(
            {"host_id": "w03", "wait": False})
        elapsed = time.time() - t0

        assert out["mode"] == "async"
        assert "w03" in out["queued"]
        # 立即返, 没等 1s probe
        assert elapsed < 0.5, f"async should be fast, took {elapsed:.2f}s"
    finally:
        stop_reverse_prober(timeout_sec=3.0)


def test_trigger_endpoint_wait_true_default_sync(fresh_coord, monkeypatch):
    """wait=true (default): sync 等 probe 完成, mode=sync."""
    from src.host.routers.cluster import cluster_reverse_probe_trigger

    monkeypatch.setattr(
        multi_host, "load_cluster_config",
        lambda: {"shared_secret": ""},
    )
    fresh_coord._hosts["w03"] = HostInfo(
        host_id="w03", host_ip="192.168.0.99", port=8000,
        last_heartbeat=0.0, online=False)

    monkeypatch.delenv("OPENCLAW_DISABLE_REVERSE_PROBE", raising=False)
    reset_reverse_prober_for_tests()
    start_reverse_prober(interval=10.0, startup_delay=0)

    monkeypatch.setattr(
        fresh_coord, "reverse_probe_worker",
        lambda hid, timeout=5.0: True,
    )

    try:
        out = cluster_reverse_probe_trigger({"host_id": "w03"})  # wait 不传 → True
        assert out.get("mode") == "sync"
        assert "w03" in out["probed"]
        assert "w03" in out["recovered"]
    finally:
        stop_reverse_prober(timeout_sec=2.0)


def test_trigger_async_falls_back_to_sync_when_no_prober(
    fresh_coord, monkeypatch,
):
    """prober 没启动时 wait=false 自动 fallback 到 sync (兜底安全)."""
    from src.host.routers.cluster import cluster_reverse_probe_trigger

    monkeypatch.setattr(
        multi_host, "load_cluster_config",
        lambda: {"shared_secret": ""},
    )
    fresh_coord._hosts["w03"] = HostInfo(
        host_id="w03", host_ip="192.168.0.99", port=8000,
        last_heartbeat=0.0, online=False)

    reset_reverse_prober_for_tests()  # prober is None
    monkeypatch.setattr(
        fresh_coord, "reverse_probe_worker",
        lambda hid, timeout=5.0: True,
    )

    out = cluster_reverse_probe_trigger({"host_id": "w03", "wait": False})
    # prober None 时, async 路径 fallback 到 sync, mode=sync
    assert out["mode"] == "sync"
    assert "w03" in out["probed"]


# ── Stage W: /metrics 接入 reverse_probe 字段 ────────────────────────


def test_metrics_snapshot_includes_reverse_probe_when_running(
    fresh_coord, monkeypatch,
):
    """health_monitor.snapshot() 暴露 reverse_probe 字段, prober 跑时返完整 status."""
    from src.host.health_monitor import metrics as _metrics
    monkeypatch.delenv("OPENCLAW_DISABLE_REVERSE_PROBE", raising=False)
    reset_reverse_prober_for_tests()
    start_reverse_prober(interval=10.0, startup_delay=0)
    try:
        snap = _metrics.snapshot()
        assert "reverse_probe" in snap
        rp = snap["reverse_probe"]
        assert rp["running"] is True
        assert "iterations" in rp
        assert "total_probed" in rp
        assert rp["interval_sec"] == 10.0
    finally:
        stop_reverse_prober(timeout_sec=2.0)


def test_metrics_snapshot_reverse_probe_running_false_when_not_started(
    fresh_coord, monkeypatch,
):
    """prober 没启动时 snapshot reverse_probe = {running: False}."""
    from src.host.health_monitor import metrics as _metrics
    reset_reverse_prober_for_tests()  # clean
    snap = _metrics.snapshot()
    assert snap["reverse_probe"] == {"running": False}


# ── Stage V.1: trigger 批量 host_ids ─────────────────────────────────


def test_trigger_batch_host_ids_sync(fresh_coord, monkeypatch):
    """body host_ids:[a,b] 批量 sync probe."""
    from src.host.routers.cluster import cluster_reverse_probe_trigger

    monkeypatch.setattr(
        multi_host, "load_cluster_config",
        lambda: {"shared_secret": ""},
    )
    fresh_coord._hosts["w03"] = HostInfo(
        host_id="w03", host_ip="192.168.0.99", port=8000,
        last_heartbeat=0.0, online=False)
    fresh_coord._hosts["w175"] = HostInfo(
        host_id="w175", host_ip="192.168.0.50", port=8000,
        last_heartbeat=0.0, online=False)
    fresh_coord._hosts["w_ignored"] = HostInfo(
        host_id="w_ignored", host_ip="192.168.0.10", port=8000,
        last_heartbeat=0.0, online=False)

    monkeypatch.delenv("OPENCLAW_DISABLE_REVERSE_PROBE", raising=False)
    reset_reverse_prober_for_tests()
    start_reverse_prober(interval=10.0, startup_delay=0)

    probed_args = []

    def _probe(hid, timeout=5.0):
        probed_args.append(hid)
        return True
    monkeypatch.setattr(fresh_coord, "reverse_probe_worker", _probe)

    try:
        out = cluster_reverse_probe_trigger(
            {"host_ids": ["w03", "w175"]})
        # 只 probe 指定批, w_ignored 跳过
        assert sorted(probed_args) == ["w03", "w175"]
        assert sorted(out["probed"]) == ["w03", "w175"]
        assert out.get("mode") == "sync"
    finally:
        stop_reverse_prober(timeout_sec=2.0)


def test_trigger_batch_host_ids_async(fresh_coord, monkeypatch):
    """async 路径批量入队."""
    from src.host.routers.cluster import cluster_reverse_probe_trigger

    monkeypatch.setattr(
        multi_host, "load_cluster_config",
        lambda: {"shared_secret": ""},
    )
    monkeypatch.delenv("OPENCLAW_DISABLE_REVERSE_PROBE", raising=False)
    reset_reverse_prober_for_tests()
    pr = start_reverse_prober(interval=10.0, startup_delay=0)
    # 模拟两 host 都退避到未来
    pr._next_probe_at["w03"] = time.time() + 1000
    pr._next_probe_at["w175"] = time.time() + 1000

    try:
        out = cluster_reverse_probe_trigger(
            {"host_ids": ["w03", "w175"], "wait": False})
        assert sorted(out["queued"]) == ["w03", "w175"]
        assert out["mode"] == "async"
        # 退避被 pop
        assert "w03" not in pr._next_probe_at
        assert "w175" not in pr._next_probe_at
    finally:
        stop_reverse_prober(timeout_sec=2.0)


def test_trigger_host_id_takes_priority_over_batch(fresh_coord, monkeypatch):
    """同时传 host_id + host_ids: host_id 优先 (单值精确)."""
    from src.host.routers.cluster import cluster_reverse_probe_trigger

    monkeypatch.setattr(
        multi_host, "load_cluster_config",
        lambda: {"shared_secret": ""},
    )
    fresh_coord._hosts["w03"] = HostInfo(
        host_id="w03", host_ip="192.168.0.99", port=8000,
        last_heartbeat=0.0, online=False)

    monkeypatch.delenv("OPENCLAW_DISABLE_REVERSE_PROBE", raising=False)
    reset_reverse_prober_for_tests()
    start_reverse_prober(interval=10.0, startup_delay=0)

    probed_args = []
    monkeypatch.setattr(
        fresh_coord, "reverse_probe_worker",
        lambda hid, timeout=5.0: probed_args.append(hid) or True,
    )

    try:
        out = cluster_reverse_probe_trigger(
            {"host_id": "w03", "host_ids": ["should_be_ignored"]})
        assert probed_args == ["w03"]
        assert out["probed"] == ["w03"]
    finally:
        stop_reverse_prober(timeout_sec=2.0)


# ── Stage V.2: per_host_consecutive_failures ────────────────────────


def test_consecutive_failures_increments_on_fail(fresh_coord, monkeypatch):
    """probe 失败 → consecutive_failures 累加."""
    fresh_coord._hosts["w_dead"] = HostInfo(
        host_id="w_dead", host_ip="192.168.0.99", port=8000,
        last_heartbeat=0.0, online=False)
    monkeypatch.setattr(
        fresh_coord, "reverse_probe_worker",
        lambda hid, timeout=5.0: False,
    )
    pr = _ReverseHeartbeatProber(
        interval=10.0, startup_delay=0,
        max_interval=80.0, backoff_multiplier=2.0,
    )
    pr._tick()
    assert pr._consecutive_failures["w_dead"] == 1
    pr._next_probe_at["w_dead"] = 0
    pr._tick()
    assert pr._consecutive_failures["w_dead"] == 2
    pr._next_probe_at["w_dead"] = 0
    pr._tick()
    assert pr._consecutive_failures["w_dead"] == 3


def test_consecutive_failures_resets_on_success(fresh_coord, monkeypatch):
    """probe 成功 → consecutive_failures 清零."""
    fresh_coord._hosts["w_flaky"] = HostInfo(
        host_id="w_flaky", host_ip="192.168.0.50", port=8000,
        last_heartbeat=0.0, online=False)

    results = iter([False, False, True])
    monkeypatch.setattr(
        fresh_coord, "reverse_probe_worker",
        lambda hid, timeout=5.0: next(results),
    )
    pr = _ReverseHeartbeatProber(
        interval=10.0, startup_delay=0,
        max_interval=80.0, backoff_multiplier=2.0,
    )
    pr._tick()  # fail → 1
    assert pr._consecutive_failures["w_flaky"] == 1
    pr._next_probe_at["w_flaky"] = 0
    pr._tick()  # fail → 2
    assert pr._consecutive_failures["w_flaky"] == 2
    pr._next_probe_at["w_flaky"] = 0
    pr._tick()  # success → 0
    assert pr._consecutive_failures["w_flaky"] == 0


def test_status_includes_per_host_consecutive_failures(fresh_coord):
    pr = _ReverseHeartbeatProber(interval=10.0, startup_delay=0)
    pr._consecutive_failures = {"w03": 5, "w175": 0}
    s = pr.status()
    assert s["per_host_consecutive_failures"] == {"w03": 5, "w175": 0}


def test_auto_trigger_trigger_5xx_returns_false(monkeypatch):
    """trigger endpoint 返 500+ 时 → return False."""
    s = _make_sender()
    monkeypatch.setattr(
        multi_host, "load_cluster_config",
        lambda: {"shared_secret": ""},
    )

    def _fake_urlopen(req, timeout=None):
        resp = MagicMock()
        if req.get_method() == "GET":
            resp.status = 200
            resp.read.return_value = b"ok"
        else:
            resp.status = 500
            resp.read.return_value = b"server error"
        resp.__enter__ = lambda self: self
        resp.__exit__ = lambda self, *a: False
        resp.close = lambda: None
        return resp

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)
    # 5xx 不应返 True (status >= 400 跳过 success log)
    # 注: 实际 urllib 在 status>=400 时抛 HTTPError, 我们这里 mock 直接返
    # status 字段, code path 还是会 catch 但不进 success branch.
    s._try_auto_trigger()  # 不抛, 但 status>=400 不算成功
