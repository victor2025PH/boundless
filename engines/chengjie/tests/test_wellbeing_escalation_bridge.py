# -*- coding: utf-8 -*-
"""R8 危机升级 → 工作台落点桥（#185 决策 D7，2026-09-05）。

契约：
1. ``escalation_needed(category=crisis)`` 的事件（severe + escalated）落库后，工作台三处
   **同时**亮：会话打 HANDOFF_TAG（「需人工」红徽标同源）+ handoff_meta 可解释 +
   会话置顶 + ``escalations`` 表落一条案例；
2. elevated / 未升级的 severe 只留痕**不叫人**（否则每条负面情绪都钉顶＝徽标失去意义）；
3. 推路径：``CrisisEventStore.record()`` 落库即同步进桥——**skill_manager 零改动**；
4. 扫路径：看门狗 ``sweep`` 只处理启动水位以上的事件（不追溯陈年事件），与推路径按
   event id 去重（同一事件两路都到＝只落一次）；
5. 反查不到工作台会话 → 如实 ``unresolved``，不猜、不抛；所有异常吞掉不反噬主回复；
6. 配置出厂值：cloud_light / desktop.internal 两个 profile 都把留痕 + 升级打开。
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest
import yaml

from src.inbox.models import InboxConversation
from src.inbox.store import InboxStore
from src.integrations.protocol_autoreply import HANDOFF_TAG
from src.utils.crisis_event_store import (
    CrisisEventStore,
    add_crisis_event_listener,
    remove_crisis_event_listener,
)
from src.companion.wellbeing_escalation_bridge import (
    CRISIS_SOURCE,
    CrisisEscalationBridge,
    apply_crisis_escalation,
    resolve_conversation,
    should_bridge,
)

_ENGINE = Path(__file__).resolve().parents[1]


def _mk_store(tmp_path: Path) -> InboxStore:
    store = InboxStore(tmp_path / "inbox.db")
    store.upsert_conversation(InboxConversation(
        conversation_id="telegram:acc1:555", platform="telegram", account_id="acc1",
        chat_key="555", display_name="A", last_ts=time.time() - 60,
    ))
    # 同 chat_key 的另一个账号会话：更早活跃 → 反查应取最近那条
    store.upsert_conversation(InboxConversation(
        conversation_id="telegram:acc0:555", platform="telegram", account_id="acc0",
        chat_key="555", display_name="A-old", last_ts=time.time() - 86400,
    ))
    return store


def _severe(**kw):
    ev = {"id": 1, "user_id": "555", "chat_id": "555", "level": "severe",
          "category": "self_harm", "streak": 2, "escalated": True}
    ev.update(kw)
    return ev


# ── 纯函数 ────────────────────────────────────────────────────────────────

def test_should_bridge_only_severe_escalated():
    assert should_bridge(_severe()) is True
    assert should_bridge(_severe(escalated=False)) is False
    assert should_bridge(_severe(level="elevated")) is False
    assert should_bridge({}) is False
    assert should_bridge(None) is False  # type: ignore[arg-type]


def test_resolve_conversation_prefers_most_recent(tmp_path):
    store = _mk_store(tmp_path)
    conv = resolve_conversation(store, _severe())
    assert conv and conv["conversation_id"] == "telegram:acc1:555"
    # chat_id 优先于 user_id
    store.upsert_conversation(InboxConversation(
        conversation_id="line:acc1:U9", platform="line", account_id="acc1",
        chat_key="U9", last_ts=time.time(),
    ))
    conv = resolve_conversation(store, _severe(chat_id="U9", user_id="555"))
    assert conv["conversation_id"] == "line:acc1:U9"
    assert resolve_conversation(store, _severe(chat_id="nope", user_id="nope")) is None
    assert resolve_conversation(None, _severe()) is None


# ── 三落点 ────────────────────────────────────────────────────────────────

def test_apply_marks_badge_pin_and_case(tmp_path):
    store = _mk_store(tmp_path)
    now = time.time()
    res = apply_crisis_escalation(store, _severe(), now=now)
    assert res["bridged"] is True and res["reason"] == "ok"
    cid = res["conversation_id"]
    assert cid == "telegram:acc1:555"
    # 1) 需人工徽标 + 可解释元数据
    assert res["tagged"] is True
    assert HANDOFF_TAG in store.get_conv_tags(cid)
    meta = store.get_handoff_meta(cid)
    assert meta.get("reason") == "crisis:self_harm"
    assert meta.get("source") == CRISIS_SOURCE
    # 2) 置顶
    assert res["pinned"] is True
    pinned = [r["conversation_id"] for r in store.list_pinned_conversations()]
    assert cid in pinned
    # 3) 案例跟进
    assert res["escalation_recorded"] is True
    escs = store.list_escalations(since_ts=now - 10)
    assert len(escs) == 1
    assert escs[0]["conversation_id"] == cid
    assert escs[0]["reason"] == "crisis:self_harm"


def test_apply_is_idempotent_within_dedup_window(tmp_path):
    store = _mk_store(tmp_path)
    now = time.time()
    apply_crisis_escalation(store, _severe(), now=now)
    res2 = apply_crisis_escalation(store, _severe(id=2), now=now + 5)
    assert res2["bridged"] is True
    assert res2["tagged"] is False, "已打过 HANDOFF_TAG 不重复打（不算失败）"
    assert res2["escalation_recorded"] is False, "去重窗内同会话不重复入案"
    assert len(store.list_escalations(since_ts=now - 10)) == 1
    assert store.get_conv_tags(res2["conversation_id"]).count(HANDOFF_TAG) == 1


def test_apply_skips_non_escalated_and_unresolved(tmp_path):
    store = _mk_store(tmp_path)
    r = apply_crisis_escalation(store, _severe(level="elevated", escalated=False))
    assert r["bridged"] is False and r["reason"] == "skipped_not_escalated"
    assert store.list_escalations() == []
    r = apply_crisis_escalation(store, _severe(user_id="ghost", chat_id="ghost"))
    assert r["bridged"] is False and r["reason"] == "unresolved"
    assert store.list_escalations() == []
    r = apply_crisis_escalation(None, _severe())
    assert r["reason"] == "no_store"


def test_apply_swallows_store_exceptions(tmp_path):
    class Boom:
        def find_conversations_by_chat_key(self, *a, **k):
            return [{"conversation_id": "x:y:z", "platform": "x", "account_id": "y",
                     "chat_key": "z", "last_ts": 1.0}]

        def get_conv_tags(self, cid):
            raise RuntimeError("db down")

        def set_conv_tags(self, cid, tags):
            raise RuntimeError("db down")

        def set_conversation_pinned(self, cid, pinned):
            raise RuntimeError("db down")

        def record_escalation(self, *a, **k):
            raise RuntimeError("db down")

    res = apply_crisis_escalation(Boom(), _severe())
    # 旁路：不抛，只如实报三处都没落成
    assert res["bridged"] is True
    assert res["tagged"] is False and res["pinned"] is False
    assert res["escalation_recorded"] is False


# ── 推路径：record() → 监听器 → 工作台（skill_manager 零改动）────────────────

def test_record_pushes_through_listener_end_to_end(tmp_path):
    inbox = _mk_store(tmp_path)
    crisis = CrisisEventStore(tmp_path / "crisis.db")
    bridge = CrisisEscalationBridge(lambda: inbox)
    assert bridge.install() is True
    assert bridge.install() is False, "幂等安装"
    try:
        eid = crisis.record(user_id="555", chat_id="555", level="severe",
                            category="self_harm", streak=2, escalated=True,
                            excerpt="我不想活了")
        assert eid
        assert bridge.stats["pushed"] == 1 and bridge.stats["bridged"] == 1
        assert HANDOFF_TAG in inbox.get_conv_tags("telegram:acc1:555")
        assert inbox.list_escalations()
        # elevated 只留痕不叫人
        crisis.record(user_id="555", level="elevated", excerpt="好绝望")
        assert bridge.stats["pushed"] == 2 and bridge.stats["skipped"] == 1
        assert len(inbox.list_escalations()) == 1
    finally:
        bridge.uninstall()


def test_listener_exception_never_breaks_record(tmp_path):
    crisis = CrisisEventStore(tmp_path / "crisis.db")

    def boom(ev):
        raise RuntimeError("listener exploded")

    add_crisis_event_listener(boom)
    try:
        eid = crisis.record(user_id="u", level="severe", escalated=True)
        assert eid is not None, "监听器炸了 record 仍成功（主回复不受旁路牵连）"
        assert crisis.count() == 1
    finally:
        remove_crisis_event_listener(boom)


# ── 扫路径：水位 + 与推路径按 id 去重 ─────────────────────────────────────

def test_sweep_sets_high_water_first_then_only_new(tmp_path):
    inbox = _mk_store(tmp_path)
    crisis = CrisisEventStore(tmp_path / "crisis.db")
    # 陈年事件：监听器/桥都还没装时就落了库
    crisis.record(user_id="555", chat_id="555", level="severe", escalated=True)
    bridge = CrisisEscalationBridge(lambda: inbox)
    assert bridge.sweep(crisis) == [], "首扫只立水位，不追溯历史"
    assert HANDOFF_TAG not in inbox.get_conv_tags("telegram:acc1:555")
    # 水位之后来的事件（模拟监听器缺席）→ 补扫捞到
    crisis.record(user_id="555", chat_id="555", level="severe", escalated=True)
    crisis.record(user_id="555", chat_id="555", level="elevated")  # 不该被 only_escalated 捞
    res = bridge.sweep(crisis)
    assert len(res) == 1 and res[0]["bridged"] is True
    assert bridge.stats["swept"] == 1
    assert HANDOFF_TAG in inbox.get_conv_tags("telegram:acc1:555")
    # 再扫无新事件
    assert bridge.sweep(crisis) == []


def test_push_and_sweep_dedup_same_event(tmp_path):
    inbox = _mk_store(tmp_path)
    crisis = CrisisEventStore(tmp_path / "crisis.db")
    bridge = CrisisEscalationBridge(lambda: inbox)
    bridge.sweep(crisis)          # 立水位 = 0
    bridge.install()
    try:
        crisis.record(user_id="555", chat_id="555", level="severe", escalated=True)
        assert bridge.stats["pushed"] == 1
        # 同一事件再被扫到 → duplicate，不重复入账
        res = bridge.sweep(crisis)
        assert res == []
        assert bridge.stats["swept"] == 0
        assert len(inbox.list_escalations()) == 1
    finally:
        bridge.uninstall()


def test_bridge_store_getter_resolved_lazily(tmp_path):
    """bootstrap 顺序无关：桥先建、store 后到。"""
    holder = {"store": None}
    crisis = CrisisEventStore(tmp_path / "crisis.db")
    bridge = CrisisEscalationBridge(lambda: holder["store"])
    bridge.install()
    try:
        crisis.record(user_id="555", chat_id="555", level="severe", escalated=True)
        assert bridge.stats["skipped"] == 1   # no_store，如实
        holder["store"] = _mk_store(tmp_path)
        crisis.record(user_id="555", chat_id="555", level="severe", escalated=True)
        assert bridge.stats["bridged"] == 1
    finally:
        bridge.uninstall()


# ── 看门狗接线（静态）+ 出厂配置（D7）──────────────────────────────────────

def test_watchdog_wires_bridge_check():
    src = (_ENGINE / "src" / "inbox" / "health_watchdog.py").read_text(encoding="utf-8")
    assert "self._check_crisis_escalation_bridge()" in src
    assert "def _check_crisis_escalation_bridge" in src
    assert "CrisisEscalationBridge(self._inbox)" in src
    assert "bridge.install()" in src and "bridge.sweep(crisis_store)" in src


class _FakeWatchdog:
    """只带 `_check_crisis_escalation_bridge` 依赖面的假看门狗（真调用测试用）。"""

    def __init__(self, inbox, sm, cfg):
        self._app = type("App", (), {})()
        self._app.state = type("S", (), {})()
        self._app.state.skill_manager = sm
        self._config_manager = type("CM", (), {"config": cfg})()
        self.__inbox = inbox

    def _inbox(self):
        return self.__inbox


def test_watchdog_check_actually_runs_and_bridges(tmp_path):
    """真把 ``_check_crisis_escalation_bridge`` 跑起来（源码断言抓不到缺导入/改名）。

    首 tick 只立水位（不追溯历史）→ 新落库的 severe+escalated 事件由推路径即时进桥；
    下一 tick 补扫不重复；累计计数器与 install 幂等由此一并验证。
    """
    from src.inbox.health_watchdog import HealthWatchdog

    store = _mk_store(tmp_path)
    crisis = CrisisEventStore(tmp_path / "crisis.db")
    sm = type("SM", (), {})()
    sm._crisis_store = crisis
    wd = _FakeWatchdog(store, sm, {"companion": {"wellbeing": {"enabled": True}}})
    wd._skill_manager = lambda: sm
    wd._crisis_bridge = lambda: HealthWatchdog._crisis_bridge(wd)

    HealthWatchdog._check_crisis_escalation_bridge(wd)          # 首 tick：装监听 + 立水位
    bridge = wd._crisis_escalation_bridge
    try:
        assert bridge._installed is True
        assert wd.total_crisis_bridge_applied == 0
        crisis.record(user_id="555", chat_id="555", level="severe",
                      category="self_harm", escalated=True)     # 推路径落点
        assert HANDOFF_TAG in store.get_conv_tags("telegram:acc1:555")
        HealthWatchdog._check_crisis_escalation_bridge(wd)      # 次 tick：补扫不重复
        assert bridge.stats["bridged"] == 1
        assert bridge._installed is True                        # install 幂等
        # 关 wellbeing 总闸 → 巡检直接返回，不装不扫
        wd2 = _FakeWatchdog(store, sm, {"companion": {"wellbeing": {"enabled": False}}})
        wd2._crisis_bridge = lambda: (_ for _ in ()).throw(AssertionError("不该建桥"))
        HealthWatchdog._check_crisis_escalation_bridge(wd2)
    finally:
        bridge.uninstall()


@pytest.mark.parametrize("rel", [
    "config/profiles/cloud_light.yaml",
    "config/config.desktop.internal.yaml",
])
def test_profiles_ship_audit_and_escalation_on(rel):
    cfg = yaml.safe_load((_ENGINE / rel).read_text(encoding="utf-8")) or {}
    wb = ((cfg.get("companion") or {}).get("wellbeing") or {})
    assert wb.get("crisis_audit") is True, f"{rel}: 留痕必须出厂开（D7）"
    assert wb.get("crisis_escalation") is True, f"{rel}: 人工升级必须出厂开（D7）"
    assert int(wb.get("escalate_after", 0)) >= 1


def test_excerpt_capped_at_120_chars(tmp_path):
    crisis = CrisisEventStore(tmp_path / "crisis.db")
    crisis.record(user_id="u", level="severe", excerpt="我" * 500)
    row = crisis.list_recent(limit=1)[0]
    assert len(row["excerpt"]) <= 120, "留痕只存短摘要（隐私边界）"
