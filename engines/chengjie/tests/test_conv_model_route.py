# -*- coding: utf-8 -*-
"""会话级模型路由 / 「无限制」模式（conv_route，2026-09-12）——SLICE B 门禁。

钉住：
- 归一语义（切档灌默认旋钮 / 切回清空 / 标准档不许挂 bypass_safety）；
- KV 存取（默认路由不落键、脏值回默认、审计字段）；
- 端点事实（ai.models 显式档 > ai.fallback；上下文档按 max_ctx 封顶）；
- 守卫读点：质量层跳 / 安全层只在 bypass_safety 下跳 / 未登记层恒不跳；
- 生成作用域：context_depth 档位随会话覆盖、退出还原；
- autosend_policy.decide：无限制会话风控层让路，硬停按安全刹车语义保留；
- DraftService.resolve_with_audit：L4 强拦 / 稿龄护栏让路且审计留痕，account_offline 照拦；
- HTTP 壳：GET/POST conv-model-route、授权闸 403、clear、health/stats 形状；
- feature_gate / 会员矣阵 i18n 登记。
"""
from __future__ import annotations

import json
import time
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from src.ai import context_depth, conv_route
from src.ai.conv_route import Route


# ── 夹具 ─────────────────────────────────────────────────────────────────────

class _KV:
    """InboxStore 通用 KV 的最小替身（get/set/list_app_settings）。"""

    def __init__(self) -> None:
        self.kv: Dict[str, str] = {}
        self.writes: List[Any] = []

    def get_app_setting(self, key, default=""):
        return self.kv.get(key, default)

    def set_app_setting(self, key, value, updated_by=""):
        self.writes.append((key, value, updated_by))
        if value == "":
            self.kv.pop(key, None)
        else:
            self.kv[key] = value
        return True

    def list_app_settings(self, prefix=""):
        return [{"key": k, "value": v, "updated_by": "t", "updated_at": 0}
                for k, v in sorted(self.kv.items()) if k.startswith(prefix)]


CID = "telegram:acct1:12345"


@pytest.fixture(autouse=True)
def _reset():
    conv_route.reset_for_tests()
    yield
    conv_route.reset_for_tests()


# ── 归一 / Route ─────────────────────────────────────────────────────────────

def test_default_route_is_standard_and_noop_context():
    r = Route.standard()
    assert r.is_default and not r.unrestricted
    ctx = {"_route": "stale", "_unrestricted": True, "x": 1}
    r.apply_context(ctx)
    assert ctx == {"x": 1}          # 只清键，不加任何东西


def test_switch_to_unrestricted_fills_defaults_and_back_clears():
    r = conv_route.normalize({"profile": "unrestricted"})
    assert r.unrestricted
    assert (r.depth, r.effort, r.thinking) == ("max", "high", False)
    r2 = conv_route.normalize({"profile": "standard"}, base=r)
    assert r2.is_default


def test_standard_profile_cannot_carry_bypass_safety():
    r = conv_route.normalize({"profile": "standard", "bypass_safety": True})
    assert r.bypass_safety is False
    r = conv_route.normalize({"profile": "unrestricted", "bypass_safety": "1"})
    assert r.bypass_safety is True


def test_invalid_values_fall_back():
    base = conv_route.normalize({"profile": "unrestricted", "depth": "deep"})
    r = conv_route.normalize({"profile": "nope", "depth": "huge", "effort": "ultra"}, base=base)
    assert r.profile == "unrestricted" and r.depth == "deep" and r.effort == "high"


def test_apply_context_sets_strict_route_keys():
    ctx: Dict[str, Any] = {}
    conv_route.normalize({"profile": "unrestricted", "thinking": True}).apply_context(ctx)
    assert ctx["_route"] == "profile:unrestricted"
    assert ctx["_route_strict"] is True and ctx["_unrestricted"] is True
    assert ctx["_thinking"] is True
    assert "_unrestricted_bypass_safety" not in ctx
    assert ctx["_conv_route"]["profile"] == "unrestricted"


def test_strategy_overrides_only_when_effort_set():
    assert Route.standard().strategy_overrides() == {}
    so = conv_route.normalize({"profile": "unrestricted", "effort": "low"}).strategy_overrides()
    assert so == {"max_tokens": 512, "temperature": 0.6}


# ── KV 存取 ───────────────────────────────────────────────────────────────────

def test_set_get_clear_roundtrip_and_default_deletes_key():
    st = _KV()
    assert conv_route.get(st, CID).is_default
    r = conv_route.set(st, CID, {"profile": "unrestricted"}, by="alice", now=1000.0)
    assert r is not None and r.unrestricted and r.updated_by == "alice" and r.updated_at == 1000.0
    assert conv_route.KEY_PREFIX + CID in st.kv
    got = conv_route.get(st, CID)
    assert got.unrestricted and got.depth == "max"
    # 切回标准 → 键删除
    conv_route.set(st, CID, {"profile": "standard"})
    assert conv_route.KEY_PREFIX + CID not in st.kv
    conv_route.set(st, CID, {"profile": "unrestricted"})
    assert conv_route.clear(st, CID)
    assert conv_route.get(st, CID).is_default


