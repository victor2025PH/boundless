"""同群开口：额度夹紧、新号爬坡、时段、选人、占坑、错误分桶、回音。不碰真 Telegram。"""
import time

from src.companion.group_member_outreach import (
    OutreachPolicy,
    build_preview,
    classify_send_error,
    clamp_outreach_cap,
    effective_outreach_cap,
    enqueue_today,
    finalize_release,
    hours_block_reason,
    note_inbound_reply,
    opener_block_reason,
    prepare_release,
    select_candidates,
    suggest_opener,
)
from src.companion.group_members_store import (
    OUTREACH_BLOCKED,
    OUTREACH_HOLD_ALL,
    OUTREACH_NONE,
    OUTREACH_QUEUED,
    OUTREACH_REPLIED,
    OUTREACH_SENDING,
    OUTREACH_SENT,
    OUTREACH_SKIPPED,
    GroupMembersStore,
)

# 测试里天龄固定给足、时段固定给在窗内，只在专门的用例里改它们。
POLICY = OutreachPolicy(cap=8, min_gap_sec=1500)
OLD = 30.0
OPEN_HOUR = 12
TEXT = "在群里看到你发言，打个招呼。"


def _row(**kw):
    base = {
        "group_id": "-100", "user_id": "1", "username": "neo", "first_name": "Neo",
        "last_name": "", "is_admin": False, "is_bot": False, "spoke": True,
        "last_spoke_ts": 100.0, "group_title": "Bug群", "source_account_id": "accA",
        "hash_account_id": "accA", "access_hash": "555", "score": 90,
        "outreach_state": "none", "extracted_at": 1.0,
    }
    base.update(kw)
    return base


def _prep(st, uid, now, **kw):
    args = dict(account_id="accA", group_id="-100", user_id=uid, text=TEXT, now=now,
                since_ts=now - 10, policy=POLICY, age_days=OLD, hour=OPEN_HOUR)
    args.update(kw)
    return prepare_release(st, **args)


def test_cap_is_clamped_away_from_extract_quota():
    assert clamp_outreach_cap(200) == 10
    assert clamp_outreach_cap(1) == 5
    assert clamp_outreach_cap(8) == 8
    assert clamp_outreach_cap("nope") == 8


def test_policy_from_config_tolerates_bad_values():
    p = OutreachPolicy.from_config({
        "outreach_daily_cap": 200, "outreach_min_gap_sec": "x",
        "outreach_warmup_start_cap": 0, "outreach_hours": [21, 10],
    })
    assert p.cap == 10 and p.min_gap_sec == 1500
    assert p.warmup_start == 1
    assert (p.hours_start, p.hours_end) == (10, 21)
    assert OutreachPolicy.from_config(None) == OutreachPolicy()
    q = OutreachPolicy.from_config({"outreach_hours": [9, 22], "outreach_warmup_ramp_days": 7})
    assert (q.hours_start, q.hours_end, q.warmup_days) == (9, 22, 7)


def test_new_account_ramps_from_three_to_cap():
    p = OutreachPolicy(cap=8, warmup_start=3, warmup_days=14)
    assert effective_outreach_cap(p, None) == 3        # 天龄不明按新号
    assert effective_outreach_cap(p, 0) == 3
    assert effective_outreach_cap(p, 7) == 5
    assert effective_outreach_cap(p, 14) == 8
    assert effective_outreach_cap(p, 400) == 8
    assert effective_outreach_cap(OutreachPolicy(cap=8, warmup_days=0), None) == 8


def test_hours_window_is_half_open_local():
    p = OutreachPolicy(hours_start=10, hours_end=21)
    assert hours_block_reason(p, 0, hour=9) == "hours"
    assert hours_block_reason(p, 0, hour=10) == ""
    assert hours_block_reason(p, 0, hour=20) == ""
    assert hours_block_reason(p, 0, hour=21) == "hours"


def test_opener_rejects_pitch_and_mentions_the_group():
    assert "Bug群" in suggest_opener(_row())
    assert opener_block_reason("") == "empty"
    assert opener_block_reason("加我微信") == "pitch"
    assert opener_block_reason("see https://t.me/x") == "pitch"
    assert opener_block_reason(TEXT) == ""


def test_select_skips_admin_other_account_and_already_touched():
    members = [
        _row(user_id="1", username="neo", last_spoke_ts=10),
        _row(user_id="2", username="", last_spoke_ts=99, first_name="晚"),
        _row(user_id="3", is_admin=True, username="adm"),
        _row(user_id="4", hash_account_id="accB", access_hash="9"),
        _row(user_id="5", access_hash=""),
        _row(user_id="1", group_id="-200", last_spoke_ts=1000),  # 同一人另一个群
    ]
    picked = select_candidates(members, account_id="accA", slots=5, touched={"9"})
    ids = [(m["group_id"], m["user_id"]) for m in picked]
    assert ids[0] == ("-200", "1")          # 有用户名且最近发言；跨群只留一条
    assert ("-100", "1") not in ids
    assert all(m["user_id"] not in {"3", "4", "5"} for m in picked)
    assert [m["user_id"] for m in picked] == ["1", "2"]


def test_queue_send_gap_and_flood_do_not_burn_the_cap():
    st = GroupMembersStore(":memory:")
    now = 1_000_000.0
    st.record_members([
        _row(user_id="1"),
        _row(user_id="2", username="bee", first_name="Bee", last_spoke_ts=50),
    ])
    since = now - 10
    q = enqueue_today(st, "accA", now=now, since_ts=since, policy=POLICY, age_days=OLD)
    assert q["queued_now"] == 2
    assert st.get_member("-100", "1")["outreach_state"] == OUTREACH_QUEUED

    bad = _prep(st, "1", now, text="https://t.me/x")
    assert bad["kind"] == "text"
    assert st.get_member("-100", "1")["outreach_state"] == OUTREACH_QUEUED

    ready = _prep(st, "1", now, since_ts=since)
    assert ready["ok"] is True and ready["access_hash"] == 555
    done = finalize_release(
        st, account_id="accA", group_id="-100", user_id="1", now=now,
        result={"ok": True, "kind": "sent"})
    assert done["kind"] == "sent"
    assert st.count_outreach_sent_since("accA", since) == 1

    too_soon = _prep(st, "2", now + 10, since_ts=since)
    assert too_soon["kind"] == "gap"
    assert st.get_member("-100", "2")["outreach_state"] == OUTREACH_QUEUED

    later = now + 2000
    ready2 = _prep(st, "2", later, since_ts=since)
    assert ready2["ok"] is True
    flooded = finalize_release(
        st, account_id="accA", group_id="-100", user_id="2", now=later,
        result={"ok": False, "kind": "flood"})
    assert flooded["kind"] == "flood"
    assert st.get_member("-100", "2")["outreach_state"] == OUTREACH_NONE
    assert st.count_outreach_sent_since("accA", since) == 1
    assert st.get_hold("accA")["flood_until"] > later

    blocked = enqueue_today(st, "accA", now=later + 1, since_ts=since, policy=POLICY,
                            age_days=OLD)
    assert blocked["kind"] == "flood"


def test_warmup_cap_limits_queue_and_send_for_a_new_account():
    st = GroupMembersStore(":memory:")
    now = 1_500_000.0
    st.record_members([_row(user_id=str(i), username="u%d" % i) for i in range(1, 7)])
    q = enqueue_today(st, "accA", now=now, since_ts=now - 1, policy=POLICY, age_days=0)
    assert q["queued_now"] == 3                     # 新号第 0 天只排 3 个
    pv = build_preview(st, "accA", now=now, since_ts=now - 1, policy=POLICY, age_days=0,
                       hour=OPEN_HOUR)
    assert pv["effective_cap"] == 3 and pv["cap"] == 8 and pv["warming_up"] is True
    assert pv["remaining"] == 0 and pv["hours"]["ok"] is True

    # 三条都发出去后，第四条即使还在队列也被 cap 拦
    t = now
    for uid in [m["user_id"] for m in pv["queue"]]:
        assert _prep(st, uid, t, since_ts=now - 1, age_days=0)["ok"]
        finalize_release(st, account_id="accA", group_id="-100", user_id=uid, now=t,
                         result={"ok": True, "kind": "sent"})
        t += 2000
    st.cas_outreach("-100", "6", expect_states=(OUTREACH_NONE,), new_state=OUTREACH_QUEUED,
                    account_id="accA", now=t)
    assert _prep(st, "6", t, since_ts=now - 1, age_days=0)["kind"] == "cap"
    # 养熟的号同样 3 条已发、额度 8 → 放行
    assert _prep(st, "6", t, since_ts=now - 1, age_days=OLD)["ok"] is True


def test_outside_hours_blocks_send_but_not_queue():
    st = GroupMembersStore(":memory:")
    now = 1_600_000.0
    st.record_members([_row()])
    assert enqueue_today(st, "accA", now=now, since_ts=now - 1, policy=POLICY,
                         age_days=OLD)["queued_now"] == 1
    late = _prep(st, "1", now, hour=23)
    assert late["kind"] == "hours" and late["http"] == 409
    assert st.get_member("-100", "1")["outreach_state"] == OUTREACH_QUEUED
    pv = build_preview(st, "accA", now=now, since_ts=now - 1, policy=POLICY, age_days=OLD,
                       hour=23)
    assert pv["hours"]["ok"] is False
    assert _prep(st, "1", now, hour=10)["ok"] is True


