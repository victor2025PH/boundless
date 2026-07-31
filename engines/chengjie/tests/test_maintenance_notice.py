"""维护预告闭环门禁（2026-07-31「连接中断」横幅根因治理）。

背景：生产实测 07-28..31 每天 14-16 次实例重启，每次 15-30s 全站不可用；
冷却状态在重启**之后**才写盘、断站期间 status 接口不可达 → 坐席每次都只能
看到红色「连接中断」。修复链＝重启脚本停机前 POST 内部路由 → 登记
maintenance_notice 状态 + EventBus→SSE 即时广播 → 前端切蓝色「维护窗口」。

覆盖：纯函数（clamp/set/expiry/reason 消毒）· seat_restart_banner 折叠
quiet_poll（宣告与停机之间的轮询须确认而非清除）· 内部路由鉴权双通道
（loopback 直通 / 非本机走 api_auth）· EventBus 广播 + SSE 白名单 ·
前端模板与重启脚本的静态接线（热更新面直上生产，静态钉住防哑接线）。
"""

from __future__ import annotations

import pathlib
import re
import time
import types

import pytest
from fastapi import FastAPI, HTTPException
from starlette.testclient import TestClient

from src.utils import maintenance_notice as mn


@pytest.fixture(autouse=True)
def _clean_notice():
    """进程级单例状态逐测隔离（防串味到其他 SSE/status 测试）。"""
    mn.clear_notice()
    yield
    mn.clear_notice()


# ── 纯函数 ────────────────────────────────────────────────────────────────


def test_clamp_window_sec_bounds_and_fallback():
    assert mn.clamp_window_sec(None) == mn.DEFAULT_WINDOW_SEC
    assert mn.clamp_window_sec("abc") == mn.DEFAULT_WINDOW_SEC
    assert mn.clamp_window_sec(1) == mn.MIN_WINDOW_SEC
    assert mn.clamp_window_sec(10**9) == mn.MAX_WINDOW_SEC
    assert mn.clamp_window_sec("120") == 120


def test_set_snapshot_expiry_roundtrip():
    now = 1_700_000_000.0
    snap = mn.set_notice(120, reason="restart_instance", now=now)
    assert snap["active"] is True
    assert snap["left_sec"] == 120
    assert snap["until_ts"] == now + 120
    mid = mn.snapshot(now=now + 60)
    assert mid["active"] is True and 0 < mid["left_sec"] <= 60
    # 过期后所有字段归零——消费方无需自行判窗
    after = mn.snapshot(now=now + 121)
    assert after["active"] is False
    assert after["left_sec"] == 0 and after["until_ts"] == 0 and after["reason"] == ""


def test_renotice_refreshes_window_and_reason_capped():
    now = 1_700_000_000.0
    mn.set_notice(60, reason="first", now=now)
    snap = mn.set_notice(300, reason="x" * 500, now=now + 30)
    assert snap["left_sec"] == 300  # 重复宣告＝刷新窗口（幂等）
    assert len(snap["reason"]) <= 200


def test_clear_notice():
    mn.set_notice(120, now=time.time())
    mn.clear_notice()
    assert mn.snapshot()["active"] is False


# ── seat_restart_banner 折叠（宣告→停机间隙的轮询必须"确认"而非"清除"）──────


def test_seat_banner_folds_maintenance_into_quiet(tmp_path, monkeypatch):
    # 隔离机器级文件：无冷却记录、无状态快照 —— quiet 只能来自维护预告
    d = tmp_path / "cd"
    d.mkdir()
    ops = tmp_path / "ops"
    ops.mkdir()
    monkeypatch.setenv("CHENGJIE_RESTART_COOLDOWN_DIR", str(d))
    monkeypatch.setenv("CHENGJIE_OPS_DIR", str(ops))
    monkeypatch.setenv("CHENGJIE_PRODUCT_ID", "zhiliao")
    from src.utils.instance_restart_status import (
        collect_restart_status,
        seat_restart_banner,
    )

    ban0 = seat_restart_banner(now=time.time())
    assert ban0["quiet_poll"] is False
    assert ban0["maintenance"]["active"] is False

    mn.set_notice(180, reason="restart_instance")
    ban = seat_restart_banner(now=time.time())
    assert ban["quiet_poll"] is True
    assert ban["maintenance"]["active"] is True
    # 维护预告 ≠ 冷却：不点亮冷却横幅（那是重启完成后的事），只降速+改文案
    assert ban["cooldown_active"] is False
    # ops 卡全量快照同样带 maintenance 键（可观测「quiet 为什么亮」）
    full = collect_restart_status(now=time.time())
    assert full["maintenance"]["active"] is True


# ── 内部路由 ──────────────────────────────────────────────────────────────


def _deny(request):
    raise HTTPException(401, "Unauthorized")


def _mk_app(api_auth):
    from src.web.routes.unified_inbox_realtime_routes import register_realtime_routes

    app = FastAPI()
    register_realtime_routes(app, api_auth=api_auth)
    return app


