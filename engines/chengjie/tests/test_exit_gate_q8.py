# -*- coding: utf-8 -*-
"""Q-8 D（#264 #263）：退场闸 exit_claim——AI 不许自己结束对话。

合法退场只有三种：① 班表到点 ② 客户先告别 ③ 用户开启留白策略；其余改写为问句 / 话头。
合法退场留钩子 + 关怀排一条真实后续；每次检出落 stats ``exit_claim`` + ``[exit]`` 日志。
"""
from __future__ import annotations

import inspect
import logging
import time

import pytest

from src.inbox import ai_fingerprint_stats as fp
from src.inbox import exit_gate as xg

CONV = "whatsapp:12137839654:13105551234"
T0 = time.mktime((2026, 9, 10, 14, 30, 0, 0, 0, -1))


class _Inbox:
    def __init__(self):
        self.msgs = []

    def add(self, direction, text, ts=T0):
        self.msgs.append({"direction": direction, "text": text, "ts": ts})

    def list_recent_messages(self, conv, limit=30):
        return list(self.msgs)[-limit:]


class _Care:
    def __init__(self):
        self.rows = []

    def add_scheduled_care(self, **kw):
        self.rows.append(kw)
        return len(self.rows)


@pytest.fixture(autouse=True)
def _reset_stats():
    fp._reset_for_tests()
    yield
    fp._reset_for_tests()


# ---------------- 检出 ----------------

@pytest.mark.parametrize("text", [
    "gotta go", "anyway, gotta run!", "I have to go now", "I should get going", "have a good one",
    "talk later", "ttyl", "I'm heading out now", "heading out now", "gotta get back to work",
    "I need to get back to work", "I'm going back to work now", "time to get back to work",
    "I'll let you go", "signing off for tonight", "I'm off for the day",
    "我先忙了", "我得去忙了", "我要去忙一下", "回去工作了", "我回去工作了", "下次聊", "改天再聊",
    "晚点聊", "我得走了", "先不聊了", "不打扰你了",
])
def test_detect_exit_claim_hits(text):
    assert xg.detect_exit_claim(text)


@pytest.mark.parametrize("text", [
    "are you going back to work tomorrow?",
    "you've got to go see it, it's so good",
    "did you have a good one?",
    "haha you are too sweet, what did you do today?",
    "我忙了一天，累死了，你呢？",
    "you gotta go there some time",
    "I got to say you are funny",
    "let's go for a walk this weekend?",
    "later tonight I am free, you?",
    "you should get back to work haha, boss is watching",
    "",
])
def test_detect_exit_claim_no_false_positive(text):
    assert xg.detect_exit_claim(text) == ""


def test_is_goodbye():
    assert xg.is_goodbye("ok good night!")
    assert xg.is_goodbye("talk to you tomorrow")
    assert xg.is_goodbye("晚安")
    assert xg.is_goodbye("我先去睡了")
    assert not xg.is_goodbye("I will be home later")
    assert not xg.is_goodbye("哈哈你真逗")
    assert not xg.is_goodbye("")


def test_strip_exit_sentences_keeps_rest():
    kept, removed = xg.strip_exit_sentences("haha thanks! anyway I gotta get back to work. have a good one")
    assert kept == "haha thanks!"
    assert len(removed) == 2


# ---------------- 闸：改写 ----------------

def test_rewrite_in_shift_no_bye_appends_question_and_logs(caplog):
    ib = _Inbox()
    ib.add("in", "you are so funny lol")
    caplog.set_level(logging.INFO, logger="src.inbox.exit_gate")
    out, rep = xg.guard_exit("haha you're too kind. anyway gotta get back to work, talk later!",
                             conversation_id=CONV, lang="en", cfg_root={}, inbox_store=ib, now=T0)
    assert rep["action"] == "rewrite" and rep["reason"] == "in_shift_no_bye"
    assert "back to work" not in out and "talk later" not in out
    assert out.startswith("haha you're too kind.")
    assert "?" in out  # 补了一句把话题转回对方的问题
    assert rep["hit"]
    line = [r.getMessage() for r in caplog.records if "[exit]" in r.getMessage()]
    assert line and f"conv={CONV} allowed=0 reason=in_shift_no_bye" in line[0]


