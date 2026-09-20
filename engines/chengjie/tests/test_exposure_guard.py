# -*- coding: utf-8 -*-
"""Q-1 D（#264 #269）：识破守卫——识破词 → 目标停 24h + needs_human + 打标 + 一句挽回 + 自证句黑名单。"""
from __future__ import annotations

import logging
import time
from types import SimpleNamespace

from src.companion.goals import service
from src.companion.goals.service import build_block_for_chat
from src.companion.goals.store import get_goal_store, reset_goal_store
from src.inbox import exposure_guard as xg

CONV = "whatsapp:12137839654:13105551234"
PLAT, ACCT, CK = "whatsapp", "12137839654", "13105551234"
T0 = time.mktime((2026, 9, 8, 20, 30, 0, 0, 0, -1))


class _Inbox:
    """最小 InboxStore：消息 / app_settings KV / 会话标签。"""

    def __init__(self):
        self.msgs, self.kv, self.tags = [], {}, {}

    def add(self, direction, text, ts):
        self.msgs.append({"direction": direction, "text": text, "ts": ts})

    def list_recent_messages(self, conv, limit=30):
        return list(self.msgs)[-limit:]

    def get_conversation(self, conv):
        return {"last_ts": max((m["ts"] for m in self.msgs), default=0)}

    def get_conv_meta(self, conv):
        return {}

    def get_automation_mode(self, conv):
        return "auto_ai"

    def get_app_setting(self, key, default=""):
        return self.kv.get(key, default)

    def set_app_setting(self, key, value, updated_by=""):
        self.kv[key] = value
        return True

    def get_conv_tags(self, cid):
        return list(self.tags.get(cid, []))

    def set_conv_tags(self, cid, tags):
        self.tags[cid] = list(tags)
        return True


def test_detect_exposure_hits_and_misses():
    for t in ("Are you a bot??", "this is like the 20th time you asked me", "you already asked that lol",
              "I told you that already", "you're a robot aren't you", "你是机器人吧", "你又问一遍",
              "我都说过了", "u sound like a bot", "is this a bot"):
        assert xg.detect_exposure(t), t
    for t in ("you told me about your dog yesterday", "my robot vacuum died lol", "what time is it",
              "I asked my boss for a raise", "第一次来这个城市", "机器人电影好看", ""):
        assert xg.detect_exposure(t) == "", t


def test_self_proof_blacklist_strips_sentence_only():
    out, hits = xg.strip_self_proof("Haha no way. I promise I'm not a robot! Anyway how was your day", "en")
    assert hits and "robot" not in out and out.startswith("Haha no way.") and "how was your day" in out
    out2, hits2 = xg.strip_self_proof("我不是机器人啦！今天累不累", "zh")
    assert hits2 and out2 == "今天累不累"
    assert xg.strip_self_proof("I'm real tired today", "en")[1] == []   # 「real tired」不是自证
    assert xg.find_self_proof("I am a real person, promise") and not xg.find_self_proof("nice day")


def test_handle_inbound_idempotent_tags_needs_human_and_marks_kv(caplog):
    st = _Inbox()
    with caplog.at_level(logging.WARNING, logger="src.inbox.exposure_guard"):
        r1 = xg.handle_inbound("are you a bot", conversation_id=CONV, inbox_store=st, inbound_ts=T0, now=T0)
        r2 = xg.handle_inbound("are you a bot", conversation_id=CONV, inbox_store=st, inbound_ts=T0, now=T0 + 5)
    assert r1["handled"] and r1["hit"] and r2["already"] and not r2["handled"]
    tags = st.get_conv_tags(CONV)
    assert xg.EXPOSURE_TAG in tags and "需人工" in tags
    mark = xg._kv_get(st, CONV)
    assert mark and mark["consumed"] is False and mark["hit"]
    assert any("action=needs_human" in r.getMessage() for r in caplog.records)
    assert xg.handle_inbound("nice weather", conversation_id=CONV, inbox_store=st) == {
        "hit": "", "handled": False, "already": False}