def test_dirty_kv_value_falls_back_to_standard():
    st = _KV()
    st.kv[conv_route.KEY_PREFIX + CID] = "{not json"
    assert conv_route.get(st, CID).is_default
    st.kv[conv_route.KEY_PREFIX + CID] = json.dumps({"depth": "max"})   # 无 profile
    assert conv_route.get(st, CID).is_default


def test_all_routes_lists_only_non_default():
    st = _KV()
    conv_route.set(st, CID, {"profile": "unrestricted"})
    conv_route.set(st, "line:a:2", {"profile": "unrestricted", "bypass_safety": True})
    routes = conv_route.all_routes(st)
    assert set(routes) == {CID, "line:a:2"}
    snap = conv_route.stats_snapshot(st)
    assert snap["unrestricted_convs"] == 2 and snap["bypass_safety_convs"] == 1


def test_store_missing_never_raises():
    assert conv_route.get(None, CID).is_default
    assert conv_route.set(None, CID, {"profile": "unrestricted"}) is None
    assert conv_route.is_unrestricted(object(), CID) is False


# ── 端点事实 / 深度封顶 ───────────────────────────────────────────────────────

def test_endpoint_spec_prefers_models_profile_then_fallback():
    cfg = {"ai": {"fallback": {"enabled": True, "base_url": "http://192.168.0.173:8001/v1",
                               "model": "chatx", "api_key": "vllm", "num_ctx": 8192}}}
    spec = conv_route.endpoint_spec(cfg)
    assert spec["source"] == "ai.fallback" and spec["model"] == "chatx"
    assert spec["host"] == "192.168.0.173:8001" and spec["max_ctx"] == 8192
    assert "api_key" not in spec
    cfg["ai"]["models"] = {"unrestricted": {"base_url": "http://10.0.0.9:8010/v1/",
                                            "model": "coder", "max_ctx": 65536}}
    spec = conv_route.endpoint_spec(cfg)
    assert spec["source"] == "ai.models.unrestricted" and spec["base_url"].endswith("8010/v1")
    assert conv_route.endpoint_spec({"ai": {}}) == {}


def test_depth_cap_and_allowed_depths():
    assert conv_route.depth_cap(8192) == "standard"
    assert conv_route.depth_cap(24_576) == "standard"   # 24k 吃不下 32k deep
    assert conv_route.depth_cap(65536) == "max"
    assert conv_route.depth_cap(300_000) == "ultra"
    lan = {"ai": {"models": {"unrestricted": {"base_url": "http://x/v1", "model": "m",
                                              "max_ctx": 24576}}}}
    assert conv_route.allowed_depths(lan, unrestricted=True) == ["", "standard", "max"]
    assert conv_route.allowed_depths(lan, unrestricted=False) == [
        "", "standard", "deep", "max", "ultra"]
    wide = {"ai": {"models": {"unrestricted": {"base_url": "http://x/v1", "model": "m",
                                               "max_ctx": 65536}}}}
    assert conv_route.allowed_depths(wide, unrestricted=True) == [
        "", "standard", "deep", "max"]
    r = conv_route.normalize({"profile": "unrestricted", "depth": "ultra"})
    assert r.depth == "ultra"
    assert conv_route.effective_depth(r, lan) == "max"   # 超窗 → 满窗键
    assert conv_route.effective_depth(Route.standard(), lan) == ""


# ── 守卫读点 ─────────────────────────────────────────────────────────────────

def test_skip_guard_quality_vs_safety_vs_unknown():
    assert conv_route.skip_guard({}, "persona_guard") is False
    ctx = {"_unrestricted": True}
    assert conv_route.skip_guard(ctx, "persona_guard") is True
    assert conv_route.skip_guard(ctx, "risk_level") is True
    assert conv_route.skip_guard(ctx, "kill_switch") is False        # 安全层：没钥匙不跳
    assert conv_route.skip_guard(ctx, "crisis_safety_net") is False
    assert conv_route.skip_guard(ctx, "some_new_guard") is False     # 未登记 → 照拦
    ctx["_unrestricted_bypass_safety"] = True
    assert conv_route.skip_guard(ctx, "kill_switch") is True
    snap = conv_route.stats_snapshot()
    assert snap["skipped_total"] == 3 and snap["safety_bypassed"] == 1
    assert snap["by_layer"]["persona_guard"] == 1


def test_skip_for_conv_uses_store_and_global_key():
    st = _KV()
    assert conv_route.skip_for_conv(st, CID, "persona_guard") is False
    conv_route.set(st, CID, {"profile": "unrestricted"})
    assert conv_route.skip_for_conv(st, CID, "persona_guard") is True
    assert conv_route.skip_for_conv(st, CID, "hard_stop") is False
    assert conv_route.skip_for_conv(
        st, CID, "hard_stop", {"ai": {"unrestricted": {"bypass_safety_brakes": True}}}) is True
    conv_route.set(st, CID, {"bypass_safety": True})
    assert conv_route.skip_for_conv(st, CID, "hard_stop") is True


def test_attach_folds_global_bypass_into_context():
    st = _KV()
    conv_route.set(st, CID, {"profile": "unrestricted"})
    ctx: Dict[str, Any] = {}
    r = conv_route.attach(ctx, CID, store=st,
                          config={"ai": {"unrestricted": {"bypass_safety_brakes": True}}})
    assert r.bypass_safety is True and ctx["_unrestricted_bypass_safety"] is True
    ctx2: Dict[str, Any] = {"_unrestricted": True}
    conv_route.attach(ctx2, "", store=st)
    assert "_unrestricted" not in ctx2          # 无会话 → 标准档只清键


