# -*- coding: utf-8 -*-
"""``group_show.ledgers``——「读账 → 拼参」这一段的唯一实现。

这里守的不是某个算法对不对，而是**三个消费方拿到的是同一份东西**：影子 CLI 的预览、
导播台的卡片、以及将来的真发链路。这三处只要有一处自己再读一遍账，就会出现那种最难
发现的事故——排练里四个号演得热热闹闹，真发只有两个能上，而两个数都自称权威。

另一条同等重要的不变量：**任何一本账读挂都必须留痕**。少读一本的表现是共现表变空、
冷却名单变空，跟「一切安全」长得一模一样，没有人会去怀疑一片绿。
"""
import time

import pytest

from src.companion.group_show.ledgers import (
    ALL_HISTORY,
    ShowContext,
    merge_speech,
    read_last_spoke,
    read_prior_slots,
    read_role_counts,
    read_show_context,
    read_speech,
    speaking_groups,
)


# ── 替身 ────────────────────────────────────────────────────────────────────


class _Shows:
    """场次库替身：记下每次被问到的 ``since``，好断言窗口口径。"""

    def __init__(self, *, speech=None, roles=None, last=None, members=None,
                 prior=None):
        self._speech = speech or {}
        self._roles = roles or {}
        self._last = last or {}
        self._members = members or {}
        self._prior = prior or {}
        self.since_seen = []
        self.prior_groups_seen = []

    def performance_ledger(self, *, platform="telegram", since=0.0):
        self.since_seen.append(since)
        return dict(self._speech)

    def role_ledger(self, *, platform="telegram", since=0.0):
        return dict(self._roles)

    def prior_slots(self, *, group_key="", platform="telegram"):
        self.prior_groups_seen.append(group_key)
        return dict(self._prior)

    def last_spoke_at(self, *, platform="telegram", since=0.0, exclude_group=""):
        return {k: v for k, v in self._last.items() if k != exclude_group}

    def memberships(self):
        return dict(self._members)


class _Inbox:
    def __init__(self, *, speech=None, last=None):
        self._speech = speech or {}
        self._last = last or {}

    def group_speech_ledger(self, *, since_ts=0.0):
        return dict(self._speech)

    def group_last_spoke_at(self, *, since_ts=0.0, exclude_group=""):
        return dict(self._last)


class _Boom:
    def performance_ledger(self, **kw):
        raise RuntimeError("boom")

    def role_ledger(self, **kw):
        raise RuntimeError("boom")

    def prior_slots(self, **kw):
        raise RuntimeError("boom")

    def last_spoke_at(self, **kw):
        raise RuntimeError("boom")

    def memberships(self):
        raise RuntimeError("boom")


class _Old:
    """老版本 store：不认 ``exclude_group`` 这个新 kwarg。"""

    def last_spoke_at(self, *, platform="telegram", since=0.0):
        return {"a": 5000.0}


# ── 合并口径 ────────────────────────────────────────────────────────────────


def test_merge_speech_dedups_within_a_group_and_keeps_order():
    """同一个号既演过戏又自动回复过，算两次会让共现矩阵凭空虚高一倍。"""
    merged = merge_speech({"g1": ["a", "b"]}, {"g1": ["b", "c"], "g2": ["a"]})
    assert merged == {"g1": ["a", "b", "c"], "g2": ["a"]}
    assert merge_speech(None, {"": ["x"]}, {"g": []}) == {"g": []}


def test_merge_speech_survives_junk_shapes():
    """台账来自外部库，形状不由我们保证；一行脏数据不该让整张卡白屏。"""
    assert merge_speech("not a dict", 42, None) == {}


def test_speaking_groups_counts_distinct_groups_per_account():
    """角色占比的分母＝这个号在几个群开过口，不是发过几条。"""
    assert speaking_groups({"g1": ["a", "b"], "g2": ["a"]}) == {"a": 2, "b": 1}
    assert speaking_groups(None) == {}
    assert speaking_groups({"g1": ["", None]}) == {}