def test_privacy_blocks_the_person_not_the_account():
    st = GroupMembersStore(":memory:")
    now = 2_000_000.0
    st.record_members([_row(user_id="7", username="p")])
    enqueue_today(st, "accA", now=now, since_ts=now - 1, policy=POLICY, age_days=OLD)
    assert _prep(st, "7", now)["ok"]
    finalize_release(
        st, account_id="accA", group_id="-100", user_id="7", now=now,
        result={"ok": False, "kind": "privacy"})
    assert st.get_member("-100", "7")["outreach_state"] == OUTREACH_BLOCKED
    assert st.count_outreach_sent_since("accA", now - 1) == 0
    preview = build_preview(st, "accA", now=now + 120, since_ts=now - 1, policy=POLICY,
                            age_days=OLD, hour=OPEN_HOUR)
    assert preview["candidates"] == [] and preview["hold"] == ""


def test_reply_marks_replied_and_still_counts_as_sent():
    st = GroupMembersStore(":memory:")
    now = 2_500_000.0
    st.record_members([_row(user_id="7", username="p"), _row(user_id="7", group_id="-200")])
    enqueue_today(st, "accA", now=now, since_ts=now - 1, policy=POLICY, age_days=OLD)
    assert _prep(st, "7", now, group_id="-100")["ok"] or _prep(st, "7", now, group_id="-200")["ok"]
    sent_gid = "-100" if st.get_member("-100", "7")["outreach_state"] == OUTREACH_SENDING else "-200"
    finalize_release(st, account_id="accA", group_id=sent_gid, user_id="7", now=now,
                     result={"ok": True, "kind": "sent"})

    # 别的号收到同一个人的消息不算；群消息（负 chat_id）不算；回填不算
    assert note_inbound_reply({"platform": "telegram", "account_id": "accB",
                               "chat_key": "7", "direction": "in"}, st) == 0
    assert note_inbound_reply({"platform": "telegram", "account_id": "accA",
                               "chat_key": "-100", "direction": "in"}, st) == 0
    assert note_inbound_reply({"platform": "telegram", "account_id": "accA",
                               "chat_key": "7", "direction": "in", "backfill": True}, st) == 0
    assert note_inbound_reply({"platform": "telegram", "account_id": "accA",
                               "chat_key": "7", "direction": "in"}, st) == 1
    assert st.get_member(sent_gid, "7")["outreach_state"] == OUTREACH_REPLIED
    assert st.count_outreach_sent_since("accA", now - 1) == 1
    # 第二条回复幂等
    assert note_inbound_reply({"platform": "telegram", "account_id": "accA",
                               "chat_key": "7"}, st) == 0


def test_emit_incoming_hook_marks_reply(monkeypatch):
    from src.companion import group_members_store as gms
    from src.integrations import protocol_bridge as pb

    gms.reset_group_members_store()
    st = gms.configure_group_members_store(":memory:")
    try:
        now = 2_600_000.0
        st.record_members([_row(user_id="8", username="q")])
        st.cas_outreach("-100", "8", expect_states=(OUTREACH_NONE,), new_state=OUTREACH_SENT,
                        account_id="accA", now=now)
        seen = []
        old_sink = pb.get_inbox_sink()
        pb.register_inbox_sink(seen.append)
        try:
            pb.emit_incoming({"platform": "telegram", "account_id": "accA",
                              "chat_key": "8", "direction": "in", "text": "hi"})
        finally:
            pb.register_inbox_sink(old_sink)
        assert len(seen) == 1
        assert st.get_member("-100", "8")["outreach_state"] == OUTREACH_REPLIED
    finally:
        gms.reset_group_members_store()


def test_stale_sending_is_skipped_not_resent():
    st = GroupMembersStore(":memory:")
    now = 2_700_000.0
    st.record_members([_row()])
    st.cas_outreach("-100", "1", expect_states=(OUTREACH_NONE,), new_state=OUTREACH_SENDING,
                    account_id="accA", now=now - 1000)
    assert st.reap_stale_sending(now) == 1
    row = st.get_member("-100", "1")
    assert row["outreach_state"] == OUTREACH_SKIPPED
    assert "1" in st.touched_user_ids()
    assert st.count_outreach_sent_since("accA", now - 5000) == 0


def test_hash_backfill_does_not_replace_another_accounts_credential():
    st = GroupMembersStore(":memory:")
    st.record_members([_row(access_hash="", hash_account_id="")])
    assert st.get_member("-100", "1")["access_hash"] == ""
    st.record_members([_row(access_hash="777", hash_account_id="accA")])
    assert st.get_member("-100", "1")["access_hash"] == "777"
    st.record_members([_row(access_hash="888", hash_account_id="accB")])
    got = st.get_member("-100", "1")
    assert got["access_hash"] == "777" and got["hash_account_id"] == "accA"


def test_global_stop_blocks_every_account_and_resume_keeps_flood():
    st = GroupMembersStore(":memory:")
    now = 3_000_000.0
    st.record_members([_row()])
    st.set_hold(OUTREACH_HOLD_ALL, paused=True, reason="operator_stop")
    assert enqueue_today(st, "accA", now=now, since_ts=now - 1, policy=POLICY,
                         age_days=OLD)["kind"] == "paused"
    st.clear_outreach_pause("")
    assert enqueue_today(st, "accA", now=now, since_ts=now - 1, policy=POLICY,
                         age_days=OLD)["ok"] is True
    st.set_hold("accA", flood_until=now + 100, reason="peer_flood")
    st.clear_outreach_pause("accA")
    assert st.get_hold("accA")["flood_until"] == now + 100
    assert enqueue_today(st, "accA", now=now + 1, since_ts=now - 1, policy=POLICY,
                         age_days=OLD)["kind"] == "flood"


def test_classify_send_error_buckets():
    class PeerFlood(Exception):
        pass

    class FloodWait(Exception):
        pass

    class UserPrivacyRestricted(Exception):
        pass

    class InputUserDeactivated(Exception):
        pass

    class PeerIdInvalid(Exception):
        pass

    assert classify_send_error(PeerFlood()) == "flood"
    assert classify_send_error(FloodWait()) == "flood"
    assert classify_send_error(UserPrivacyRestricted()) == "privacy"
    assert classify_send_error(InputUserDeactivated()) == "deactivated"
    assert classify_send_error(PeerIdInvalid()) == "peer_invalid"
    assert classify_send_error(RuntimeError("timeout")) == "retryable"


def test_migrate_outreach_columns_on_old_db(tmp_path):
    import sqlite3
    db = str(tmp_path / "old.db")
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE tg_group_members ("
        " group_id TEXT NOT NULL, user_id TEXT NOT NULL,"
        " username TEXT NOT NULL DEFAULT '', first_name TEXT NOT NULL DEFAULT '',"
        " last_name TEXT NOT NULL DEFAULT '', is_admin INTEGER NOT NULL DEFAULT 0,"
        " is_bot INTEGER NOT NULL DEFAULT 0, spoke INTEGER NOT NULL DEFAULT 0,"
        " last_spoke_ts REAL NOT NULL DEFAULT 0, group_title TEXT NOT NULL DEFAULT '',"
        " source_account_id TEXT NOT NULL DEFAULT '', job_id TEXT NOT NULL DEFAULT '',"
        " batch_id TEXT NOT NULL DEFAULT '', outreach_state TEXT NOT NULL DEFAULT 'none',"
        " extracted_at REAL NOT NULL DEFAULT 0, score INTEGER NOT NULL DEFAULT 0,"
        " PRIMARY KEY (group_id, user_id))")
    conn.commit()
    conn.close()
    st = GroupMembersStore(db)
    st.record_members([_row()])
    row = st.get_member("-100", "1")
    assert row["access_hash"] == "555"
    assert row["outreach_state"] == OUTREACH_NONE
    st.cas_outreach("-100", "1", expect_states=(OUTREACH_NONE,),
                    new_state=OUTREACH_SENT, account_id="accA", now=10)
    assert st.get_member("-100", "1")["outreach_state"] == OUTREACH_SENT
    assert st.mark_outreach_replied("accA", "1") == 1
    # 二期列 + holds.mode 也要在旧库上长出来
    assert row["opener_text"] == "" and row["lang_code"] == ""
    assert st.get_outreach_mode("accA") == "manual"
    assert st.set_outreach_mode("accA", "approve") == "approve"
    assert st.get_hold("accA")["mode"] == "approve"


# ── 二期：AI 开口 / 批准态 / 对方拒绝 / 调度器 ───────────────────────────────

import asyncio

from src.companion.group_member_opener import (
    compose_opener,
    compose_queue,
    language_mismatch,
    member_lang,
    postprocess_opener,
    template_opener,
    too_similar,
)
from src.companion.group_member_outreach_runner import OutreachRunner
from src.companion.group_members_store import OUTREACH_APPROVED


class _AI:
    """假 LLM：按次序吐预设答复，记录 prompt。"""

    def __init__(self, replies):
        self.replies = list(replies)
        self.prompts = []

    async def chat(self, prompt, strategy_overrides=None):
        self.prompts.append(prompt)
        return self.replies.pop(0) if self.replies else ""


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class _NoRegistry:
    """空注册表：避免测试里 resolve 人设时去建真实 account_registry.db。"""

    def get(self, platform, account_id):
        return None


def test_member_lang_prefers_language_code_then_cjk():
    assert member_lang({"lang_code": "zh-hans"}) == "zh"
    assert member_lang({"lang_code": "ru"}) == "ru"
    assert member_lang({"lang_code": "", "last_msg_text": "有人用过吗"}) == "zh"
    assert member_lang({"lang_code": "", "last_msg_text": "anyone tried"}) == "en"
    assert member_lang({"lang_code": "", "group_title": "美股交流"}) == "zh"


