# -*- coding: utf-8 -*-
"""M-7 B（#236 = #166 族第五次复报）：注入不断档 + 日志无死角。

取证（发版对账_v1.0.77_M7 §2 问 1）：skuio 机 3h 59 稿零 ``[goal-inject]`` 行——不是
disabled 走 DEBUG、也不是没调注入，而是 ``src/companion/goals/*`` 全包 logger 用根级
独立名（GoalService / GoalSprintTicker / GoalStore …）：``logging_setup`` 把 root 钉在
WARNING、只给 ``src.*`` / ``ai_chat_assistant.*`` 放 INFO → 本包 INFO 在
``isEnabledFor`` 就被丢，handler 都没碰到。J-2 A2 的测试用
``caplog.at_level(INFO, logger="GoalService")`` 显式抬级所以全绿。

本文件钉：
1. 生产日志装配形态复现（root=WARNING + src 挂 INFO handler）下，注入口每稿必出一行；
2. goals 包全部 logger 都在 ``src.companion.goals.`` 命名空间；
3. disabled / no_goal / goal_expired 都是 INFO，60s 节流按「原因|会话」；
4. 被动注入（客户来消息）不受拍数上限限制，主动侧超额记 beat_blocked(pace_cap)；
5. 目标创建 / 状态变更落 ``[goal-state]`` INFO。
"""
from __future__ import annotations

import glob
import logging
import re
import time
from types import SimpleNamespace

from src.companion.goals import service
from src.companion.goals.pace import slot_key
from src.companion.goals.service import build_block_for_chat, refresh_goal
from src.companion.goals.store import GoalStore, get_goal_store, reset_goal_store

CONV = "whatsapp:17345893506:13308422244"
PLAT, ACCT, CK = "whatsapp", "17345893506", "13308422244"
NOW = time.time()


def _cfg(goals_cfg=None):
    base = {"enabled": True, "db_path": ":memory:"}
    if goals_cfg:
        base.update(goals_cfg)
    return SimpleNamespace(config={"companion": {"goals": base}}, config_path=None)


def setup_function(_fn):
    reset_goal_store()
    service._inject_log_seen.clear()


def teardown_function(_fn):
    reset_goal_store()


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.INFO)
        self.records = []

    def emit(self, record):
        self.records.append(record)


class _ProdLogging:
    """复刻 src/bootstrap/logging_setup.py 的装配：root 钉 WARNING，只有 ``src``
    logger 挂 INFO handler 且 propagate=False。进入时清掉 goals 各 logger 的显式级别
    （caplog 可能残留），退出全部还原。"""

    def __enter__(self):
        self.root = logging.getLogger()
        self.root_level = self.root.level
        self.root.setLevel(logging.WARNING)
        self.src = logging.getLogger("src")
        self.src_level, self.src_prop = self.src.level, self.src.propagate
        self.src.setLevel(logging.INFO)
        self.src.propagate = False
        self.h = _Capture()
        self.src.addHandler(self.h)
        self.saved = {}
        for name in list(logging.Logger.manager.loggerDict):
            if name.startswith("src.companion.goals") or name.startswith("Goal"):
                lg = logging.getLogger(name)
                self.saved[name] = lg.level
                lg.setLevel(logging.NOTSET)
        return self.h

    def __exit__(self, *exc):
        self.src.removeHandler(self.h)
        self.src.setLevel(self.src_level)
        self.src.propagate = self.src_prop
        self.root.setLevel(self.root_level)
        for name, lvl in self.saved.items():
            logging.getLogger(name).setLevel(lvl)
        return False


def _msgs(h, needle):
    return [r.getMessage() for r in h.records if needle in r.getMessage()]


# ── 1/2. 生产装配下每稿一行；全包 logger 命名空间 ─────────────────────────────
def test_goals_package_loggers_live_under_src_namespace():
    bad = []
    for p in glob.glob("src/companion/goals/*.py"):
        src = open(p, encoding="utf-8").read()
        for m in re.finditer(r'logging\.getLogger\("([^"]+)"\)', src):
            name = m.group(1)
            if not (name.startswith("src.companion.goals.")
                    or name.startswith("ai_chat_assistant.")):
                bad.append((p, name))
    assert not bad, f"根级 logger 名在生产里 INFO 全丢（root=WARNING）：{bad}"