def test_read_speech_takes_the_union_of_both_sources():
    """只读场次库 ⇒「一场戏没演过就安全」，而这些号早靠自动回复在几十个群里同框了。"""
    speech = read_speech(_Shows(speech={"g1": ["a"]}),
                         _Inbox(speech={"g1": ["b"], "g2": ["a"]}))
    assert speech == {"g1": ["a", "b"], "g2": ["a"]}


def test_read_speech_notes_a_source_that_blew_up():
    """少一个来源 ＝ 共现变小 ＝ 看起来更安全。不留痕的话这个假绿灯没人会怀疑。"""
    degraded = []
    speech = read_speech(_Boom(), _Inbox(speech={"g": ["a"]}), degraded=degraded)
    assert speech == {"g": ["a"]}
    assert degraded == ["shows"]


def test_read_role_counts_only_comes_from_the_show_store():
    """日常自动回复没有「角色」这回事，硬安一个 advocate 会把这条轴的读数彻底污染。"""
    assert read_role_counts(_Shows(roles={("a", "advocate"): 3})) == {
        ("a", "advocate"): 3}
    assert read_role_counts(None) == {}
    degraded = []
    assert read_role_counts(_Boom(), degraded=degraded) == {}
    assert degraded == ["shows"]


# ── 窗口口径 ────────────────────────────────────────────────────────────────


def test_window_defaults_to_the_rolling_window_not_all_history():
    """共现会饱和：跑几个月每一对都同过台，全历史口径下这个数再也降不下来。"""
    shows = _Shows(speech={"g": ["a"]})
    read_speech(shows, None, window_days=0)
    assert shows.since_seen[-1] > 0, "缺省必须是滚动窗，不是全历史"
    read_speech(shows, None, window_days=7)
    assert time.time() - shows.since_seen[-1] == pytest.approx(7 * 86400, abs=5)


def test_all_history_is_an_explicit_opt_in():
    """看板需要累计视角来决定要不要换号池；闸门永远不该走这一档。"""
    shows = _Shows(speech={"g": ["a"]})
    read_speech(shows, None, window_days=ALL_HISTORY)
    assert shows.since_seen[-1] == 0.0


# ── 跨群冷却 ────────────────────────────────────────────────────────────────


def test_read_last_spoke_merges_both_sources_and_takes_the_later_one():
    """只读场次库会给出「这个号三天没说话」的假读数，而它十秒前刚在隔壁群回复过。"""
    merged = read_last_spoke(_Shows(last={"a": 1000.0, "b": 9000.0}),
                             _Inbox(last={"a": 5000.0, "c": 7000.0}))
    assert merged == {"a": 5000.0, "b": 9000.0, "c": 7000.0}


def test_read_last_spoke_survives_a_source_that_blows_up():
    """一个来源读不动只该少一个来源，绝不能让整条闸门读不出数而卡死。"""
    degraded = []
    assert read_last_spoke(_Boom(), None, degraded=degraded) == {}
    assert degraded == ["shows"]
    assert read_last_spoke(None, None) == {}


def test_read_last_spoke_falls_back_for_an_older_store():
    """老库不认新 kwarg 时退一步用旧签名：偏保守的读数好过整本账变空。"""
    degraded = []
    assert read_last_spoke(_Old(), None, group_key="g1",
                           degraded=degraded) == {"a": 5000.0}
    assert degraded == [], "能降级读到就不算降级"


def test_read_last_spoke_excludes_the_group_we_are_about_to_play_in():
    """同一个群里连着说两句是正常对话；不排除的话号会被自己挡住，闸门只会被关掉。"""
    assert read_last_spoke(_Shows(last={"g1": 1.0, "a": 2.0}), None,
                           group_key="g1") == {"a": 2.0}


# ── ShowContext：拼参与判词 ─────────────────────────────────────────────────


