# -*- coding: utf-8 -*-
"""
Phase 7 测试套件:
  P0: 代理池管理器（proxy_pool_manager.py）
  P1: 设备状态与发布联动（base_publisher._check_proxy_circuit_breaker）
  P2: MockLocation APK 构建配置（APK 源代码结构验证）
"""
import json
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ═══════════════════════════════════════════════
# Test 1: proxy_pool_manager — 基础池操作
# ═══════════════════════════════════════════════
class TestProxyPoolManager(unittest.TestCase):

    def _make_pool(self, n: int = 3, expired: int = 0) -> list:
        """创建测试代理池数据。"""
        pool = []
        for i in range(n):
            pool.append({
                "proxy_id": f"test_{i:03d}",
                "label": f"test_{i:03d}",
                "server": f"10.0.0.{i+1}",
                "port": 1080,
                "username": "u",
                "password": "p",
                "country": "us",
                "source": "922s5",
                "active": True,
                "expire_time": "2030-01-01T00:00:00Z",
            })
        # 添加过期的
        for i in range(expired):
            pool.append({
                "proxy_id": f"expired_{i:03d}",
                "label": f"expired_{i:03d}",
                "server": f"10.0.1.{i+1}",
                "port": 1080,
                "username": "u",
                "password": "p",
                "country": "us",
                "source": "922s5",
                "active": True,
                "expire_time": "2020-01-01T00:00:00Z",  # 已过期
            })
        return pool

    def test_get_pool_stats_empty(self):
        """空池返回正确统计。"""
        import src.device_control.proxy_pool_manager as pm
        with patch.object(pm, 'load_pool', return_value=[]):
            stats = pm.get_pool_stats()
            self.assertEqual(stats["total"], 0)
            self.assertEqual(stats["active"], 0)
            self.assertTrue(stats["needs_attention"])
        print("  [OK] 空池统计正确")

    def test_get_pool_stats_with_proxies(self):
        """含代理时统计正确，包括过期识别。"""
        import src.device_control.proxy_pool_manager as pm
        pool = self._make_pool(n=4, expired=2)
        with patch.object(pm, 'load_pool', return_value=pool):
            stats = pm.get_pool_stats()
            self.assertEqual(stats["total"], 6)
            self.assertEqual(stats["active"], 4)
            self.assertEqual(stats["expired"], 2)
            self.assertIn("us", stats["by_country"])
            self.assertIn("922s5", stats["by_source"])
        print("  [OK] 含代理池统计正确（过期识别）")

    def test_get_available_proxies_filters_expired(self):
        """get_available_proxies 应过滤过期代理。"""
        import src.device_control.proxy_pool_manager as pm
        pool = self._make_pool(n=2, expired=1)
        with patch.object(pm, 'load_pool', return_value=pool):
            available = pm.get_available_proxies()
            self.assertEqual(len(available), 2, "过期代理应被过滤")
        print("  [OK] get_available_proxies 正确过滤过期代理")

    def test_get_available_proxies_filters_by_country(self):
        """国家过滤正确工作。"""
        import src.device_control.proxy_pool_manager as pm
        pool = self._make_pool(n=3)
        pool[0]["country"] = "jp"  # 一个日本代理
        with patch.object(pm, 'load_pool', return_value=pool):
            us_only = pm.get_available_proxies(country="us")
            jp_only = pm.get_available_proxies(country="jp")
            self.assertEqual(len(us_only), 2)
            self.assertEqual(len(jp_only), 1)
        print("  [OK] get_available_proxies 国家过滤正确")

    def test_get_available_proxies_excludes_blacklist(self):
        """黑名单代理应被排除。"""
        import src.device_control.proxy_pool_manager as pm
        pool = self._make_pool(n=3)
        blacklist = ["test_000", "test_001"]
        with patch.object(pm, 'load_pool', return_value=pool):
            available = pm.get_available_proxies(exclude_ids=blacklist)
            self.assertEqual(len(available), 1)
            self.assertNotIn(available[0]["proxy_id"], blacklist)
        print("  [OK] get_available_proxies 黑名单排除正确")

    def test_cleanup_expired_marks_inactive(self):
        """cleanup_expired 应将过期代理标记为 inactive。"""
        import src.device_control.proxy_pool_manager as pm
        pool = self._make_pool(n=2, expired=2)
        saved = []
        with patch.object(pm, 'load_pool', return_value=pool), \
             patch.object(pm, 'save_pool', side_effect=saved.append):
            count = pm.cleanup_expired()
            self.assertEqual(count, 2, "应标记2个过期代理")
            # 验证过期代理被标记为 inactive
            if saved:
                for p in saved[0]:
                    if "2020" in p.get("expire_time", ""):
                        self.assertFalse(p.get("active", True),
                                         "过期代理应标记为 inactive")
        print("  [OK] cleanup_expired 正确标记过期代理")


