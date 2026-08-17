# -*- coding: utf-8 -*-
"""会话级「一键接管 / 交还」门禁（驾驶舱 P0，2026-08-13）。

钉住四层不变量：

- **接管原子语义**：档位切 manual（快照接管前档位）+ 取消在途 pending/enriching
  草稿（防事件驱动 worker 竞态投递）+ 打 TAKEOVER_TAG；档位写失败＝整体失败
  （绝不假装接管成功），tag/草稿取消是 best-effort 增强；
- **交还语义**：接管前有显式档位原样恢复（review 回 review），没有则回配置全局
  默认；摘 tag；时长入历史；未接管的会话交还 → not_active；
- **注册表健壮性**：幂等（重复接管返回现状）/ 坏文件 fail-open / 历史 capped /
  超时筛选（watchdog 提醒口径）；
- **路由契约**（/api/takeover/*）：最小 FastAPI app 端到端（无 session 中间件时
  写保护按「无角色」放行，与 surface_fusion 路由门禁同口径）。
"""
from __future__ import annotations

import json

import pytest

from src.inbox import takeover as tk


@pytest.fixture()
def tko_env(tmp_path, monkeypatch):
    """注册表文件隔离进 tmp（config_dir 按 AITR_DATA_DIR 解析）+ 缓存复位。"""
    monkeypatch.setenv("AITR_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("AITR_CONFIG_PATH", raising=False)
    tk._reset_cache_for_tests()
    yield tmp_path
    tk._reset_cache_for_tests()


class _FakeStore:
    """最小假 store：档位 / 标签 / 草稿三面。"""

    def __init__(self, drafts=None, mode=None, fail_mode_set=False):
        self.modes = {}          # cid -> mode（显式档位表）
        self.mode_sources = []   # (cid, mode, source)
        self.tags = {}           # cid -> [tags]
        self.drafts = list(drafts or [])
        self.cancelled = []      # (draft_id, status, decided_by)
        self.messages = []       # list_recent_messages 用
        self._fail_mode_set = fail_mode_set
        if mode is not None:
            self.modes["preset"] = mode

    # ── automation mode ──
    def get_automation_mode_if_set(self, cid):
        return self.modes.get(cid)

    def set_automation_mode(self, cid, mode, *, source=""):
        if self._fail_mode_set:
            raise RuntimeError("db down")
        self.modes[cid] = mode
        self.mode_sources.append((cid, mode, source))

    # ── tags ──
    def get_conv_tags(self, cid):
        return list(self.tags.get(cid) or [])

    def set_conv_tags(self, cid, tags):
        self.tags[cid] = list(tags)
        return True

    # ── drafts ──
    def list_drafts(self, *, conversation_id="", status="", limit=50):
        return [d for d in self.drafts
                if d.get("conversation_id") == conversation_id
                and d.get("status") == status]

    def update_draft_status(self, draft_id, *, status, final_text="",
                            decided_by="", expected_statuses=("pending", "enriching")):
        self.cancelled.append((draft_id, status, decided_by))
        return True

    def list_recent_messages(self, cid, *, limit=50, **kw):
        return [m for m in self.messages if m.get("conversation_id") == cid][-limit:]


_CID = "telegram:tg1:5433982810"


# ── 接管语义 ──────────────────────────────────────────────────────────────────


def test_start_snapshots_mode_cancels_drafts_and_tags(tko_env):
    store = _FakeStore(drafts=[
        {"draft_id": "d1", "conversation_id": _CID, "status": "pending"},
        {"draft_id": "d2", "conversation_id": _CID, "status": "enriching"},
        {"draft_id": "d3", "conversation_id": "other:cid", "status": "pending"},
    ])
    store.modes[_CID] = "review"   # 接管前显式档位
    res = tk.start_takeover(store, _CID, by="agent01")
    assert res["ok"] is True and not res.get("already")
    e = res["entry"]
    assert e["prev_mode"] == "review"
    assert e["platform"] == "telegram"
    assert e["by"] == "agent01"
    # 档位已切 manual 且来源标注 takeover
    assert store.modes[_CID] == "manual"
    assert ("takeover" in store.mode_sources[-1][2])
    # 只取消本会话在途稿（pending+enriching），不误伤别的会话
    assert {c[0] for c in store.cancelled} == {"d1", "d2"}
    assert all(c[1] == "cancelled" and c[2] == "takeover" for c in store.cancelled)
    assert e["cancelled_drafts"] == 2
    # 标签已打
    assert tk.TAKEOVER_TAG in store.tags[_CID]
    # 注册表可读回 + 落盘
    assert tk.get_takeover(_CID)["by"] == "agent01"
    raw = json.loads((tko_env / "config" / "takeover_registry.json")
                     .read_text(encoding="utf-8"))
    assert _CID in raw["active"]


def test_start_idempotent_when_already_active(tko_env):
    store = _FakeStore()
    first = tk.start_takeover(store, _CID, by="a1")
    n_sources = len(store.mode_sources)
    second = tk.start_takeover(store, _CID, by="a2")
    assert second["ok"] is True and second["already"] is True
    assert second["entry"]["by"] == "a1"          # 保留首个接管人
    assert len(store.mode_sources) == n_sources   # 不重复写档位
    assert first["entry"]["since"] == second["entry"]["since"]


def test_start_mode_set_failure_is_hard_fail(tko_env):
    store = _FakeStore(fail_mode_set=True)
    res = tk.start_takeover(store, _CID)
    assert res["ok"] is False and res["reason"] == "mode_set_failed"
    assert tk.get_takeover(_CID) is None   # 不留半截记录


def test_start_requires_store_and_cid(tko_env):
    assert tk.start_takeover(None, _CID)["reason"] == "store_unready"
    assert tk.start_takeover(_FakeStore(), "")["reason"] == "conv_required"


# ── 交还语义 ──────────────────────────────────────────────────────────────────


def test_end_restores_explicit_prev_mode(tko_env):
    store = _FakeStore()
    store.modes[_CID] = "review"
    tk.start_takeover(store, _CID, by="a1", now=1000.0)
    res = tk.end_takeover(store, _CID, by="a1", now=1300.0)
    assert res["ok"] is True
    assert res["restored_mode"] == "review"
    assert store.modes[_CID] == "review"
    assert res["duration_sec"] == 300.0
    assert tk.get_takeover(_CID) is None
    assert tk.TAKEOVER_TAG not in store.tags.get(_CID, [])


def test_end_without_prev_mode_uses_config_default(tko_env):
    store = _FakeStore()   # 无显式档位
    tk.start_takeover(store, _CID)
    res = tk.end_takeover(
        store, _CID,
        config={"inbox": {"auto_draft": {"automation_mode": "review"}}})
    assert res["restored_mode"] == "review"
    # 配置缺省 → auto_ai（换一个从未显式设档的会话——上面那个交还后已留下
    # 显式 review 档，再接管快照到的就是它，不再是「无显式档位」场景）
    store2 = _FakeStore()
    cid2 = "telegram:tg1:fresh"
    tk.start_takeover(store2, cid2)
    res2 = tk.end_takeover(store2, cid2, config={})
    assert res2["restored_mode"] == "auto_ai"


def test_end_not_active(tko_env):
    res = tk.end_takeover(_FakeStore(), _CID)
    assert res["ok"] is False and res["reason"] == "not_active"


# ── 注册表健壮性 ──────────────────────────────────────────────────────────────


def test_corrupt_registry_fails_open(tko_env):
    p = tko_env / "config"
    p.mkdir(parents=True, exist_ok=True)
    (p / "takeover_registry.json").write_text("{not json", encoding="utf-8")
    tk._reset_cache_for_tests()
    assert tk.get_takeover(_CID) is None
    assert tk.list_active() == []
    # 坏文件之上仍可正常开始新接管（覆盖写）
    assert tk.start_takeover(_FakeStore(), _CID)["ok"] is True


def test_history_capped(tko_env):
    store = _FakeStore()
    for i in range(120):
        cid = f"telegram:tg1:{i}"
        tk.start_takeover(store, cid, now=1000.0 + i)
        tk.end_takeover(store, cid, now=2000.0 + i)
    raw = json.loads((tko_env / "config" / "takeover_registry.json")
                     .read_text(encoding="utf-8"))
    assert len(raw["history"]) <= 200


def test_overdue_and_stats(tko_env):
    store = _FakeStore()
    tk.start_takeover(store, "telegram:tg1:aaa", now=1000.0)
    tk.start_takeover(store, "telegram:tg1:bbb", now=8000.0)
    over = tk.overdue_takeovers(3600.0, now=9000.0)
    assert [e["conversation_id"] for e in over] == ["telegram:tg1:aaa"]
    assert over[0]["elapsed_sec"] == 8000.0
    tk.end_takeover(store, "telegram:tg1:aaa", now=10000.0)
    st = tk.takeover_stats()
    assert st["active"] == 1
    assert st["started"] == 2 and st["ended"] == 1
    assert st["avg_duration_sec"] == 9000.0


def test_list_active_sorted_oldest_first(tko_env):
    store = _FakeStore()
    tk.start_takeover(store, "telegram:tg1:new", now=5000.0)
    tk.start_takeover(store, "telegram:tg1:old", now=1000.0)
    ids = [e["conversation_id"] for e in tk.list_active()]
    assert ids == ["telegram:tg1:old", "telegram:tg1:new"]


# ── 路由契约 ──────────────────────────────────────────────────────────────────


def _noop_auth(request: "Request") -> None:  # noqa: F821 - 注解给 FastAPI 看
    return None


def _client(store):
    from fastapi import FastAPI, Request
    from fastapi.testclient import TestClient

    from src.web.routes.takeover_routes import register_takeover_routes
    _noop_auth.__annotations__["request"] = Request

    class _CM:
        config = {"inbox": {"auto_draft": {"automation_mode": "auto_ai"}}}

    app = FastAPI()
    app.state.inbox_store = store
    register_takeover_routes(app, api_auth=_noop_auth, config_manager=_CM())
    return TestClient(app)


def test_routes_start_status_end_roundtrip(tko_env):
    store = _FakeStore()
    client = _client(store)
    r = client.post("/api/takeover/start", json={"conversation_id": _CID})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["entry"]["conversation_id"] == _CID
    assert store.modes[_CID] == "manual"

    g = client.get("/api/takeover/status", params={"conversation_id": _CID})
    assert g.json()["entry"]["conversation_id"] == _CID

    a = client.get("/api/takeover/active").json()
    assert a["stats"]["active"] == 1
    assert a["active"][0]["conversation_id"] == _CID

    e = client.post("/api/takeover/end", json={"conversation_id": _CID})
    assert e.status_code == 200
    assert e.json()["restored_mode"] == "auto_ai"
    assert client.get("/api/takeover/status",
                      params={"conversation_id": _CID}).json()["entry"] is None


def test_routes_validation(tko_env):
    client = _client(_FakeStore())
    assert client.post("/api/takeover/start", json={}).status_code == 400
    assert client.get("/api/takeover/status").status_code == 400
    # 未接管的会话交还 → 409（前端提示「可能已被交还」）
    assert client.post("/api/takeover/end",
                       json={"conversation_id": _CID}).status_code == 409


def test_routes_store_unready_503(tko_env):
    client = _client(None)
    r = client.post("/api/takeover/start", json={"conversation_id": _CID})
    assert r.status_code == 503


# ── 交接提醒（P2 handback note）──────────────────────────────────────────────


def test_handback_note_ttl_window(tko_env):
    store = _FakeStore()
    # 接管中（未交还）：不给提醒（AI 本就不该说话）
    tk.start_takeover(store, _CID, by="a1", now=1000.0)
    assert tk.handback_note(_CID, now=2000.0) == ""
    # 交还后 TTL 窗内：给提醒（含时长/多久前的人话表述 + 衔接指令）
    tk.end_takeover(store, _CID, by="a1", now=4600.0)   # 处理 60 分钟
    note = tk.handback_note(_CID, now=4600.0 + 600.0)   # 交还 10 分钟后
    assert "交接提醒" in note and "衔接" in note
    assert "10 分钟前" in note and "60 分钟" in note
    # TTL（2h）过后：不再提醒
    assert tk.handback_note(_CID, now=4600.0 + tk.HANDBACK_NOTE_TTL_SEC + 1) == ""
    # 无记录的会话：空
    assert tk.handback_note("telegram:tg1:nobody", now=5000.0) == ""


def test_handback_note_refreshes_on_retakeover(tko_env):
    """再次接管 → 交还：提醒按最新一轮计（handbacks 同键覆写）。"""
    store = _FakeStore()
    tk.start_takeover(store, _CID, now=0.0)
    tk.end_takeover(store, _CID, now=600.0)
    tk.start_takeover(store, _CID, now=10000.0)
    # 第二轮接管期间：不提醒（正在人工，AI 闸着）……交还前的旧记录不应漏出
    tk.start_takeover(store, _CID, now=10000.0)
    tk.end_takeover(store, _CID, now=10000.0 + 1200.0)
    note = tk.handback_note(_CID, now=11200.0 + 60.0)
    assert "20 分钟" in note   # 按第二轮 1200s 计


def test_handback_records_capped(tko_env):
    store = _FakeStore()
    for i in range(120):
        cid = f"telegram:tg1:hb{i}"
        tk.start_takeover(store, cid, now=1000.0 + i)
        tk.end_takeover(store, cid, now=2000.0 + i)
    raw = json.loads((tko_env / "config" / "takeover_registry.json")
                     .read_text(encoding="utf-8"))
    assert len(raw["handbacks"]) <= 100
    # 截留的是最新的（按 until 降序）
    assert "telegram:tg1:hb119" in raw["handbacks"]
    assert "telegram:tg1:hb0" not in raw["handbacks"]


def test_handback_note_wired_into_both_chains():
    """接线钉（源码级，与 test_goal_wiring 同模式）：B 线 persona_reply 与
    A 线 protocol_autoreply 都必须消费 handback_note——挪走先红。
    生成当下必须把 inbox_store 传进 store=（交还后才回流的出站才能补进摘录）。"""
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[1] / "src"
    pr = (root / "inbox" / "persona_reply.py").read_text(
        encoding="utf-8", errors="ignore")
    assert "handback_note" in pr and "_time_hint" in pr
    assert "store=_ibx_hb" in pr
    pa = (root / "integrations" / "protocol_autoreply.py").read_text(
        encoding="utf-8", errors="ignore")
    assert "handback_note" in pa and "_topic_switch_hint" in pa
    assert "store=getattr(app.state, \"inbox_store\"" in pa


def test_handback_note_includes_outbound_digest(tko_env):
    """交还时把接管窗内 direction=out 原文写进提醒；窗外/入站不进。"""
    store = _FakeStore()
    store.messages = [
        {"conversation_id": _CID, "direction": "in", "ts": 1100.0,
         "text": "客户的话"},
        {"conversation_id": _CID, "direction": "out", "ts": 900.0,
         "text": "接管前说的"},          # since=1000 之前
        {"conversation_id": _CID, "direction": "out", "ts": 1500.0,
         "text": "晚上再聊，别等我电话"},
        {"conversation_id": _CID, "direction": "out", "ts": 1800.0,
         "text": "地址还是老地方"},
        {"conversation_id": _CID, "direction": "out", "ts": 5000.0,
         "text": "交还后 AI 说的"},      # until=4600 之后
    ]
    tk.start_takeover(store, _CID, now=1000.0)
    tk.end_takeover(store, _CID, now=4600.0)
    raw = json.loads((tko_env / "config" / "takeover_registry.json")
                     .read_text(encoding="utf-8"))
    assert raw["handbacks"][_CID]["outbound"] == [
        "晚上再聊，别等我电话", "地址还是老地方"]
    note = tk.handback_note(_CID, now=5200.0)
    assert "晚上再聊，别等我电话" in note and "地址还是老地方" in note
    assert "接管前说的" not in note and "交还后 AI 说的" not in note
    assert "客户的话" not in note


def test_handback_note_empty_outbound_is_honest(tko_env):
    """窗内零出站：提醒必须说没记到、禁止编造（Messenger 回流滞后的诚实口径）。"""
    store = _FakeStore()
    tk.start_takeover(store, _CID, now=1000.0)
    tk.end_takeover(store, _CID, now=4600.0)
    note = tk.handback_note(_CID, now=5200.0)
    assert "没有记录到" in note and "不要编造" in note
    assert "衔接" in note


def test_handback_since_zero_not_collapsed(tko_env):
    """since=0.0 是合法时刻，不得被 `or ts` 塌成交还时刻（与时长计算同坑）。"""
    store = _FakeStore()
    store.messages = [
        {"conversation_id": _CID, "direction": "out", "ts": 1.0, "text": "最早一句"},
    ]
    tk.start_takeover(store, _CID, now=0.0)
    tk.end_takeover(store, _CID, now=600.0)
    raw = json.loads((tko_env / "config" / "takeover_registry.json")
                     .read_text(encoding="utf-8"))
    assert raw["handbacks"][_CID]["since"] == 0.0
    assert raw["handbacks"][_CID]["outbound"] == ["最早一句"]


def test_handback_note_live_store_refreshes_quotes(tko_env):
    """生成当下再读 store：交还时还没有、但 ts 仍落在接管窗内的出站补进摘录。"""
    store = _FakeStore()
    tk.start_takeover(store, _CID, now=1000.0)
    tk.end_takeover(store, _CID, now=4600.0)
    assert "没有记录到" in tk.handback_note(_CID, now=4700.0)
    store.messages = [
        {"conversation_id": _CID, "direction": "out", "ts": 2000.0,
         "text": "晚点回流的人工话"},
    ]
    note = tk.handback_note(_CID, now=4700.0, store=store)
    assert "晚点回流的人工话" in note


# ── 看门狗提醒 ────────────────────────────────────────────────────────────────


def test_watchdog_overdue_publishes_and_throttles(tko_env, monkeypatch):
    """升级式提醒：超时首提 → interval 内不重提 → 交还后节流表清理。"""
    from src.inbox.health_watchdog import HealthWatchdog

    store = _FakeStore()
    tk.start_takeover(store, _CID, by="agent01", now=0.0)

    published = []

    class _Bus:
        def publish(self, etype, payload):
            published.append((etype, payload))

    import src.integrations.shared.event_bus as eb
    monkeypatch.setattr(eb, "get_event_bus", lambda: _Bus())

    class _CM:
        config = {"health_watchdog": {"takeover_remind": {
            "enabled": True, "after_min": 120, "interval_min": 60}}}

    wd = HealthWatchdog.__new__(HealthWatchdog)   # 免全量 __init__（巡检自足）
    wd._config_manager = _CM()

    # 未超时：不提醒
    wd._check_takeover_overdue(now=100.0)
    assert published == []
    # 超时（>=120min）：首提
    wd._check_takeover_overdue(now=7300.0)
    assert len(published) == 1
    etype, payload = published[0]
    assert etype == "takeover_alert"
    assert payload["conversation_id"] == _CID
    assert payload["elapsed_min"] >= 120
    assert payload["rate_key"].endswith(":takeover_remind")
    # interval（60min）内不重提
    wd._check_takeover_overdue(now=7400.0)
    assert len(published) == 1
    # interval 过后重提
    wd._check_takeover_overdue(now=7300.0 + 3700.0)
    assert len(published) == 2
    # 交还 → 静默 + 节流表清理
    tk.end_takeover(store, _CID, now=12000.0)
    wd._check_takeover_overdue(now=99999.0)
    assert len(published) == 2
    assert wd._takeover_reminded == {}


def test_watchdog_disabled_silent(tko_env, monkeypatch):
    from src.inbox.health_watchdog import HealthWatchdog

    store = _FakeStore()
    tk.start_takeover(store, _CID, now=0.0)
    published = []

    class _Bus:
        def publish(self, etype, payload):
            published.append(etype)

    import src.integrations.shared.event_bus as eb
    monkeypatch.setattr(eb, "get_event_bus", lambda: _Bus())

    class _CM:
        config = {"health_watchdog": {"takeover_remind": {"enabled": False}}}

    wd = HealthWatchdog.__new__(HealthWatchdog)
    wd._config_manager = _CM()
    wd._check_takeover_overdue(now=999999.0)
    assert published == []
