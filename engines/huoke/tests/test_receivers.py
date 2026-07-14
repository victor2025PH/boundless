# -*- coding: utf-8 -*-
"""接收方账号管理 (Phase 6.B) 单元 + API 测试。"""
from __future__ import annotations

import pytest


@pytest.fixture
def tmp_receivers(tmp_db, tmp_path, monkeypatch):
    """临时 referral_receivers.yaml 文件, 每个测试独立。"""
    import src.host.lead_mesh.receivers as rx

    # 把 _cfg_path 重定向到 tmp
    cfg = tmp_path / "referral_receivers.yaml"
    monkeypatch.setattr(rx, "_cfg_path", cfg)
    # 重建 YamlCache 指向新路径(否则旧 cache 仍生效)
    from src.host._yaml_cache import YamlCache
    rx._CACHE = YamlCache(
        path=cfg, defaults=rx._FALLBACK,
        post_process=rx._post_process,
        log_label="referral_receivers.yaml(test)",
        logger=rx.logger,
    )
    yield rx


# ─── 配置加载 + CRUD ─────────────────────────────────────────────────
class TestConfigCRUD:
    def test_empty_config_returns_defaults(self, tmp_receivers):
        d = tmp_receivers.load_receivers()
        assert d["receivers"] == {}
        assert "defaults" in d

    def test_upsert_and_get(self, tmp_receivers):
        r = tmp_receivers.upsert_receiver("line_a", {
            "channel": "line", "account_id": "@a",
            "daily_cap": 10, "enabled": True})
        assert r["key"] == "line_a"
        assert r["channel"] == "line"
        assert tmp_receivers.get_receiver("line_a")["account_id"] == "@a"

    def test_delete(self, tmp_receivers):
        tmp_receivers.upsert_receiver("line_a",
            {"channel": "line", "account_id": "@a"})
        assert tmp_receivers.delete_receiver("line_a") is True
        assert tmp_receivers.get_receiver("line_a") is None
        # 删不存在的
        assert tmp_receivers.delete_receiver("line_a") is False

    def test_list_filters(self, tmp_receivers):
        tmp_receivers.upsert_receiver("line_a",
            {"channel": "line", "account_id": "@a", "enabled": True})
        tmp_receivers.upsert_receiver("line_b",
            {"channel": "line", "account_id": "@b", "enabled": False})
        tmp_receivers.upsert_receiver("wa_a",
            {"channel": "whatsapp", "account_id": "+1", "enabled": True})

        # by channel
        lines = tmp_receivers.list_receivers(channel="line")
        assert len(lines) == 2
        # enabled only
        enabled_lines = tmp_receivers.list_receivers(
            channel="line", enabled_only=True)
        assert len(enabled_lines) == 1
        assert enabled_lines[0]["key"] == "line_a"

    def test_persona_filter(self, tmp_receivers):
        tmp_receivers.upsert_receiver("line_jp",
            {"channel": "line", "account_id": "@jp",
             "persona_filter": ["jp_female_midlife"]})
        tmp_receivers.upsert_receiver("line_us",
            {"channel": "line", "account_id": "@us",
             "persona_filter": ["us_male_young"]})
        tmp_receivers.upsert_receiver("line_any",
            {"channel": "line", "account_id": "@any",
             "persona_filter": []})    # 空 = 所有

        jp_list = tmp_receivers.list_receivers(
            persona_key="jp_female_midlife")
        keys = {r["key"] for r in jp_list}
        assert "line_jp" in keys          # persona 匹配
        assert "line_any" in keys         # persona_filter=[] = 接所有
        assert "line_us" not in keys      # persona 不匹配 → 排除