def test_inject_logs_reach_src_handler_under_production_logging():
    """就是 skuio 机的形态：goals 关着 → 以前 DEBUG+根级名双重死角，现在一行 INFO。"""
    with _ProdLogging() as h:
        build_block_for_chat(
            _cfg({"enabled": False}), platform=PLAT, chat_key=CK, account_id=ACCT,
            conversation_id=CONV, user_context={}, chain="draft", inbound_text="hi",
            now=NOW)
        get_goal_store(":memory:")
        build_block_for_chat(
            _cfg(), platform=PLAT, chat_key=CK, account_id=ACCT,
            conversation_id=CONV, user_context={}, chain="draft", inbound_text="hi",
            now=NOW)
    assert _msgs(h, "[goal-inject] NOT injected reason=disabled")
    ng = _msgs(h, "[goal-inject] NOT injected reason=no_goal")
    assert ng and f"conv={CONV}" in ng[0] and "chain=draft" in ng[0]
    assert "last_goal=none" in ng[0]
    assert all(r.name.startswith("src.companion.goals.") for r in h.records)


def test_every_draft_logs_one_line_when_goal_active():
    store = get_goal_store(":memory:")
    store.create_goal(conversation_id=CONV, platform=PLAT, account_id=ACCT,
                      chat_key=CK, template="custom", autonomy="auto",
                      params={"note": "推进到愿意视频"}, deadline_days=3, now=NOW - 3600)
    with _ProdLogging() as h:
        for i in range(3):
            blk = build_block_for_chat(
                _cfg(), platform=PLAT, chat_key=CK, account_id="default",
                conversation_id=CONV, user_context={}, chain="draft",
                inbound_text=f"hello {i}", now=NOW + i)
            assert blk
    inj = _msgs(h, "[goal-inject] injected")
    assert len(inj) == 3, "有活跃目标时每一稿一行，不节流"
    assert all("status=active" in m and "chain=draft" in m for m in inj)
    # 首次消费当日拍 → 一条 beat_injected 事件（每槽位一次，不按每稿刷）
    gid = store.list_goals(status="active")[0]["goal_id"]
    assert len(store.list_events(gid, kinds=("beat_injected",))) == 1


# ── 3. 到期即说 goal_expired；no_goal 尾注最近终态目标；60s 节流 ──────────────
def test_expired_on_read_logs_goal_expired_then_no_goal_names_last_goal():
    store = get_goal_store(":memory:")
    g = store.create_goal(conversation_id=CONV, platform=PLAT, account_id=ACCT,
                          chat_key=CK, template="custom", autonomy="auto",
                          params={"note": "x"}, deadline_days=1, now=NOW - 2 * 86400)
    gid = g["goal_id"]
    ctx = {}
    with _ProdLogging() as h:
        blk = build_block_for_chat(
            _cfg(), platform=PLAT, chat_key=CK, account_id=ACCT,
            conversation_id=CONV, user_context=ctx, chain="draft",
            inbound_text="hi", now=NOW)
        assert blk is None
        assert ctx["_goal_inject_meta"]["reason"] == "goal_expired"
        assert store.get_goal(gid)["status"] == "expired"
        # 下一稿：查无活跃目标 → no_goal，但尾注点名刚到期的那条
        build_block_for_chat(
            _cfg(), platform=PLAT, chat_key=CK, account_id=ACCT,
            conversation_id=CONV, user_context={}, chain="draft",
            inbound_text="hi again", now=NOW + 1)
    exp = _msgs(h, "reason=goal_expired")
    assert exp and f"goal={gid[:12]}" in exp[0] and "status=expired" in exp[0]
    ng = _msgs(h, "reason=no_goal")
    assert ng and f"last_goal={gid[:12]} status=expired" in ng[0]
    # 到期本身也留 [goal-state] 一行（store.update_goal_fields status=expired）
    assert any("[goal-state] updated" in m and "status=expired" in m
               for m in _msgs(h, "[goal-state]"))


def test_no_goal_throttle_is_60s_per_conversation():
    get_goal_store(":memory:")
    with _ProdLogging() as h:
        for dt in (0, 10, 59, 61, 70, 125):
            build_block_for_chat(
                _cfg(), platform=PLAT, chat_key=CK, account_id=ACCT,
                conversation_id=CONV, user_context={}, chain="draft",
                inbound_text="hi", now=NOW + dt)
    assert len(_msgs(h, "reason=no_goal")) == 3        # 0 / 61 / 125
    assert service._INJECT_LOG_THROTTLE_SEC == 60.0


# ── 4. 被动注入不受拍数上限；主动侧超额记 beat_blocked(pace_cap) ─────────────
def _today_goal(store: GoalStore):
    return store.create_goal(
        conversation_id=CONV, platform=PLAT, account_id=ACCT, chat_key=CK,
        template="custom", autonomy="auto", deadline_days=6 / 24.0,
        params={"pace": "today", "note": "拿到微信号"}, now=NOW - 3 * 3600)


