# -*- coding: utf-8 -*-
"""实施86 0830 午批·批D 门禁：发送真相层（#72 假阴性对账 + #73 dead-peer 误标）。

金标=DC8F 包 UDEKBY 铁证：05:44 手动发送界面报「失败」而 05:47 轮询镜像确认
实际送达（慢确认 30-65s/条撞前端超时）；同一 peer 被 blocked 标 4 分钟内自动链
持续跳过、送达成功后标记仍不解除（AI 对该客户永久静默且坐席无感知）。
"""
from __future__ import annotations

import time
from pathlib import Path

_ENGINE_ROOT = Path(__file__).resolve().parents[1]


# ── #72：文本发送对账（复用 voice_send_tracker，text: scope） ────────────────

def test_text_send_tracker_three_states():
    from src.inbox import voice_send_tracker as t
    scope = "text:telegram:acct:peer72"
    t.record_start(scope, "cmid-1")
    st = t.get_status(scope, "cmid-1")
    assert st["state"] == "in_flight"
    t.record_sent(scope, "cmid-1", payload={"message_id": "86556"})
    st2 = t.get_status(scope, "cmid-1")
    assert st2["state"] == "sent"
    assert st2["message_id"] == "86556"
    t.record_start(scope, "cmid-2")
    t.record_failed(scope, "cmid-2", "near_duplicate")
    assert t.get_status(scope, "cmid-2")["state"] == "failed"
    # 查无此键=unknown（老后端/过期/从未提交成功——前端按保守提示处理）
    assert t.get_status(scope, "cmid-ghost")["state"] == "unknown"


def test_send_route_wires_text_tracker():
    """路由静态契约：开始/终局各出口都落账 + 对账端点存在。"""
    src = (_ENGINE_ROOT / "src" / "web" / "routes"
           / "unified_inbox_send_routes.py").read_text(encoding="utf-8")
    assert 'f"text:{platform}:{account_id}:{chat_key}"' in src
    assert src.count("_track_failed(") >= 5   # 双守卫/两异常/undelivered
    assert "record_sent(_track_scope" in src
    assert '@app.get("/api/unified-inbox/send-status")' in src


def test_send_status_route_in_inventory():
    src = (_ENGINE_ROOT / "tests"
           / "test_admin_route_inventory.py").read_text(encoding="utf-8")
    assert "/api/unified-inbox/send-status\tGET" in src


# ── #73：dead-peer 误标三件套 ────────────────────────────────────────────────

def _reg(tmp_path, **kw):
    from src.ops.dead_peer_registry import DeadPeerRegistry
    return DeadPeerRegistry(path=str(tmp_path / "dp.json"), **kw)


def test_blocked_gets_default_ttl_not_permanent(tmp_path):
    """#73-②：blocked 未配置 TTL 时不再永久——内置 7 天到期放行探路。"""
    r = _reg(tmp_path)
    r.record("telegram", "8730484257", "blocked", evidence="USER_IS_BLOCKED x")
    assert r.is_blocked("telegram", "8730484257") is True
    # 篡改时间戳到 8 天前 → 过期释放
    key = "telegram:8730484257"
    r._data[key]["ts"] = time.time() - 8 * 86400
    assert r.is_blocked("telegram", "8730484257") is False


def test_deactivated_stays_permanent(tmp_path):
    """账号注销不可逆——内置 TTL 绝不适用（安全第一）。"""
    r = _reg(tmp_path)
    r.record("telegram", "111", "deactivated")
    r._data["telegram:111"]["ts"] = time.time() - 365 * 86400
    assert r.is_blocked("telegram", "111") is True


def test_evidence_provenance_recorded(tmp_path):
    """#73-①：标记带来源证据——「这个标哪来的」从无人能答变成条目自带答案。"""
    r = _reg(tmp_path)
    r.record("telegram", "222", "blocked",
             evidence="USER_IS_BLOCKED: [400 USER_IS_BLOCKED] ...")
    info = r.info_of("telegram", "222")
    assert info and info["evidence"].startswith("USER_IS_BLOCKED")
    assert r.info_of("telegram", "ghost") is None


def test_clear_on_delivery_unblocks(tmp_path, monkeypatch):
    """#73-②：真实送达即清标（送达成功=可达的最硬证据）。"""
    import src.ops.dead_peer_registry as dpr
    r = _reg(tmp_path)
    monkeypatch.setattr(dpr, "_SINGLETON", r)
    r.record("telegram", "8730484257", "blocked")
    assert dpr.clear_on_delivery("telegram", "8730484257") is True
    assert r.is_blocked("telegram", "8730484257") is False
    # 未标记/单例缺席都是 no-op False
    assert dpr.clear_on_delivery("telegram", "8730484257") is False
    monkeypatch.setattr(dpr, "_SINGLETON", None)
    assert dpr.clear_on_delivery("telegram", "1") is False


def test_send_route_clears_dead_peer_on_success():
    src = (_ENGINE_ROOT / "src" / "web" / "routes"
           / "unified_inbox_send_routes.py").read_text(encoding="utf-8")
    assert "clear_on_delivery" in src


def test_sender_clears_and_records_evidence():
    src = (_ENGINE_ROOT / "src" / "client" / "sender.py").read_text(
        encoding="utf-8")
    assert "unblock(" in src            # 送达成功清标
    assert "evidence=str(e)" in src     # 记录来源证据


def test_send_caps_exposes_dead_peer_73():
    """#73-③：send-caps 带 chat_key 时回 dead_peer 可见块（坐席看得见停用+原因）。"""
    src = (_ENGINE_ROOT / "src" / "web" / "routes"
           / "unified_inbox_send_routes.py").read_text(encoding="utf-8")
    assert '"dead_peer"' in src
    assert "info_of" in src