# ═══════════════════════════════════════════════
# Test 2: proxy_pool_manager — 922S5 同步
# ═══════════════════════════════════════════════
class TestProxyPoolSync(unittest.TestCase):

    def test_sync_from_922s5_success(self):
        """成功同步时应添加新代理。"""
        import src.device_control.proxy_pool_manager as pm
        from src.device_control.proxy_922s5 import Proxy922S5Info

        # 直接测试 run_proxy_pool_sync 集成流程（mock sync_from_922s5 避免真实API调用）
        with patch.object(pm, 'sync_from_922s5', return_value={"fetched": 1, "added": 1, "skipped": 0, "error": None}), \
             patch.object(pm, 'cleanup_expired', return_value=0), \
             patch.object(pm, 'check_balance_and_alert', return_value=None), \
             patch.object(pm, 'get_pool_stats', return_value={"active": 5, "total": 5,
                          "expired": 0, "by_country": {}, "by_source": {},
                          "needs_attention": False}):
            result = pm.run_proxy_pool_sync({"sync": True, "cleanup": True, "check_balance": False})
            self.assertTrue(result.get("ok"))
            self.assertEqual(result["sync_result"]["added"], 1)
        print("  [OK] run_proxy_pool_sync 同步流程正确")

    def test_sync_skips_duplicates(self):
        """重复的代理（相同 proxy_id 或 server:port）不应被添加。"""
        import src.device_control.proxy_pool_manager as pm
        from src.device_control.proxy_922s5 import Proxy922S5Info

        existing_pool = [
            {"proxy_id": "exist_001", "server": "1.1.1.1", "port": 1080,
             "source": "922s5", "label": "x", "active": True}
        ]
        new_proxies = [
            Proxy922S5Info({
                "proxyId": "exist_001", "host": "1.1.1.1", "port": 1080,
                "username": "u", "password": "p", "country": "US",
            })
        ]

        mock_client = MagicMock()
        mock_client.list_proxies.return_value = new_proxies

        # get_922s5_client 在函数内部 import，需要 patch 源模块
        import src.device_control.proxy_922s5 as p922_src
        original_client = getattr(p922_src, 'get_922s5_client', None)
        p922_src.get_922s5_client = MagicMock(return_value=mock_client)

        with patch.object(pm, 'load_pool', return_value=existing_pool), \
             patch.object(pm, 'save_pool') as mock_save:
            try:
                result = pm.sync_from_922s5()
                self.assertEqual(result["added"], 0, "重复代理不应添加")
                self.assertEqual(result["skipped"], 1)
                mock_save.assert_not_called()  # 没有添加，不应保存
            finally:
                if original_client:
                    p922_src.get_922s5_client = original_client
        print("  [OK] sync_from_922s5 去重机制正确")

    def test_replenish_triggered_when_low(self):
        """可用代理不足时应触发补货。"""
        import src.device_control.proxy_pool_manager as pm
        import src.device_control.proxy_922s5 as p922_src

        mock_replenish = MagicMock(return_value={"ok": True, "purchased": 2, "pool_size": 5})
        # replenish_proxy_pool 在函数内部 import，patch 源模块属性
        original_replenish = getattr(p922_src, 'replenish_proxy_pool', None)
        p922_src.replenish_proxy_pool = mock_replenish

        try:
            with patch.object(pm, 'sync_from_922s5', return_value={"added": 0, "error": None}), \
                 patch.object(pm, 'cleanup_expired', return_value=0), \
                 patch.object(pm, 'check_balance_and_alert', return_value=None), \
                 patch.object(pm, 'get_pool_stats', return_value={
                     "active": 1, "total": 1, "expired": 0,
                     "by_country": {}, "by_source": {}, "needs_attention": True
                 }):
                result = pm.run_proxy_pool_sync({"min_pool_size": 3})
                self.assertIn("replenish", result)
        finally:
            if original_replenish:
                p922_src.replenish_proxy_pool = original_replenish
        print("  [OK] 代理不足时触发自动补货")


