# -*- coding: utf-8 -*-
"""演出效果归因口径门禁（`src/companion/group_show/outcome.py` + ledgers 读账）。

这张卡是全页唯一回答「值不值得继续投」的读数，会被截图进周报、变成「这批号还养不养」
的依据。所以三条诚实红线必须有回归网钉住——它们坏掉时**数字看起来完全正常**：

1. **排练不算数**：dry_run 场次一条消息都没发过，算它有「效果」是纯自欺。
2. **窗口没走完不下判词**：40 分钟前刚演完的戏当然没数字，标成「无人问津」会让人
   去改一个根本没问题的剧本；且它不能进汇总分母，否则「演得越勤效果越差」。
3. **转化只认硬证据**：必须是「在群里发过言」且「首次私聊晚于演出」。用 last_ts 之类
   的近似会把本来就在聊的老客户全算成新转化——那个数字看起来同样完全正常。
"""
import pytest

from src.companion.group_show.outcome import (
    DEFAULT_WINDOW_HOURS,
    MAX_WINDOW_HOURS,
    VERDICT_COLD,
    VERDICT_HOT,
    VERDICT_PENDING,
    VERDICT_WARM,
    build_outcome,
    clamp_window_hours,
    count_conversions,
    is_real_show,
    outcome_verdict,
    rollup,
    summarize_inbound,
    window_bounds,
)

_HOUR = 3600.0


def _session(**kw):
    base = {
        "session_id": "s1", "playbook_id": "duo_matrixx", "group_key": "-100123",
        "platform": "telegram", "dry_run": False,
        "started_at": 1_000_000.0, "ended_at": 1_000_600.0,
        "cast": {"members": [{"account_id": "a1"}, {"account_id": "a2"}]},
    }
    base.update(kw)
    return base


def _msg(ts, sender="u1", text="hi"):
    return {"ts": ts, "sender_id": sender, "sender_name": sender, "text": text}


# ── 红线 1：排练绝不能算进效果 ────────────────────────────────────────────────


def test_rehearsal_is_never_an_outcome():
    assert is_real_show(_session(dry_run=False)) is True
    assert is_real_show(_session(dry_run=True)) is False


def test_session_without_dry_run_flag_is_treated_as_rehearsal():
    """缺字段的旧行按排练处理：把来历不明的场次算成真发会凭空造出一批「零效果」。"""
    s = _session()
    s.pop("dry_run")
    assert is_real_show(s) is False


def test_show_without_group_is_not_a_real_show():
    assert is_real_show(_session(group_key="")) is False


# ── 红线 2：窗口没走完不下判词，也不进分母 ────────────────────────────────────


def test_window_starts_at_end_of_show_and_knows_if_it_elapsed():
    s = _session()
    start, end, done = window_bounds(s, 24.0, now=s["ended_at"] + 1 * _HOUR)
    assert start == s["ended_at"]
    assert end == s["ended_at"] + 24 * _HOUR
    assert done is False
    _, _, done2 = window_bounds(s, 24.0, now=s["ended_at"] + 25 * _HOUR)
    assert done2 is True


def test_window_falls_back_to_start_when_show_never_ended():
    s = _session(ended_at=0.0)
    start, _, _ = window_bounds(s, 24.0, now=s["started_at"] + _HOUR)
    assert start == s["started_at"]


def test_incomplete_window_is_pending_regardless_of_numbers():
    assert outcome_verdict(window_complete=False, responders=0,
                           conversions=0) == VERDICT_PENDING
    assert outcome_verdict(window_complete=False, responders=9,
                           conversions=3) == VERDICT_PENDING


def test_pending_shows_do_not_dilute_the_response_rate():
    """还没到点的戏进了分母 ⇒「最近演得越勤，效果看起来越差」，纯读数陷阱。"""
    s = _session()
    hot = build_outcome(s, [_msg(s["ended_at"] + 60)], {"u1": s["started_at"] + 99},
                        window_hours=24.0, now=s["ended_at"] + 30 * _HOUR)
    fresh = build_outcome(_session(session_id="s2"), [], None,
                          window_hours=24.0, now=s["ended_at"] + 1 * _HOUR)
    r = rollup([hot, fresh])
    assert r["total"] == 2 and r["pending"] == 1 and r["judged"] == 1
    assert r["response_rate"] == 1.0, "未走完的场次不该被算进分母"


# ── 红线 3：转化只认硬证据链 ─────────────────────────────────────────────────


def test_conversion_needs_first_dm_after_the_show():
    ids = ("u1", "u2")
    # u1 首次私聊晚于演出 ⇒ 算；u2 早就在私聊 ⇒ 不算（老客户不是这场戏带来的）
    n, hit = count_conversions(ids, {"u1": 2_000.0, "u2": 500.0}, after=1_000.0)
    assert n == 1 and hit == ("u1",)


def test_conversion_unknown_when_ledger_missing():
    """读不到首次私聊表 ⇒ 0，但调用方必须另标 degraded：和「真的没人私聊」不是一回事。"""
    assert count_conversions(("u1",), None, after=0.0) == (0, ())
    assert count_conversions(("u1",), {}, after=0.0) == (0, ())