def test_guard_outbound_recovers_once_then_normal(caplog):
    st = _Inbox()
    st.add("out", "What do you do for work?", T0 - 600)
    st.add("in", "this is like the 20th time you asked", T0)
    draft = "Haha sorry! So what do you do for work? I promise I'm not a robot."
    with caplog.at_level(logging.WARNING, logger="src.inbox.exposure_guard"):
        out, rep = xg.guard_outbound(draft, conversation_id=CONV, lang="en", inbox_store=st, now=T0 + 30)
    assert rep["action"] == "recover" and out in xg._RECOVERY["en"]
    assert "robot" not in out and "?" not in out
    assert xg.EXPOSURE_TAG in st.get_conv_tags(CONV)
    assert any("action=recover" in r.getMessage() for r in caplog.records)
    # 第二稿：标记已消费 → 不再整稿替换，但自证句仍删
    out2, rep2 = xg.guard_outbound("Anyway. I'm a real person btw. How's the weather?",
                                   conversation_id=CONV, lang="en", inbox_store=st, now=T0 + 60)
    assert rep2["action"] == "strip_selfproof" and "real person" not in out2 and "weather" in out2
    out3, rep3 = xg.guard_outbound("How's the weather?", conversation_id=CONV, lang="en",
                                   inbox_store=st, now=T0 + 90)
    assert rep3["action"] == "clean" and out3 == "How's the weather?"


def test_guard_outbound_only_self_proof_left_becomes_recovery_line():
    st = _Inbox()
    out, rep = xg.guard_outbound("我不是机器人！我是真人啦", conversation_id=CONV, lang="zh",
                                 inbox_store=st, now=T0)
    assert rep["action"] == "strip_selfproof" and out in xg._RECOVERY["zh"]


def test_goal_paused_24h_on_exposure_and_not_injected_until_wakes(caplog):
    reset_goal_store()
    service._inject_log_seen.clear()
    gs = get_goal_store(":memory:")
    goal = gs.create_goal(conversation_id=CONV, platform=PLAT, account_id=ACCT, chat_key=CK,
                          template="profile_discovery", autonomy="auto", deadline_days=3,
                          params={"slots": "occupation,age,location"}, now=T0) or {}
    cfg = SimpleNamespace(config={"companion": {"goals": {"enabled": True, "db_path": ":memory:"}}},
                          config_path=None)
    inbox = _Inbox()

    def inject(text, now):
        uc = {}
        blk = build_block_for_chat(cfg, platform=PLAT, chat_key=CK, account_id=ACCT, conversation_id=CONV,
                                   user_context=uc, chain="draft", inbound_text=text, inbox_store=inbox, now=now)
        return blk, uc.get("_goal_inject_meta") or {}

    blk0, m0 = inject("long day at work", T0)
    assert blk0 and m0.get("injected")
    with caplog.at_level(logging.WARNING, logger="src.inbox.exposure_guard"):
        blk1, m1 = inject("are you a bot? this is the 20th time you asked", T0 + 60)
    assert blk1 is None and m1.get("reason") == "exposure_paused" and m1.get("hit")
    g = gs.get_goal(goal["goal_id"])
    until = xg.goal_paused_until(g)
    assert abs(until - (T0 + 60 + xg.PAUSE_SEC)) < 1
    assert any(e.get("kind") == xg.GOAL_EVENT_PAUSE for e in gs.list_events(goal["goal_id"], limit=20))
    assert xg.EXPOSURE_TAG in inbox.get_conv_tags(CONV) and "需人工" in inbox.get_conv_tags(CONV)
    assert any("action=pause_goals" in r.getMessage() for r in caplog.records)
    # 停牌期内：任何入站都不注入
    blk2, m2 = inject("ok whatever", T0 + 3600)
    assert blk2 is None and m2.get("reason") == "goals_paused"
    blk3, m3 = inject("long day at work", T0 + 20 * 3600)
    assert blk3 is None and m3.get("reason") == "goals_paused"
    # 24h 后醒
    blk4, m4 = inject("long day at work", T0 + 60 + xg.PAUSE_SEC + 5)
    assert blk4 and m4.get("injected")


def test_hooked_in_draft_humanize_after_repeat_before_claim():
    import inspect
    from src.inbox import outbound_humanize
    src = inspect.getsource(outbound_humanize.apply_draft_humanize)
    assert src.index("check_repeat_questions(") < src.index("guard_outbound(") < src.index("check_claims(")
    out, meta = outbound_humanize.apply_draft_humanize(
        "Haha. I promise I'm not a robot. How was your day?", conversation_id="", lang="en", origin="auto")
    assert meta.get("exposure") == "strip_selfproof" and "robot" not in (out or "")
