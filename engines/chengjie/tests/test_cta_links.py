# -*- coding: utf-8 -*-
"""实施93 门禁：CTA 追踪短链 + 引导转化旅程（guide 风味）。

不变量：
- public_base 未配置 → 铸链如实拒绝（绝不把内网地址发给客户）；
- 同会话同目标 24h 复用同 token；铸链即推进「已引导」（quoting 位，棘轮）；
- 点击：302 到目标（可附 utm）、计数封顶、首点推进「已转化」（deal 位）且
  **不写 deal_events**（点击不是钱）、取消因旧阶段自动挂上的促发链；
- /r/{token} 公开路由不受 workflows flag 闸（发出去的链接不能变死链）；
- guide 风味：价格关键词推导停用；预设接线表换 guide 链并摘除 sales 链触发。
"""

import json
import time

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware

from src.inbox.cta_links import (
    build_redirect,
    handle_click,
    mint_link,
    resolve_cta_cfg,
    resolve_step_target_id,
)
from src.inbox.journey_stage import (
    mark_guided_by_cta,
    record_conversion,
    resolve_journey_cfg,
    scan_and_update,
)
from src.inbox.store import InboxStore
from src.web.routes.unified_inbox_workflow_routes import register_workflow_routes

_CFG = {"inbox": {"cta": {"public_base": "https://go.example.com"},
                  "workflows": {"journey": {"enabled": True}}}}


def _api_auth(request: Request):
    return None


def _store(tmp_path):
    return InboxStore(tmp_path / "inbox.db")


