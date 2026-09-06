# -*- coding: utf-8 -*-
"""#222 幽灵未读第三轮（M-4 A，2026-09-06 证据包 GQREHD / FF5PGS）：角标口径统一 +
群/频道单列 + 存量被埋一次性清理。

事故形态：skuio 机 steven 号头像角标 5 / 账号切换器 5，私聊列表零未读；同号群组 11 /
频道 2 / 已归档 16；看门狗被埋 7→8→9 个、最老 100h；群/频道 ``skip_groups=True``
只进不出天然堆积。J-4 A 做了入口、L-3 B 做了闭环，本轮只钉三件事：

1. **角标 = 当前列表 scope 内能点开的未读**：private / group / channel 三桶互补，
   ``unread_maps(scope=)`` 与 ``unread_conversations(scope=)`` 同一份 WHERE；
   四桶明细 ``unread_breakdown`` 让悬浮能说清「另有 群 N / 频道 N / 归档 N」。
2. **被埋清单与横幅 / 主徽标同口径**：``buried_conversations`` 私聊 + 有效未读 +
   可见消息；看门狗与 ``/api/admin/buried-conversations`` 优先走它，旧 store 回落。
3. **存量一次性清理**：``sweep_buried_unread`` 装载时只清「人工归档 + 无入站 > 72h」，
   <72h 的与自动归档的留给横幅；横幅「全部标已读」按视角清全部。
"""
from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from src.inbox import health_watchdog as hw
from src.inbox.store import InboxConversation, InboxMessage, InboxStore
from src.inbox.unread_aggregate import (
    buried_conversations,
    normalize_scope,
    sweep_buried_unread,
    unread_breakdown,
    unread_conversations,
    unread_maps,
)

_ENGINE_ROOT = Path(__file__).resolve().parents[1]
_TPL = _ENGINE_ROOT / "src/web/templates/unified_inbox.html"
_I18N = _ENGINE_ROOT / "src/web/i18n_packs/inbox_workspace.py"

H = 3600.0


def _conv(cid: str, *, unread: int, last_ts: float, chat_type: str = "private",
          platform: str = "telegram", account: str = "acc",
          chat_key: str = "") -> InboxConversation:
    ck = chat_key or cid.split(":", 2)[-1]
    return InboxConversation(
        conversation_id=cid, platform=platform, account_id=account,
        chat_key=ck, display_name=f"客户{ck}", last_text="hi",
        last_ts=last_ts, unread=unread, chat_type=chat_type,
    )


def _msg(cid: str, mid: str, ts: float, direction: str = "in") -> InboxMessage:
    return InboxMessage(conversation_id=cid, platform_msg_id=mid,
                        direction=direction, text="hello", ts=ts)


def _put(store: InboxStore, cid: str, *, unread: int, last_ts: float, **kw) -> None:
    """一条带可见入站消息的会话（能进清单＝能进角标）。"""
    store.ingest_batch(_conv(cid, unread=unread, last_ts=last_ts, **kw),
                       [_msg(cid, "m-" + cid, last_ts)])


def _set_auto_archived(store: InboxStore, cid: str, ts: float) -> None:
    with store._lock:                                        # noqa: SLF001
        store._conn.execute(                                 # noqa: SLF001
            "UPDATE conversation_meta SET auto_archived_at=? WHERE conversation_id=?",
            (ts, cid))
        store._conn.commit()                                 # noqa: SLF001


@pytest.fixture
def store(tmp_path):
    s = InboxStore(tmp_path / "inbox.db")
    yield s
    s.close()


@pytest.fixture
def steven(store):
    """GQREHD 形态：私聊 5 / 群 11 / 频道 2 / 归档私聊 16。"""
    now = time.time()
    _put(store, "telegram:acc:p1", unread=3, last_ts=now - 5 * H)
    _put(store, "telegram:acc:p2", unread=2, last_ts=now - 6 * H)
    # 群：chat_type=group 的 ×2（6+4）+ legacy（chat_type 顶着 private、Telegram 负号键）×1（1）
    _put(store, "telegram:acc:-5011147281", unread=6, last_ts=now - 1 * H, chat_type="group")
    _put(store, "telegram:acc:-5011147282", unread=4, last_ts=now - 2 * H, chat_type="group")
    _put(store, "telegram:acc:-1001234", unread=1, last_ts=now - 3 * H, chat_type="private")
    _put(store, "telegram:acc:ch1", unread=2, last_ts=now - 4 * H, chat_type="channel")
    _put(store, "telegram:acc:a1", unread=10, last_ts=now - 100 * H)
    _put(store, "telegram:acc:a2", unread=6, last_ts=now - 30 * H)
    store.set_conv_archived("telegram:acc:a1", True)
    store.set_conv_archived("telegram:acc:a2", True)
    return store


