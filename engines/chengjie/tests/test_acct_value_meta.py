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


def test_persist_skips_when_not_in_registry(monkeypatch):
    class _Reg:
        def get(self, *a, **k):
            return None

        def upsert(self, *a, **k):
            raise AssertionError("should not upsert")

    monkeypatch.setattr(
        "src.integrations.account_registry.get_account_registry", lambda: _Reg())
    acct_routes._persist_tg_history_sync("ghost", finished_at=1.0)
