"""营销目标后台 API（goal_routes，/api/goals*）门禁。

自建 FastAPI app + 假 auth dep（session 直接塞 scope）+ SimpleNamespace 假
config_manager（.config dict + .config_path），不起生产服务。

覆盖：disabled 全端点 403；templates 形状；create（合法 / 未知模板 400 /
缺会话 400 / conversation_id 自动拆三元组 / 活跃上限 409 + 硬顶 3）；
for-conversation（活跃视图含 today / 无目标 goal=None / 终态目标进 last）；
list + summary；detail 404 与正常（actions/events）；update（title/autonomy/
deadline_days、空字段 400、非法 autonomy 单独提交按实际行为 500）；改期限
护栏（P25：过短=立即过期 400 + 文案带最小天数 / 「今天收口」边界合法 /
终态 409 且不闸其他字段 / 事件台账记差值 / active 返回带 today 的 refreshed
视图 / planned 拍当场重排、adopted/rejected 拍原样保留）；status
转移全表 + 非法转移 400；viewer 只读三写端点 403。

goals.db_path 用 ":memory:"（进程单例）→ autouse fixture 每测复位防串味。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from starlette.testclient import TestClient

from src.companion.goals.store import reset_goal_store
from src.web.routes.goal_routes import register_goal_routes

CONV = "telegram:a1:100"


@pytest.fixture(autouse=True)
def _hermetic_goal_env(monkeypatch):
    import src.integrations.protocol_bridge as pb
    import src.utils.companion_context as cc

    monkeypatch.setattr(cc, "_REL_PROVIDERS", {})
    monkeypatch.setattr(pb, "_inbox_store_getter", None)
    reset_goal_store()
    yield
    reset_goal_store()


def _build_client(enabled=True, **goals_extra):
    """自建 app；返回 (client, sess)——改 sess["role"] 可切 viewer。"""
    goals = {"enabled": enabled, "db_path": ":memory:"}
    goals.update(goals_extra)
    cfg = {"companion": {"goals": goals}}
    sess = {"role": "", "user": "tester"}
    app = FastAPI()

    def auth_dep(request: Request) -> None:
        request.scope["session"] = dict(sess)

    register_goal_routes(
        app, auth_dep, SimpleNamespace(config=cfg, config_path=None))
    return TestClient(app), sess


def _create(client, conv=CONV, template="conversion_unlock", **extra):
    body = {"template": template, "conversation_id": conv}
    body.update(extra)
    return client.post("/api/goals", json=body)


def _backdate_start(gid: str, days: float) -> None:
    """把目标开始时刻往回拨 ``days`` 天（模拟已进行 N 天；deadline_ts 不动）。

    start_ts 不在 update_goal_fields 白名单（生产不该改开始时刻），测试走
    裸 SQL 直改单例库。"""
    import time as _t

    from src.companion.goals.store import get_goal_store
    store = get_goal_store()
    store._conn.execute(
        "UPDATE goals SET start_ts = ? WHERE goal_id = ?",
        (_t.time() - days * 86400.0, gid))
    store._conn.commit()


# ── disabled 门 ─────────────────────────────────────────────────────────────

def test_disabled_all_endpoints_403():
    client, _ = _build_client(enabled=False)
    assert client.get("/api/goals/templates").status_code == 403
    assert client.get(
        "/api/goals/for-conversation?conversation_id=x").status_code == 403
    assert client.get("/api/goals").status_code == 403
    assert client.get("/api/goals/some-id").status_code == 403
    assert client.post("/api/goals", json={}).status_code == 403
    assert client.post("/api/goals/some-id/update", json={}).status_code == 403
    assert client.post("/api/goals/some-id/status", json={}).status_code == 403
    assert client.get("/api/goals/report").status_code == 403
    assert client.post("/api/goals/batch", json={}).status_code == 403
    assert client.post("/api/goals/some-id/beat/feedback",
                       json={}).status_code == 403
    assert client.get("/api/goals/profile?platform=x&chat_key=y"
                      ).status_code == 403
    assert client.post("/api/goals/profile", json={}).status_code == 403


# ── templates ───────────────────────────────────────────────────────────────

def test_templates_shape():
    client, _ = _build_client()
    r = client.get("/api/goals/templates")
    assert r.status_code == 200
    d = r.json()
    assert len(d["templates"]) == 9
    assert {t["id"] for t in d["templates"]} == {
        "conversion_unlock", "conversion_subscribe", "relationship_stage",
        "relationship_intimacy", "engagement_reactivate",
        "acquire_and_convert", "retention_expand", "profile_discovery",
        "custom"}
    assert d["autonomy_levels"] == ["observe", "suggest", "auto"]
    assert set(d["statuses"]) == {"active", "paused", "done", "failed",
                                  "expired", "cancelled"}
    for t in d["templates"]:
        assert "intents" not in t                 # 内部意图池不外泄
        assert len(t["milestones"]) in (4, 5)     # 获客转化=5 段弧线
        assert "sprint_ok" in t


# ── create ──────────────────────────────────────────────────────────────────

class TestCreate:
    def test_create_ok_with_refreshed_view(self):
        client, _ = _build_client()
        r = _create(client, params={"item_id": "bazi_reading",
                                    "item_label": "八字详批"})
        assert r.status_code == 200
        d = r.json()
        assert d["ok"] is True
        g = d["goal"]
        assert g["goal_id"] and g["status"] == "active"
        assert g["conversation_id"] == CONV
        assert g["platform"] == "telegram"        # conversation_id 自动拆三元组
        assert g["account_id"] == "a1" and g["chat_key"] == "100"
        assert g["template"] == "conversion_unlock"
        assert g["milestone_idx"] == 0
        # settle-on-read：建目标即出当日拍（planned，路由不消耗）
        assert g["today"] is not None and g["today"]["intent"]
        assert g["today"]["status"] == "planned"

    def test_create_default_autonomy_is_auto(self):
        """缺省档＝auto（2026-08-12 运营方针「全自动为主」）：不带 autonomy
        建目标落 auto——observe 只能是显式选择，绝不当缺省。"""
        client, _ = _build_client()
        g = _create(client).json()["goal"]
        assert g["autonomy"] == "auto"

    def test_create_splits_chat_key_with_colon(self):
        client, _ = _build_client()
        g = _create(client, conv="telegram:a1:room:5").json()["goal"]
        assert g["platform"] == "telegram" and g["account_id"] == "a1"
        assert g["chat_key"] == "room:5"          # chat_key 可含冒号（split 限 2 刀）

    def test_create_composes_conversation_id_from_parts(self):
        client, _ = _build_client()
        r = client.post("/api/goals", json={
            "template": "custom", "platform": "telegram",
            "account_id": "a9", "chat_key": "900"})
        assert r.status_code == 200
        assert r.json()["goal"]["conversation_id"] == "telegram:a9:900"

    def test_create_session_pace_stores_subday_deadline(self):
        client, _ = _build_client()
        r = _create(client, template="custom", pace="session",
                    deadline_days=60 / 1440.0)
        assert r.status_code == 200
        g = r.json()["goal"]
        assert g["pace"] == "session"
        assert g["params"]["pace"] == "session"
        span = float(g["deadline_ts"]) - float(g["start_ts"])
        assert abs(span - 3600.0) < 2.0          # 60 分钟，不被抬到 1 天
        assert g["today"] and str(g["today"]["day"]).startswith("s:")

    def test_create_session_rejected_on_relationship_template(self):
        client, _ = _build_client()
        r = _create(client, template="relationship_intimacy", pace="session",
                    deadline_days=60 / 1440.0)
        assert r.status_code == 400

    def test_create_without_pace_autofalls_by_deadline_span(self):
        """#65 C1（2026-09-04）：客户端没给 pace 的子日期限 → 按跨度自动落档
        （≤2.5h session / <20h today），子日跨度原样入库——旧行为「一律
        natural 再夹成 1 天」正是 60 分钟目标在库里永远是 1 天 natural
        的入口失真。8h 落 today 而非 session：session 夹限 15–120 分。"""
        client, _ = _build_client()
        r = _create(client, deadline_days=0.04)      # conversion_unlock ∈ SPRINT_OK
        assert r.status_code == 200
        g = r.json()["goal"]
        assert g["pace"] == "session" and g["params"]["pace"] == "session"
        span = float(g["deadline_ts"]) - float(g["start_ts"])
        assert abs(span - 0.04 * 86400.0) < 2.0
        # 8 小时 → today
        r2 = _create(client, conv="telegram:a1:c8h", template="custom",
                     deadline_days=8 / 24.0)
        assert r2.json()["goal"]["pace"] == "today"
        # 白名单外模板（关系）子日期限仍走 natural（夹成 1 天），不放开
        r3 = _create(client, conv="telegram:a1:crel",
                     template="relationship_intimacy", deadline_days=0.04)
        assert r3.status_code == 200
        assert r3.json()["goal"]["pace"] == "natural"
        assert r3.json()["goal"]["total_days"] == 1

    def test_create_explicit_natural_still_clamps_below_one_day(self):
        """显式 pace=natural 是坐席/客户端的明示选择 → 仍按 natural 夹到 ≥1 天。"""
        client, _ = _build_client()
        r = _create(client, pace="natural", deadline_days=0.04)
        assert r.status_code == 200
        g = r.json()["goal"]
        assert g["pace"] == "natural"
        assert g["total_days"] == 1

    def test_create_sprint_observe_coerces_to_suggest(self):
        client, _ = _build_client()
        r = _create(client, template="custom", pace="session",
                    autonomy="observe", deadline_days=60 / 1440.0)
        assert r.status_code == 200
        assert r.json()["goal"]["autonomy"] == "suggest"

    def test_beat_feedback_honors_session_slot_day(self):
        client, _ = _build_client()
        g = _create(client, template="custom", pace="session",
                    deadline_days=60 / 1440.0).json()["goal"]
        day = g["today"]["day"]
        r = client.post(f"/api/goals/{g['goal_id']}/beat/feedback",
                        json={"verdict": "adopt", "day": day})
        assert r.status_code == 200
        assert r.json()["goal"]["today"]["detail"] == "adopted"
        assert r.json()["goal"]["today"]["day"] == day

    def test_unknown_template_400(self):
        client, _ = _build_client()
        assert _create(client, template="no_such").status_code == 400

    def test_missing_conversation_400(self):
        client, _ = _build_client()
        r = client.post("/api/goals", json={"template": "custom"})
        assert r.status_code == 400

    def test_active_cap_409(self):
        client, _ = _build_client(max_active_per_conversation=1)
        assert _create(client).status_code == 200
        assert _create(client).status_code == 409          # 同会话第二个 → 顶
        assert _create(client, conv="telegram:a1:200").status_code == 200

    def test_cap_hard_ceiling_is_three(self):
        client, _ = _build_client(max_active_per_conversation=99)
        for _i in range(3):                                # 配置超发被硬顶夹到 3
            assert _create(client).status_code == 200
        assert _create(client).status_code == 409


# ── for-conversation ────────────────────────────────────────────────────────

class TestForConversation:
    def test_requires_conversation_id(self):
        client, _ = _build_client()
        assert client.get("/api/goals/for-conversation").status_code == 400

    def test_no_goal_returns_none(self):
        client, _ = _build_client()
        r = client.get(f"/api/goals/for-conversation?conversation_id={CONV}")
        assert r.status_code == 200
        assert r.json() == {"goal": None, "last": None}

    def test_active_goal_view_with_today(self):
        client, _ = _build_client()
        _create(client)
        d = client.get(
            f"/api/goals/for-conversation?conversation_id={CONV}").json()
        assert d["last"] is None
        g = d["goal"]
        assert g["conversation_id"] == CONV and g["status"] == "active"
        assert g["today"] and g["today"]["intent"]
        assert g["milestone_label"]

    def test_terminal_goal_shows_as_last(self):
        client, _ = _build_client()
        gid = _create(client).json()["goal"]["goal_id"]
        assert client.post(f"/api/goals/{gid}/status",
                           json={"action": "cancel"}).status_code == 200
        d = client.get(
            f"/api/goals/for-conversation?conversation_id={CONV}").json()
        assert d["goal"] is None
        assert d["last"]["goal_id"] == gid
        assert d["last"]["status"] == "cancelled"


# ── list + summary ──────────────────────────────────────────────────────────

def test_list_and_summary():
    client, _ = _build_client()
    _create(client)
    _create(client, conv="telegram:a1:200", template="custom")
    r = client.get("/api/goals")
    assert r.status_code == 200
    d = r.json()
    assert len(d["goals"]) == 2
    assert all(g["today"] for g in d["goals"])        # 活跃目标逐条 settle 出今日拍
    assert d["summary"]["total"] == 2
    assert d["summary"]["by_status"] == {"active": 2}
    assert d["summary"]["active_by_template"] == {
        "conversion_unlock": 1, "custom": 1}
    # 状态过滤
    assert len(client.get("/api/goals?status=paused").json()["goals"]) == 0
    assert len(client.get("/api/goals?status=active").json()["goals"]) == 2


# ── detail ──────────────────────────────────────────────────────────────────

class TestDetail:
    def test_detail_404(self):
        client, _ = _build_client()
        assert client.get("/api/goals/ghost").status_code == 404

    def test_detail_with_actions_and_events(self):
        client, _ = _build_client()
        gid = _create(client).json()["goal"]["goal_id"]
        d = client.get(f"/api/goals/{gid}").json()
        assert d["goal"]["goal_id"] == gid
        assert len(d["actions"]) >= 1                 # 今日拍已在时间线上
        assert "created" in {e["kind"] for e in d["events"]}


# ── update ──────────────────────────────────────────────────────────────────

class TestUpdate:
    def test_update_title_and_autonomy(self):
        client, _ = _build_client()
        gid = _create(client).json()["goal"]["goal_id"]
        r = client.post(f"/api/goals/{gid}/update",
                        json={"title": "改标题", "autonomy": "auto"})
        assert r.status_code == 200
        g = r.json()["goal"]
        assert g["title"] == "改标题" and g["autonomy"] == "auto"

    def test_update_deadline_days_recomputes_total(self):
        client, _ = _build_client()
        gid = _create(client).json()["goal"]["goal_id"]
        r = client.post(f"/api/goals/{gid}/update", json={"deadline_days": 30})
        assert r.status_code == 200
        assert r.json()["goal"]["total_days"] == 30

    def test_update_params_merges(self):
        client, _ = _build_client()
        gid = _create(client, params={"item_id": "a"}).json()["goal"]["goal_id"]
        r = client.post(f"/api/goals/{gid}/update",
                        json={"params": {"item_label": "详批"}})
        assert r.status_code == 200
        assert r.json()["goal"]["params"] == {
            "item_id": "a", "item_label": "详批", "pace": "natural"}

    def test_update_empty_payload_400(self):
        client, _ = _build_client()
        gid = _create(client).json()["goal"]["goal_id"]
        assert client.post(f"/api/goals/{gid}/update", json={}).status_code == 400

    def test_update_invalid_autonomy_alone_is_500_by_design_gap(self):
        # 按实际行为：路由把 autonomy 传给 store，store 白名单静默丢掉非法值 →
        # 无字段可更新返回 False → 路由报 update_failed(500)（而非 400）。
        # 疑似 src 侧小瑕疵，仅记录现状防回归漂移。
        client, _ = _build_client()
        gid = _create(client).json()["goal"]["goal_id"]
        r = client.post(f"/api/goals/{gid}/update", json={"autonomy": "bogus"})
        assert r.status_code == 500

    def test_update_invalid_autonomy_mixed_with_title_applies_title(self):
        client, _ = _build_client()
        gid = _create(client).json()["goal"]["goal_id"]
        r = client.post(f"/api/goals/{gid}/update",
                        json={"autonomy": "bogus", "title": "T2"})
        assert r.status_code == 200
        g = r.json()["goal"]
        # 非法值被忽略，保持创建时的缺省档 auto（2026-08-12 起缺省=auto）
        assert g["title"] == "T2" and g["autonomy"] == "auto"

    def test_update_404(self):
        client, _ = _build_client()
        assert client.post("/api/goals/ghost/update",
                           json={"title": "x"}).status_code == 404

    # ── 改期限（调节奏）护栏 + 当日拍重排（P25） ─────────────────────────────

    def test_update_deadline_shorter_than_elapsed_400(self):
        # 已进行 2.5 天还改成 2 天 → 新截止时间落在过去＝立即判死不是加速，拦下
        client, _ = _build_client()
        gid = _create(client).json()["goal"]["goal_id"]
        _backdate_start(gid, 2.5)
        r = client.post(f"/api/goals/{gid}/update", json={"deadline_days": 2})
        assert r.status_code == 400
        assert "3" in r.json()["detail"]        # 文案里给出最小可改天数（第3天）

    def test_update_deadline_min_edge_ok(self):
        # 第 3 天改成 3 天（=「今天收口」chip 的语义）合法：截止时间仍在未来
        client, _ = _build_client()
        gid = _create(client).json()["goal"]["goal_id"]
        _backdate_start(gid, 2.5)
        r = client.post(f"/api/goals/{gid}/update", json={"deadline_days": 3})
        assert r.status_code == 200
        g = r.json()["goal"]
        assert g["total_days"] == 3 and g["day_index"] == 3
        assert g["status"] == "active"          # 没被顺手结算成过期

    def test_update_deadline_terminal_409(self):
        # 终态目标的期限是死数据，改了也不会被结算读到——静默接受＝误导
        client, _ = _build_client()
        gid = _create(client).json()["goal"]["goal_id"]
        client.post(f"/api/goals/{gid}/status", json={"action": "cancel"})
        r = client.post(f"/api/goals/{gid}/update", json={"deadline_days": 7})
        assert r.status_code == 409

    def test_update_terminal_title_still_editable(self):
        # 终态护栏只闸期限：其他字段（标题等）照旧可改，最小干预
        client, _ = _build_client()
        gid = _create(client).json()["goal"]["goal_id"]
        client.post(f"/api/goals/{gid}/status", json={"action": "cancel"})
        r = client.post(f"/api/goals/{gid}/update", json={"title": "归档名"})
        assert r.status_code == 200 and r.json()["goal"]["title"] == "归档名"

    def test_update_deadline_event_records_old_new(self):
        # 事件台账记差值（deadline_days:14->30）——复盘时间线能看出节奏为何变
        client, _ = _build_client()
        gid = _create(client).json()["goal"]["goal_id"]   # 模板默认 14 天
        client.post(f"/api/goals/{gid}/update", json={"deadline_days": 30})
        events = client.get(f"/api/goals/{gid}").json()["events"]
        rows = [e for e in events if e.get("kind") == "updated"]
        assert rows and "deadline_days:14->30" in rows[0]["detail"]

    def test_update_returns_refreshed_view_with_today(self):
        # active 目标的 update 返回 settle 后完整视图（带 today），与 detail 同口径
        client, _ = _build_client()
        gid = _create(client).json()["goal"]["goal_id"]
        r = client.post(f"/api/goals/{gid}/update", json={"title": "改标题"})
        assert r.status_code == 200
        today = r.json()["goal"]["today"]
        assert today and today.get("intent")

    def test_update_deadline_replans_planned_beat(self):
        # planned 且无坐席反馈的今日拍 → 改期限当场按新节奏重排（action_id 换新）
        client, _ = _build_client()
        created = _create(client).json()["goal"]
        gid = created["goal_id"]
        old_aid = created["today"]["action_id"]
        r = client.post(f"/api/goals/{gid}/update", json={"deadline_days": 30})
        today = r.json()["goal"]["today"]
        assert today and today["action_id"] and today["action_id"] != old_aid
        assert today["status"] == "planned"

    def test_update_deadline_keeps_adopted_beat(self):
        # 坐席已采纳＝人的决定：改期限不得抹掉（action_id 原样保留）
        client, _ = _build_client()
        created = _create(client).json()["goal"]
        gid = created["goal_id"]
        aid = created["today"]["action_id"]
        client.post(f"/api/goals/{gid}/beat/feedback", json={"verdict": "adopt"})
        r = client.post(f"/api/goals/{gid}/update", json={"deadline_days": 30})
        today = r.json()["goal"]["today"]
        assert today["action_id"] == aid and today["detail"] == "adopted"

    def test_update_deadline_keeps_rejected_beat(self):
        # 坐席已驳回（今日不推）不得被一次改期限悄悄复活
        client, _ = _build_client()
        created = _create(client).json()["goal"]
        gid = created["goal_id"]
        aid = created["today"]["action_id"]
        client.post(f"/api/goals/{gid}/beat/feedback", json={"verdict": "reject"})
        r = client.post(f"/api/goals/{gid}/update", json={"deadline_days": 30})
        today = r.json()["goal"]["today"]
        assert today["action_id"] == aid
        assert today["status"] == "skipped"

    def test_update_deadline_records_direction_stats(self):
        # P1 观测反哺：加急/延期方向计数（等值改动不计——那不是节奏信号）
        from src.companion.goals.stats import get_goal_stats
        client, _ = _build_client()
        gid = _create(client).json()["goal"]["goal_id"]   # 模板默认 14 天
        s = get_goal_stats()
        base_s, base_e = s.deadline_shorten, s.deadline_extend
        client.post(f"/api/goals/{gid}/update", json={"deadline_days": 7})
        assert (s.deadline_shorten, s.deadline_extend) == (base_s + 1, base_e)
        client.post(f"/api/goals/{gid}/update", json={"deadline_days": 30})
        assert (s.deadline_shorten, s.deadline_extend) == (base_s + 1, base_e + 1)
        client.post(f"/api/goals/{gid}/update", json={"deadline_days": 30})
        assert (s.deadline_shorten, s.deadline_extend) == (base_s + 1, base_e + 1)
        dump = s.dump()
        assert dump["deadline_edits"]["shorten"] >= 1
        assert 'goals_deadline_edits_total{direction="shorten"}' in s.dump_prom()


# ── status 生命周期 ─────────────────────────────────────────────────────────

class TestStatusTransitions:
    def _status(self, client, gid, action):
        return client.post(f"/api/goals/{gid}/status", json={"action": action})

    def test_pause_resume_cancel_chain(self):
        client, _ = _build_client()
        gid = _create(client).json()["goal"]["goal_id"]
        r = self._status(client, gid, "pause")
        assert r.status_code == 200 and r.json()["goal"]["status"] == "paused"
        r = self._status(client, gid, "resume")
        assert r.status_code == 200 and r.json()["goal"]["status"] == "active"
        r = self._status(client, gid, "pause")
        assert r.json()["goal"]["status"] == "paused"
        r = self._status(client, gid, "cancel")           # paused → cancel
        assert r.status_code == 200 and r.json()["goal"]["status"] == "cancelled"

    def test_active_cancel_direct(self):
        client, _ = _build_client()
        gid = _create(client).json()["goal"]["goal_id"]
        r = self._status(client, gid, "cancel")           # active → cancel
        assert r.status_code == 200 and r.json()["goal"]["status"] == "cancelled"

    def test_illegal_transitions_400(self):
        client, _ = _build_client()
        gid = _create(client).json()["goal"]["goal_id"]
        assert self._status(client, gid, "resume").status_code == 400   # active
        assert self._status(client, gid, "boom").status_code == 400     # 未知动作
        assert self._status(client, gid, "").status_code == 400         # 缺动作
        assert self._status(client, gid, "cancel").status_code == 200
        assert self._status(client, gid, "pause").status_code == 400    # 终态不可再操作
        assert self._status(client, gid, "cancel").status_code == 400

    def test_status_404(self):
        client, _ = _build_client()
        assert self._status(client, "ghost", "pause").status_code == 404


# ── P2：beat feedback（坐席采纳/驳回今日拍）─────────────────────────────────

class TestBeatFeedback:
    def _fb(self, client, gid, verdict, **extra):
        body = {"verdict": verdict}
        body.update(extra)
        return client.post(f"/api/goals/{gid}/beat/feedback", json=body)

    def test_adopt_marks_detail_and_event(self):
        client, _ = _build_client()
        gid = _create(client).json()["goal"]["goal_id"]
        r = self._fb(client, gid, "adopt")
        assert r.status_code == 200
        g = r.json()["goal"]
        assert g["today"]["detail"] == "adopted"
        assert g["today"]["status"] == "planned"      # 状态不动，后续照常消耗
        d = client.get(f"/api/goals/{gid}").json()
        assert "beat_adopted" in {e["kind"] for e in d["events"]}

    def test_reject_skips_today_and_blocks_injection(self):
        client, _ = _build_client()
        gid = _create(client).json()["goal"]["goal_id"]
        r = self._fb(client, gid, "reject", reason="too_pushy")
        assert r.status_code == 200
        g = r.json()["goal"]
        assert g["today"]["status"] == "skipped"
        assert g["today"]["detail"] == "rejected:too_pushy"
        d = client.get(f"/api/goals/{gid}").json()
        assert "beat_rejected" in {e["kind"] for e in d["events"]}
        # 驳回日注入口彻底关闭（服务层同店同拍）
        from types import SimpleNamespace as NS

        from src.companion.goals.service import build_block_for_chat
        cfg = NS(config={"companion": {"goals": {
            "enabled": True, "db_path": ":memory:"}}}, config_path=None)
        assert build_block_for_chat(
            cfg, platform="telegram", chat_key="100", account_id="a1",
            conversation_id=CONV) is None

    def test_verdict_validation_400(self):
        client, _ = _build_client()
        gid = _create(client).json()["goal"]["goal_id"]
        assert self._fb(client, gid, "meh").status_code == 400
        assert client.post(f"/api/goals/{gid}/beat/feedback",
                           json={}).status_code == 400

    def test_not_found_404_and_not_active_409(self):
        client, _ = _build_client()
        assert self._fb(client, "ghost", "adopt").status_code == 404
        gid = _create(client).json()["goal"]["goal_id"]
        client.post(f"/api/goals/{gid}/status", json={"action": "pause"})
        assert self._fb(client, gid, "adopt").status_code == 409

    def test_no_beat_today_409(self):
        client, _ = _build_client()
        gid = _create(client).json()["goal"]["goal_id"]
        from src.companion.goals.store import peek_goal_store
        store = peek_goal_store()
        store._conn.execute("DELETE FROM goal_actions")   # 模拟 hold 日无拍
        store._conn.commit()
        assert self._fb(client, gid, "reject").status_code == 409

    def test_viewer_403(self):
        client, sess = _build_client()
        gid = _create(client).json()["goal"]["goal_id"]
        sess["role"] = "viewer"
        assert self._fb(client, gid, "adopt").status_code == 403


# ── P2：batch campaign ──────────────────────────────────────────────────────

class TestBatch:
    def test_batch_creates_and_skips_with_reasons(self):
        client, _ = _build_client()
        _create(client)                     # CONV 已有活跃目标 → active_limit
        r = client.post("/api/goals/batch", json={
            "template": "relationship_intimacy",
            "targets": [
                CONV,
                "telegram:a1:200",
                {"platform": "line", "account_id": "L1", "chat_key": "u9"},
                "telegram:a1:200",          # 同批重复
                "badformat",
            ],
            "autonomy": "suggest", "priority": 2, "deadline_days": 21,
        })
        assert r.status_code == 200
        d = r.json()
        assert d["requested"] == 5 and d["created"] == 2
        reasons = {s["target"]: s["reason"] for s in d["skipped"]}
        assert reasons[CONV] == "active_limit"
        assert reasons["telegram:a1:200"] == "duplicate"
        assert reasons["badformat"] == "bad_target"
        g = client.get("/api/goals/for-conversation"
                       "?conversation_id=telegram:a1:200").json()["goal"]
        assert g["template"] == "relationship_intimacy"
        assert g["priority"] == 2 and g["autonomy"] == "suggest"
        assert g["total_days"] == 21
        assert client.get("/api/goals/for-conversation"
                          "?conversation_id=line:L1:u9").json()["goal"]

    def test_batch_validation_400(self):
        client, _ = _build_client()
        assert client.post("/api/goals/batch", json={
            "template": "no_such", "targets": [CONV]}).status_code == 400
        assert client.post("/api/goals/batch", json={
            "template": "custom"}).status_code == 400
        assert client.post("/api/goals/batch", json={
            "template": "custom", "targets": []}).status_code == 400

    def test_batch_per_call_cap_400(self):
        client, _ = _build_client(batch={"max_per_call": 2})
        r = client.post("/api/goals/batch", json={
            "template": "custom",
            "targets": ["telegram:a1:1", "telegram:a1:2", "telegram:a1:3"]})
        assert r.status_code == 400

    def test_batch_viewer_403(self):
        client, sess = _build_client()
        sess["role"] = "viewer"
        assert client.post("/api/goals/batch", json={
            "template": "custom", "targets": [CONV]}).status_code == 403


# ── P2：report（结果闭环读数面）──────────────────────────────────────────────

def test_report_shape_and_window_clamp():
    client, _ = _build_client()
    _create(client)                                       # active → active_now
    g2 = _create(client, conv="telegram:a1:200",
                 template="custom").json()["goal"]["goal_id"]
    client.post(f"/api/goals/{g2}/status", json={"action": "cancel"})
    r = client.get("/api/goals/report?days=7")
    assert r.status_code == 200
    d = r.json()
    assert d["window_days"] == 7
    assert d["active_now"] == 1
    assert d["totals"]["cancelled"] == 1 and d["totals"]["n"] == 1
    assert d["totals"]["done_rate"] == 0.0                # cancelled 不进分母
    assert d["by_template"]["custom"]["cancelled"] == 1
    assert d["beats"]["planned"] >= 1                     # 建目标即出当日拍
    assert d["recent"][0]["goal_id"] == g2
    assert d["feedback"] == {"adopt": 0, "reject": 0}
    assert client.get(
        "/api/goals/report?days=9999").json()["window_days"] == 180
    assert client.get(
        "/api/goals/report?days=0").json()["window_days"] == 30   # 0=用默认


def test_report_deadline_edits_end_to_end():
    """P2 校准闭环端到端：改期限（update 落事件）→ 标成交 → report 三队列。"""
    client, _ = _build_client()
    gid = _create(client).json()["goal"]["goal_id"]
    client.post(f"/api/goals/{gid}/update", json={"deadline_days": 7})   # 加急
    client.post(f"/api/goals/{gid}/status", json={"action": "done"})
    g2 = _create(client, conv="telegram:a1:210").json()["goal"]["goal_id"]
    client.post(f"/api/goals/{g2}/status", json={"action": "done"})      # 未调基线
    de = client.get("/api/goals/report?days=30").json()["deadline_edits"]
    assert de["totals"]["edited_n"] == 1
    bt = de["by_template"]["conversion_unlock"]
    assert bt["shortened"]["n"] == 1 and bt["shortened"]["done"] == 1
    assert bt["shortened"]["done_rate"] == 1.0
    assert bt["extended"]["n"] == 0
    assert bt["unedited"]["n"] == 1 and bt["unedited"]["done"] == 1


# ── P1：客户画像卡 ──────────────────────────────────────────────────────────

class TestProfile:
    def test_get_empty_profile_full_slot_skeleton(self):
        from src.companion.goals.profile_slots import SLOTS
        client, _ = _build_client()
        r = client.get("/api/goals/profile?platform=telegram&chat_key=u1")
        assert r.status_code == 200
        d = r.json()
        # 4 relation + 6 bant + 1 lifecycle（churn_reason）——随注册表走
        assert len(d["slots"]) == len(SLOTS)
        assert all(s["value"] == "" for s in d["slots"])
        assert d["fill"]["bant"] == 0.0
        assert len(d["missing_bant"]) == 6

    def test_post_then_get_roundtrip_conversation_id(self):
        client, _ = _build_client()
        r = client.post("/api/goals/profile", json={
            "conversation_id": "telegram:a1:u2",
            "fields": {"need": "获客难", "budget": "500刀", "bogus": "x"}})
        assert r.status_code == 200
        assert r.json()["fill"]["bant"] > 0
        # POST 返回与 GET 同形的完整视图（前端保存后免二次拉取）
        assert r.json()["ok"] is True
        from src.companion.goals.profile_slots import SLOTS
        assert len(r.json()["slots"]) == len(SLOTS)
        d = client.get("/api/goals/profile"
                       "?conversation_id=telegram:a1:u2").json()
        vals = {s["key"]: s for s in d["slots"]}
        assert vals["need"]["value"] == "获客难"
        assert vals["need"]["src"] == "agent"
        assert "bogus" not in vals                   # 未知槽位键被忽略
        assert "need" not in d["missing_bant"]

    def test_post_clears_with_empty_string(self):
        client, _ = _build_client()
        client.post("/api/goals/profile", json={
            "platform": "telegram", "chat_key": "u3",
            "fields": {"budget": "500刀"}})
        client.post("/api/goals/profile", json={
            "platform": "telegram", "chat_key": "u3",
            "fields": {"budget": ""}})
        d = client.get(
            "/api/goals/profile?platform=telegram&chat_key=u3").json()
        assert {s["key"]: s["value"] for s in d["slots"]}["budget"] == ""

    def test_profile_validation_and_viewer(self):
        client, sess = _build_client()
        assert client.get("/api/goals/profile").status_code == 400
        assert client.post("/api/goals/profile", json={
            "platform": "telegram", "chat_key": "u4"}).status_code == 400
        sess["role"] = "viewer"
        assert client.post("/api/goals/profile", json={
            "platform": "telegram", "chat_key": "u4",
            "fields": {"need": "x"}}).status_code == 403
        # viewer 读画像照常
        assert client.get(
            "/api/goals/profile?platform=telegram&chat_key=u4"
        ).status_code == 200


# ── viewer 只读 ─────────────────────────────────────────────────────────────

def test_viewer_write_endpoints_403_reads_ok():
    client, sess = _build_client()
    gid = _create(client).json()["goal"]["goal_id"]       # 先以 master 建目标

    sess["role"] = "viewer"
    r = _create(client, conv="telegram:a1:300")
    assert r.status_code == 403 and "只读" in r.json()["detail"]
    assert client.post(f"/api/goals/{gid}/update",
                       json={"title": "x"}).status_code == 403
    assert client.post(f"/api/goals/{gid}/status",
                       json={"action": "pause"}).status_code == 403
    # 读端点对 viewer 照常开放
    assert client.get("/api/goals/templates").status_code == 200
    assert client.get("/api/goals").status_code == 200
    assert client.get(
        f"/api/goals/for-conversation?conversation_id={CONV}").status_code == 200
    assert client.get(f"/api/goals/{gid}").status_code == 200


# ── P17：反馈撤销（verdict=undo）─────────────────────────────────────────────

def _events(client, gid, kind):
    d = client.get(f"/api/goals/{gid}").json()
    return [e for e in d["events"] if e["kind"] == kind]


def _today_action(gid):
    from src.companion.goals.planner import day_key
    from src.companion.goals.store import peek_goal_store
    return peek_goal_store().get_action(gid, day_key())


class TestBeatFeedbackUndo:
    def _fb(self, client, gid, verdict, **extra):
        body = {"verdict": verdict}
        body.update(extra)
        return client.post(f"/api/goals/{gid}/beat/feedback", json=body)

    def test_undo_reverts_reject_and_reopens_injection(self):
        client, _ = _build_client()
        gid = _create(client).json()["goal"]["goal_id"]
        assert self._fb(client, gid, "reject",
                        reason="too_pushy").status_code == 200
        r = self._fb(client, gid, "undo", reason="misclick")
        assert r.status_code == 200
        g = r.json()["goal"]
        assert g["today"]["status"] == "planned"     # 拍打回可用态
        assert g["today"]["detail"] == ""            # 驳回标记清干净
        assert len(_events(client, gid, "beat_reject_undone")) == 1
        # 驳回本身留在台账里（审计只增不删；退避补偿靠相减不靠删行）
        assert len(_events(client, gid, "beat_rejected")) == 1
        # 当天注入口随之放开（服务层 skipped/blocked 闸门读同一张拍）
        from types import SimpleNamespace as NS

        from src.companion.goals.service import build_block_for_chat
        cfg = NS(config={"companion": {"goals": {
            "enabled": True, "db_path": ":memory:"}}}, config_path=None)
        assert build_block_for_chat(
            cfg, platform="telegram", chat_key="100", account_id="a1",
            conversation_id=CONV)

    def test_undo_of_adopt_clears_mark_only(self):
        client, _ = _build_client()
        gid = _create(client).json()["goal"]["goal_id"]
        assert self._fb(client, gid, "adopt").status_code == 200
        r = self._fb(client, gid, "undo")
        assert r.status_code == 200
        g = r.json()["goal"]
        assert g["today"]["detail"] == "" and g["today"]["status"] == "planned"
        assert len(_events(client, gid, "beat_adopt_undone")) == 1
        assert not _events(client, gid, "beat_reject_undone")

    def test_undo_keeps_consumed_status(self):
        """采纳发生在拍已进入生成之后 → 撤销只清标记，不把 consumed 打回 planned。"""
        client, _ = _build_client()
        gid = _create(client).json()["goal"]["goal_id"]
        from src.companion.goals.store import peek_goal_store
        aid = str(_today_action(gid)["action_id"])
        assert peek_goal_store().mark_action(aid, "consumed", detail="reply")
        assert self._fb(client, gid, "adopt").status_code == 200
        g = self._fb(client, gid, "undo").json()["goal"]
        assert g["today"]["status"] == "consumed" and g["today"]["detail"] == ""

    def test_undo_without_prior_feedback_is_quiet_noop(self):
        client, _ = _build_client()
        gid = _create(client).json()["goal"]["goal_id"]
        r = self._fb(client, gid, "undo")
        assert r.status_code == 200 and r.json()["ok"] is True
        assert r.json()["goal"]["today"]["status"] == "planned"
        assert not _events(client, gid, "beat_reject_undone")
        assert not _events(client, gid, "beat_adopt_undone")

    def test_undo_is_idempotent(self):
        client, _ = _build_client()
        gid = _create(client).json()["goal"]["goal_id"]
        self._fb(client, gid, "reject")
        for _i in range(3):                       # 连点三下撤销
            assert self._fb(client, gid, "undo").status_code == 200
        assert len(_events(client, gid, "beat_reject_undone")) == 1
        assert _today_action(gid)["status"] == "planned"

    def test_undo_shares_existing_gates(self):
        client, sess = _build_client()
        assert self._fb(client, "ghost", "undo").status_code == 404
        gid = _create(client).json()["goal"]["goal_id"]
        from src.companion.goals.store import peek_goal_store
        store = peek_goal_store()
        store._conn.execute("DELETE FROM goal_actions")   # 模拟 hold 日无拍
        store._conn.commit()
        assert self._fb(client, gid, "undo").status_code == 409
        sess["role"] = "viewer"
        assert self._fb(client, gid, "undo").status_code == 403

    def test_legacy_verdicts_unchanged(self):
        """老客户端只会发 adopt/reject —— 新增 undo 后行为逐字如前。"""
        client, _ = _build_client()
        gid = _create(client).json()["goal"]["goal_id"]
        g = self._fb(client, gid, "adopt").json()["goal"]
        assert g["today"]["detail"] == "adopted" and g["today"]["status"] == "planned"
        g = self._fb(client, gid, "reject", reason="r1").json()["goal"]
        assert g["today"]["status"] == "skipped"
        assert g["today"]["detail"] == "rejected:r1"
        assert self._fb(client, gid, "meh").status_code == 400


# ── P17：撤销必须真撤 —— planner 退避补偿 ───────────────────────────────────

class TestRejectUndoCompensatesPlanner:
    def _fb(self, client, gid, verdict):
        return client.post(f"/api/goals/{gid}/beat/feedback",
                           json={"verdict": verdict})

    def _tomorrow_beat(self, client, gid):
        """清掉今日拍模拟「到了明天」，再读一次让 planner 按现有台账重排。"""
        from src.companion.goals.store import peek_goal_store
        store = peek_goal_store()
        store._conn.execute(
            "DELETE FROM goal_actions WHERE goal_id = ?", (gid,))
        store._conn.commit()
        return client.get(f"/api/goals/{gid}").json()["goal"]["today"]

    def _direct_goal(self, client):
        from src.companion.goals.store import peek_goal_store
        gid = _create(client).json()["goal"]["goal_id"]
        assert peek_goal_store().update_goal_fields(gid, milestone_idx=2)
        return gid                                # 转化模板 m2 = direct

    def test_effective_rejects_is_floored_difference(self):
        from src.companion.goals.planner import effective_rejects
        assert effective_rejects(0, 0) == 0
        assert effective_rejects(3) == 3                 # 没撤过 = 原样
        assert effective_rejects(2, 1) == 1
        assert effective_rejects(1, 3) == 0              # 撤多于驳不倒扣成负
        assert effective_rejects(None, None) == 0        # 坏输入按 0

    def test_reject_downgrades_tomorrow(self):
        client, _ = _build_client()
        gid = self._direct_goal(client)
        assert self._tomorrow_beat(client, gid)["push_level"] == "direct"
        assert self._fb(client, gid, "reject").status_code == 200
        assert self._tomorrow_beat(client, gid)["push_level"] == "soft"

    def test_undo_restores_tomorrows_push_level(self):
        client, _ = _build_client()
        gid = self._direct_goal(client)
        assert self._fb(client, gid, "reject").status_code == 200
        assert self._fb(client, gid, "undo").status_code == 200
        # 撤销后明天恢复 direct——否则「撤销」只是擦掉标记的谎话
        assert self._tomorrow_beat(client, gid)["push_level"] == "direct"

    def test_two_rejects_force_care_day(self):
        """基线（不撤销）：连驳两次 → 明天退避成纯陪伴日。"""
        from src.companion.goals.templates import CARE_INTENTS
        client, _ = _build_client()
        gid = self._direct_goal(client)
        assert self._fb(client, gid, "reject").status_code == 200      # 第 1 次
        assert self._tomorrow_beat(client, gid)["push_level"] == "soft"
        assert self._fb(client, gid, "reject").status_code == 200      # 第 2 次
        beat = self._tomorrow_beat(client, gid)
        assert beat["push_level"] == "none"                # ≥2 次 = 退避陪伴日
        assert beat["intent"] in CARE_INTENTS

    def test_undo_walks_care_day_back_to_soft(self):
        """撤掉第 2 次驳回 → 有效驳回 2-1=1 → 明天从陪伴日回到封顶 soft。"""
        client, _ = _build_client()
        gid = self._direct_goal(client)
        assert self._fb(client, gid, "reject").status_code == 200      # 第 1 次
        assert self._tomorrow_beat(client, gid)["push_level"] == "soft"
        assert self._fb(client, gid, "reject").status_code == 200      # 第 2 次
        assert self._fb(client, gid, "undo").status_code == 200        # 撤掉它
        assert self._tomorrow_beat(client, gid)["push_level"] == "soft"


# ── P17：手动标成交的可选归因 meta ──────────────────────────────────────────

class TestWonMeta:
    def _done(self, client, gid, **body):
        payload = {"action": "done"}
        payload.update(body)
        return client.post(f"/api/goals/{gid}/status", json=payload)

    def _won_meta(self, client, gid):
        import json
        rows = _events(client, gid, "won_meta")
        return [json.loads(r["detail"]) for r in rows]

    def test_done_with_meta_persists_attribution(self):
        client, _ = _build_client()
        gid = _create(client).json()["goal"]["goal_id"]
        r = self._done(client, gid, meta={
            "product": "chengjie-pro", "amount": "1980",
            "note": "线下微信收款，走对公", "bogus": "ignored"})
        assert r.status_code == 200
        g = r.json()["goal"]
        assert g["status"] == "done" and g["progress"] == 1.0
        # result 逐字不变：outcome_report 的 won 判定（manual: 前缀）与
        # sold_plan_counts 的 order: 口径都不能被归因 meta 搅动
        assert g["result"] == "manual:agent"
        assert self._won_meta(client, gid) == [{
            "product": "chengjie-pro", "amount": 1980,
            "note": "线下微信收款，走对公"}]                # 未知键已丢弃

    def test_done_without_meta_behaves_exactly_as_before(self):
        client, _ = _build_client()
        gid = _create(client).json()["goal"]["goal_id"]
        r = self._done(client, gid)
        assert r.status_code == 200
        g = r.json()["goal"]
        assert g["status"] == "done" and g["result"] == "manual:agent"
        assert g["progress"] == 1.0 and g["milestone_idx"] == 3
        assert self._won_meta(client, gid) == []          # 不落噪声行
        d = client.get(f"/api/goals/{gid}").json()
        assert "status" in {e["kind"] for e in d["events"]}

    def test_amount_optional_and_empty_meta_drops(self):
        client, _ = _build_client()
        gid = _create(client).json()["goal"]["goal_id"]
        assert self._done(client, gid, meta={"product": "只填了产品"}
                          ).status_code == 200
        assert self._won_meta(client, gid) == [{"product": "只填了产品"}]

        g2 = _create(client, conv="telegram:a1:920").json()["goal"]["goal_id"]
        assert self._done(client, g2, meta={"amount": "", "note": "  "}
                          ).status_code == 200
        assert self._won_meta(client, g2) == []           # 全空 = 不落事件

    def test_meta_ignored_for_non_done_actions(self):
        client, _ = _build_client()
        gid = _create(client).json()["goal"]["goal_id"]
        assert client.post(f"/api/goals/{gid}/status", json={
            "action": "pause", "meta": {"product": "x"}}).status_code == 200
        assert self._won_meta(client, gid) == []

    def test_sanitize_won_meta_pure_rules(self):
        from src.companion.goals.service import (
            WON_META_NOTE_MAX,
            WON_META_PRODUCT_MAX,
            sanitize_won_meta,
        )
        assert sanitize_won_meta(None) == {}
        assert sanitize_won_meta("nope") == {}
        assert sanitize_won_meta({}) == {}
        assert sanitize_won_meta({"amount": -1}) == {}        # 负数丢弃
        assert sanitize_won_meta({"amount": "abc"}) == {}     # 非数丢弃
        assert sanitize_won_meta({"amount": True}) == {}      # 布尔不是金额
        assert sanitize_won_meta({"amount": 0}) == {"amount": 0}
        assert sanitize_won_meta({"amount": 19.999}) == {"amount": 20}
        assert sanitize_won_meta({"amount": 19.5}) == {"amount": 19.5}
        assert sanitize_won_meta(
            {"product": "P" * 200})["product"] == "P" * WON_META_PRODUCT_MAX
        assert sanitize_won_meta(
            {"note": "N" * 500})["note"] == "N" * WON_META_NOTE_MAX


# ── 完成通知链路状态（2026-08-18 可见化）────────────────────────────────────
# 面板「达成后会通知谁」状态行的数据源：goals.notify 扫描器开关（YAML）×
# 告警渠道是否订阅 goal_complete（notify_webhooks.json）。文件真相口径，零密钥。


class TestNotifyStatus:
    def test_disabled_403(self):
        client, _ = _build_client(enabled=False)
        assert client.get("/api/goals/notify-status").status_code == 403

    def test_covered_shape(self, monkeypatch):
        from src.integrations import notify_webhooks_store as nws
        monkeypatch.setattr(
            nws, "effective_webhooks",
            lambda cfg: [{"name": "tg-ops", "enabled": True,
                          "format": "telegram",
                          "events": ["host_alert", "goal_complete"]}])
        client, _ = _build_client(notify={"enabled": True})
        r = client.get("/api/goals/notify-status")
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True and body["notify_enabled"] is True
        assert body["push_covered"] is True
        assert body["push_channels"] == ["tg-ops"]
        # 零密钥响应：不得带 token/secret/url/target 字段
        assert not ({"token", "secret", "url", "target"} & set(body))

    def test_uncovered_and_scan_off(self, monkeypatch):
        from src.integrations import notify_webhooks_store as nws
        monkeypatch.setattr(
            nws, "effective_webhooks",
            lambda cfg: [{"name": "tg-ops", "enabled": True,
                          "format": "telegram", "events": ["host_alert"]}])
        client, _ = _build_client()          # notify 缺省关
        body = client.get("/api/goals/notify-status").json()
        assert body["notify_enabled"] is False        # 扫描器没开如实说
        assert body["push_covered"] is False
        assert body["push_channels"] == []

    def test_disabled_channel_not_counted(self, monkeypatch):
        from src.integrations import notify_webhooks_store as nws
        monkeypatch.setattr(
            nws, "effective_webhooks",
            lambda cfg: [{"name": "tg-ops", "enabled": False,
                          "format": "telegram", "events": ["goal_complete"]}])
        client, _ = _build_client(notify={"enabled": True})
        body = client.get("/api/goals/notify-status").json()
        assert body["push_covered"] is False   # 停用渠道订了也不算接通

    def test_push_agent_fields(self, tmp_path, monkeypatch):
        """P2：push_agent 回显 + self_bound 按当前登录人真实绑定判定。"""
        from src.companion.goals import notify as gn
        from src.utils.web_user_store import WebUserStore
        monkeypatch.setattr(gn, "_USER_STORE_CACHE", {})
        us = WebUserStore(tmp_path / "web_users.db")
        u = us.create_user("tester", "pass123456", "agent")
        # 默认（未开 push_agent）→ 两字段如实 False
        client0, _ = _build_client(notify={"enabled": True})
        b0 = client0.get("/api/goals/notify-status").json()
        assert b0["push_agent"] is False and b0["self_bound"] is False
        # 开 push_agent + config_path 指向 tmp（user_store_for 同目录寻址）：
        # tester 未绑定 → self_bound False；绑定后 → True
        goals = {"enabled": True, "db_path": ":memory:",
                 "notify": {"enabled": True, "push_agent": True}}
        sess = {"role": "", "user": "tester"}
        app = FastAPI()

        def auth_dep(request: Request) -> None:
            request.scope["session"] = dict(sess)

        register_goal_routes(app, auth_dep, SimpleNamespace(
            config={"companion": {"goals": goals}},
            config_path=tmp_path / "config.yaml"))
        client = TestClient(app)
        b1 = client.get("/api/goals/notify-status").json()
        assert b1["push_agent"] is True and b1["self_bound"] is False
        us.update_user(u["id"], notify_tg_chat_id="5433982810")
        b2 = client.get("/api/goals/notify-status").json()
        assert b2["self_bound"] is True


# ── created_by＝session 真键（P3 2026-08-18 生产 bug 修复钉）────────────────
# 登录只写 session["username"]（auth_user_routes:137），从不写 ["user"]——修前
# actor 恒空串 → 人建目标 created_by 全空 → 定向推送「创建人」解析链整条落空。
# 本测钉住：username 键单独在场即可归因；旧 "user" 键仍兼容（测试桩用它）。


def test_created_by_reads_session_username_key():
    goals = {"enabled": True, "db_path": ":memory:"}
    sess = {"role": "", "username": "amy"}       # 生产形态：只有 username
    app = FastAPI()

    def auth_dep(request: Request) -> None:
        request.scope["session"] = dict(sess)

    register_goal_routes(
        app, auth_dep, SimpleNamespace(
            config={"companion": {"goals": goals}}, config_path=None))
    client = TestClient(app)
    gid = _create(client).json()["goal"]["goal_id"]
    detail = client.get(f"/api/goals/{gid}").json()
    assert detail["goal"]["created_by"] == "amy"
    # 批量口同样归因（后缀 :batch 语义不变）
    r = client.post("/api/goals/batch", json={
        "template": "conversion_unlock",
        "targets": ["telegram:a1:777"]})
    assert r.status_code == 200
    rows = client.get("/api/goals?limit=20").json()["goals"]
    batch_row = next(g for g in rows
                     if g["conversation_id"] == "telegram:a1:777")
    assert batch_row["created_by"] == "amy:batch"


# ── 终局卡「已推送提醒」回执（P2 2026-08-18）──────────────────────────────
# for-conversation 的 done 终局视图附 notified（读通知幂等标记，与扫描器同一
# 事实源）；未通知/非 done 不附或 False——前端据此渲染「✓ 已推送提醒」chip。


def test_for_conversation_done_carries_notified_receipt():
    from src.companion.goals.notify import NOTIFIED_EVENT_KIND
    from src.companion.goals.store import get_goal_store
    client, _ = _build_client()
    gid = _create(client).json()["goal"]["goal_id"]
    assert client.post(f"/api/goals/{gid}/status",
                       json={"action": "done"}).status_code == 200
    r1 = client.get(f"/api/goals/for-conversation?conversation_id={CONV}").json()
    last = r1["last"]
    assert last and last["status"] == "done"
    assert last.get("notified") is False          # 扫描器还没发过
    get_goal_store().add_event(gid, NOTIFIED_EVENT_KIND, "{}")
    r2 = client.get(f"/api/goals/for-conversation?conversation_id={CONV}").json()
    assert r2["last"]["notified"] is True          # 发过了 → 回执点亮


# ── 完成推送设置写口（P3 2026-08-18）────────────────────────────────────────
# 白名单四开关经 set_overlay_flag 写 overlay；拒 agent/viewer；goals 关随全家
# 403；config_manager 无写能力时如实报 config_unavailable 不装死。


def _build_client_with_flags(enabled=True):
    """带可写 set_overlay_flag 的假 config_manager：记录写入并回填 config
    （模拟 overlay 热重载后的生效态）。"""
    goals = {"enabled": enabled, "db_path": ":memory:"}
    cfg = {"companion": {"goals": goals}}
    sess = {"role": "", "user": "tester", "username": "tester"}
    writes: list = []

    class _CM:
        config = cfg
        config_path = None

        def set_overlay_flag(self, path, value):
            writes.append((path, value))
            cur = cfg
            parts = path.split(".")
            for p in parts[:-1]:
                cur = cur.setdefault(p, {})
            cur[parts[-1]] = value
            return True, "ok"

    app = FastAPI()

    def auth_dep(request: Request) -> None:
        request.scope["session"] = dict(sess)

    register_goal_routes(app, auth_dep, _CM())
    return TestClient(app), sess, writes


def test_notify_settings_writes_whitelisted_flags_only():
    client, _sess, writes = _build_client_with_flags()
    r = client.post("/api/goals/notify-settings", json={
        "enabled": True, "push_agent": True, "bogus_key": True})
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True
    assert sorted(d["applied"]) == ["enabled", "push_agent"]   # bogus 被白名单拒
    assert ("companion.goals.notify.enabled", True) in writes
    assert ("companion.goals.notify.push_agent", True) in writes
    assert all(not p.endswith("bogus_key") for p, _v in writes)
    assert d["effective"]["notify_enabled"] is True
    assert d["effective"]["push_agent"] is True
    # 空 body=没东西可写，如实报
    r2 = client.post("/api/goals/notify-settings", json={})
    assert r2.json()["reason"] == "nothing_to_update"


def test_notify_settings_role_and_capability_gates():
    # viewer / agent 拒（403）；master/operator/空 role 放行
    client, sess, _w = _build_client_with_flags()
    for role in ("viewer", "agent"):
        sess["role"] = role
        assert client.post("/api/goals/notify-settings",
                           json={"enabled": True}).status_code == 403
    sess["role"] = "master"
    assert client.post("/api/goals/notify-settings",
                       json={"enabled": True}).status_code == 200
    # goals 总闸关 → 随全家 403
    client_off, _s2, _w2 = _build_client_with_flags(enabled=False)
    assert client_off.post("/api/goals/notify-settings",
                           json={"enabled": True}).status_code == 403
    # config_manager 无写能力（SimpleNamespace）→ 如实 config_unavailable
    client_plain, _ = _build_client()
    r = client_plain.post("/api/goals/notify-settings", json={"enabled": True})
    assert r.status_code == 200
    assert r.json() == {"ok": False, "reason": "config_unavailable"}


def test_notify_status_echoes_include_profile():
    client, _ = _build_client(notify={"enabled": True, "include_profile": True})
    d = client.get("/api/goals/notify-status").json()
    assert d["include_profile"] is True


# ── 逐目标点名收件人 notify_extra（P3 2026-08-18）───────────────────────────


def test_create_sanitizes_notify_extra():
    client, _ = _build_client()
    r = _create(client, params={
        "notify_extra": "amy, amy，-1001234567890, " + "x" * 99})
    assert r.status_code == 200
    got = r.json()["goal"]["params"]["notify_extra"]
    assert got[:2] == ["amy", "-1001234567890"]    # 去重保序
    assert len(got) == 3 and len(got[2]) == 64     # 单条截 64
    # 全垃圾输入 → 键整个不落库（不留空列表噪声）
    r2 = _create(client, conv="telegram:a1:200",
                 params={"notify_extra": [" ", ""]})
    assert "notify_extra" not in r2.json()["goal"]["params"]


def test_update_notify_extra_set_and_clear():
    client, _ = _build_client()
    gid = _create(client).json()["goal"]["goal_id"]
    r = client.post(f"/api/goals/{gid}/update", json={
        "params": {"notify_extra": ["boss", "12345678"]}})
    assert r.status_code == 200
    assert r.json()["goal"]["params"]["notify_extra"] == ["boss", "12345678"]
    # 显式传空=清除点名收件人
    r2 = client.post(f"/api/goals/{gid}/update", json={
        "params": {"notify_extra": ""}})
    assert r2.status_code == 200
    assert "notify_extra" not in r2.json()["goal"]["params"]


# ── slots_progress 带来源 src（P3 2026-08-18）───────────────────────────────


def test_slots_progress_carries_src():
    from src.companion.goals.store import get_goal_store
    client, _ = _build_client()
    r = _create(client, template="profile_discovery",
                params={"slots": "age,interests"})
    assert r.status_code == 200
    get_goal_store().upsert_customer_profile(
        "telegram", "100", {"age": "28岁"}, source="llm")
    d = client.get(
        f"/api/goals/for-conversation?conversation_id={CONV}").json()
    rows = {s["key"]: s for s in d["goal"]["slots_progress"]}
    assert rows["age"]["filled"] is True and rows["age"]["src"] == "llm"
    assert rows["age"]["stale"] is False          # 刚写的值不陈旧
    assert rows["interests"]["filled"] is False and rows["interests"]["src"] == ""
    assert rows["interests"]["stale"] is False    # 空槽绝不误标陈旧


def test_slots_progress_flags_stale_values():
    """画像时效（P1 2026-08-29）：值超 90 天未更新 → stale=True（⏳ 提醒
    顺口再确认）；旧行裸值无 ts → 年龄未知，绝不误标。"""
    import time as _t

    from src.companion.goals.store import get_goal_store
    client, _ = _build_client()
    r = _create(client, template="profile_discovery",
                params={"slots": "budget,timeline"})
    assert r.status_code == 200
    get_goal_store().upsert_customer_profile(
        "telegram", "100", {"budget": "500刀"}, source="auto",
        now=_t.time() - 120 * 86400)
    d = client.get(
        f"/api/goals/for-conversation?conversation_id={CONV}").json()
    rows = {s["key"]: s for s in d["goal"]["slots_progress"]}
    assert rows["budget"]["filled"] is True and rows["budget"]["stale"] is True
    assert rows["timeline"]["stale"] is False


def test_slot_staleness_pure_helpers():
    import time as _t

    from src.companion.goals.profile_slots import (
        SLOT_STALE_DAYS,
        slot_age_days,
        slot_is_stale,
    )

    now = _t.time()
    fields = {
        "budget": {"v": "500刀", "src": "auto", "ts": now - 100 * 86400},
        "age": {"v": "28", "src": "auto", "ts": now - 3600},
        "occupation": "老师",              # 旧行裸值无 ts
        "need": {"v": "", "src": "auto", "ts": now - 999 * 86400},   # 空值
    }
    assert SLOT_STALE_DAYS == 90.0
    assert 99 < slot_age_days(fields, "budget", now=now) < 101
    assert slot_is_stale(fields, "budget", now=now) is True
    assert slot_is_stale(fields, "age", now=now) is False
    assert slot_age_days(fields, "occupation", now=now) == -1.0
    assert slot_is_stale(fields, "occupation", now=now) is False
    assert slot_is_stale(fields, "need", now=now) is False
    assert slot_is_stale({}, "budget", now=now) is False
    assert slot_is_stale(None, "budget", now=now) is False


# ── 限时终局复盘（P2 2026-08-29）：用了几拍 + 最后一拍意图 ───────────────────


def test_for_conversation_sprint_recap_on_terminal():
    import time as _t

    from src.companion.goals.store import get_goal_store
    client, _ = _build_client()
    r = _create(client, template="custom", pace="session",
                deadline_days=60 / 1440.0,
                params={"note": "这轮要到TA的微信"})
    assert r.status_code == 200
    gid = r.json()["goal"]["goal_id"]
    store = get_goal_store()
    # 建目标当轮已规划首拍（无入站 → 槽 s:0）；再补两拍模拟真实回合
    store.upsert_action(gid, "s:123", intent="试探意向", push_level="soft")
    store.upsert_action(gid, "s:456", intent="要一个结果", push_level="direct")
    assert store.update_goal_fields(gid, deadline_ts=_t.time() - 60)
    # 活跃路径：settle-on-read 当轮转 expired → 复盘字段随视图附上
    d = client.get(
        f"/api/goals/for-conversation?conversation_id={CONV}").json()
    g = d["goal"]
    assert g["status"] == "expired" and g["pace"] == "session"
    assert g["beats_used"] == 3
    assert g["last_beat_intent"] == "要一个结果"    # s:456 字典序最新
    # 已终态后再取 → last 路径同样带复盘
    d2 = client.get(
        f"/api/goals/for-conversation?conversation_id={CONV}").json()
    assert d2["goal"] is None
    assert d2["last"]["beats_used"] == 3
    assert d2["last"]["last_beat_intent"] == "要一个结果"


def test_for_conversation_natural_view_has_no_recap_fields():
    """自然天目标（含活跃限时目标）不带复盘字段——只有终态限时目标才查拍表。"""
    client, _ = _build_client()
    r = _create(client)
    assert r.status_code == 200
    d = client.get(
        f"/api/goals/for-conversation?conversation_id={CONV}").json()
    assert "beats_used" not in d["goal"]