def _fill_cap(store: GoalStore, gid: str, cap: int):
    """在「今天」记满 cap 拍，且不占当前小时槽。

    用「现在往前推整点」在刚过本地零点时会落到昨天，cap 计数只认当天前缀，
    主动链就不会再 hold。槽位改成今天日期上、当前小时之后的小时键。
    """
    lt = time.localtime(NOW)
    day = f"{lt.tm_year:04d}-{lt.tm_mon:02d}-{lt.tm_mday:02d}"
    for i in range(cap):
        slot = f"{day}T{(lt.tm_hour + 1 + i) % 24:02d}"
        store.upsert_action(gid, slot, intent=f"p{i}", push_level="soft", status="consumed",
                            now=NOW)


def test_passive_inject_ignores_pace_cap_but_proactive_holds_and_records():
    cfg_root = {"companion": {"goals": {"enabled": True, "sprint": {
        "enabled": True, "today_cap": 2}}}}
    store = GoalStore(":memory:")
    g = _today_goal(store)
    gid = g["goal_id"]
    _fill_cap(store, gid, 2)
    # 主动链（无入站）：仍被 cap 拦，且记 beat_blocked(pace_cap@<槽>)
    res = refresh_goal(store, cfg_root, store.get_goal(gid), now=NOW,
                       inbound_turn=False)
    assert res.get("hold") == "pace_cap" and res.get("action") is None
    blocked = store.list_events(gid, kinds=("beat_blocked",))
    assert len(blocked) == 1
    assert blocked[0]["detail"] == f"pace_cap@{slot_key('today', NOW)}"
    assert blocked[0]["conversation_id"] == CONV
    refresh_goal(store, cfg_root, store.get_goal(gid), now=NOW + 5, inbound_turn=False)
    assert len(store.list_events(gid, kinds=("beat_blocked",))) == 1   # 同槽去重
    # 被动链（客户来消息）：不 hold，照常出当日拍供组块
    res2 = refresh_goal(store, cfg_root, store.get_goal(gid), now=NOW,
                        inbound_turn=True)
    assert not res2.get("hold")
    assert res2.get("cap_reached") is True
    assert res2.get("action") is not None


def test_build_block_injects_when_cap_reached_on_inbound(monkeypatch):
    """端到端：cap 已满 + 客户来消息 → 仍注入方向块（此前 hold(pace_cap) 一刀切）。"""
    store = get_goal_store(":memory:")
    g = _today_goal(store)
    _fill_cap(store, g["goal_id"], 2)
    cfg = _cfg({"sprint": {"enabled": True, "today_cap": 2}})
    ctx = {}
    with _ProdLogging() as h:
        blk = build_block_for_chat(
            cfg, platform=PLAT, chat_key=CK, account_id=ACCT,
            conversation_id=CONV, user_context=ctx, chain="draft",
            inbound_text="are you there?", now=NOW)
    assert blk and ctx["_goal_inject_meta"]["injected"] is True
    assert _msgs(h, "[goal-inject] injected")
    assert not _msgs(h, "pace_cap")


# ── 5. [goal-state] 每次状态变更一行 ─────────────────────────────────────────
def test_goal_state_lines_on_create_and_status_change():
    with _ProdLogging() as h:
        store = GoalStore(":memory:")
        g = store.create_goal(conversation_id=CONV, platform=PLAT, account_id=ACCT,
                              chat_key=CK, template="custom", autonomy="auto",
                              deadline_days=3, created_by="agent:u1",
                              title="推进到愿意视频")
        gid = g["goal_id"]
        store.update_goal_fields(gid, progress=0.3)             # 日常刷新：不打
        store.update_goal_fields(gid, status="paused")
        store.update_goal_fields(gid, deadline_ts=NOW + 86400 * 4, milestone_idx=1)
        store.update_goal_fields(gid, autonomy="suggest")
    lines = _msgs(h, "[goal-state]")
    assert len(lines) == 4
    assert lines[0].startswith("[goal-state] created goal=" + gid)
    assert f"conv={CONV}" in lines[0] and "autonomy=auto" in lines[0]
    assert "by=agent:u1" in lines[0] and "deadline_days=3.00" in lines[0]
    assert "status=paused" in lines[1]
    assert "milestone_idx=1" in lines[2] and "deadline_ts=" in lines[2]
    assert "autonomy=suggest" in lines[3]
    # 事件流仍完整（created 事件带会话）
    created = [e for e in store.list_events(gid) if e["kind"] == "created"]
    assert created and created[0]["conversation_id"] == CONV
