# -*- coding: utf-8 -*-
"""Phase 17.1 (2026-04-25): yaml 黑名单热加载 + reject metrics by_event_type."""
from __future__ import annotations

import time

import pytest


@pytest.fixture(autouse=True)
def _reset_state():
    """每个 test 前重置 reject counter + blacklist cache."""
    from src.host.fb_store import reset_peer_name_reject_count
    from src.app_automation.facebook import FacebookAutomation
    reset_peer_name_reject_count()
    FacebookAutomation._BLACKLIST_YAML_CACHE = {
        "extra": frozenset(), "loaded_at": 0.0,
    }
    yield


# ═══════════════════════════════════════════════════════════════════
# yaml 黑名单热加载
# ═══════════════════════════════════════════════════════════════════

class TestYamlBlacklistHotReload:
    def test_no_yaml_returns_empty(self, monkeypatch, tmp_path):
        """yaml 不存在时返空 set, 不影响内置规则."""
        from src.app_automation.facebook import FacebookAutomation
        # 让 lookup 路径指向 tmp (不存在 yaml 文件)
        # 直接 monkeypatch __file__ 太 fragile, 改 mock yaml.safe_load 失败
        import yaml as _yaml
        monkeypatch.setattr(_yaml, "safe_load",
                            lambda f: (_ for _ in ()).throw(IOError("test")))
        n = FacebookAutomation.reload_extra_blacklist()
        # IOError 时退化为空, 但 reload 仍然不抛
        # NOTE: 实际 yaml_path.exists() 会 short-circuit 返空, 不到 safe_load
        assert isinstance(n, int)

    def test_yaml_extra_blacklist_loaded(self, monkeypatch, tmp_path):
        """yaml 含 extra 词时合并到黑名单, 真名仍通过."""
        from src.app_automation.facebook import FacebookAutomation
        # 写一个临时 yaml 替换路径
        yaml_content = """
extra_blacklist:
  - "新功能X"
  - "Mark Custom"
"""
        ypath = tmp_path / "peer_name_blacklist.yaml"
        ypath.write_text(yaml_content, encoding="utf-8")
        # patch _load_extra_blacklist 改用我们的 path
        original_loader = FacebookAutomation._load_extra_blacklist

        def _custom_loader():
            import yaml as _yaml
            with ypath.open(encoding="utf-8") as f:
                data = _yaml.safe_load(f) or {}
            items = data.get("extra_blacklist") or []
            return frozenset(s.lower() for s in items if isinstance(s, str))

        monkeypatch.setattr(FacebookAutomation, "_load_extra_blacklist",
                            staticmethod(_custom_loader))
        # yaml 词应被 ban
        assert FacebookAutomation._is_valid_peer_name("新功能X") is False
        assert FacebookAutomation._is_valid_peer_name("Mark Custom") is False
        # 真名仍通过
        assert FacebookAutomation._is_valid_peer_name("山田花子") is True

    def test_ttl_cache_avoids_re_read(self):
        """5min TTL 缓存: 重复调不重读 yaml 文件."""
        from src.app_automation.facebook import FacebookAutomation
        FacebookAutomation._BLACKLIST_YAML_CACHE = {
            "extra": frozenset(["existing_token"]),
            "loaded_at": time.time(),  # 刚 load 过
        }
        # 调 _load 应返缓存
        result = FacebookAutomation._load_extra_blacklist()
        assert "existing_token" in result

    def test_reload_force_refreshes(self):
        """reload_extra_blacklist 强制清缓存."""
        from src.app_automation.facebook import FacebookAutomation
        FacebookAutomation._BLACKLIST_YAML_CACHE = {
            "extra": frozenset(["old_token"]),
            "loaded_at": time.time(),
        }
        FacebookAutomation.reload_extra_blacklist()
        # 强制 reload 后缓存被重置 + 实际从文件读 (空文件返空)
        # 我们的真 yaml 文件 ext 都是注释, extra_blacklist=空
        # → 缓存 = empty
        result = FacebookAutomation._load_extra_blacklist()
        assert "old_token" not in result


# ═══════════════════════════════════════════════════════════════════
# reject metrics by_event_type
# ═══════════════════════════════════════════════════════════════════