def test_rewrite_zh_and_keeps_existing_question():
    ib = _Inbox()
    ib.add("in", "你太厉害了")
    out, rep = xg.guard_exit("哈哈谢谢。我先忙了，你今天下班打算吃什么？", conversation_id=CONV, lang="zh",
                             cfg_root={}, inbox_store=ib, now=T0)
    assert rep["action"] == "rewrite"
    assert "我先忙了" not in out
    # 已有问句 → 不再叠加轮换问题
    assert out.count("？") == 1 and "吃什么" in out


def test_only_exit_sentence_becomes_turn_back_line():
    out, rep = xg.guard_exit("gotta go", conversation_id=CONV, lang="en", cfg_root={}, inbox_store=_Inbox(), now=T0)
    assert rep["action"] == "rewrite" and out and "?" in out and "gotta go" not in out


def test_clean_text_untouched_no_stats():
    out, rep = xg.guard_exit("haha what did you get up to today?", conversation_id=CONV, lang="en",
                             cfg_root={}, inbox_store=_Inbox(), now=T0)
    assert rep["action"] == "clean" and out == "haha what did you get up to today?"
    assert fp.snapshot()["counts"].get("exit_claim", 0) == 0


def test_disabled_by_config_passes_through():
    out, rep = xg.guard_exit("gotta go", conversation_id=CONV, lang="en",
                             cfg_root={"inbox": {"exit_gate": {"enabled": False}}}, inbox_store=_Inbox(), now=T0)
    assert rep["action"] == "clean" and out == "gotta go"


# ---------------- 闸：合法退场 ----------------

def test_allow_customer_bye_keeps_text_adds_hook_and_enqueues_care(caplog):
    ib = _Inbox()
    ib.add("in", "I was telling you about my sister's wedding, so much drama", T0 - 600)
    ib.add("out", "omg tell me", T0 - 500)
    ib.add("in", "ok gotta sleep, good night!", T0 - 10)
    care = _Care()
    caplog.set_level(logging.INFO, logger="src.inbox.exit_gate")
    out, rep = xg.guard_exit("night night, talk later!", conversation_id=CONV, lang="en", cfg_root={},
                             inbox_store=ib, now=T0, care_store=care)
    assert rep["action"] == "allow" and rep["reason"] == "customer_bye"
    assert out.startswith("night night, talk later!") and len(out) > len("night night, talk later!")
    assert rep["care_id"] == 1 and len(care.rows) == 1
    row = care.rows[0]
    assert row["platform"] == "whatsapp" and row["account_id"] == "12137839654" and row["chat_key"] == "13105551234"
    assert row["topic_norm"].startswith(xg.FOLLOWUP_NORM_PREFIX + CONV)
    assert abs(row["due_at"] - (T0 + xg.DEFAULT_FOLLOWUP_HOURS * 3600)) < 1
    assert row["dedup_days"] == 1.0
    line = [r.getMessage() for r in caplog.records if "[exit]" in r.getMessage()]
    assert line and f"conv={CONV} allowed=1 reason=customer_bye" in line[0]


def test_allow_silence_policy_and_followup_toggle():
    care = _Care()
    cfg = {"inbox": {"exit_gate": {"allow_silence": True, "followup_hours": 1.5}}}
    out, rep = xg.guard_exit("我先忙了", conversation_id=CONV, lang="zh", cfg_root=cfg,
                             inbox_store=_Inbox(), now=T0, care_store=care)
    assert rep["action"] == "allow" and rep["reason"] == "allow_silence"
    assert "我先忙了" in out and len(care.rows) == 1
    assert abs(care.rows[0]["due_at"] - (T0 + 1.5 * 3600)) < 1
    care2 = _Care()
    cfg2 = {"inbox": {"exit_gate": {"allow_silence": True, "followup_on_silence": False}}}
    _out, rep2 = xg.guard_exit("我先忙了", conversation_id=CONV, lang="zh", cfg_root=cfg2,
                               inbox_store=_Inbox(), now=T0, care_store=care2)
    assert rep2["action"] == "allow" and rep2["care_id"] is None and not care2.rows