# ── 生成作用域 × context_depth ───────────────────────────────────────────────
# context_depth.override_scope / vendor_params.thinking_extra_body 是 picker 线的在途改动
# （SLICE B 先入库）：HEAD 上缺席时 SKIP 而非红——红只会让别的线误判自己。
_NEEDS_OVERRIDE_SCOPE = pytest.mark.skipif(
    not hasattr(context_depth, "override_scope"),
    reason="context_depth.override_scope 由 picker 线提交（generation_scope 依赖）")


def _has_thinking_extra_body() -> bool:
    try:
        from src.ai import vendor_params
        return hasattr(vendor_params, "thinking_extra_body")
    except Exception:
        return False


@_NEEDS_OVERRIDE_SCOPE
def test_generation_scope_overrides_depth_and_restores():
    assert context_depth.resolve({}).key == "standard"
    wide = {"ai": {"fallback": {"enabled": True, "base_url": "http://x/v1",
                                "model": "m", "num_ctx": 65536}}}
    r = conv_route.normalize({"profile": "unrestricted", "depth": "deep"})
    assert r.depth == "deep"
    assert conv_route.active_unrestricted() is False
    with conv_route.generation_scope(r, wide):
        assert conv_route.active_unrestricted() is True
        assert context_depth.resolve({"ai": {"context_depth": "standard"}}).key == "deep"
        assert context_depth.describe({})["override"] == "deep"
    assert conv_route.active_unrestricted() is False
    assert context_depth.resolve({}).key == "standard"
    assert context_depth.describe({})["override"] == ""


def test_generation_scope_standard_route_is_noop():
    with conv_route.generation_scope(Route.standard(), {}):
        assert conv_route.active_route() is None
        assert context_depth.resolve({}).key == "standard"


@_NEEDS_OVERRIDE_SCOPE
def test_unrestricted_scope_bypasses_economy_cap():
    cfg = {"ai": {"usage_mode": "economy"}}
    r = conv_route.normalize({"profile": "unrestricted", "depth": "max"})
    with conv_route.generation_scope(r, cfg):
        budget_unr = context_depth.prompt_budget(cfg, 12000)
    with context_depth.override_scope("max"):
        budget_eco = context_depth.prompt_budget(cfg, 12000)
    assert budget_unr >= budget_eco
    assert budget_unr >= 128_000


def test_endpoint_prompt_cap_reserves_completion():
    assert conv_route.endpoint_prompt_cap({}) == 0
    cfg = {"ai": {"fallback": {"enabled": True, "base_url": "http://x/v1",
                               "model": "chatx", "num_ctx": 24576}}}
    assert conv_route.endpoint_prompt_cap(cfg, max_tokens=2048) == 24576 - 2048 - 128
    assert conv_route.endpoint_prompt_cap(cfg, max_tokens=512) == 24576 - 512 - 128


@_NEEDS_OVERRIDE_SCOPE
def test_unrestricted_prompt_budget_clamps_to_endpoint():
    cfg = {"ai": {"fallback": {"enabled": True, "base_url": "http://192.168.0.173:8001/v1",
                               "model": "chatx", "num_ctx": 24576}}}
    r = conv_route.normalize({"profile": "unrestricted", "depth": "max", "effort": "high"})
    cap = conv_route.endpoint_prompt_cap(cfg, max_tokens=2048)
    assert 20_000 <= cap < 24_576
    with conv_route.generation_scope(r, cfg):
        assert context_depth.prompt_budget(cfg, 80_000) == cap
        assert context_depth.prompt_budget(cfg, 0) == cap          # 不裁也封顶
        assert context_depth.prompt_budget(cfg, 12_000) == cap     # max 抬到 128k 后再封
    assert context_depth.prompt_budget(cfg, 80_000) == 80_000     # 作用域外标准档不碰


# ── 思考开关 extra_body ──────────────────────────────────────────────────────

@pytest.mark.skipif(not _has_thinking_extra_body(),
                    reason="vendor_params.thinking_extra_body 由 picker 线提交")
def test_thinking_extra_body_per_vendor():
    from src.ai.vendor_params import thinking_extra_body, thinking_off_extra_body
    on = thinking_extra_body("http://192.168.0.173:8001/v1", "chatx", enabled=True)
    assert on == {"chat_template_kwargs": {"enable_thinking": True}}
    off = thinking_extra_body("http://192.168.0.173:8001/v1", "chatx", enabled=False)
    assert off == thinking_off_extra_body("http://192.168.0.173:8001/v1", "chatx", reasoning=False)
    assert thinking_extra_body("https://api.deepseek.com", "deepseek-chat", enabled=True) == {
        "thinking": {"type": "enabled"}}
    assert thinking_extra_body("https://example.com/v1", "gpt-x", enabled=True) == {}


# ── autosend_policy.decide ───────────────────────────────────────────────────

def _decide(store, **kw):
    from src.inbox.autosend_policy import decide
    base = dict(peer_risk="high", peer_reasons=["keyword"], automation_mode="auto_ai",
                conversation_id=CID, store=store, policy_mode="shadow")
    base.update(kw)
    return decide(**base)