# ══ 1. 角标 = 当前 scope 内能点开的未读；三桶互补 ═══════════════════════════

def test_badge_follows_scope_and_matches_drilldown(steven):
    for scope, want in (("private", 5), ("group", 11), ("channel", 2)):
        maps = unread_maps(steven, scope=scope)
        assert maps is not None
        assert maps[0].get("telegram:acc", 0) == want, (scope, maps)
        convs = unread_conversations(steven, "telegram", "acc", scope=scope)
        assert sum(c["unread"] for c in convs) == want, (scope, convs)
    # 群视图点角标看到的是群（含 legacy 负号键那条），一条私聊都不混进来
    g = unread_conversations(steven, "telegram", "acc", scope="group")
    assert {c["chat_key"] for c in g} == {"-5011147281", "-5011147282", "-1001234"}
    # 缺省 scope 仍是主徽标口径（旧调用方零改动）
    assert unread_maps(steven)[0] == {"telegram:acc": 5}


def test_breakdown_four_buckets_partition(steven):
    bd = unread_breakdown(steven)
    assert bd == {"telegram:acc": {"private": 5, "group": 11, "channel": 2, "archived": 16}}
    # 四桶各自与按 scope 的聚合恒等（同一份 WHERE，不许两套口径）
    for scope in ("private", "group", "channel"):
        assert bd["telegram:acc"][scope] == unread_maps(steven, scope=scope)[0]["telegram:acc"]
    assert bd["telegram:acc"]["archived"] == unread_maps(
        steven, include_archived=True)[0]["telegram:acc"] - bd["telegram:acc"]["private"]


def test_legacy_group_key_never_lands_in_private(store):
    now = time.time()
    _put(store, "telegram:acc:-100777", unread=3, last_ts=now - H, chat_type="")
    _put(store, "line:acc:U1:group:G9", unread=2, last_ts=now - H, chat_type="private",
         platform="line", chat_key="U1:group:G9")
    assert unread_maps(store, scope="private") == ({}, {})
    bd = unread_breakdown(store)
    assert bd["telegram:acc"]["group"] == 3 and bd["telegram:acc"]["private"] == 0
    assert bd["line:acc"]["group"] == 2 and bd["line:acc"]["private"] == 0


def test_breakdown_respects_listable_gate(store):
    """占位残值（零可见消息）与墓碑会话在四桶里同样不算——与主徽标同一闸门。"""
    now = time.time()
    store.upsert_protocol_chats("telegram", "acc", [
        {"chat_key": "-999", "unread": 7, "ts": now - H}])   # 群占位、无消息
    _put(store, "telegram:acc:dead", unread=2, last_ts=now - H)
    store.delete_conversation_data("telegram:acc:dead", deleted_by="agent")
    assert unread_breakdown(store) == {}


def test_normalize_scope_falls_back_to_private():
    assert normalize_scope("group") == "group"
    assert normalize_scope("CHANNEL ") == "channel"
    for bad in ("", None, "all", "archived", 42):
        assert normalize_scope(bad) == "private"


# ══ 2. 被埋清单与横幅 / 主徽标同口径 ═══════════════════════════════════════

def test_buried_list_is_private_effective_and_visible_only(steven):
    now = time.time()
    # 归档的群（skip_groups 只进不出的堆积）与归档占位（无消息）都不该算「客户在等」
    _put(steven, "telegram:acc:-777", unread=9, last_ts=now - 50 * H, chat_type="group")
    steven.set_conv_archived("telegram:acc:-777", True)
    steven.upsert_protocol_chats("telegram", "acc", [
        {"chat_key": "ph", "unread": 5, "ts": now - 60 * H}])
    steven.set_conv_archived("telegram:acc:ph", True)
    rows = buried_conversations(steven)
    assert rows is not None
    assert [r["conversation_id"] for r in rows] == ["telegram:acc:a1", "telegram:acc:a2"]
    assert sum(r["unread"] for r in rows) == unread_breakdown(steven)["telegram:acc"]["archived"] == 16
    # 行形状兼容 list_buried_archived 的消费方（看门狗 / admin 端点读这些键）
    for k in ("conversation_id", "platform", "account_id", "display_name", "unread",
              "last_ts", "archived_at", "auto_archived_at", "last_in_ts"):
        assert k in rows[0], k
    # 旧口径把群 / 占位都算进去（正是「被埋 9 个」与横幅 N 条各说各话的来源）
    assert len(steven.list_buried_archived()) == 4


