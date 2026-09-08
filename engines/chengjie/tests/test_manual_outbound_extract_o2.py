"""O-2 B（2026-09-08，#201 / DY534Y）：``origin=manual`` 出站同走记忆抽取。

现象：抽取只挂在「AI 回复之后」（A 线 process_message / B 线 generate_inbox_draft /
persona_reply 写回），``[episodic] schedule run user=客户号`` 只随客户入站 + AI 回复出现；
人工接管期间会话不拟稿 → 客户对人工说的约定（地点 / 时间 / 生日）从不进记忆。#177 做的是
坐席替人设说的**自述**进 ``_human_said_log``，不是客户事实抽取。

修法：手动发送成功后的既有漏斗 ``human_outbound_memory.on_human_outbound`` 多调一次
``SkillManager.schedule_manual_outbound_extract``——user_msg＝上一条出站之后客户连续说的话、
reply＝坐席这条；``user=`` 仍是客户号；日志 ``source=manual_out``；两道去重。

本文件钉：
1. 纯人工接管：客户两句 → 坐席一句 → 恰好一次调度，参数结构与 B 线同口径；
2. 坐席连发第二条 → 不重复抽（mark）；
3. 人审后手发（B 线已对该入站拟稿，``user_msg_id`` 相等）→ 跳过；
4. 无文本入站 / 无 store / 空文本 / account_id=default 补账号 / 长文本截尾；
5. ``_schedule_episodic_memory_extract`` 的日志行：入站路径逐字不变，manual_out 多 ``source=``；
6. 端到端经 ``on_human_outbound`` / ``record_human_outbound``：有 store 才触发，缺 store 旧行为。
"""
from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

from src.inbox import human_outbound_memory as hom
from src.skills.skill_manager import SkillManager
from src.utils.context_store import ContextStore, make_context_key

MARK = SkillManager._MANUAL_OUT_MARK_KEY
CID = "whatsapp:17345893506:13308422244"


class _Store:
    def __init__(self, rows: List[Dict[str, Any]]):
        self.rows = rows
        self.calls: List[Any] = []

    def list_recent_messages(self, conversation_id, *, limit=50, **_):
        self.calls.append((conversation_id, limit))
        return list(self.rows)[-limit:]


def _row(direction: str, text: str, ts: float, mid: str) -> Dict[str, Any]:
    return {"direction": direction, "text": text, "ts": ts, "message_id": mid}


class _SM:
    """只给 schedule_manual_outbound_extract 用到的成员；方法本体取自 SkillManager。"""
    _MANUAL_OUT_MARK_KEY = MARK
    logger = logging.getLogger("test.o2b")

    def __init__(self, intent: str = "direct_chat"):
        self.scheduled: List[Dict[str, Any]] = []
        self._intent = intent

    def _recognize_intent(self, text: str) -> str:
        return "complaint" if "退款" in text else self._intent

    def _schedule_episodic_memory_extract(self, user_id, user_msg, reply, intent, chat_id,
                                          platform="", account_id="", source=""):
        self.scheduled.append(dict(user_id=user_id, user_msg=user_msg, reply=reply,
                                   intent=intent, chat_id=chat_id, platform=platform,
                                   account_id=account_id, source=source))


def _call(sm, store, ctx, *, text="好的，周六三点星巴克见", account_id="17345893506"):
    return SkillManager.schedule_manual_outbound_extract(
        sm, platform="whatsapp", account_id=account_id, chat_key="13308422244",
        operator_text=text, conversation_id=CID, inbox_store=store, user_context=ctx)


_ROWS_TAKEOVER = [
    _row("out", "在的～", 1.0, "a1"),                       # 早先 AI 回复
    _row("in", "周六下午三点星巴克见吧", 10.0, "m1"),        # 客户对人工说的约定
    _row("in", "对了我生日是3月2号", 11.0, "m2"),
    _row("out", "好的，周六三点星巴克见", 12.0, "o1"),       # 坐席刚发出（已落库）
]


# ── 1. 纯人工接管：恰好一次调度，参数与 B 线同口径 ──────────────────────────

def test_manual_takeover_schedules_customer_turn_once():
    sm, ctx = _SM(), {}
    res = _call(sm, _Store(_ROWS_TAKEOVER), ctx)
    assert res["ok"] is True and res["n_inbound"] == 2 and res["intent"] == "direct_chat"
    assert len(sm.scheduled) == 1
    s = sm.scheduled[0]
    assert s["user_id"] == "13308422244", "user= 仍是客户号"
    assert s["user_msg"] == "周六下午三点星巴克见吧\n对了我生日是3月2号", "客户这一轮的话按时间序拼接"
    assert s["reply"] == "好的，周六三点星巴克见", "坐席这条作 reply（语境），绝不当 user_msg"
    assert s["chat_id"] == "" and s["platform"] == "whatsapp" and s["account_id"] == "17345893506"
    assert s["source"] == "manual_out"
    assert ctx[MARK] == 11.0


