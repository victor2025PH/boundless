# -*- coding: utf-8 -*-
"""
API integration tests — verify all endpoints respond correctly.

Run: pytest tests/test_api_integration.py -v
Requires: server running on localhost:18080 (or set OPENCLAW_BASE_URL)

These tests use TestClient (no real server needed).
"""
import os
import sys
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("OPENCLAW_API_KEY", "")


@pytest.fixture(scope="module")
def client():
    """API integration TestClient — 不进 lifespan context (跳过 startup/shutdown).

    根因 (2026-05-04): 旧版 ``with TestClient(app) as c`` 触发 lifespan startup
    起 ~15 个 daemon thread (drain / scheduler / W03 cache / proxy_health /
    router_manager / openclaw_agent ...), 但 lifespan shutdown 段只停 7 个,
    剩余 daemon 让 starlette ``TestClient.__exit__`` 的 ``wait_shutdown`` 卡
    portal.call() 永挂 -> 全 suite pytest --timeout=60 触发 stack trace, fail-fast
    被破坏.

    fix: 不进 ``with``, TestClient(app) 直接构造跳过 lifespan. endpoint route
    已在 import 时注册, /health 等用 ``_get_metrics().snapshot()`` lazy 读 +
    ``snap.get(..., {})`` 兜底, 无 lifespan 注入也能正常返回. 测试只用
    lightweight read endpoint, 不依赖 lifespan 副产物 (e.g. WorkflowEngine
    action register / auto_start_cluster).

    Trade-off: 若未来加测试用例触到 lifespan 依赖 endpoint, 需单独 spin up
    一个进 with 的 client fixture (并接受 teardown 慢).
    """
    from src.host.database import init_db
    init_db()
    from src.host.api import app
    from fastapi.testclient import TestClient
    yield TestClient(app)


# ═══════════════════════════════════════════════════════════════════
# Health & Status
# ═══════════════════════════════════════════════════════════════════

class TestHealthAPI:
    def test_health(self, client):
        r = client.get("/health")
        assert r.status_code == 200
        data = r.json()
        assert "status" in data
        assert "facebook_launch" in data and isinstance(data["facebook_launch"], dict)
        assert "tiktok_launch" in data and isinstance(data["tiktok_launch"], dict)
        assert "tiktok_campaign_launch" in data and isinstance(data["tiktok_campaign_launch"], dict)
        assert "skipped_no_local_device" in data["tiktok_campaign_launch"]
        assert "skipped_local_offline" in data["tiktok_campaign_launch"]
        assert "skipped_local_offline" in data["tiktok_launch"]

    def test_health_alerts(self, client):
        r = client.get("/health/alerts")
        assert r.status_code == 200

    def test_stats(self, client):
        r = client.get("/stats")
        assert r.status_code == 200

    def test_pool(self, client):
        r = client.get("/pool")
        assert r.status_code == 200

    def test_metrics(self, client):
        r = client.get("/metrics")
        assert r.status_code == 200

    def test_dashboard(self, client):
        r = client.get("/dashboard")
        assert r.status_code == 200


# ═══════════════════════════════════════════════════════════════════
# TikTok Endpoints
# ═══════════════════════════════════════════════════════════════════

