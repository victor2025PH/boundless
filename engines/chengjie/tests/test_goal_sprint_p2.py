# -*- coding: utf-8 -*-
"""冲刺完成提速门禁（P2 2026-08-30）。

覆盖：expired_with_signal 分桶（ledger）/ 冲刺失守即时告警（scan_sprint_miss
幂等+闸门+payload 形状）/ auto_settle_contact 自动结算（默认关、开后直落 done、
result 单列）/ expired→done 补确认迁移（路由）/ 冲刺插队回程票（settle 与
人工终局两条路都恢复）。
"""

from __future__ import annotations

import time
from types import SimpleNamespace

from fastapi import FastAPI, Request
from starlette.testclient import TestClient

from src.companion.goals.store import GoalStore

NOW = time.time()

_ROOT_ON = {"companion": {"goals": {
    "enabled": True,
    "sprint": {"enabled": True},
    "notify": {"enabled": True},
}}}


def _sprint_goal(gs, *, started_ago=1200.0, span_h=3.0, chat="u1",
                 note="加个微信吧", extra_params=None):
    params = {"pace": "today", "note": note}
    if extra_params:
        params.update(extra_params)
    return gs.create_goal(
        conversation_id=f"telegram:a1:{chat}", platform="telegram",
        account_id="a1", chat_key=chat, template="custom", autonomy="auto",
        title="拿到微信号", deadline_days=span_h / 24.0, params=params,
        now=NOW - started_ago)


# ── ledger：expired_with_signal 分桶 ─────────────────────────────────────
def test_expired_with_signal_result_bucket():
    from src.companion.goals.ledger import settle_goal
    from src.companion.goals.signals import GoalSignals
    from src.companion.goals.templates import get_template
    base = {
        "status": "active", "milestone_idx": 0, "progress": 0.0, "result": "",
        "start_ts": NOW - 4 * 3600, "deadline_ts": NOW - 60,
        "template": "custom",
    }
    sig = GoalSignals(now=NOW)
    # 带达成信号的过期 → expired_with_signal
    g1 = dict(base, params={"outcome_signal": {"kind": "contact", "v": "vx1"}})
    r1 = settle_goal(template_id="custom", template=get_template("custom"),
                     goal=g1, signals=sig, now=NOW)
    assert r1["status"] == "expired"
    assert r1["result"] == "expired_with_signal"
    # 无信号 → 维持 deadline 口径
    g2 = dict(base, params={})
    r2 = settle_goal(template_id="custom", template=get_template("custom"),
                     goal=g2, signals=sig, now=NOW)
    assert r2["status"] == "expired" and r2["result"] == "deadline"


# ── scan_sprint_miss：即时单条告警 ───────────────────────────────────────
def _expired_sprint_row(gs, **kw):
    g = _sprint_goal(gs, **kw)
    gid = str(g["goal_id"])
    gs.update_goal_fields(
        gid, status="expired", done_at=NOW - 60, result="expired_with_signal")
    return gs.get_goal(gid)


def test_sprint_miss_alerts_once_and_marks():
    from src.companion.goals.notify import MISS_EVENT_KIND, scan_sprint_miss
    gs = GoalStore(":memory:")
    g = _expired_sprint_row(gs)
    events = []
    out = scan_sprint_miss(gs, _ROOT_ON, now=NOW,
                           publish=lambda name, p: events.append((name, p)))
    assert out == {"scanned": 1, "alerted": 1}
    name, payload = events[0]
    assert name == "goal_miss_alert"
    # 日报同构段（webhook formatter 零改动可渲染）
    assert payload["count"] == 1 and payload["expired"] == 1
    assert payload["by_template"] and payload["by_account"]
    # 单目标附加段
    assert payload["sprint"] is True
    assert payload["goal_id"] == str(g["goal_id"])
    assert payload["with_signal"] is True
    assert payload["rate_key"].startswith("goal_miss:sprint:")
    # 幂等标记落账 → 第二轮零动作
    kinds = [e["kind"] for e in gs.list_events(str(g["goal_id"]), limit=10)]
    assert MISS_EVENT_KIND in kinds
    out2 = scan_sprint_miss(gs, _ROOT_ON, now=NOW,
                            publish=lambda name, p: events.append((name, p)))
    assert out2["alerted"] == 0 and len(events) == 1