def test_policy_unrestricted_auto_ai_high_risk_goes_l2_without_shadow():
    st = _KV()
    d0 = _decide(st)
    assert d0.level == "L1" and d0.hold_reason == "risk_high_review"     # 未开＝旧行为
    conv_route.set(st, CID, {"profile": "unrestricted"})
    d = _decide(st)
    assert d.level == "L2" and d.hold_reason == "" and d.shadow is None
    assert d.review_required is False and not d.hard_stop
    # enforce 档同样让路；risk_hold 也不参与
    assert _decide(st, policy_mode="enforce").level == "L2"
    assert _decide(st, risk_hold_reason="commitment").level == "L2"


def test_policy_unrestricted_keeps_agent_chosen_mode():
    st = _KV()
    conv_route.set(st, CID, {"profile": "unrestricted"})
    d = _decide(st, automation_mode="review")
    assert d.level == "L1" and d.hold_reason == "mode:review" and d.shadow is None


def test_policy_unrestricted_hard_stop_is_safety_brake():
    st = _KV()
    conv_route.set(st, CID, {"profile": "unrestricted"})
    d = _decide(st, peer_reasons=["stop_contact"])
    assert d.hard_stop == "stop_contact" and d.farewell is True         # 刹车保留：告别一条
    conv_route.set(st, CID, {"bypass_safety": True})
    d2 = _decide(st, peer_reasons=["stop_contact"])
    assert d2.level == "L2" and not d2.hard_stop


def test_policy_without_store_or_conv_is_untouched():
    d = _decide(None)
    assert d.level == "L1" and d.hold_reason == "risk_high_review"
    st = _KV()
    conv_route.set(st, CID, {"profile": "unrestricted"})
    d = _decide(st, conversation_id="")
    assert d.level == "L1"


# ── DraftService.resolve_with_audit ──────────────────────────────────────────

class _DraftStore(_KV):
    def __init__(self, row: Dict[str, Any]) -> None:
        super().__init__()
        self.row = row
        self.upserts: List[Dict[str, Any]] = []

    def get_draft(self, draft_id):
        return dict(self.row) if draft_id == self.row["draft_id"] else None

    def get_conversation(self, cid):
        return {"display_name": "x"}

    def list_recent_messages(self, conversation_id, *, limit=50, before_ts=None):
        return []

    def upsert_draft(self, row):
        self.upserts.append(dict(row))


def _draft_svc(row_over: Dict[str, Any] | None = None):
    from src.inbox.drafts import DraftService
    row = {"draft_id": "inbox:7", "source_kind": "inbox", "platform": "telegram",
           "conversation_id": CID, "status": "pending", "autopilot_level": "L4",
           "risk_level": "high", "peer_text": "hi", "draft_text": "hello",
           "created_ts": time.time() - 100 * 3600.0}
    row.update(row_over or {})
    st = _DraftStore(row)
    svc = DraftService(inbox_store=st)

    async def _cb(_row):
        return {"ok": True}
    svc.set_inbox_deliver_callback(_cb, stale_approve_hours=24.0)
    audits: List[Dict[str, Any]] = []
    svc._write_audit = lambda did, lvl, action, by, **kw: audits.append(
        {"level": lvl, "action": action, **kw})
    svc.resolve = lambda did, action, **kw: {"ok": True, "resolved": action}
    return svc, st, audits


def test_resolve_with_audit_l4_block_and_stale_guard_kept_by_default():
    svc, st, audits = _draft_svc()
    out = svc.resolve_with_audit("inbox:7", "approve", by="bob", deliver=False)
    # 稿龄 100h > 24h → 先被稿龄护栏拦（更早的 409）
    assert out["ok"] is False and out.get("too_stale") is True
    svc2, st2, audits2 = _draft_svc({"created_ts": time.time() - 60})
    out2 = svc2.resolve_with_audit("inbox:7", "approve", by="bob", deliver=False)
    assert out2["ok"] is False and out2.get("blocked") is True and out2["code"] == 422
    assert audits2[-1]["action"] == "blocked"


def test_resolve_with_audit_unrestricted_skips_l4_and_stale_with_audit_trail():
    svc, st, audits = _draft_svc()
    conv_route.set(st, CID, {"profile": "unrestricted"})
    out = svc.resolve_with_audit("inbox:7", "approve", by="bob", deliver=False)
    assert out.get("ok") is True and out.get("resolved") == "approve"
    fo = [a for a in audits if a["action"] == "force_override"]
    assert fo and "unrestricted" in fo[0]["reason"]
    assert all(a["action"] != "blocked" for a in audits)


def test_resolve_with_audit_unrestricted_still_blocks_account_offline(monkeypatch):
    svc, st, audits = _draft_svc({"created_ts": time.time() - 60, "autopilot_level": "L2",
                                  "risk_level": "low"})
    conv_route.set(st, CID, {"profile": "unrestricted"})
    monkeypatch.setattr(type(svc), "_account_offline_block", staticmethod(lambda d: True))
    out = svc.resolve_with_audit("inbox:7", "approve", by="bob", deliver=False)
    assert out["ok"] is False and out.get("account_offline") is True


# ── HTTP 壳 ──────────────────────────────────────────────────────────────────