# ── 2. 坐席连发第二条：不重复抽 ─────────────────────────────────────────────

def test_second_consecutive_manual_send_does_not_reextract():
    sm, ctx = _SM(), {}
    store = _Store(list(_ROWS_TAKEOVER))
    assert _call(sm, store, ctx)["ok"] is True
    store.rows.append(_row("out", "到了给我发消息", 13.0, "o2"))
    res = _call(sm, store, ctx, text="到了给我发消息")
    assert res["ok"] is False and res["reason"] == "no_fresh_inbound"
    assert len(sm.scheduled) == 1


def test_new_inbound_after_mark_is_extracted_again():
    sm, ctx = _SM(), {}
    store = _Store(list(_ROWS_TAKEOVER))
    _call(sm, store, ctx)
    store.rows += [_row("in", "那我带我妹妹一起来", 20.0, "m3"),
                   _row("out", "好呀", 21.0, "o2")]
    res = _call(sm, store, ctx, text="好呀")
    assert res["ok"] is True and res["n_inbound"] == 1
    assert sm.scheduled[-1]["user_msg"] == "那我带我妹妹一起来"
    assert ctx[MARK] == 20.0


# ── 3. 人审后手发：B 线已对该入站拟稿 → 跳过 ─────────────────────────────────

def test_drafted_inbound_is_skipped_to_avoid_double_extract():
    sm = _SM()
    ctx = {"user_msg_id": "m2"}    # generate_inbox_draft 对 m2 拟过稿（抽取已在那时走过）
    res = _call(sm, _Store(_ROWS_TAKEOVER), ctx)
    assert res["ok"] is False and res["reason"] == "drafted"
    assert sm.scheduled == []
    assert ctx[MARK] == 11.0, "跳过也要落 mark，下次手发不再回头看这一轮"


def test_stale_user_msg_id_does_not_block():
    """人工接管中 user_msg_id 停在更早那轮（m0）→ 本轮 m2 未拟稿 → 照抽。"""
    sm, ctx = _SM(), {"user_msg_id": "m0"}
    assert _call(sm, _Store(_ROWS_TAKEOVER), ctx)["ok"] is True
    assert len(sm.scheduled) == 1


# ── 4. 边界 ─────────────────────────────────────────────────────────────────

def test_media_only_inbound_has_no_text():
    sm, ctx = _SM(), {}
    rows = [_row("out", "hi", 1.0, "a"), _row("in", "", 2.0, "img"), _row("out", "收到", 3.0, "o")]
    res = _call(sm, _Store(rows), ctx, text="收到")
    assert res["reason"] == "no_text" and sm.scheduled == [] and ctx[MARK] == 2.0


def test_no_store_or_empty_text_skips():
    sm = _SM()
    assert _call(sm, None, {})["reason"] == "no_store"
    assert _call(sm, _Store(_ROWS_TAKEOVER), {}, text="   ")["reason"] == "empty"
    assert sm.scheduled == []


def test_default_account_id_resolved_from_conversation_id():
    sm = _SM()
    _call(sm, _Store(_ROWS_TAKEOVER), {}, account_id="default")
    assert sm.scheduled[0]["account_id"] == "17345893506", "记忆键须与 B 线同桶（account:chat_key）"


def test_intent_comes_from_recognizer_and_long_text_keeps_tail():
    sm = _SM()
    rows = [_row("out", "hi", 1.0, "a"),
            _row("in", "x" * 1600, 2.0, "m1"),
            _row("in", "我要退款", 3.0, "m2"),
            _row("out", "好", 4.0, "o")]
    res = _call(sm, _Store(rows), {}, text="好")
    assert res["intent"] == "complaint"
    um = sm.scheduled[0]["user_msg"]
    assert len(um) == 1500 and um.endswith("我要退款"), "截尾保留最近的话"


def test_store_exception_never_raises():
    class _Boom:
        def list_recent_messages(self, *a, **k):
            raise RuntimeError("db gone")
    sm = _SM()
    res = _call(sm, _Boom(), {})
    assert res["reason"] == "error" and sm.scheduled == []


# ── 5. 日志行：入站路径逐字不变，manual_out 多 source= ──────────────────────

class _LogSM:
    # 不挂在 ai_chat_assistant.* 名下：那棵树的上色 handler 会把 record 消息包进 ANSI 码
    logger = logging.getLogger("test.o2b.schedule_log")
    _episodic_store = object()
    _memory_cfg = {"enabled": True, "extract": {"enabled": True, "intents": ["direct_chat"]}}

    async def _episodic_memory_extract_async(self, *a, **k):   # 无事件循环时不会被调到
        return None