def test_sprint_miss_skips_natural_and_gates():
    from src.companion.goals.notify import scan_sprint_miss
    gs = GoalStore(":memory:")
    # natural 过期目标不走即时通道（归日报）
    g = gs.create_goal(
        conversation_id="telegram:a1:u5", platform="telegram", account_id="a1",
        chat_key="u5", template="custom", autonomy="auto", deadline_days=14,
        params={"note": "长线"}, now=NOW - 20 * 86400)
    gs.update_goal_fields(str(g["goal_id"]), status="expired",
                          done_at=NOW - 60, result="deadline")
    events = []
    out = scan_sprint_miss(gs, _ROOT_ON, now=NOW,
                           publish=lambda n, p: events.append(p))
    assert out["alerted"] == 0 and not events
    # sprint 总闸关 → 静默
    gs2 = GoalStore(":memory:")
    _expired_sprint_row(gs2)
    root_off = {"companion": {"goals": {
        "enabled": True, "sprint": {"enabled": False},
        "notify": {"enabled": True}}}}
    assert scan_sprint_miss(gs2, root_off, now=NOW,
                            publish=lambda n, p: events.append(p)
                            )["alerted"] == 0
    # notify 总闸关 → 静默
    root_nn = {"companion": {"goals": {
        "enabled": True, "sprint": {"enabled": True},
        "notify": {"enabled": False}}}}
    assert scan_sprint_miss(gs2, root_nn, now=NOW,
                            publish=lambda n, p: events.append(p)
                            )["alerted"] == 0
    assert not events


def test_sprint_miss_marked_rows_stay_out_of_digest():
    """即时告警标记过的冲刺行不再进失守日报（同一 MISS_EVENT_KIND）。"""
    from src.companion.goals.notify import scan_miss_digest, scan_sprint_miss
    gs = GoalStore(":memory:")
    _expired_sprint_row(gs)
    scan_sprint_miss(gs, _ROOT_ON, now=NOW, publish=lambda n, p: None)
    digested = []
    out = scan_miss_digest(gs, _ROOT_ON, now=NOW,
                           publish=lambda n, p: digested.append(p))
    assert out["pending"] == 0 and not digested


# ── auto_settle_contact：信号自动结算 ────────────────────────────────────
def _cfg_obj(auto_settle: bool):
    return SimpleNamespace(config={"companion": {"goals": {
        "enabled": True, "db_path": ":memory:",
        "sprint": {"enabled": True, "auto_settle_contact": auto_settle},
    }}}, config_path=None)


def _run_block(cfg_obj, gs, conv, text):
    """经 build_block_for_chat 真链（含 outcome 检测→自动结算）。"""
    import src.companion.goals.service as svc

    class _CfgStoreShim:
        pass

    # 注入自建 store：build_block_for_chat 走 get_configured_store —— 用
    # monkeypatch 不方便（非测试夹具函数），这里直接替换单例解析。
    orig = svc.get_configured_store
    svc.get_configured_store = lambda *a, **k: gs
    try:
        uc = {}
        block = svc.build_block_for_chat(
            cfg_obj, platform="telegram", chat_key=conv.split(":", 2)[2],
            account_id="a1", conversation_id=conv,
            user_context=uc, inbound_text=text, now=NOW)
        return block, uc
    finally:
        svc.get_configured_store = orig


def test_auto_settle_contact_settles_sprint_goal():
    gs = GoalStore(":memory:")
    g = _sprint_goal(gs, note="要到TA的微信")
    gid = str(g["goal_id"])
    conv = str(g["conversation_id"])
    block, uc = _run_block(_cfg_obj(True), gs, conv, "加我微信号 vx88996677")
    assert block is None                       # 目标已完成，本轮不再注入
    meta = uc.get("_goal_inject_meta") or {}
    assert meta.get("reason") == "outcome_auto_settled"
    fresh = gs.get_goal(gid)
    assert fresh["status"] == "done"
    assert str(fresh["result"]).startswith("signal:contact:")
    assert float(fresh["progress"]) == 1.0
    kinds = [e["kind"] for e in gs.list_events(gid, limit=20)]
    assert "outcome_signal" in kinds
    assert any("signal_auto" in str(e.get("detail") or "")
               for e in gs.list_events(gid, limit=20))


