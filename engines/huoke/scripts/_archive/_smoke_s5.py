"""Sprint 5 P0/P1 离线烟雾测试。

覆盖:
- s5_0: risk_auto_heal._cancel_running_on_device 发出 cooperative cancel 信号
- s5_1: tiktok.video_watched / video_liked 经 _emit_event → tt_funnel_events
- s5_2: geo_check._lookup_ip 总超时硬封顶 10s
- 额外: base_automation._AdbUiObject._find TikTok 硬编码路径加 package guard
- 额外: facebook.smart_tap 漂移自愈后自动 retry tap 一次
"""
from __future__ import annotations

import os
import sys
import time
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent  # 仓库根（scripts/ 的上级）
sys.path.insert(0, str(ROOT))


def _banner(name: str):
    print()
    print("=" * 60)
    print(f"[s5] {name}")
    print("=" * 60)


def test_find_package_guard():
    _banner("_AdbUiObject._find package guard")
    from src.app_automation.base_automation import _AdbUiObject

    fake_dev = MagicMock()
    fake_dev._w = 720
    fake_dev._h = 1600
    fake_dev._app_cache = {"pkg": "com.facebook.katana", "ts": time.time()}

    obj = _AdbUiObject(fake_dev, description="Search Facebook")

    def fake_try_xml_match(self):
        return False
    with patch.object(_AdbUiObject, "_try_xml_match", fake_try_xml_match):
        found = obj.exists(timeout=0.1)
    assert found is False, (
        "FB 包下应屏蔽 TikTok 硬编码 fallback, but got True"
    )
    print("  OK: FB 包下 _find 返回 False(guard 生效)")

    fake_dev._app_cache = {"pkg": "com.ss.android.ugc.trill", "ts": time.time()}
    obj2 = _AdbUiObject(fake_dev, description="search")
    with patch.object(_AdbUiObject, "_try_xml_match", fake_try_xml_match):
        found2 = obj2.exists(timeout=0.1)
    assert found2 is True, (
        "TikTok 包下仍应允许硬编码 fallback"
    )
    print("  OK: TikTok 包下 _find 返回 True(硬编码路径保留)")


def test_smart_tap_heal_retry_semantics():
    _banner("facebook.smart_tap 漂移自愈 + retry tap")
    from src.app_automation import facebook as fbmod

    fb = fbmod.FacebookAutomation.__new__(fbmod.FacebookAutomation)

    tap_counter = {"n": 0}
    def fake_parent_tap(self, target_desc, context="", device_id=None):
        tap_counter["n"] += 1
        return True

    heal_counter = {"xspace": 0}
    def fake_xspace(self, d, did):
        heal_counter["xspace"] += 1

    restart_counter = {"n": 0}
    def fake_restart(self, did):
        restart_counter["n"] += 1

    adb_calls = []
    def fake_adb(self, cmd, device_id=None):
        adb_calls.append(cmd)

    dd = MagicMock()
    states = iter([
        {"package": "com.miui.securitycore"},
        {"package": "com.facebook.katana"},
        {"package": "com.facebook.katana"},
        {"package": "com.facebook.katana"},
    ])
    dd.app_current = lambda: next(states, {"package": "com.facebook.katana"})
    dd.invalidate_app_cache = lambda: None

    with patch("src.app_automation.base_automation.BaseAutomation.smart_tap",
               fake_parent_tap), \
         patch.object(fbmod.FacebookAutomation, "_handle_xspace_dialog", fake_xspace), \
         patch.object(fbmod.FacebookAutomation, "_adb_start_main_user", fake_restart), \
         patch.object(fbmod.FacebookAutomation, "_adb", fake_adb), \
         patch.object(fbmod.FacebookAutomation, "_did", lambda self, x: "fakedev"), \
         patch.object(fbmod.FacebookAutomation, "_u2", lambda self, did: dd):
        result = fb.smart_tap("Search bar or search icon", "", "fakedev")

    assert tap_counter["n"] == 2, (
        f"父类 smart_tap 应被调 2 次(原始+retry),实际 {tap_counter['n']}")
    print(f"  OK: 父类 smart_tap 调用 {tap_counter['n']} 次(original + retry)")
    assert heal_counter["xspace"] >= 1, "未执行 XSpace 自愈"
    print(f"  OK: XSpace dialog 自愈 {heal_counter['xspace']} 次")
    print(f"  smart_tap 最终返回: {result}")


