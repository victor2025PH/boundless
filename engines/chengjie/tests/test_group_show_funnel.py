"""向导漏斗口径：成功 = 开演后群成员自己先加向导，并且开始测试。

这条和 outcome 的「群里说过话再私聊」分开。潜水成员加向导要算进来；
帮手号的私聊、我们先发出去的冷私聊、没有测试信号的添加，都不能算测完。
"""
from __future__ import annotations

from src.companion.group_show.funnel import (
    attribute_inbound,
    configured_guide_account,
    configured_guide_feature,
    fallback_peers,
    features_by_playbook,
    playbook_declares_guide,
    single_feature,
    speakers_answered_by_guide,
    trial_marker,
)
from src.companion.group_show.playbook import Playbook, Role

AFTER = 1_700_000_000.0


def _members(*ids, group="g1"):
    return [{"user_id": i, "group_key": group} for i in ids]


def test_lurker_who_messages_the_guide_after_the_show_counts_as_added():
    """没在群里开口、但在成员名单里，开演后自己先找向导，算添加。"""
    out = attribute_inbound(
        _members("u1", "u2"),
        [{"user_id": "u1", "account_id": "guide", "ts": AFTER + 10,
          "inbound": True}],
        guide_account_id="guide", after=AFTER, group_key="g1")
    assert out["added"] == ("u1",)
    assert out["tested"] == ()


def test_tested_requires_an_explicit_signal_and_does_not_upgrade_a_bare_add():
    out = attribute_inbound(
        _members("u1", "u2"),
        [
            {"user_id": "u1", "account_id": "guide", "ts": AFTER + 5,
             "inbound": True, "tested": True},
            {"user_id": "u2", "account_id": "guide", "ts": AFTER + 6,
             "inbound": True},
        ],
        guide_account_id="guide", after=AFTER)
    assert out["added"] == ("u1", "u2")
    assert out["tested"] == ("u1",)


def test_helper_dms_and_cold_outbound_do_not_count():
    """帮手号收到的私聊、向导先开口的线程，都不是客户主动来试。"""
    out = attribute_inbound(
        _members("u1", "u2", "u3"),
        [
            {"user_id": "u1", "account_id": "helper", "ts": AFTER + 5,
             "inbound": True, "tested": True},
            {"user_id": "u2", "account_id": "guide", "ts": AFTER + 1,
             "inbound": False},
            {"user_id": "u2", "account_id": "guide", "ts": AFTER + 9,
             "inbound": True, "tested": True},
            {"user_id": "u3", "account_id": "guide", "ts": AFTER - 20,
             "inbound": True},
        ],
        guide_account_id="guide", after=AFTER)
    assert out["added"] == ()
    assert out["tested"] == ()


def test_people_outside_this_groups_member_list_are_ignored():
    out = attribute_inbound(
        _members("u1", group="g1") + [{"user_id": "u9", "group_key": "g2"}],
        [
            {"user_id": "u9", "account_id": "guide", "ts": AFTER + 3,
             "inbound": True, "tested": True},
            {"user_id": "stranger", "account_id": "guide", "ts": AFTER + 4,
             "inbound": True},
        ],
        guide_account_id="guide", after=AFTER, group_key="g1")
    assert out == {"added": (), "tested": ()}


def test_direction_alias_and_missing_guide_account():
    out = attribute_inbound(
        _members("u1"),
        [{"user_id": "u1", "account_id": "guide", "ts": AFTER + 2,
          "direction": "in", "test_started": True}],
        guide_account_id="guide", after=AFTER)
    assert out["tested"] == ("u1",)
    blank = attribute_inbound(
        _members("u1"),
        [{"user_id": "u1", "account_id": "guide", "ts": AFTER + 2,
          "inbound": True}],
        guide_account_id="  ", after=AFTER)
    assert blank == {"added": (), "tested": ()}


