# -*- coding: utf-8 -*-
"""Q-8 F（#264 #263）：当地时间 + 今日日程注入（Q-6 addenda ④ 接线点）；借口只从日程取；
「工作」类借口同客户每日 ≤1（发送门真计数、起草层只判超额改写）；夸赞「接住 + 反投问题」进陪伴底稿。"""
from __future__ import annotations

import inspect
import logging
import re
import time
from pathlib import Path

import pytest

from src.inbox import excuse_budget as eb
from src.inbox.prompt_addenda import time_schedule_addendum
from src.utils import business_domain as bd

CONV = "whatsapp:12137839654:13105551234"
T0 = time.mktime((2026, 9, 10, 14, 30, 0, 0, 0, -1))
CFG_COMPANION = {"business_domain": "companion", "companion": {"selfie": {"enabled": True, "scene_rotation": [
    "coffee run downtown", "desk at home", "walk along the seawall", "couch with a blanket"]}}}
CFG_SALES = {"business_domain": "sales"}


class _Inbox:
    def __init__(self):
        self.kv = {}

    def get_app_setting(self, key, default=""):
        return self.kv.get(key, default)

    def set_app_setting(self, key, value, updated_by=""):
        self.kv[key] = value
        return True


@pytest.fixture(autouse=True)
def _reset_domain():
    bd.reset_active_business_domain()
    yield
    bd.reset_active_business_domain()


# ---------------- 检出 ----------------

@pytest.mark.parametrize("text", [
    "sorry, I'm swamped with work today", "work is crazy right now", "stuck at work", "I'm in a meeting",
    "working late tonight", "I'm at work now", "got a ton of work", "my boss needs me",
    "我在上班呢", "工作太忙了", "我这边工作有点忙", "老板催得紧", "手头有点活", "我得去开会了", "还在加班",
])
def test_detect_work_excuse_hits(text):
    assert eb.detect_work_excuse(text)


@pytest.mark.parametrize("text", [
    "你在上班吗？", "are you busy at work?", "how was work today?", "你老板凶吗", "I love my work",
    "我今天不上班，休息", "your boss sounds awful", "",
])
def test_detect_work_excuse_no_false_positive(text):
    assert eb.detect_work_excuse(text) == ""


# ---------------- 纯格式化 ----------------

def test_addendum_zh_time_itinerary_and_over_budget():
    s = time_schedule_addendum("14:30 周四，加拿大·温哥华", [("上午", "coffee run"), ("下午", "desk at home")],
                               lang="zh", current_bucket="下午", work_excuse_used=1)
    assert s.startswith("【当地时间与今日日程】")
    assert "14:30 周四" in s and "上午:coffee run→下午(现在):desk at home" in s
    assert "只能取自这条日程" in s and "不许再以工作" in s and "1/1" in s


def test_addendum_en_time_only_and_empty():
    assert time_schedule_addendum("", [], lang="en") == ""
    s = time_schedule_addendum("14:30 Thu, Vancouver, Canada", [], lang="en")
    assert s.startswith("【local time & today's schedule】") and "14:30 Thu" in s
    assert "don't invent a reason" in s and "work excuse" not in s


def test_addendum_over_budget_alone_still_emits():
    s = time_schedule_addendum("", [], lang="zh", work_excuse_used=2, work_excuse_cap=1)
    assert "不许再以工作" in s and "2/1" in s


# ---------------- 开关 / 计数 ----------------

def test_enabled_by_domain_or_explicit():
    assert eb.is_enabled(CFG_COMPANION) is True
    assert eb.is_enabled(CFG_SALES) is False
    assert eb.is_enabled({"business_domain": "companion", "companion": {"time_schedule": {"enabled": False}}}) is False
    assert eb.is_enabled({"business_domain": "sales", "companion": {"time_schedule": {"enabled": True}}}) is True
    assert eb.work_excuse_cap({}) == 1
    assert eb.work_excuse_cap({"companion": {"time_schedule": {"work_excuse_cap": 3}}}) == 3