def test_person_who_never_spoke_in_group_is_not_attributed():
    """潜水观众不可归因——转化数是下限，不是全部战果。"""
    n, _ = count_conversions((), {"lurker": 9_999.0}, after=0.0)
    assert n == 0


# ── 群内反响：窗口上界必须裁，演员自己不算 ────────────────────────────────────


def test_inbound_outside_the_window_is_excluded():
    """`group_inbound_since` 只给下界；不裁上界的话，戏越老读数越好看。"""
    stat = summarize_inbound(
        [_msg(50), _msg(150, "u2"), _msg(9_999, "u3")], start=100.0, end=200.0)
    assert stat["responders"] == 1 and stat["replies"] == 1
    assert stat["sender_ids"] == ("u2",)


def test_distinct_people_counted_once_but_messages_counted_all():
    stat = summarize_inbound(
        [_msg(110, "u1"), _msg(120, "u1"), _msg(130, "u2")], start=100.0, end=200.0)
    assert stat["responders"] == 2 and stat["replies"] == 3


def test_no_reply_reports_minus_one_not_zero_latency():
    """0 会被读成「秒回」，恰好是相反的意思。"""
    assert summarize_inbound([], start=100.0, end=200.0)["first_reply_sec"] == -1.0


def test_first_reply_latency_is_measured_from_window_start():
    stat = summarize_inbound([_msg(160), _msg(130, "u2")], start=100.0, end=200.0)
    assert stat["first_reply_sec"] == 30.0


# ── 判词与汇总 ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("resp,conv,want", [
    (0, 0, VERDICT_COLD),
    (3, 0, VERDICT_WARM),
    (3, 1, VERDICT_HOT),
])
def test_verdict_ladder(resp, conv, want):
    assert outcome_verdict(window_complete=True, responders=resp,
                           conversions=conv) == want


def test_build_outcome_end_to_end():
    s = _session()
    now = s["ended_at"] + 30 * _HOUR
    o = build_outcome(s, [_msg(s["ended_at"] + 120, "u1"),
                          _msg(s["ended_at"] + 300, "u2")],
                      {"u1": s["started_at"] + 5 * _HOUR}, window_hours=24.0, now=now)
    assert o.window_complete is True
    assert o.responders == 2 and o.replies == 2
    assert o.conversions == 1 and o.verdict == VERDICT_HOT
    assert o.first_reply_sec == 120.0
    assert set(o.to_dict()) >= {"session_id", "verdict", "conversions", "responders"}


def test_thin_sample_playbook_gets_no_percentage():
    """两场戏的运气差异会被读成结论，运营据此砍掉一本其实没问题的剧本。"""
    from src.companion.group_show.outcome import MIN_SHOWS_FOR_PLAYBOOK, by_playbook

    s = _session()
    now = s["ended_at"] + 30 * _HOUR
    few = [build_outcome(_session(session_id=f"x{i}", playbook_id="thin"),
                         [_msg(s["ended_at"] + 60, f"u{i}")], None,
                         window_hours=24.0, now=now)
           for i in range(2)]
    row = by_playbook(few)[0]
    assert row["judged"] == 2 and row["enough_sample"] is False
    assert row["response_rate"] == -1.0, "样本不足却给了百分比"
    assert row["min_shows"] == MIN_SHOWS_FOR_PLAYBOOK


def test_playbook_with_enough_shows_gets_a_rate():
    from src.companion.group_show.outcome import MIN_SHOWS_FOR_PLAYBOOK, by_playbook

    s = _session()
    now = s["ended_at"] + 30 * _HOUR
    many = [build_outcome(_session(session_id=f"y{i}", playbook_id="solid"),
                          [_msg(s["ended_at"] + 60, f"u{i}")], None,
                          window_hours=24.0, now=now)
            for i in range(MIN_SHOWS_FOR_PLAYBOOK)]
    row = by_playbook(many)[0]
    assert row["enough_sample"] is True and row["response_rate"] == 1.0


def test_pending_shows_do_not_count_toward_playbook_sample():
    """还没到点的场次不能既拉低比率、又假装凑够了样本。"""
    from src.companion.group_show.outcome import by_playbook

    s = _session()
    fresh = build_outcome(_session(playbook_id="p"), [], None,
                          window_hours=24.0, now=s["ended_at"] + 1 * _HOUR)
    row = by_playbook([fresh])[0]
    assert row["shows"] == 1 and row["judged"] == 0
    assert row["enough_sample"] is False and row["response_rate"] == -1.0


def test_rollup_of_nothing_is_all_zero_not_a_crash():
    r = rollup([])
    assert r["total"] == 0 and r["response_rate"] == 0.0 and r["conversions"] == 0


# ── 窗口参数夹紧（脏输入不许炸，也不许放飞） ──────────────────────────────────


@pytest.mark.parametrize("raw,want", [
    (None, DEFAULT_WINDOW_HOURS), ("", DEFAULT_WINDOW_HOURS),
    ("abc", DEFAULT_WINDOW_HOURS), (0, DEFAULT_WINDOW_HOURS),
    (-5, DEFAULT_WINDOW_HOURS), (99999, MAX_WINDOW_HOURS), (48, 48.0),
])
def test_clamp_window_hours(raw, want):
    assert clamp_window_hours(raw) == want