def test_buried_list_returns_none_for_stub_store():
    """测试桩 / 旧 store（无 _conn）→ None，调用方回落 list_buried_archived，绝不抛。"""
    stub = SimpleNamespace(list_buried_archived=lambda **k: [])
    assert buried_conversations(stub) is None
    assert unread_breakdown(stub) is None
    assert sweep_buried_unread(stub) == 0


# ══ 3. 存量一次性清理 ═══════════════════════════════════════════════════════

def test_startup_sweep_clears_only_manual_archived_idle_over_72h(steven, caplog):
    now = time.time()
    # 再补一条自动归档、200h 无入站的：manual_only 下不动
    _put(steven, "telegram:acc:auto", unread=4, last_ts=now - 200 * H)
    steven.set_conv_archived("telegram:acc:auto", True)
    _set_auto_archived(steven, "telegram:acc:auto", now - 199 * H)
    before = {r["conversation_id"] for r in buried_conversations(steven)}
    assert before == {"telegram:acc:a1", "telegram:acc:a2", "telegram:acc:auto"}

    with caplog.at_level(logging.INFO, logger="src.inbox.unread_aggregate"):
        n = sweep_buried_unread(steven, min_idle_sec=72 * H, manual_only=True,
                                now=now, reason="startup_sweep")
    assert n == 1                                            # 只有 a1（100h、人工）
    after = buried_conversations(steven)
    assert {r["conversation_id"] for r in after} == {"telegram:acc:a2", "telegram:acc:auto"}
    # 清的是「已读水位」而不是删数据：会话与消息原样在，横幅 / 徽标口径下它不再算未读
    assert steven.get_conversation("telegram:acc:a1") is not None
    assert steven.count_messages("telegram:acc:a1") == 1
    assert unread_breakdown(steven)["telegram:acc"]["archived"] == 6 + 4
    assert any("[buried] startup_sweep cleared 1" in r.getMessage() for r in caplog.records)
    # 幂等：再跑一次没有可清的
    assert sweep_buried_unread(steven, min_idle_sec=72 * H, manual_only=True, now=now) == 0


def test_banner_mark_read_clears_current_view_regardless_of_age(steven):
    now = time.time()
    _put(steven, "whatsapp:w1:x", unread=3, last_ts=now - 2 * H, platform="whatsapp",
         account="w1")
    steven.set_conv_archived("whatsapp:w1:x", True)
    n = sweep_buried_unread(steven, platform="telegram", account_id="acc",
                            reason="banner_mark_read")
    assert n == 2                                            # a1 + a2，不看 72h
    left = buried_conversations(steven)
    assert [r["conversation_id"] for r in left] == ["whatsapp:w1:x"]   # 别的账号不动
    assert unread_breakdown(steven).get("telegram:acc", {}).get("archived", 0) == 0
    # 主徽标（私聊未归档）不受影响
    assert unread_maps(steven)[0] == {"telegram:acc": 5}


# ══ 4. 看门狗接线 ═══════════════════════════════════════════════════════════

class _Bus:
    def __init__(self) -> None:
        self.events: List[tuple] = []

    def publish(self, name: str, payload: Dict[str, Any]) -> None:
        self.events.append((name, payload))


def _watchdog(monkeypatch, store, conf: Dict[str, Any]):
    bus = _Bus()
    monkeypatch.setattr("src.integrations.shared.event_bus.get_event_bus", lambda: bus)
    state = SimpleNamespace(inbox_store=store)
    w = hw.HealthWatchdog.__new__(hw.HealthWatchdog)
    w._app = state
    w._config_manager = SimpleNamespace(config=conf)
    w._bc_alerted = False
    w._bc_last_remind = 0.0
    w._hygiene_done = False
    w.total_buried_conv_alerts = 0
    return w, bus, state


def test_watchdog_startup_hygiene_runs_buried_sweep(monkeypatch, steven, caplog):
    w, _bus, _state = _watchdog(monkeypatch, steven,
                                {"health_watchdog": {"buried_conv_remind": {"enabled": True}}})
    with caplog.at_level(logging.INFO):
        w._startup_hygiene()
    assert w._hygiene_done is True
    assert {r["conversation_id"] for r in buried_conversations(steven)} == {"telegram:acc:a2"}
    assert any("startup_sweep cleared 1" in r.getMessage() for r in caplog.records)


def test_watchdog_sweep_hours_zero_disables(monkeypatch, steven):
    w, _bus, _state = _watchdog(monkeypatch, steven, {
        "health_watchdog": {"buried_conv_remind": {"enabled": True, "sweep_hours": 0}}})
    w._startup_hygiene()
    assert {r["conversation_id"] for r in buried_conversations(steven)} == {
        "telegram:acc:a1", "telegram:acc:a2"}


