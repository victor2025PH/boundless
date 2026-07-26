"""营销目标后台 API（goal_routes，/api/goals*）门禁。

自建 FastAPI app + 假 auth dep（session 直接塞 scope）+ SimpleNamespace 假
config_manager（.config dict + .config_path），不起生产服务。

覆盖：disabled 全端点 403；templates 形状；create（合法 / 未知模板 400 /
缺会话 400 / conversation_id 自动拆三元组 / 活跃上限 409 + 硬顶 3）；
for-conversation（活跃视图含 today / 无目标 goal=None / 终态目标进 last）；
list + summary；detail 404 与正常（actions/events）；update（title/autonomy/
deadline_days、空字段 400、非法 autonomy 单独提交按实际行为 500）；status
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


# ── templates ───────────────────────────────────────────────────────────────

def test_templates_shape():
    client, _ = _build_client()
    r = client.get("/api/goals/templates")
    assert r.status_code == 200
    d = r.json()
    assert len(d["templates"]) == 6
    assert {t["id"] for t in d["templates"]} == {
        "conversion_unlock", "conversion_subscribe", "relationship_stage",
        "relationship_intimacy", "engagement_reactivate", "custom"}
    assert d["autonomy_levels"] == ["observe", "suggest", "auto"]
    assert set(d["statuses"]) == {"active", "paused", "done", "failed",
                                  "expired", "cancelled"}
    for t in d["templates"]:
        assert "intents" not in t                 # 内部意图池不外泄
        assert len(t["milestones"]) == 4


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
        assert r.json()["goal"]["params"] == {"item_id": "a",
                                              "item_label": "详批"}

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
        assert g["title"] == "T2" and g["autonomy"] == "suggest"   # 非法值被忽略

    def test_update_404(self):
        client, _ = _build_client()
        assert client.post("/api/goals/ghost/update",
                           json={"title": "x"}).status_code == 404


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