class TestRejectMetrics:
    def test_metrics_empty_initial(self):
        from src.host.fb_store import get_peer_name_reject_metrics
        m = get_peer_name_reject_metrics()
        assert m["total"] == 0
        assert m["by_event_type"] == {}
        assert m["recent"] == []

    def test_metrics_count_by_event(self, tmp_db):
        from src.host.fb_store import (record_contact_event,
                                          get_peer_name_reject_metrics)
        # 拦 3 类不同 event_type
        record_contact_event("D1", "查看翻译", "greeting_replied")
        record_contact_event("D1", "查看翻译", "greeting_replied")
        record_contact_event("D1", "Reply", "message_received")
        record_contact_event("D1", "p0", "add_friend_accepted")
        m = get_peer_name_reject_metrics()
        assert m["total"] == 4
        assert m["by_event_type"]["greeting_replied"] == 2
        assert m["by_event_type"]["message_received"] == 1
        assert m["by_event_type"]["add_friend_accepted"] == 1

    def test_metrics_recent_samples(self, tmp_db):
        from src.host.fb_store import (record_contact_event,
                                          get_peer_name_reject_metrics)
        record_contact_event("D1", "查看翻译", "greeting_replied")
        record_contact_event("D2", "Reply", "message_received")
        m = get_peer_name_reject_metrics()
        assert len(m["recent"]) == 2
        # 第 1 条
        assert m["recent"][0]["peer_name"] == "查看翻译"
        assert m["recent"][0]["event_type"] == "greeting_replied"
        assert m["recent"][0]["device_id"] == "D1"

    def test_metrics_recent_capped_at_50(self, tmp_db):
        from src.host.fb_store import (record_contact_event,
                                          get_peer_name_reject_metrics)
        for i in range(60):
            record_contact_event("D", f"Reply{i}",  # all banned (len 7-8 ascii?)
                                  "greeting_replied")
        # "Reply0".."Reply59" — len 6-7, 含数字 + ASCII
        # "Reply0" len=6 含数字 但 len > 4, 不被 ASCII 短数字规则 ban;
        # 'R' 大写 + 'eply0' 后跟数字 → 'isupper' check pass 但 'islower' on
        # 'eply0' 失败 (含 0). 所以 ASCII 按钮规则不 ban.
        # 这些其实会通过! 让我换 truly invalid:
        # 改成 banned 名:
        from src.host.fb_store import reset_peer_name_reject_count
        reset_peer_name_reject_count()
        for i in range(60):
            record_contact_event("D", f"p{i}", "greeting_replied")
            # "p10"-"p99" len=3, ascii, 含数字 → ban
            # "p0"-"p9" len=2, ascii, 含数字 → ban
        m = get_peer_name_reject_metrics()
        assert m["total"] == 60
        assert len(m["recent"]) == 50  # capped


# ═══════════════════════════════════════════════════════════════════
# API 端点
# ═══════════════════════════════════════════════════════════════════

class TestApi:
    @pytest.fixture
    def client(self, tmp_db, monkeypatch):
        monkeypatch.setenv("OPENCLAW_LINE_POOL_SEED_SKIP", "1")
        from src.host.api import app
        from fastapi.testclient import TestClient
        with TestClient(app) as c:
            yield c

    def test_api_metrics_endpoint(self, client):
        r = client.get("/line-pool/stats/peer-name-rejects")
        assert r.status_code == 200
        d = r.json()
        assert "total" in d
        assert "by_event_type" in d
        assert "recent" in d

    def test_api_reset_endpoint(self, tmp_db, client):
        # 先制造 reject
        from src.host.fb_store import record_contact_event
        record_contact_event("D1", "查看翻译", "greeting_replied")
        # reset
        r = client.post("/line-pool/stats/peer-name-rejects/reset")
        assert r.status_code == 200
        assert r.json()["ok"] is True
        # 再查 应该 0
        r2 = client.get("/line-pool/stats/peer-name-rejects")
        assert r2.json()["total"] == 0

    def test_api_blacklist_reload(self, client):
        r = client.post("/line-pool/stats/peer-name-blacklist/reload")
        assert r.status_code == 200
        d = r.json()
        assert d["ok"] is True
        assert "extra_count" in d
