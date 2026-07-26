"""目录同步观测门禁。

覆盖：
- DirectorySyncStats 计数/消毒/账号隔离/上限/dump/dump_prom/reset；
- telegram_directory_sync.sync_directory_once 的埋点接线（成功轮 + 两段失败）；
- **埋点绝不影响同步**：统计模块抛异常时同步仍照常返回、照常落库。
"""

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from src.inbox.store import InboxStore
from src.integrations import protocol_bridge, telegram_directory_sync
from src.integrations.directory_sync_stats import (
    DirectorySyncStats,
    _esc,
    get_directory_sync_stats,
)
from src.integrations.telegram_directory_sync import sync_directory_once
from src.web.routes.drafts_routes import register_metrics_route

CFG_ON = {"enabled": True, "max_contacts": 100, "max_dialogs": 100,
          "seed_chats_max": 200, "pace_seconds": 0}


@pytest.fixture(autouse=True)
def _isolate_singleton():
    """进程级单例前后清零，防串测（接线用例写的是同一单例）。"""
    get_directory_sync_stats().reset()
    yield
    get_directory_sync_stats().reset()


class _Clock:
    """确定性时钟：真实 time.time() 在 Windows 上分辨率不足以保证「后记的更晚」，
    排序用例不能靠它，否则偶发 flaky。"""

    def __init__(self, start=1_000.0):
        self.now = start

    def time(self):
        self.now += 1.0
        return self.now


# ── 单元：计数 / 消毒 / 上限 ───────────────────────────────────────────

def test_record_sync_counts_and_last_values():
    s = DirectorySyncStats()
    s.record_sync("telegram", "acct1", contacts=12, chats=30)
    s.record_sync("telegram", "acct1", contacts=13, chats=31)
    d = s.dump()
    assert d["total_runs"] == 2
    assert d["total_failures"] == 0
    assert d["overflow"] == 0
    row = d["accounts"]["telegram:acct1"]
    assert row["runs"] == 2
    # last_* 是「最近一轮」快照而非累加
    assert row["last_contacts"] == 13
    assert row["last_chats"] == 31
    assert row["last_sync_ts"] > 0
    assert row["last_failure_ts"] == 0.0
    assert row["last_failure_stage"] == ""
    assert d["last_sync_ts"] == row["last_sync_ts"]


def test_record_failure_counts_separately_from_runs():
    s = DirectorySyncStats()
    s.record_sync("telegram", "acct1", contacts=5, chats=5)
    s.record_failure("telegram", "acct1", "contacts")
    s.record_failure("telegram", "acct1", "dialogs")
    d = s.dump()
    row = d["accounts"]["telegram:acct1"]
    assert row["runs"] == 1            # 失败不吃掉成功轮数
    assert row["failures"] == 2
    assert row["last_failure_stage"] == "dialogs"
    assert row["last_failure_ts"] > 0
    assert row["last_contacts"] == 5   # 失败不清掉上轮快照
    assert d["total_runs"] == 1
    assert d["total_failures"] == 2
    assert d["failures_by_stage"] == {"contacts": 1, "dialogs": 1}


def test_accounts_are_isolated():
    """多账号部署：一个号挂了不该把另一个号的计数带偏。"""
    s = DirectorySyncStats()
    s.record_sync("telegram", "acct1", contacts=10, chats=10)
    s.record_sync("telegram", "acct2", contacts=99, chats=98)
    s.record_failure("telegram", "acct2", "contacts")
    accts = s.dump()["accounts"]
    assert accts["telegram:acct1"] == {
        "runs": 1, "failures": 0, "last_contacts": 10, "last_chats": 10,
        "last_sync_ts": accts["telegram:acct1"]["last_sync_ts"],
        "last_failure_ts": 0.0, "last_failure_stage": "",
    }
    assert accts["telegram:acct2"]["failures"] == 1
    assert accts["telegram:acct2"]["last_contacts"] == 99
    assert accts["telegram:acct1"]["failures"] == 0


def test_platform_dimension_is_kept_for_future_reuse():
    """同 account_id 不同平台不得串味（WA/LINE 后续复用同一口径）。"""
    s = DirectorySyncStats()
    s.record_sync("telegram", "1", contacts=1)
    s.record_sync("whatsapp", "1", contacts=2)
    accts = s.dump()["accounts"]
    assert accts["telegram:1"]["last_contacts"] == 1
    assert accts["whatsapp:1"]["last_contacts"] == 2