# ═══════════════════════════════════════════════
# Test 3: _check_proxy_circuit_breaker — 发布前熔断检查
# ═══════════════════════════════════════════════
class TestPublisherCircuitBreakerCheck(unittest.TestCase):

    def _make_status(self, state: str, circuit_open: bool = False,
                     consecutive_fails: int = 0, circuit_open_time: float = 0):
        """创建模拟 DeviceProxyStatus（用 MagicMock 避免 dataclass 默认值问题）。"""
        status = MagicMock()
        status.state = state
        status.circuit_open = circuit_open
        status.consecutive_fails = consecutive_fails
        status.circuit_open_time = circuit_open_time
        status.actual_ip = "1.2.3.4"
        status.expected_ip = "1.2.3.4"
        return status

    def test_ok_state_allows_publish(self):
        """state=ok 时应允许发布。"""
        from src.studio.publishers.base_publisher import _check_proxy_circuit_breaker

        mock_status = self._make_status("ok", circuit_open=False)
        mock_monitor = MagicMock()
        mock_monitor._status_cache = {"dev_ok": mock_status}
        mock_monitor._status_lock = __import__('threading').Lock()

        with patch('src.behavior.proxy_health.get_proxy_health_monitor',
                   return_value=mock_monitor):
            result = _check_proxy_circuit_breaker("dev_ok")
            self.assertFalse(result["blocked"])
            self.assertEqual(result["state"], "ok")
        print("  [OK] state=ok 允许发布")

    def test_unverified_state_allows_publish(self):
        """state=unverified 时应允许发布（降级策略）。"""
        from src.studio.publishers.base_publisher import _check_proxy_circuit_breaker

        mock_status = self._make_status("unverified", circuit_open=False)
        mock_monitor = MagicMock()
        mock_monitor._status_cache = {"dev_unverified": mock_status}
        mock_monitor._status_lock = __import__('threading').Lock()

        with patch('src.behavior.proxy_health.get_proxy_health_monitor',
                   return_value=mock_monitor):
            result = _check_proxy_circuit_breaker("dev_unverified")
            self.assertFalse(result["blocked"])
        print("  [OK] state=unverified 允许发布（降级策略）")

    def test_circuit_open_blocks_publish(self):
        """熔断器打开时应阻止发布。"""
        from src.studio.publishers.base_publisher import _check_proxy_circuit_breaker

        mock_status = self._make_status(
            "leak", circuit_open=True,
            consecutive_fails=3, circuit_open_time=time.time() - 100
        )
        mock_monitor = MagicMock()
        mock_monitor._status_cache = {"dev_breaker": mock_status}
        mock_monitor._status_lock = __import__('threading').Lock()

        with patch('src.behavior.proxy_health.get_proxy_health_monitor',
                   return_value=mock_monitor):
            result = _check_proxy_circuit_breaker("dev_breaker")
            self.assertTrue(result["blocked"])
            self.assertTrue(result["circuit_open"])
            self.assertIn("熔断", result["reason"])
            self.assertGreater(result["cooldown_remaining"], 0)
        print("  [OK] 熔断器打开时阻止发布")

    def test_leak_state_blocks_publish(self):
        """state=leak（IP泄漏）时应阻止发布。"""
        from src.studio.publishers.base_publisher import _check_proxy_circuit_breaker

        mock_status = self._make_status("leak", circuit_open=False)
        mock_status.actual_ip = "9.9.9.9"
        mock_status.expected_ip = "1.2.3.4"
        mock_monitor = MagicMock()
        mock_monitor._status_cache = {"dev_leak": mock_status}
        mock_monitor._status_lock = __import__('threading').Lock()

        with patch('src.behavior.proxy_health.get_proxy_health_monitor',
                   return_value=mock_monitor):
            result = _check_proxy_circuit_breaker("dev_leak")
            self.assertTrue(result["blocked"])
            self.assertIn("泄漏", result["reason"])
        print("  [OK] state=leak 阻止发布")

    def test_no_ip_state_blocks_publish(self):
        """state=no_ip 时应阻止发布。"""
        from src.studio.publishers.base_publisher import _check_proxy_circuit_breaker

        mock_status = self._make_status("no_ip", circuit_open=False)
        mock_monitor = MagicMock()
        mock_monitor._status_cache = {"dev_no_ip": mock_status}
        mock_monitor._status_lock = __import__('threading').Lock()

        with patch('src.behavior.proxy_health.get_proxy_health_monitor',
                   return_value=mock_monitor):
            result = _check_proxy_circuit_breaker("dev_no_ip")
            self.assertTrue(result["blocked"])
            self.assertIn("no_ip", result["state"])
        print("  [OK] state=no_ip 阻止发布")

    def test_unregistered_device_allows_publish(self):
        """未注册到监控系统的设备应允许发布（监控可能未启动）。"""
        from src.studio.publishers.base_publisher import _check_proxy_circuit_breaker

        mock_monitor = MagicMock()
        mock_monitor._status_cache = {}  # 设备不在缓存中
        mock_monitor._status_lock = __import__('threading').Lock()

        with patch('src.behavior.proxy_health.get_proxy_health_monitor',
                   return_value=mock_monitor):
            result = _check_proxy_circuit_breaker("unregistered_device")
            self.assertFalse(result["blocked"])
            self.assertEqual(result["state"], "unknown")
        print("  [OK] 未注册设备允许发布")

    def test_import_error_allows_publish(self):
        """proxy_health 不可用时应安全降级（允许发布）。"""
        from src.studio.publishers.base_publisher import _check_proxy_circuit_breaker

        with patch.dict('sys.modules', {'src.behavior.proxy_health': None}):
            try:
                result = _check_proxy_circuit_breaker("any_device")
                # ImportError 应被捕获，返回允许发布
                self.assertFalse(result.get("blocked", True))
            except Exception:
                # 如果抛出任何异常，测试失败
                self.fail("ImportError 应被安全降级处理，不应抛出异常")
        print("  [OK] proxy_health 不可用时安全降级")

    def test_exception_allows_publish(self):
        """任何未预期异常都应降级为允许发布。"""
        from src.studio.publishers.base_publisher import _check_proxy_circuit_breaker

        with patch('src.behavior.proxy_health.get_proxy_health_monitor',
                   side_effect=RuntimeError("unexpected error")):
            result = _check_proxy_circuit_breaker("any_device")
            self.assertFalse(result["blocked"])
            self.assertEqual(result["state"], "error")
        print("  [OK] 未预期异常时安全降级")


