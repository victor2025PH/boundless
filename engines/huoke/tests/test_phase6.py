# -*- coding: utf-8 -*-
"""
Phase 6 P0 验证测试:
  1. MockLocation 多APP适配器 — 包名扫描 / 坐标映射 / 缓存机制
  2. 922S5 代理 API 客户端 — 响应解析 / 池同步 / 补货逻辑
  3. proxy_health.set_device_gps — 路由到新适配器

注意: 不需要真实设备或 922S5 账户，全部使用 Mock/Patch。
"""
import json
import sys
import time
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, call

# 添加项目根目录
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ═══════════════════════════════════════════════
# Test 1: mock_location_manager — 坐标映射
# ═══════════════════════════════════════════════
class TestMockLocationCoords(unittest.TestCase):

    def test_known_countries(self):
        """已知国家应该返回正确坐标。"""
        from src.device_control.mock_location_manager import get_country_gps_for_mock
        cases = [
            ("us", 40.7128, -74.0060),
            ("uk", 51.5074, -0.1278),
            ("japan", 35.6762, 139.6503),
            ("korea", 37.5665, 126.9780),
            ("brazil", -23.5505, -46.6333),
            ("singapore", 1.3521, 103.8198),
        ]
        for country, exp_lat, exp_lon in cases:
            coords = get_country_gps_for_mock(country)
            self.assertIsNotNone(coords, f"国家 {country} 应有坐标")
            self.assertAlmostEqual(coords[0], exp_lat, places=2, msg=f"{country} lat")
            self.assertAlmostEqual(coords[1], exp_lon, places=2, msg=f"{country} lon")
        print("  [OK] 已知国家坐标映射正确")

    def test_alias_countries(self):
        """别名（gb/us/de等简码）也应返回坐标。"""
        from src.device_control.mock_location_manager import get_country_gps_for_mock
        self.assertIsNotNone(get_country_gps_for_mock("gb"))
        self.assertIsNotNone(get_country_gps_for_mock("de"))
        self.assertIsNotNone(get_country_gps_for_mock("fr"))
        self.assertIsNotNone(get_country_gps_for_mock("au"))
        print("  [OK] 国家别名坐标映射正确")

    def test_unknown_country(self):
        """未知国家应返回 None。"""
        from src.device_control.mock_location_manager import get_country_gps_for_mock
        self.assertIsNone(get_country_gps_for_mock("xx"))
        self.assertIsNone(get_country_gps_for_mock("zz_invalid"))
        print("  [OK] 未知国家返回 None")

    def test_case_insensitive(self):
        """国家代码不区分大小写。"""
        from src.device_control.mock_location_manager import get_country_gps_for_mock
        self.assertEqual(get_country_gps_for_mock("US"), get_country_gps_for_mock("us"))
        self.assertEqual(get_country_gps_for_mock("JAPAN"), get_country_gps_for_mock("japan"))
        print("  [OK] 国家代码不区分大小写")