def _app(cfg: Dict[str, Any] | None = None):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from src.web.routes.conv_model_route_routes import register_conv_model_route_routes

    app = FastAPI()
    st = _KV()
    app.state.inbox_store = st

    def _auth():
        return True

    register_conv_model_route_routes(
        app, api_auth=_auth, config_manager=SimpleNamespace(config=cfg or {}))
    return TestClient(app), st


_Q = {"platform": "telegram", "account_id": "acct1", "chat_key": "12345"}


def test_http_get_default_and_post_unrestricted_roundtrip():
    cli, st = _app({"ai": {"fallback": {"enabled": True, "base_url": "http://h:8001/v1",
                                        "model": "chatx", "num_ctx": 65536}}})
    r = cli.get("/api/unified-inbox/conv-model-route", params=_Q).json()
    assert r["ok"] and r["conversation_id"] == CID
    assert r["route"]["profile"] == "standard" and r["allowed"] is True and r["enabled"] is True
    assert r["endpoint"]["model"] == "chatx" and "api_key" not in r["endpoint"]
    assert r["choices"]["depths"][-1] == "ultra"          # 云端标准档四档全放

    r = cli.post("/api/unified-inbox/conv-model-route",
                 json={**_Q, "profile": "unrestricted"}).json()
    assert r["ok"] and r["route"]["unrestricted"] is True and r["route"]["depth"] == "max"
    assert r["choices"]["depths"] == ["", "standard", "deep", "max"]   # 64k 端点：深度进得去，满窗用 max
    assert conv_route.get(st, CID).unrestricted

    r = cli.post("/api/unified-inbox/conv-model-route",
                 json={**_Q, "depth": "deep", "thinking": True, "effort": "low"}).json()
    assert (r["route"]["depth"], r["route"]["thinking"], r["route"]["effort"]) == ("deep", True, "low")
    assert r["effective_depth"] == "deep"

    r = cli.post("/api/unified-inbox/conv-model-route", json={**_Q, "clear": True}).json()
    assert r["ok"] and r["cleared"] is True and r["route"]["profile"] == "standard"
    assert conv_route.get(st, CID).is_default


def test_http_bad_conversation_and_noop_patch():
    cli, _ = _app()
    assert cli.get("/api/unified-inbox/conv-model-route",
                   params={"platform": "telegram"}).json()["error"] == "bad_conversation"
    assert cli.post("/api/unified-inbox/conv-model-route",
                    json={"platform": "telegram"}).json()["error"] == "bad_conversation"
    r = cli.post("/api/unified-inbox/conv-model-route", json={**_Q, "junk": 1}).json()
    assert r["ok"] and r["noop"] is True


def test_http_feature_gate_locks_unrestricted_but_not_standard_knobs():
    cfg = {"licensing": {"feature_gate": {"enabled": True, "plan_override": "pro"}}}
    cli, st = _app(cfg)
    g = cli.get("/api/unified-inbox/conv-model-route", params=_Q).json()
    assert g["allowed"] is False
    r = cli.post("/api/unified-inbox/conv-model-route", json={**_Q, "profile": "unrestricted"})
    assert r.status_code == 403 and r.headers.get("X-Deny-Reason") == "feature_locked"
    assert conv_route.get(st, CID).is_default
    # 标准档下调深度不受闸门影响
    r = cli.post("/api/unified-inbox/conv-model-route", json={**_Q, "depth": "deep"}).json()
    assert r["ok"] and r["route"]["depth"] == "deep" and r["route"]["profile"] == "standard"
    # 旗舰放行
    cli2, _ = _app({"licensing": {"feature_gate": {"enabled": True, "plan_override": "flagship"}}})
    assert cli2.post("/api/unified-inbox/conv-model-route",
                     json={**_Q, "profile": "unrestricted"}).status_code == 200


def test_http_disabled_switch_blocks_unrestricted():
    cli, _ = _app({"ai": {"unrestricted": {"enabled": False}}})
    r = cli.post("/api/unified-inbox/conv-model-route", json={**_Q, "profile": "unrestricted"})
    assert r.status_code == 403 and r.headers.get("X-Deny-Reason") == "disabled"


def test_http_health_and_stats_shapes():
    cli, st = _app({"ai": {}})
    h = cli.get("/api/ai/model-route/health").json()
    assert h["ok"] and h["health"]["configured"] is False and h["health"]["error"] == "no_endpoint"
    conv_route.set(st, CID, {"profile": "unrestricted"})
    s = cli.get("/api/ai/model-route/stats").json()
    assert s["ok"] and s["stats"]["unrestricted_convs"] == 1
    assert {"skipped_total", "by_layer", "offline_holds", "global_bypass_safety"} <= set(s["stats"])


# ── 授权登记 / 会员矩阵 i18n ─────────────────────────────────────────────────

def test_feature_gate_registration_and_membership_labels():
    from src.licensing.feature_gate import FEATURE_MIN_PLAN, feature_enabled
    from src.web.i18n_packs import membership
    assert FEATURE_MIN_PLAN["unrestricted_model"] == "flagship"
    assert feature_enabled("unrestricted_model", {}) is True                 # 闸关＝放行
    assert feature_enabled("unrestricted_model",
                           {"licensing": {"feature_gate": {"enabled": True,
                                                           "plan_override": "pro"}}}) is False
    assert membership.ZH["mb_feat_unrestricted_model"] and membership.EN["mb_feat_unrestricted_model"]