def test_postprocess_filters_pitch_ai_identity_language_and_dupes():
    m = {"first_name": "Neo", "group_title": "Bug群", "last_msg_text": "有人试过这个吗"}
    assert postprocess_opener("Neo，看到你问那个，我也试过，私聊聊？", persona=None, lang="zh",
                              avoid=[])["reason"] == ""
    assert postprocess_opener("加我微信聊", persona=None, lang="zh", avoid=[])["reason"]
    assert postprocess_opener("https://t.me/x 看看", persona=None, lang="zh", avoid=[])["reason"]
    assert postprocess_opener("Hi Neo, saw you in the group", persona=None, lang="zh",
                              avoid=[])["reason"] == "lang"
    assert postprocess_opener("在吗", persona=None, lang="zh", avoid=[])["reason"] == "filler"
    assert postprocess_opener("我是AI助手，很高兴为你服务", persona=None, lang="zh",
                              avoid=[])["reason"] in ("ai_self_id", "filler", "")
    assert postprocess_opener("x" * 200, persona=None, lang="zh", avoid=[])["reason"] == "too_long"
    same = "Neo，在群里看到你那条，想聊两句。"
    assert postprocess_opener(same, persona=None, lang="zh", avoid=[same])["reason"] == "similar"
    # 引号/多行输出会被剥干净
    got = postprocess_opener('"Neo，刚看到你说那个，我也遇到过。"\n（解释：……）', persona=None,
                             lang="zh", avoid=[])
    assert got["reason"] == "" and got["text"].startswith("Neo，") and "解释" not in got["text"]
    assert too_similar("abc def", ["abc def!"]) is True
    assert language_mismatch("Hello", "zh") and not language_mismatch("你好", "zh")


def test_compose_opener_uses_ai_then_regenerates_then_falls_back():
    m = _row(user_id="7", first_name="Neo", last_msg_text="有人用过这个吗", lang_code="zh")
    ctx = {"persona_block": "你是老周，做跨境物流十年。", "goal_hint": "认识同行", "persona": None}
    ai = _AI(["Neo，你问的那个我用过半年，有坑可以聊。"])
    got = _run(compose_opener(ai, member=m, ctx=ctx))
    assert got["source"] == "ai" and "Neo" in got["text"]
    assert "老周" in ai.prompts[0] and "有人用过这个吗" in ai.prompts[0] and "认识同行" in ai.prompts[0]

    # 第一稿带链接 → 重生成一次 → 第二稿干净
    ai2 = _AI(["看这个 https://x.y", "Neo，那个问题我也踩过，私聊说？"])
    got2 = _run(compose_opener(ai2, member=m, ctx=ctx))
    assert got2["source"] == "ai" and "https" not in got2["text"] and len(ai2.prompts) == 2

    # 两稿都不行 → 模板，且记下最后一次被拒原因
    ai3 = _AI(["加我微信", "加我 whatsapp"])
    got3 = _run(compose_opener(ai3, member=m, ctx=ctx))
    assert got3["source"] == "template" and got3["reason"] == "pitch"
    assert not language_mismatch(got3["text"], "zh")

    # 没有 LLM → 直接模板；英文成员用英文模板
    en = _row(user_id="8", first_name="Bob", lang_code="en", last_msg_text="anyone tried")
    got4 = _run(compose_opener(None, member=en, ctx=ctx))
    assert got4["source"] == "template" and "Bob" in got4["text"]
    assert not language_mismatch(got4["text"], "en")


def test_template_opener_rotates_away_from_todays_texts():
    m = _row(first_name="Neo", group_title="Bug群", last_msg_text="")
    first = template_opener(m, "zh", seed=1)
    second = template_opener(m, "zh", seed=1, avoid=[first])
    assert first != second


def test_compose_queue_fills_only_missing_and_respects_manual():
    st = GroupMembersStore(":memory:")
    now = 2_000_000.0
    st.record_members([_row(user_id="1", lang_code="zh"), _row(user_id="2", username="b",
                                                                 first_name="Bee", lang_code="zh")])
    enqueue_today(st, "accA", now=now, since_ts=now - 1, policy=POLICY, age_days=OLD)
    st.set_opener("-100", "2", "Bee 我自己写的", "manual")
    ai = _AI(["Neo，群里看到你了，聊两句？"])
    r = _run(compose_queue(st, ai, account_id="accA", ctx={}, since_ts=now - 1))
    assert r["composed"] == 1 and r["skipped"] == 1 and r["ai"] == 1
    assert st.get_member("-100", "1")["opener_source"] == "ai"
    assert st.get_member("-100", "2")["opener_text"] == "Bee 我自己写的"
    # force 也不动 manual
    r2 = _run(compose_queue(st, _AI(["Neo，另一个版本。"]), account_id="accA", ctx={},
                            since_ts=now - 1, force=True))
    assert r2["composed"] == 1 and st.get_member("-100", "2")["opener_source"] == "manual"
    # reset_manual 不点名也不动；点名了才放弃手改
    r3 = _run(compose_queue(st, _AI(["Neo，第三版开场。"]), account_id="accA",
                            ctx={}, since_ts=now - 1, force=True, reset_manual=True))
    assert st.get_member("-100", "2")["opener_source"] == "manual"
    r4 = _run(compose_queue(st, _AI(["Bee，你那条我也遇到过，私聊说说？"]), account_id="accA", ctx={},
                            since_ts=now - 1, force=True, reset_manual=True,
                            pairs=[("-100", "2")]))
    assert r4["composed"] == 1 and st.get_member("-100", "2")["opener_source"] == "ai"


def test_approve_flow_then_release_uses_stored_opener():
    st = GroupMembersStore(":memory:")
    now = 3_000_000.0
    st.record_members([_row(user_id="1"), _row(user_id="2", username="b", first_name="Bee")])
    enqueue_today(st, "accA", now=now, since_ts=now - 1, policy=POLICY, age_days=OLD)
    # 没文案批不了
    assert st.approve_queued("accA", now) == 0
    st.set_opener("-100", "1", "Neo，群里见过，打个招呼。", "ai")
    assert st.approve_queued("accA", now) == 1
    assert st.get_member("-100", "1")["outreach_state"] == OUTREACH_APPROVED
    assert st.count_outreach_queued("accA") == 2          # approved 仍占今日额度
    nxt = st.next_approved("accA")
    assert nxt["user_id"] == "1"
    pv = build_preview(st, "accA", now=now, since_ts=now - 1, policy=POLICY, age_days=OLD,
                       hour=OPEN_HOUR)
    assert pv["queue"][0]["user_id"] == "1" and pv["queue"][0]["opener_source"] == "ai"
    assert pv["mode"] == "manual" and pv["summary"]["approved"] == 1 and pv["summary"]["queued"] == 1
    assert "access_hash" not in pv["queue"][0]
    # text 留空 → 用行上的文案
    ready = _prep(st, "1", now, since_ts=now - 1, text="")
    assert ready["ok"] and ready["text"] == "Neo，群里见过，打个招呼。"
    finalize_release(st, account_id="accA", group_id="-100", user_id="1", now=now,
                     result={"ok": True, "kind": "sent"})
    assert st.next_approved("accA") is None
    assert st.skip_outreach("-100", "2") is True
    s = st.outreach_summary("accA", now - 1)
    assert s["sent"] == 1 and s["skipped"] == 1 and s["errors"].get("operator_skip") == 1


def test_stop_contact_blocks_person_everywhere():
    st = GroupMembersStore(":memory:")
    now = 4_000_000.0
    st.record_members([_row(user_id="1"), _row(user_id="1", group_id="-200", group_title="另一群",
                                               hash_account_id="accB", source_account_id="accB")])
    enqueue_today(st, "accA", now=now, since_ts=now - 1, policy=POLICY, age_days=OLD)
    assert _prep(st, "1", now, since_ts=now - 1)["ok"]
    finalize_release(st, account_id="accA", group_id="-100", user_id="1", now=now,
                     result={"ok": True, "kind": "sent"})
    n = note_inbound_reply({"platform": "telegram", "account_id": "accA", "chat_key": "1",
                            "text": "别再给我发消息了"}, store=st)
    assert n == 1
    a = st.get_member("-100", "1")
    b = st.get_member("-200", "1")
    assert a["outreach_state"] == OUTREACH_REPLIED and a["outreach_error"] == "stop_contact"
    assert b["outreach_state"] == OUTREACH_BLOCKED and b["outreach_error"] == "stop_contact"
    assert st.count_outreach_sent_since("accA", now - 1) == 1       # 额度统计不回退
    # 普通回复不触发
    st.record_members([_row(user_id="3", username="c", first_name="Cee")])
    note_inbound_reply({"platform": "telegram", "account_id": "accA", "chat_key": "3",
                        "text": "你好呀"}, store=st)
    assert st.get_member("-100", "3")["outreach_state"] == OUTREACH_NONE


def _runner(st, accounts, sent_log, *, gate=None, pyro_ok=True, rng_seed=1, cfg=None):
    import random as _r

    async def _deliver(pyro, loop, *, user_id, access_hash, text):
        sent_log.append((user_id, access_hash, text))
        return {"ok": True, "kind": "sent"}

    return OutreachRunner(
        st, cfg_fn=lambda: cfg or {"companion": {"group_members": {"outreach_hours": [0, 24]}}},
        accounts_fn=lambda: accounts,
        pyro_fn=lambda a: (object(), None) if pyro_ok else (None, None),
        ai_fn=lambda: _AI(["Neo，群里看到你了。", "Bee，群里看到你了。", "Cee，群里看到你了。"]),
        registry_fn=lambda: _NoRegistry(),
        gate_fn=gate or (lambda a: (False, "")),
        age_fn=lambda a, n: OLD,
        deliver_fn=_deliver, rng=_r.Random(rng_seed),
    )