class TestTikTokAPI:
    def test_devices_list(self, client):
        r = client.get("/tiktok/devices")
        assert r.status_code == 200

    def test_tiktok_stats(self, client):
        r = client.get("/tiktok/stats")
        assert r.status_code == 200

    def test_seeds(self, client):
        r = client.get("/tiktok/seeds/italy")
        assert r.status_code == 200
        data = r.json()
        assert "seeds" in data or isinstance(data, list)

    def test_device_init(self, client):
        r = client.post("/tiktok/devices/TEST_DEV_API/init")
        assert r.status_code == 200

    def test_device_summary(self, client):
        client.post("/tiktok/devices/TEST_DEV_API/init")
        r = client.get("/tiktok/devices/TEST_DEV_API")
        assert r.status_code == 200
        data = r.json()
        assert data["phase"] == "cold_start"

    def test_device_accounts(self, client):
        r = client.get("/tiktok/devices/TEST_DEV_API/accounts")
        assert r.status_code == 200

    def test_campaign_skips_worker_when_local_offline_and_env(self, client, monkeypatch):
        from unittest.mock import patch, MagicMock

        from src.device_control.device_manager import DeviceInfo, DeviceStatus

        monkeypatch.setenv("OPENCLAW_TT_CAMPAIGN_SKIP_WORKER_WHEN_OFFLINE", "1")
        fake_dev = DeviceInfo(
            device_id="tt_offline_skip_test_xx",
            display_name="t",
            platform="tiktok",
            status=DeviceStatus.OFFLINE,
        )
        mgr = MagicMock()
        mgr.get_device_info.return_value = fake_dev

        with patch("src.device_control.device_manager.get_device_manager", return_value=mgr):
            r = client.post("/tiktok/device/tt_offline_skip_test_xx/launch", json={})
        assert r.status_code == 200
        body = r.json()
        assert body.get("skipped") == "local_offline"
        assert body.get("ok") is False
        assert "OPENCLAW_TT_CAMPAIGN_SKIP_WORKER_WHEN_OFFLINE" in (body.get("error") or "")

    def test_flow_steps_skips_when_local_offline_and_env(self, client, monkeypatch):
        from unittest.mock import patch, MagicMock

        from src.device_control.device_manager import DeviceInfo, DeviceStatus

        monkeypatch.setenv("OPENCLAW_TT_CAMPAIGN_SKIP_WORKER_WHEN_OFFLINE", "1")
        fake_dev = DeviceInfo(
            device_id="tt_flow_offline_yy",
            display_name="t",
            platform="tiktok",
            status=DeviceStatus.DISCONNECTED,
        )
        mgr = MagicMock()
        mgr.get_device_info.return_value = fake_dev

        with patch("src.device_control.device_manager.get_device_manager", return_value=mgr):
            r = client.post(
                "/tiktok/device/tt_flow_offline_yy/launch",
                json={"flow_steps": [{"type": "tiktok_scroll_feed", "params": {}}]},
            )
        assert r.status_code == 200
        body = r.json()
        assert body.get("skipped") == "local_offline"
        assert body.get("ok") is False
        assert body.get("task_count") == 0
        assert body.get("flow_tasks") == []


# ═══════════════════════════════════════════════════════════════════
# Conversion Funnel
# ═══════════════════════════════════════════════════════════════════

class TestFunnelAPI:
    def test_funnel(self, client):
        r = client.get("/funnel")
        assert r.status_code == 200
        data = r.json()
        assert "funnel" in data
        assert "rates" in data

    def test_funnel_daily(self, client):
        r = client.get("/funnel/daily?days=7")
        assert r.status_code == 200
        data = r.json()
        assert "daily" in data
        assert len(data["daily"]) == 7

    def test_funnel_devices(self, client):
        r = client.get("/funnel/devices")
        assert r.status_code == 200


# ═══════════════════════════════════════════════════════════════════
# Risk / Adaptive Compliance
# ═══════════════════════════════════════════════════════════════════

class TestRiskAPI:
    def test_risk_all(self, client):
        r = client.get("/risk")
        assert r.status_code == 200
        data = r.json()
        assert "devices" in data
        assert "high_risk" in data

    def test_risk_device(self, client):
        r = client.get("/risk/TEST_DEVICE")
        assert r.status_code == 200
        data = r.json()
        assert "risk_score" in data
        assert "risk_level" in data

    def test_recovery_trigger(self, client):
        r = client.post("/risk/TEST_RECOVERY/recover?reason=api_test")
        assert r.status_code == 200
        assert r.json()["status"] == "recovery_started"

    def test_recovery_exit(self, client):
        client.post("/risk/TEST_EXIT/recover?reason=test")
        r = client.post("/risk/TEST_EXIT/exit-recovery")
        assert r.status_code == 200
        assert r.json()["status"] == "recovery_exited"


# ═══════════════════════════════════════════════════════════════════
# A/B Testing
# ═══════════════════════════════════════════════════════════════════

class TestExperimentsAPI:
    def test_list_experiments(self, client):
        r = client.get("/experiments")
        assert r.status_code == 200

    def test_create_experiment(self, client):
        name = f"api_test_{uuid.uuid4().hex[:8]}"
        r = client.post("/experiments", json={
            "name": name,
            "category": "test",
            "variants": ["a", "b"],
        })
        assert r.status_code == 200
        data = r.json()
        assert "experiment_id" in data

    def test_get_experiment(self, client):
        name = f"api_get_{uuid.uuid4().hex[:8]}"
        client.post("/experiments", json={
            "name": name, "category": "test", "variants": ["x", "y"],
        })
        r = client.get(f"/experiments/{name}")
        assert r.status_code == 200

    def test_record_event(self, client):
        name = f"api_rec_{uuid.uuid4().hex[:8]}"
        client.post("/experiments", json={
            "name": name, "category": "test", "variants": ["v1", "v2"],
        })
        r = client.post(f"/experiments/{name}/record", json={
            "variant": "v1",
            "event_type": "sent",
            "device_id": "DEV01",
        })
        assert r.status_code == 200

    def test_end_experiment(self, client):
        name = f"api_end_{uuid.uuid4().hex[:8]}"
        client.post("/experiments", json={
            "name": name, "category": "test",
        })
        r = client.post(f"/experiments/{name}/end")
        assert r.status_code == 200