def test_cast_kwargs_carries_every_gate_at_once():
    """这是全系统唯一一处决定「选角受哪些约束」的地方，少一个键 ＝ 悄悄少挂一条闸门。"""
    ctx = read_show_context(
        _Shows(speech={f"g{i}": ["a", "b"] for i in range(40)},
               roles={("a", "advocate"): 4},
               members={f"g{i}": ["a", "b"] for i in range(40)},
               prior={"a": "advocate"}),
        None, pool=["a", "b", "c"], group_key="new")
    kwargs = ctx.cast_kwargs(seed="new")
    assert set(kwargs) == {"co_performance", "max_speakers", "role_counts",
                           "role_limit", "prior_slots", "seed"}
    assert kwargs["co_performance"][("a", "b")] == 40
    assert kwargs["role_counts"] == {("a", "advocate"): 4}
    assert kwargs["prior_slots"] == {"a": "advocate"}
    assert kwargs["role_limit"] > 0 and kwargs["seed"] == "new"


def test_prior_slots_are_asked_for_the_target_group_only():
    """粘性是「对这个群的观众别翻脸」——问账必须带目标群，全局看板问不出粘性。"""
    shows = _Shows(prior={"a": "advocate"})
    ctx = read_show_context(shows, None, pool=["a"], group_key="g7")
    assert ctx.prior_slots == {"a": "advocate"}
    assert shows.prior_groups_seen == ["g7"]


def test_prior_slots_stay_empty_without_a_target_group():
    shows = _Shows(prior={"a": "advocate"})
    ctx = read_show_context(shows, None, pool=["a"])
    assert ctx.prior_slots == {}
    assert shows.prior_groups_seen == [], "没有目标群就不该去问这本账"


def test_an_old_store_without_the_prior_ledger_is_not_an_incident():
    """缺方法是老库/降级实例的正常形态：空表即可，不该记降级、更不该拦开演。"""
    degraded = []
    assert read_prior_slots(_Old(), group_key="g", degraded=degraded) == {}
    assert degraded == []


def test_role_gate_stays_off_without_a_ledger():
    """没台账还硬拦 ＝ 凭空造约束。上限 0 ＝ 关闸，与这条轴上线前一字不差。"""
    ctx = read_show_context(_Shows(), None, pool=["a"])
    assert ctx.role_limit == 0 and ctx.cast_kwargs()["role_limit"] == 0


def test_budget_stays_open_when_the_pool_is_empty():
    """号池为空不是「预算＝独白」，是没有数据。全新装机每场被压成独白 ＝ 工具坏了。"""
    ctx = read_show_context(_Shows(speech={"g": ["a"]}), None, pool=[])
    assert ctx.budget == {}
    assert ctx.speaker_cap(requested=4)["effective"] == 4
    # 但报告本身照出——卡片要能显示「你还没有号」这个事实
    assert ctx.exposure


def test_verdict_takes_the_single_worst_axis():
    """平台按最可疑的那个特征下手，不是按平均分。"""
    assert ShowContext(exposure={"verdict": "safe"},
                       roles={"verdict": "danger"}).verdict == "danger"
    assert ShowContext().verdict == "safe"


def test_context_survives_a_store_that_blows_up_on_everything():
    """读账全挂时退化成「不限」而不是「全禁」：全禁会让运营把整套护栏关掉。"""
    ctx = read_show_context(_Boom(), None, pool=["a", "b"])
    assert ctx.speech == {} and ctx.role_counts == {} and ctx.last_spoke == {}
    assert ctx.role_limit == 0
    assert ctx.speaker_cap(requested=3)["effective"] == 3
    assert "shows" in ctx.degraded


def test_waiting_names_who_is_still_in_the_cooldown_window():
    now = time.time()
    ctx = ShowContext(last_spoke={"hot": now - 60.0, "cold": now - 99999.0})
    rows = ctx.waiting(["hot", "cold", "unknown"], at=now)
    assert [r["account"] for r in rows] == ["hot"]
    assert rows[0]["wait_sec"] > 0
    # 缺记录一律放行：冷启动/换库之后这张表必然是空的
    assert ctx.delay_for("unknown", now) == 0
