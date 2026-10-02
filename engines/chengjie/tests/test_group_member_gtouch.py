"""申报号龄 + 群里先接话（公开回复 TA 那句，之后私聊顺着接）。"""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.companion.group_member_extract import collect_speaker_context, user_row
from src.companion.group_member_gtouch import (
    classify_group_send_error,
    compose_gtouch,
    deliver_gtouch,
    finalize_gtouch,
    gtouch_block_reason,
    gtouch_preview,
    prepare_gtouch,
)
from src.companion.group_member_opener import opener_prompt
from src.companion.group_member_outreach import (
    OutreachPolicy,
    account_age_days,
    select_candidates,
)
from src.companion.group_members_store import (
    GroupMembersStore,
    configure_group_members_store,
    reset_group_members_store,
)

POL = OutreachPolicy(hours_start=0, hours_end=24)
TERMS = {"positive": ["客服", "翻译"], "negative": ["博彩"]}
NOW = 2_000_000_000.0


def _m(uid, *, gid="-100", acct="accA", text="客服回不过来怎么办", ts=NOW - 3600, mid="77", **kw):
    row = {"group_id": gid, "user_id": uid, "username": f"u{uid}", "first_name": f"N{uid}",
           "spoke": True, "is_admin": False, "group_title": "跨境群", "source_account_id": acct,
           "hash_account_id": acct, "access_hash": "12345", "extracted_at": NOW - 7200,
           "last_msg_text": text, "last_msg_ts": ts, "last_msg_id": mid}
    row.update(kw)
    return row


# ── 号龄 ──

def test_declared_age_grows_and_takes_the_larger():
    st = GroupMembersStore(":memory:")
    assert st.declared_age_days("accA", NOW) is None
    assert st.set_declared_age("accA", 400, NOW) == 400
    assert st.declared_age_days("accA", NOW + 2 * 86400) == pytest.approx(402)
    reg = {"created_at": NOW - 3 * 86400}
    assert account_age_days(reg, st, "accA", NOW) == pytest.approx(400)
    assert account_age_days(reg, st, "accB", NOW) == pytest.approx(3)
    assert account_age_days(None, st, "accB", NOW) is None
    assert st.set_declared_age("accA", 0, NOW) == 0
    assert account_age_days(reg, st, "accA", NOW) == pytest.approx(3)
    assert st.set_declared_age("accA", 99999, NOW) == 3650
    assert st.get_outreach_mode("accA") == "manual"


def test_declared_age_does_not_unlock_auto_without_track_record():
    from src.companion.group_member_outreach import auto_mode_block_reason
    st = GroupMembersStore(":memory:")
    st.set_declared_age("accA", 400, NOW)
    age = account_age_days(None, st, "accA", NOW)
    g = auto_mode_block_reason(st, "accA", now=NOW, age_days=age, policy=POL)
    assert g["reason"] == "declared_unproven" and g["detail"]["min"] == POL.auto_min_sample
    st.record_members([_m(str(i)) for i in range(POL.auto_min_sample)])
    for i in range(POL.auto_min_sample):
        st.cas_outreach("-100", str(i), expect_states=("none",), new_state="sent",
                        account_id="accA", now=NOW - 3600)
        if i < 3:
            st.mark_outreach_replied("accA", str(i), now=NOW - 60)
    assert auto_mode_block_reason(st, "accA", now=NOW, age_days=age, policy=POL)["ok"] is True
    # 没申报、靠真实天龄够格的号不受这条影响
    st2 = GroupMembersStore(":memory:")
    assert auto_mode_block_reason(st2, "accA", now=NOW, age_days=30, policy=POL)["ok"] is True


def test_flood_voids_declared_age():
    st = GroupMembersStore(":memory:")
    st.set_declared_age("accA", 400, NOW)
    st.set_hold("accA", paused=True)
    assert st.declared_age_days("accA", NOW) == pytest.approx(400)
    st.set_hold("accA", flood_until=NOW + 86400, reason="peer_flood", last_flood_at=NOW)
    assert st.declared_age_days("accA", NOW) is None
    assert account_age_days({"created_at": NOW - 2 * 86400}, st, "accA", NOW) == pytest.approx(2)