def test_runner_sends_one_approved_per_tick_with_jitter_and_only_owned_accounts():
    st = GroupMembersStore(":memory:")
    now = 5_000_000.0
    st.record_members([
        _row(user_id="1"), _row(user_id="2", username="b", first_name="Bee"),
        _row(user_id="9", group_id="-900", hash_account_id="accB", source_account_id="accB"),
    ])
    enqueue_today(st, "accA", now=now, since_ts=now - 1, policy=POLICY, age_days=OLD)
    enqueue_today(st, "accB", now=now, since_ts=now - 1, policy=POLICY, age_days=OLD)
    for uid in ("1", "2"):
        st.set_opener("-100", uid, "u%s 的文案" % uid, "ai")
    st.set_opener("-900", "9", "u9 的文案", "ai")
    st.approve_queued("accA", now)
    st.approve_queued("accB", now)
    sent = []
    # manual 模式：调度器不碰
    r = _runner(st, ["accA", "accB"], sent)
    rep = _run(r.tick(now, hour=OPEN_HOUR))
    assert rep["accounts"]["accA"]["kind"] == "manual" and sent == []

    st.set_outreach_mode("accA", "approve")
    # accB 没在本实例 owns 列表 → 不碰
    r = _runner(st, ["accA"], sent)
    rep = _run(r.tick(now, hour=OPEN_HOUR))
    assert rep["accounts"]["accA"]["kind"] == "sent" and len(sent) == 1
    assert sent[0][0] == 1 and sent[0][2] == "u1 的文案"
    assert st.get_member("-100", "1")["outreach_state"] == OUTREACH_SENT
    assert st.get_member("-900", "9")["outreach_state"] == OUTREACH_APPROVED
    # 同一 tick 不发第二条；下一 tick 在抖动窗内也不发
    rep2 = _run(r.tick(now + 150, hour=OPEN_HOUR))
    assert rep2["accounts"]["accA"]["kind"] == "jitter" and len(sent) == 1
    # 抖动过了（≥90min 一定过）→ 发第二条
    rep3 = _run(r.tick(now + 91 * 60, hour=OPEN_HOUR))
    assert rep3["accounts"]["accA"]["kind"] == "sent" and len(sent) == 2
    # 队列空 → idle
    rep4 = _run(r.tick(now + 200 * 60, hour=OPEN_HOUR))
    assert rep4["accounts"]["accA"]["kind"] == "idle"


def test_runner_respects_hours_gate_hold_and_missing_client():
    st = GroupMembersStore(":memory:")
    now = 6_000_000.0
    st.record_members([_row(user_id="1")])
    enqueue_today(st, "accA", now=now, since_ts=now - 1, policy=POLICY, age_days=OLD)
    st.set_opener("-100", "1", "文案", "ai")
    st.approve_queued("accA", now)
    st.set_outreach_mode("accA", "approve")
    sent = []
    cfg = {"companion": {"group_members": {"outreach_hours": [10, 21]}}}
    r = _runner(st, ["accA"], sent, cfg=cfg)
    assert _run(r.tick(now, hour=3))["accounts"]["accA"]["kind"] == "hours"
    r = _runner(st, ["accA"], sent, gate=lambda a: (True, "red"))
    assert _run(r.tick(now, hour=OPEN_HOUR))["accounts"]["accA"]["kind"] == "gate"
    st.set_hold("accA", paused=True, reason="operator_stop")
    r = _runner(st, ["accA"], sent)
    assert _run(r.tick(now, hour=OPEN_HOUR))["accounts"]["accA"]["kind"] == "paused"
    st.clear_outreach_pause("accA")
    r = _runner(st, ["accA"], sent, pyro_ok=False)
    rep = _run(r.tick(now, hour=OPEN_HOUR))
    assert rep["accounts"]["accA"]["kind"] == "no_client" and sent == []
    # 客户端不在 → 人退回原来的 approved，批准不丢
    assert st.get_member("-100", "1")["outreach_state"] == OUTREACH_APPROVED


def test_runner_auto_mode_fills_queue_composes_and_sends():
    st = GroupMembersStore(":memory:")
    now = 7_000_000.0
    st.record_members([_row(user_id="1", lang_code="zh"),
                       _row(user_id="2", username="b", first_name="Bee", lang_code="zh")])
    st.set_outreach_mode("accA", "auto")
    sent = []
    r = _runner(st, ["accA"], sent)
    rep = _run(r.tick(now, hour=OPEN_HOUR))
    assert rep["accounts"]["accA"]["kind"] == "sent" and len(sent) == 1
    states = sorted(st.get_member("-100", u)["outreach_state"] for u in ("1", "2"))
    assert states == [OUTREACH_APPROVED, OUTREACH_SENT]
    assert st.get_member("-100", sent[0][0] and str(sent[0][0]))["opener_source"] == "ai"


# ── 三期：全自动门槛 / 72h 跟进 / 封存 / 回复率切片 ─────────────────────────

from src.companion.group_member_opener import compact_persona_block, compose_followup
from src.companion.group_member_outreach import (
    auto_mode_block_reason,
    finalize_followup,
    prepare_followup,
    public_member,
)
from src.companion.group_members_store import OUTREACH_CLOSED

H = 3600.0


def _sent_row(st, uid, now, *, acct="accA", gid="-100", text="开场"):
    """直接把一个人推到 sent（省掉排队/占坑那几步）。"""
    assert st.cas_outreach(gid, uid, expect_states=(OUTREACH_NONE,), new_state=OUTREACH_QUEUED,
                           account_id=acct, now=now)
    st.set_opener(gid, uid, text, "ai", persona_id="lao_zhou")
    assert st.cas_outreach(gid, uid, expect_states=(OUTREACH_QUEUED,), new_state=OUTREACH_SENT,
                           account_id=acct, now=now)


def test_auto_mode_gate_age_flood_and_reply_rate():
    st = GroupMembersStore(":memory:")
    now = 8_000_000.0
    p = OutreachPolicy(cap=10, min_gap_sec=60)
    assert auto_mode_block_reason(st, "accA", now=now, age_days=None, policy=p)["reason"] == "age"
    assert auto_mode_block_reason(st, "accA", now=now, age_days=13, policy=p)["reason"] == "age"
    assert auto_mode_block_reason(st, "accA", now=now, age_days=14, policy=p)["ok"] is True
    # 近 7 天撞过风控 → 不许；8 天前的不算
    st.set_hold("accA", last_flood_at=now - 2 * 86400)
    assert auto_mode_block_reason(st, "accA", now=now, age_days=OLD, policy=p)["reason"] == "flood"
    st.set_hold("accA", last_flood_at=now - 8 * 86400)
    assert auto_mode_block_reason(st, "accA", now=now, age_days=OLD, policy=p)["ok"]
    # 样本够了（10 条）且零回复 → 不许；样本不够不看回复率
    st.record_members([_row(user_id=str(i), username="u%d" % i) for i in range(1, 13)])
    t = now - 3 * 86400
    for i in range(1, 10):
        _sent_row(st, str(i), t + i * 120)
    assert auto_mode_block_reason(st, "accA", now=now, age_days=OLD, policy=p)["ok"]
    _sent_row(st, "10", t + 10 * 120)
    g = auto_mode_block_reason(st, "accA", now=now, age_days=OLD, policy=p)
    assert g["reason"] == "reply_rate" and g["detail"]["sent"] == 10
    # 一个人回了 → 10% ≥ 5% 地板 → 放行
    st.mark_outreach_replied("accA", "3")
    assert auto_mode_block_reason(st, "accA", now=now, age_days=OLD, policy=p)["ok"]
    # 风控落 last_flood_at 走 finalize_release 的 flood 分支
    _sent_row(st, "11", t + 11 * 120)
    st.cas_outreach("-100", "12", expect_states=(OUTREACH_NONE,), new_state=OUTREACH_QUEUED,
                    account_id="accA", now=now)
    st.set_opener("-100", "12", "x", "ai")
    assert prepare_release(st, account_id="accA", group_id="-100", user_id="12", text="", now=now,
                           since_ts=now - 10, policy=p, age_days=OLD, hour=OPEN_HOUR)["ok"]
    finalize_release(st, account_id="accA", group_id="-100", user_id="12", now=now,
                     result={"ok": False, "kind": "flood"})
    assert st.get_hold("accA")["last_flood_at"] == now