def test_account_key_cap_overflows():
    s = DirectorySyncStats()
    for i in range(60):                      # 上限 50
        s.record_sync("telegram", f"acct{i}", contacts=1)
    d = s.dump()
    assert d["total_runs"] == 60
    assert len(d["accounts"]) <= 51          # 50 distinct + __other__
    assert "__other__" in d["accounts"]
    assert d["overflow"] >= 10
    assert d["accounts"]["__other__"]["runs"] == 10


def test_input_sanitization():
    s = DirectorySyncStats()
    s.record_sync("TeleGram!", "acct 1;drop", contacts=3)     # 脏 platform → unknown
    s.record_sync("telegram", "   ", contacts=1)              # 空 account → unknown
    s.record_sync("telegram", "a" * 80, contacts=1)           # 超长 → 截断 48
    s.record_failure("telegram", "acct1", "Not-A-Stage")      # 脏 stage → unknown
    accts = s.dump()["accounts"]
    assert "unknown:acct1drop" in accts                       # 空格/分号被剔除
    assert "telegram:unknown" in accts
    assert f"telegram:{'a' * 48}" in accts
    assert all(" " not in k and ";" not in k for k in accts)
    assert s.dump()["failures_by_stage"] == {"unknown": 1}


def test_non_int_counts_are_coerced():
    """上游把条数传成 None/字符串/负数也不许抛（观测比数据洁癖重要）。"""
    s = DirectorySyncStats()
    s.record_sync("telegram", "acct1", contacts=None, chats="7")
    row = s.dump()["accounts"]["telegram:acct1"]
    assert row["last_contacts"] == 0 and row["last_chats"] == 7
    s.record_sync("telegram", "acct1", contacts=-5, chats="nope")
    row = s.dump()["accounts"]["telegram:acct1"]
    assert row["last_contacts"] == 0 and row["last_chats"] == 0


# ── dump 契约 ─────────────────────────────────────────────────────────

def test_dump_contract_shape():
    s = DirectorySyncStats()
    s.record_sync("telegram", "acct1", contacts=1, chats=2)
    d = s.dump()
    assert set(d) == {"started_at", "last_sync_ts", "total_runs", "total_failures",
                      "overflow", "failures_by_stage", "accounts"}
    assert set(d["accounts"]["telegram:acct1"]) == {
        "runs", "failures", "last_contacts", "last_chats",
        "last_sync_ts", "last_failure_ts", "last_failure_stage"}


def test_dump_orders_accounts_by_last_sync_desc(monkeypatch):
    """看板第一行＝最近同步的号；从没同步过的（ts=0）沉底最扎眼。"""
    monkeypatch.setattr("src.integrations.directory_sync_stats.time", _Clock())
    s = DirectorySyncStats()
    s.record_sync("telegram", "acct1", contacts=1)
    s.record_sync("telegram", "acct2", contacts=1)
    s.record_failure("telegram", "acct9", "contacts")   # 只失败过，last_sync_ts=0
    s.record_sync("telegram", "acct3", contacts=1)
    assert list(s.dump()["accounts"]) == [
        "telegram:acct3", "telegram:acct2", "telegram:acct1", "telegram:acct9"]


# ── Prometheus ────────────────────────────────────────────────────────

def test_dump_prom_shape():
    s = DirectorySyncStats()
    s.record_sync("telegram", "acct1", contacts=12, chats=30)
    s.record_failure("telegram", "acct1", "dialogs")
    txt = s.dump_prom()
    assert "directory_sync_runs_total 1" in txt
    assert "directory_sync_failures_total 1" in txt
    assert 'directory_sync_failures_by_stage_total{stage="dialogs"} 1' in txt
    assert 'directory_sync_contacts{account="telegram:acct1"} 12' in txt
    assert 'directory_sync_chats{account="telegram:acct1"} 30' in txt
    assert 'directory_sync_last_ts{account="telegram:acct1"} ' in txt
    assert 'directory_sync_last_failure_ts{account="telegram:acct1"} ' in txt
    # HELP/TYPE 行齐全，且 counter/gauge 类型标注正确
    for name in ("directory_sync_runs_total", "directory_sync_failures_total",
                 "directory_sync_failures_by_stage_total"):
        assert f"# HELP {name} " in txt
        assert f"# TYPE {name} counter" in txt
    for name in ("directory_sync_contacts", "directory_sync_chats",
                 "directory_sync_last_ts", "directory_sync_last_failure_ts"):
        assert f"# HELP {name} " in txt
        assert f"# TYPE {name} gauge" in txt
    assert txt.endswith("\n")