# ── 提取带消息 id ──

def test_speaker_context_keeps_id_of_the_text_message():
    def msg(uid, mid, text):
        return SimpleNamespace(from_user=SimpleNamespace(id=uid), id=mid, text=text, caption=None,
                               date=NOW - mid)

    class _C:
        async def get_chat_history(self, chat_id, limit=0):
            for m in (msg(1, 10, None), msg(1, 9, "需要翻译"), msg(2, 8, "hi")):
                yield m

    _seen, ctx = asyncio.run(collect_speaker_context(_C(), -100, 50))
    assert ctx[1]["id"] == 9 and ctx[1]["text"] == "需要翻译"
    assert ctx[2]["id"] == 8
    u = SimpleNamespace(id=1, username="a", first_name="A", last_name="", is_bot=False)
    row = user_row(u, group_id="-100", group_title="G", spoke=True, is_admin=False,
                   source_account_id="accA", job_id="j", batch_id="b", now=NOW, last_msg=ctx[1])
    assert row["last_msg_id"] == "9"
    assert user_row(u, group_id="-100", group_title="G", spoke=True, is_admin=False,
                    source_account_id="accA", job_id="j", batch_id="b", now=NOW,
                    last_msg={"text": "", "ts": NOW, "id": 5})["last_msg_id"] == ""


def test_rescan_updates_msg_id_only_for_newer_message():
    st = GroupMembersStore(":memory:")
    st.record_members([_m("1", ts=NOW - 100, mid="50")])
    st.record_members([_m("1", ts=NOW - 500, mid="40")])
    assert st.get_member("-100", "1")["last_msg_id"] == "50"
    st.record_members([_m("1", ts=NOW - 10, mid="60", text="翻译")])
    assert st.get_member("-100", "1")["last_msg_id"] == "60"


# ── 候选与预览 ──

def _seeded():
    st = GroupMembersStore(":memory:")
    st.record_members([
        _m("1"),                                       # 有意向
        _m("2", text="今天天气不错"),                    # 没意向 → 预览不列
        _m("3", ts=NOW - 5 * 86400),                   # 太旧
        _m("4", acct="accB"),                          # 别的号的群
        _m("5", mid=""),                               # 没消息 id
        _m("6", text="翻译软件推荐", ts=NOW - 60),         # 有意向，较新
        _m("7", text="客服 博彩"),                       # 排除词
        _m("8", gid="-200"),                           # 另一个群
    ])
    assert st.cas_outreach("-100", "6", expect_states=("none",), new_state="blocked",
                           account_id="accA", error="privacy", now=NOW)
    return st


def test_candidates_and_preview_order():
    st = _seeded()
    raw = {m["user_id"] for m in st.list_gtouch_candidates("accA", since_msg_ts=NOW - 48 * 3600)}
    assert raw == {"1", "2", "6", "7", "8"}
    pv = gtouch_preview(st, "accA", now=NOW, since_ts=NOW - 3600, policy=POL, intent=TERMS)
    ids = [c["user_id"] for c in pv["candidates"]]
    assert ids[0] == "6" and pv["candidates"][0]["dm_blocked"] is True
    assert set(ids) == {"1", "6", "8"}
    assert pv["remaining"] == 5 and pv["intent_configured"] is True
    assert "access_hash" not in str(pv)


