# -*- coding: utf-8 -*-
"""「接管即静音」可见化 + 自动接回 门禁（P0 2026-08-09，.198/.104 事故沉淀）。

事故形态：坐席手动发一条消息 → 会话被切 manual 停 AI（Sprint1 设计），但该写入
source 留空、UI 无提示、无恢复机制——实测两台坐席机各有会话在 manual 钉死 27h，
坐席全程不知道是自己关掉了 AI。本文件钉四层修复的关键不变量：

1. 接管打标（takeover_rearm.record_agent_takeover）：保留接管前档位、重复接管
   不覆盖首次 from、旧 store 签名诚实降级；
2. 自动接回 sweep：只碰 source=takeover* 的 manual（显式手动/守卫降档永不动）、
   恢复到接管前档位、窗口内不动、配置关不动；
3. 路由：GET /automation 捎带 mode_source/rearm；POST 群聊上全自动要 confirm_group；
   GET why-no-reply findings 含 takeover_manual；POST warmup-review 主管门禁 + 写 overlay；
4. watchdog 接线：_check_takeover_rearm 消费 app.state.inbox_store。
"""
from __future__ import annotations

import time
from types import SimpleNamespace

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware

from src.inbox.models import InboxConversation
from src.inbox.store import InboxStore
from src.inbox.takeover_rearm import (
    is_takeover_source,
    rearm_restore_mode,
    rearm_state,
    record_agent_takeover,
    sweep_takeover_rearm,
    takeover_prev_mode,
    takeover_rearm_cfg,
)
from src.web.routes.unified_inbox_routes import register_unified_inbox_routes

_REARM_ON = {"inbox": {
    "takeover_rearm": {"enabled": True, "after_minutes": 30},
    "auto_draft": {"automation_mode": "auto_ai"},
}}


# ── 纯函数：source 编码 ────────────────────────────────────────────────


def test_source_vocabulary_roundtrip():
    assert is_takeover_source("takeover")
    assert is_takeover_source("takeover_from:auto_ai")
    assert not is_takeover_source("human")
    assert not is_takeover_source("sweep")
    assert not is_takeover_source("")
    assert takeover_prev_mode("takeover_from:auto_ai") == "auto_ai"
    assert takeover_prev_mode("takeover_from:review") == "review"
    assert takeover_prev_mode("takeover") == ""
    assert takeover_prev_mode("takeover_from:bogus") == ""


def test_cfg_defaults_off():
    cfg = takeover_rearm_cfg(None)
    assert cfg["enabled"] is False and cfg["after_minutes"] == 30.0
    cfg2 = takeover_rearm_cfg({"inbox": {"takeover_rearm": {
        "enabled": True, "after_minutes": 0}}})
    assert cfg2["enabled"] is True
    assert cfg2["after_minutes"] >= 1.0   # 下限护栏


# ── 接管打标（真 InboxStore）───────────────────────────────────────────