def test_loopback_helper_semantics():
    from src.web.routes.unified_inbox_realtime_routes import _is_loopback_client

    mk = lambda host: types.SimpleNamespace(client=types.SimpleNamespace(host=host))
    assert _is_loopback_client(mk("127.0.0.1")) is True
    assert _is_loopback_client(mk("::1")) is True
    assert _is_loopback_client(mk("192.168.0.117")) is False
    assert _is_loopback_client(types.SimpleNamespace(client=None)) is False


def test_route_non_loopback_falls_back_to_api_auth():
    # starlette TestClient 的 client.host="testclient"（非 loopback）→ 必走 api_auth
    client = TestClient(_mk_app(api_auth=_deny))
    r = client.post("/api/internal/ops/maintenance-notice", json={"window_sec": 60})
    assert r.status_code == 401
    assert mn.snapshot()["active"] is False  # 拒绝路径绝不落状态


def test_route_sets_state_publishes_event_and_clamps():
    from src.integrations.shared.event_bus import get_event_bus

    bus = get_event_bus()
    q = bus.subscribe()
    try:
        client = TestClient(_mk_app(api_auth=lambda r: None))
        r = client.post(
            "/api/internal/ops/maintenance-notice",
            json={"window_sec": 10**9, "reason": "restart_instance"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert body["maintenance"]["active"] is True
        # clamp 到上限，防手滑把工作台钉在维护态半天
        assert body["maintenance"]["left_sec"] <= mn.MAX_WINDOW_SEC
        assert mn.snapshot()["active"] is True

        got = []
        while not q.empty():
            got.append(q.get_nowait())
        mnt_evts = [e for e in got if e.get("type") == "maintenance_notice"]
        assert mnt_evts, f"expected maintenance_notice on bus, got {[e.get('type') for e in got]}"
        data = mnt_evts[-1].get("data") or {}
        assert data.get("left_sec") and data.get("until_ts")
    finally:
        bus.unsubscribe(q)


def test_route_bad_body_falls_back_to_default_window():
    client = TestClient(_mk_app(api_auth=lambda r: None))
    r = client.post(
        "/api/internal/ops/maintenance-notice",
        content=b"not-json",
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 200
    assert r.json()["maintenance"]["left_sec"] <= mn.DEFAULT_WINDOW_SEC


def test_sse_whitelist_includes_maintenance_notice():
    from src.web.routes.unified_inbox_realtime_routes import (
        _NOTIF_EVENT_TYPES,
        _SSE_EVENT_TYPES,
    )

    assert "maintenance_notice" in _SSE_EVENT_TYPES
    # 瞬态运维事件不进 P24 通知中心（坐席不需要历史里堆维护记录）
    assert "maintenance_notice" not in _NOTIF_EVENT_TYPES


# ── 静态接线（模板热更新直上生产；脚本是链路发起端）────────────────────────


def test_static_wiring_frontend_and_restart_script():
    repo = pathlib.Path(__file__).resolve().parent.parent  # engines/chengjie
    root = pathlib.Path(__file__).resolve().parents[3]  # boundless

    inbox = (repo / "src" / "web" / "templates" / "unified_inbox.html").read_text(
        encoding="utf-8"
    )
    # SSE 分支存在且消费 left_sec/until_ts 双源；维护窗口过期须转红如实报警
    assert "maintenance_notice" in inbox
    assert "until_ts" in inbox
    assert "Date.now() < _rc.until" in inbox

    ps = (root / "deploy" / "instances" / "restart_instance.ps1").read_text(
        encoding="utf-8"
    )
    # 停机前预告播 + 手动重启间隔 + Force 穿越留痕
    assert "/api/internal/ops/maintenance-notice" in ps
    assert "$ManualSpacingMin = 30" in ps
    assert "[forced]" in ps
    # 预告必须发生在 stop 之前（发起端顺序错了整条链失效）
    assert ps.index("/api/internal/ops/maintenance-notice") < ps.index(
        "stopping instance..."
    )


def test_offline_page_probe_has_timeout_and_sw_version_bumped():
    """PWA 离线页探针必须带超时中止（2026-07-31 实锤：重启窗口内端口无监听时
    Windows 防火墙对局域网 SYN 静默丢弃，无超时的 fetch 挂起数分钟 → busy 钉死，
    退避循环/手动重试/回前台补探全部停摆——服务器早已恢复，页面却永远停在
    「正在探测…」）。改 offline.html 必须同步递增 sw.js VERSION，否则老客户端
    继续吃 SHELL_CACHE 里预缓存的旧壳，修了等于没修。"""
    repo = pathlib.Path(__file__).resolve().parent.parent
    pwa = repo / "src" / "web" / "static" / "pwa"
    offline = (pwa / "offline.html").read_text(encoding="utf-8")
    assert "AbortController" in offline
    assert "PROBE_TIMEOUT_MS" in offline
    assert "signal" in offline
    sw = (pwa / "sw.js").read_text(encoding="utf-8")
    m = re.search(r'VERSION\s*=\s*"v(\d+)-', sw)
    assert m and int(m.group(1)) >= 5, "sw.js VERSION 须随 offline.html 改动递增（>=v5）"