def test_prepare_gates_and_finalize_paths():
    st = _seeded()
    kw = dict(account_id="accA", now=NOW, since_ts=NOW - 3600, policy=POL)
    assert prepare_gtouch(st, group_id="-100", user_id="1", text="试试分类回复 私聊我", **kw)["kind"] \
        == "gtouch_text_dm_ask"
    assert prepare_gtouch(st, group_id="-100", user_id="3", text="好", **kw)["kind"] == "gtouch_stale"
    assert prepare_gtouch(st, group_id="-100", user_id="4", text="好", **kw)["kind"] == "gtouch_state"
    closed = OutreachPolicy(hours_start=0, hours_end=1)
    assert prepare_gtouch(st, group_id="-100", user_id="1", text="好",
                          **dict(kw, policy=closed, now=NOW))["kind"] in ("hours", "ready")
    p = prepare_gtouch(st, group_id="-100", user_id="1", text="  消息多可以先按问题分类  ", **kw)
    assert p["ok"] and p["chat_id"] == -100 and p["reply_to"] == 77 and p["text"] == "消息多可以先按问题分类"
    # 已占坑 → 同一人不能再占；距上一条太近 → gap
    assert prepare_gtouch(st, group_id="-100", user_id="8", text="好", **kw)["kind"] == "gap"
    assert finalize_gtouch(st, account_id="accA", group_id="-100", user_id="1", now=NOW,
                           text=p["text"], result={"ok": True})["ok"]
    row = st.get_member("-100", "1")
    assert row["gtouch_state"] == "sent" and row["gtouch_account_id"] == "accA"
    assert st.count_gtouch_since("accA", NOW - 1) == 1
    # 慢速模式 → 还坑；群里被禁言 → 失败且这个群不再列人
    later = dict(kw, now=NOW + 3600)
    p8 = prepare_gtouch(st, group_id="-200", user_id="8", text="好的", **later)
    assert p8["ok"]
    finalize_gtouch(st, account_id="accA", group_id="-200", user_id="8", now=NOW + 3600,
                    text="好的", result={"ok": False, "kind": "slowmode"})
    assert st.get_member("-200", "8")["gtouch_state"] == "drafted"
    p8 = prepare_gtouch(st, group_id="-200", user_id="8", text="好的", **later)
    assert p8["ok"]
    finalize_gtouch(st, account_id="accA", group_id="-200", user_id="8", now=NOW + 3600,
                    text="好的", result={"ok": False, "kind": "group_denied"})
    assert st.gtouch_denied_groups("accA") == ["-200"]
    assert prepare_gtouch(st, group_id="-200", user_id="8", text="好", **dict(kw, now=NOW + 7200))["kind"] \
        in ("group_denied", "gtouch_state")


def test_caps_and_flood_hold():
    st = GroupMembersStore(":memory:")
    st.record_members([_m(str(i)) for i in range(1, 5)])
    pol = OutreachPolicy(hours_start=0, hours_end=24, gtouch_group_daily_cap=2, gtouch_min_gap_sec=60)
    t = NOW
    for uid in ("1", "2"):
        p = prepare_gtouch(st, account_id="accA", group_id="-100", user_id=uid, text="好",
                           now=t, since_ts=NOW - 3600, policy=pol)
        assert p["ok"]
        finalize_gtouch(st, account_id="accA", group_id="-100", user_id=uid, now=t, text="好",
                        result={"ok": True})
        t += 120
    assert prepare_gtouch(st, account_id="accA", group_id="-100", user_id="3", text="好",
                          now=t, since_ts=NOW - 3600, policy=pol)["kind"] == "gtouch_group_cap"
    pol2 = OutreachPolicy(hours_start=0, hours_end=24, gtouch_group_daily_cap=5, gtouch_min_gap_sec=60)
    p = prepare_gtouch(st, account_id="accA", group_id="-100", user_id="3", text="好",
                       now=t, since_ts=NOW - 3600, policy=pol2)
    assert p["ok"]
    assert finalize_gtouch(st, account_id="accA", group_id="-100", user_id="3", now=t, text="好",
                           result={"ok": False, "kind": "flood"})["kind"] == "flood"
    assert st.get_member("-100", "3")["gtouch_state"] == "drafted"
    assert prepare_gtouch(st, account_id="accA", group_id="-100", user_id="3", text="好",
                          now=t + 600, since_ts=NOW - 3600, policy=pol2)["kind"] == "flood"