# ═══════════════════════════════════════════════
# Test 4: MockLocation APK 源代码结构验证
# ═══════════════════════════════════════════════
class TestMockLocationAPKStructure(unittest.TestCase):

    _PROJECT_ROOT = Path(__file__).resolve().parent.parent

    def test_manifest_exists(self):
        """AndroidManifest.xml 应存在且包含正确包名。"""
        manifest = self._PROJECT_ROOT / "tools/mock_location_helper/apk_src/AndroidManifest.xml"
        self.assertTrue(manifest.exists(), "AndroidManifest.xml 不存在")
        content = manifest.read_text(encoding="utf-8")
        self.assertIn("com.openclaw.mocklocation", content)
        self.assertIn("MockLocationReceiver", content)
        self.assertIn("ACCESS_MOCK_LOCATION", content)
        print("  [OK] AndroidManifest.xml 结构正确")

    def test_receiver_java_exists(self):
        """MockLocationReceiver.java 应存在且包含关键实现。"""
        java = self._PROJECT_ROOT / "tools/mock_location_helper/apk_src/MockLocationReceiver.java"
        self.assertTrue(java.exists(), "MockLocationReceiver.java 不存在")
        content = java.read_text(encoding="utf-8")
        self.assertIn("class MockLocationReceiver", content)
        self.assertIn("setTestProviderLocation", content)
        self.assertIn("latitude", content)
        self.assertIn("longitude", content)
        self.assertIn("com.openclaw.SET_MOCK_LOCATION", content)
        print("  [OK] MockLocationReceiver.java 实现正确")

    def test_service_java_exists(self):
        """MockLocationService.java 应存在（持续位置维持）。"""
        java = self._PROJECT_ROOT / "tools/mock_location_helper/apk_src/MockLocationService.java"
        self.assertTrue(java.exists(), "MockLocationService.java 不存在")
        content = java.read_text(encoding="utf-8")
        self.assertIn("class MockLocationService", content)
        self.assertIn("setTestProviderLocation", content)
        self.assertIn("START_STICKY", content)
        print("  [OK] MockLocationService.java 实现正确")

    def test_build_script_exists(self):
        """build.py 应存在且可解析。"""
        build = self._PROJECT_ROOT / "tools/mock_location_helper/build.py"
        self.assertTrue(build.exists(), "build.py 不存在")
        content = build.read_text(encoding="utf-8")
        self.assertIn("def build(", content)
        self.assertIn("def install(", content)
        self.assertIn("aapt", content)
        self.assertIn("d8", content)
        self.assertIn("apksigner", content)
        print("  [OK] build.py 构建脚本结构正确")

    def test_openclaw_apk_highest_priority(self):
        """OpenClaw 自建 APK 应在已知应用列表的最高优先级。"""
        from src.device_control.mock_location_manager import _KNOWN_MOCK_APPS
        first_pkg = _KNOWN_MOCK_APPS[0][0]
        self.assertEqual(first_pkg, "com.openclaw.mocklocation",
                         "OpenClaw MockLoc 应是第一优先级")
        print("  [OK] OpenClaw MockLoc 在已知应用列表最高优先级")

    def test_recommended_apk_points_to_openclaw(self):
        """_RECOMMENDED_APK 应指向 OpenClaw 自建 APK。"""
        from src.device_control.mock_location_manager import _RECOMMENDED_APK
        self.assertEqual(_RECOMMENDED_APK["package"], "com.openclaw.mocklocation")
        self.assertEqual(_RECOMMENDED_APK["filename"], "openclaw_mock_location.apk")
        print("  [OK] _RECOMMENDED_APK 指向 OpenClaw 自建 APK")

    def test_apks_dir_created(self):
        """config/apks/ 目录应已创建（runtime APK 投放目录，由 .gitkeep 占位）。"""
        apks_dir = self._PROJECT_ROOT / "config/apks"
        self.assertTrue(apks_dir.exists(), "config/apks/ 目录未创建")
        print("  [OK] config/apks/ 目录已创建")