def _seed_conv(store, cid, *, chat_type="private"):
    with store._lock:
        store._conn.execute(
            """INSERT OR IGNORE INTO conversations
               (conversation_id, platform, account_id, chat_key, chat_type,
                last_ts, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (cid, "telegram", "a1", cid.split(":")[-1], chat_type,
             time.time(), time.time(), time.time()))
        store._conn.commit()


def _mk_target(store, name="落地页A", url="https://land.example.com/a",
               **kw) -> str:
    return store.upsert_cta_target({"name": name, "url": url, **kw})


class _FakeCM:
    def __init__(self, cfg):
        self.config = cfg
        self.flags = []

    def set_overlay_flag(self, path, value):
        self.flags.append((path, value))
        node = self.config
        keys = path.split(".")
        for k in keys[:-1]:
            node = node.setdefault(k, {})
        node[keys[-1]] = value
        return True, "已保存"


def _client(tmp_path, cfg=None):
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="t")
    register_workflow_routes(app, api_auth=_api_auth)
    app.state.inbox_store = InboxStore(tmp_path / "inbox.db")
    app.state.config_manager = _FakeCM(
        cfg if cfg is not None else json.loads(json.dumps(_CFG)))
    return TestClient(app)


# ── 铸链 ────────────────────────────────────────────────────────────────────

def test_mint_requires_public_base(tmp_path):
    store = _store(tmp_path)
    tid = _mk_target(store)
    res = mint_link(store, {}, "tg:a1:c1", tid)
    assert res == {"ok": False, "error": "no_public_base"}


def test_mint_reuse_and_guided_advance(tmp_path):
    store = _store(tmp_path)
    _seed_conv(store, "tg:a1:c1")
    tid = _mk_target(store)
    r1 = mint_link(store, _CFG, "tg:a1:c1", tid)
    assert r1["ok"] and r1["url"].startswith("https://go.example.com/r/")
    assert r1["reused"] is False
    assert store.get_journey_stage("tg:a1:c1")["stage"] == "quoting"
    assert store.get_journey_stage("tg:a1:c1")["src"] == "cta"
    r2 = mint_link(store, _CFG, "tg:a1:c1", tid)
    assert r2["reused"] is True and r2["token"] == r1["token"]
    # 更高阶段不回退（棘轮）
    store.set_journey_stage("tg:a1:c1", "deal", src="deal")
    mint_link(store, _CFG, "tg:a1:c1", tid)
    assert store.get_journey_stage("tg:a1:c1")["stage"] == "deal"


def test_mint_guards(tmp_path):
    store = _store(tmp_path)
    assert mint_link(store, _CFG, "tg:a1:c1", "ghost")["error"] == "target_not_found"
    tid = _mk_target(store, enabled=False)
    assert mint_link(store, _CFG, "tg:a1:c1", tid)["error"] == "target_disabled"


def test_resolve_step_target_primary(tmp_path):
    store = _store(tmp_path)
    assert resolve_step_target_id(store, "@primary") == ""
    t_off = _mk_target(store, name="停用", enabled=False)
    t_on = _mk_target(store, name="启用")
    assert resolve_step_target_id(store, "@primary") == t_on
    assert resolve_step_target_id(store, t_off) == ""
    assert resolve_step_target_id(store, t_on) == t_on
    assert resolve_step_target_id(store, "") == ""


# ── 点击 ────────────────────────────────────────────────────────────────────

def test_click_redirect_utm_and_conversion(tmp_path):
    store = _store(tmp_path)
    _seed_conv(store, "tg:a1:c2")
    tid = _mk_target(store)
    r = mint_link(store, _CFG, "tg:a1:c2", tid)
    url = handle_click(store, _CFG, r["token"])
    assert url.startswith("https://land.example.com/a?utm_source=chat")
    assert f"utm_content={r['token']}" in url
    st = store.get_journey_stage("tg:a1:c2")
    assert st["stage"] == "deal" and st["src"] == "cta"
    # 点击不是钱：deal_events 零行
    assert store.list_deal_events("tg:a1:c2") == []
    # 二次点击：仍 302、阶段不动、计数 +1
    assert handle_click(store, _CFG, r["token"]) is not None
    link = store.get_cta_link(r["token"])
    assert link["clicks"] == 2
    assert store.get_journey_stage("tg:a1:c2")["stage"] == "deal"


def test_click_unknown_token(tmp_path):
    store = _store(tmp_path)
    assert handle_click(store, _CFG, "nope1234") is None


def test_click_cancels_guided_stage_chains(tmp_path):
    """点击=转化：因「已引导」阶段自动挂上的促发链让路取消。"""
    store = _store(tmp_path)
    _seed_conv(store, "tg:a1:c3")
    tid = _mk_target(store)
    store.upsert_workflow_chain({
        "chain_id": "nudge", "name": "nudge",
        "steps": [{"action_type": "template", "note": "催点", "delay_hours": 24}],
        "trigger_conditions": {"stage_enter": "quoting"},
    })
    store.upsert_workflow_chain({
        "chain_id": "other", "name": "other",
        "steps": [{"action_type": "note", "note": "x", "delay_hours": 0}],
        "trigger_conditions": {},
    })
    r = mint_link(store, _CFG, "tg:a1:c3", tid)          # → guided
    eid_nudge = store.start_chain_execution("nudge", "tg:a1:c3", {})
    eid_other = store.start_chain_execution("other", "tg:a1:c3", {})
    handle_click(store, _CFG, r["token"])                 # → converted
    assert store.get_workflow_execution(eid_nudge)["status"] == "cancelled"
    assert store.get_workflow_execution(eid_other)["status"] == "running"


def test_no_utm_when_disabled(tmp_path):
    store = _store(tmp_path)
    t = {"target_id": "t1", "name": "x", "url": "https://x.example/y",
         "utm": 0}
    assert build_redirect(t, "tok", "chat") == "https://x.example/y"


# ── guide 风味 ──────────────────────────────────────────────────────────────

_GUIDE_CFG = {"inbox": {"workflows": {"journey": {
    "enabled": True, "flavor": "guide"}}}}


def test_flavor_parse_and_default():
    assert resolve_journey_cfg({})["flavor"] == "sales"
    assert resolve_journey_cfg(_GUIDE_CFG)["flavor"] == "guide"
    assert resolve_journey_cfg({"inbox": {"workflows": {"journey": {
        "flavor": "bogus"}}}})["flavor"] == "sales"


def test_guide_flavor_disables_quote_keywords(tmp_path):
    store = _store(tmp_path)
    now = time.time()
    cid = "tg:a1:g1"
    _seed_conv(store, cid)
    with store._lock:
        store._conn.execute(
            "INSERT INTO messages (message_id, conversation_id, direction,"
            " text, ts, ingested_at) VALUES (?,?,?,?,?,?)",
            (f"{cid}:in:1", cid, "in", "这个多少钱？", now - 100, now - 100))
        store._conn.execute(
            "INSERT INTO messages (message_id, conversation_id, direction,"
            " text, ts, ingested_at) VALUES (?,?,?,?,?,?)",
            (f"{cid}:out:1", cid, "out", "你好", now - 90, now - 90))
        store._conn.commit()
    scan_and_update(store, _GUIDE_CFG, {}, now=now)
    # guide：价格词不推 quoting（只到 contacted）；sales 对照会推到 quoting
    assert store.get_journey_stage(cid)["stage"] == "contacted"


def test_record_conversion_semantics(tmp_path):
    store = _store(tmp_path)
    _seed_conv(store, "tg:a1:g2")
    mark_guided_by_cta(store, "tg:a1:g2")
    r = record_conversion(store, "tg:a1:g2")
    assert r["ok"] and r["stage"] == "deal" and r["advanced"] is True
    # 幂等：已转化再点不动（复购语义留给真实二次转化事件）
    r2 = record_conversion(store, "tg:a1:g2")
    assert r2["advanced"] is False


def test_preset_flavor_wiring(tmp_path):
    """guide 风味：预设接 guide 链 + 摘除 sales 链触发（互斥防双打扰）。"""
    from src.inbox.workflow_starter import (
        GUIDE_STAGE_CHAIN_REC,
        STAGE_CHAIN_REC,
        apply_deal_engine,
        deal_engine_status,
        ensure_starter_chains,
    )
    store = _store(tmp_path)
    # 先按 sales 开一次（模拟本部署历史）
    cm_sales = _FakeCM({})
    apply_deal_engine(store, cm_sales, True)
    conds = json.loads(store.get_workflow_chain(
        STAGE_CHAIN_REC["quoting"])["trigger_conditions"])
    assert conds.get("stage_enter") == "quoting"
    # 切 guide 再开：guide 链接上、sales 链触发摘除
    cm_guide = _FakeCM(json.loads(json.dumps(_GUIDE_CFG)))
    res = apply_deal_engine(store, cm_guide, True, auto_send=True)
    assert res["ok"] is True
    for stage, cid in GUIDE_STAGE_CHAIN_REC.items():
        c = store.get_workflow_chain(cid)
        assert json.loads(c["trigger_conditions"]).get("stage_enter") == stage
        assert c["exec_mode"] == "auto"
    for _stage, cid in STAGE_CHAIN_REC.items():
        conds = json.loads(store.get_workflow_chain(cid)["trigger_conditions"])
        assert "stage_enter" not in conds, cid
    st = deal_engine_status(store, cm_guide.config)
    assert st["flavor"] == "guide" and st["engine_on"] is True
    assert st["auto_send"]["on"] is True


def test_guide_seed_pack_steps_have_cta():
    from src.inbox.workflow_starter import GUIDE_CHAINS
    ids = {c["chain_id"] for c in GUIDE_CHAINS}
    assert ids == {"guide_warmup_3", "guide_click_nudge", "guide_post_convert"}
    cta_steps = [s for c in GUIDE_CHAINS for s in c["steps"] if s.get("cta")]
    assert cta_steps and all(s["cta"] == "@primary" for s in cta_steps)


# ── 路由端到端 ──────────────────────────────────────────────────────────────

def test_cta_routes_end_to_end(tmp_path):
    c = _client(tmp_path)
    store = c.app.state.inbox_store
    _seed_conv(store, "tg:a1:r1")
    # 目标 CRUD
    r = c.post("/api/workspace/cta-targets",
               json={"name": "落地页", "url": "https://l.example/x"})
    assert r.status_code == 200
    tid = r.json()["target_id"]
    d = c.get("/api/workspace/cta-targets").json()
    assert d["public_base"] == "https://go.example.com"
    assert len(d["targets"]) == 1
    # 铸链
    r2 = c.post("/api/workspace/conv/tg:a1:r1/cta-link",
                json={"target_id": tid})
    assert r2.status_code == 200
    tok = r2.json()["token"]
    # 公开跳转（无鉴权、无 flag 闸）
    r3 = c.get(f"/r/{tok}", follow_redirects=False)
    assert r3.status_code == 302
    assert r3.headers["location"].startswith("https://l.example/x?utm_source=")
    assert store.get_journey_stage("tg:a1:r1")["stage"] == "deal"
    # journey 响应带 flavor + funnel 带 cta 块
    j = c.get("/api/workspace/conv/tg:a1:r1/journey").json()
    assert j["flavor"] == "sales"
    f = c.get("/api/workspace/journey-funnel").json()
    assert f["cta"]["links"] == 1 and f["cta"]["clicked"] == 1
    # 无效目标/坏 token
    assert c.post("/api/workspace/conv/tg:a1:r1/cta-link",
                  json={"target_id": "ghost"}).status_code == 422
    assert c.get("/r/zzzzzzzz", follow_redirects=False).status_code == 404
    assert c.get("/r/бэд", follow_redirects=False).status_code == 404
    # 删除目标
    assert c.delete(f"/api/workspace/cta-targets/{tid}").json()["ok"] is True


def test_mint_route_no_base_422(tmp_path):
    c = _client(tmp_path, cfg={"inbox": {}})
    store = c.app.state.inbox_store
    _seed_conv(store, "tg:a1:r2")
    tid = store.upsert_cta_target({"name": "x", "url": "https://x.example"})
    r = c.post("/api/workspace/conv/tg:a1:r2/cta-link", json={"target_id": tid})
    assert r.status_code == 422


def test_redirect_survives_workflows_off(tmp_path):
    """模块关（flag off）：管理端点 403，但已发出的短链照常 302。"""
    cfg = json.loads(json.dumps(_CFG))
    c = _client(tmp_path, cfg=cfg)
    store = c.app.state.inbox_store
    _seed_conv(store, "tg:a1:r3")
    tid = store.upsert_cta_target({"name": "x", "url": "https://x.example"})
    tok = c.post("/api/workspace/conv/tg:a1:r3/cta-link",
                 json={"target_id": tid}).json()["token"]
    cfg["inbox"].setdefault("workflows", {})["enabled"] = False
    assert c.get("/api/workspace/cta-targets").status_code == 403
    assert c.get(f"/r/{tok}", follow_redirects=False).status_code == 302


# ── 自动步 CTA 追加 ─────────────────────────────────────────────────────────

def test_auto_step_appends_link_or_degrades(tmp_path, monkeypatch):
    import asyncio

    from src.inbox import workflow_auto_step as was
    store = _store(tmp_path)
    _seed_conv(store, "tg:a1:s1")
    now = time.time()
    with store._lock:
        store._conn.execute(
            "INSERT INTO messages (message_id, conversation_id, direction,"
            " text, ts, ingested_at) VALUES (?,?,?,?,?,?)",
            ("tg:a1:s1:in:1", "tg:a1:s1", "in", "看看", now - 60, now - 60))
        store._conn.commit()

    async def _fake_reply(**_kw):
        return {"ok": True, "reply": "给你整理好了，点开看看", "reply_lang": "zh"}

    import src.inbox.persona_reply as pr
    monkeypatch.setattr(pr, "generate_persona_reply", _fake_reply)
    tid = _mk_target(store)
    ex = {"exec_id": "e1", "chain_id": "c1", "chain_name": "导流",
          "conversation_id": "tg:a1:s1", "current_step": 0}
    ok = asyncio.run(was._generate_and_stage(
        object(), store, ex, "发引导链接", cta_raw="@primary", cfg_root=_CFG))
    assert ok is True
    drafts = store.list_drafts(status="pending", conversation_id="tg:a1:s1",
                               limit=5)
    assert len(drafts) == 1
    text = drafts[0]["draft_text"]
    assert "https://go.example.com/r/" in text.splitlines()[-1]
    # 阶段已推进「已引导」
    assert store.get_journey_stage("tg:a1:s1")["stage"] == "quoting"
    # 无可用目标 → fail-closed 降级（不落草稿）
    _seed_conv(store, "tg:a1:s2")
    with store._lock:
        store._conn.execute(
            "INSERT INTO messages (message_id, conversation_id, direction,"
            " text, ts, ingested_at) VALUES (?,?,?,?,?,?)",
            ("tg:a1:s2:in:1", "tg:a1:s2", "in", "hi", now - 50, now - 50))
        store._conn.commit()
    ex2 = dict(ex, exec_id="e2", conversation_id="tg:a1:s2")
    ok2 = asyncio.run(was._generate_and_stage(
        object(), store, ex2, "发链", cta_raw="ghost-target", cfg_root=_CFG))
    assert ok2 is False
    assert store.list_drafts(status="pending", conversation_id="tg:a1:s2",
                             limit=5) == []


# ── 实施93c 点击提醒接线（事件 → SSE/铃铛/webhook 三消费面） ─────────────────

def test_first_click_publishes_cta_clicked(tmp_path, monkeypatch):
    """首点必须发布 cta_clicked（坐席实时跟进的信号源）；复点不重复发布。"""
    from src.integrations.shared import event_bus as eb
    store = _store(tmp_path)
    _seed_conv(store, "tg:a1:e1")
    tid = _mk_target(store)
    tok = mint_link(store, _CFG, "tg:a1:e1", tid)["token"]
    got = []

    class _Bus:
        def publish(self, etype, data):
            got.append((etype, data))

    monkeypatch.setattr(eb, "get_event_bus", lambda: _Bus())
    assert handle_click(store, _CFG, tok, now=time.time()) is not None
    assert handle_click(store, _CFG, tok, now=time.time() + 5) is not None
    clicks = [g for g in got if g[0] == "cta_clicked"]
    assert len(clicks) == 1                      # 仅首点
    assert clicks[0][1]["conversation_id"] == "tg:a1:e1"
    assert clicks[0][1]["target_name"] == "落地页A"


def test_cta_clicked_wired_to_sse_and_bell():
    """SSE 转发白名单 / 铃铛历史 / 会话级合并三张表都必须收录 cta_clicked——
    漏任何一张＝坐席端静默（事件发了没人听，93 初版实况）。"""
    from src.web.routes import unified_inbox_realtime_routes as rr
    assert "cta_clicked" in rr._SSE_EVENT_TYPES
    assert "cta_clicked" in rr._NOTIF_EVENT_TYPES
    assert "cta_clicked" in rr._COALESCE_NOTIF_TYPES


def test_cta_click_alias_is_business_alert():
    """webhook 别名 cta_click 必须存在且归 business 受众（面板可勾选）。"""
    from src.inbox.webhook_notifier import (
        _EVENT_ALIASES, alert_audience)
    assert "cta_clicked" in (_EVENT_ALIASES.get("cta_click") or {}).get("types", set())
    assert alert_audience("cta_click") == "business"


# ── 实施93b 深转化 webhook ──────────────────────────────────────────────────

_SECRET_CFG = {"inbox": {
    "cta": {"public_base": "https://go.example.com", "webhook_secret": "s3cr3t"},
    "workflows": {"journey": {"enabled": True}}}}


def _convert_client(tmp_path):
    c = _client(tmp_path, cfg=json.loads(json.dumps(_SECRET_CFG)))
    store = c.app.state.inbox_store
    _seed_conv(store, "tg:a1:w1")
    tid = store.upsert_cta_target({"name": "站点", "url": "https://x.example"})
    tok = c.post("/api/workspace/conv/tg:a1:w1/cta-link",
                 json={"target_id": tid}).json()["token"]
    return c, store, tok


def test_convert_webhook_secret_discipline(tmp_path):
    """S6 同款：未配置 secret 整体拒绝；头不匹配拒绝；绝不静默接受。"""
    c = _client(tmp_path, cfg=json.loads(json.dumps(_CFG)))  # 无 secret
    r = c.post("/api/cta/convert", json={"conversation_id": "x"})
    assert r.json() == {"ok": False, "reason": "webhook_secret_not_configured"}
    c2, _, tok = _convert_client(tmp_path)
    r2 = c2.post("/api/cta/convert", json={"token": tok},
                 headers={"X-CTA-Secret": "wrong"})
    assert r2.json() == {"ok": False, "reason": "unauthorized"}


def test_convert_webhook_amount_deal_and_ref_idempotent(tmp_path):
    c, store, tok = _convert_client(tmp_path)
    h = {"X-CTA-Secret": "s3cr3t"}
    r = c.post("/api/cta/convert", headers=h, json={
        "token": tok, "kind": "order", "amount": 49.9,
        "currency": "USD", "ref": "ord-1001"})
    j = r.json()
    assert j["ok"] is True and j["stage"] == "deal"
    evs = store.list_deal_events("tg:a1:w1")
    assert len(evs) == 1
    assert evs[0]["source"] == "cta_webhook" and evs[0]["ref"] == "ord-1001"
    assert abs(float(evs[0]["amount"]) - 49.9) < 1e-6
    # 同 ref 重推 → 去重，不新增行
    r2 = c.post("/api/cta/convert", headers=h, json={
        "token": tok, "kind": "order", "amount": 49.9, "ref": "ord-1001"})
    assert r2.json()["deduped"] is True
    assert len(store.list_deal_events("tg:a1:w1")) == 1
    # 二笔新 ref → repeat（复购语义走 record_deal 既有逻辑）
    r3 = c.post("/api/cta/convert", headers=h, json={
        "conversation_id": "tg:a1:w1", "amount": 20, "ref": "ord-1002"})
    assert r3.json()["stage"] == "repeat"


def test_convert_webhook_light_and_zero_amount_ref(tmp_path):
    c, store, tok = _convert_client(tmp_path)
    h = {"X-CTA-Secret": "s3cr3t"}
    # 轻转化（无金额无 ref）：只推阶段，不落台账
    r = c.post("/api/cta/convert", headers=h,
               json={"token": tok, "kind": "signup"})
    assert r.json()["stage"] == "deal"
    assert store.list_deal_events("tg:a1:w1") == []
    # 带 ref 的 0 元转化：落 0 元锚点行（幂等可查）+ 重推去重
    r2 = c.post("/api/cta/convert", headers=h,
                json={"token": tok, "kind": "install", "ref": "ins-7"})
    assert r2.json()["ok"] is True
    assert len(store.list_deal_events("tg:a1:w1")) == 1
    r3 = c.post("/api/cta/convert", headers=h,
                json={"token": tok, "kind": "install", "ref": "ins-7"})
    assert r3.json()["deduped"] is True
    # 未知 token / 无会话线索
    r4 = c.post("/api/cta/convert", headers=h, json={"token": "nope"})
    assert r4.json() == {"ok": False, "reason": "unknown_token"}
