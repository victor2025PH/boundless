# -*- coding: utf-8 -*-
"""
Sprint 4 P0 离线冒烟测试:
  1. AdbFallbackDevice.dump_hierarchy TTL 缓存命中/失效
  2. send_keys 长文本分段、clear 键序列、dump invalidate 联动
  3. FacebookAutomation.smart_tap 后置自愈触发路径(mock app_current 漂移)
  4. DeviceManager TCP 重连前置逻辑(mock execute_adb_command)

无真机即可全通。
"""
from __future__ import annotations
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.app_automation.base_automation import AdbFallbackDevice  # noqa: E402


class FakeDM:
    """极简 DeviceManager mock, 记录所有 execute_adb_command 调用."""
    def __init__(self):
        self.calls: list[tuple[str, str]] = []
        self.wm_size = "Physical size: 720x1600"

    def execute_adb_command(self, cmd, device_id=None, timeout=None):
        self.calls.append((cmd, device_id))
        if cmd.startswith("shell wm size"):
            return True, self.wm_size
        if cmd.startswith("shell uiautomator dump"):
            return True, ""
        if cmd.startswith("shell cat /sdcard/openclaw_ui.xml"):
            return True, "<hierarchy>...</hierarchy>"
        return True, ""

    def get_u2(self, device_id):
        return None  # 强制走路 2(adb uiautomator dump)


def _print(title, ok, detail=""):
    icon = "PASS" if ok else "FAIL"
    print(f"[{icon}] {title}{(' - ' + detail) if detail else ''}")
    return ok


def test_dump_ttl_cache():
    dm = FakeDM()
    dev = AdbFallbackDevice("FAKE1", dm)
    dm.calls.clear()

    _ = dev.dump_hierarchy()
    first_io = len([c for c in dm.calls if "uiautomator" in c[0] or "cat" in c[0]])

    _ = dev.dump_hierarchy()
    _ = dev.dump_hierarchy()
    _ = dev.dump_hierarchy()
    cached_io = len([c for c in dm.calls if "uiautomator" in c[0] or "cat" in c[0]]) - first_io

    ok = _print("TTL cache hits avoid IO", cached_io == 0,
                f"first_io={first_io} cached_extra_io={cached_io}")

    dev.click(100, 200)
    before = len(dm.calls)
    _ = dev.dump_hierarchy()
    new_io = len([c for c in dm.calls[before:] if "uiautomator" in c[0] or "cat" in c[0]])
    ok2 = _print("click invalidates cache", new_io >= 1,
                 f"new_io_after_click={new_io}")
    return ok and ok2


def test_send_keys_long_text_and_clear():
    dm = FakeDM()
    dev = AdbFallbackDevice("FAKE1", dm)
    dm.calls.clear()

    long_text = "The quick brown fox jumps over the lazy dog " * 3  # ~135 chars
    dev.send_keys(long_text, clear=True)
    cmds = [c[0] for c in dm.calls]

    has_clear_a = any("keyevent 29" in c for c in cmds)
    has_clear_del = any("keyevent 67" in c for c in cmds)
    input_text_pieces = [c for c in cmds if c.startswith('shell input text ')]
    ok1 = _print("clear uses Ctrl+A + DEL", has_clear_a and has_clear_del)
    ok2 = _print("long text is split into multiple pieces",
                 len(input_text_pieces) >= 2,
                 f"pieces={len(input_text_pieces)}")

    dev._dump_cache = {"xml": "<hierarchy/>", "ts": 9e18}
    dev.send_keys("hi", clear=False)
    ok3 = _print("send_keys invalidates dump cache",
                 dev._dump_cache["ts"] == 0.0)
    return ok1 and ok2 and ok3