def test_block_reason_and_error_buckets():
    assert gtouch_block_reason("私信太多可以先分类处理") == ""
    assert gtouch_block_reason("看我主页 https://x.cc") == "pitch"
    assert gtouch_block_reason("加我微信聊") == "pitch"
    assert gtouch_block_reason("@bob 你看看") == "dm_ask"
    assert gtouch_block_reason("x" * 300) == "too_long"

    class SlowmodeWait(Exception):
        pass

    class ChatWriteForbidden(Exception):
        pass

    assert classify_group_send_error(SlowmodeWait("A wait of 30 seconds")) == "slowmode"
    assert classify_group_send_error(ChatWriteForbidden("CHAT_WRITE_FORBIDDEN")) == "group_denied"
    assert classify_group_send_error(Exception("FLOOD_WAIT_X")) == "flood"
    assert classify_group_send_error(Exception("MESSAGE_ID_INVALID")) == "msg_gone"
    assert classify_group_send_error(Exception("boom")) == "retryable"


def test_deliver_replies_to_the_message():
    calls = []

    class _C:
        async def send_message(self, **kw):
            calls.append(kw)
            return SimpleNamespace(id=901)

    r = asyncio.run(deliver_gtouch(_C(), chat_id=-100, reply_to=77, text="hi"))
    assert r == {"ok": True, "kind": "sent", "msg_id": "901"}
    assert calls == [{"chat_id": -100, "text": "hi", "reply_to_message_id": 77}]

    class _Bad:
        async def send_message(self, **kw):
            raise Exception("CHAT_WRITE_FORBIDDEN")

    assert asyncio.run(deliver_gtouch(_Bad(), chat_id=-100, reply_to=77, text="hi"))["kind"] == "group_denied"


class _AI:
    def __init__(self, replies):
        self.replies = list(replies)
        self.prompts = []

    async def chat(self, prompt, strategy_overrides=None):
        self.prompts.append(prompt)
        return self.replies.pop(0) if self.replies else ""


def test_compose_filters_dm_asks_and_needs_ai():
    m = _m("1")
    ai = _AI(["有需要可以私聊我", "消息多的话先把常见问题做成快捷回复，能省一半时间"])
    got = asyncio.run(compose_gtouch(ai, member=m, ctx={"persona": None, "persona_block": ""}))
    assert got["text"].startswith("消息多的话") and got["reason"] == ""
    assert "公开回复" in ai.prompts[0] and "客服回不过来怎么办" in ai.prompts[0]
    assert "不要自称 AI" in ai.prompts[0]
    pub = {"identity": {"public_ai": True, "deny_ai": False}}
    ai2 = _AI(["我是个 AI，这类问题见得多：先做快捷回复"])
    got2 = asyncio.run(compose_gtouch(ai2, member=m, ctx={"persona": pub, "persona_block": ""}))
    assert got2["text"] and "大方说" in ai2.prompts[0]
    assert asyncio.run(compose_gtouch(None, member=m, ctx={}))["reason"] == "no_ai"
    assert asyncio.run(compose_gtouch(_AI(["私聊我", "私信我"]), member=m, ctx={}))["text"] == ""


# ── 私聊开口接上群里那句 ──

def test_dm_waits_after_group_touch_then_goes_first():
    base = dict(access_hash="1", outreach_state="none", is_bot=False, is_admin=False, spoke=True)
    a = _m("1", **base)
    b = dict(_m("2", **base), gtouch_state="sent", gtouch_account_id="accA", gtouch_at=NOW - 3600,
             last_msg_text="天气", gtouch_text="先做快捷回复")
    c = dict(_m("3", **base), gtouch_state="sent", gtouch_account_id="accB", gtouch_at=NOW - 60,
             gtouch_text="别的号回的")
    pick = lambda now: [m["user_id"] for m in select_candidates(
        [a, b, c], account_id="accA", slots=5, touched=set(), intent=TERMS, now=now,
        gtouch_wait_sec=6 * 3600)]
    assert pick(NOW) == ["1", "3"]
    assert pick(NOW + 6 * 3600) == ["2", "1", "3"]
    p = opener_prompt(persona_block="", member=dict(b, outreach_account_id="accA"), lang="zh",
                      goal_hint="", avoid=[])
    assert "已经在群里公开回过TA" in p
    p2 = opener_prompt(persona_block="", member=dict(c, outreach_account_id="accA"), lang="zh",
                       goal_hint="", avoid=[])
    assert "已经在群里公开回过TA" not in p2