# ═══════════════════════════════════════════════════════════════════
# Conversation FSM
# ═══════════════════════════════════════════════════════════════════

class TestConversationAPI:
    def test_follow_ups(self, client):
        r = client.get("/conversations/follow-ups")
        assert r.status_code == 200
        data = r.json()
        assert "follow_ups" in data

    def test_pipeline_stats(self, client):
        r = client.get("/conversations/stats/pipeline")
        assert r.status_code == 200


# ═══════════════════════════════════════════════════════════════════
# Leads CRM
# ═══════════════════════════════════════════════════════════════════

class TestLeadsAPI:
    def test_list_leads(self, client):
        r = client.get("/leads")
        assert r.status_code == 200

    def test_create_lead(self, client):
        r = client.post("/leads", json={
            "name": f"Test Lead {uuid.uuid4().hex[:6]}",
            "source": "tiktok",
        })
        assert r.status_code == 200
        assert "lead_id" in r.json()

    def test_lead_stats(self, client):
        r = client.get("/leads/stats")
        assert r.status_code == 200


# ═══════════════════════════════════════════════════════════════════
# Compliance
# ═══════════════════════════════════════════════════════════════════

class TestComplianceAPI:
    def test_tiktok_compliance(self, client):
        r = client.get("/compliance/tiktok")
        assert r.status_code == 200

    def test_follow_remaining(self, client):
        r = client.get("/compliance/tiktok/follow/remaining")
        assert r.status_code == 200


# ═══════════════════════════════════════════════════════════════════
# AI Endpoints
# ═══════════════════════════════════════════════════════════════════

class TestAIAPI:
    def test_ai_stats(self, client):
        r = client.get("/ai/stats")
        assert r.status_code == 200

    def test_rewrite(self, client):
        r = client.post("/ai/rewrite", json={
            "template": "Hello {name}!",
            "variables": {"name": "Marco"},
            "platform": "tiktok",
        })
        assert r.status_code == 200

    def test_classify_intent(self, client):
        r = client.post("/ai/classify_intent", json={
            "message": "I'm interested in learning more",
            "platform": "tiktok",
        })
        assert r.status_code == 200


# ═══════════════════════════════════════════════════════════════════
# Schedule / Smart Timing
# ═══════════════════════════════════════════════════════════════════

class TestScheduleAPI:
    def test_list_schedules(self, client):
        r = client.get("/schedules")
        assert r.status_code == 200

    def test_best_send_time(self, client):
        r = client.post("/schedule/best_send_time", json={
            "timezone": "Europe/Rome",
        })
        assert r.status_code == 200


# ═══════════════════════════════════════════════════════════════════
# Events
# ═══════════════════════════════════════════════════════════════════

class TestEventsAPI:
    def test_recent_events(self, client):
        r = client.get("/events/recent")
        assert r.status_code == 200

    def test_emit_event(self, client):
        r = client.post("/events/emit", json={
            "type": "test.api_test",
            "data": {"source": "test"},
        })
        assert r.status_code == 200

    def test_subscriptions(self, client):
        r = client.get("/events/subscriptions")
        assert r.status_code == 200


# ═══════════════════════════════════════════════════════════════════
# Observability
# ═══════════════════════════════════════════════════════════════════

class TestObservabilityAPI:
    def test_observability_metrics(self, client):
        r = client.get("/observability/metrics")
        assert r.status_code == 200

    def test_prometheus(self, client):
        r = client.get("/observability/prometheus")
        assert r.status_code == 200
        body = r.text
        assert "openclaw_facebook_device_launch_launches" in body
        assert "openclaw_tiktok_device_launch_launches" in body
        assert "openclaw_tiktok_campaign_launch_launches" in body
        assert "openclaw_tiktok_campaign_launch_skipped_no_local_device_total" in body
        assert "openclaw_tiktok_campaign_launch_skipped_local_offline_total" in body
        assert "openclaw_tiktok_flow_steps_skipped_local_offline_total" in body

    def test_prometheus_with_api_key_when_required(self, client):
        from unittest.mock import patch
        import src.host.routers.auth as auth_mod

        with patch.object(auth_mod, "_API_KEY", "prom_integ_test_key"):
            r401 = client.get("/observability/prometheus")
            assert r401.status_code == 401
            r_ok = client.get(
                "/observability/prometheus",
                headers={"X-API-Key": "prom_integ_test_key"},
            )
            assert r_ok.status_code == 200
            assert "openclaw_facebook_device_launch_launches" in r_ok.text

    def test_logs_files(self, client):
        r = client.get("/observability/logs/files")
        assert r.status_code == 200

    def test_execution_stats(self, client):
        r = client.get("/observability/executions/stats/summary")
        assert r.status_code == 200

    def test_alerts(self, client):
        r = client.get("/observability/alerts")
        assert r.status_code == 200