def test_prom_timestamps_are_integers_not_scientific():
    """epoch 秒必须整数化输出——float 会被格式成 1.7e+09，Prometheus 虽能解析但不可读。"""
    s = DirectorySyncStats()
    s.record_sync("telegram", "acct1")
    line = [l for l in s.dump_prom().splitlines() if l.startswith("directory_sync_last_ts{")][0]
    value = line.rsplit(" ", 1)[1]
    assert value.isdigit() and int(value) > 1_700_000_000


def test_prom_label_escaping():
    assert _esc('a"b\\c') == 'a\\"b\\\\c'
    assert _esc("a\nb") == "a b"
    s = DirectorySyncStats()
    s.record_sync("telegram", 'a"b\\c', contacts=1)   # 消毒已剔除，输出仍须合法
    txt = s.dump_prom()
    for line in txt.splitlines():
        if line.startswith("directory_sync_contacts{"):
            assert line.count('"') == 2


def test_reset_clears():
    s = DirectorySyncStats()
    s.record_sync("telegram", "acct1", contacts=1, chats=1)
    s.record_failure("telegram", "acct1", "contacts")
    s.reset()
    d = s.dump()
    assert d["total_runs"] == 0 and d["total_failures"] == 0
    assert d["overflow"] == 0 and d["accounts"] == {} and d["failures_by_stage"] == {}
    assert d["last_sync_ts"] == 0.0


def test_singleton_is_stable():
    assert get_directory_sync_stats() is get_directory_sync_stats()


# ── 埋点接线（真 sync_directory_once + duck-typed 假 client）────────────
#
# 假 client 与 tests/test_telegram_directory_sync.py 同款构造（pyrogram 只需
# get_contacts / get_dialogs 两个只读 RPC），此处保留本地最小副本以免门禁互相耦合。

class FakeUser:
    def __init__(self, uid, first_name=""):
        self.id = uid
        self.first_name = first_name
        self.last_name = ""
        self.username = ""


class FakeChatType:
    def __init__(self, name):
        self.name = name


class FakeChat:
    def __init__(self, cid, type_name="GROUP", title=""):
        self.id = cid
        self.type = FakeChatType(type_name)
        self.title = title
        self.first_name = ""
        self.last_name = ""
        self.username = ""


class FakeDialog:
    def __init__(self, chat):
        self.chat = chat
        self.top_message = None
        self.unread_messages_count = 0


class FakeClient:
    def __init__(self, contacts=None, dialogs=None):
        self._contacts = list(contacts or [])
        self._dialogs = list(dialogs or [])

    async def get_contacts(self):
        return list(self._contacts)

    def get_dialogs(self, limit=0):
        dialogs = self._dialogs

        async def _gen():
            for d in dialogs[:limit] if limit else dialogs:
                yield d

        return _gen()