# ── 跨群同一个人：接话按人算 ──

def _two_groups():
    st = GroupMembersStore(":memory:")
    st.record_members([
        _m("9"), _m("9", gid="-200", group_title="外贸群", text="今天好忙"),
        _m("10"), _m("10", gid="-200", group_title="外贸群", text="有没有好用的工具"),
    ])
    return st


def _send_gtouch(st, gid, uid, now, text="先做快捷回复"):
    p = prepare_gtouch(st, account_id="accA", group_id=gid, user_id=uid, text=text, now=now,
                       since_ts=now - 3600, policy=POL)
    assert p["ok"], p
    return finalize_gtouch(st, account_id="accA", group_id=gid, user_id=uid, now=now, text=text,
                           result={"ok": True})


def test_gtouch_once_per_person_across_groups():
    st = _two_groups()
    _send_gtouch(st, "-100", "9", NOW)
    cand = {(m["group_id"], m["user_id"])
            for m in st.list_gtouch_candidates("accA", since_msg_ts=NOW - 48 * 3600)}
    assert ("-200", "9") not in cand and ("-200", "10") in cand
    later = NOW + 3600
    assert prepare_gtouch(st, account_id="accA", group_id="-200", user_id="9", text="好",
                          now=later, since_ts=later - 3600, policy=POL)["kind"] == "gtouch_state"


def test_dm_in_other_group_waits_and_quotes_the_touched_message():
    from src.companion.group_member_outreach import build_preview
    st = _two_groups()
    _send_gtouch(st, "-100", "10", NOW)
    pv = build_preview(st, "accA", now=NOW + 60, since_ts=NOW - 3600, policy=POL, age_days=30)
    assert "10" not in {c["user_id"] for c in pv["candidates"]}
    pv = build_preview(st, "accA", now=NOW + 7 * 3600, since_ts=NOW, policy=POL, age_days=30)
    ids = [c["user_id"] for c in pv["candidates"]]
    assert ids[0] == "10"
    from src.companion.group_member_outreach import overlay_gtouch
    row = [m for m in overlay_gtouch(st, [st.get_member("-200", "10")], "accA")][0]
    p = opener_prompt(persona_block="", member=dict(row, outreach_account_id="accA"), lang="zh",
                      goal_hint="", avoid=[])
    assert "群「跨境群」里公开回过TA说的「客服回不过来怎么办」" in p and "先做快捷回复" in p


def test_queued_dm_is_requeued_and_held_after_group_touch():
    from src.companion.group_member_outreach import prepare_release
    st = _two_groups()
    assert st.cas_outreach("-200", "9", expect_states=("none",), new_state="queued",
                           account_id="accA", now=NOW - 600)
    st.set_opener("-200", "9", "你好呀，最近忙啥", "ai")
    assert st.approve_queued("accA", NOW - 500) == 1
    out = _send_gtouch(st, "-100", "9", NOW)
    assert out["requeued"] == 1
    row = st.get_member("-200", "9")
    assert row["outreach_state"] == "queued" and row["opener_text"] == ""
    st.set_opener("-200", "9", "刚群里说的快捷回复，你那边消息量大概多少", "ai")
    st.approve_queued("accA", NOW + 60)
    assert st.next_approved("accA", gtouch_after_ts=NOW + 120 - 6 * 3600) is None
    kw = dict(account_id="accA", group_id="-200", user_id="9", text="", since_ts=NOW,
              policy=POL, age_days=30)
    r = prepare_release(st, now=NOW + 120, **kw)
    assert r["kind"] == "gtouch_wait" and r["gap_wait_sec"] > 5 * 3600
    t = NOW + 6 * 3600 + 10
    assert st.next_approved("accA", gtouch_after_ts=t - 6 * 3600)["user_id"] == "9"
    assert prepare_release(st, now=t, **kw)["ok"]