def test_configured_guide_account_reads_the_one_key():
    cfg = {"companion": {"group_show": {"guide_account": "  acc-9  "}}}
    assert configured_guide_account(cfg) == "acc-9"
    assert configured_guide_account(None) == ""
    assert configured_guide_account({"companion": {}}) == ""
    pb = Playbook(id="g", name="g", roles=(Role("guide"), Role("asker")))
    assert playbook_declares_guide(pb) is True
    assert playbook_declares_guide(
        Playbook(id="o", name="o", roles=(Role("advocate"),))) is False


def test_trial_marker_counts_and_a_casual_mention_does_not():
    """「开始测试」只认事件行。正文里随口出现这几个字不算测完。"""
    marker = trial_marker("matrixx")
    hit = attribute_inbound(
        _members("u1"),
        [
            {"user_id": "u1", "account_id": "guide", "ts": AFTER + 2,
             "direction": "in", "text": "在吗"},
            {"user_id": "u1", "account_id": "guide", "ts": AFTER + 8,
             "direction": "in", "text": marker},
        ],
        guide_account_id="guide", after=AFTER)
    assert hit["added"] == ("u1",)
    assert hit["tested"] == ("u1",)
    miss = attribute_inbound(
        _members("u1"),
        [{"user_id": "u1", "account_id": "guide", "ts": AFTER + 2,
          "direction": "in", "text": "我想 guide_trial 一下"}],
        guide_account_id="guide", after=AFTER)
    assert miss["added"] == ("u1",)
    assert miss["tested"] == ()


def test_a_second_private_line_is_not_doing_the_feature():
    """向导回过之后对方又说一句，仍然只是添加。没做那一项功能。"""
    started = attribute_inbound(
        _members("u1"),
        [
            {"user_id": "u1", "account_id": "guide", "ts": AFTER + 2,
             "direction": "in", "text": "在吗"},
            {"user_id": "u1", "account_id": "guide", "ts": AFTER + 5,
             "direction": "out", "text": "先看这一项"},
            {"user_id": "u1", "account_id": "guide", "ts": AFTER + 9,
             "direction": "in", "text": "我按这个走了"},
        ],
        guide_account_id="guide", after=AFTER, feature="matrixx")
    assert started["added"] == ("u1",)
    assert started["tested"] == ()


def test_only_the_feature_this_show_named_counts_as_tested():
    rows = [
        {"user_id": "u1", "account_id": "guide", "ts": AFTER + 2,
         "direction": "in", "text": "在吗"},
        {"user_id": "u1", "account_id": "guide", "ts": AFTER + 8,
         "direction": "in", "text": trial_marker("lingox")},
    ]
    wrong = attribute_inbound(
        _members("u1"), rows, guide_account_id="guide", after=AFTER,
        feature="matrixx")
    assert wrong["added"] == ("u1",) and wrong["tested"] == ()
    rows[-1]["text"] = trial_marker("matrixx")
    hit = attribute_inbound(
        _members("u1"), rows, guide_account_id="guide", after=AFTER,
        feature="matrixx")
    assert hit["tested"] == ("u1",)
    assert trial_marker("") == ""
    pb = Playbook(id="guide_trial", name="g", products=("matrixx",))
    assert single_feature(pb) == "matrixx"
    assert single_feature(Playbook(id="mix", name="m", products=("a", "b"))) == ""
    assert features_by_playbook({"guide_trial": pb}) == {"guide_trial": "matrixx"}
    cfg = {"companion": {"group_show": {"guide_feature": " matrixx "}}}
    assert configured_guide_feature(cfg) == "matrixx"
    assert configured_guide_feature({}) == ""


def test_only_a_guide_reply_after_the_line_counts_as_answered():
    inbound = [
        {"sender_id": "u1", "ts": 100},
        {"sender_id": "u2", "ts": 130},
    ]
    assert speakers_answered_by_guide(
        inbound, [{"ts": 120, "account_id": "guide"}]) == ("u1",)
    assert speakers_answered_by_guide(inbound, []) == ()


def test_fallback_waits_for_the_window_and_skips_people_who_already_added():
    assert fallback_peers(["u1", "u2"], ["u1", "u2"], ["u1"],
                          window_complete=False) == ()
    assert fallback_peers(["u1", "u2", "u3"], ["u1", "u2", "outsider"], ["u1"],
                          window_complete=True) == ("u2",)
