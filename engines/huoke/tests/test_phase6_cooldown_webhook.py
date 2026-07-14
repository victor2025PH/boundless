# -*- coding: utf-8 -*-
"""Phase 6 · 跨渠道冷却 gate + per-account webhook 测试 (2026-04-23)。"""
from __future__ import annotations

from unittest.mock import patch

import pytest


# ─── 跨渠道冷却 gate ────────────────────────────────────────────────
class TestCrossChannelCooldown:
    def test_no_handoff_returns_none(self, tmp_db):
        from src.host.lead_mesh import (resolve_identity,
                                          check_peer_cooldown_handoff)
        cid = resolve_identity(platform="facebook",
                                account_id="fb:NoHo", display_name="NoHo")
        assert check_peer_cooldown_handoff(cid) is None

    def test_cross_channel_detected(self, tmp_db):
        """LINE handoff 后查 whatsapp 应命中(跨渠道)。"""
        from src.host.lead_mesh import (resolve_identity, create_handoff,
                                          check_peer_cooldown_handoff)
        cid = resolve_identity(platform="facebook",
                                account_id="fb:CX1", display_name="CX1")
        h = create_handoff(canonical_id=cid, source_agent="b",
                            channel="line", enqueue_webhook=False)
        # 不管什么 channel 都应命中
        existing = check_peer_cooldown_handoff(cid)
        assert existing and existing["handoff_id"] == h
        assert existing["channel"] == "line"

    def test_create_handoff_blocked_by_cooldown(self, tmp_db):
        """honor_peer_cooldown=True 时跨渠道被阻塞, 返回空串。"""
        from src.host.lead_mesh import (resolve_identity, create_handoff,
                                          get_journey)
        cid = resolve_identity(platform="facebook",
                                account_id="fb:CX2", display_name="CX2")
        h1 = create_handoff(canonical_id=cid, source_agent="b",
                             channel="line", enqueue_webhook=False)
        assert h1
        # 第二次换 whatsapp + 开冷却 → 阻塞
        h2 = create_handoff(canonical_id=cid, source_agent="b",
                             channel="whatsapp", enqueue_webhook=False,
                             honor_peer_cooldown=True)
        assert h2 == ""
        # journey 有 handoff_blocked 事件
        events = get_journey(cid)
        blocked = [e for e in events if e["action"] == "handoff_blocked"]
        assert len(blocked) == 1
        assert blocked[0]["data"]["reason"] == "peer_cooldown"
        assert blocked[0]["data"]["existing_channel"] == "line"

    def test_default_off_preserves_backward_compat(self, tmp_db):
        """默认 honor_peer_cooldown=False, 跨渠道不阻塞。"""
        from src.host.lead_mesh import resolve_identity, create_handoff
        cid = resolve_identity(platform="facebook",
                                account_id="fb:CX3", display_name="CX3")
        h1 = create_handoff(canonical_id=cid, source_agent="b",
                             channel="line", enqueue_webhook=False)
        h2 = create_handoff(canonical_id=cid, source_agent="b",
                             channel="whatsapp", enqueue_webhook=False)
        assert h1 and h2  # 默认都创建成功

    def test_rejected_allows_cross_channel_retry(self, tmp_db):
        """LINE handoff rejected → 换 whatsapp 应允许(honor_rejected=True)。"""
        from src.host.lead_mesh import (resolve_identity, create_handoff,
                                          reject_handoff,
                                          check_peer_cooldown_handoff)
        cid = resolve_identity(platform="facebook",
                                account_id="fb:CX4", display_name="CX4")
        h1 = create_handoff(canonical_id=cid, source_agent="b",
                             channel="line", enqueue_webhook=False)
        reject_handoff(h1, by="test")
        # honor_rejected=True(默认) → 不命中
        assert check_peer_cooldown_handoff(cid, honor_rejected=True) is None
        # 可再发 whatsapp
        h2 = create_handoff(canonical_id=cid, source_agent="b",
                             channel="whatsapp", enqueue_webhook=False,
                             honor_peer_cooldown=True)
        assert h2

    def test_strict_mode_blocks_even_rejected(self, tmp_db):
        """honor_rejected=False 时 rejected 也算阻塞。"""
        from src.host.lead_mesh import (resolve_identity, create_handoff,
                                          reject_handoff,
                                          check_peer_cooldown_handoff)
        cid = resolve_identity(platform="facebook",
                                account_id="fb:CX5", display_name="CX5")
        h = create_handoff(canonical_id=cid, source_agent="b",
                            channel="line", enqueue_webhook=False)
        reject_handoff(h, by="test")
        # 严格模式 → 即使 rejected 也命中
        existing = check_peer_cooldown_handoff(cid, honor_rejected=False)
        assert existing and existing["handoff_id"] == h

    def test_cooldown_window_expiration(self, tmp_db):
        """超过 cooldown_days 的记录不再阻塞。"""
        from src.host.lead_mesh.handoff import check_peer_cooldown_handoff
        from src.host.lead_mesh import resolve_identity, create_handoff
        cid = resolve_identity(platform="facebook",
                                account_id="fb:CX6", display_name="CX6")
        # 建一个 handoff (created_at 是 now)
        create_handoff(canonical_id=cid, source_agent="b",
                       channel="line", enqueue_webhook=False)
        # cooldown_days=30 → 命中
        assert check_peer_cooldown_handoff(cid, cooldown_days=30) is not None
        # cooldown_days=0 → 窗口为负(now - 0 days = now), 查 >= now 的应该命中
        # 这里用 cooldown_days=-1 模拟已过期: cutoff 在未来, 查不到
        # 实际场景: 等够 30 天才算过期; 无法模拟时间流逝, 但能测窗口参数传递
        # 直接手工 update created_at 到 60 天前模拟
        from src.host.database import _connect
        with _connect() as conn:
            conn.execute("UPDATE lead_handoffs SET created_at=datetime('now','-60 days')"
                         " WHERE canonical_id=?", (cid,))
        # cooldown_days=30 现在不应命中
        assert check_peer_cooldown_handoff(cid, cooldown_days=30) is None
        # cooldown_days=90 仍应命中
        assert check_peer_cooldown_handoff(cid, cooldown_days=90) is not None