# ── 读账层：排练必须被过滤掉，且缺账要如实标降级 ──────────────────────────────


class _Shows:
    def __init__(self, rows):
        self._rows = rows

    def recent_sessions(self, *, limit=20, group_key=""):
        return list(self._rows)[:limit]


class _Inbox:
    def __init__(self, inbound=None, dm=None, boom=False):
        self._in = inbound or []
        self._dm = dm or {}
        self._boom = boom

    def group_inbound_since(self, *, chat_key, since_ts, platform,
                            exclude_senders, limit):
        return list(self._in)

    def first_private_inbound_ts(self, *, chat_keys, platform=""):
        if self._boom:
            raise RuntimeError("db down")
        return dict(self._dm)


def test_read_show_outcomes_drops_rehearsals():
    from src.companion.group_show.ledgers import read_show_outcomes

    shows = _Shows([_session(session_id="live1", dry_run=False),
                    _session(session_id="dry1", dry_run=True)])
    got = read_show_outcomes(shows, _Inbox(), window_hours=24.0, limit=10,
                             now=2_000_000.0)
    assert [o.session_id for o in got] == ["live1"], "排练混进了效果读数"


def test_read_show_outcomes_flags_degraded_when_dm_ledger_fails():
    """读挂必须留痕：不然「查不到」会被当成「真的没人私聊」。"""
    from src.companion.group_show.ledgers import read_show_outcomes

    s = _session()
    deg = []
    got = read_show_outcomes(_Shows([s]),
                             _Inbox(inbound=[_msg(s["ended_at"] + 60)], boom=True),
                             window_hours=24.0, limit=5,
                             now=s["ended_at"] + 30 * _HOUR, degraded=deg)
    assert got and got[0].conversions == 0
    assert deg, "首次私聊表读挂了却没标 degraded"


def test_first_private_inbound_ts_uses_min_not_last_activity(tmp_path):
    """真库回归：转化必须按**首次**私聊判，用 last_ts 会把老客户全算成新转化。

    这条是整条效果链最贵的一个坑——老客户当然「最近有活跃」，用最后活跃时刻算，
    转化数会直接刷爆，而那个数字看起来完全正常，没人会去怀疑。
    """
    from src.inbox.store import InboxStore

    st = InboxStore(str(tmp_path / "inbox.db"))
    conn = st._conn

    def cols(t):
        return [r[1] for r in conn.execute(f"PRAGMA table_info({t})").fetchall()]

    def ins(t, **kw):
        ks = [k for k in kw if k in cols(t)]
        conn.execute(
            f"INSERT INTO {t} ({','.join(ks)}) VALUES ({','.join('?' * len(ks))})",
            [kw[k] for k in ks])

    cv = dict(platform="telegram", account_id="a1", created_at=1, updated_at=1)
    ins("conversations", conversation_id="c_old", chat_key="old_customer",
        chat_type="private", last_ts=9000, **cv)
    ins("conversations", conversation_id="c_new", chat_key="new_lead",
        chat_type="private", last_ts=9000, **cv)
    ins("conversations", conversation_id="c_grp", chat_key="grp_guy",
        chat_type="group", last_ts=9000, **cv)
    # 老客户：很早就私聊过(100)，最近也活跃(9000)
    ins("messages", conversation_id="c_old", ts=100, direction="in",
        text="hi", sender_id="old_customer", ingested_at=1)
    ins("messages", conversation_id="c_old", ts=9000, direction="in",
        text="yo", sender_id="old_customer", ingested_at=1)
    # 新线索：演出之后(5000)才第一次私聊
    ins("messages", conversation_id="c_new", ts=5000, direction="in",
        text="ask", sender_id="new_lead", ingested_at=1)
    # 只在群里说过话的人：不该出现在私聊表里
    ins("messages", conversation_id="c_grp", ts=5000, direction="in",
        text="grp", sender_id="grp_guy", ingested_at=1)
    conn.commit()

    got = st.first_private_inbound_ts(["old_customer", "new_lead", "grp_guy"])
    assert got["old_customer"] == 100.0, "取的不是 MIN，老客户会被误判成新转化"
    assert got["new_lead"] == 5000.0
    assert "grp_guy" not in got, "群会话被当成私聊了"

    n, hit = count_conversions(["old_customer", "new_lead"], got, after=1000.0)
    assert n == 1 and hit == ("new_lead",)


def test_read_show_outcomes_skips_dm_lookup_when_nobody_spoke():
    """没人开口就没有可归因对象，不该白跑查询、更不该把 degraded 标脏。"""
    from src.companion.group_show.ledgers import read_show_outcomes

    s = _session()
    deg = []
    got = read_show_outcomes(_Shows([s]), _Inbox(inbound=[], boom=True),
                             window_hours=24.0, limit=5,
                             now=s["ended_at"] + 30 * _HOUR, degraded=deg)
    assert got and got[0].verdict == VERDICT_COLD
    assert not deg, "没人开口时不该触发首次私聊查询"