def test_watchdog_buried_check_counts_private_only(monkeypatch, steven):
    """归档群的堆积不再被报成「客户在等」：通知中心文案的 N 与横幅同口径。"""
    now = time.time()
    _put(steven, "telegram:acc:-777", unread=9, last_ts=now - 50 * H, chat_type="group")
    steven.set_conv_archived("telegram:acc:-777", True)
    w, bus, state = _watchdog(monkeypatch, steven,
                              {"health_watchdog": {"buried_conv_remind": {"enabled": True}}})
    w._check_buried_conversations(now=now)
    got = [n for n in (getattr(state, "notif_queue", None) or [])
           if n.get("type") == "sys_status" and (n.get("data") or {}).get("id") == "buried_conv"]
    assert len(got) == 1
    txt = got[0]["data"]["text"]
    assert "有 16 条未读在 2 个已归档会话里" in txt, txt      # 不是 25 条 / 3 个
    alert = [p for (name, p) in bus.events if name == "buried_conv_alert"]
    assert alert and alert[0]["buried_count"] == 2 and alert[0]["total_unread"] == 16


def test_watchdog_falls_back_to_legacy_rows_for_stub_store(monkeypatch):
    """既有测试桩（SimpleNamespace 只带 list_buried_archived）路径不变。"""
    rows = [{"conversation_id": "c1", "platform": "whatsapp", "account_id": "a",
             "chat_key": "c1", "display_name": "x", "unread": 3,
             "last_ts": time.time() - 86 * H, "last_text": "?",
             "archived_at": time.time() - 87 * H, "auto_archived_at": 0.0}]
    stub = SimpleNamespace(list_buried_archived=lambda **k: list(rows))
    w, bus, state = _watchdog(monkeypatch, stub,
                              {"health_watchdog": {"buried_conv_remind": {"enabled": True}}})
    w._check_buried_conversations(now=time.time())
    alert = [p for (name, p) in bus.events if name == "buried_conv_alert"]
    assert alert and alert[0]["buried_count"] == 1 and alert[0]["total_unread"] == 3


# ══ 5. 路由 / 前端接线（静态） ═══════════════════════════════════════════════

def test_read_routes_ai_skip_groups_reads_autodraft_key():
    from src.web.routes.unified_inbox_read_routes import _ai_skip_groups
    on = SimpleNamespace(config={"inbox": {"auto_draft": {"skip_group_chats": True}}})
    off = SimpleNamespace(config={"inbox": {"auto_draft": {}}})
    assert _ai_skip_groups(on) is True
    assert _ai_skip_groups(off) is False
    assert _ai_skip_groups(None) is False


def test_template_wires_scope_breakdown_and_buried_read():
    src = _TPL.read_text(encoding="utf-8")
    # 浮层与角标同一 scope
    assert "/api/unified-inbox/account-unread?platform=" in src
    assert "&scope='+encodeURIComponent(scope)" in src
    # 四桶明细进入状态 + 角标数按 scope 取
    assert "unreadBreakdownByAccount=d.unread_breakdown_by_account" in src
    assert "function _scopedAcctUnread(k)" in src
    assert "_acUnreadTitle(plat,aid)" in src
    # 被埋存量清理入口（横幅第二动作）+ 群/频道说明条
    assert 'data-bh-read="1"' in src
    assert "/api/unified-inbox/buried-mark-read" in src
    assert 'id="scope-note"' in src and "function _renderScopeNote()" in src
    assert "_aiSkipGroups=d.ai_skip_groups" in src
    # 主题窗口级钉子不变量（L7–19）未被本轮推翻
    assert "绝不写" in src[:4000] and "localStorage(cp_theme)" in src[:4000]


def test_i18n_keys_present_zh_and_en():
    txt = _I18N.read_text(encoding="utf-8")
    for key in ("inbox.buried.read_all", "inbox.buried.read_t", "inbox.buried.read_ok",
                "inbox.buried.read_fail", "inbox.acct.bd_title", "inbox.acct.bd_others",
                "inbox.acct.bd_private", "inbox.acct.bd_group", "inbox.acct.bd_channel",
                "inbox.acct.bd_archived", "inbox.acct.bd_scope_private",
                "inbox.acct.bd_scope_group", "inbox.acct.bd_scope_channel",
                "inbox.scope.ai_skip_note", "inbox.scope.ai_skip_note_t"):
        assert len(re.findall(re.escape(f'"{key}"'), txt)) == 2, key   # zh + en 各一