# ── 「模型 / 模式」拆面板（2026-09-12）：Route.model ＝标准模式下「用谁答」 ─────────

_CFG_VENDORS = {"ai": {
    "base_url": "https://api.deepseek.com/v1", "model": "deepseek-flash", "api_key": "sk-main",
    "models": {
        "gpt": {"base_url": "https://api.openai.com/v1", "model": "gpt-x", "api_key": "sk-oa"},
        "gem": {"base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
                "model": "gemini-x", "max_ctx": 200000},
        "_lan_tool": {"base_url": "http://h:8001/v1", "model": "chatx"},
    },
    "fallback": {"enabled": True, "base_url": "http://192.168.0.173:8001/v1", "model": "chatx",
                 "num_ctx": 24576},
}}


def test_model_field_normalize_and_context_non_strict():
    r = conv_route.normalize({"model": "gpt"})
    assert r.model == "gpt" and not r.is_default and r.route_key == "profile:gpt"
    ctx: Dict[str, Any] = {}
    r.apply_context(ctx)
    assert ctx["_route"] == "profile:gpt"
    assert "_route_strict" not in ctx and "_unrestricted" not in ctx   # 云厂商档不严格、规则全开
    # 非法名回落；空串＝跟随主链＝默认路由（不落键）
    assert conv_route.normalize({"model": "bad name!"}).model == ""
    assert conv_route.normalize({"model": ""}, base=r).is_default


def test_model_and_unrestricted_are_mutually_exclusive():
    unr = conv_route.normalize({"profile": "unrestricted"})
    # 无限制会话里点选云厂商（未显式给 profile）→ 视为改回标准模式，旋钮清空
    r = conv_route.normalize({"model": "gpt"}, base=unr)
    assert r.profile == "standard" and r.model == "gpt" and r.depth == "" and r.thinking is False
    # 反向：带着模型切无限制 → 模型字段清空（端点由模式决定）
    r2 = conv_route.normalize({"profile": "unrestricted"}, base=r)
    assert r2.unrestricted and r2.model == ""
    assert r2.route_key == "profile:unrestricted"


def test_model_catalog_main_first_vendor_labels_and_lan_row():
    cat = conv_route.model_catalog(_CFG_VENDORS)
    names = [row["name"] for row in cat]
    assert names[0] == ""                                   # 主链永远第一
    assert "gpt" in names and "gem" in names and "_lan_tool" not in names   # 内部档不列
    assert "unrestricted" in names                          # ai.models 没配 → 绑 ai.fallback 的 LAN 行
    by = {row["name"]: row for row in cat}
    assert by[""]["vendor"] == "deepseek" and by[""]["label"] == "DeepSeek"
    assert by["gpt"]["label"] == "ChatGPT" and by["gpt"]["private"] is False
    assert by["gem"]["max_ctx"] == 200000                   # 显式 max_ctx 优先于厂商缺省
    assert by["unrestricted"]["private"] is True and by["unrestricted"]["source"] == "ai.fallback"
    for row in cat:
        assert "api_key" not in row
    # allowed_depths 按档窗口筛：gpt 缺省 128k → 不放 ultra；主链不筛（四档全放）
    assert conv_route.allowed_depths(_CFG_VENDORS, model="gpt") == ["", "standard", "deep", "max"]
    assert conv_route.allowed_depths(_CFG_VENDORS) == list(conv_route.DEPTH_CHOICES)
    assert conv_route.allowed_depths(_CFG_VENDORS, model="unrestricted") == ["", "standard"]
    # active_endpoint 跟着路由走
    assert conv_route.active_endpoint(Route.standard(), _CFG_VENDORS)["model"] == "deepseek-flash"
    assert conv_route.active_endpoint(conv_route.normalize({"model": "gpt"}), _CFG_VENDORS)["model"] == "gpt-x"
    assert conv_route.active_endpoint(conv_route.normalize({"profile": "unrestricted"}),
                                      _CFG_VENDORS)["host"] == "192.168.0.173:8001"


def test_http_model_roundtrip_unknown_400_and_catalog_shape():
    cli, st = _app(_CFG_VENDORS)
    g = cli.get("/api/unified-inbox/conv-model-route", params=_Q).json()
    assert [m["name"] for m in g["choices"]["models"]][0] == ""
    assert g["active_endpoint"]["model"] == "deepseek-flash" and g["model_missing"] is False
    r = cli.post("/api/unified-inbox/conv-model-route", json={**_Q, "model": "gpt"}).json()
    assert r["ok"] and r["route"]["model"] == "gpt" and r["route"]["profile"] == "standard"
    assert r["active_endpoint"]["host"] == "api.openai.com"
    assert r["choices"]["depths"] == ["", "standard", "deep", "max"]
    assert conv_route.get(st, CID).model == "gpt"
    bad = cli.post("/api/unified-inbox/conv-model-route", json={**_Q, "model": "nope"})
    assert bad.status_code == 400 and bad.headers.get("X-Deny-Reason") == "unknown_model"
    assert conv_route.get(st, CID).model == "gpt"           # 400 不改库
    # 切无限制 → model 清空；再点云厂商 → 回标准
    r = cli.post("/api/unified-inbox/conv-model-route", json={**_Q, "profile": "unrestricted"}).json()
    assert r["route"]["unrestricted"] and r["route"]["model"] == ""
    r = cli.post("/api/unified-inbox/conv-model-route", json={**_Q, "model": "gem"}).json()
    assert r["route"]["profile"] == "standard" and r["route"]["model"] == "gem"
    # 目录里删掉该档 → model_missing 如实报
    cfg2 = json.loads(json.dumps(_CFG_VENDORS))
    cfg2["ai"]["models"].pop("gem")
    cli2, _ = _app(cfg2)
    cli2.app.state.inbox_store = st
    g2 = cli2.get("/api/unified-inbox/conv-model-route", params=_Q).json()
    assert g2["model_missing"] is True


