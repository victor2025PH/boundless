"""P4 账号卡价值信息：今日会话计数 + TG 上次同步注入 platform_status。"""

from __future__ import annotations

import time

from src.inbox.models import InboxConversation
from src.inbox.store import InboxStore
from src.web.routes import unified_inbox_account_routes as acct_routes
from src.web.routes import unified_inbox_read_routes as read_routes


def test_count_conversations_active_since_groups_by_account(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    now = time.time()
    day0 = read_routes._local_day_start_ts(now)
    # 今日活动：acctA ×2、acctB ×1；昨日活动不应计入
    for i, (aid, ts) in enumerate([
        ("acctA", now - 60),
        ("acctA", now - 120),
        ("acctB", now - 30),
        ("acctA", day0 - 3600),
    ]):
        store.upsert_conversation(InboxConversation(
            conversation_id=f"telegram:{aid}:{i}",
            platform="telegram", account_id=aid, chat_key=str(i),
            display_name=f"c{i}", last_ts=ts, last_text="hi",
        ))
    counts = store.count_conversations_active_since(day0)
    assert counts[("telegram", "acctA")] == 2
    assert counts[("telegram", "acctB")] == 1
    assert ("telegram", "acctA") in counts
    # 平台过滤
    assert store.count_conversations_active_since(day0, platform="line") == {}


def test_local_day_start_ts_is_midnight():
    ts = read_routes._local_day_start_ts(1_720_000_000)  # 固定时刻
    lt = time.localtime(ts)
    assert (lt.tm_hour, lt.tm_min, lt.tm_sec) == (0, 0, 0)


def test_enrich_platform_status_value_injects_fields(tmp_path, monkeypatch):
    store = InboxStore(tmp_path / "inbox.db")
    now = time.time()
    store.upsert_conversation(InboxConversation(
        conversation_id="telegram:acc1:1",
        platform="telegram", account_id="acc1", chat_key="1",
        display_name="Alice", last_ts=now, last_text="hi",
    ))
    # 内存快照：上次同步 5 分钟前
    aid = "acc1"
    acct_routes._TG_HIST_SYNC.pop(aid, None)
    acct_routes._tg_hist_update(aid, state="done", finished_at=now - 300,
                                dialogs_done=3, messages=10)
    # 注册表可能不可用——stub 掉 list
    monkeypatch.setattr(
        "src.integrations.account_registry.get_account_registry",
        lambda: type("R", (), {"list": staticmethod(lambda platform=None: [])})(),
    )
    status = {
        "telegram:acc1": {
            "platform": "telegram", "account_id": "acc1", "running": True,
        },
        "whatsapp:w1": {
            "platform": "whatsapp", "account_id": "w1", "running": True,
        },
    }
    read_routes._enrich_platform_status_value(status, store)
    tg = status["telegram:acc1"]
    assert tg["today_conv_count"] == 1
    assert abs(tg["last_sync_ts"] - (now - 300)) < 1
    assert "sync_state" not in tg  # done 且非 running → 不注入 sync_state
    wa = status["whatsapp:w1"]
    assert wa["today_conv_count"] == 0
    assert "last_sync_ts" not in wa
    acct_routes._TG_HIST_SYNC.pop(aid, None)


def test_enrich_injects_running_progress(tmp_path, monkeypatch):
    store = InboxStore(tmp_path / "inbox.db")
    monkeypatch.setattr(
        "src.integrations.account_registry.get_account_registry",
        lambda: type("R", (), {"list": staticmethod(lambda platform=None: [])})(),
    )
    aid = "run1"
    acct_routes._TG_HIST_SYNC.pop(aid, None)
    assert acct_routes._tg_hist_try_start(aid) is True
    acct_routes._tg_hist_update(aid, dialogs_done=2, dialogs_total=10, messages=5)
    status = {"k": {"platform": "telegram", "account_id": aid, "running": True}}
    read_routes._enrich_platform_status_value(status, store)
    assert status["k"]["sync_state"] == "running"
    assert status["k"]["sync_dialogs_done"] == 2
    assert status["k"]["sync_dialogs_total"] == 10
    acct_routes._TG_HIST_SYNC.pop(aid, None)


def test_enrich_prefers_newer_of_memory_and_persisted(tmp_path, monkeypatch):
    store = InboxStore(tmp_path / "inbox.db")
    now = time.time()
    aid = "accP"
    acct_routes._TG_HIST_SYNC.pop(aid, None)
    # 内存较旧，持久化较新 → 取较大
    acct_routes._tg_hist_update(aid, state="done", finished_at=now - 900)
    monkeypatch.setattr(
        "src.integrations.account_registry.get_account_registry",
        lambda: type("R", (), {
            "list": staticmethod(lambda platform=None: [{
                "account_id": aid,
                "meta": {"last_history_sync_ts": now - 60},
            }]),
        })(),
    )
    status = {"k": {"platform": "telegram", "account_id": aid}}
    read_routes._enrich_platform_status_value(status, store)
    assert abs(status["k"]["last_sync_ts"] - (now - 60)) < 1
    acct_routes._TG_HIST_SYNC.pop(aid, None)


def test_persist_tg_history_sync_merges_meta(tmp_path, monkeypatch):
    """read-merge-write：不得抹掉既有 meta 键。"""
    calls = []

    class _Reg:
        def get(self, platform, account_id):
            return {"meta": {"session_string": "KEEP", "self_name": "Katie"}}

        def upsert(self, platform, account_id, **kw):
            calls.append((platform, account_id, kw.get("meta")))

    monkeypatch.setattr(
        "src.integrations.account_registry.get_account_registry", lambda: _Reg())
    finished = time.time()
    acct_routes._persist_tg_history_sync(
        "accX", finished_at=finished, dialogs=4, messages=12)
    assert len(calls) == 1
    meta = calls[0][2]
    assert meta["session_string"] == "KEEP"
    assert meta["self_name"] == "Katie"
    assert meta["last_history_sync_ts"] == finished
    assert meta["last_history_sync_dialogs"] == 4
    assert meta["last_history_sync_messages"] == 12


def test_sum_effective_unread_by_account_matches_effective_unread(tmp_path):
    """全库聚合与逐行 effective_unread 同口径（已读水位覆盖 → 不计）。"""
    store = InboxStore(tmp_path / "inbox.db")
    now = time.time()
    # acctA：2 未读且未读水位落后 → 计入；1 条已读水位盖住 → 不计
    store.upsert_conversation(InboxConversation(
        conversation_id="telegram:acctA:1",
        platform="telegram", account_id="acctA", chat_key="1",
        display_name="a1", last_ts=now, last_text="hi", unread=3,
    ))
    store.upsert_conversation(InboxConversation(
        conversation_id="telegram:acctA:2",
        platform="telegram", account_id="acctA", chat_key="2",
        display_name="a2", last_ts=now - 10, last_text="yo", unread=2,
    ))
    store.upsert_conversation(InboxConversation(
        conversation_id="telegram:acctA:3",
        platform="telegram", account_id="acctA", chat_key="3",
        display_name="a3", last_ts=now - 20, last_text="zz", unread=5,
    ))
    assert store.mark_conversation_read("telegram:acctA:3") > 0
    # acctB：跨平台不串
    store.upsert_conversation(InboxConversation(
        conversation_id="line:acctB:1",
        platform="line", account_id="acctB", chat_key="1",
        display_name="b1", last_ts=now, last_text="hi", unread=1,
    ))
    # #159：徽标口径（_unread_aggregate_maps）只数**清单里点得开**的会话，
    # 而 upsert_conversation 建的是零消息占位行——本测的对象是「聚合 vs 逐行
    # effective_unread 同口径」，得给每条补一条可见消息才落在测试对象上，
    # 否则测的是占位剔除（那条另有 tests/test_unread_aggregate.py 专测）。
    for cid, ts in (("telegram:acctA:1", now), ("telegram:acctA:2", now - 10),
                    ("telegram:acctA:3", now - 20), ("line:acctB:1", now)):
        _mk_msg(store, cid, direction="in", ts=ts)
    got = store.sum_effective_unread_by_account()
    assert got[("telegram", "acctA")] == 5  # 3+2
    assert got[("line", "acctB")] == 1
    assert ("telegram", "acctA") in got
    # 平台过滤
    assert store.sum_effective_unread_by_account(platform="line") == {
        ("line", "acctB"): 1,
    }
    by_acct, by_plat = read_routes._unread_aggregate_maps(store)
    assert by_acct["telegram:acctA"] == 5
    assert by_plat["telegram"] == 5
    assert by_plat["line"] == 1


def test_enrich_injects_unread_from_aggregate(tmp_path, monkeypatch):
    store = InboxStore(tmp_path / "inbox.db")
    now = time.time()
    store.upsert_conversation(InboxConversation(
        conversation_id="telegram:acc1:1",
        platform="telegram", account_id="acc1", chat_key="1",
        display_name="Alice", last_ts=now, last_text="hi", unread=4,
    ))
    # #159：徽标口径要求会话在清单里点得开（至少一条未软删消息），补一条
    _mk_msg(store, "telegram:acc1:1", direction="in", ts=now)
    monkeypatch.setattr(
        "src.integrations.account_registry.get_account_registry",
        lambda: type("R", (), {"list": staticmethod(lambda platform=None: [])})(),
    )
    status = {
        "telegram:acc1": {
            "platform": "telegram", "account_id": "acc1", "running": True,
        },
        "whatsapp:w1": {
            "platform": "whatsapp", "account_id": "w1", "running": True,
        },
    }
    by_acct, _ = read_routes._unread_aggregate_maps(store)
    read_routes._enrich_platform_status_value(
        status, store, unread_by_account=by_acct)
    assert status["telegram:acc1"]["unread"] == 4
    assert status["whatsapp:w1"]["unread"] == 0


def _mk_conv(store, cid, *, plat="telegram", aid="acctA", last_ts):
    key = cid.split(":")[-1]
    store.upsert_conversation(InboxConversation(
        conversation_id=cid, platform=plat, account_id=aid, chat_key=key,
        display_name=key, last_ts=last_ts, last_text="hi",
    ))


def _mk_msg(store, cid, *, direction, ts):
    from src.inbox.models import InboxMessage
    store.ingest_message(InboxMessage(
        conversation_id=cid, platform_msg_id=f"m-{cid}-{ts}",
        direction=direction, text="x", ts=ts,
    ))


def test_conversations_active_since_minimal_columns(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    now = time.time()
    _mk_conv(store, "telegram:acctA:1", last_ts=now - 60)
    _mk_conv(store, "telegram:acctA:2", last_ts=now - 999999)  # 窗外
    rows = store.conversations_active_since(now - 3600)
    assert [r["conversation_id"] for r in rows] == ["telegram:acctA:1"]
    assert rows[0]["platform"] == "telegram" and rows[0]["account_id"] == "acctA"


def test_attn_aggregate_map_semantics(tmp_path):
    """attn 口径矩阵：crit 计入 / 未超阈不计 / 我方收尾不计 / 需人工标签计入 /
    归档剔除 / 搁置未到点剔除 / 近窗外剔除；按 platform:account 分桶。"""
    store = InboxStore(tmp_path / "inbox.db")
    now = time.time()
    crit_sec = 1800

    # ① crit：对方消息收尾且等待超阈 → 计入
    _mk_conv(store, "telegram:acctA:c1", last_ts=now - 3600)
    _mk_msg(store, "telegram:acctA:c1", direction="in", ts=now - 3600)
    # ② 对方收尾但未超阈 → 不计
    _mk_conv(store, "telegram:acctA:c2", last_ts=now - 60)
    _mk_msg(store, "telegram:acctA:c2", direction="in", ts=now - 60)
    # ③ 我方收尾 → 不计
    _mk_conv(store, "telegram:acctA:c3", last_ts=now - 7200)
    _mk_msg(store, "telegram:acctA:c3", direction="out", ts=now - 7200)
    # ④ 我方收尾但挂「需人工」标签 → 计入
    _mk_conv(store, "telegram:acctA:c4", last_ts=now - 300)
    _mk_msg(store, "telegram:acctA:c4", direction="out", ts=now - 300)
    store.set_conv_tags("telegram:acctA:c4", ["需人工", "vip"])
    # ⑤ crit 但已归档 → 剔除
    _mk_conv(store, "telegram:acctA:c5", last_ts=now - 3600)
    _mk_msg(store, "telegram:acctA:c5", direction="in", ts=now - 3600)
    store.set_conv_archived("telegram:acctA:c5", True)
    # ⑥ crit 但搁置未到点 → 剔除
    _mk_conv(store, "telegram:acctA:c6", last_ts=now - 3600)
    _mk_msg(store, "telegram:acctA:c6", direction="in", ts=now - 3600)
    store.set_snooze("telegram:acctA:c6", now + 3600)
    # ⑦ crit 但最后动静在近窗外 → 剔除（active_since 取数面挡掉）
    _mk_conv(store, "telegram:acctA:c7", last_ts=now - 100 * 3600)
    _mk_msg(store, "telegram:acctA:c7", direction="in", ts=now - 100 * 3600)
    # ⑧ 另一账号 crit → 独立分桶
    _mk_conv(store, "telegram:acctB:c8", aid="acctB", last_ts=now - 3600)
    _mk_msg(store, "telegram:acctB:c8", direction="in", ts=now - 3600)

    got = read_routes._attn_aggregate_map(
        store, crit_sec=crit_sec, now=now, lookback_sec=72 * 3600)
    assert got == {"telegram:acctA": 2, "telegram:acctB": 1}  # ①+④ / ⑧


def test_enrich_injects_attn_from_aggregate(tmp_path, monkeypatch):
    store = InboxStore(tmp_path / "inbox.db")
    monkeypatch.setattr(
        "src.integrations.account_registry.get_account_registry",
        lambda: type("R", (), {"list": staticmethod(lambda platform=None: [])})(),
    )
    status = {
        "telegram:acc1": {"platform": "telegram", "account_id": "acc1"},
        "whatsapp:w1": {"platform": "whatsapp", "account_id": "w1"},
    }
    read_routes._enrich_platform_status_value(
        status, store, unread_by_account={},
        attn_by_account={"telegram:acc1": 3})
    assert status["telegram:acc1"]["attn"] == 3
    assert status["whatsapp:w1"]["attn"] == 0


def test_persist_skips_when_not_in_registry(monkeypatch):
    class _Reg:
        def get(self, *a, **k):
            return None

        def upsert(self, *a, **k):
            raise AssertionError("should not upsert")

    monkeypatch.setattr(
        "src.integrations.account_registry.get_account_registry", lambda: _Reg())
    acct_routes._persist_tg_history_sync("ghost", finished_at=1.0)