# ═══════════════════════════════════════════════
# Test 5: proxy_pool_manager — normalize 和合并视图
# ═══════════════════════════════════════════════
class TestProxyPoolNormalize(unittest.TestCase):

    def test_normalize_pool_entry(self):
        """_normalize_pool_entry 应统一字段名。"""
        from src.device_control.proxy_pool_manager import _normalize_pool_entry
        # 测试 proxy_id 格式
        entry1 = _normalize_pool_entry({
            "proxy_id": "abc123",
            "label": "test_label",
            "type": "socks5",
            "server": "1.2.3.4",
            "port": 1080,
            "username": "user",
            "password": "pass",
            "country": "us",
            "source": "922s5",
        })
        self.assertEqual(entry1["id"], "abc123")
        self.assertEqual(entry1["server"], "1.2.3.4")

        # 测试备选字段名
        entry2 = _normalize_pool_entry({
            "id": "alt_id",
            "host": "5.6.7.8",  # 备选
            "port": 9999,
            "user": "u",
            "pass": "p",
            "protocol": "http",
        })
        self.assertEqual(entry2["id"], "alt_id")
        self.assertEqual(entry2["server"], "5.6.7.8")
        self.assertEqual(entry2["type"], "http")
        print("  [OK] _normalize_pool_entry 字段标准化正确")

    def test_merged_pool_deduplicates(self):
        """get_merged_proxy_pool 应去除重复的 server:port。"""
        from src.device_control.proxy_pool_manager import get_merged_proxy_pool

        fake_s5 = [
            {"proxy_id": "s5_001", "label": "s5_001", "server": "1.1.1.1",
             "port": 1080, "country": "us", "source": "922s5",
             "active": True, "expire_time": "2030-01-01T00:00:00Z",
             "username": "u", "password": "p", "type": "socks5"},
        ]
        fake_manual = [
            {"server": "1.1.1.1", "port": 1080, "label": "manual_dup",
             "type": "socks5"},  # 与 922s5 重复
            {"server": "2.2.2.2", "port": 1080, "label": "manual_unique",
             "type": "socks5"},  # 不重复
        ]

        import src.device_control.proxy_pool_manager as pm
        with patch.object(pm, 'get_available_proxies', return_value=fake_s5), \
             patch.object(pm, '_load_manual_pool', return_value=fake_manual):
            merged = get_merged_proxy_pool()
            servers = [(p["server"], p["port"]) for p in merged]
            # 不应有重复
            self.assertEqual(len(servers), len(set(servers)))
            self.assertEqual(len(merged), 2)  # 1 + 1（去除重复的manual）
        print("  [OK] get_merged_proxy_pool 去重正确")


if __name__ == "__main__":
    print("=" * 60)
    print("Phase 7 测试套件")
    print("=" * 60)

    loader = unittest.TestLoader()
    test_classes = [
        TestProxyPoolManager,
        TestProxyPoolSync,
        TestPublisherCircuitBreakerCheck,
        TestMockLocationAPKStructure,
        TestProxyPoolNormalize,
    ]
    suites = [loader.loadTestsFromTestCase(c) for c in test_classes]
    all_tests = unittest.TestSuite(suites)
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(all_tests)
    sys.exit(0 if result.wasSuccessful() else 1)
