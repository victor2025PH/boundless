# -*- coding: utf-8 -*-
"""新账号「AI 接管方式」确认（账号级档位层，P0 2026-08-30）门禁。

守什么：
- 功能关 = 完全旧行为（account_mode_for 恒 None，解析链回落全局）；
- 新账号（注册表 created_at 晚于启用基线）未确认 → 生效档 review（安全默认，
  AI 只写稿不发送）；确认后按所选档；存量账号零打扰；
- 解析层级：会话显式 > 账号级 > 全局；群守卫不被账号层 auto_ai 绕过；
- 账号层生效期 bootstrap 不落盘（会话动态跟随账号决策）；
- 决策对齐只动系统落档行（bootstrap/standby/account_mode），human 行绝不覆盖，
  升 auto_ai 跳过群；
- 路由契约：GET 清单 / POST 决策（主管权限、非法档位拒绝、功能关拒绝）。
"""
import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

import src.inbox.account_mode_onboarding as amo
from src.inbox.automation_mode import (
    maybe_bootstrap_automation_mode,
    resolve_automation_mode,
)

NOW = time.time()
CFG_ON = {"inbox": {"auto_draft": {
    "automation_mode": "auto_ai",
    "account_mode_onboarding": {"enabled": True},
}}}
CFG_OFF = {"inbox": {"auto_draft": {
    "automation_mode": "auto_ai",
    "account_mode_onboarding": {"enabled": False},
}}}


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    """状态文件逐测试隔离 + 进程缓存复位（禁止互串/写生产）。"""
    p = tmp_path / "account_mode_onboarding.json"
    monkeypatch.setattr(amo, "_state_path", lambda: p)
    amo._reset_for_tests()
    yield
    amo._reset_for_tests()


def _connected(ts_map):
    """替身：resolve_account_connected_at 按 (platform:account) 查表，缺省 0.0。"""
    def fn(platform, account_id, *, now=None):
        return float(ts_map.get(f"{platform}:{account_id}", 0.0))
    return fn


class _FakeStore:
    """automation_mode 解析/对齐所需的最小 store 假件。"""

    def __init__(self, explicit=None, rows=None, groups=()):
        self.explicit = dict(explicit or {})
        self.rows = list(rows or [])
        self.groups = set(groups)
        self.set_calls = []

    def get_automation_mode_if_set(self, cid):
        return self.explicit.get(cid)

    def set_automation_mode(self, cid, mode, *, source=""):
        self.set_calls.append((cid, mode, source))
        self.explicit[cid] = mode

    def list_automation_mode_rows(self):
        return list(self.rows)

    def get_conversation(self, cid):
        if cid in self.groups:
            return {"conversation_id": cid, "chat_type": "group"}
        return {"conversation_id": cid, "chat_type": "private"}


# ── 纯函数层 ──────────────────────────────────────────────────────────────

def test_disabled_means_no_opinion_and_global_fallback(monkeypatch):
    monkeypatch.setattr(amo, "_resolve_connected_at",
                        _connected({"telegram:acc1": NOW + 999}))
    assert amo.account_mode_for("telegram", "acc1", CFG_OFF) is None
    store = _FakeStore()
    assert resolve_automation_mode(
        store, "telegram:acc1:peer", CFG_OFF) == "auto_ai"


def test_baseline_freezes_on_first_call():
    b1 = amo.ensure_baseline(now=1000.0)
    b2 = amo.ensure_baseline(now=2000.0)
    assert b1 == b2 == 1000.0


def test_new_account_defaults_review_old_and_unknown_fall_through(monkeypatch):
    amo.ensure_baseline(now=NOW - 3600)
    monkeypatch.setattr(amo, "_resolve_connected_at", _connected({
        "telegram:new1": NOW,                 # 基线后接入 → 新账号
        "telegram:old1": NOW - 30 * 86400,    # 早于 baseline 超过宽限 → 存量
        # telegram:unknown 不在表里 → 0.0
    }))
    assert amo.account_mode_for("telegram", "new1", CFG_ON) == "review"
    assert amo.account_mode_for("telegram", "old1", CFG_ON) is None
    # #167：判不出按待确认（首登协议号常无 created_at），不再 fail-open 成存量
    assert amo.account_mode_for("telegram", "unknown", CFG_ON) == "review"


def test_decide_overrides_pending_default(monkeypatch):
    amo.ensure_baseline(now=NOW - 3600)
    monkeypatch.setattr(amo, "_resolve_connected_at",
                        _connected({"telegram:new1": NOW}))
    assert amo.decide_account_mode("telegram", "new1", "auto_ai", actor="boss")
    assert amo.account_mode_for("telegram", "new1", CFG_ON) == "auto_ai"
    # 改档（决策可换，不需要「恢复默认」逃生门）
    assert amo.decide_account_mode("telegram", "new1", "manual")
    assert amo.account_mode_for("telegram", "new1", CFG_ON) == "manual"