# ═══════════════════════════════════════════════
# Test 2: mock_location_manager — 缓存机制
# ═══════════════════════════════════════════════
class TestMockLocationCache(unittest.TestCase):

    def setUp(self):
        # 每个测试前清除内存缓存
        import src.device_control.mock_location_manager as mlm
        with mlm._cache_lock:
            mlm._device_app_cache.clear()

    def test_cache_hit_avoids_scan(self):
        """缓存命中时不应重新扫描。"""
        import src.device_control.mock_location_manager as mlm
        fake_app = {
            "package": "com.lexa.fakegps",
            "provider": "com.lexa.fakegps.FakeLocationProvider",
            "intent_action": "com.lexa.fakegps.FAKE_LOCATION",
            "description": "Fake GPS Location - Lexa",
            "source": "cache_test",
        }
        # 预填缓存
        with mlm._cache_lock:
            mlm._device_app_cache["test_device_001"] = fake_app

        with patch.object(mlm, '_get_mock_app_setting', return_value="") as mock_settings, \
             patch.object(mlm, '_is_package_installed', return_value=False) as mock_pm:
            result = mlm.scan_mock_apps("test_device_001")
            # 缓存命中，不应调用 pm list packages
            mock_pm.assert_not_called()
            self.assertEqual(result["package"], "com.lexa.fakegps")
        print("  [OK] 缓存命中时跳过包名扫描")

    def test_cache_clear(self):
        """清除缓存后应重新扫描。"""
        import src.device_control.mock_location_manager as mlm
        with mlm._cache_lock:
            mlm._device_app_cache["device_clear_test"] = {"package": "fake"}

        mlm.clear_device_cache("device_clear_test")

        with mlm._cache_lock:
            self.assertNotIn("device_clear_test", mlm._device_app_cache)
        print("  [OK] 设备缓存清除正常")

    def test_force_rescan_bypasses_cache(self):
        """force_rescan=True 应绕过缓存。"""
        import src.device_control.mock_location_manager as mlm
        with mlm._cache_lock:
            mlm._device_app_cache["device_force"] = {"package": "cached_app"}

        with patch.object(mlm, '_get_mock_app_setting', return_value="") as mock_settings, \
             patch.object(mlm, '_is_package_installed', return_value=False) as mock_pm:
            result = mlm.scan_mock_apps("device_force", force_rescan=True)
            # 强制重扫，应该调用 pm list packages
            self.assertTrue(mock_pm.called)
            self.assertIsNone(result)  # 没找到应用
        print("  [OK] force_rescan 绕过缓存")


# ═══════════════════════════════════════════════
# Test 3: mock_location_manager — Intent 发送
# ═══════════════════════════════════════════════
class TestMockLocationIntent(unittest.TestCase):

    def test_set_location_success(self):
        """当 Intent 广播成功时应返回 True。"""
        import src.device_control.mock_location_manager as mlm
        # 预填缓存
        with mlm._cache_lock:
            mlm._device_app_cache["dev_intent"] = {
                "package": "com.lexa.fakegps",
                "provider": "provider",
                "intent_action": "com.lexa.fakegps.FAKE_LOCATION",
                "description": "Test App",
            }

        def fake_adb(serial, *args, **kwargs):
            cmd_str = " ".join(args)
            if "am broadcast" in cmd_str:
                return "Broadcast completed: result=0", 0
            return "", 0

        with patch.object(mlm, '_adb', side_effect=fake_adb):
            ok = mlm.set_mock_location("dev_intent", 40.71, -74.00)
            self.assertTrue(ok)
        print("  [OK] Intent 广播成功时返回 True")

    def test_set_location_no_app(self):
        """未找到应用时应返回 False。"""
        import src.device_control.mock_location_manager as mlm
        with mlm._cache_lock:
            mlm._device_app_cache.pop("dev_no_app", None)

        with patch.object(mlm, '_get_mock_app_setting', return_value=""), \
             patch.object(mlm, '_is_package_installed', return_value=False):
            ok = mlm.set_mock_location("dev_no_app", 40.71, -74.00)
            self.assertFalse(ok)
        print("  [OK] 无 MockLocation 应用时返回 False")

    def test_known_apps_count(self):
        """已知应用列表应有足够多的条目。"""
        from src.device_control.mock_location_manager import _KNOWN_MOCK_APPS
        self.assertGreaterEqual(len(_KNOWN_MOCK_APPS), 15,
                                f"应知道至少15个MockLocation应用，当前只有{len(_KNOWN_MOCK_APPS)}个")
        # 确保每个条目有正确结构
        for pkg, provider, action, desc in _KNOWN_MOCK_APPS:
            self.assertTrue(pkg.startswith("com.") or pkg.startswith("de.") or
                            pkg.startswith("net.") or pkg.startswith("uk.") or
                            pkg.startswith("cn."),
                            f"包名格式异常: {pkg}")
            self.assertIsNotNone(action)
        print(f"  [OK] 已知MockLocation应用列表: {len(_KNOWN_MOCK_APPS)} 个")