def test_manual_opener_survives_requeue():
    st = _two_groups()
    st.cas_outreach("-200", "9", expect_states=("none",), new_state="queued",
                    account_id="accA", now=NOW - 600)
    st.set_opener("-200", "9", "坐席手写的", "manual")
    st.approve_queued("accA", NOW - 500)
    _send_gtouch(st, "-100", "9", NOW)
    row = st.get_member("-200", "9")
    assert row["outreach_state"] == "queued" and row["opener_text"] == "坐席手写的"


def test_stale_gtouch_sending_is_reaped_and_not_resent():
    st = _two_groups()
    p = prepare_gtouch(st, account_id="accA", group_id="-100", user_id="9", text="好",
                       now=NOW, since_ts=NOW - 3600, policy=POL)
    assert p["ok"]
    assert st.reap_stale_gtouch(NOW + 60) == 0
    assert st.reap_stale_gtouch(NOW + 700) == 1
    row = st.get_member("-100", "9")
    assert row["gtouch_state"] == "failed" and row["gtouch_error"] == "stale_sending"
    cand = {(m["group_id"], m["user_id"])
            for m in st.list_gtouch_candidates("accA", since_msg_ts=NOW - 48 * 3600)}
    assert ("-100", "9") not in cand and ("-200", "9") not in cand


# ── 群里接话后 TA 私聊来找：归到这条线 ──

def test_privacy_blocked_member_dming_after_group_touch_is_attributed():
    from src.companion.group_member_outreach import attach_won, outreach_context_note
    st = _seeded()                      # 6 号私聊被隐私挡住
    p = prepare_gtouch(st, account_id="accA", group_id="-100", user_id="6", text="先分类处理",
                       now=NOW, since_ts=NOW - 3600, policy=POL)
    finalize_gtouch(st, account_id="accA", group_id="-100", user_id="6", now=NOW, text=p["text"],
                    result={"ok": True})
    assert st.mark_outreach_replied("accB", "6", now=NOW + 60, text="你好") == 0
    assert st.mark_outreach_replied("accA", "6", now=NOW + 600, text="你说的分类怎么做") == 1
    row = st.get_member("-100", "6")
    assert row["outreach_state"] == "replied" and row["outreach_error"] == "gtouch_inbound"
    assert row["outreach_account_id"] == "accA" and row["outreach_at"] == 0
    assert st.reply_rate("accA", NOW - 86400)["sent"] == 0
    # 后续来话不重复计
    assert st.mark_outreach_replied("accA", "6", now=NOW + 900, text="在吗") == 0
    note = outreach_context_note("accA", "6", store=st)
    assert "群里公开回过TA，TA来私聊" in note and len(note) <= 80
    contacted = st.contacted_replies(NOW - 86400, "accA")
    assert [c["user_id"] for c in contacted] == ["6"]
    stats = attach_won({"funnel": {"sent": 0}, "by_variant": []}, contacted,
                       {("accA", "6"): {"done_at": NOW + 3600, "manual": False}})
    assert stats["won"]["total"] == 1 and stats["won"]["via_gtouch"] == 1
    assert stats["won"]["recent"][0]["via_gtouch"] is True


def test_untouched_member_dming_is_not_attributed():
    st = _seeded()
    assert st.mark_outreach_replied("accA", "1", now=NOW, text="hi") == 0
    assert st.get_member("-100", "1")["outreach_state"] == "none"


def test_context_note_mentions_group_touch_for_dm_in_other_group():
    from src.companion.group_member_outreach import outreach_context_note
    st = _two_groups()
    _send_gtouch(st, "-100", "9", NOW)
    st.cas_outreach("-200", "9", expect_states=("none",), new_state="sent", account_id="accA",
                    now=NOW + 7 * 3600)
    st.mark_outreach_replied("accA", "9", now=NOW + 8 * 3600, text="好啊")
    note = outreach_context_note("accA", "9", store=st)
    assert "群里回过TA又私聊了" in note and len(note) <= 80


