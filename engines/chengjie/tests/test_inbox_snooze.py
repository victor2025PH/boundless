"""P0-companion：会话搁置（snooze）单测。

覆盖不变量：
- set/clear/snoozed_ids/list_snoozed 基本读写；
- 到点自动重浮（读时按 now 过滤，无需扫表）；
- 过去时间 = 取消搁置（不误挂）；
- clear_snooze 对「未搁置/无 meta 行」是 no-op（不建行、不报错）——入站热路径安全；
- 客户再次来消息（ingest_collected_chats 新入站）→ 立即取消搁置；
- _snoozed_set 读侧助手正确过滤（待接管/SLA 队列据此排除）；
- 永久搁置（SNOOZE_FOREVER_TS 哨兵）：几十年后仍搁置、客户回复仍唤醒（永久≠静音）、
  +inf/超远期钉哨兵（防 FastAPI allow_nan=False 序列化 500）、NaN 按非法输入取消；
- 路由层：{"forever": true} 旗标、until_ts 过去→400（旧行为静默取消误导前端）、
  超远期 clamp、{"minutes"} 老契约回归、/api/workspace/snoozed 带 permanent 旗标；
- ops_events 审计：set/forever/clear 三档落 kind=conv_snooze（谁搁置了哪个客户可查）。
"""

import time

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.inbox.ingest import ingest_collected_chats
from src.inbox.models import InboxConversation
from src.inbox.store import SNOOZE_FOREVER_TS, InboxStore, is_permanent_snooze
from src.web.routes.unified_inbox_sla import _snoozed_set
from src.web.routes.unified_inbox_workspace_escalation_routes import (
    register_workspace_escalation_routes,
)


def _conv(store, cid="line:a:room1"):
    store.upsert_conversation(InboxConversation(
        conversation_id=cid, platform="line", account_id="a", chat_key="room1",
        display_name="User", language="ja", last_text="hi", last_ts=100, unread=1,
    ))


