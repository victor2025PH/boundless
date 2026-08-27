# -*- coding: utf-8 -*-
"""体检面板 P2 增强门禁（2026-08-21「平台封顶忘摘」事故闭环）。

事故：8/18 因 WA 无发送通道临时封顶 `platform_modes: {whatsapp: review}`，
8/20 通道接回 + 根因（线索占位符拟稿）另行修掉后**没人记得摘封顶**，
自动发送多停了一天半，坐席只看到「恢复需运营调整 platform_modes」的黑话。

三件闭环（均须与面板文案契约同步，键在 i18n pack inbox_takeover_diag）：

1. ``cap_platform`` finding 带 ``note``（运营备注，读
   ``inbox.auto_draft.platform_mode_notes.<平台>``）与 ``channel_ok``
   （会话账号 registry online 且 mode ∈ ORCHESTRATED_MODES ＝发送通道
   大概率活着 → 前端提示「封顶原因可能已消除」）；求值失败一律 False。
2. ``pending_drafts`` finding 带 ``stale``/``stale_h``（陈旧护栏预判，
   与 ``stale_approve_hours`` 同阈值口径）——面板引导坐席去点「通过」前
   先说清「会被 409 拦、该重新生成」；阈值 0=护栏关=永不标 stale。
3. ``POST /api/unified-inbox/platform-cap``：主管设置/解除平台封顶，
   写 overlay 单键；**解除必须写显式空串**（删键会回落构造期快照——
   P0 当天实锤快照里还躺着旧封顶）；平台名净化防点分路径注入。
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.inbox.reply_diagnosis import diagnose_conversation


class _Store:
    """最小假 store：只实现 diagnose_conversation 会摸的方法（密闭零 IO）。"""

    def __init__(self, drafts: Optional[List[Dict[str, Any]]] = None):
        self._drafts = drafts or []

    def get_conversation(self, cid: str) -> Dict[str, Any]:
        return {"conversation_id": cid, "platform": "whatsapp",
                "chat_type": "private", "display_name": "N", "peer_is_bot": 0}

    def get_automation_mode_if_set(self, cid: str) -> str:
        return "auto_ai"

    def get_automation_mode_meta(self, cid: str) -> Dict[str, Any]:
        return {"mode": "auto_ai", "source": "human", "updated_at": 0.0}

    def list_automation_mode_log(self, cid: str, limit: int = 10) -> list:
        return []

    def list_recent_messages(self, cid: str, limit: int = 120) -> list:
        return []

    def list_drafts(self, status: str = "pending", conversation_id: str = "",
                    limit: int = 20) -> list:
        return list(self._drafts)


_CFG_CAP = {"inbox": {"auto_draft": {
    "platform_modes": {"whatsapp": "review"},
    "platform_mode_notes": {"whatsapp": "8/18 通道故障封顶"},
}}}


def _by_code(out: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {f["code"]: f for f in out.get("findings", [])}


def _patch_registry(monkeypatch, row):
    import src.integrations.account_registry as reg
    monkeypatch.setattr(reg, "peek_account", lambda p, a: row)


def _diag(cfg, store=None):
    return diagnose_conversation(
        store if store is not None else _Store(), cfg,
        platform="whatsapp", account_id="639270135480", chat_key="p1")


# ── ① cap_platform：note + channel_ok ────────────────────────────────

def test_cap_platform_carries_note_and_channel_ok(monkeypatch):
    _patch_registry(monkeypatch, {"status": "online", "mode": "protocol",
                                  "created_at": 0.0})
    f = _by_code(_diag(_CFG_CAP))["cap_platform"]
    assert f["params"]["note"] == "8/18 通道故障封顶"
    assert f["params"]["channel_ok"] is True
    assert f["params"]["ceiling"] == "review"


def test_cap_platform_personal_rpa_is_not_a_send_channel(monkeypatch):
    # personal_rpa 不在 ORCHESTRATED_MODES：账号「online」也没有本机发送
    # 通道（leadbus 外部 RPA 归属账号），绝不能提示「可评估解除」。
    _patch_registry(monkeypatch, {"status": "online", "mode": "personal_rpa",
                                  "created_at": 0.0})
    f = _by_code(_diag(_CFG_CAP))["cap_platform"]
    assert f["params"]["channel_ok"] is False


def test_cap_platform_offline_or_missing_account(monkeypatch):
    _patch_registry(monkeypatch, {"status": "offline", "mode": "protocol",
                                  "created_at": 0.0})
    f = _by_code(_diag(_CFG_CAP))["cap_platform"]
    assert f["params"]["channel_ok"] is False
    _patch_registry(monkeypatch, None)
    f2 = _by_code(_diag(_CFG_CAP))["cap_platform"]
    assert f2["params"]["channel_ok"] is False
    assert f2["params"]["note"] == "8/18 通道故障封顶"  # note 与账号无关仍在


def test_cap_platform_no_note_configured(monkeypatch):
    _patch_registry(monkeypatch, None)
    cfg = {"inbox": {"auto_draft": {"platform_modes": {"whatsapp": "review"}}}}
    f = _by_code(_diag(cfg))["cap_platform"]
    assert f["params"]["note"] == ""


# ── ② pending_drafts：陈旧护栏预判 ───────────────────────────────────

def test_pending_drafts_stale_flag_default_threshold(monkeypatch):
    _patch_registry(monkeypatch, None)
    store = _Store(drafts=[{"created_at": time.time() - 30 * 3600.0}])
    f = _by_code(_diag({}, store))["pending_drafts"]
    assert f["params"]["stale"] is True
    assert f["params"]["stale_h"] == 24.0


def test_pending_drafts_fresh_not_stale(monkeypatch):
    _patch_registry(monkeypatch, None)
    store = _Store(drafts=[{"created_at": time.time() - 3600.0}])
    f = _by_code(_diag({}, store))["pending_drafts"]
    assert f["params"]["stale"] is False


def test_pending_drafts_threshold_zero_disables_stale(monkeypatch):
    # stale_approve_hours=0 = 护栏关：再老的稿也不标（预判必须与护栏一致）
    _patch_registry(monkeypatch, None)
    cfg = {"inbox": {"auto_draft": {"stale_approve_hours": 0}}}
    store = _Store(drafts=[{"created_at": time.time() - 300 * 3600.0}])
    f = _by_code(_diag(cfg, store))["pending_drafts"]
    assert f["params"]["stale"] is False


# ── ③ POST /api/unified-inbox/platform-cap ──────────────────────────

class _Templates:
    def TemplateResponse(self, *a, **k):
        raise AssertionError("page rendering is not used in API tests")


def _mk_app(monkeypatch, allow: bool):
    from src.web.routes.unified_inbox_routes import register_unified_inbox_routes
    import src.web.routes.unified_inbox_auth as auth_mod
    monkeypatch.setattr(auth_mod, "_is_supervisor", lambda request: allow)
    app = FastAPI()
    register_unified_inbox_routes(
        app, page_auth=lambda request: True, api_auth=lambda request: True,
        templates=_Templates())
    calls: List[tuple] = []

    class _CM:
        config: Dict[str, Any] = {}

        def set_overlay_flag(self, path, value):
            calls.append((path, value))
            return True, "已保存"

    app.state.config_manager = _CM()
    return app, calls


def test_platform_cap_clear_writes_explicit_empty_string(monkeypatch):
    app, calls = _mk_app(monkeypatch, allow=True)
    c = TestClient(app)
    r = c.post("/api/unified-inbox/platform-cap",
               json={"platform": "WhatsApp", "ceiling": ""})
    assert r.status_code == 200 and r.json()["ok"] is True
    # 解除=显式空串（删键会回落构造期快照）；平台名归一小写
    assert calls == [("inbox.auto_draft.platform_modes.whatsapp", "")]


def test_platform_cap_set_review(monkeypatch):
    app, calls = _mk_app(monkeypatch, allow=True)
    c = TestClient(app)
    r = c.post("/api/unified-inbox/platform-cap",
               json={"platform": "messenger", "ceiling": "review"})
    assert r.status_code == 200
    assert calls == [("inbox.auto_draft.platform_modes.messenger", "review")]


def test_platform_cap_rejects_bad_ceiling_and_platform(monkeypatch):
    app, calls = _mk_app(monkeypatch, allow=True)
    c = TestClient(app)
    assert c.post("/api/unified-inbox/platform-cap",
                  json={"platform": "whatsapp", "ceiling": "auto_ai"}
                  ).status_code == 400
    # 平台名进点分 overlay 路径：带点/斜杠必须拒（防键注入）
    assert c.post("/api/unified-inbox/platform-cap",
                  json={"platform": "a.b", "ceiling": ""}).status_code == 400
    assert c.post("/api/unified-inbox/platform-cap",
                  json={"ceiling": ""}).status_code == 400
    assert calls == []


def test_platform_cap_requires_supervisor(monkeypatch):
    app, calls = _mk_app(monkeypatch, allow=False)
    c = TestClient(app)
    r = c.post("/api/unified-inbox/platform-cap",
               json={"platform": "whatsapp", "ceiling": ""})
    assert r.status_code == 403
    assert calls == []