def test_http_health_per_profile_and_all(monkeypatch):
    async def _fake_probe(config, *, force=False, timeout=6.0, profile=None):
        return {"configured": True, "online": profile != "gpt", "profile": profile,
                "error": "" if profile != "gpt" else "http_401"}
    monkeypatch.setattr(conv_route, "probe_endpoint", _fake_probe)
    cli, _ = _app(_CFG_VENDORS)
    h = cli.get("/api/ai/model-route/health", params={"profile": "gpt"}).json()
    assert h["health"]["online"] is False and h["health"]["error"] == "http_401"
    h = cli.get("/api/ai/model-route/health", params={"all": 1}).json()
    assert set(h["health_all"]) == {"", "gpt", "gem", "unrestricted"}
    assert h["health_all"][""]["online"] is True and h["health_all"]["gpt"]["online"] is False
    assert h["health"]["profile"] is None                    # 不带 profile＝历史语义（无限制端点）


class _FakeHttpx:
    """httpx.AsyncClient 假件：云厂商走 GET /models（零 token），私网 / 不支持列表走 POST ping。"""
    get_sc = 200
    get_ids: list = ["gpt-x", "deepseek-flash"]
    post_sc = 200
    calls: list = []

    class _Resp:
        def __init__(self, sc, ids=None):
            self.status_code = sc
            self._ids = ids

        def json(self):
            return {"data": [{"id": i} for i in (self._ids or [])]}

    def __init__(self, *a, **k): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False

    async def get(self, url, headers=None):
        _FakeHttpx.calls.append(("GET", url, None, headers))
        return self._Resp(_FakeHttpx.get_sc, _FakeHttpx.get_ids)

    async def post(self, url, json=None, headers=None):
        _FakeHttpx.calls.append(("POST", url, json, headers))
        return self._Resp(_FakeHttpx.post_sc)


def test_probe_endpoint_cloud_uses_models_list_private_uses_ping(monkeypatch):
    import asyncio
    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", _FakeHttpx)
    _FakeHttpx.calls = []
    # 云厂商密钥错 → GET /models 401 → 离线；不发 POST（零 token）
    _FakeHttpx.get_sc = 401
    h = asyncio.run(conv_route.probe_endpoint(_CFG_VENDORS, force=True, profile="gpt"))
    assert h["online"] is False and h["error"] == "http_401" and h["probe"] == "models"
    assert [c[0] for c in _FakeHttpx.calls] == ["GET"]
    assert _FakeHttpx.calls[-1][1].endswith("/models") and _FakeHttpx.calls[-1][3]["Authorization"] == "Bearer sk-oa"
    # 云厂商在线且模型名在列表里
    _FakeHttpx.calls = []
    _FakeHttpx.get_sc = 200
    h = asyncio.run(conv_route.probe_endpoint(_CFG_VENDORS, force=True, profile=""))
    assert h["online"] is True and h["probe"] == "models" and "warn" not in h
    assert _FakeHttpx.calls[-1][3]["Authorization"] == "Bearer sk-main"
    # 列表里没有这个模型名：仍在线但带 warn（chat 时大概率 404）
    _FakeHttpx.get_ids = ["models/gemini-other"]
    h = asyncio.run(conv_route.probe_endpoint(_CFG_VENDORS, force=True, profile="gem"))
    assert h["online"] is True and h["warn"] == "model_not_listed"
    _FakeHttpx.get_ids = ["models/gemini-x"]          # Gemini 兼容层的 models/ 前缀也能对上
    h = asyncio.run(conv_route.probe_endpoint(_CFG_VENDORS, force=True, profile="gem"))
    assert h["online"] is True and "warn" not in h
    # 厂商不支持 /models（404）→ 回落 1-token ping，且按厂商送关思考字段
    _FakeHttpx.calls = []
    _FakeHttpx.get_sc = 404
    _FakeHttpx.post_sc = 200
    h = asyncio.run(conv_route.probe_endpoint(_CFG_VENDORS, force=True, profile=""))
    assert h["online"] is True and h["probe"] == "chat"
    assert [c[0] for c in _FakeHttpx.calls] == ["GET", "POST"]
    assert _FakeHttpx.calls[-1][2].get("thinking") == {"type": "disabled"}   # DeepSeek 官方关思考字段
    assert "chat_template_kwargs" not in _FakeHttpx.calls[-1][2]           # 云厂商不送 vLLM 私有字段
    # 私网端点（历史语义＝无限制端点）：直接 POST ping，不碰 /models
    _FakeHttpx.calls = []
    _FakeHttpx.get_sc = 200
    h = asyncio.run(conv_route.probe_endpoint(_CFG_VENDORS, force=True))
    assert h["online"] is True and h["probe"] == "chat"
    assert [c[0] for c in _FakeHttpx.calls] == ["POST"]
    assert _FakeHttpx.calls[-1][2].get("chat_template_kwargs") == {"enable_thinking": False}
    # ping 404（模型名不存在）→ 离线
    _FakeHttpx.post_sc = 404
    h = asyncio.run(conv_route.probe_endpoint(_CFG_VENDORS, force=True))
    assert h["online"] is False and h["error"] == "http_404"