def test_followup_once_after_72h_then_close():
    st = GroupMembersStore(":memory:")
    t0 = 9_000_000.0
    st.record_members([_row(user_id="1"), _row(user_id="2", username="b", first_name="Bee")])
    _sent_row(st, "1", t0)
    p = OutreachPolicy(cap=8, min_gap_sec=60)
    args = dict(account_id="accA", group_id="-100", user_id="1", text="前两天打过招呼，再冒个泡。",
                policy=p, hour=OPEN_HOUR)
    # 太早
    early = prepare_followup(st, now=t0 + 10 * H, since_ts=t0, **args)
    assert early["kind"] == "followup_early"
    now = t0 + 73 * H
    assert st.list_followup_due("accA", now - 72 * H)[0]["user_id"] == "1"
    ok = prepare_followup(st, now=now, since_ts=now - 10, **args)
    assert ok["ok"] and ok["access_hash"] == 555
    # 占了坑：第二条链拿不到
    assert prepare_followup(st, now=now, since_ts=now - 10, **args)["kind"] == "followup_state"
    done = finalize_followup(st, account_id="accA", group_id="-100", user_id="1", now=now,
                             text=args["text"], result={"ok": True, "kind": "sent"})
    assert done["ok"]
    row = st.get_member("-100", "1")
    assert row["outreach_state"] == OUTREACH_SENT and row["followup_at"] == now
    assert row["followup_text"] == args["text"]
    assert st.count_followups_since("accA", now - 10) == 1
    # 只跟进一次：再也不在待跟进列表里
    assert st.list_followup_due("accA", now + 100 * H) == []
    # 跟进后 72h 内不封；过了封存，原因 no_reply；封存的人若回话仍翻 replied
    assert st.close_unanswered(now + 71 * H - 72 * H) == 0
    assert st.close_unanswered((now + 73 * H) - 72 * H) == 1
    row = st.get_member("-100", "1")
    assert row["outreach_state"] == OUTREACH_CLOSED and row["outreach_error"] == "no_reply"
    assert "1" in st.touched_user_ids()
    assert st.mark_outreach_replied("accA", "1") == 1
    assert st.get_member("-100", "1")["outreach_error"] == ""
    # 跟进失败还坑；对方不可达 → blocked
    _sent_row(st, "2", t0 + 100)
    later = t0 + 80 * H
    a2 = dict(args, user_id="2")
    assert prepare_followup(st, now=later, since_ts=later - 10, **a2)["ok"]
    finalize_followup(st, account_id="accA", group_id="-100", user_id="2", now=later,
                      text=args["text"], result={"ok": False, "kind": "retryable"})
    assert st.get_member("-100", "2")["followup_at"] == 0
    assert prepare_followup(st, now=later + 100, since_ts=later - 10, **a2)["ok"]
    finalize_followup(st, account_id="accA", group_id="-100", user_id="2", now=later + 100,
                      text=args["text"], result={"ok": False, "kind": "privacy"})
    assert st.get_member("-100", "2")["outreach_state"] == OUTREACH_BLOCKED
    # 拒绝过的人永不跟进
    st.record_members([_row(user_id="3", username="c", first_name="Cee")])
    _sent_row(st, "3", t0)
    st.mark_outreach_stop_contact("3")
    assert st.list_followup_due("accA", now + 500 * H) == []


def test_followup_respects_daily_cap_and_shared_gap():
    st = GroupMembersStore(":memory:")
    t0 = 10_000_000.0
    st.record_members([_row(user_id=str(i), username="u%d" % i) for i in range(1, 4)])
    for i in range(1, 4):
        _sent_row(st, str(i), t0 + i * 2000)
    now = t0 + 100 * H
    p = OutreachPolicy(cap=8, min_gap_sec=1500, followup_daily_cap=2)
    a = dict(account_id="accA", group_id="-100", text="再冒个泡。", policy=p, hour=OPEN_HOUR)
    assert prepare_followup(st, now=now, since_ts=now - 10, user_id="1", **a)["ok"]
    finalize_followup(st, account_id="accA", group_id="-100", user_id="1", now=now, text="再冒个泡。",
                      result={"ok": True, "kind": "sent"})
    # 刚发完一条跟进 → 和开口共用的最小间隔拦
    assert prepare_followup(st, now=now + 60, since_ts=now - 10, user_id="2", **a)["kind"] == "gap"
    # 间隔过了 → 第二条放行；第三条被跟进日额（2）拦
    assert prepare_followup(st, now=now + 2000, since_ts=now - 10, user_id="2", **a)["ok"]
    finalize_followup(st, account_id="accA", group_id="-100", user_id="2", now=now + 2000,
                      text="再冒个泡。", result={"ok": True, "kind": "sent"})
    assert prepare_followup(st, now=now + 4000, since_ts=now - 10, user_id="3", **a)["kind"] == "followup_cap"
    # 开关关了 → followup_off
    off = OutreachPolicy(cap=8, followup_enabled=False)
    assert prepare_followup(st, now=now + 4000, since_ts=now - 10, user_id="3",
                            **dict(a, policy=off))["kind"] == "followup_off"
    # 跟进不占「每天新人」额度：发了 2 条跟进后，开口的今日额度仍是 0/8
    assert st.count_outreach_sent_since("accA", now - 10) == 0


def test_compose_followup_ai_then_template_and_never_repeats_opener():
    m = _row(user_id="7", first_name="Neo", lang_code="zh", opener_text="Neo，群里看到你了，聊两句？")
    ctx = {"persona_block": "你是老周。", "persona": None}
    ai = _AI(["Neo，前两天那条可能沉了，不方便聊也没事。"])
    got = _run(compose_followup(ai, member=m, ctx=ctx))
    assert got["source"] == "ai" and "老周" in ai.prompts[0] and "群里看到你了" in ai.prompts[0]
    # 和上次开口一模一样 → 拒 → 模板
    got2 = _run(compose_followup(_AI(["Neo，群里看到你了，聊两句？"]), member=m, ctx=ctx))
    assert got2["source"] == "template" and got2["reason"] == "similar"
    assert "Neo" in got2["text"] and not language_mismatch(got2["text"], "zh")
    en = _row(user_id="8", first_name="Bob", lang_code="en", opener_text="hi")
    got3 = _run(compose_followup(None, member=en, ctx=ctx))
    assert got3["source"] == "template" and "Bob" in got3["text"]


def test_compact_persona_block_is_short_and_identity_first():
    p = {"id": "lz", "name": "老周", "role": "跨境物流小老板",
         "personality": {"style": "直爽短句", "traits": ["热心"]}, "background": "深圳十年。"}
    b = compact_persona_block(p)
    assert b.startswith("你是老周，跨境物流小老板。") and "直爽短句" in b and len(b) <= 700
    assert compact_persona_block(None) == "" and compact_persona_block({}) == ""


def test_runner_demotes_auto_persists_jitter_and_follows_up_when_idle():
    import random as _r
    st = GroupMembersStore(":memory:")
    t0 = 11_000_000.0
    st.record_members([_row(user_id="1"), _row(user_id="2", username="b", first_name="Bee")])
    _sent_row(st, "1", t0)                       # 三天前发过、没回
    st.set_outreach_mode("accA", "auto")
    st.set_hold("accA", last_flood_at=t0 + 70 * H)   # 刚撞过风控 → 不够格全自动
    sent = []
    now = t0 + 73 * H
    r = _runner(st, ["accA"], sent, cfg={"companion": {"group_members": {
        "outreach_hours": [0, 24], "outreach_min_gap_sec": 60}}})
    rep = _run(r.tick(now, hour=OPEN_HOUR))["accounts"]["accA"]
    # 降级到 approve；approve 模式下队列空（2 号没批）→ 轮到跟进 1 号
    assert rep.get("demoted") == "flood" and st.get_outreach_mode("accA") == "approve"
    assert rep["kind"] == "followup_sent" and len(sent) == 1 and sent[0][0] == 1
    row = st.get_member("-100", "1")
    assert row["followup_at"] == now and row["followup_text"] == sent[0][2]
    assert row["followup_text"] != row["opener_text"]
    # 抖动落库：新 runner 实例（模拟重启）同一时刻仍在等待
    assert st.get_hold("accA")["next_auto_at"] > now
    r2 = _runner(st, ["accA"], sent, cfg={"companion": {"group_members": {"outreach_hours": [0, 24]}}})
    assert _run(r2.tick(now + 60, hour=OPEN_HOUR))["accounts"]["accA"]["kind"] == "jitter"
    # 再过 72h 没回 → 封存；auto 不再被改（已是 approve）
    rep3 = _run(r2.tick(now + 73 * H, hour=OPEN_HOUR))["accounts"]["accA"]
    assert st.get_member("-100", "1")["outreach_state"] == OUTREACH_CLOSED
    assert rep3["kind"] == "idle"


def test_outreach_stats_slices_and_funnel():
    st = GroupMembersStore(":memory:")
    t0 = 12_000_000.0
    st.record_members([_row(user_id=str(i), username="u%d" % i) for i in range(1, 6)])
    for i in range(1, 4):
        _sent_row(st, str(i), t0 + i * 2000)
    st.set_opener("-100", "4", "tpl", "template")   # 没发出去，不进切片
    st.cas_outreach("-100", "4", expect_states=(OUTREACH_NONE,), new_state=OUTREACH_QUEUED,
                    account_id="accA", now=t0)
    st.mark_outreach_replied("accA", "2")
    s = st.outreach_stats(t0 - 1)
    src = {r["key"]: r for r in s["by_source"]}
    assert src["ai"]["sent"] == 3 and src["ai"]["replied"] == 1 and abs(src["ai"]["rate"] - 1 / 3) < 1e-9
    per = {r["key"]: r for r in s["by_persona"]}
    assert per["lao_zhou"]["sent"] == 3
    assert sum(r["sent"] for r in s["by_hour"]) == 3
    assert s["by_account"][0]["key"] == "accA"
    f = s["funnel"]
    assert f["members"] == 5 and f["sent"] == 3 and f["replied"] == 1 and f["queued"] == 1
    # 按号过滤 + 时间窗外为空
    assert st.outreach_stats(t0 - 1, "accB")["by_source"] == []
    assert st.outreach_stats(t0 + 10 * H)["by_source"] == []
    rr = st.reply_rate("accA", t0 - 1)
    assert rr == {"sent": 3, "replied": 1, "rate": 1 / 3}


# ── 第三轮：收件闭环 / 切入方式 A/B ────────────────────────────────────────────