def test_decide_rejects_bad_mode_and_empty_ids():
    assert not amo.decide_account_mode("telegram", "a1", "multi_choice")
    assert not amo.decide_account_mode("telegram", "a1", "on")
    assert not amo.decide_account_mode("telegram", "", "review")
    assert not amo.decide_account_mode("", "a1", "review")


def test_decisions_persist_across_cache_reset(monkeypatch):
    amo.ensure_baseline(now=NOW - 3600)
    amo.decide_account_mode("line", "u9", "review", actor="op")
    amo._reset_for_tests()   # 模拟进程重启（重新从盘装载）
    assert amo.decided_mode("line", "u9") == "review"
    snap = amo.decisions_snapshot()
    assert snap and snap[0]["platform"] == "line" and snap[0]["mode"] == "review"


# ── 解析链接入 ────────────────────────────────────────────────────────────

def test_resolve_priority_explicit_over_account_over_global(monkeypatch):
    amo.ensure_baseline(now=NOW - 3600)
    monkeypatch.setattr(amo, "_resolve_connected_at",
                        _connected({"telegram:new1": NOW}))
    cid = "telegram:new1:peer"
    # 账号层（pending → review）压过全局 auto_ai
    assert resolve_automation_mode(_FakeStore(), cid, CFG_ON) == "review"
    # 会话显式设置压过账号层
    st = _FakeStore(explicit={cid: "auto_ai"})
    assert resolve_automation_mode(st, cid, CFG_ON) == "auto_ai"
    # 决策后跟随决策
    amo.decide_account_mode("telegram", "new1", "manual")
    assert resolve_automation_mode(_FakeStore(), cid, CFG_ON) == "manual"


def test_account_auto_ai_does_not_bypass_group_guard(monkeypatch):
    amo.ensure_baseline(now=NOW - 3600)
    monkeypatch.setattr(amo, "_resolve_connected_at",
                        _connected({"whatsapp:new1": NOW}))
    amo.decide_account_mode("whatsapp", "new1", "auto_ai")
    cid = "whatsapp:new1:12345@g.us"
    st = _FakeStore(groups={cid})
    assert resolve_automation_mode(st, cid, CFG_ON) == "review"
    assert maybe_bootstrap_automation_mode(st, cid, CFG_ON) == "review"
    assert st.set_calls == []   # 群绝不代写档位


def test_bootstrap_skips_persist_while_account_layer_active(monkeypatch):
    amo.ensure_baseline(now=NOW - 3600)
    monkeypatch.setattr(amo, "_resolve_connected_at",
                        _connected({"telegram:new1": NOW}))
    st = _FakeStore()
    cid = "telegram:new1:peer"
    # pending：解析为 review 且不落盘
    assert maybe_bootstrap_automation_mode(st, cid, CFG_ON) == "review"
    assert st.set_calls == []
    # 决策 auto_ai：解析跟随、仍不落盘（会话动态跟随账号档）
    amo.decide_account_mode("telegram", "new1", "auto_ai")
    assert maybe_bootstrap_automation_mode(st, cid, CFG_ON) == "auto_ai"
    assert st.set_calls == []


def test_bootstrap_unchanged_for_old_accounts(monkeypatch):
    amo.ensure_baseline(now=NOW - 3600)
    monkeypatch.setattr(amo, "_resolve_connected_at",
                        _connected({"telegram:old1": NOW - 30 * 86400}))
    # 存量 + 用户显式人设：未确认仍回落全局 auto_ai，但 #167 不落盘
    monkeypatch.setattr(amo, "persona_is_user_explicit",
                        lambda *a, **k: True)
    st = _FakeStore()
    cid = "telegram:old1:peer"
    assert maybe_bootstrap_automation_mode(st, cid, CFG_ON) == "auto_ai"
    assert st.set_calls == []


# ── 决策对齐 ──────────────────────────────────────────────────────────────

def test_align_only_touches_system_rows_and_skips_groups():
    rows = [
        {"conversation_id": "telegram:a1:c1", "automation_mode": "review",
         "source": "bootstrap"},
        {"conversation_id": "telegram:a1:c2", "automation_mode": "review",
         "source": "human"},              # 人的决定：不动
        {"conversation_id": "telegram:a1:c3", "automation_mode": "manual",
         "source": "account_mode"},       # 上次账号决策写的：跟随换档
        {"conversation_id": "telegram:a1:g1", "automation_mode": "review",
         "source": "standby"},            # 群：升 auto_ai 时跳过
        {"conversation_id": "telegram:a2:c9", "automation_mode": "review",
         "source": "bootstrap"},          # 别的账号：不动
    ]
    st = _FakeStore(rows=rows, groups={"telegram:a1:g1"})
    n = amo.align_account_conversations(st, "telegram", "a1", "auto_ai")
    assert n == 2
    changed = {c[0]: (c[1], c[2]) for c in st.set_calls}
    assert changed == {
        "telegram:a1:c1": ("auto_ai", "account_mode"),
        "telegram:a1:c3": ("auto_ai", "account_mode"),
    }


def test_align_noop_without_store_or_bad_mode():
    assert amo.align_account_conversations(None, "telegram", "a1", "auto_ai") == 0
    assert amo.align_account_conversations(
        _FakeStore(), "telegram", "a1", "bogus") == 0