def test_allow_shift_end_via_schedule_state(monkeypatch):
    from src.inbox import work_hours_gate
    monkeypatch.setattr(work_hours_gate, "schedule_state", lambda ws, p, a, now_ts=None: {
        "enabled": True, "gated": True, "in_hours": True,
        "next_change_ts": (now_ts or T0) + 10 * 60, "next_change_kind": "close"})
    care = _Care()
    out, rep = xg.guard_exit("ok I gotta go, shift's ending", conversation_id=CONV, lang="en", cfg_root={},
                             inbox_store=_Inbox(), now=T0, care_store=care)
    assert rep["action"] == "allow" and rep["reason"] == "shift_end" and care.rows
    # 下班点在 grace 之外 → 不算合法
    monkeypatch.setattr(work_hours_gate, "schedule_state", lambda ws, p, a, now_ts=None: {
        "enabled": True, "gated": True, "in_hours": True,
        "next_change_ts": (now_ts or T0) + 3 * 3600, "next_change_kind": "close"})
    _out, rep2 = xg.guard_exit("ok I gotta go", conversation_id=CONV, lang="en", cfg_root={},
                               inbox_store=_Inbox(), now=T0, care_store=_Care())
    assert rep2["action"] == "rewrite"
    # 班表关 → 不算
    monkeypatch.setattr(work_hours_gate, "schedule_state", lambda ws, p, a, now_ts=None: {
        "enabled": False, "gated": False, "in_hours": True})
    _out, rep3 = xg.guard_exit("ok I gotta go", conversation_id=CONV, lang="en", cfg_root={},
                               inbox_store=_Inbox(), now=T0, care_store=_Care())
    assert rep3["action"] == "rewrite"


def test_shift_end_after_hours():
    from src.inbox import work_hours_gate
    orig = work_hours_gate.schedule_state
    try:
        work_hours_gate.schedule_state = lambda ws, p, a, now_ts=None: {"enabled": True, "gated": True, "in_hours": False}
        assert xg.shift_ending({}, "whatsapp", "1", now=T0) is True
    finally:
        work_hours_gate.schedule_state = orig


# ---------------- stats / 接线 ----------------

def test_stats_exit_claim_recorded_and_in_snapshot():
    xg.guard_exit("gotta go", conversation_id=CONV, lang="en", cfg_root={}, inbox_store=_Inbox(), now=T0)
    xg.guard_exit("talk later", conversation_id=CONV, lang="en", cfg_root={}, inbox_store=_Inbox(), now=T0)
    assert "exit_claim" in fp.KINDS
    snap = fp.snapshot()
    assert snap["counts"].get("exit_claim") == 2
    assert snap["exit_claim"]["hits"] == 2


def test_hooked_in_apply_draft_humanize_after_commitment_guard():
    from src.inbox import outbound_humanize
    src = inspect.getsource(outbound_humanize.apply_draft_humanize)
    assert src.index("apply_claim_rewrites(") < src.index("guard_exit(") < src.index("humanize(cur")
    out, meta = outbound_humanize.apply_draft_humanize(
        "Haha you're sweet. Anyway I gotta get back to work, have a good one!",
        conversation_id="", lang="en", origin="auto")
    assert meta.get("exit") == "rewrite"
    assert "back to work" not in (out or "").lower() and "have a good one" not in (out or "").lower()
    assert "?" in (out or "")


def test_verbatim_origin_bypasses_exit_gate():
    from src.inbox import outbound_humanize
    out, meta = outbound_humanize.apply_draft_humanize("gotta go", conversation_id="", lang="en", origin="verbatim")
    assert meta.get("skipped") and out == "gotta go"