def test_auto_settle_off_keeps_hint_only():
    gs = GoalStore(":memory:")
    g = _sprint_goal(gs, note="要到TA的微信", chat="u2")
    gid = str(g["goal_id"])
    conv = str(g["conversation_id"])
    block, _uc = _run_block(_cfg_obj(False), gs, conv, "加我微信号 vx88996677")
    fresh = gs.get_goal(gid)
    assert fresh["status"] == "active"         # 只提示不结算（默认行为）
    sig = (fresh.get("params") or {}).get("outcome_signal") or {}
    assert sig.get("v")                        # 信号照记（绿条提示用）
    assert block is not None                   # 目标块照常注入


def test_auto_settle_ignores_natural_goals():
    gs = GoalStore(":memory:")
    g = gs.create_goal(
        conversation_id="telegram:a1:u3", platform="telegram", account_id="a1",
        chat_key="u3", template="custom", autonomy="auto", deadline_days=14,
        params={"note": "要到TA的微信"}, now=NOW - 600)
    conv = str(g["conversation_id"])
    _block, _uc = _run_block(_cfg_obj(True), gs, conv, "微信号 vx88996677")
    fresh = gs.get_goal(str(g["goal_id"]))
    assert fresh["status"] == "active"         # natural 不吃冲刺自动结算


# ── 回程票：冲刺终局恢复被暂停的长线目标 ─────────────────────────────────
def test_linked_resume_on_settle_expiry():
    from src.companion.goals.service import refresh_goal
    gs = GoalStore(":memory:")
    longg = gs.create_goal(
        conversation_id="telegram:a1:u4", platform="telegram", account_id="a1",
        chat_key="u4", template="custom", autonomy="auto", deadline_days=14,
        params={"note": "长线"}, now=NOW - 86400)
    lid = str(longg["goal_id"])
    gs.update_goal_fields(lid, status="paused")
    sprint = _sprint_goal(gs, chat="u4", started_ago=4 * 3600, span_h=3.0,
                          extra_params={"resume_goal_id": lid})
    # settle-on-read：已过期 → expired + 回程票恢复长线目标
    res = refresh_goal(gs, _ROOT_ON, sprint, now=NOW)
    assert str(res["goal"]["status"]) == "expired"
    assert gs.get_goal(lid)["status"] == "active"
    kinds = [e["kind"] for e in gs.list_events(lid, limit=10)]
    assert "status" in kinds


def test_linked_resume_only_touches_paused():
    from src.companion.goals.service import maybe_resume_linked_goal
    gs = GoalStore(":memory:")
    longg = gs.create_goal(
        conversation_id="telegram:a1:u6", platform="telegram", account_id="a1",
        chat_key="u6", template="custom", autonomy="auto", deadline_days=14,
        params={"note": "长线"}, now=NOW - 86400)
    lid = str(longg["goal_id"])
    gs.update_goal_fields(lid, status="cancelled")   # 人工又动过 → 不碰
    done_sprint = {"status": "done",
                   "goal_id": "s1", "params": {"resume_goal_id": lid}}
    assert maybe_resume_linked_goal(gs, done_sprint) is False
    assert gs.get_goal(lid)["status"] == "cancelled"
    # 非终态目标不触发
    assert maybe_resume_linked_goal(
        gs, {"status": "active", "goal_id": "s2",
             "params": {"resume_goal_id": lid}}) is False


# ── 路由：expired→done 补确认 + 人工终局回程票 ───────────────────────────
def _client(gs):
    from src.web.routes.goal_routes import register_goal_routes
    cfg = {"companion": {"goals": {"enabled": True, "db_path": ":memory:"}}}
    app = FastAPI()

    def auth_dep(request: Request) -> None:
        request.scope["session"] = {"role": "", "username": "tester"}

    import src.companion.goals.service as svc
    orig = svc.get_configured_store
    svc.get_configured_store = lambda *a, **k: gs
    register_goal_routes(
        app, auth_dep, SimpleNamespace(config=cfg, config_path=None))
    client = TestClient(app)
    client._restore = (svc, orig)   # 调用方负责还原
    return client