def test_facebook_smart_tap_heal():
    from src.app_automation import facebook as fb_mod

    events = []

    class FakeD:
        def __init__(self):
            self._call_count = 0
        def invalidate_app_cache(self):
            events.append("invalidate_app_cache")
        def app_current(self):
            events.append("app_current")
            self._call_count += 1
            if self._call_count == 1:
                return {"package": "com.facebook.orca"}
            return {"package": fb_mod.PACKAGE}

    class FakeFB(fb_mod.FacebookAutomation):
        def __init__(self):
            pass
        def _did(self, device_id=None):
            return "FAKE1"
        def _u2(self, device_id=None):
            return FakeD()
        def _handle_xspace_dialog(self, d, did):
            events.append("xspace")
        def _adb(self, cmd, device_id=None, timeout=15):
            events.append(f"adb:{cmd}")
            return ""
        def _adb_start_main_user(self, did):
            events.append("start_main_user")
        @property
        def logger(self):
            class _L:
                def debug(self, *a, **k): pass
                def info(self, *a, **k): pass
                def warning(self, *a, **k): pass
            return _L()

    from src.app_automation import base_automation as base_mod
    orig_super_smart_tap = base_mod.BaseAutomation.smart_tap
    try:
        def fake_super_smart_tap(self, target_desc, context="", device_id=None):
            events.append(f"parent_tap:{target_desc}")
            return True
        base_mod.BaseAutomation.smart_tap = fake_super_smart_tap
        fb = FakeFB()
        ok_ret = fb.smart_tap("Search bar or search icon")
    finally:
        base_mod.BaseAutomation.smart_tap = orig_super_smart_tap

    drift_detected = "xspace" in events
    # 自 Sprint 5 S5_4 起,smart_tap 自愈后会自动 retry 一次;retry 仍失败时
    # 返回 False(而非 True),因此真实业务语义是"业务意图是否达成"。
    # 这里 FakeD 没有模拟 retry 成功,所以 ok_ret 应为 False。
    ok1 = _print("smart_tap returns False after heal+retry both fail",
                 ok_ret is False)
    ok2 = _print("drift triggers _handle_xspace_dialog", drift_detected,
                 f"events={events[:6]}...")
    return ok1 and ok2


def test_tcp_reconnect_logic():
    from src.device_control.device_manager import DeviceInfo, DeviceStatus
    dm_mod = __import__('src.device_control.device_manager', fromlist=['DeviceManager'])

    known_tcp = {
        "192.168.1.100:5555": DeviceInfo(
            device_id="192.168.1.100:5555",
            display_name="test_tcp",
            platform="android",
            status=DeviceStatus.DISCONNECTED,
        ),
    }
    captured = []

    def spy(self, cmd, device_id=None):
        captured.append(cmd)
        if cmd == "devices":
            return True, "List of devices attached\n"
        if cmd.startswith("connect"):
            return True, f"connected to {cmd.split()[-1]}"
        return True, ""

    klass = dm_mod.DeviceManager
    old_exec = klass.execute_adb_command
    try:
        klass.execute_adb_command = spy
        inst = klass.__new__(klass)
        inst.devices = known_tcp
        inst.logger = type("L", (), {
            "debug": lambda *a, **k: None,
            "info": lambda *a, **k: None,
            "warning": lambda *a, **k: None,
        })()
        inst._cluster_role = "standalone"
        inst._discover_cache_time = 0
        inst._discover_cache_result = []
        inst._removed_devices = set()
        inst._last_problem_devices = []
        try:
            inst.discover_devices(force=True)
        except Exception as e:
            print(f"   (expected partial exec, {type(e).__name__}: {e})")
    finally:
        klass.execute_adb_command = old_exec

    connects = [c for c in captured if c.startswith("connect 192.168.1.100")]
    return _print("tcp_reconnect dials adb connect for disconnected",
                  len(connects) >= 1, f"captured[:5]={captured[:5]}")


def main():
    results = []
    print("── Sprint 4 P0 离线冒烟 ────────────────────────────")
    results.append(("dump_ttl", test_dump_ttl_cache()))
    print()
    results.append(("send_keys", test_send_keys_long_text_and_clear()))
    print()
    results.append(("smart_tap_heal", test_facebook_smart_tap_heal()))
    print()
    results.append(("tcp_reconnect", test_tcp_reconnect_logic()))
    print()
    total_ok = all(ok for _, ok in results)
    print("────────────────────────────────────────────────────")
    print(f"Overall: {'PASS' if total_ok else 'FAIL'}")
    for name, ok in results:
        print(f"  {name}: {'OK' if ok else 'FAIL'}")
    return 0 if total_ok else 1


if __name__ == "__main__":
    sys.exit(main())