# ─── pick_receiver 算法 ────────────────────────────────────────────────
class TestPickReceiver:
    def test_picks_least_loaded(self, tmp_receivers):
        tmp_receivers.upsert_receiver("a",
            {"channel": "line", "account_id": "@a",
             "daily_cap": 10, "enabled": True})
        tmp_receivers.upsert_receiver("b",
            {"channel": "line", "account_id": "@b",
             "daily_cap": 20, "enabled": True})
        # 无负载, b 的 remaining=20 > a 的 10, 应选 b
        picked = tmp_receivers.pick_receiver("line")
        assert picked and picked["key"] == "b"

    def test_disabled_not_picked(self, tmp_receivers):
        tmp_receivers.upsert_receiver("a",
            {"channel": "line", "account_id": "@a",
             "daily_cap": 10, "enabled": False})
        picked = tmp_receivers.pick_receiver("line")
        assert picked is None

    def test_backup_chain(self, tmp_receivers):
        """A 满 → 跳 backup B。"""
        # 创建并伪造 A 已到 cap
        tmp_receivers.upsert_receiver("a",
            {"channel": "line", "account_id": "@a",
             "daily_cap": 1, "backup_key": "b", "enabled": True})
        tmp_receivers.upsert_receiver("b",
            {"channel": "line", "account_id": "@b",
             "daily_cap": 10, "enabled": True})
        # 模拟 A 今日已有一个 pending handoff (占配额)
        from src.host.lead_mesh import create_handoff, resolve_identity
        cid = resolve_identity(platform="facebook",
                                account_id="fb:x", display_name="x")
        create_handoff(canonical_id=cid, source_agent="agent_b",
                        channel="line",
                        receiver_account_key="a",
                        auto_pick_receiver=False,  # 直接占位不走 auto
                        enqueue_webhook=False)
        # 现在 a.current=1/1 = at_cap; pick 应该跳到 b
        picked = tmp_receivers.pick_receiver("line")
        assert picked and picked["key"] == "b"

    def test_preferred_key_wins(self, tmp_receivers):
        tmp_receivers.upsert_receiver("preferred",
            {"channel": "line", "account_id": "@p",
             "daily_cap": 5, "enabled": True})
        tmp_receivers.upsert_receiver("other",
            {"channel": "line", "account_id": "@o",
             "daily_cap": 100, "enabled": True})
        picked = tmp_receivers.pick_receiver("line",
                                                preferred_key="preferred")
        assert picked and picked["key"] == "preferred"
        # preferred 如果禁用 → 降级
        tmp_receivers.upsert_receiver("preferred", {"enabled": False})
        picked = tmp_receivers.pick_receiver("line",
                                                preferred_key="preferred")
        assert picked and picked["key"] == "other"

    def test_all_at_cap_returns_none(self, tmp_receivers):
        tmp_receivers.upsert_receiver("only",
            {"channel": "line", "account_id": "@o",
             "daily_cap": 0, "enabled": True})
        picked = tmp_receivers.pick_receiver("line")
        assert picked is None


# ─── receiver_load ─────────────────────────────────────────────────────
class TestReceiverLoad:
    def test_returns_zero_load(self, tmp_receivers):
        tmp_receivers.upsert_receiver("x",
            {"channel": "line", "account_id": "@x", "daily_cap": 10})
        load = tmp_receivers.receiver_load("x")
        assert load["exists"] is True
        assert load["current"] == 0
        assert load["cap"] == 10
        assert load["percent_used"] == 0

    def test_masked_account(self, tmp_receivers):
        tmp_receivers.upsert_receiver("y",
            {"channel": "whatsapp", "account_id": "+819012345678"})
        load = tmp_receivers.receiver_load("y")
        assert "+8" in load["account_id_masked"]
        assert "*" in load["account_id_masked"]
        assert "1234" not in load["account_id_masked"]

    def test_nonexistent(self, tmp_receivers):
        load = tmp_receivers.receiver_load("nosuchkey")
        assert load["exists"] is False