def test_route_expired_to_done_revive():
    gs = GoalStore(":memory:")
    g = _expired_sprint_row(gs)
    gid = str(g["goal_id"])
    client = _client(gs)
    try:
        r = client.post(f"/api/goals/{gid}/status",
                        json={"action": "done",
                              "meta": {"product": "微信号", "amount": 0}})
        assert r.status_code == 200
        body = r.json()
        assert body["goal"]["status"] == "done"
        fresh = gs.get_goal(gid)
        assert fresh["result"] == "manual:agent"
        assert float(fresh["progress"]) == 1.0
        # cancelled 仍不可复活
        g2 = _sprint_goal(gs, chat="u9")
        gid2 = str(g2["goal_id"])
        gs.update_goal_fields(gid2, status="cancelled", done_at=NOW)
        r2 = client.post(f"/api/goals/{gid2}/status", json={"action": "done"})
        assert r2.status_code == 400
    finally:
        svc, orig = client._restore
        svc.get_configured_store = orig


# ── 路由：立即推进（nudge）────────────────────────────────────────────────
class _NudgeInbox:
    def __init__(self, mode="auto_ai", meta=None):
        self.mode = mode
        self.meta = meta or {}

    def get_automation_mode(self, cid):
        return self.mode

    def get_conv_meta(self, cid):
        return dict(self.meta)

    def list_recent_messages(self, cid, limit=30):
        return []


def _nudge_client(gs, care_store, monkeypatch, *, inbox=None,
                  sprint_enabled=True):
    import src.contacts.care_schedule as cs_mod
    import src.integrations.protocol_bridge as pb
    from src.web.routes.goal_routes import register_goal_routes

    monkeypatch.setattr(cs_mod, "_singleton", care_store)
    monkeypatch.setattr(pb, "_inbox_store_getter",
                        (lambda: inbox) if inbox is not None else None)
    cfg = {"companion": {"goals": {
        "enabled": True, "db_path": ":memory:",
        "sprint": {"enabled": sprint_enabled},
    }}}
    app = FastAPI()

    def auth_dep(request: Request) -> None:
        request.scope["session"] = {"role": "", "username": "tester"}

    import src.companion.goals.service as svc
    orig = svc.get_configured_store
    svc.get_configured_store = lambda *a, **k: gs
    register_goal_routes(
        app, auth_dep, SimpleNamespace(config=cfg, config_path=None))
    client = TestClient(app)
    client._restore = (svc, orig)
    return client


def test_nudge_schedules_immediate_phase(monkeypatch):
    from src.contacts.care_schedule import CareScheduleStore
    gs, cs = GoalStore(":memory:"), CareScheduleStore(":memory:")
    g = _sprint_goal(gs, chat="n1")
    gid = str(g["goal_id"])
    client = _nudge_client(gs, cs, monkeypatch, inbox=_NudgeInbox())
    try:
        r = client.post(f"/api/goals/{gid}/sprint/nudge")
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True and body["phase"] == 0
        rows = cs.list_due(now=NOW + 30)
        assert len(rows) == 1
        assert rows[0]["topic_norm"] == f"goal:{gid}:p0"
        kinds = [e["kind"] for e in gs.list_events(gid, limit=10)]
        assert "sprint_nudge" in kinds
        # 连点第二次：p0 已排 → 顺延到 p1（同样立即排入，不双发 p0）
        r2 = client.post(f"/api/goals/{gid}/sprint/nudge")
        assert r2.status_code == 200 and r2.json()["phase"] == 1
        # 相位排尽 → 409 exhausted
        client.post(f"/api/goals/{gid}/sprint/nudge")
        r4 = client.post(f"/api/goals/{gid}/sprint/nudge")
        assert r4.status_code == 409
    finally:
        svc, orig = client._restore
        svc.get_configured_store = orig