def test_reply_records_first_text_latency_and_inbound_first_leaves_queue():
    st = GroupMembersStore(":memory:")
    t0 = 13_000_000.0
    st.record_members([_row(user_id="1", first_name="Ann"), _row(user_id="2", first_name="Bob"),
                       _row(user_id="3", first_name="Cat")])
    _sent_row(st, "1", t0)
    # 2 还在排队，3 没碰过
    st.cas_outreach("-100", "2", expect_states=(OUTREACH_NONE,), new_state=OUTREACH_QUEUED,
                    account_id="accA", now=t0)
    # 1 回了：首回文案 + 时间只记第一条；第二条不覆盖
    assert note_inbound_reply({"platform": "telegram", "account_id": "accA", "chat_key": "1",
                               "direction": "in", "text": "  哈哈 是我\n在的  ", "ts": t0 + 2 * H}, st) == 1
    m1 = st.get_member("-100", "1")
    assert m1["outreach_state"] == OUTREACH_REPLIED and m1["reply_text"] == "哈哈 是我 在的"
    assert m1["replied_at"] == t0 + 2 * H
    note_inbound_reply({"platform": "telegram", "account_id": "accA", "chat_key": "1",
                        "direction": "in", "text": "第二句", "ts": t0 + 3 * H}, st)
    assert st.get_member("-100", "1")["reply_text"] == "哈哈 是我 在的"
    # 2 人家先来找我们：出队标 replied/inbound_first，outreach_at 仍 0 → 不进回复率分母
    assert note_inbound_reply({"platform": "telegram", "account_id": "accA", "chat_key": "2",
                               "direction": "in", "text": "hey", "ts": t0 + H}, st) == 1
    m2 = st.get_member("-100", "2")
    assert m2["outreach_state"] == OUTREACH_REPLIED and m2["outreach_error"] == "inbound_first"
    assert m2["outreach_at"] == 0 and m2["outreach_account_id"] == "accA"
    assert st.count_outreach_queued("accA") == 0
    # 3 没凭证关系也没发过 → 不动
    assert note_inbound_reply({"platform": "telegram", "account_id": "accB", "chat_key": "3",
                               "direction": "in", "text": "yo"}, st) == 0
    rr = st.reply_rate("accA", t0 - 1)
    assert rr == {"sent": 1, "replied": 1, "rate": 1.0}
    s = st.outreach_stats(t0 - 1)
    assert s["funnel"]["sent"] == 1 and s["funnel"]["replied"] == 1 and s["funnel"]["inbound_first"] == 1
    assert s["reply_latency"] == {"n": 1, "median_sec": int(2 * H), "p75_sec": int(2 * H)}
    # 今日回音列表 + 公开行带会话 id、不带 access_hash
    rep = st.list_replied_since("accA", t0 - 1)
    assert {r["user_id"] for r in rep} == {"1", "2"}
    pub = public_member(m1)
    assert pub["conversation_id"] == "telegram:accA:1" and "access_hash" not in pub
    assert pub["reply_text"] == "哈哈 是我 在的"


def test_second_hop_answered_and_stalled_list():
    from src.companion.group_member_outreach import note_outbound_answer, stalled_before
    from src.integrations import protocol_bridge as pb

    st = GroupMembersStore(":memory:")
    t0 = 16_000_000.0
    st.record_members([_row(user_id="1", first_name="Ann"), _row(user_id="2", first_name="Bob"),
                       _row(user_id="3", first_name="Cat")])
    for u in ("1", "2", "3"):
        _sent_row(st, u, t0)
    # 开场自己的镜像（sent 态）不算接上
    assert note_outbound_answer({"platform": "telegram", "account_id": "accA", "chat_key": "1",
                                 "direction": "out", "ts": t0 + 1}, st) == 0
    # 1、2 回了；3 没回
    for u in ("1", "2"):
        note_inbound_reply({"platform": "telegram", "account_id": "accA", "chat_key": u,
                            "direction": "in", "text": "在", "ts": t0 + H}, st)
    # 1：AI/坐席 20 分钟后回了 → 接上；再回一次幂等
    assert note_outbound_answer({"platform": "telegram", "account_id": "accA", "chat_key": "1",
                                 "direction": "out", "ts": t0 + H + 1200}, st) == 1
    assert st.get_member("-100", "1")["answered_at"] == t0 + H + 1200
    assert note_outbound_answer({"platform": "telegram", "account_id": "accA", "chat_key": "1",
                                 "direction": "out", "ts": t0 + 2 * H}, st) == 0
    # 入站不算、别的号不算、群不算
    assert note_outbound_answer({"platform": "telegram", "account_id": "accA", "chat_key": "2",
                                 "direction": "in", "ts": t0 + 2 * H}, st) == 0
    assert note_outbound_answer({"platform": "telegram", "account_id": "accB", "chat_key": "2",
                                 "direction": "out", "ts": t0 + 2 * H}, st) == 0
    assert note_outbound_answer({"platform": "telegram", "account_id": "accA", "chat_key": "-100",
                                 "direction": "out", "ts": t0 + 2 * H}, st) == 0
    assert st.get_member("-100", "2")["answered_at"] == 0
    # 没接上清单：2 在 48h 后出现；1（接上了）、3（没回）不在
    p = OutreachPolicy(cap=10, min_gap_sec=60)
    assert st.list_stalled_replies(stalled_before(p, t0 + 10 * H), "accA") == []
    stalled = st.list_stalled_replies(stalled_before(p, t0 + 50 * H), "accA")
    assert [r["user_id"] for r in stalled] == ["2"]
    # 对方拒绝联系的不提醒
    st.mark_outreach_stop_contact("2")
    assert st.list_stalled_replies(stalled_before(p, t0 + 50 * H), "accA") == []
    # 漏斗：接上 1
    f = st.outreach_stats(t0 - 1)["funnel"]
    assert f["replied"] == 2 and f["answered"] == 1
    assert len(st.outreach_stats(t0 - 1)["recent_replies"]) == 2
    # 真经 emit_incoming 一条出站镜像也会记接上
    from src.companion import group_members_store as gms
    gms.reset_group_members_store()
    st2 = gms.configure_group_members_store(":memory:")
    try:
        st2.record_members([_row(user_id="8")])
        _sent_row(st2, "8", t0)
        note_inbound_reply({"platform": "telegram", "account_id": "accA", "chat_key": "8",
                            "direction": "in", "text": "hi", "ts": t0 + H}, st2)
        old_sink = pb.get_inbox_sink()
        pb.register_inbox_sink(lambda m: None)
        try:
            pb.emit_incoming({"platform": "telegram", "account_id": "accA", "chat_key": "8",
                              "direction": "out", "text": "来了", "ts": t0 + H + 60})
        finally:
            pb.register_inbox_sink(old_sink)
        assert st2.get_member("-100", "8")["answered_at"] == t0 + H + 60
    finally:
        gms.reset_group_members_store()


def test_runner_audits_stalled_once_per_day_even_for_manual_accounts():
    from src.inbox.store import InboxStore
    from src.integrations.protocol_autoreply import HANDOFF_TAG

    st = GroupMembersStore(":memory:")
    inbox = InboxStore(":memory:")
    t0 = 17_000_000.0
    st.record_members([_row(user_id="1"), _row(user_id="2")])
    _sent_row(st, "1", t0)
    note_inbound_reply({"platform": "telegram", "account_id": "accA", "chat_key": "1",
                        "direction": "in", "text": "在", "ts": t0 + H}, st)
    audits = []
    r = _runner(st, ["accA"], [])
    r.inbox_fn = lambda: inbox
    r.audit_fn = lambda a, acct, d: audits.append((a, acct, d))
    rep = _run(r.tick(t0 + 10 * H, hour=OPEN_HOUR))
    assert rep["accounts"]["accA"] == {"kind": "manual", "stalled": 0} and audits == []
    assert inbox.get_conv_tags("telegram:accA:1") == []
    rep = _run(r.tick(t0 + 50 * H, hour=OPEN_HOUR))
    assert rep["accounts"]["accA"]["stalled"] == 1
    assert len(audits) == 1 and audits[0][0] == "tg_members_outreach_stalled" and "users=1" in audits[0][2]
    # 收件箱会话被打「需人工」，原因可解释
    cid = "telegram:accA:1"
    assert HANDOFF_TAG in inbox.get_conv_tags(cid)
    assert inbox.get_handoff_meta(cid)["reason"] == "outreach_stalled"
    # 同一天再 tick 不重复提醒、不重复打标
    _run(r.tick(t0 + 51 * H, hour=OPEN_HOUR))
    assert len(audits) == 1 and inbox.get_conv_tags(cid).count(HANDOFF_TAG) == 1
    # 我方回了一句（经出站钩子）→ answered + 自动摘标；清零
    from src.companion.group_member_outreach import note_outbound_answer
    assert note_outbound_answer({"platform": "telegram", "account_id": "accA", "chat_key": "1",
                                 "direction": "out", "ts": t0 + 52 * H}, st, inbox=inbox) == 1
    assert HANDOFF_TAG not in inbox.get_conv_tags(cid) and inbox.get_handoff_meta(cid) == {}
    rep = _run(r.tick(t0 + 53 * H, hour=OPEN_HOUR))
    assert rep["accounts"]["accA"]["stalled"] == 0


