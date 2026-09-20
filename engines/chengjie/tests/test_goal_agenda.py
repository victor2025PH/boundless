"""营销目标「今日工作清单」（``GET /api/goals/agenda``）门禁。

自建 FastAPI app + 假 auth dep（session 直接塞 scope）+ SimpleNamespace 假
config_manager，不起生产服务；goals.db_path 用 ":memory:"（进程单例）→
autouse fixture 每测复位防串味。

覆盖：功能未启用 403；空清单**逐字**形状（另一侧 UI 按此契约取数，形状即协议）；
混合态计数（待审/已采纳/已驳回/让路）；state 筛选与 shown；limit 夹取；
排序确定性（待审优先 → 力度 → 天数倒序 → goal_id）；viewer 只读可读；
scope/state 非法值 400；agenda_reads 计数不点亮 active 看板旗标；
``?names=1`` 展示名富集（B3 ops 抽屉：开=顶层 ``names`` map／关=键缺席／
无 store·异常=软失败 ``{}``，行契约 ITEM_KEYS 恒不动）。

造数据刻意**绕开建目标路由**直接写 store：路由建目标当场就 settle 出 m0 的拍，
而清单要测的正是「不同力度/不同反馈态」并存——先落库再改里程碑，才能让首次
settle 按目标里程碑规划出 soft/direct 的拍。
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from starlette.testclient import TestClient

from src.companion.goals.planner import day_key
from src.companion.goals.store import get_goal_store, reset_goal_store
from src.web.routes.goal_routes import register_goal_routes

# 清单行的字段契约（前端按此渲染；增删键=破坏协议，必须同步改这份门禁）
ITEM_KEYS = {
    "goal_id", "conversation_id", "platform", "account_id", "chat_key",
    "title", "template", "template_name", "status",
    "day_index", "total_days", "progress",
    "milestone_idx", "milestone_label",
    # intent_en＝additive 英文展示态（i18n P0 2026-08-19 加进 agenda_item，
    # 契约钉当时漏更新——0830 冲刺批补钉）
    "intent", "intent_en", "push_level", "hold", "feedback", "beat_status",
    "autonomy",
}
COUNT_KEYS = {"total", "with_push", "pending_feedback", "adopted", "rejected",
              "hold", "shown"}


@pytest.fixture(autouse=True)
def _hermetic_goal_env(monkeypatch):
    """信号源清零（「信号未知」的确定性保守路径）+ 目标库单例复位。"""
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


def _store():
    """路由与测试共用的进程单例（db_path=":memory:" → 同一实例）。"""
    return get_goal_store(":memory:")


def _mk(store, chat_key, *, template="conversion_unlock", milestone_idx=0,
        age_days=0.0):
    """直接落库建一个活跃目标（``age_days``＝把起点往前挪几天 → day_index）。"""
    n = time.time() - float(age_days) * 86400.0
    g = store.create_goal(
        conversation_id=f"telegram:a1:{chat_key}", platform="telegram",
        account_id="a1", chat_key=chat_key, template=template,
        deadline_days=30, now=n)
    assert g is not None
    if milestone_idx:
        assert store.update_goal_fields(
            g["goal_id"], milestone_idx=int(milestone_idx))
    return str(g["goal_id"])


def _silence(store, gid, n=4):
    """连发 n 拍对方一直没回 → planner 沉默熔断（halt_after 默认 4）→ 今日 hold。"""
    now = time.time()
    for i in range(1, n + 1):
        a = store.upsert_action(gid, day_key(now - i * 86400.0), intent="x")
        assert a is not None
        assert store.mark_action(str(a["action_id"]), "consumed", detail="reply")


def _agenda(client, **params):
    r = client.get("/api/goals/agenda", params=params or None)
    assert r.status_code == 200, r.text
    return r.json()


def _ids(payload):
    return [it["goal_id"] for it in payload["items"]]


def _mix(client, store):
    """一屏典型混合态：direct 待审 / soft 待审 / none 待审 / 已采纳 / 已驳回 / 让路。"""
    ids = {
        "direct": _mk(store, "d1", milestone_idx=2),      # 转化模板 m2 = direct
        "soft": _mk(store, "s1", template="retention_expand"),   # m0 = soft
        "none": _mk(store, "n1"),                          # 转化模板 m0 = none
        "adopted": _mk(store, "a1"),
        "rejected": _mk(store, "r1"),
        "hold": _mk(store, "h1"),
    }
    _silence(store, ids["hold"])          # 必须在首次 settle 前，否则会先出当日拍
    _agenda(client)                       # 逐条 settle 出当日拍（反馈需要拍存在）
    for key, verdict in (("adopted", "adopt"), ("rejected", "reject")):
        r = client.post(f"/api/goals/{ids[key]}/beat/feedback",
                        json={"verdict": verdict})
        assert r.status_code == 200, r.text
    return ids


# ── disabled 门 ─────────────────────────────────────────────────────────────

def test_agenda_403_when_feature_disabled():
    client, _ = _build_client(enabled=False)
    assert client.get("/api/goals/agenda").status_code == 403


# ── 空清单：形状即协议 ──────────────────────────────────────────────────────

def test_empty_agenda_exact_shape():
    client, _ = _build_client()
    assert _agenda(client) == {
        "ok": True,
        "scope": "today",
        "day": day_key(),
        "counts": {"total": 0, "with_push": 0, "pending_feedback": 0,
                   "adopted": 0, "rejected": 0, "hold": 0, "shown": 0},
        "items": [],
    }


def test_item_field_contract():
    client, _ = _build_client()
    store = _store()
    gid = _mk(store, "c1", milestone_idx=2)
    it = _agenda(client)["items"][0]
    assert set(it) == ITEM_KEYS
    assert it["goal_id"] == gid and it["status"] == "active"
    assert it["conversation_id"] == "telegram:a1:c1"
    assert it["platform"] == "telegram" and it["account_id"] == "a1"
    assert it["chat_key"] == "c1"
    assert it["template"] == "conversion_unlock" and it["template_name"]
    assert it["title"] and it["milestone_label"]
    assert it["day_index"] == 1 and it["total_days"] == 30
    assert it["milestone_idx"] == 2 and it["autonomy"] == "suggest"
    assert it["intent"] and it["push_level"] == "direct"
    assert it["beat_status"] == "planned"           # 清单只读，不消耗拍
    assert it["hold"] == "" and it["feedback"] == ""
    # 刻意不联表客户昵称：展示名由前端拿会话列表自己拼
    assert "peer_name" not in it and "display_name" not in it


# ── 计数：混合态 ────────────────────────────────────────────────────────────

def test_counts_across_mixed_states():
    client, _ = _build_client()
    store = _store()
    ids = _mix(client, store)
    d = _agenda(client)
    assert set(d["counts"]) == COUNT_KEYS
    assert d["counts"] == {
        "total": 6,
        "with_push": 5,          # 让路那条今天没意图，其余 5 条都有
        "pending_feedback": 3,   # 已采纳/已驳回的不再待审
        "adopted": 1, "rejected": 1, "hold": 1, "shown": 6,
    }
    by_id = {it["goal_id"]: it for it in d["items"]}
    assert by_id[ids["adopted"]]["feedback"] == "adopted"
    assert by_id[ids["adopted"]]["beat_status"] == "planned"
    assert by_id[ids["rejected"]]["feedback"] == "rejected"
    assert by_id[ids["rejected"]]["beat_status"] == "skipped"
    held = by_id[ids["hold"]]
    assert held["hold"] == "silent"
    assert held["intent"] == "" and held["push_level"] == "none"
    assert held["beat_status"] == "" and held["feedback"] == ""
    assert by_id[ids["direct"]]["push_level"] == "direct"
    assert by_id[ids["soft"]]["push_level"] == "soft"
    assert by_id[ids["none"]]["push_level"] == "none"


def test_terminal_goal_drops_out_of_agenda():
    """settle-on-read 当场把过期目标结算掉 → 它今天已不是待办，不占清单名额。"""
    client, _ = _build_client()
    store = _store()
    keep = _mk(store, "k1")
    gone = _mk(store, "g1")
    assert store.update_goal_fields(gone, deadline_ts=time.time() - 86400.0)
    d = _agenda(client)
    assert _ids(d) == [keep]
    assert d["counts"]["total"] == 1
    assert str(store.get_goal(gone)["status"]) == "expired"


# ── state 筛选 ──────────────────────────────────────────────────────────────

def test_state_filter_keeps_true_total_and_reports_shown():
    client, _ = _build_client()
    store = _store()
    ids = _mix(client, store)

    pending = _agenda(client, state="pending")
    assert pending["counts"]["total"] == 6      # total 恒为过滤前的真实规模
    assert pending["counts"]["shown"] == 3
    assert set(_ids(pending)) == {ids["direct"], ids["soft"], ids["none"]}

    push = _agenda(client, state="push")
    assert push["counts"]["shown"] == 5 and ids["hold"] not in _ids(push)
    assert _ids(_agenda(client, state="hold")) == [ids["hold"]]
    assert _ids(_agenda(client, state="adopted")) == [ids["adopted"]]
    assert _ids(_agenda(client, state="rejected")) == [ids["rejected"]]
    # 空/all/大小写混写都=不筛
    for raw in ("", "all", "ALL", "Pending"):
        d = _agenda(client, state=raw)
        assert d["counts"]["shown"] == (3 if raw.lower() == "pending" else 6)


def test_bad_scope_and_state_400():
    client, _ = _build_client()
    assert client.get("/api/goals/agenda",
                      params={"scope": "week"}).status_code == 400
    assert client.get("/api/goals/agenda",
                      params={"state": "bogus"}).status_code == 400
    # 显式 today / 空 scope 都照常
    assert client.get("/api/goals/agenda",
                      params={"scope": "today"}).status_code == 200
    assert client.get("/api/goals/agenda",
                      params={"scope": ""}).status_code == 200


# ── limit 夹取 ──────────────────────────────────────────────────────────────

def test_limit_clamped_to_1_200():
    client, _ = _build_client()
    store = _store()
    for i in range(3):
        _mk(store, f"L{i}")
    assert len(_agenda(client)["items"]) == 3
    assert len(_agenda(client, limit=1)["items"]) == 1
    assert _agenda(client, limit=1)["counts"]["total"] == 1   # 取了几条就报几条
    assert len(_agenda(client, limit=-5)["items"]) == 1       # 下限夹到 1
    assert len(_agenda(client, limit=0)["items"]) == 3        # 0=用默认
    assert len(_agenda(client, limit=9999)["items"]) == 3     # 上限夹到 200
    assert client.get("/api/goals/agenda",
                      params={"limit": "abc"}).status_code == 422


# ── 排序确定性 ──────────────────────────────────────────────────────────────

def test_sort_pending_then_push_then_day_desc():
    client, _ = _build_client()
    store = _store()
    a = _mk(store, "S1", milestone_idx=2)                       # 待审 + direct
    b = _mk(store, "S2", template="retention_expand", age_days=5)  # 待审 soft 第6天
    c = _mk(store, "S3", template="retention_expand")           # 待审 soft 第1天
    d = _mk(store, "S4")                                        # 已采纳（沉底）
    _agenda(client)
    assert client.post(f"/api/goals/{d}/beat/feedback",
                       json={"verdict": "adopt"}).status_code == 200
    out = _agenda(client)
    assert _ids(out) == [a, b, c, d]
    assert [it["day_index"] for it in out["items"]] == [1, 6, 1, 1]


def test_sort_is_stable_across_calls():
    client, _ = _build_client()
    store = _store()
    _mix(client, store)
    assert _ids(_agenda(client)) == _ids(_agenda(client))


# ── 权限：只读端点 ──────────────────────────────────────────────────────────

def test_viewer_can_read_agenda():
    client, sess = _build_client()
    store = _store()
    gid = _mk(store, "v1")
    sess["role"] = "viewer"
    assert _ids(_agenda(client)) == [gid]


# ── names 富集（B3 ops 抽屉）────────────────────────────────────────────────

class _FakeInboxStore:
    """最小假 inbox store：只备 names 富集用到的批量查名接口（可注错）。"""

    def __init__(self, rows=None, boom=False):
        self.rows = dict(rows or {})
        self.boom = boom
        self.calls = []

    def get_conversations_for_ids(self, conversation_ids):
        self.calls.append(list(conversation_ids))
        if self.boom:
            raise RuntimeError("inbox down")
        # 与真 store 同语义：只回命中的行（dict(sqlite Row) 形状）
        return {cid: dict(row) for cid, row in self.rows.items()
                if cid in conversation_ids}


def _wire_inbox(monkeypatch, fake):
    """把假 store 挂进路由 ``_inbox_store()`` 实际读的那个 getter。"""
    import src.integrations.protocol_bridge as pb
    monkeypatch.setattr(pb, "_inbox_store_getter", lambda: fake)


def test_agenda_names_enrichment(monkeypatch):
    client, _ = _build_client()
    store = _store()
    gid = _mk(store, "c1")
    cid = "telegram:a1:c1"
    fake = _FakeInboxStore(rows={
        cid: {"display_name": "小美", "name": "被忽略"},
        "telegram:a1:other": {"display_name": "隔壁老王"},   # 非清单会话不该出现
    })
    _wire_inbox(monkeypatch, fake)
    d = _agenda(client, names="1")
    assert d["names"] == {cid: "小美"}
    assert _ids(d) == [gid]
    assert set(d["items"][0]) == ITEM_KEYS       # 行契约不因富集而动
    assert fake.calls == [[cid]]                 # 批量一次、只带清单 cid
    # "true" 大小写不限同义；其他值一律=off
    assert _agenda(client, names="TRUE")["names"] == {cid: "小美"}
    for off in ("0", "yes", "on", "2"):
        assert "names" not in _agenda(client, names=off)


def test_agenda_names_fallback_and_empty_skipped(monkeypatch):
    """display_name 空回落 name；两者皆空的行不进 map（只收非空名）。"""
    client, _ = _build_client()
    store = _store()
    _mk(store, "f1")
    _mk(store, "f2")
    fake = _FakeInboxStore(rows={
        "telegram:a1:f1": {"display_name": "", "name": "备用名"},
        "telegram:a1:f2": {"display_name": "", "name": ""},
    })
    _wire_inbox(monkeypatch, fake)
    d = _agenda(client, names="true")
    assert d["names"] == {"telegram:a1:f1": "备用名"}
    assert d["counts"]["total"] == 2             # 富集不影响清单本体


def test_agenda_without_names_param_untouched(monkeypatch):
    """缺省=旧口径：信封不带 names 键，连查名都不发生。"""
    client, _ = _build_client()
    store = _store()
    _mk(store, "c1")
    fake = _FakeInboxStore(rows={"telegram:a1:c1": {"display_name": "小美"}})
    _wire_inbox(monkeypatch, fake)
    d = _agenda(client)
    assert "names" not in d
    assert fake.calls == []


def test_agenda_names_soft_fail_on_error(monkeypatch):
    """富集抛异常 → 路由照常 200，names 软失败为空 map，清单本体完好。"""
    client, _ = _build_client()
    store = _store()
    gid = _mk(store, "c1")
    _wire_inbox(monkeypatch, _FakeInboxStore(boom=True))
    d = _agenda(client, names="1")
    assert d["names"] == {}
    assert _ids(d) == [gid]


def test_agenda_names_without_inbox_store_and_on_empty_agenda():
    """无 inbox store（autouse fixture 已置空 getter）/空清单 → names 恒 {}。"""
    client, _ = _build_client()
    assert _agenda(client, names="1")["names"] == {}     # 空清单不查名
    store = _store()
    _mk(store, "c1")
    assert _agenda(client, names="1")["names"] == {}     # 有清单但无 store


# ── 观测 ────────────────────────────────────────────────────────────────────

def test_agenda_read_counted_without_flipping_active():
    from src.companion.goals.stats import get_goal_stats

    client, _ = _build_client()
    st = get_goal_stats()
    before = st.dump()
    _agenda(client)
    after = st.dump()
    assert after["agenda_reads"] == before["agenda_reads"] + 1
    # 纯读数不该点亮看板卡（active 只认真业务流量）
    assert after["active"] == before["active"]