# ═══════════════════════════════════════════════
# Test 4: 922S5 响应解析
# ═══════════════════════════════════════════════
class TestProxy922S5Parsing(unittest.TestCase):

    def test_proxy_info_parsing(self):
        """Proxy922S5Info 应正确解析 API 返回的数据。"""
        from src.device_control.proxy_922s5 import Proxy922S5Info
        data = {
            "proxyId": "abc123",
            "host": "192.168.1.100",
            "port": 1080,
            "username": "user1",
            "password": "pass1",
            "protocol": "socks5",
            "country": "US",
            "city": "New York",
            "expireTime": "2026-12-31T00:00:00Z",
            "status": "active",
        }
        info = Proxy922S5Info(data)
        self.assertEqual(info.proxy_id, "abc123")
        self.assertEqual(info.server, "192.168.1.100")
        self.assertEqual(info.port, 1080)
        self.assertEqual(info.username, "user1")
        self.assertEqual(info.country, "US")
        self.assertEqual(info.protocol, "socks5")
        print("  [OK] Proxy922S5Info 解析正确")

    def test_proxy_info_to_pool_entry(self):
        """to_pool_entry() 应返回正确的池条目格式。"""
        from src.device_control.proxy_922s5 import Proxy922S5Info
        info = Proxy922S5Info({
            "proxyId": "xyz789",
            "host": "10.0.0.1",
            "port": 10800,
            "username": "u",
            "password": "p",
            "protocol": "http",
            "country": "JP",
            "city": "Tokyo",
        })
        entry = info.to_pool_entry()
        self.assertIn("server", entry)
        self.assertIn("port", entry)
        self.assertIn("source", entry)
        self.assertEqual(entry["source"], "922s5")
        self.assertEqual(entry["server"], "10.0.0.1")
        self.assertIn("jp", entry["label"].lower())
        print("  [OK] Proxy922S5Info.to_pool_entry() 格式正确")

    def test_proxy_info_alternate_fields(self):
        """兼容备选字段名（id/host/user等不同API版本格式）。"""
        from src.device_control.proxy_922s5 import Proxy922S5Info
        data = {
            "id": "alt_id_001",  # 备选字段名
            "server": "proxy.example.com",  # 备选字段名
            "port": 9999,
            "user": "testuser",  # 备选字段名
            "pass": "testpass",  # 备选字段名
            "countryCode": "DE",
        }
        info = Proxy922S5Info(data)
        self.assertEqual(info.proxy_id, "alt_id_001")
        self.assertEqual(info.server, "proxy.example.com")
        self.assertEqual(info.country, "DE")
        print("  [OK] Proxy922S5Info 兼容备选字段名")


# ═══════════════════════════════════════════════
# Test 5: 922S5 池同步
# ═══════════════════════════════════════════════
class TestProxy922S5PoolSync(unittest.TestCase):

    def test_sync_adds_new_proxies(self):
        """sync_proxies_to_pool 应添加新代理、跳过已存在的。"""
        from src.device_control.proxy_922s5 import Proxy922S5Info, sync_proxies_to_pool

        fake_pool = [
            {"server": "1.1.1.1", "port": 1080, "proxy_id": "exist_001",
             "source": "922s5", "label": "existing"}
        ]
        new_proxies = [
            Proxy922S5Info({"proxyId": "exist_001", "host": "1.1.1.1", "port": 1080,
                            "username": "u", "password": "p", "country": "US"}),
            Proxy922S5Info({"proxyId": "new_002", "host": "2.2.2.2", "port": 1080,
                            "username": "u2", "password": "p2", "country": "JP"}),
        ]

        saved_pool = []

        def fake_load():
            return fake_pool

        def fake_save(pool):
            saved_pool.extend(pool)

        import src.device_control.proxy_922s5 as p922
        with patch.object(p922, '_load_pool', side_effect=fake_load), \
             patch.object(p922, '_save_pool', side_effect=fake_save):
            added = sync_proxies_to_pool(new_proxies)
            self.assertEqual(added, 1, "应只添加1个新代理（跳过已存在的）")
        print("  [OK] sync_proxies_to_pool: 新代理添加正确，重复跳过")

    def test_sync_dedup_by_server_port(self):
        """相同 server:port 不同 proxy_id 也不应重复添加。"""
        from src.device_control.proxy_922s5 import Proxy922S5Info, sync_proxies_to_pool

        fake_pool = [
            {"server": "3.3.3.3", "port": 1080, "proxy_id": "old_id",
             "source": "922s5", "label": "x"}
        ]
        new_proxies = [
            Proxy922S5Info({"proxyId": "new_id_diff", "host": "3.3.3.3", "port": 1080,
                            "username": "u", "password": "p", "country": "US"}),
        ]

        import src.device_control.proxy_922s5 as p922
        with patch.object(p922, '_load_pool', return_value=fake_pool), \
             patch.object(p922, '_save_pool'):
            added = sync_proxies_to_pool(new_proxies)
            self.assertEqual(added, 0, "同 server:port 不应重复添加")
        print("  [OK] sync_proxies_to_pool: server:port 去重正确")