# ═══════════════════════════════════════════════════════════════════
# Device cleanup (preview + backup)
# ═══════════════════════════════════════════════════════════════════


class TestDeviceCleanupAPI:
    def test_cleanup_candidates(self, client):
        r = client.get("/devices/cleanup-candidates?max_age_minutes=60")
        assert r.status_code == 200
        data = r.json()
        assert "candidates" in data
        assert "count" in data
        assert data["max_age_minutes"] == 60

    def test_devices_meta(self, client):
        r = client.get("/devices/meta")
        assert r.status_code == 200
        m = r.json()
        assert m.get("schema_version") == 1
        assert "breakdown" in m

    def test_cleanup_post_shape(self, client):
        r = client.post(
            "/devices/cleanup",
            json={"max_age_minutes": 999999, "backup": False},
        )
        assert r.status_code == 200
        body = r.json()
        assert body.get("ok") is True
        assert "removed" in body
        assert "backup" in body

    def test_devices_meta_alerts_shape(self, client):
        r = client.get("/devices/meta")
        assert r.status_code == 200
        m = r.json()
        assert "alerts" in m
        assert "has_warning" in m["alerts"]
        assert "adb_problem_preview" in m
        assert "stale_alias_keys" in m["breakdown"]

    def test_aliases_prune_dry_run(self, client):
        r = client.post("/devices/aliases/prune-orphans", json={"dry_run": True})
        assert r.status_code == 200
        b = r.json()
        assert b.get("ok") is True
        assert b.get("dry_run") is True
        assert "would_remove" in b

    def test_cleanup_empty_device_ids_list(self, client):
        """[] 表示本机不清理任何条目（与未传 device_ids 不同）。"""
        r = client.post("/devices/cleanup", json={"max_age_minutes": 1, "backup": False, "device_ids": []})
        assert r.status_code == 200
        b = r.json()
        assert b.get("ok") is True
        assert b.get("device_ids_filter") == []

    def test_cleanup_device_ids_bad_type(self, client):
        r = client.post("/devices/cleanup", json={"device_ids": "not-a-list"})
        assert r.status_code == 400


# ═══════════════════════════════════════════════════════════════════
# Task params / audience presets
# ═══════════════════════════════════════════════════════════════════


class TestTaskParamsAPI:
    def test_audience_presets_list(self, client):
        r = client.get("/task-params/audience-presets")
        assert r.status_code == 200
        data = r.json()
        assert data.get("ok") is True
        assert isinstance(data.get("presets"), list)
        assert "etag" in data and data["etag"]
        assert "version" in data
        assert "mtime" in data
        assert data.get("unchanged") is not True
        ids = {p.get("id") for p in data["presets"] if isinstance(p, dict)}
        assert "italy_male_30p" in ids

    def test_audience_presets_unchanged_if_etag(self, client):
        r = client.get("/task-params/audience-presets")
        etag = r.json()["etag"]
        r2 = client.get("/task-params/audience-presets", params={"if_etag": etag})
        assert r2.status_code == 200
        d2 = r2.json()
        assert d2.get("ok") is True
        assert d2.get("unchanged") is True
        assert d2.get("presets") is None
        assert d2.get("etag") == etag

    def test_audience_presets_wrong_etag_returns_full(self, client):
        r = client.get("/task-params/audience-presets", params={"if_etag": "0:0"})
        assert r.status_code == 200
        d = r.json()
        assert d.get("unchanged") is not True
        assert isinstance(d.get("presets"), list)
        assert len(d["presets"]) >= 1

    def test_reload_audience_presets_post(self, client):
        r = client.post("/task-params/reload-audience-presets", json={})
        assert r.status_code == 200
        d = r.json()
        assert d.get("ok") is True
        assert "etag" in d
        assert isinstance(d.get("presets"), list)
        assert len(d["presets"]) >= 1

    def test_normalize_applies_audience_preset(self, client):
        r = client.post(
            "/task-params/normalize",
            json={
                "task_type": "tiktok_follow",
                "params": {"audience_preset": "italy_male_30p", "max_follows": 10},
            },
        )
        assert r.status_code == 200
        body = r.json()
        assert body.get("ok") is True
        assert body["params"].get("target_country") == "italy"
        assert body["params"].get("_audience_preset") == "italy_male_30p"
        assert body["params"].get("max_follows") == 10
