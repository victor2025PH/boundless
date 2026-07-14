# -*- coding: utf-8 -*-
"""Phase 8 测试套件 — 代理池 UI / APK 构建 API / 跨路由器亲和力评分

覆盖:
  P0: 代理池 Web UI 端点可达性（/proxy/pool/*）
  P1: APK 构建 API 端点（SDK 检测 / 文件上传）
  P2: 亲和力评分（记录 / 读取 / 综合评分 / 候选排序）
"""

import sys
import os
import json
import time
import tempfile
import importlib
from pathlib import Path
from unittest.mock import MagicMock, patch

# ── 路径 ──
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import src.device_control.proxy_rotator as rotator_mod

# ════════════════════════════════════════
# P2: 亲和力评分
# ════════════════════════════════════════

class TestAffinityScore:
    """亲和力评分系统测试。"""

    def setup_method(self):
        """每个测试前使用临时文件。"""
        self._tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        self._tmp.close()
        self._orig = rotator_mod._AFFINITY_FILE
        rotator_mod._AFFINITY_FILE = Path(self._tmp.name)
        # 清空
        Path(self._tmp.name).write_text("{}", encoding="utf-8")

    def teardown_method(self):
        rotator_mod._AFFINITY_FILE = self._orig
        try:
            os.unlink(self._tmp.name)
        except Exception:
            pass

    def test_no_history_returns_neutral(self):
        score = rotator_mod.get_affinity_score("router-01", "proxy-abc")
        assert score == 0.5, f"无历史应返回0.5，实际={score}"

    def test_record_success_increases_score(self):
        rotator_mod.record_affinity("router-01", "proxy-abc", True)
        score = rotator_mod.get_affinity_score("router-01", "proxy-abc")
        assert score > 0.5, f"成功后分数应>0.5，实际={score}"

    def test_record_failure_decreases_score(self):
        rotator_mod.record_affinity("router-01", "proxy-abc", False)
        score = rotator_mod.get_affinity_score("router-01", "proxy-abc")
        assert score < 0.5, f"失败后分数应<0.5，实际={score}"

    def test_all_success_returns_high_score(self):
        for _ in range(5):
            rotator_mod.record_affinity("router-01", "proxy-good", True)
        score = rotator_mod.get_affinity_score("router-01", "proxy-good")
        assert score >= 0.85, f"全部成功应>=0.85，实际={score}"

    def test_all_failure_returns_low_score(self):
        for _ in range(3):
            rotator_mod.record_affinity("router-01", "proxy-bad", False)
        score = rotator_mod.get_affinity_score("router-01", "proxy-bad")
        assert score <= 0.25, f"全部失败应<=0.25，实际={score}"

    def test_mixed_history(self):
        for _ in range(3):
            rotator_mod.record_affinity("router-01", "proxy-mixed", True)
        for _ in range(2):
            rotator_mod.record_affinity("router-01", "proxy-mixed", False)
        score = rotator_mod.get_affinity_score("router-01", "proxy-mixed")
        # 3/5 = 60% × 0.8 = 0.48，应在 0.3-0.7 范围
        assert 0.3 <= score <= 0.7, f"混合历史应在0.3-0.7，实际={score}"

    def test_different_routers_independent(self):
        rotator_mod.record_affinity("router-01", "proxy-abc", True)
        rotator_mod.record_affinity("router-02", "proxy-abc", False)
        s1 = rotator_mod.get_affinity_score("router-01", "proxy-abc")
        s2 = rotator_mod.get_affinity_score("router-02", "proxy-abc")
        assert s1 > s2, f"路由器01应>路由器02：{s1} vs {s2}"

    def test_persistence(self):
        rotator_mod.record_affinity("router-01", "proxy-abc", True)
        # 重新加载
        data = rotator_mod._load_affinity()
        assert "router-01" in data
        assert "proxy-abc" in data["router-01"]
        assert data["router-01"]["proxy-abc"]["success"] == 1

    def test_combined_score_formula(self):
        """综合评分 = 健康×0.6 + 亲和力×0.4。"""
        # 模拟健康评分0.8，亲和力0.9（全部成功）
        for _ in range(5):
            rotator_mod.record_affinity("router-01", "proxy-test", True)

        with patch.object(rotator_mod, "get_proxy_score", return_value=0.8):
            combined = rotator_mod.get_combined_score("router-01", "proxy-test")

        affinity = rotator_mod.get_affinity_score("router-01", "proxy-test")
        expected = round(0.8 * 0.6 + affinity * 0.4, 3)
        assert abs(combined - expected) < 0.01, f"综合评分计算错误: {combined} vs {expected}"