def test_schedule_log_line_unchanged_for_inbound_and_tagged_for_manual_out(caplog):
    sm = _LogSM()
    with caplog.at_level(logging.INFO, logger=_LogSM.logger.name):
        SkillManager._schedule_episodic_memory_extract(sm, "u1", "hello", "hi", "direct_chat", "")
        SkillManager._schedule_episodic_memory_extract(
            sm, "u1", "hello", "hi", "direct_chat", "", source="manual_out")
    runs = [r.getMessage() for r in caplog.records if "schedule run" in r.getMessage()]
    assert runs[0] == "[episodic] schedule run user=u1 intent=direct_chat msg_len=5"
    assert runs[1] == "[episodic] schedule run user=u1 intent=direct_chat msg_len=5 source=manual_out"


# ── 6. 端到端：经 on_human_outbound / record_human_outbound ──────────────────

class _FakeSM:
    _MANUAL_OUT_MARK_KEY = MARK
    logger = logging.getLogger("test.o2b.e2e")

    def __init__(self, db: Path):
        self._context_store = ContextStore(db_path=db, ttl_days=30)
        self.scheduled: List[Dict[str, Any]] = []

    def _get_user_context(self, user_id, account_id="", chat_scope=""):
        key = make_context_key(user_id, account_id)
        ctx = self._context_store.get(key)
        ctx["user_id"] = str(user_id)
        ctx["_context_store_key"] = key
        return ctx

    def _push_recent_reply(self, user_context, reply):
        user_context.setdefault("recent_replies", []).append(reply)

    def _recognize_intent(self, text):
        return "direct_chat"

    def _schedule_episodic_memory_extract(self, *a, **k):
        self.scheduled.append((a, k))

    # 真方法接到假对象上：走的就是生产那段代码
    schedule_manual_outbound_extract = SkillManager.schedule_manual_outbound_extract


def test_on_human_outbound_triggers_customer_extract_and_persists_mark(tmp_path):
    sm = _FakeSM(tmp_path / "bot.db")
    res = hom.on_human_outbound(
        "whatsapp", "17345893506", "13308422244", "好的，周六三点星巴克见",
        conversation_id=CID, skill_manager=sm, inbox_store=_Store(_ROWS_TAKEOVER))
    assert res["ok"] is True
    assert res["customer_extract"]["ok"] is True and res["customer_extract"]["n_inbound"] == 2
    a, k = sm.scheduled[0]
    assert a[0] == "13308422244" and k["source"] == "manual_out"
    # mark 随 ContextStore 持久（重启后不重抽）
    sm._context_store.close()
    sm2 = _FakeSM(tmp_path / "bot.db")
    ctx2 = sm2._get_user_context("13308422244", account_id="17345893506")
    assert ctx2[MARK] == 11.0
    sm2._context_store.close()


def test_on_human_outbound_without_store_keeps_old_behaviour(tmp_path):
    sm = _FakeSM(tmp_path / "bot.db")
    res = hom.on_human_outbound(
        "whatsapp", "17345893506", "13308422244", "好的", conversation_id=CID, skill_manager=sm)
    assert res["ok"] is True and res["customer_extract"] == {} and sm.scheduled == []
    sm._context_store.close()


def test_media_manual_outbound_does_not_trigger_customer_extract(tmp_path):
    """语音 / 图片人工出站只记占位（模块既有口径），不拿它当 reply 去抽。"""
    sm = _FakeSM(tmp_path / "bot.db")
    res = hom.on_human_outbound(
        "whatsapp", "17345893506", "13308422244", "", media_type="voice",
        conversation_id=CID, skill_manager=sm, inbox_store=_Store(_ROWS_TAKEOVER))
    assert res["ok"] is True and res["customer_extract"] == {} and sm.scheduled == []
    sm._context_store.close()


def test_record_human_outbound_passes_app_state_inbox_store(tmp_path):
    sm = _FakeSM(tmp_path / "bot.db")
    st = SimpleNamespace(skill_manager=sm, inbox_store=_Store(_ROWS_TAKEOVER))
    res = hom.record_human_outbound(
        st, "whatsapp", "17345893506", "13308422244", "好的，周六三点星巴克见",
        conversation_id=CID)
    assert res["customer_extract"]["ok"] is True and len(sm.scheduled) == 1
    # app.state 没挂 inbox_store（测试环境 / 老装配）→ 旧行为
    sm2 = _FakeSM(tmp_path / "bot2.db")
    res2 = hom.record_human_outbound(
        SimpleNamespace(skill_manager=sm2), "whatsapp", "17345893506", "13308422244", "好的",
        conversation_id=CID)
    assert res2["ok"] is True and res2["customer_extract"] == {}
    sm._context_store.close()
    sm2._context_store.close()