def test_set_and_clear_snooze_roundtrip(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    cid = "line:a:room1"
    _conv(store, cid)
    until = time.time() + 3600
    assert store.set_snooze(cid, until) is True
    assert cid in store.snoozed_ids()
    meta = store.get_conv_meta(cid)
    assert abs(float(meta["snooze_until"]) - until) < 1.0
    store.clear_snooze(cid)
    assert cid not in store.snoozed_ids()
    store.close()


def test_snooze_auto_wakes_at_deadline(tmp_path):
    """到点自动重浮：snoozed_ids 传入晚于 until 的 now 即不再包含（无需定时扫表）。"""
    store = InboxStore(tmp_path / "inbox.db")
    cid = "line:a:room1"
    _conv(store, cid)
    now = time.time()
    store.set_snooze(cid, now + 60)
    assert cid in store.snoozed_ids(now=now + 30)      # 窗口内 → 搁置中
    assert cid not in store.snoozed_ids(now=now + 90)   # 已过点 → 自动重浮
    store.close()


def test_set_snooze_past_time_is_cancel(tmp_path):
    """until<=now 视为取消，不会把会话「搁置到过去」。"""
    store = InboxStore(tmp_path / "inbox.db")
    cid = "line:a:room1"
    _conv(store, cid)
    store.set_snooze(cid, time.time() + 3600)
    assert store.set_snooze(cid, time.time() - 10) is False
    assert cid not in store.snoozed_ids()
    store.close()


def test_clear_snooze_noop_without_meta_row(tmp_path):
    """未搁置/无 meta 行时 clear 是 no-op：不报错、不凭空建 meta 行（入站热路径安全）。"""
    store = InboxStore(tmp_path / "inbox.db")
    cid = "line:a:room1"
    _conv(store, cid)
    store.clear_snooze(cid)              # 不应抛
    assert store.get_conv_meta(cid) is None  # 未被凭空建行
    store.close()


def test_list_snoozed_reports_remaining(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    cid = "line:a:room1"
    _conv(store, cid)
    store.set_snooze(cid, time.time() + 120)
    items = store.list_snoozed()
    assert len(items) == 1
    it = items[0]
    assert it["conversation_id"] == cid
    assert it["name"] == "User"
    assert 0 < it["remaining_sec"] <= 120
    store.close()


def test_customer_reply_wakes_snooze_via_ingest(tmp_path):
    """客户再次来消息（新入站）→ ingest 侧 clear_snooze，立即重浮。"""
    store = InboxStore(tmp_path / "inbox.db")
    cid = "line:a:room1"
    _conv(store, cid)
    store.set_snooze(cid, time.time() + 3600)
    assert cid in store.snoozed_ids()
    chat = {
        "conversation_id": cid, "platform": "line", "account_id": "a",
        "chat_key": "room1", "name": "User", "last_ts": 200,
        "last_message": {"text": "在吗？", "direction": "in", "ts": 200},
    }
    inserted = ingest_collected_chats(store, [chat])
    assert inserted == 1
    assert cid not in store.snoozed_ids()   # 客户回复已唤醒
    store.close()


def test_snoozed_set_helper_filters(tmp_path):
    """_snoozed_set(inbox, ids)：待接管/SLA 快照据此把搁置会话排除出队列。"""
    store = InboxStore(tmp_path / "inbox.db")
    _conv(store, "line:a:room1")
    _conv(store, "line:a:room2")
    store.set_snooze("line:a:room1", time.time() + 600)
    got = _snoozed_set(store, ["line:a:room1", "line:a:room2"])
    assert got == {"line:a:room1"}
    store.close()


# ── 永久搁置（SNOOZE_FOREVER_TS 哨兵）────────────────────────────────────────


def test_forever_snooze_roundtrip(tmp_path):
    """永久搁置＝哨兵时间戳：几十年后仍在搁置中，list_snoozed 打 permanent 旗标。"""
    store = InboxStore(tmp_path / "inbox.db")
    cid = "line:a:room1"
    _conv(store, cid)
    assert store.set_snooze(cid, SNOOZE_FOREVER_TS) is True
    # 10 年后（定时搁置早已到点的时间尺度）仍在搁置中——「不按时间重浮」语义
    assert cid in store.snoozed_ids(now=time.time() + 10 * 365 * 86400)
    items = store.list_snoozed()
    assert len(items) == 1
    assert items[0]["permanent"] is True
    assert is_permanent_snooze(items[0]["snooze_until"])
    store.close()


def test_forever_snooze_wakes_on_customer_reply(tmp_path):
    """永久≠静音：客户再来消息，ingest 侧 clear_snooze 照样立即重浮。

    这是永久搁置与「归档」的语义分界，protection 别被将来的「优化」拆掉。
    """
    store = InboxStore(tmp_path / "inbox.db")
    cid = "line:a:room1"
    _conv(store, cid)
    store.set_snooze(cid, SNOOZE_FOREVER_TS)
    assert cid in store.snoozed_ids()
    chat = {
        "conversation_id": cid, "platform": "line", "account_id": "a",
        "chat_key": "room1", "name": "User", "last_ts": 200,
        "last_message": {"text": "在吗？", "direction": "in", "ts": 200},
    }
    assert ingest_collected_chats(store, [chat]) == 1
    assert cid not in store.snoozed_ids()
    store.close()


def test_set_snooze_clamps_inf_and_overlong(tmp_path):
    """+inf / 超过哨兵的远期值一律钉到 SNOOZE_FOREVER_TS；NaN=非法输入按取消。

    inf 若入库，FastAPI JSONResponse(allow_nan=False) 序列化 /api/workspace/snoozed
    会直接 500；NaN 与 now 的比较恒 False，不拦会绕过「过去=取消」护栏被存进库。
    """
    store = InboxStore(tmp_path / "inbox.db")
    cid = "line:a:room1"
    _conv(store, cid)
    assert store.set_snooze(cid, float("inf")) is True
    assert float(store.get_conv_meta(cid)["snooze_until"]) == SNOOZE_FOREVER_TS
    assert store.set_snooze(cid, SNOOZE_FOREVER_TS * 2) is True
    assert float(store.get_conv_meta(cid)["snooze_until"]) == SNOOZE_FOREVER_TS
    assert store.set_snooze(cid, float("nan")) is False
    assert cid not in store.snoozed_ids()
    store.close()


def test_list_snoozed_orders_timed_before_forever(tmp_path):
    """到点先后排序：定时搁置在前、永久沉底（ORDER BY snooze_until ASC 自然成立）。"""
    store = InboxStore(tmp_path / "inbox.db")
    _conv(store, "line:a:room1")
    _conv(store, "line:a:room2")
    store.set_snooze("line:a:room2", SNOOZE_FOREVER_TS)
    store.set_snooze("line:a:room1", time.time() + 600)
    items = store.list_snoozed()
    assert [i["conversation_id"] for i in items] == ["line:a:room1", "line:a:room2"]
    assert [i["permanent"] for i in items] == [False, True]
    store.close()


# ── 路由层：forever 旗标 / until_ts 校验 ─────────────────────────────────────


def _api_client(store) -> TestClient:
    app = FastAPI()
    register_workspace_escalation_routes(app, api_auth=lambda r: True)
    app.state.inbox_store = store
    return TestClient(app)


def test_route_snooze_forever_flag(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    cid = "line:a:room1"
    _conv(store, cid)
    cli = _api_client(store)
    r = cli.post(f"/api/workspace/conversation/{cid}/snooze", json={"forever": True})
    d = r.json()
    assert r.status_code == 200 and d["ok"] and d["snoozed"] and d["permanent"]
    assert d["snooze_until"] == SNOOZE_FOREVER_TS
    assert cid in store.snoozed_ids(now=time.time() + 10 * 365 * 86400)
    store.close()


def test_route_until_ts_past_is_400(tmp_path):
    """过去时刻明确 400——旧行为静默取消，前端只看到莫名其妙的「搁置失败」。"""
    store = InboxStore(tmp_path / "inbox.db")
    cid = "line:a:room1"
    _conv(store, cid)
    cli = _api_client(store)
    r = cli.post(f"/api/workspace/conversation/{cid}/snooze",
                 json={"until_ts": time.time() - 100})
    assert r.status_code == 400
    assert cid not in store.snoozed_ids()
    store.close()


def test_route_until_ts_overlong_clamped_to_forever(tmp_path):
    """自定义时间给到超远期 → 按「永久」哨兵收口，响应可 JSON 序列化。"""
    store = InboxStore(tmp_path / "inbox.db")
    cid = "line:a:room1"
    _conv(store, cid)
    cli = _api_client(store)
    r = cli.post(f"/api/workspace/conversation/{cid}/snooze",
                 json={"until_ts": SNOOZE_FOREVER_TS * 3})
    d = r.json()
    assert r.status_code == 200 and d["snoozed"] and d["permanent"]
    assert d["snooze_until"] == SNOOZE_FOREVER_TS
    store.close()


def test_route_minutes_still_works(tmp_path):
    """回归：老调用方 {"minutes": N} 契约原样不动，且不误标 permanent。"""
    store = InboxStore(tmp_path / "inbox.db")
    cid = "line:a:room1"
    _conv(store, cid)
    cli = _api_client(store)
    r = cli.post(f"/api/workspace/conversation/{cid}/snooze", json={"minutes": 90})
    d = r.json()
    assert r.status_code == 200 and d["snoozed"] and d["permanent"] is False
    assert 0 < d["snooze_until"] - time.time() <= 90 * 60 + 5
    store.close()


def test_route_snoozed_listing_reports_permanent(tmp_path):
    """/api/workspace/snoozed：定时在前、永久沉底，各带 permanent 旗标。"""
    store = InboxStore(tmp_path / "inbox.db")
    _conv(store, "line:a:room1")
    _conv(store, "line:a:room2")
    cli = _api_client(store)
    cli.post("/api/workspace/conversation/line:a:room2/snooze", json={"forever": True})
    cli.post("/api/workspace/conversation/line:a:room1/snooze", json={"minutes": 30})
    d = cli.get("/api/workspace/snoozed").json()
    assert d["total"] == 2
    assert [i["permanent"] for i in d["items"]] == [False, True]
    store.close()


def test_snooze_counts_total_and_permanent(tmp_path):
    """snooze_counts：总数/永久数聚合读数（监督面）；到点自动出账（同 snoozed_ids 口径）。"""
    store = InboxStore(tmp_path / "inbox.db")
    _conv(store, "line:a:room1")
    _conv(store, "line:a:room2")
    now = time.time()
    store.set_snooze("line:a:room1", now + 600)
    store.set_snooze("line:a:room2", SNOOZE_FOREVER_TS)
    assert store.snooze_counts() == {"total": 2, "permanent": 1}
    # 定时那条到点后自动出账，仅剩永久
    assert store.snooze_counts(now=now + 1200) == {"total": 1, "permanent": 1}
    store.close()


def test_route_escalations_reports_snoozed_forever(tmp_path):
    """升级快照（团队安全网）点名永久搁置存量——防「沉默坟场」的监督读数。"""
    store = InboxStore(tmp_path / "inbox.db")
    _conv(store, "line:a:room1")
    _conv(store, "line:a:room2")
    store.set_snooze("line:a:room2", SNOOZE_FOREVER_TS)
    cli = _api_client(store)
    d = cli.get("/api/workspace/escalations").json()
    assert d["ok"] is True
    assert d["snoozed_forever"] == 1
    store.close()


def test_route_snooze_history_from_audit(tmp_path):
    """snooze-history：从 ops_events 审计反查该会话操作史（新→旧；他会话零串扰）。"""
    store = InboxStore(tmp_path / "inbox.db")
    cid = "line:a:room1"
    _conv(store, cid)
    _conv(store, "line:a:room2")
    cli = _api_client(store)
    cli.post(f"/api/workspace/conversation/{cid}/snooze", json={"minutes": 30})
    cli.post(f"/api/workspace/conversation/{cid}/snooze", json={"forever": True})
    cli.post(f"/api/workspace/conversation/{cid}/unsnooze", json={})
    cli.post("/api/workspace/conversation/line:a:room2/snooze", json={"minutes": 5})
    d = cli.get(f"/api/workspace/conversation/{cid}/snooze-history").json()
    assert d["ok"] is True and d["total"] == 3
    actions = [i["action"] for i in d["items"]]
    assert actions == ["clear", "forever", "set"]  # recent 倒序＝新→旧
    fv = d["items"][1]
    assert fv["by"] == "agent"  # 无 session 回落身份
    assert fv["until_ts"] >= SNOOZE_FOREVER_TS - 1
    # 邻会话互不串扰
    d2 = cli.get("/api/workspace/conversation/line:a:room2/snooze-history").json()
    assert d2["total"] == 1 and d2["items"][0]["action"] == "set"
    store.close()


def test_route_snooze_writes_ops_audit(tmp_path):
    """搁置/取消落 ops_events 审计（kind=conv_snooze；reason=set/forever/clear 分档）。

    「谁把哪个客户永久搁置了」从不可考变成可查——P0 的 ``set_snooze(by=)`` 只收参
    不落痕，路由层补齐最后一米。conftest 已把 ops_events 单例隔离到 tmp（绝不写生产台账）；
    审计是 best-effort，本测试同时钉住「审计字段可反解会话/账号」。
    """
    from src.ops.ops_events import get_ops_event_store

    store = InboxStore(tmp_path / "inbox.db")
    cid = "line:a:room1"
    _conv(store, cid)
    cli = _api_client(store)
    cli.post(f"/api/workspace/conversation/{cid}/snooze", json={"minutes": 30})
    cli.post(f"/api/workspace/conversation/{cid}/snooze", json={"forever": True})
    cli.post(f"/api/workspace/conversation/{cid}/unsnooze", json={})
    ops = get_ops_event_store()
    assert ops is not None
    rows = [r for r in ops.recent(limit=20) if r.get("kind") == "conv_snooze"]
    reasons = {str(r.get("reason")) for r in rows}
    assert {"set", "forever", "clear"} <= reasons
    newest = rows[0]  # recent 按 id 倒序 → 最新是 clear
    assert str(newest.get("reason")) == "clear"
    assert str(newest.get("platform")) == "line"
    assert str(newest.get("account_id")) == "a"
    assert f"conv={cid}" in str(newest.get("detail"))
    fv = next(r for r in rows if r.get("reason") == "forever")
    assert ";until=" in str(fv.get("detail"))
    store.close()