def test_count_and_note_kv_per_day():
    ib = _Inbox()
    assert eb.count_today(ib, CONV, T0) == 0
    assert eb.note_used(ib, CONV, T0) == 1
    assert eb.note_used(ib, CONV, T0) == 2
    assert eb.count_today(ib, CONV, T0) == 2
    assert eb.count_today(ib, CONV, T0 + 86400) == 0
    assert list(ib.kv)[0].startswith(eb.KEY_PREFIX + CONV + ":")


# ---------------- 出站闸 ----------------

def test_guard_first_excuse_passes_and_counts_on_send_gate_only(caplog):
    ib = _Inbox()
    caplog.set_level(logging.INFO, logger="src.inbox.excuse_budget")
    txt = "sorry, I'm swamped with work today. what are you up to?"
    out, rep = eb.guard_outbound(txt, conversation_id=CONV, lang="en", cfg_root=CFG_COMPANION,
                                 inbox_store=ib, now=T0, count=False)
    assert out == txt and rep["action"] == "pass" and eb.count_today(ib, CONV, T0) == 0
    out, rep = eb.guard_outbound(txt, conversation_id=CONV, lang="en", cfg_root=CFG_COMPANION,
                                 inbox_store=ib, now=T0, count=True)
    assert out == txt and rep["action"] == "pass" and rep["today"] == 1 and eb.count_today(ib, CONV, T0) == 1
    assert any(f"[excuse] conv={CONV} kind=work today=1 cap=1 action=pass" in r.getMessage() for r in caplog.records)


def test_guard_second_excuse_rewritten_keeps_rest(caplog):
    ib = _Inbox()
    eb.note_used(ib, CONV, T0)
    caplog.set_level(logging.INFO, logger="src.inbox.excuse_budget")
    out, rep = eb.guard_outbound("haha sorry, I'm swamped with work today. what are you up to?",
                                 conversation_id=CONV, lang="en", cfg_root=CFG_COMPANION, inbox_store=ib, now=T0)
    assert rep["action"] == "rewrite" and rep["today"] == 1
    assert "work" not in out and out == "haha sorry. what are you up to?"
    assert eb.count_today(ib, CONV, T0) == 1  # 改写不再计
    assert any(f"[excuse] conv={CONV} kind=work today=1 cap=1 action=rewrite" in r.getMessage() for r in caplog.records)


def test_guard_only_excuse_becomes_fallback_line_zh():
    ib = _Inbox()
    eb.note_used(ib, CONV, T0)
    out, rep = eb.guard_outbound("我在加班呢", conversation_id=CONV, lang="zh", cfg_root=CFG_COMPANION,
                                 inbox_store=ib, now=T0)
    assert rep["action"] == "rewrite" and out in eb._FALLBACK_LINE["zh"]


def test_guard_clause_level_strip_zh():
    ib = _Inbox()
    eb.note_used(ib, CONV, T0)
    out, rep = eb.guard_outbound("我在加班呢，你吃了吗？", conversation_id=CONV, lang="zh", cfg_root=CFG_COMPANION,
                                 inbox_store=ib, now=T0)
    assert rep["action"] == "rewrite" and out == "你吃了吗？"


def test_guard_sales_domain_and_clean_text_untouched():
    ib = _Inbox()
    out, rep = eb.guard_outbound("I'm in a meeting, will call you back", conversation_id=CONV, lang="en",
                                 cfg_root=CFG_SALES, inbox_store=ib, now=T0)
    assert rep["action"] == "clean" and not ib.kv
    out, rep = eb.guard_outbound("what did you cook tonight?", conversation_id=CONV, lang="en",
                                 cfg_root=CFG_COMPANION, inbox_store=ib, now=T0)
    assert rep["action"] == "clean"


def test_note_outbound_excuse_counts_bypass_origin():
    ib = _Inbox()
    assert eb.note_outbound_excuse("我在上班呢", conversation_id=CONV, cfg_root=CFG_COMPANION, inbox_store=ib, now=T0) == 1
    assert eb.note_outbound_excuse("你好呀", conversation_id=CONV, cfg_root=CFG_COMPANION, inbox_store=ib, now=T0) == 0
    assert eb.note_outbound_excuse("我在上班呢", conversation_id=CONV, cfg_root=CFG_SALES, inbox_store=ib, now=T0) == 0


# ---------------- 装配（当地时间 + 日程） ----------------