# ════════════════════════════════════════
# P2: 候选排序验证
# ════════════════════════════════════════

class TestCandidateSorting:
    """验证候选代理按综合评分排序，亲和力高的优先。"""

    def setup_method(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        self._tmp.close()
        self._orig_aff = rotator_mod._AFFINITY_FILE
        rotator_mod._AFFINITY_FILE = Path(self._tmp.name)
        Path(self._tmp.name).write_text("{}", encoding="utf-8")

        self._tmp_scores = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        self._tmp_scores.close()
        self._orig_scores = rotator_mod._SCORES_FILE
        rotator_mod._SCORES_FILE = Path(self._tmp_scores.name)
        Path(self._tmp_scores.name).write_text("{}", encoding="utf-8")

    def teardown_method(self):
        rotator_mod._AFFINITY_FILE = self._orig_aff
        rotator_mod._SCORES_FILE = self._orig_scores
        for f in [self._tmp.name, self._tmp_scores.name]:
            try:
                os.unlink(f)
            except Exception:
                pass

    def test_affinity_wins_over_equal_health(self):
        """相同健康评分时，亲和力高的排前面。"""
        # proxy-A 在 router-01 上有成功历史
        for _ in range(3):
            rotator_mod.record_affinity("router-01", "proxy-A", True)
        # proxy-B 无历史

        candidates = [
            {"id": "proxy-B", "label": "B", "server": "1.1.1.1", "port": 1080},
            {"id": "proxy-A", "label": "A", "server": "2.2.2.2", "port": 1080},
        ]

        # 健康评分相同（均为0.75，未测试）
        candidates.sort(key=lambda p: rotator_mod.get_combined_score("router-01", p["id"]),
                        reverse=True)
        assert candidates[0]["id"] == "proxy-A", \
            f"有亲和力历史的A应排第一，实际={candidates[0]['id']}"

    def test_high_health_beats_no_affinity(self):
        """健康评分高的代理在无亲和力差异时优先。"""
        # proxy-high 有高健康分
        rotator_mod.record_proxy_test("proxy-high", True, 50)
        rotator_mod.record_proxy_test("proxy-high", True, 60)
        rotator_mod.record_proxy_test("proxy-high", True, 55)

        candidates = [
            {"id": "proxy-low", "label": "Low"},
            {"id": "proxy-high", "label": "High"},
        ]
        candidates.sort(key=lambda p: rotator_mod.get_combined_score("router-01", p["id"]),
                        reverse=True)
        assert candidates[0]["id"] == "proxy-high", \
            f"高健康评分的应排第一，实际={candidates[0]['id']}"


# ════════════════════════════════════════
# P1: APK 构建/上传 API
# ════════════════════════════════════════

class TestApkBuildApi:
    """APK 构建与上传 API 的单元测试（不实际构建）。"""

    def test_build_api_detects_missing_sdk(self):
        """SDK 缺失时返回 sdk_missing=True。"""
        from src.host.routers.router_mgmt import build_mock_location_apk
        # 强制 SDK 路径不存在
        with patch("shutil.which", return_value=None), \
             patch("os.path.isdir", return_value=False), \
             patch("os.environ.get", return_value=None):
            result = build_mock_location_apk()
        assert result.get("sdk_missing") is True, f"应检测到SDK缺失: {result}"
        assert "instructions" in result, "应有安装指引"

    def test_upload_apk_validates_extension(self):
        """上传非.apk文件应拒绝。"""
        from src.host.routers.router_mgmt import upload_apk
        import asyncio

        async def _run():
            req = MagicMock()
            req.headers = {"content-type": "application/json"}
            async def _json():
                return {"filename": "test.exe", "content_base64": "AAAA"}
            req.json = _json
            return await upload_apk(req)

        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(_run())
        finally:
            loop.close()
        assert result.get("ok") is False, f"非APK文件应被拒绝: {result}"

    def test_upload_valid_apk(self):
        """有效base64 APK应被写入到 config/apks/。"""
        import base64
        import asyncio
        from src.host.routers.router_mgmt import upload_apk

        fake_content = b"PK fake apk content for testing"
        b64 = base64.b64encode(fake_content).decode()
        test_filename = "test_phase8_upload.apk"
        dest_path = ROOT / "config" / "apks" / test_filename

        async def _run():
            req = MagicMock()
            req.headers = {"content-type": "application/json"}
            async def _json():
                return {"filename": test_filename, "content_base64": b64}
            req.json = _json
            return await upload_apk(req)

        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(_run())
        finally:
            loop.close()
            # 清理测试文件
            if dest_path.exists():
                dest_path.unlink()

        assert result.get("ok") is True, f"上传应成功: {result}"
        assert result.get("size_bytes") == len(fake_content), \
            f"文件大小不匹配: {result.get('size_bytes')} vs {len(fake_content)}"


# ════════════════════════════════════════
# P0: 代理池端点可达性（mock 依赖，只检查路由注册）
# ════════════════════════════════════════

class TestProxyPoolEndpoints:
    """验证代理池 API 端点已注册并可调用。"""

    def test_pool_stats_endpoint_exists(self):
        from src.host.routers.router_mgmt import router as api_router
        routes = {r.path for r in api_router.routes}
        assert "/proxy/pool/stats" in routes, f"缺少 /proxy/pool/stats，已有: {routes}"

    def test_pool_list_endpoint_exists(self):
        from src.host.routers.router_mgmt import router as api_router
        routes = {r.path for r in api_router.routes}
        assert "/proxy/pool/list" in routes, f"缺少 /proxy/pool/list"

    def test_pool_sync_endpoint_exists(self):
        from src.host.routers.router_mgmt import router as api_router
        routes = {r.path for r in api_router.routes}
        assert "/proxy/pool/sync" in routes, f"缺少 /proxy/pool/sync"

    def test_affinity_endpoint_exists(self):
        from src.host.routers.router_mgmt import router as api_router
        routes = {r.path for r in api_router.routes}
        assert "/proxy/affinity" in routes, f"缺少 /proxy/affinity，已有: {routes}"

    def test_build_apk_endpoint_exists(self):
        from src.host.routers.router_mgmt import router as api_router
        routes = {r.path for r in api_router.routes}
        assert "/tools/build-mock-location-apk" in routes, \
            f"缺少 /tools/build-mock-location-apk，已有: {routes}"

    def test_upload_apk_endpoint_exists(self):
        from src.host.routers.router_mgmt import router as api_router
        routes = {r.path for r in api_router.routes}
        assert "/tools/upload-apk" in routes, f"缺少 /tools/upload-apk"


# ════════════════════════════════════════
# 执行
# ════════════════════════════════════════

if __name__ == "__main__":
    passed = 0
    failed = 0
    errors = []

    test_classes = [
        TestAffinityScore,
        TestCandidateSorting,
        TestApkBuildApi,
        TestProxyPoolEndpoints,
    ]

    for cls in test_classes:
        instance = cls()
        methods = [m for m in dir(cls) if m.startswith("test_")]
        for method_name in methods:
            test_name = f"{cls.__name__}.{method_name}"
            try:
                if hasattr(instance, "setup_method"):
                    instance.setup_method()
                getattr(instance, method_name)()
                if hasattr(instance, "teardown_method"):
                    instance.teardown_method()
                print(f"  OK  {test_name}")
                passed += 1
            except Exception as e:
                if hasattr(instance, "teardown_method"):
                    try:
                        instance.teardown_method()
                    except Exception:
                        pass
                print(f"  FAIL {test_name}: {e}")
                errors.append((test_name, str(e)))
                failed += 1

    print(f"\n{'='*50}")
    print(f"Phase 8 测试结果: {passed} 通过, {failed} 失败")
    if errors:
        print("失败项:")
        for name, err in errors:
            print(f"  - {name}: {err}")
    sys.exit(0 if failed == 0 else 1)