def test_takeover_preserves_prev_mode(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    cid = "telegram:a:100"
    store.set_automation_mode(cid, "auto_ai", source="human")
    src = record_agent_takeover(store, cid)
    assert src == "takeover_from:auto_ai"
    meta = store.get_automation_mode_meta(cid)
    assert meta["mode"] == "manual" and meta["source"] == src
    store.close()


def test_repeated_takeover_keeps_first_from_and_refreshes_ts(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    cid = "telegram:a:101"
    store.set_automation_mode(cid, "review", source="human")
    record_agent_takeover(store, cid)
    ts1 = store.get_automation_mode_meta(cid)["updated_at"]
    time.sleep(0.02)
    src2 = record_agent_takeover(store, cid)   # 第二条手动消息
    meta = store.get_automation_mode_meta(cid)
    assert src2 == "takeover_from:review"      # from 不被覆盖成 manual
    assert meta["updated_at"] >= ts1           # 计时器被重置
    store.close()


def test_takeover_without_explicit_row(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    src = record_agent_takeover(store, "telegram:a:102")
    assert src == "takeover"
    store.close()


def test_takeover_old_store_signature_degrades_honestly():
    """旧 store / 测试假件无 source 形参：仍切 manual，返回空串（不谎报打标）。"""
    class OldStore:
        def __init__(self):
            self.calls = []

        def get_automation_mode_if_set(self, cid):
            return None

        def set_automation_mode(self, cid, mode):   # 无 source 形参
            self.calls.append((cid, mode))

    s = OldStore()
    assert record_agent_takeover(s, "c") == ""
    assert s.calls == [("c", "manual")]


# ── 自动接回 sweep（真 InboxStore + list_takeover_manual）──────────────


def _aged(store, cid, sec=3600):
    """把该行 updated_at 拨回 sec 秒前（sweep 判据用）。"""
    with store._lock:
        store._conn.execute(
            "UPDATE conversation_settings SET updated_at=? WHERE conversation_id=?",
            (time.time() - sec, cid))
        store._conn.commit()


def test_sweep_restores_takeover_rows_only(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    # 接管态（应恢复到 auto_ai）
    store.set_automation_mode("c:tk", "auto_ai", source="human")
    record_agent_takeover(store, "c:tk")
    _aged(store, "c:tk")
    # 坐席显式手动（绝不能动）
    store.set_automation_mode("c:human", "manual", source="human")
    _aged(store, "c:human")
    # 守卫降档（绝不能动）
    store.set_automation_mode("c:guard", "manual", source="sweep")
    _aged(store, "c:guard")
    # 接管但还在窗口内（不动）
    store.set_automation_mode("c:fresh", "auto_ai", source="human")
    record_agent_takeover(store, "c:fresh")

    res = sweep_takeover_rearm(store, _REARM_ON)
    assert res["enabled"] is True
    assert res["restored"] == 1 and res["restored_cids"] == ["c:tk"]
    meta = store.get_automation_mode_meta("c:tk")
    assert meta["mode"] == "auto_ai" and meta["source"] == "rearm"
    assert store.get_automation_mode_meta("c:human")["mode"] == "manual"
    assert store.get_automation_mode_meta("c:guard")["mode"] == "manual"
    assert store.get_automation_mode_meta("c:fresh")["mode"] == "manual"
    store.close()


def test_sweep_disabled_is_noop(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    record_agent_takeover(store, "c:x")
    _aged(store, "c:x")
    res = sweep_takeover_rearm(store, {"inbox": {}})
    assert res["enabled"] is False and res["restored"] == 0
    assert store.get_automation_mode_meta("c:x")["mode"] == "manual"
    store.close()


def test_sweep_unknown_prev_falls_back_to_global_default(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    record_agent_takeover(store, "c:nofrom")       # source="takeover"（无 from）
    _aged(store, "c:nofrom")
    cfg = {"inbox": {"takeover_rearm": {"enabled": True, "after_minutes": 30},
                     "auto_draft": {"automation_mode": "review"}}}
    res = sweep_takeover_rearm(store, cfg)
    assert res["restored"] == 1
    assert store.get_automation_mode_meta("c:nofrom")["mode"] == "review"
    store.close()


def test_rearm_restore_mode_semantics():
    assert rearm_restore_mode("takeover_from:auto_ai", None) == "auto_ai"
    assert rearm_restore_mode("takeover_from:review", None) == "review"
    # 无 from → 全局默认（缺省 auto_ai）
    assert rearm_restore_mode("takeover", None) == "auto_ai"
    # 全局默认是 manual（怪配置）→ 无事可做
    assert rearm_restore_mode(
        "takeover", {"inbox": {"auto_draft": {"automation_mode": "manual"}}}) == ""


def test_rearm_state_only_for_takeover_manual():
    now = time.time()
    meta_tk = {"mode": "manual", "source": "takeover_from:auto_ai",
               "updated_at": now - 60}
    st = rearm_state(meta_tk, _REARM_ON, now=now)
    assert st and st["enabled"] and st["restore_mode"] == "auto_ai"
    assert 0 < st["eta_in_sec"] <= 30 * 60
    # 显式手动（source=human）→ None（横幅不渲染，自动接回不认领）
    assert rearm_state({"mode": "manual", "source": "human",
                        "updated_at": now}, _REARM_ON) is None
    # 非 manual → None
    assert rearm_state({"mode": "auto_ai", "source": "takeover",
                        "updated_at": now}, _REARM_ON) is None
    # 配置关：仍返回状态（横幅可见）但 enabled=False（不承诺自动接回）
    st_off = rearm_state(meta_tk, {"inbox": {}}, now=now)
    assert st_off is not None and st_off["enabled"] is False


# ── 路由契约 ───────────────────────────────────────────────────────────


class _Templates:
    def TemplateResponse(self, request, name, context):
        raise AssertionError("page rendering is not used in API tests")


def _client(tmp_path, config=None, overlay_calls=None):
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="t")

    def page_auth(request: Request):
        return True

    def api_auth(request: Request):
        return True

    register_unified_inbox_routes(
        app, page_auth=page_auth, api_auth=api_auth, templates=_Templates())

    @app.post("/__login")
    async def _login(request: Request):
        body = await request.json()
        request.session.clear()
        for k in ("username", "role"):
            if k in body:
                request.session[k] = body[k]
        return {"ok": True}

    store = InboxStore(tmp_path / "inbox.db")
    app.state.inbox_store = store

    def _set_overlay_flag(path, value):
        if overlay_calls is not None:
            overlay_calls.append((path, value))
        return True, ""

    app.state.config_manager = SimpleNamespace(
        config=config if config is not None else {},
        set_overlay_flag=_set_overlay_flag)
    return TestClient(app), store


def test_automation_get_carries_mode_source_and_rearm(tmp_path):
    c, store = _client(tmp_path, config=_REARM_ON)
    cid = "telegram:a:200"
    store.set_automation_mode(cid, "auto_ai", source="human")
    record_agent_takeover(store, cid)
    r = c.get("/api/unified-inbox/automation?platform=telegram"
              "&account_id=a&chat_key=200")
    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "manual"
    ms = body.get("mode_source")
    assert ms and ms["source"] == "takeover_from:auto_ai"
    rr = body.get("rearm")
    assert rr and rr["enabled"] and rr["restore_mode"] == "auto_ai"
    store.close()


def test_automation_get_mode_source_none_without_explicit_row(tmp_path):
    c, store = _client(tmp_path)
    r = c.get("/api/unified-inbox/automation?platform=telegram"
              "&account_id=a&chat_key=201")
    body = r.json()
    assert body.get("mode_source") is None and body.get("rearm") is None
    store.close()


def test_post_automation_group_requires_confirm(tmp_path):
    c, store = _client(tmp_path)
    cid = "telegram:a:-100999"
    store.upsert_conversation(InboxConversation(
        conversation_id=cid, platform="telegram", account_id="a",
        chat_key="-100999", chat_type="group", display_name="G"))
    payload = {"platform": "telegram", "account_id": "a",
               "chat_key": "-100999", "mode": "auto_ai"}
    r = c.post("/api/unified-inbox/automation", json=payload)
    assert r.status_code == 409
    det = r.json()["detail"]
    assert det["code"] == "group_confirm_required"
    assert store.get_automation_mode_if_set(cid) is None   # 未写库
    # 带确认重试 → 放行
    r2 = c.post("/api/unified-inbox/automation",
                json={**payload, "confirm_group": True})
    assert r2.status_code == 200 and r2.json()["ok"]
    assert store.get_automation_mode_if_set(cid) == "auto_ai"
    # 降档方向（群 → manual）不需要确认
    r3 = c.post("/api/unified-inbox/automation",
                json={**payload, "mode": "manual"})
    assert r3.status_code == 200
    store.close()


def test_post_automation_private_chat_unaffected(tmp_path):
    c, store = _client(tmp_path)
    store.upsert_conversation(InboxConversation(
        conversation_id="telegram:a:300", platform="telegram", account_id="a",
        chat_key="300", chat_type="private", display_name="P"))
    r = c.post("/api/unified-inbox/automation", json={
        "platform": "telegram", "account_id": "a", "chat_key": "300",
        "mode": "auto_ai"})
    assert r.status_code == 200
    # 会话行不存在（从未入库）→ fail-open 放行
    r2 = c.post("/api/unified-inbox/automation", json={
        "platform": "telegram", "account_id": "a", "chat_key": "301",
        "mode": "auto_ai"})
    assert r2.status_code == 200
    store.close()


def test_why_no_reply_route_reports_takeover(tmp_path):
    c, store = _client(tmp_path, config=_REARM_ON)
    cid = "telegram:a:400"
    store.upsert_conversation(InboxConversation(
        conversation_id=cid, platform="telegram", account_id="a",
        chat_key="400", chat_type="private", display_name="N"))
    store.set_automation_mode(cid, "auto_ai", source="human")
    record_agent_takeover(store, cid)
    r = c.get("/api/unified-inbox/why-no-reply?platform=telegram"
              "&account_id=a&chat_key=400")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    codes = [f["code"] for f in body["findings"]]
    assert "takeover_manual" in codes
    tk = next(f for f in body["findings"] if f["code"] == "takeover_manual")
    assert tk["level"] == "block"
    assert tk["params"]["restore_mode"] == "auto_ai"
    assert tk["params"]["rearm_enabled"] is True
    store.close()


def test_why_no_reply_route_looks_alive_when_clean(tmp_path):
    cfg = {**_REARM_ON,
           "platform_login": {"telegram": {"companion_runtime": True}}}
    c, store = _client(tmp_path, config=cfg)
    cid = "telegram:a:401"
    store.upsert_conversation(InboxConversation(
        conversation_id=cid, platform="telegram", account_id="a",
        chat_key="401", chat_type="private", display_name="N"))
    store.set_automation_mode(cid, "auto_ai", source="human")
    r = c.get("/api/unified-inbox/why-no-reply?platform=telegram"
              "&account_id=a&chat_key=401")
    body = r.json()
    levels = {f["code"]: f["level"] for f in body["findings"]}
    assert levels.get("looks_alive") == "ok"
    assert not any(lv == "block" for lv in levels.values())
    store.close()


def test_why_no_reply_requires_params(tmp_path):
    c, store = _client(tmp_path)
    r = c.get("/api/unified-inbox/why-no-reply?platform=telegram"
              "&account_id=a&chat_key=")
    assert r.status_code == 400
    store.close()


def test_warmup_review_route_supervisor_gated(tmp_path):
    calls = []
    c, store = _client(tmp_path, overlay_calls=calls)
    # 未登录/无角色 → 403
    r = c.post("/api/unified-inbox/warmup-review", json={"enabled": False})
    assert r.status_code == 403 and not calls
    # 普通坐席 → 403
    c.post("/__login", json={"username": "a1", "role": "agent"})
    r2 = c.post("/api/unified-inbox/warmup-review", json={"enabled": False})
    assert r2.status_code == 403 and not calls
    # 主管 → 写 overlay
    c.post("/__login", json={"username": "boss", "role": "master"})
    r3 = c.post("/api/unified-inbox/warmup-review", json={"enabled": False})
    assert r3.status_code == 200 and r3.json()["ok"]
    assert calls == [(
        "companion.proactive_topic.cold_start.warmup_review", False)]
    store.close()


# ── watchdog 接线 ──────────────────────────────────────────────────────


def test_watchdog_check_takeover_rearm_wiring(tmp_path):
    from src.inbox.health_watchdog import HealthWatchdog

    store = InboxStore(tmp_path / "inbox.db")
    store.set_automation_mode("c:wd", "auto_ai", source="human")
    record_agent_takeover(store, "c:wd")
    _aged(store, "c:wd")
    app = SimpleNamespace(state=SimpleNamespace(inbox_store=store))
    cm = SimpleNamespace(config=_REARM_ON)
    wd = HealthWatchdog(app=app, config_manager=cm)
    wd._check_takeover_rearm()
    assert store.get_automation_mode_meta("c:wd")["mode"] == "auto_ai"
    store.close()


def test_watchdog_check_survives_missing_store():
    from src.inbox.health_watchdog import HealthWatchdog
    app = SimpleNamespace(state=SimpleNamespace())
    cm = SimpleNamespace(config=_REARM_ON)
    wd = HealthWatchdog(app=app, config_manager=cm)
    wd._check_takeover_rearm()   # 不抛即过