def test_nudge_safety_gates(monkeypatch):
    from src.contacts.care_schedule import CareScheduleStore
    # 人审会话 → 409（fail-closed：读不到档位同理）
    gs, cs = GoalStore(":memory:"), CareScheduleStore(":memory:")
    g = _sprint_goal(gs, chat="n2")
    gid = str(g["goal_id"])
    client = _nudge_client(gs, cs, monkeypatch,
                           inbox=_NudgeInbox(mode="review"))
    try:
        assert client.post(
            f"/api/goals/{gid}/sprint/nudge").status_code == 409
    finally:
        svc, orig = client._restore
        svc.get_configured_store = orig
    # 非冲刺目标 → 400
    gs2, cs2 = GoalStore(":memory:"), CareScheduleStore(":memory:")
    g2 = gs2.create_goal(
        conversation_id="telegram:a1:n3", platform="telegram", account_id="a1",
        chat_key="n3", template="custom", autonomy="auto", deadline_days=14,
        params={"note": "长线"}, now=NOW - 600)
    client2 = _nudge_client(gs2, cs2, monkeypatch, inbox=_NudgeInbox())
    try:
        assert client2.post(
            f"/api/goals/{g2['goal_id']}/sprint/nudge").status_code == 400
    finally:
        svc, orig = client2._restore
        svc.get_configured_store = orig
    # sprint 总闸关 → 409
    gs3, cs3 = GoalStore(":memory:"), CareScheduleStore(":memory:")
    g3 = _sprint_goal(gs3, chat="n4")
    client3 = _nudge_client(gs3, cs3, monkeypatch, inbox=_NudgeInbox(),
                            sprint_enabled=False)
    try:
        assert client3.post(
            f"/api/goals/{g3['goal_id']}/sprint/nudge").status_code == 409
    finally:
        svc, orig = client3._restore
        svc.get_configured_store = orig
    # 危机 block（meta 口径：wellbeing 词表内的负面情绪+高强度只到 soft，
    # block 需危机事件——这里用 meta 只验证「不 block 时放行」的对照面已在
    # 上面用例覆盖；suggest 档 → 409 nudge_not_auto）
    gs4, cs4 = GoalStore(":memory:"), CareScheduleStore(":memory:")
    g4 = _sprint_goal(gs4, chat="n5")
    gs4.update_goal_fields(str(g4["goal_id"]), autonomy="suggest")
    client4 = _nudge_client(gs4, cs4, monkeypatch, inbox=_NudgeInbox())
    try:
        assert client4.post(
            f"/api/goals/{g4['goal_id']}/sprint/nudge").status_code == 409
    finally:
        svc, orig = client4._restore
        svc.get_configured_store = orig


def test_for_conversation_attaches_sprint_live(monkeypatch):
    from src.contacts.care_schedule import CareScheduleStore
    gs, cs = GoalStore(":memory:"), CareScheduleStore(":memory:")
    _sprint_goal(gs, chat="n6", started_ago=600)
    client = _nudge_client(gs, cs, monkeypatch, inbox=_NudgeInbox())
    try:
        d = client.get(
            "/api/goals/for-conversation?conversation_id=telegram:a1:n6"
        ).json()
        live = (d.get("goal") or {}).get("sprint_live") or {}
        assert live.get("ticker_on") is True
        assert live.get("nudgeable") is True
        assert float(live.get("next_phase_ts") or 0) > NOW   # 45% 相位还在未来
    finally:
        svc, orig = client._restore
        svc.get_configured_store = orig


def test_route_manual_done_resumes_linked():
    gs = GoalStore(":memory:")
    longg = gs.create_goal(
        conversation_id="telegram:a1:u7", platform="telegram", account_id="a1",
        chat_key="u7", template="custom", autonomy="auto", deadline_days=14,
        params={"note": "长线"}, now=NOW - 86400)
    lid = str(longg["goal_id"])
    gs.update_goal_fields(lid, status="paused")
    sprint = _sprint_goal(gs, chat="u7",
                          extra_params={"resume_goal_id": lid})
    gid = str(sprint["goal_id"])
    client = _client(gs)
    try:
        r = client.post(f"/api/goals/{gid}/status", json={"action": "done"})
        assert r.status_code == 200
        assert gs.get_goal(lid)["status"] == "active"
    finally:
        svc, orig = client._restore
        svc.get_configured_store = orig