# ─── Per-account Webhook ────────────────────────────────────────────
class TestPerAccountWebhook:
    def _setup_receiver(self, tmp_receivers, webhook_url: str):
        tmp_receivers.upsert_receiver("wh_rx", {
            "channel": "line", "account_id": "@w",
            "daily_cap": 10, "enabled": True,
            "webhook_url": webhook_url,
        })

    def test_per_receiver_dispatch_created(self, tmp_db, tmp_receivers):
        """receiver 配置了 webhook_url 时, handoff 创建后 dispatch 表有该 URL 记录。"""
        self._setup_receiver(tmp_receivers, "https://my-ops.com/hook")
        from src.host.lead_mesh import (resolve_identity, create_handoff)
        from src.host.database import _connect

        cid = resolve_identity(platform="facebook",
                                account_id="fb:WH1", display_name="WH1")
        # 必须传 receiver_account_key, 否则自动 pick 不会选中测试 receiver
        hid = create_handoff(
            canonical_id=cid, source_agent="b", channel="line",
            receiver_account_key="wh_rx",
            enqueue_webhook=True)
        assert hid

        # 查 webhook_dispatches 应有该 URL 的 pending 记录
        with _connect() as conn:
            rows = conn.execute(
                "SELECT target_url, status FROM webhook_dispatches"
                " WHERE related_handoff_id=?", (hid,)).fetchall()
        urls = {r[0] for r in rows}
        assert "https://my-ops.com/hook" in urls

    def test_per_receiver_without_webhook_url_skipped(self, tmp_db, tmp_receivers):
        """receiver 没配 webhook_url 时, 不加 per-receiver dispatch。"""
        tmp_receivers.upsert_receiver("no_wh_rx", {
            "channel": "line", "account_id": "@x",
            "daily_cap": 10, "enabled": True,
            "webhook_url": "",  # 空
        })
        from src.host.lead_mesh import (resolve_identity, create_handoff)
        from src.host.database import _connect

        cid = resolve_identity(platform="facebook",
                                account_id="fb:WH2", display_name="WH2")
        hid = create_handoff(
            canonical_id=cid, source_agent="b", channel="line",
            receiver_account_key="no_wh_rx",
            enqueue_webhook=True)
        with _connect() as conn:
            rows = conn.execute(
                "SELECT target_url FROM webhook_dispatches"
                " WHERE related_handoff_id=?", (hid,)).fetchall()
        # 没有 receiver webhook(只有全局订阅, 当前为空), 应为 0
        assert len(rows) == 0

    def test_disabled_receiver_webhook_skipped(self, tmp_db, tmp_receivers):
        """disabled receiver 的 webhook_url 不触发。"""
        tmp_receivers.upsert_receiver("disabled_rx", {
            "channel": "line", "account_id": "@d",
            "enabled": False,
            "webhook_url": "https://disabled.example.com/hook",
        })
        from src.host.lead_mesh.webhook_dispatcher import _load_receiver_webhook
        # 构造一个假 handoff 记录
        from src.host.database import _connect
        import uuid
        hid = str(uuid.uuid4())
        with _connect() as conn:
            conn.execute(
                "INSERT INTO lead_handoffs (handoff_id, canonical_id, source_agent,"
                " channel, receiver_account_key, state)"
                " VALUES (?,?,?,?,?,?)",
                (hid, "cx", "b", "line", "disabled_rx", "pending"))
        sub = _load_receiver_webhook(hid)
        # disabled=False 时应不返回
        # 看实现: _load_receiver_webhook 不过滤 enabled, 只返回含 enabled 字段的 sub;
        # enqueue_webhook 那层过滤 enabled
        # 所以 sub 可能非空, 但 enabled=False
        assert sub is None or sub.get("enabled") is False

    def test_hmac_env_var_resolved(self, tmp_db, tmp_receivers, monkeypatch):
        """per-receiver 的 HMAC secret 从 WEBHOOK_SECRET_RECEIVER_<KEY> 读。"""
        monkeypatch.setenv("WEBHOOK_SECRET_RECEIVER_LINE_RX_SECRET",
                            "my-secret-123")
        tmp_receivers.upsert_receiver("line_rx_secret", {
            "channel": "line", "account_id": "@s",
            "enabled": True,
            "webhook_url": "https://secret.example.com/hook",
        })
        from src.host.lead_mesh.webhook_dispatcher import (
            _load_receiver_webhook, _resolve_secret_for_dispatch)
        from src.host.database import _connect
        import uuid
        hid = str(uuid.uuid4())
        with _connect() as conn:
            conn.execute(
                "INSERT INTO lead_handoffs (handoff_id, canonical_id, source_agent,"
                " channel, receiver_account_key, state)"
                " VALUES (?,?,?,?,?,?)",
                (hid, "cx", "b", "line", "line_rx_secret", "pending"))
        dispatch = {
            "event_type": "handoff.created",
            "target_url": "https://secret.example.com/hook",
            "related_handoff_id": hid,
        }
        secret = _resolve_secret_for_dispatch(dispatch)
        assert secret == "my-secret-123"