# ═══════════════════════════════════════════════
# Test 6: 922S5 补货逻辑
# ═══════════════════════════════════════════════
class TestProxy922S5Replenish(unittest.TestCase):

    def test_no_replenish_needed(self):
        """代理充足时不触发补货。"""
        import src.device_control.proxy_922s5 as p922
        fake_pool = [
            {"server": f"{i}.0.0.1", "port": 1080, "label": f"proxy{i}"}
            for i in range(5)
        ]
        with patch.object(p922, '_load_pool', return_value=fake_pool), \
             patch.object(p922, 'load_922s5_config', return_value={
                 "min_pool_size": 3, "auto_replenish": True
             }):
            result = p922.check_and_replenish()
            self.assertFalse(result["needs_replenish"])
            self.assertEqual(result["action"], "skipped")
        print("  [OK] 代理充足时不触发补货")

    def test_replenish_triggered_when_low(self):
        """代理不足时应触发补货。"""
        import src.device_control.proxy_922s5 as p922

        mock_client = MagicMock()
        from src.device_control.proxy_922s5 import Proxy922S5Info
        mock_client.get_balance.return_value = {"balance": 50.0, "currency": "USD"}
        mock_client.buy_proxies.return_value = [
            Proxy922S5Info({"proxyId": "new_p1", "host": "5.5.5.5", "port": 1080,
                            "username": "u", "password": "p", "country": "US"}),
        ]

        fake_pool = [{"server": "1.1.1.1", "port": 1080, "label": "only_one"}]

        with patch.object(p922, '_load_pool', return_value=fake_pool), \
             patch.object(p922, 'load_922s5_config', return_value={
                 "min_pool_size": 3, "auto_replenish": True,
                 "preferred_countries": ["US"],
                 "low_balance_threshold": 5.0,
             }), \
             patch.object(p922, 'get_922s5_client', return_value=mock_client), \
             patch.object(p922, 'sync_proxies_to_pool', return_value=1) as mock_sync:
            result = p922.check_and_replenish()
            self.assertTrue(result["needs_replenish"])
            mock_client.buy_proxies.assert_called()
            mock_sync.assert_called()
        print("  [OK] 代理不足时触发购买并同步")

    def test_dry_run_mode(self):
        """dry_run 模式下不实际购买。"""
        import src.device_control.proxy_922s5 as p922
        with patch.object(p922, '_load_pool', return_value=[]), \
             patch.object(p922, 'load_922s5_config', return_value={
                 "min_pool_size": 3, "auto_replenish": True
             }), \
             patch.object(p922, 'get_922s5_client') as mock_get_client:
            result = p922.check_and_replenish(dry_run=True)
            self.assertEqual(result["action"], "dry_run")
            mock_get_client.assert_not_called()  # dry_run 不应调用客户端
        print("  [OK] dry_run 模式不调用 922S5 API")