def test_stalled_flag_respects_manual_clear_and_tracks_rebreak():
    from src.companion.group_member_outreach import note_outbound_answer
    from src.inbox.store import InboxStore
    from src.integrations.protocol_autoreply import HANDOFF_TAG, clear_needs_human

    st = GroupMembersStore(":memory:")
    inbox = InboxStore(":memory:")
    t0 = 17_000_000.0
    cid = "telegram:accA:1"
    st.record_members([_row(user_id="1")])
    _sent_row(st, "1", t0)

    def inbound(ts):
        note_inbound_reply({"platform": "telegram", "account_id": "accA", "chat_key": "1",
                            "direction": "in", "text": "在", "ts": ts}, st)

    def answer(ts):
        return note_outbound_answer({"platform": "telegram", "account_id": "accA", "chat_key": "1",
                                     "direction": "out", "ts": ts}, st, inbox=inbox)

    def stalled(ts):
        return _run(r.tick(ts, hour=OPEN_HOUR))["accounts"]["accA"].get("stalled", 0)

    r = _runner(st, ["accA"], [])
    r.inbox_fn = lambda: inbox
    inbound(t0 + H)
    assert stalled(t0 + 50 * H) == 1 and HANDOFF_TAG in inbox.get_conv_tags(cid)
    assert st.get_member("-100", "1")["stalled_flagged_at"] == t0 + 50 * H
    # 坐席手动摘标但没回 → 本轮不再打回去（隔天也不打），但仍计入没接上
    assert clear_needs_human(inbox, cid, actor="agent:alice") is True
    assert stalled(t0 + 51 * H) == 1 and stalled(t0 + 75 * H) == 1
    assert HANDOFF_TAG not in inbox.get_conv_tags(cid)
    # 对方又来催 → 新一轮等待，允许再打
    inbound(t0 + 76 * H)
    assert st.get_member("-100", "1")["stalled_flagged_at"] == 0
    assert stalled(t0 + 77 * H) == 1 and HANDOFF_TAG in inbox.get_conv_tags(cid)
    # 接上 → 摘标、清记号
    assert answer(t0 + 78 * H) == 1
    assert HANDOFF_TAG not in inbox.get_conv_tags(cid)
    m = st.get_member("-100", "1")
    assert m["last_out_at"] == t0 + 78 * H and m["stalled_flagged_at"] == 0
    # 接上后又断了：对方 80h 来话，我方没回 → 按最后来话计时，128h 后才算
    inbound(t0 + 80 * H)
    assert st.get_member("-100", "1")["last_in_at"] == t0 + 80 * H
    assert st.get_member("-100", "1")["replied_at"] == t0 + H     # 首回时间不动
    assert stalled(t0 + 100 * H) == 0 and HANDOFF_TAG not in inbox.get_conv_tags(cid)
    assert stalled(t0 + 129 * H) == 1 and HANDOFF_TAG in inbox.get_conv_tags(cid)
    # 再回一句：不是首次接上（返回 0），但标照样摘
    assert answer(t0 + 130 * H) == 0
    assert HANDOFF_TAG not in inbox.get_conv_tags(cid) and inbox.get_handoff_meta(cid) == {}
    assert stalled(t0 + 131 * H) == 0
    # 漏斗口径不受影响：answered 只算一次
    assert st.outreach_stats(t0 - 1)["funnel"]["answered"] == 1


async def test_deliver_outreach_uses_raw_send_with_stored_hash():
    from pyrogram.raw.types import (
        Message, PeerUser, UpdateMessageID, UpdateNewMessage, UpdateShortSentMessage, Updates)
    from src.companion.group_member_outreach import deliver_outreach

    class _Cli:
        def __init__(self, reply):
            self.reply, self.calls = reply, []

        def rnd_id(self):
            return 42

        async def invoke(self, fn):
            self.calls.append(fn)
            if isinstance(self.reply, Exception):
                raise self.reply
            return self.reply

        async def send_message(self, *a, **k):   # 高层接口不许再走（session 缓存查不到 / 绑参失败）
            raise AssertionError("send_message must not be used")

    cli = _Cli(UpdateShortSentMessage(out=True, id=7, pts=1, pts_count=1, date=1_700_000_000))
    r = await deliver_outreach(cli, user_id=8118214990, access_hash=-123, text="hi")
    assert r == {"ok": True, "kind": "sent", "msg_id": "7", "ts": 1_700_000_000.0}
    fn = cli.calls[0]
    assert fn.peer.user_id == 8118214990 and fn.peer.access_hash == -123
    assert fn.message == "hi" and fn.random_id == 42
    msg = Message(id=9, peer_id=PeerUser(user_id=1), date=1_700_000_100, message="hi")
    cli = _Cli(Updates(updates=[UpdateMessageID(id=9, random_id=42),
                                UpdateNewMessage(message=msg, pts=1, pts_count=1)],
                       users=[], chats=[], date=1_700_000_100, seq=0))
    r = await deliver_outreach(cli, user_id=1, access_hash=1, text="hi")
    assert r["msg_id"] == "9" and r["ts"] == 1_700_000_100.0

    class UserPrivacyRestricted(Exception):
        pass

    r = await deliver_outreach(_Cli(UserPrivacyRestricted()), user_id=1, access_hash=1, text="hi")
    assert r == {"ok": False, "kind": "privacy"}


def test_enqueue_explains_why_nothing_was_queued():
    from src.companion.group_member_outreach import enqueue_today

    st = GroupMembersStore(":memory:")
    now = 9_000_000.0
    args = dict(now=now, since_ts=now - 10, policy=POLICY, age_days=OLD)
    # 这个号一个人都没提过
    r = enqueue_today(st, "accA", **args)
    assert r["queued_now"] == 0 and r["empty"]["pool"] == 0 and r["empty"]["unhashed"] == 0
    # 提了：两个没发过言、一个管理员、一个没拿到凭证、一个已被别的号开过口
    st.record_members([_row(user_id="1", spoke=False), _row(user_id="2", spoke=False),
                       _row(user_id="3", is_admin=True),
                       _row(user_id="4", access_hash="", hash_account_id=""),
                       _row(user_id="5", hash_account_id="accB")])
    _sent_row(st, "5", now - 100, acct="accB")
    st.record_members([_row(user_id="5", group_id="-200")])
    r = enqueue_today(st, "accA", **args)
    assert r["queued_now"] == 0
    assert r["empty"] == {"pool": 4, "silent": 2, "admin_or_bot": 1, "touched": 1, "unhashed": 1}
    # 有人可排时不带 empty
    st.record_members([_row(user_id="6")])
    r = enqueue_today(st, "accA", **args)
    assert r["queued_now"] == 1 and "empty" not in r


def test_unflag_only_removes_our_own_needs_human():
    from src.companion.group_member_outreach import flag_stalled_in_inbox, unflag_stalled_in_inbox
    from src.inbox.store import InboxStore
    from src.integrations.protocol_autoreply import HANDOFF_TAG, tag_needs_human

    inbox = InboxStore(":memory:")
    cid = "telegram:accA:5"
    # 别的原因打的标（隐私类）→ 我们不摘
    tag_needs_human(inbox, {"platform": "telegram", "account_id": "accA", "chat_key": "5"},
                    reason="high_risk", source="system", now=1_000.0)
    assert unflag_stalled_in_inbox(inbox, cid) is False
    assert HANDOFF_TAG in inbox.get_conv_tags(cid)
    # 没标的会话：打 → 摘；inbox 缺席 → False
    m = {"outreach_account_id": "accA", "user_id": "6"}
    assert flag_stalled_in_inbox(None, m, now=2_000.0) is False
    assert flag_stalled_in_inbox(inbox, m, now=2_000.0) is True
    assert flag_stalled_in_inbox(inbox, m, now=2_001.0) is False      # 幂等
    assert unflag_stalled_in_inbox(inbox, "telegram:accA:6") is True
    assert inbox.get_conv_tags("telegram:accA:6") == []


def test_pick_variant_explores_then_exploits_and_respects_silent_members():
    from src.companion.group_member_opener import (
        OPENER_VARIANTS, allowed_variants, opener_prompt, pick_variant)

    # 没说过话 → 只能从群切入
    assert allowed_variants({"last_msg_text": ""}) == ("group",)
    assert allowed_variants({"last_msg_text": "hi"}) == OPENER_VARIANTS
    assert pick_variant({}, ("group",), seed="x") == "group"
    # 没样本 → 全探索：三种都会被选到，且先补最少的
    picks = {pick_variant({}, OPENER_VARIANTS, seed=i) for i in range(40)}
    assert picks == set(OPENER_VARIANTS)
    rates = {"echo": {"sent": 9, "replied": 1}, "group": {"sent": 9, "replied": 1},
             "ask": {"sent": 2, "replied": 0}}
    assert all(pick_variant(rates, OPENER_VARIANTS, seed=i) == "ask" for i in range(20))
    # 都够样本 → 大多数走最高回复率，少数探索
    rates = {"echo": {"sent": 20, "replied": 6}, "group": {"sent": 20, "replied": 1},
             "ask": {"sent": 20, "replied": 2}}
    got = [pick_variant(rates, OPENER_VARIANTS, seed=i, explore=0.25) for i in range(200)]
    assert 0.6 < got.count("echo") / len(got) < 0.95 and set(got) == set(OPENER_VARIANTS)
    assert all(v == "echo" for v in (pick_variant(rates, OPENER_VARIANTS, seed=i, explore=0.0)
                                     for i in range(20)))
    # 先验平滑：样本刚过线时 1/5 vs 2/5 不该一边倒——group 2/5 原始更高，但 echo 先验高、平滑后 echo 胜
    from src.companion.group_member_opener import ab_settings
    rates = {"echo": {"sent": 5, "replied": 1}, "group": {"sent": 5, "replied": 2},
             "ask": {"sent": 5, "replied": 0}}
    assert pick_variant(rates, OPENER_VARIANTS, seed=1, explore=0.0,
                        prior={"echo": 0.9, "group": 0.0, "ask": 0.0}) == "echo"
    assert pick_variant(rates, OPENER_VARIANTS, seed=1, explore=0.0,
                        prior={"echo": 0.0, "group": 0.0, "ask": 0.0}) == "group"
    # 旋钮从 config 读，坏值回默认，未知切入忽略
    ab = ab_settings({"companion": {"group_members": {
        "outreach_ab_explore": "x", "outreach_ab_min_sample": 3,
        "outreach_ab_prior": {"echo": 0.5, "bogus": 1, "ask": "bad"}}}})
    assert ab["explore"] == 0.25 and ab["min_sample"] == 3 and ab["prior_weight"] == 5.0
    assert ab["prior"] == {"echo": 0.5, "ask": 0.08, "group": 0.06}
    # 权重 0 = 不平滑：回到原始 2/5 > 1/5 → group
    assert pick_variant(rates, OPENER_VARIANTS, seed=1, explore=0.0,
                        prior={"echo": 0.9}, prior_weight=0) == "group"
    assert ab_settings({})["prior"] == {"echo": 0.10, "ask": 0.08, "group": 0.06}
    # 同一个人每次选到的一样（种子稳定）→ 重拟不会在切入上来回跳
    assert pick_variant(rates, OPENER_VARIANTS, seed="accA|7") == pick_variant(rates, OPENER_VARIANTS, seed="accA|7")
    # prompt 带切入提示；没发言的人即使传 echo 也不写（没话可接）
    m = {"first_name": "Neo", "group_title": "G", "last_msg_text": "显卡又涨了"}
    p = opener_prompt(persona_block="", member=m, lang="zh", goal_hint="", avoid=(), variant="ask")
    assert "切入方式" in p and "小问题" in p
    p2 = opener_prompt(persona_block="", member={"first_name": "Neo"}, lang="zh", goal_hint="",
                       avoid=(), variant="echo")
    assert "切入方式" not in p2
    assert "别编造" in p2 and "优先接TA说的那句" not in p2
    assert "别编造" not in p and "优先接TA说的那句" in p