# ── 授权闸 multi_vendor_model + 按模型档分桶统计（2026-09-12 P3）─────────────

def test_vendor_gate_registration_and_labels():
    from src.licensing.feature_gate import FEATURE_MIN_PLAN, feature_enabled
    from src.web.i18n_packs import membership
    assert FEATURE_MIN_PLAN["multi_vendor_model"] == "pro"
    assert conv_route.vendor_allowed({}) is True                            # 闸关＝放行
    assert feature_enabled("multi_vendor_model",
                           {"licensing": {"feature_gate": {"enabled": True,
                                                           "plan_override": "basic"}}}) is False
    assert membership.ZH["mb_feat_multi_vendor_model"] and membership.EN["mb_feat_multi_vendor_model"]


def test_http_vendor_gate_locks_named_model_but_not_main_chain():
    cfg = {**_CFG_VENDORS, "licensing": {"feature_gate": {"enabled": True, "plan_override": "basic"}}}
    cli, st = _app(cfg)
    g = cli.get("/api/unified-inbox/conv-model-route", params=_Q).json()
    assert g["vendor_allowed"] is False and g["allowed"] is False
    r = cli.post("/api/unified-inbox/conv-model-route", json={**_Q, "model": "gpt"})
    assert r.status_code == 403 and r.headers.get("X-Deny-Reason") == "vendor_locked"
    assert conv_route.get(st, CID).is_default
    # 主链 "" 恒可选（回到主链不是「点名厂商」）
    r = cli.post("/api/unified-inbox/conv-model-route", json={**_Q, "model": ""}).json()
    assert r["ok"] and r["route"]["model"] == ""
    # pro 放行
    cli2, _ = _app({**_CFG_VENDORS, "licensing": {"feature_gate": {"enabled": True, "plan_override": "pro"}}})
    r2 = cli2.post("/api/unified-inbox/conv-model-route", json={**_Q, "model": "gpt"}).json()
    assert r2["ok"] and r2["route"]["model"] == "gpt" and r2["vendor_allowed"] is True


def test_stats_by_model_buckets_and_model_convs():
    st = _KV()
    conv_route.record_reply(None, ok=True, latency_ms=100)                       # 主链
    conv_route.record_reply({"profile": "standard", "model": "gpt"}, ok=True, latency_ms=300)
    conv_route.record_reply({"profile": "standard", "model": "gpt"}, ok=False, latency_ms=0)
    conv_route.record_reply({"profile": "unrestricted", "unrestricted": True}, ok=True, latency_ms=900)
    conv_route.set(st, CID, {"model": "gpt"})
    conv_route.set(st, "telegram:acct1:other", {"model": "gpt"})
    conv_route.set(st, "telegram:acct1:third", {"profile": "unrestricted"})
    s = conv_route.stats_snapshot(st)
    bm = s["by_model"]
    assert bm["main"]["calls"] == 1 and bm["main"]["avg_latency_ms"] == 100
    assert bm["gpt"] == {"calls": 2, "ok": 1, "fail": 1, "latency_ms_total": 300, "avg_latency_ms": 150}
    assert bm["unrestricted"]["calls"] == 1
    assert s["model_convs"] == {"gpt": 2}                       # 无限制会话不算「点名厂商」
    conv_route.reset_for_tests()
    assert conv_route.stats_snapshot(st)["by_model"] == {}


def test_prompt_trace_record_feeds_by_model_and_persists_summary(tmp_path, monkeypatch):
    from src.ai import prompt_trace
    prompt_trace.reset()
    monkeypatch.setenv("AITR_DATA_DIR", str(tmp_path))
    prompt_trace.record(messages=[{"role": "system", "content": "SECRET PERSONA"},
                                  {"role": "user", "content": "客户原话"}],
                        model="gpt-x", host="api.openai.com", conv="telegram:12345",
                        latency_ms=420, ok=True, route={"profile": "standard", "model": "gpt"})
    assert conv_route.stats_snapshot(None)["by_model"]["gpt"]["calls"] == 1
    assert prompt_trace.flush_summaries() is True
    p = prompt_trace.summary_path()
    raw = p.read_text(encoding="utf-8")
    assert "SECRET PERSONA" not in raw and "客户原话" not in raw          # 正文绝不落盘
    assert '"model": "gpt-x"' in raw and '"host": "api.openai.com"' in raw
    # 模拟重启：清环 → 装回 → 「上一条回复」仍能回答
    prompt_trace.reset()
    assert prompt_trace.list_entries(conv="12345", limit=1) == []
    assert prompt_trace.load_summaries() == 1
    items = prompt_trace.list_entries(conv="12345", limit=1)
    assert items and items[0]["model"] == "gpt-x" and items[0]["restored"] is True
    assert items[0]["route"]["model"] == "gpt"
    # 进程内已有新留痕时不覆盖
    assert prompt_trace.load_summaries() == 0
    prompt_trace.reset()