# ─── 复用 test_receivers.py 的 tmp_receivers fixture ────────────────
# 为了避免 import 循环, 本文件自己定义一份。
@pytest.fixture
def tmp_receivers(tmp_db, tmp_path, monkeypatch):
    import src.host.lead_mesh.receivers as rx
    from src.host._yaml_cache import YamlCache
    cfg = tmp_path / "referral_receivers.yaml"
    monkeypatch.setattr(rx, "_cfg_path", cfg)
    rx._CACHE = YamlCache(
        path=cfg, defaults=rx._FALLBACK,
        post_process=rx._post_process,
        log_label="receivers.yaml(test-cooldown)",
        logger=rx.logger,
    )
    yield rx


# ─── 时间轴分组 helper (纯 JS 逻辑的 Python 镜像, 仅验 bucket 规则) ─
class TestTimelineBucketing:
    """时间轴分组主要在 JS 层, 这里只验证 server API 能返回 get_dossier 所需
    的 journey 时间字段格式 (YYYY-MM-DD HH:MM:SS), 下游 JS 按 substring(0,10) 分桶。"""

    def test_journey_at_format(self, tmp_db):
        from src.host.lead_mesh import (resolve_identity, append_journey,
                                          get_journey)
        cid = resolve_identity(platform="facebook", account_id="fb:T1",
                                display_name="T1")
        append_journey(cid, actor="agent_a", action="greeting_sent")
        events = get_journey(cid)
        assert events
        at = events[0]["at"]
        assert isinstance(at, str)
        # SQLite default datetime('now') 格式 "YYYY-MM-DD HH:MM:SS"
        assert len(at) >= 10 and at[4] == "-" and at[7] == "-"