@pytest.fixture()
def store_bound(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    protocol_bridge.register_inbox_store_getter(lambda: store)
    try:
        yield store
    finally:
        protocol_bridge.register_inbox_store_getter(None)
        store.close()


async def test_sync_records_stats(store_bound):
    client = FakeClient(
        contacts=[FakeUser(111, "Alice"), FakeUser(222, "Bob")],
        dialogs=[FakeDialog(FakeChat(-100, "GROUP", title="家族群"))],
    )
    stats = await sync_directory_once(client, "acct1", CFG_ON)
    assert stats == {"contacts": 2, "chats": 3}

    d = get_directory_sync_stats().dump()
    assert d["total_runs"] == 1 and d["total_failures"] == 0
    row = d["accounts"]["telegram:acct1"]
    # 埋点数字与函数返回值同源，不得各算各的
    assert row["last_contacts"] == stats["contacts"]
    assert row["last_chats"] == stats["chats"]
    assert row["last_sync_ts"] > 0


async def test_sync_records_stage_failure(store_bound):
    """通讯录段 RPC 挂掉：记 contacts 段失败，但本轮仍算跑过（会话段成功了）。"""

    class BoomContacts(FakeClient):
        async def get_contacts(self):
            raise RuntimeError("FLOOD_WAIT")

    client = BoomContacts(dialogs=[FakeDialog(FakeChat(-100, "GROUP", title="群"))])
    assert await sync_directory_once(client, "acct1", CFG_ON) == {"contacts": 0, "chats": 1}

    d = get_directory_sync_stats().dump()
    assert d["failures_by_stage"] == {"contacts": 1}
    row = d["accounts"]["telegram:acct1"]
    assert row["failures"] == 1 and row["runs"] == 1
    assert row["last_failure_stage"] == "contacts"


async def test_sync_records_dialog_stage_failure(store_bound):
    class BoomDialogs(FakeClient):
        def get_dialogs(self, limit=0):
            raise RuntimeError("FLOOD_WAIT")

    client = BoomDialogs(contacts=[FakeUser(111, "Alice")])
    assert await sync_directory_once(client, "acct1", CFG_ON) == {"contacts": 1, "chats": 0}
    assert get_directory_sync_stats().dump()["failures_by_stage"] == {"dialogs": 1}


async def test_disabled_or_bad_input_records_nothing(store_bound):
    """未启用/无 client 是「没跑」而不是「跑了一轮 0 条」，不得污染 runs。"""
    await sync_directory_once(FakeClient(), "acct1", {**CFG_ON, "enabled": False})
    await sync_directory_once(None, "acct1", CFG_ON)
    d = get_directory_sync_stats().dump()
    assert d["total_runs"] == 0 and d["accounts"] == {}


async def test_stats_failure_never_breaks_sync(store_bound, monkeypatch):
    """硬惯例回归：埋点炸了同步照跑照落库——宁可缺一轮指标，不可缺一轮名单。"""
    import src.integrations.directory_sync_stats as dss

    def _boom():
        raise RuntimeError("stats exploded")

    monkeypatch.setattr(dss, "get_directory_sync_stats", _boom)

    client = FakeClient(
        contacts=[FakeUser(111, "Alice")],
        dialogs=[FakeDialog(FakeChat(-100, "GROUP", title="群"))],
    )
    assert await sync_directory_once(client, "acct1", CFG_ON) == {"contacts": 1, "chats": 2}
    # 落库照常
    assert len(store_bound.list_protocol_contacts("telegram", "acct1")) == 1


async def test_stats_failure_on_failure_path_never_breaks_sync(store_bound, monkeypatch):
    """失败路径上埋点再炸一次也不许把原始异常吞掉的语义改坏（仍返回统计 dict）。"""
    import src.integrations.directory_sync_stats as dss

    def _boom():
        raise RuntimeError("stats exploded")

    monkeypatch.setattr(dss, "get_directory_sync_stats", _boom)

    class BoomContacts(FakeClient):
        async def get_contacts(self):
            raise RuntimeError("FLOOD_WAIT")

    client = BoomContacts(dialogs=[FakeDialog(FakeChat(-100, "GROUP", title="群"))])
    assert await sync_directory_once(client, "acct1", CFG_ON) == {"contacts": 0, "chats": 1}


def test_helpers_swallow_everything():
    """两个 helper 是最后一道闸：任何异常都不得逸出。"""
    telegram_directory_sync._record_sync("acct1", {"contacts": "x"})
    telegram_directory_sync._record_failure("acct1", "contacts")
    assert get_directory_sync_stats().dump()["total_runs"] == 1


# ── metrics 出口端到端 ────────────────────────────────────────────────

def _make_app():
    app = FastAPI()

    @app.middleware("http")
    async def _inject(req: Request, call_next):
        req.scope["session"] = {"role": "admin", "user_id": "u1"}
        return await call_next(req)

    def api_auth(r: Request):
        return True

    register_metrics_route(app, api_auth=api_auth)
    return TestClient(app, raise_server_exceptions=True)


def test_metrics_json_exposes_directory_sync():
    get_directory_sync_stats().record_sync("telegram", "acct1", contacts=7, chats=9)
    m = _make_app().get("/api/workspace/metrics").json()
    ds = m.get("directory_sync")
    assert ds is not None
    assert ds["total_runs"] == 1
    assert ds["accounts"]["telegram:acct1"]["last_contacts"] == 7


def test_metrics_prometheus_includes_directory_sync():
    get_directory_sync_stats().record_sync("telegram", "acct1", contacts=7, chats=9)
    r = _make_app().get("/api/workspace/metrics?format=prometheus")
    assert r.status_code == 200
    assert "directory_sync_runs_total 1" in r.text
    assert 'directory_sync_contacts{account="telegram:acct1"} 7' in r.text