def test_compose_queue_records_variant_only_for_ai_drafts():
    st = GroupMembersStore(":memory:")
    now = 15_000_000.0
    st.record_members([_row(user_id="1", first_name="Neo", last_msg_text="显卡又涨了"),
                       _row(user_id="2", first_name="Bee", last_msg_text="")])
    enqueue_today(st, "accA", now=now, since_ts=now - 1, policy=POLICY, age_days=OLD)
    r = _run(compose_queue(st, _AI(["Neo，显卡这波你还追吗？", "Bee，这群你来多久了？"]),
                           account_id="accA", ctx={}, since_ts=now - 1))
    assert r["composed"] == 2 and r["ai"] == 2
    v1 = st.get_member("-100", "1")["opener_variant"]
    assert v1 in ("echo", "group", "ask") and r["variants"].get(v1, 0) >= 1
    assert st.get_member("-100", "2")["opener_variant"] == "group"   # 没发言只能从群切入
    # 模板稿不记切入方式
    st.record_members([_row(user_id="3", first_name="Cee", last_msg_text="x")])
    enqueue_today(st, "accA", now=now, since_ts=now - 1, policy=POLICY, age_days=OLD)
    r = _run(compose_queue(st, None, account_id="accA", ctx={}, since_ts=now - 1))
    assert r["template"] == 1 and st.get_member("-100", "3")["opener_variant"] == ""
    # 手改清掉切入方式；切片里模板/手写归 '-'
    st.set_opener("-100", "1", "我自己写的", "manual", variant="")
    assert st.get_member("-100", "1")["opener_variant"] == ""
    assert st.variant_rates(now - 1, "accA") == {}   # 还没发出，不进切片


def test_sent_opener_and_followup_are_mirrored_into_inbox(monkeypatch):
    from src.companion import group_member_outreach as gmo
    from src.integrations import protocol_bridge as pb

    st = GroupMembersStore(":memory:")
    t0 = 14_000_000.0
    st.record_members([_row(user_id="5", username="eve", first_name="Eve", last_name="L")])
    st.cas_outreach("-100", "5", expect_states=(OUTREACH_NONE,), new_state=OUTREACH_QUEUED,
                    account_id="accA", now=t0)
    st.set_opener("-100", "5", "Eve，群里看到你了。", "ai")
    seen = []
    old_sink = pb.get_inbox_sink()
    pb.register_inbox_sink(seen.append)
    try:
        prep = prepare_release(st, account_id="accA", group_id="-100", user_id="5", text="",
                               now=t0, since_ts=t0 - 1, policy=POLICY, age_days=OLD, hour=OPEN_HOUR)
        assert prep["ok"], prep
        finalize_release(st, account_id="accA", group_id="-100", user_id="5", now=t0,
                         text=prep["text"], result={"ok": True, "kind": "sent", "msg_id": "901",
                                                    "ts": t0 + 1})
        assert len(seen) == 1
        m = seen[0]
        assert m["direction"] == "out" and m["platform"] == "telegram"
        assert m["account_id"] == "accA" and m["chat_key"] == "5" and m["msg_id"] == "901"
        assert m["text"] == "Eve，群里看到你了。" and m["name"] == "Eve L" and m["ts"] == t0 + 1
        assert m["source"]["chat_type"] == "private" and "access_hash" not in m
        # 跟进也镜像；失败不镜像
        pf = OutreachPolicy(cap=10, min_gap_sec=60)
        fu = prepare_followup(st, account_id="accA", group_id="-100", user_id="5", text="还在吗，不急。",
                              now=t0 + 80 * H, since_ts=t0 + 79 * H, policy=pf, hour=OPEN_HOUR)
        assert fu["ok"], fu
        finalize_followup(st, account_id="accA", group_id="-100", user_id="5", now=t0 + 80 * H,
                          text="还在吗，不急。", result={"ok": False, "kind": "retryable"})
        assert len(seen) == 1
        fu = prepare_followup(st, account_id="accA", group_id="-100", user_id="5", text="还在吗，不急。",
                              now=t0 + 80 * H, since_ts=t0 + 79 * H, policy=pf, hour=OPEN_HOUR)
        assert fu["ok"], fu
        finalize_followup(st, account_id="accA", group_id="-100", user_id="5", now=t0 + 80 * H,
                          text="还在吗，不急。", result={"ok": True, "kind": "sent"})
        assert len(seen) == 2 and seen[1]["text"] == "还在吗，不急。" and seen[1]["ts"] == t0 + 80 * H
    finally:
        pb.register_inbox_sink(old_sink)
    # sink 抛异常不影响发送结果
    def _boom(_m):
        raise RuntimeError("x")
    monkeypatch.setattr(pb, "emit_incoming", _boom)
    assert gmo.mirror_outbound(st, account_id="accA", group_id="-100", user_id="5", text="a",
                               result={}, now=t0) is False


def test_outreach_context_note_tells_how_we_met():
    from src.companion.group_member_outreach import outreach_context_note
    st = GroupMembersStore(":memory:")
    now = 3_100_000.0
    st.record_members([_row(user_id="21", group_title="红包测试群", last_msg_text="谁有好用的翻译工具"),
                       _row(user_id="22", group_title="红包测试群", last_msg_text=""),
                       _row(user_id="23")])
    for uid in ("21", "22"):
        st.cas_outreach("-100", uid, expect_states=(OUTREACH_NONE,), new_state=OUTREACH_SENT,
                        account_id="accA", now=now)
    # 没开过口 / 别的号开的口 → 无背景
    assert outreach_context_note("accA", "23", st) == ""
    assert outreach_context_note("accB", "21", st) == ""
    # 已发（回复链可能先于入站钩子跑）也认
    n = outreach_context_note("accA", "21", st)
    assert "红包测试群" in n and "谁有好用的翻译工具" in n and "你先私聊" in n
    assert len(n) <= 80
    st.mark_outreach_replied("accA", "22", now=now + 60, text="你好")
    n2 = outreach_context_note("accA", "22", st)
    assert "红包测试群" in n2 and "说过" not in n2
    # 排队中对方先来找 → 换说法
    st.cas_outreach("-100", "23", expect_states=(OUTREACH_NONE,), new_state=OUTREACH_QUEUED,
                    account_id="accA", now=now)
    st.mark_outreach_replied("accA", "23", now=now + 5, text="在吗")
    assert "TA先私聊来找你" in outreach_context_note("accA", "23", st)


def test_default_goal_note_carries_outreach_context(monkeypatch):
    from src.companion import group_members_store as gms
    from src.companion.goals import defaults as gdef
    from src.companion.goals.store import get_goal_store

    class _KV:
        def __init__(self):
            self.kv = {}

        def get_app_setting(self, key, default=""):
            return self.kv.get(key, default)

        def set_app_setting(self, key, value, updated_by=""):
            self.kv[key] = value
            return True

        def get_conv_tags(self, conv):
            return []

    gms.reset_group_members_store()
    st = gms.configure_group_members_store(":memory:")
    try:
        now = 3_200_000.0
        st.record_members([_row(user_id="31", group_title="红包测试群", last_msg_text="求推荐客服工具")])
        st.cas_outreach("-100", "31", expect_states=(OUTREACH_NONE,), new_state=OUTREACH_SENT,
                        account_id="accA", now=now)
        inbox = _KV()
        gdef.set_default(inbox, gdef.account_key("telegram", "accA"), gdef.normalize_spec(
            {"template": "acquire_and_convert", "params": {"product_id": "chatx", "note": "主推智聊"}}))
        monkeypatch.setattr(gdef, "_conv_persona_id", lambda uc, ck: "")
        cfg = {"companion": {"goals": {"enabled": True, "db_path": ":memory:"}}}
        gs = get_goal_store(":memory:")
        g = gdef.maybe_attach_default_goal(gs, cfg, platform="telegram", chat_key="31", account_id="accA",
                                           conversation_id="telegram:accA:31", inbox_store=inbox, now=now)
        assert g is not None
        note = g["params"]["note"]
        assert note.startswith("同群开口：") and "红包测试群" in note and note.endswith("；主推智聊")
        assert g["params"]["product_id"] == "chatx"
        # 非开口会话 → note 原样
        g2 = gdef.maybe_attach_default_goal(gs, cfg, platform="telegram", chat_key="99", account_id="accA",
                                            conversation_id="telegram:accA:99", inbox_store=inbox, now=now)
        assert g2["params"]["note"] == "主推智聊"
    finally:
        gms.reset_group_members_store()