# ── 待确认清单 ────────────────────────────────────────────────────────────

class _FakeRegistry:
    def __init__(self, rows):
        self._rows = rows

    def list(self):
        return list(self._rows)


def test_pending_accounts_filters_baseline_and_decided():
    amo.ensure_baseline(now=NOW - 3600)
    reg = _FakeRegistry([
        {"platform": "telegram", "account_id": "new1",
         "display_name": "新号一", "status": "online", "created_at": NOW - 60},
        {"platform": "telegram", "account_id": "old1",
         "display_name": "老号", "status": "online",
         "created_at": NOW - 86400},                      # 基线前 → 豁免
        {"platform": "line", "account_id": "new2",
         "display_name": "新号二", "status": "online", "created_at": NOW - 30},
    ])
    amo.decide_account_mode("line", "new2", "review")     # 已决策 → 不再待确认
    out = amo.pending_accounts(CFG_ON, registry=reg)
    assert [a["account_id"] for a in out] == ["new1"]
    assert out[0]["default_mode"] == "review"
    assert amo.pending_accounts(CFG_OFF, registry=reg) == []


# ── 路由契约 ──────────────────────────────────────────────────────────────

def _client(config, store=None, *, supervisor=True, monkeypatch=None):
    from src.web.routes.account_mode_routes import register_account_mode_routes
    import src.web.routes.unified_inbox_auth as auth_mod
    monkeypatch.setattr(auth_mod, "_is_supervisor",
                        lambda request: supervisor)
    app = FastAPI()

    def api_auth(request: Request):
        return True

    register_account_mode_routes(
        app, api_auth=api_auth,
        config_manager=SimpleNamespace(config=config, config_path=None))
    if store is not None:
        app.state.inbox_store = store
    return TestClient(app)


def test_route_get_disabled_and_enabled(monkeypatch):
    c = _client(CFG_OFF, monkeypatch=monkeypatch)
    body = c.get("/api/reply-settings/account-modes").json()
    assert body["ok"] and body["enabled"] is False and body["pending"] == []

    amo.ensure_baseline(now=NOW - 3600)
    monkeypatch.setattr(amo, "_resolve_connected_at",
                        _connected({"telegram:new1": NOW}))
    reg = _FakeRegistry([{
        "platform": "telegram", "account_id": "new1",
        "display_name": "新号", "status": "online", "created_at": NOW - 60}])
    monkeypatch.setattr(
        "src.integrations.account_registry.get_account_registry", lambda: reg)
    c2 = _client(CFG_ON, monkeypatch=monkeypatch)
    body2 = c2.get("/api/reply-settings/account-modes").json()
    assert body2["enabled"] and len(body2["pending"]) == 1
    assert body2["default_mode"] == "review"
    assert body2["modes"] == ["auto_ai", "review", "manual"]


def test_route_decide_happy_path_aligns_and_returns_lists(monkeypatch):
    amo.ensure_baseline(now=NOW - 3600)
    monkeypatch.setattr(amo, "_resolve_connected_at",
                        _connected({"telegram:new1": NOW}))
    reg = _FakeRegistry([{
        "platform": "telegram", "account_id": "new1",
        "display_name": "新号", "status": "online", "created_at": NOW - 60}])
    monkeypatch.setattr(
        "src.integrations.account_registry.get_account_registry", lambda: reg)
    st = _FakeStore(rows=[{
        "conversation_id": "telegram:new1:c1",
        "automation_mode": "review", "source": "bootstrap"}])
    c = _client(CFG_ON, store=st, monkeypatch=monkeypatch)
    r = c.post("/api/reply-settings/account-modes/decide", json={
        "platform": "telegram", "account_id": "new1", "mode": "auto_ai"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] and body["mode"] == "auto_ai"
    assert body["aligned_conversations"] == 1
    assert body["pending"] == []          # 决策后清单即时更新
    assert body["decided"][0]["account_id"] == "new1"
    assert amo.decided_mode("telegram", "new1") == "auto_ai"


def test_route_decide_rejects_bad_mode_disabled_and_non_supervisor(monkeypatch):
    c = _client(CFG_ON, monkeypatch=monkeypatch)
    r = c.post("/api/reply-settings/account-modes/decide", json={
        "platform": "telegram", "account_id": "a", "mode": "on"})
    assert r.status_code == 200 and r.json()["ok"] is False

    c2 = _client(CFG_OFF, monkeypatch=monkeypatch)
    r2 = c2.post("/api/reply-settings/account-modes/decide", json={
        "platform": "telegram", "account_id": "a", "mode": "review"})
    assert r2.status_code == 200 and r2.json()["ok"] is False
    assert r2.json()["errors"][0]["code"] == "disabled"

    c3 = _client(CFG_ON, supervisor=False, monkeypatch=monkeypatch)
    r3 = c3.post("/api/reply-settings/account-modes/decide", json={
        "platform": "telegram", "account_id": "a", "mode": "review"})
    assert r3.status_code == 403