def test_build_addendum_with_persona_location_and_itinerary():
    ib = _Inbox()
    persona = {"id": "p1", "name": "Mia", "location": "Vancouver"}
    s = eb.build_time_schedule_addendum(persona, CONV, CFG_COMPANION, lang="en", now=T0, inbox_store=ib)
    assert s.startswith("【local time & today's schedule】")
    assert re.search(r"\d\d:\d\d \w{3}, Vancouver", s), s
    assert "Your day today:" in s and "(now)" in s and "this schedule only" in s
    assert "work excuse" not in s
    eb.note_used(ib, CONV, T0)
    s2 = eb.build_time_schedule_addendum(persona, CONV, CFG_COMPANION, lang="en", now=T0, inbox_store=ib)
    assert "already used a work excuse" in s2 and "(1/1)" in s2


def test_build_addendum_no_location_no_scenes_only_when_over_budget():
    ib = _Inbox()
    persona = {"id": "p1", "name": "Mia", "location": "none"}
    cfg = {"business_domain": "companion"}
    assert eb.build_time_schedule_addendum(persona, CONV, cfg, lang="zh", now=T0, inbox_store=ib) == ""
    eb.note_used(ib, CONV, T0)
    s = eb.build_time_schedule_addendum(persona, CONV, cfg, lang="zh", now=T0, inbox_store=ib)
    assert "不许再以工作" in s


def test_build_addendum_sales_domain_off():
    persona = {"id": "p1", "name": "Mia", "location": "Vancouver"}
    assert eb.build_time_schedule_addendum(persona, CONV, CFG_SALES, lang="en", now=T0, inbox_store=_Inbox()) == ""


def test_persona_reply_addenda_has_exactly_one_new_try_block():
    from src.inbox import persona_reply
    src = inspect.getsource(persona_reply._prompt_addenda)
    assert src.count("build_time_schedule_addendum") == 2  # import + call，恰好一个 try-block
    assert "identity_addendum(persona, account" in src
    assert src.index("identity_addendum(persona, account") < src.index("build_time_schedule_addendum(persona, ck")


def test_outbound_hooks_draft_no_count_send_gate_counts(monkeypatch):
    from src.inbox import outbound_humanize as oh
    src_draft = inspect.getsource(oh.apply_draft_humanize)
    assert src_draft.index("guard_exit(") < src_draft.index("_excuse_guard(") < src_draft.index("humanize(cur")
    assert "count=False" in src_draft
    src_send = inspect.getsource(oh.apply_outbound_humanize)
    assert "count=True" in src_send and "note_outbound_excuse(" in src_send
    ib = _Inbox()
    monkeypatch.setattr(eb, "_store", lambda s: ib)
    cfg = dict(CFG_COMPANION)
    out, meta = oh.apply_draft_humanize("ugh, work is crazy today. how was your morning?", conversation_id=CONV,
                                        lang="en", origin="auto", cfg_root=cfg)
    assert meta.get("excuse") == "pass" and eb.count_today(ib, CONV) == 0
    sent = oh.apply_outbound_humanize("ugh, work is crazy today. how was your morning?", conversation_id=CONV,
                                      lang="en", origin="auto", cfg_root=cfg)
    assert "work is crazy" in sent and eb.count_today(ib, CONV) == 1
    out2, meta2 = oh.apply_draft_humanize("sorry, stuck at work again. did you eat?", conversation_id=CONV,
                                          lang="en", origin="auto", cfg_root=cfg)
    assert meta2.get("excuse") == "rewrite" and "work" not in out2.lower() and "did you eat" in out2.lower()
    manual = oh.apply_outbound_humanize("我在开会呢", conversation_id=CONV, lang="zh", origin="manual", cfg_root=cfg)
    assert manual == "我在开会呢" and eb.count_today(ib, CONV) == 2


# ---------------- 夸赞底稿一句 ----------------

def test_companion_prompt_has_compliment_line():
    root = Path(__file__).resolve().parents[1]
    t = (root / "domains" / "conversion" / "prompts" / "system_companion.txt").read_text(encoding="utf-8")
    low = t.lower()
    assert "when they compliment you" in low
    assert "question about them" in low and "never treat it as your cue to leave" in low
