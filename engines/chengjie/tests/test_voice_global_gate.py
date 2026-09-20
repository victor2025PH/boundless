# -*- coding: utf-8 -*-
"""实施74 阶段2 门禁：B120 自动语音统一全局闸 + B119 发图闸自查面。

- B120（0826 _588/_589 + 0827 07:18 实录）：自动回复设置「启用语音回复」关闭后，
  主动关怀/仪式语音照发。统一语义＝``inbox.l2_autosend.voice.enabled`` **显式
  false** 时 B 线（原生同键）/主动触达/A 线 TG voice_reply 全部禁声；键缺席
  存量行为零变化；坐席手动 send-voice 刻意豁免。
- B119（0826 _580/_581 实录）：全局 ``companion.selfie.enabled`` 无自查面 →
  相册面板加状态行+一键开启端点；英文要图词回归钉住。
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.inbox.voice_autosend import decide_voice, global_voice_reply_off

_ENGINE_ROOT = Path(__file__).resolve().parents[1]


# ── B120 · 全局闸纯函数语义 ──────────────────────────────────────────────────

def test_gate_absent_key_means_no_global_off():
    """键缺席＝各链自己的开关各管各（存量部署零行为变化）。"""
    assert global_voice_reply_off(None) is False
    assert global_voice_reply_off({}) is False
    assert global_voice_reply_off({"inbox": {}}) is False
    assert global_voice_reply_off({"inbox": {"l2_autosend": {}}}) is False
    # voice 块在但没有 enabled 键 → 同样不算显式关
    assert global_voice_reply_off(
        {"inbox": {"l2_autosend": {"voice": {"trigger": "always"}}}}) is False


def test_gate_explicit_false_means_global_off():
    assert global_voice_reply_off(
        {"inbox": {"l2_autosend": {"voice": {"enabled": False}}}}) is True


def test_gate_explicit_true_means_on():
    assert global_voice_reply_off(
        {"inbox": {"l2_autosend": {"voice": {"enabled": True}}}}) is False


def test_gate_garbage_shapes_fail_open():
    assert global_voice_reply_off(
        {"inbox": {"l2_autosend": {"voice": "yes"}}}) is False
    assert global_voice_reply_off({"inbox": "broken"}) is False


def test_b_line_native_gate_same_key():
    """B 线 decide_voice 本就读同键：enabled=false → 文字（统一闸的 B 线半边）。"""
    d = decide_voice({"enabled": False}, "晚上好呀，今天想你了")
    assert d.send_voice is False and d.reason == "disabled"


# ── B120 · 接线契约（静态）─────────────────────────────────────────────────

def test_proactive_topic_wired_global_gate():
    src = (_ENGINE_ROOT / "src" / "companion" / "proactive_topic.py").read_text(
        encoding="utf-8")
    assert "global_voice_reply_off" in src
    assert '"global_gate"' in src  # 跳过原因进媒体归因观测


def test_a_line_sender_wired_global_gate():
    src = (_ENGINE_ROOT / "src" / "client" / "sender.py").read_text(
        encoding="utf-8")
    assert "global_voice_reply_off" in src


def test_manual_send_voice_not_wired_by_design():
    """坐席手动 send-voice 刻意豁免（人的明示决定；与「人工通过≠AI自动发」同原则）。

    反向门禁：手动发送路由不得引用全局闸——有人「顺手统一」时先红这里再讨论。
    """
    src = (_ENGINE_ROOT / "src" / "web" / "routes"
           / "unified_inbox_send_routes.py").read_text(encoding="utf-8")
    assert "global_voice_reply_off" not in src


# ── B119 · 英文要图词回归 ────────────────────────────────────────────────────

def test_selfie_request_english_previous_selfie():
    from src.ai.companion_selfie import detect_selfie_request
    for s in (
        "Just send a previous selfie",
        "Can you send a previous selfie?",
        "send me a previous photo of you",
    ):
        assert detect_selfie_request(s), s


# ── B119 · selfie-gate 端点契约 ─────────────────────────────────────────────

class _CfgMgr:
    def __init__(self, enabled=False):
        self.config = {"companion": {"selfie": {"enabled": enabled}}}
        self.writes = []

    def set_overlay_flag(self, path, value):
        self.writes.append((path, bool(value)))
        # 模拟真实写入后热重载的效果，便于 GET 复读
        self.config.setdefault("companion", {}).setdefault(
            "selfie", {})["enabled"] = bool(value)
        return True, "ok"


class _Audit:
    def __init__(self):
        self.entries = []

    def log(self, user_id, action, target="", old_val="", new_val="", snapshot_id=""):
        self.entries.append((action, target))


def _gate_client(cfg_mgr, audit=None):
    import src.web.routes.persona_media_routes as pmr
    app = FastAPI()
    pmr.register_persona_media_routes(
        app, auth_dep=lambda: True, audit_store=audit, config_manager=cfg_mgr)
    return TestClient(app)


def test_selfie_gate_get_reports_truth():
    c = _gate_client(_CfgMgr(enabled=False))
    r = c.get("/api/personas/selfie-gate")
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is False and body["writable"] is True

    c2 = _gate_client(_CfgMgr(enabled=True))
    assert c2.get("/api/personas/selfie-gate").json()["enabled"] is True


def test_selfie_gate_post_writes_overlay_and_audits():
    cm, audit = _CfgMgr(enabled=False), _Audit()
    c = _gate_client(cm, audit)
    r = c.post("/api/personas/selfie-gate", json={"value": True})
    assert r.status_code == 200 and r.json()["enabled"] is True
    assert cm.writes == [("companion.selfie.enabled", True)]
    assert any(a == "pmedia_selfie_gate" for a, _ in audit.entries)
    # GET 复读到新真值
    assert c.get("/api/personas/selfie-gate").json()["enabled"] is True


def test_selfie_gate_post_without_writer_is_503():
    c = _gate_client(None)
    r = c.post("/api/personas/selfie-gate", json={"value": True})
    assert r.status_code == 503
    # GET 仍可用（只读自查不依赖写能力）
    g = c.get("/api/personas/selfie-gate")
    assert g.status_code == 200 and g.json()["writable"] is False


# ── B119 · 相册面板接线（静态）──────────────────────────────────────────────

def test_personas_album_panel_wired():
    src = (_ENGINE_ROOT / "src" / "web" / "templates" / "personas.html"
           ).read_text(encoding="utf-8")
    assert "/api/personas/selfie-gate" in src
    assert "pmaEnableSelfieGate" in src
    assert "window.pmaEnableSelfieGate = pmaEnableSelfieGate" in src
    assert "pma_gate_off" in src and "pma_gate_enable" in src