# ─── create_handoff 自动 pick ───────────────────────────────────────────
class TestCreateHandoffAutoPick:
    def test_auto_pick_assigns_receiver(self, tmp_receivers):
        tmp_receivers.upsert_receiver("auto_rx",
            {"channel": "line", "account_id": "@auto",
             "daily_cap": 10, "enabled": True,
             "persona_filter": ["jp_female_midlife"]})
        from src.host.lead_mesh import (create_handoff, resolve_identity,
                                           get_handoff)
        cid = resolve_identity(platform="facebook",
                                account_id="fb:TestPeer",
                                display_name="TestPeer")
        hid = create_handoff(
            canonical_id=cid, source_agent="agent_b", channel="line",
            persona_key="jp_female_midlife",
            # receiver_account_key 不传, 应自动 pick
            enqueue_webhook=False)
        assert hid
        h = get_handoff(hid)
        assert h["receiver_account_key"] == "auto_rx"

    def test_explicit_key_overrides_auto(self, tmp_receivers):
        tmp_receivers.upsert_receiver("auto_rx",
            {"channel": "line", "account_id": "@auto", "daily_cap": 10})
        from src.host.lead_mesh import (create_handoff, resolve_identity,
                                           get_handoff)
        cid = resolve_identity(platform="facebook",
                                account_id="fb:P2", display_name="P2")
        hid = create_handoff(
            canonical_id=cid, source_agent="agent_b", channel="line",
            receiver_account_key="manual_key",   # 显式指定
            enqueue_webhook=False)
        h = get_handoff(hid)
        assert h["receiver_account_key"] == "manual_key"

    def test_no_receivers_handoff_still_created(self, tmp_receivers):
        """没有 receiver 配置时 handoff 不应失败, 只是 receiver_account_key 留空。"""
        from src.host.lead_mesh import (create_handoff, resolve_identity,
                                           get_handoff)
        cid = resolve_identity(platform="facebook",
                                account_id="fb:P3", display_name="P3")
        hid = create_handoff(
            canonical_id=cid, source_agent="agent_b", channel="line",
            enqueue_webhook=False)
        assert hid
        h = get_handoff(hid)
        assert h["receiver_account_key"] == ""


# ─── API 端点测试 ─────────────────────────────────────────────────────
class TestReceiversAPI:
    def test_list_empty(self, tmp_receivers):
        from fastapi.testclient import TestClient
        from src.host.api import app
        with TestClient(app) as c:
            r = c.get("/lead-mesh/receivers")
        assert r.status_code == 200
        assert r.json()["count"] == 0

    def test_upsert_via_api(self, tmp_receivers):
        from fastapi.testclient import TestClient
        from src.host.api import app
        with TestClient(app) as c:
            r = c.post("/lead-mesh/receivers/api_rx", json={
                "channel": "line", "account_id": "@api",
                "daily_cap": 12, "enabled": True})
            assert r.status_code == 200
            assert r.json()["ok"] is True
            # 读出来确认
            r2 = c.get("/lead-mesh/receivers/api_rx")
            assert r2.status_code == 200
            assert r2.json()["channel"] == "line"

    def test_toggle_enabled(self, tmp_receivers):
        tmp_receivers.upsert_receiver("toggle_rx",
            {"channel": "line", "account_id": "@t", "enabled": True})
        from fastapi.testclient import TestClient
        from src.host.api import app
        with TestClient(app) as c:
            r = c.post("/lead-mesh/receivers/toggle_rx",
                        json={"enabled": False})
            assert r.status_code == 200
        assert tmp_receivers.get_receiver("toggle_rx")["enabled"] is False

    def test_pick_endpoint(self, tmp_receivers):
        tmp_receivers.upsert_receiver("pk_rx",
            {"channel": "line", "account_id": "@p",
             "daily_cap": 10, "enabled": True})
        from fastapi.testclient import TestClient
        from src.host.api import app
        with TestClient(app) as c:
            r = c.get("/lead-mesh/receivers-pick?channel=line")
        assert r.status_code == 200
        body = r.json()
        assert body["picked"]["key"] == "pk_rx"
        assert body["all_at_cap"] is False

    def test_delete_via_api(self, tmp_receivers):
        tmp_receivers.upsert_receiver("del_rx",
            {"channel": "line", "account_id": "@d"})
        from fastapi.testclient import TestClient
        from src.host.api import app
        with TestClient(app) as c:
            r = c.delete("/lead-mesh/receivers/del_rx")
            assert r.status_code == 200
            r2 = c.get("/lead-mesh/receivers/del_rx")
            assert r2.status_code == 404