def test_cancel_running_on_device():
    _banner("risk_auto_heal._cancel_running_on_device")
    from src.host import risk_auto_heal

    did = f"test_dev_{uuid.uuid4().hex[:8]}"
    fake_task_id = f"task_{uuid.uuid4().hex[:8]}"

    mock_pool = MagicMock()
    mock_pool._active_tasks = {did: fake_task_id}
    mock_pool.cancel_task = MagicMock(return_value=True)

    mock_tasks = [{
        "task_id": fake_task_id,
        "type": "facebook_browse_feed",
        "status": "running",
    }]

    with patch("src.host.task_store.list_tasks", return_value=mock_tasks), \
         patch("src.host.worker_pool.get_worker_pool", return_value=mock_pool):
        core = risk_auto_heal.CrossPlatformRiskHealer.__new__(
            risk_auto_heal.CrossPlatformRiskHealer)
        result = core._cancel_running_on_device(did)

    mock_pool.cancel_task.assert_called_once_with(fake_task_id)
    assert result.get("facebook") == 1, f"期望 facebook=1, 实际 {result}"
    print(f"  OK: cancel_task 被调用 1 次,返回 {result}")


def test_tk_emit_video_watched_via_bus():
    _banner("tiktok._emit_event -> tt_funnel_events (video_watched/liked)")
    db_path = ROOT / "data" / "openclaw.db"
    os.environ["OPENCLAW_DB_PATH"] = str(db_path)

    from src.host import tt_funnel_store
    from src.host.database import init_db
    init_db()

    did = f"test_s5_{uuid.uuid4().hex[:8]}"
    preset = f"preset_s5_{uuid.uuid4().hex[:6]}"

    from src.app_automation import tiktok as tkmod
    fake_tk = MagicMock()
    fake_tk._bus = None
    fake_tk._bus_init_attempted = True

    tkmod.TikTokAutomation._emit_event(
        fake_tk, "tiktok.video_watched",
        device_id=did, phase="interest_building",
        preset_key=preset, watch_sec=6.5, country_target="italy",
    )
    tkmod.TikTokAutomation._emit_event(
        fake_tk, "tiktok.video_liked",
        device_id=did, phase="interest_building",
        preset_key=preset, country_target="italy",
    )

    metrics = tt_funnel_store.get_tt_funnel_metrics(device_id=did, preset_key=preset)
    exposure = metrics.get("stage_exposure", 0)
    interest = metrics.get("stage_interest", 0)
    assert exposure >= 1, f"exposure 期望 ≥1, 实际 {exposure}"
    assert interest >= 1, f"interest 期望 ≥1, 实际 {interest}"
    print(f"  OK: stage_exposure={exposure} stage_interest={interest}")


def test_geo_lookup_timeout_budget():
    _banner("geo_check._lookup_ip 总预算 10s 硬封顶")
    from src.behavior import geo_check

    slow_src = {"name": "slow", "url_tpl": "http://example/{ip}",
                "parse": lambda d: {"country": "Italy", "country_code": "IT"}}

    def slow_query(ip, source, timeout=6):
        time.sleep(8.0)
        return {"country": "Italy", "country_code": "IT", "method": source["name"]}

    t0 = time.time()
    with patch.object(geo_check, "_GEO_SOURCES", [slow_src, slow_src, slow_src]), \
         patch.object(geo_check, "_query_one_source", side_effect=slow_query):
        result = geo_check._lookup_ip("1.1.1.1")
    elapsed = time.time() - t0
    print(f"  elapsed={elapsed:.2f}s result={result}")
    assert elapsed < 11.5, f"应在 ~10s 内返回, 实际 {elapsed:.2f}s"
    print(f"  OK: 硬封顶生效({elapsed:.2f}s < 11.5s)")


if __name__ == "__main__":
    fails = []
    for name, fn in [
        ("find_package_guard", test_find_package_guard),
        ("smart_tap_heal_retry", test_smart_tap_heal_retry_semantics),
        ("cancel_running", test_cancel_running_on_device),
        ("tk_emit_funnel", test_tk_emit_video_watched_via_bus),
        ("geo_budget", test_geo_lookup_timeout_budget),
    ]:
        try:
            fn()
        except Exception as e:
            import traceback
            traceback.print_exc()
            fails.append((name, e))

    print()
    print("=" * 60)
    if fails:
        print(f"[s5] FAIL: {len(fails)}")
        for n, e in fails:
            print(f"  - {n}: {e}")
        sys.exit(1)
    print("[s5] ALL PASSED")