# ═══════════════════════════════════════════════
# Test 7: proxy_health.set_device_gps 路由
# ═══════════════════════════════════════════════
class TestProxyHealthGPSRouting(unittest.TestCase):

    def test_emulator_uses_emu_geo_fix(self):
        """模拟器应使用 emu geo fix 命令。"""
        import src.behavior.proxy_health as ph
        with patch.object(ph, '_is_emulator', return_value=True), \
             patch.object(ph, '_adb', return_value=("OK\n", 0)) as mock_adb:
            result = ph.set_device_gps("emulator-5554", 40.71, -74.00)
            self.assertTrue(result)
            call_args = mock_adb.call_args[0]
            self.assertIn("emu geo fix", call_args[1])
        print("  [OK] 模拟器使用 emu geo fix")

    def test_real_device_routes_to_mock_manager(self):
        """真机应路由到 mock_location_manager.set_mock_location。"""
        import src.behavior.proxy_health as ph
        with patch.object(ph, '_is_emulator', return_value=False), \
             patch('src.device_control.mock_location_manager.set_mock_location',
                   return_value=True) as mock_set:
            result = ph.set_device_gps("real_device_001", 35.67, 139.65)
            self.assertTrue(result)
            mock_set.assert_called_once_with("real_device_001", 35.67, 139.65)
        print("  [OK] 真机路由到 mock_location_manager")

    def test_gps_failure_non_blocking(self):
        """GPS 设置失败不应抛出异常（非阻塞）。"""
        import src.behavior.proxy_health as ph
        with patch.object(ph, '_is_emulator', return_value=False), \
             patch('src.device_control.mock_location_manager.set_mock_location',
                   return_value=False):
            result = ph.set_device_gps("device_gps_fail", 40.71, -74.00)
            self.assertFalse(result)  # 返回 False，不抛出异常
        print("  [OK] GPS 失败返回 False，不抛出异常")


# ═══════════════════════════════════════════════
# Test 8: 922S5 API URL 构建
# ═══════════════════════════════════════════════
class TestProxy922S5URLBuilding(unittest.TestCase):

    def test_url_includes_auth(self):
        """构建的 URL 应包含 appKey 和 appSecret。"""
        from src.device_control.proxy_922s5 import Proxy922S5Client
        client = Proxy922S5Client("mykey123", "mysecret456")
        url = client._build_url("/proxy/list", {"status": "active"})
        self.assertIn("appKey=mykey123", url)
        self.assertIn("appSecret=mysecret456", url)
        self.assertIn("status=active", url)
        self.assertIn("api.922s5.com", url)
        print("  [OK] API URL 包含认证参数")

    def test_url_path_construction(self):
        """路径首部斜杠应被正确处理。"""
        from src.device_control.proxy_922s5 import Proxy922S5Client
        client = Proxy922S5Client("k", "s")
        url1 = client._build_url("/proxy/list")
        url2 = client._build_url("proxy/list")  # 无斜杠
        # 两者都不应导致双斜杠
        self.assertNotIn("//proxy", url1)
        print("  [OK] URL 路径构建无双斜杠")


if __name__ == "__main__":
    print("=" * 60)
    print("Phase 6 P0 测试套件")
    print("=" * 60)

    test_classes = [
        TestMockLocationCoords,
        TestMockLocationCache,
        TestMockLocationIntent,
        TestProxy922S5Parsing,
        TestProxy922S5PoolSync,
        TestProxy922S5Replenish,
        TestProxyHealthGPSRouting,
        TestProxy922S5URLBuilding,
    ]

    total_pass = 0
    total_fail = 0

    for cls in test_classes:
        suite = unittest.TestLoader().loadTestsFromTestCase(cls)
        runner = unittest.TextTestRunner(verbosity=0, stream=open(
            import_sys := __import__('sys'), None) if False else __import__('sys').stdout)
        print(f"\n[{cls.__name__}]")
        result = unittest.TestResult()
        for test in suite:
            try:
                test.run(result)
                if not result.failures and not result.errors:
                    pass
            except Exception as e:
                print(f"  [ERROR] {e}")

    # 使用标准 unittest main 输出
    loader = unittest.TestLoader()
    suites = [loader.loadTestsFromTestCase(c) for c in test_classes]
    all_tests = unittest.TestSuite(suites)
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(all_tests)
    sys.exit(0 if result.wasSuccessful() else 1)
