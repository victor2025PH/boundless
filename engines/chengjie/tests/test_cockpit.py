# -*- coding: utf-8 -*-
"""驾驶舱介入队列门禁（cockpit P1，2026-08-13）。

钉住四层不变量：

- **store 原语**：``list_tagged_conversations`` LIKE 预筛 + json 精确复核
  （「需人工」不得被「不需人工确认」子串假阳性命中）、归档排除；
  ``archived_conversation_ids`` 口径；
- **聚合语义**：四源合成 / 同会话去重保最高优先级并合并 flags / 排序
  （priority 升序、同级久者在前）/ waiting 源的排除集（群聊、已归档、
  有 pending 草稿、接管中）与年龄窗；
- **fail-soft**：单源抛异常 → 其余源照常、坏源在 sources 里标 error，
  绝不整体失败；
- **快照缓存**：30s TTL 命中 + force 绕过；路由契约（/api/cockpit/overview）。
"""
from __future__ import annotations

import pytest

from src.inbox import cockpit as ck
from src.inbox import takeover as tk


@pytest.fixture(autouse=True)
def _fresh_caches(tmp_path, monkeypatch):
    """接管注册表隔离进 tmp + 两个模块缓存复位。"""
    monkeypatch.setenv("AITR_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("AITR_CONFIG_PATH", raising=False)
    tk._reset_cache_for_tests()
    ck._reset_cache_for_tests()
    yield
    tk._reset_cache_for_tests()
    ck._reset_cache_for_tests()


# ── store 原语（真库） ────────────────────────────────────────────────────────


def _real_store(tmp_path):
    from src.inbox.store import InboxStore
    return InboxStore(tmp_path / "inbox.db")


def _seed_conv(store, cid, *, name="客户", platform="telegram"):
    from src.inbox.normalizer import message_obj, normalize_chat
    plat, acct, chat_key = cid.split(":", 2)
    chat = normalize_chat(
        platform=plat, platform_name=plat.title(), account_id=acct,
        account_label=acct, chat_key=chat_key, name=name,
        last_msg="hi", last_ts=1000.0, unread=1,
    )
    m = message_obj(text="hi", ts=1000.0, direction="in", message_id="m1")
    chat["messages"] = [m]
    from src.inbox.ingest import ingest_collected_chats
    ingest_collected_chats(store, [chat], publish_events=False)


def test_store_tagged_conversations_exact_and_archived(tmp_path):
    store = _real_store(tmp_path)
    cid_a = "telegram:tg1:1001"
    cid_b = "telegram:tg1:1002"
    cid_c = "telegram:tg1:1003"
    for cid in (cid_a, cid_b, cid_c):
        _seed_conv(store, cid)
    store.set_conv_tags(cid_a, ["需人工"])
    store.set_conv_tags(cid_b, ["不需人工确认"])   # 子串假阳性诱饵
    store.set_conv_tags(cid_c, ["需人工", "VIP"])
    rows = store.list_tagged_conversations("需人工")
    got = {r["conversation_id"] for r in rows}
    assert got == {cid_a, cid_c}
    assert all("需人工" in (r.get("conv_tags") or []) for r in rows)
    # 归档后不再出现
    store.set_conv_archived(cid_a, True)
    got2 = {r["conversation_id"]
            for r in store.list_tagged_conversations("需人工")}
    assert got2 == {cid_c}
    assert cid_a in set(store.archived_conversation_ids())


# ── 聚合语义（假 store） ──────────────────────────────────────────────────────


class _FakeStore:
    def __init__(self):
        self.convs = []        # conversations 行
        self.dirs = {}         # cid -> {direction, ts}
        self.archived = []
        self.tagged = []       # list_tagged_conversations 返回
        self.drafts = []       # reply_drafts 行
        self.recent = {}       # cid -> 消息行列表（引用补齐探查）
        self.recent_calls = []  # 被探查过的 cid（probe cap 断言）
        self.raise_on = set()  # 方法名 -> 抛异常（fail-soft 演练）

    def _maybe_raise(self, name):
        if name in self.raise_on:
            raise RuntimeError(f"{name} down")

    def list_conversations(self, *, limit=50, **kw):
        self._maybe_raise("list_conversations")
        return [dict(c) for c in self.convs[:limit]]

    def last_message_dirs(self, conversation_ids=None):
        self._maybe_raise("last_message_dirs")
        return dict(self.dirs)

    def archived_conversation_ids(self):
        self._maybe_raise("archived_conversation_ids")
        return list(self.archived)

    def list_tagged_conversations(self, tag, *, limit=100):
        self._maybe_raise("list_tagged_conversations")
        return [dict(r) for r in self.tagged]

    def list_drafts(self, *, status="", limit=50, **kw):
        self._maybe_raise("list_drafts")
        return [dict(d) for d in self.drafts if d.get("status") == status]

    def list_recent_messages(self, conversation_id, *, limit=50,
                             before_ts=None):
        self._maybe_raise("list_recent_messages")
        self.recent_calls.append(conversation_id)
        return [dict(m) for m in self.recent.get(conversation_id, [])][-limit:]

    # takeover 依赖面（start 用）
    def get_automation_mode_if_set(self, cid):
        return None

    def set_automation_mode(self, cid, mode, *, source=""):
        pass

    def get_conv_tags(self, cid):
        return []

    def set_conv_tags(self, cid, tags):
        return True

    def update_draft_status(self, draft_id, **kw):
        return True


def _conv(cid, *, name="客户", chat_type="private", last_ts=1000.0):
    parts = cid.split(":", 2)
    plat = parts[0]
    acct = parts[1] if len(parts) >= 2 else ""
    ck_ = parts[2] if len(parts) >= 3 else ""
    return {"conversation_id": cid, "platform": plat, "name": name,
            "account_id": acct, "chat_key": ck_,
            "chat_type": chat_type, "last_ts": last_ts, "last_msg": "hi"}


NOW = 100000.0


def test_queue_composes_dedups_and_sorts():
    store = _FakeStore()
    # waiting：入站悬空 2h
    store.convs = [_conv("telegram:a:w1"), _conv("telegram:a:h1"),
                   _conv("telegram:a:d1")]
    store.dirs = {"telegram:a:w1": {"direction": "in", "ts": NOW - 7200}}
    # needs_human：同一会话 h1 也在 waiting 候选里（dirs 有入站）→ 应去重保 needs_human
    store.dirs["telegram:a:h1"] = {"direction": "in", "ts": NOW - 3600}
    store.tagged = [_conv("telegram:a:h1", name="老王")]
    # draft_pending：d1 草稿 40 分钟
    store.drafts = [{"draft_id": "dr1", "conversation_id": "telegram:a:d1",
                     "status": "pending", "created_ts": NOW - 2400,
                     "platform": "telegram", "final_text": "草稿文本",
                     "autopilot_level": "L1"}]
    # takeover_overdue：t1 接管 3h（阈值默认 120min）
    tk.start_takeover(store, "telegram:a:t1", by="agent01", now=NOW - 10800)

    q = ck.collect_intervention_queue(store, {}, now=NOW)
    kinds = [(i["kind"], i["conversation_id"]) for i in q["items"]]
    assert kinds[0] == ("takeover_overdue", "telegram:a:t1")
    assert ("needs_human", "telegram:a:h1") in kinds
    assert ("waiting", "telegram:a:w1") in kinds
    assert ("draft_pending", "telegram:a:d1") in kinds
    # 去重：h1 只出现一次，flags 合并两源
    h1 = [i for i in q["items"] if i["conversation_id"] == "telegram:a:h1"]
    assert len(h1) == 1
    assert set(h1[0]["flags"]) == {"needs_human", "waiting"}
    # d1 的 waiting 被 pending 草稿排除集挡掉（有稿是 draft_pending 辖区）
    d1 = [i for i in q["items"] if i["conversation_id"] == "telegram:a:d1"]
    assert len(d1) == 1 and d1[0]["kind"] == "draft_pending"
    assert q["counts"]["needs_human"] == 1
    assert all(v == "ok" for v in q["sources"].values())


def test_waiting_exclusions_and_age_window():
    store = _FakeStore()
    store.convs = [
        _conv("telegram:a:fresh"),            # 5 分钟：宽限内不进
        _conv("telegram:a:old"),              # 80h：超窗不进
        _conv("telegram:a:group", chat_type="group"),   # 群聊不进
        _conv("telegram:a:arch"),             # 已归档不进
        _conv("telegram:a:tk"),               # 接管中不进（超时另有 1 号源）
        _conv("telegram:a:outlast"),          # 末条是出站不进
        _conv("telegram:a:good"),             # 唯一合格
    ]
    for cid, age in (("telegram:a:fresh", 300), ("telegram:a:old", 288000),
                     ("telegram:a:group", 7200), ("telegram:a:arch", 7200),
                     ("telegram:a:tk", 7200), ("telegram:a:good", 7200)):
        store.dirs[cid] = {"direction": "in", "ts": NOW - age}
    store.dirs["telegram:a:outlast"] = {"direction": "out", "ts": NOW - 7200}
    store.archived = ["telegram:a:arch"]
    tk.start_takeover(store, "telegram:a:tk", now=NOW - 7200)

    q = ck.collect_intervention_queue(store, {}, now=NOW)
    waiting = [i["conversation_id"] for i in q["items"] if i["kind"] == "waiting"]
    assert waiting == ["telegram:a:good"]


def test_waiting_unread_weight_and_field():
    """P2 调准：同级内 unread>0（没人看过）排在 unread=0（看过没回）之前；
    更久者在各自档内靠前；item 带 unread 字段（驾驶舱卡徽章消费）。"""
    store = _FakeStore()
    store.convs = [
        dict(_conv("telegram:a:seen_old"), unread=0),
        dict(_conv("telegram:a:unread_new"), unread=3),
        dict(_conv("telegram:a:unread_old"), unread=1),
    ]
    store.dirs = {
        "telegram:a:seen_old": {"direction": "in", "ts": NOW - 50000},
        "telegram:a:unread_new": {"direction": "in", "ts": NOW - 7200},
        "telegram:a:unread_old": {"direction": "in", "ts": NOW - 40000},
    }
    q = ck.collect_intervention_queue(store, {}, now=NOW)
    order = [i["conversation_id"] for i in q["items"]]
    assert order == ["telegram:a:unread_old", "telegram:a:unread_new",
                     "telegram:a:seen_old"]
    by_cid = {i["conversation_id"]: i for i in q["items"]}
    assert by_cid["telegram:a:unread_new"]["unread"] == 3
    assert by_cid["telegram:a:seen_old"]["unread"] == 0


def test_waiting_uses_effective_unread_not_raw_sync():
    """工作台已读水位盖住末条后，协议同步回来的裸 unread 不得再当「没人看过」。"""
    store = _FakeStore()
    opened = dict(_conv("telegram:a:opened"), unread=5, last_ts=NOW - 40000,
                  last_read_ts=NOW - 100)   # 打开过，水位新于末条
    never = dict(_conv("telegram:a:never"), unread=2, last_ts=NOW - 20000,
                 last_read_ts=0)
    store.convs = [opened, never]
    store.dirs = {
        "telegram:a:opened": {"direction": "in", "ts": NOW - 40000},
        "telegram:a:never": {"direction": "in", "ts": NOW - 20000},
    }
    q = ck.collect_intervention_queue(store, {}, now=NOW)
    order = [i["conversation_id"] for i in q["items"]]
    assert order == ["telegram:a:never", "telegram:a:opened"]
    by_cid = {i["conversation_id"]: i for i in q["items"]}
    assert by_cid["telegram:a:opened"]["unread"] == 0
    assert by_cid["telegram:a:never"]["unread"] == 2


def test_fail_soft_per_source():
    store = _FakeStore()
    store.convs = [_conv("telegram:a:w1")]
    store.dirs = {"telegram:a:w1": {"direction": "in", "ts": NOW - 7200}}
    store.raise_on = {"list_tagged_conversations"}
    q = ck.collect_intervention_queue(store, {}, now=NOW)
    assert q["sources"]["needs_human"] == "error"
    assert q["sources"]["waiting"] == "ok"
    assert [i["kind"] for i in q["items"]] == ["waiting"]


def test_snapshot_cache_ttl_and_force():
    store = _FakeStore()
    store.convs = [_conv("telegram:a:w1")]
    store.dirs = {"telegram:a:w1": {"direction": "in", "ts": NOW - 7200}}
    s1 = ck.cockpit_snapshot(store, {}, now=NOW)
    assert s1["cache_age_sec"] == 0.0
    assert len(s1["items"]) == 1
    # 数据变了但 TTL 内 → 仍旧值
    store.dirs["telegram:a:w2"] = {"direction": "in", "ts": NOW - 7200}
    store.convs.append(_conv("telegram:a:w2"))
    s2 = ck.cockpit_snapshot(store, {}, now=NOW + 10)
    assert len(s2["items"]) == 1 and s2["cache_age_sec"] == 10.0
    # force → 重算
    s3 = ck.cockpit_snapshot(store, {}, now=NOW + 11, force=True)
    assert len(s3["items"]) == 2
    # TTL 过期 → 自动重算
    ck.cockpit_snapshot(store, {}, now=NOW + 11)
    s4 = ck.cockpit_snapshot(store, {}, now=NOW + 60)
    assert s4["cache_age_sec"] == 0.0


def test_snapshot_carries_takeover_block():
    store = _FakeStore()
    tk.start_takeover(store, "telegram:a:t1", by="agent01", now=NOW - 600)
    s = ck.cockpit_snapshot(store, {}, now=NOW, force=True)
    assert s["takeover"]["stats"]["active"] == 1
    act = s["takeover"]["active"]
    assert act[0]["conversation_id"] == "telegram:a:t1"
    assert act[0]["elapsed_sec"] == 600.0


def test_identity_passthrough_and_enrichment():
    """P3 身份卡：needs_human/waiting 源直传 avatar_url/username；
    takeover_overdue / draft_pending 源自身不带会话行 → 从会话映射补齐身份，
    takeover 卡另补客户末条原话 last_text（detail 的接管人语义不被覆盖）。"""
    store = _FakeStore()
    store.convs = [
        dict(_conv("telegram:a:w1", name="小美"),
             avatar_url="/static/ava/a.png", username="mei"),
        dict(_conv("telegram:a:t1", name="老张"),
             avatar_url="/static/ava/t.png", username="zhang",
             last_msg="在吗？我想换货"),
        dict(_conv("telegram:a:d1", name="阿豪"), username="hao"),
    ]
    store.dirs = {"telegram:a:w1": {"direction": "in", "ts": NOW - 7200}}
    store.tagged = [dict(_conv("telegram:a:h1", name="老王"),
                         avatar_url="/static/ava/h.png", username="wang")]
    store.drafts = [{"draft_id": "dr1", "conversation_id": "telegram:a:d1",
                     "status": "pending", "created_ts": NOW - 2400,
                     "platform": "telegram", "final_text": "草稿文本",
                     "autopilot_level": "L1"}]
    tk.start_takeover(store, "telegram:a:t1", by="agent01", now=NOW - 10800)

    q = ck.collect_intervention_queue(store, {}, now=NOW)
    by = {i["conversation_id"]: i for i in q["items"]}
    # 直传源
    assert by["telegram:a:w1"]["avatar_url"] == "/static/ava/a.png"
    assert by["telegram:a:w1"]["username"] == "mei"
    assert by["telegram:a:h1"]["avatar_url"] == "/static/ava/h.png"
    assert by["telegram:a:h1"]["username"] == "wang"
    # 补齐源：takeover 卡
    t1 = by["telegram:a:t1"]
    assert t1["kind"] == "takeover_overdue"
    assert t1["name"] == "老张"
    assert t1["avatar_url"] == "/static/ava/t.png"
    assert t1["last_text"] == "在吗？我想换货"
    assert t1["detail"] == "agent01"
    # 补齐源：draft 卡（无头像不硬造，username 补上）
    d1 = by["telegram:a:d1"]
    assert d1["username"] == "hao"
    assert not d1.get("avatar_url")
    # P4 头像代理寻址三元组：直传源与补齐源都必须带 account_id/chat_key/chat_type
    # （前端拼 /api/platforms/{platform}/{account_id}/avatar?chat_key= 懒加载回源）
    for cid in ("telegram:a:w1", "telegram:a:t1", "telegram:a:d1"):
        assert by[cid]["account_id"] == "a", cid
        assert by[cid]["chat_key"] == cid.split(":", 2)[2], cid
        assert by[cid]["chat_type"] == "private", cid


def test_quote_backfill_from_messages():
    """P5 引用补齐：会话行 last_msg 空 → 从消息表补末条**入站**文本；
    纯媒体末条给 quote_media（前端渲染本地化占位）；已有引用/draft 卡不动；
    probe cap 限探查数（终榜按优先级从上往下补）。"""
    store = _FakeStore()
    store.convs = [
        dict(_conv("telegram:a:w1"), last_msg=""),      # 空 → 补文本
        dict(_conv("telegram:a:w2"), last_msg=""),      # 空 → 纯媒体占位
        dict(_conv("telegram:a:w3"), last_msg="已有原话"),  # 有 → 不动
        dict(_conv("telegram:a:t1"), last_msg=""),      # takeover 卡 last_text 补
        dict(_conv("telegram:a:d1"), last_msg=""),      # draft 卡不补
    ]
    for cid, age in (("telegram:a:w1", 7200), ("telegram:a:w2", 7200),
                     ("telegram:a:w3", 7200)):
        store.dirs[cid] = {"direction": "in", "ts": NOW - age}
    store.drafts = [{"draft_id": "dr1", "conversation_id": "telegram:a:d1",
                     "status": "pending", "created_ts": NOW - 2400,
                     "platform": "telegram", "final_text": "",
                     "autopilot_level": "L1"}]
    tk.start_takeover(store, "telegram:a:t1", by="agent01", now=NOW - 10800)
    store.recent = {
        # 末条是自己发的出站 → 要跳过找最近一条入站
        "telegram:a:w1": [
            {"direction": "in", "text": "这个还有货吗", "ts": NOW - 7300},
            {"direction": "out", "text": "稍等我看下", "ts": NOW - 7250},
        ],
        "telegram:a:w2": [
            {"direction": "in", "text": "", "media_type": "image",
             "ts": NOW - 7200},
        ],
        "telegram:a:t1": [
            {"direction": "in", "text": "在吗？想换货", "ts": NOW - 11000},
        ],
        "telegram:a:d1": [
            {"direction": "in", "text": "不该出现", "ts": NOW - 2500},
        ],
    }
    q = ck.collect_intervention_queue(store, {}, now=NOW)
    by = {i["conversation_id"]: i for i in q["items"]}
    assert by["telegram:a:w1"]["detail"] == "这个还有货吗"
    assert by["telegram:a:w2"]["detail"] == ""
    assert by["telegram:a:w2"]["quote_media"] == "image"
    assert by["telegram:a:w3"]["detail"] == "已有原话"
    assert by["telegram:a:t1"]["last_text"] == "在吗？想换货"
    assert by["telegram:a:t1"]["detail"] == "agent01"   # 接管人语义不被覆盖
    assert not by["telegram:a:d1"].get("quote_media")
    assert by["telegram:a:d1"]["detail"] == ""
    # 有原话的 w3 与 draft 卡不产生探查
    assert set(store.recent_calls) == {"telegram:a:w1", "telegram:a:w2",
                                       "telegram:a:t1"}


def test_quote_backfill_probe_cap_and_fail_soft():
    """cap=1 只探终榜最靠前的一张空引用卡；探查抛异常不伤快照。"""
    store = _FakeStore()
    store.convs = [dict(_conv("telegram:a:w1"), last_msg=""),
                   dict(_conv("telegram:a:w2"), last_msg="")]
    store.dirs = {"telegram:a:w1": {"direction": "in", "ts": NOW - 50000},
                  "telegram:a:w2": {"direction": "in", "ts": NOW - 7200}}
    store.recent = {
        "telegram:a:w1": [{"direction": "in", "text": "补到我", "ts": NOW - 50000}],
        "telegram:a:w2": [{"direction": "in", "text": "轮不到我", "ts": NOW - 7200}],
    }
    cfg = {"inbox": {"cockpit": {"quote_probe_cap": 1}}}
    q = ck.collect_intervention_queue(store, cfg, now=NOW)
    by = {i["conversation_id"]: i for i in q["items"]}
    assert by["telegram:a:w1"]["detail"] == "补到我"   # 更久者排前，先补
    assert by["telegram:a:w2"]["detail"] == ""
    assert store.recent_calls == ["telegram:a:w1"]
    # fail-soft：探查全炸 → 快照照常出、引用保持空
    store2 = _FakeStore()
    store2.convs = [dict(_conv("telegram:a:w1"), last_msg="")]
    store2.dirs = {"telegram:a:w1": {"direction": "in", "ts": NOW - 7200}}
    store2.raise_on = {"list_recent_messages"}
    q2 = ck.collect_intervention_queue(store2, {}, now=NOW)
    assert [i["conversation_id"] for i in q2["items"]] == ["telegram:a:w1"]
    assert q2["items"][0]["detail"] == ""


# ── 路由契约 ──────────────────────────────────────────────────────────────────


def _noop_auth(request: "Request") -> None:  # noqa: F821 - 注解给 FastAPI 看
    return None


def test_route_overview_shape():
    from fastapi import FastAPI, Request
    from fastapi.testclient import TestClient

    from src.web.routes.cockpit_routes import register_cockpit_routes
    _noop_auth.__annotations__["request"] = Request

    import time as _t
    store = _FakeStore()
    store.convs = [_conv("telegram:a:w1")]
    # 路由用真实时钟 → 悬空时间必须相对 time.time()（NOW 假基准会超 72h 窗被过滤）
    store.dirs = {"telegram:a:w1": {"direction": "in", "ts": _t.time() - 7200}}

    app = FastAPI()
    app.state.inbox_store = store
    register_cockpit_routes(app, api_auth=_noop_auth)
    client = TestClient(app)
    r = client.get("/api/cockpit/overview", params={"force": 1})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert isinstance(body["items"], list) and body["items"]
    assert body["items"][0]["kind"] == "waiting"
    assert "takeover" in body and "sources" in body and "counts" in body


def test_route_store_unready_503():
    from fastapi import FastAPI, Request
    from fastapi.testclient import TestClient

    from src.web.routes.cockpit_routes import register_cockpit_routes
    _noop_auth.__annotations__["request"] = Request
    app = FastAPI()
    app.state.inbox_store = None
    register_cockpit_routes(app, api_auth=_noop_auth)
    assert TestClient(app).get("/api/cockpit/overview").status_code == 503


# ── resolve（「已处理」摘「需人工」标签）─────────────────────────────────────


class _TagStore(_FakeStore):
    """带真实 tag 状态的 fake（resolve 端到端要看写没写）。"""

    def __init__(self):
        super().__init__()
        self.tags = {}         # cid -> [tag]

    def get_conv_tags(self, cid):
        return list(self.tags.get(cid, []))

    def set_conv_tags(self, cid, tags):
        self.tags[cid] = list(tags)
        return True


def _resolve_app(store):
    from fastapi import FastAPI, Request

    from src.web.routes.cockpit_routes import register_cockpit_routes
    _noop_auth.__annotations__["request"] = Request
    app = FastAPI()
    app.state.inbox_store = store
    register_cockpit_routes(app, api_auth=_noop_auth)
    return app


def test_overview_carries_caps_resolve():
    """caps 是前端特性探测位：旧后端缺位 → 前端不渲染「已处理」按钮。"""
    from fastapi.testclient import TestClient

    store = _TagStore()
    client = TestClient(_resolve_app(store))
    body = client.get("/api/cockpit/overview", params={"force": 1}).json()
    assert body["caps"]["resolve"] is True


def test_resolve_removes_handoff_tag_and_busts_cache():
    from fastapi.testclient import TestClient

    from src.inbox.cockpit import _reset_cache_for_tests
    from src.integrations.protocol_autoreply import HANDOFF_TAG

    _reset_cache_for_tests()
    store = _TagStore()
    store.tags["telegram:a:u1"] = [HANDOFF_TAG, "vip"]
    store.tagged = [{"conversation_id": "telegram:a:u1", "name": "客户A",
                     "platform": "telegram", "tagged_ts": 1000.0}]
    client = TestClient(_resolve_app(store))
    # 先取一次 overview 建缓存（needs_human 卡在队列里）
    body = client.get("/api/cockpit/overview", params={"force": 1}).json()
    assert any(i["kind"] == "needs_human" for i in body["items"])
    r = client.post("/api/cockpit/resolve",
                    json={"conversation_id": "telegram:a:u1"})
    assert r.status_code == 200 and r.json()["removed"] is True
    # 只摘 HANDOFF_TAG，其他标签保留
    assert store.tags["telegram:a:u1"] == ["vip"]
    # 缓存被失效：下一拉不带 force 也是重算（tagged 清掉后卡消失）
    store.tagged = []
    body2 = client.get("/api/cockpit/overview").json()
    assert not any(i["kind"] == "needs_human" for i in body2["items"])


def test_resolve_idempotent_and_validates():
    from fastapi.testclient import TestClient

    store = _TagStore()
    store.tags["telegram:a:u2"] = ["vip"]        # 没有 HANDOFF_TAG
    client = TestClient(_resolve_app(store))
    r = client.post("/api/cockpit/resolve",
                    json={"conversation_id": "telegram:a:u2"})
    assert r.status_code == 200 and r.json()["removed"] is False
    assert store.tags["telegram:a:u2"] == ["vip"]   # 幂等：不写
    # 缺 conversation_id → 400
    assert client.post("/api/cockpit/resolve", json={}).status_code == 400
    # store 未就绪 → 503
    app2 = _resolve_app(store)
    app2.state.inbox_store = None
    assert TestClient(app2).post(
        "/api/cockpit/resolve",
        json={"conversation_id": "x"}).status_code == 503