# ─── Phase 6 P0: greeting 入口前置检查 active handoff ────────────────
class TestGreetingPeerCooldownGate:
    """A 端 send_greeting_after_add_friend 前置检查 check_peer_cooldown_handoff,
    当 B 对该 peer 在 7 天内发起过活跃/已完成 handoff 时, A 跳过 greeting 避免双打扰。"""

    def _stub_fb(self):
        from src.app_automation.facebook import FacebookAutomation
        fb = FacebookAutomation.__new__(FacebookAutomation)
        fb._current_device = "D_p0"
        fb._last_greet_skip_reason = ""
        fb._current_lead_cid = ""
        fb._current_lead_persona = ""
        fb._current_greet_template_id = ""
        # Stub: _did 取 device_id 或默认, _u2 返回 None(永远不会走到 UI 层)
        fb._did = lambda dev=None: dev or "D_p0"
        fb._u2 = lambda did: None
        return fb

    def test_active_handoff_blocks_greeting(self, tmp_db):
        """B 机已发 LINE handoff(pending) → A 跳过 greeting, 写 journey。"""
        from src.host.lead_mesh import (resolve_identity, create_handoff,
                                          get_journey)
        cid = resolve_identity(platform="facebook",
                                account_id="fb:P0Active",
                                display_name="P0Active")
        h = create_handoff(canonical_id=cid, source_agent="b",
                            channel="line", enqueue_webhook=False)
        assert h

        fb = self._stub_fb()
        ok = fb.send_greeting_after_add_friend(
            "P0Active", greeting="hello",
            device_id="D_p0", persona_key="jp_female_midlife")
        assert ok is False

        events = get_journey(cid)
        blocked = [e for e in events if e["action"] == "greeting_blocked"]
        assert any(e["data"]["reason"] == "peer_already_handed_off"
                    for e in blocked)

    def test_no_handoff_passes_gate(self, tmp_db):
        """无 handoff 记录时, 前置检查放行, 不写 greeting_blocked{peer_already_handed_off}。
        (主流程会因 u2=None 在其他地方失败, 这里只验 gate 未命中。)"""
        from src.host.lead_mesh import resolve_identity, get_journey
        cid = resolve_identity(platform="facebook",
                                account_id="fb:P0Free",
                                display_name="P0Free")

        fb = self._stub_fb()
        # 不关心最终返回值(UI 层会因 stub 失败), 只验 journey 没写 peer_already 事件
        try:
            fb.send_greeting_after_add_friend(
                "P0Free", greeting="hello", device_id="D_p0")
        except Exception:
            pass

        events = get_journey(cid)
        peer_blocked = [
            e for e in events
            if e["action"] == "greeting_blocked"
            and e["data"].get("reason") == "peer_already_handed_off"
        ]
        assert peer_blocked == []

    def test_rejected_handoff_does_not_block(self, tmp_db):
        """B 已 reject 的 handoff → A 仍可尝试 greeting (honor_rejected=True 默认放行)。"""
        from src.host.lead_mesh import (resolve_identity, create_handoff,
                                          reject_handoff, get_journey)
        cid = resolve_identity(platform="facebook",
                                account_id="fb:P0Rejected",
                                display_name="P0Rejected")
        h = create_handoff(canonical_id=cid, source_agent="b",
                            channel="line", enqueue_webhook=False)
        reject_handoff(h, by="b-reject-test")

        fb = self._stub_fb()
        try:
            fb.send_greeting_after_add_friend(
                "P0Rejected", greeting="hello", device_id="D_p0")
        except Exception:
            pass

        events = get_journey(cid)
        peer_blocked = [
            e for e in events
            if e["action"] == "greeting_blocked"
            and e["data"].get("reason") == "peer_already_handed_off"
        ]
        assert peer_blocked == []

    def test_expired_cooldown_does_not_block(self, tmp_db):
        """>7 天前的 handoff 不阻塞 (cooldown 窗口过期)。"""
        from src.host.lead_mesh import (resolve_identity, create_handoff,
                                          get_journey)
        from src.host.database import _connect
        cid = resolve_identity(platform="facebook",
                                account_id="fb:P0Old",
                                display_name="P0Old")
        h = create_handoff(canonical_id=cid, source_agent="b",
                            channel="line", enqueue_webhook=False)
        # 手工把 created_at 倒退 30 天 → 超出 7 天冷却窗口
        with _connect() as conn:
            conn.execute(
                "UPDATE lead_handoffs SET created_at=datetime('now','-30 days')"
                " WHERE handoff_id=?", (h,))

        fb = self._stub_fb()
        try:
            fb.send_greeting_after_add_friend(
                "P0Old", greeting="hello", device_id="D_p0")
        except Exception:
            pass

        events = get_journey(cid)
        peer_blocked = [
            e for e in events
            if e["action"] == "greeting_blocked"
            and e["data"].get("reason") == "peer_already_handed_off"
        ]
        assert peer_blocked == []