def test_gtouch_stats_layer_and_warm_vs_cold():
    st = _two_groups()
    st.record_members([_m("11"), _m("12")])
    _send_gtouch(st, "-100", "9", NOW)                       # 接话 → 6h 后私聊 → 回了
    _send_gtouch(st, "-100", "10", NOW + 700)                # 接话 → TA 主动私聊来
    st.cas_outreach("-200", "9", expect_states=("none",), new_state="sent", account_id="accA",
                    now=NOW + 7 * 3600)
    st.mark_outreach_replied("accA", "9", now=NOW + 8 * 3600, text="好")
    st.mark_outreach_replied("accA", "10", now=NOW + 3600, text="你说的工具是哪个")
    for uid in ("11", "12"):                                  # 两条冷私聊，一条回了
        st.cas_outreach("-100", uid, expect_states=("none",), new_state="sent", account_id="accA",
                        now=NOW + 2 * 3600)
    st.mark_outreach_replied("accA", "11", now=NOW + 3 * 3600, text="嗯")
    g = st.gtouch_stats(NOW - 3600, "accA")
    assert g["sent"] == 2 and g["responded"] == 2 and g["inbound"] == 1 and g["dm_after"] == 1
    assert g["dm_warm"] == {"sent": 1, "replied": 1, "rate": 1.0}
    assert g["dm_cold"] == {"sent": 2, "replied": 1, "rate": 0.5}
    assert st.gtouch_stats(NOW - 3600, "accB")["sent"] == 0


# ── 路由 ──

class _CM:
    config = {"companion": {"group_members": {"enabled": True, "outreach_hours": [0, 24]}}}


class _Reg:
    def get(self, platform, account_id):
        return {"created_at": time.time() - 86400}


@pytest.fixture()
def client():
    reset_group_members_store()
    st = configure_group_members_store(":memory:")
    now = time.time()
    st.record_members([_m("1", ts=now - 600), _m("2", ts=now - 600, text="天气")])
    from src.web.routes.group_members_routes import register_group_members_routes
    app = FastAPI()
    app.state.account_registry = _Reg()

    async def _auth():
        return True

    register_group_members_routes(app, auth_dep=_auth, audit_store=None, config_manager=_CM())
    with TestClient(app) as c:
        yield c, st
    reset_group_members_store()


def test_routes_age_and_gtouch(client, monkeypatch):
    c, st = client
    assert c.post("/api/tg-members/outreach/age", json={"account_id": "accA", "days": -1}).status_code == 400
    r = c.post("/api/tg-members/outreach/age", json={"account_id": "accA", "days": 365}).json()
    assert r["declared_days"] == 365 and r["age_days"] == pytest.approx(365, abs=0.1)
    pv = c.get("/api/tg-members/outreach/preview?account_id=accA").json()
    assert pv["age"]["declared_days"] == pytest.approx(365, abs=0.1)
    assert pv["age"]["registry_days"] == pytest.approx(1, abs=0.1)
    assert pv["warming_up"] is False
    import src.companion.group_member_opener as op
    real = op.build_opener_context

    def _ctx(*a, **k):
        out = real(*a, **k)
        out["intent"] = TERMS
        return out

    monkeypatch.setattr(op, "build_opener_context", _ctx)
    g = c.get("/api/tg-members/gtouch/preview?account_id=accA").json()
    assert [x["user_id"] for x in g["candidates"]] == ["1"] and "12345" not in str(g)
    body = {"account_id": "accA", "group_id": "-100", "user_id": "1"}
    assert c.post("/api/tg-members/gtouch/compose", json=body).json() == \
        {"ok": False, "text": "", "reason": "no_ai"}
    assert c.post("/api/tg-members/gtouch/send", json=dict(body, text="好")).status_code == 400
    r = c.post("/api/tg-members/gtouch/send", json=dict(body, text="私聊我", confirm=True))
    assert r.status_code == 400
    r = c.post("/api/tg-members/gtouch/send", json=dict(body, text="先做快捷回复", confirm=True))
    assert r.status_code == 503
    assert st.get_member("-100", "1")["gtouch_state"] == "drafted"
    assert c.post("/api/tg-members/gtouch/skip", json=body).json()["ok"] is True
    assert c.get("/api/tg-members/gtouch/preview?account_id=accA").json()["candidates"] == []
