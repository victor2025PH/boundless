# -*- coding: utf-8 -*-
"""群戏落库门禁 —— 落库是旁路能力，它的失败模式必须永远是「少一行审计」。

一场戏是跨小时的长事务，中途必然遇到重启/切实例/让路/冻结。这个文件守两条主线：

1. **续演正确性**：写进去的必须能原样读回来（游标、演员表、事件流），且重复写入
   幂等。读回来少一拍 ＝ 同一批号在同一个群把同一段话又演一遍，那是最刺眼的机器特征。
2. **软失败纪律**：建库失败、坏 casting、脏行、并发写，一律不得把正在演的戏掀翻。
   宁可丢一行审计，也不能丢半场戏。

全部用 ``tmp_path``，不碰生产库，不联网。
"""
from __future__ import annotations

import sqlite3
import threading

import pytest

from src.companion.group_show.playbook import (
    Beat,
    CastMember,
    Casting,
    Playbook,
    Role,
    ShowEvent,
    ShowState,
)
from src.companion.group_show.store import (
    DEFAULT_DB_PATH,
    GroupShowStore,
    casting_from_json,
    casting_to_json,
    configure_group_show_store,
    get_group_show_store,
    reset_group_show_store,
)


@pytest.fixture()
def store(tmp_path):
    st = GroupShowStore(tmp_path / "sub" / "gs.db")
    yield st
    if st._conn is not None:          # noqa: SLF001 —— 测试收尾释放文件句柄
        st._conn.close()


@pytest.fixture(autouse=True)
def _isolate_singleton():
    """单例是进程级全局，测试前后都清干净，免得污染同 session 的其它用例。"""
    reset_group_show_store()
    yield
    reset_group_show_store()


def _casting() -> Casting:
    return Casting(
        members=(
            CastMember(slot="advocate", account_id="a1", persona_id="p1",
                       display_name="小美", platform="telegram"),
            CastMember(slot="skeptic", account_id="a2", persona_id="p2",
                       display_name="老王", platform="line"),
        ),
        unfilled=("bystander",),
    )


def _playbook() -> Playbook:
    return Playbook(id="pb_x", name="剧本X", system="growth",
                    roles=(Role("advocate"), Role("skeptic")),
                    beats=(Beat(id="b1", role="advocate", intent="抛痛点"),))


def _state(sid="s1", n_events=3, **kw) -> ShowState:
    st = ShowState(session_id=sid, group_key=kw.pop("group_key", "g1"),
                   playbook=_playbook(), casting=_casting(), **kw)
    for i in range(1, n_events + 1):
        st.append_event(ShowEvent(seq=i, ts=1000.0 + i, speaker_account=f"a{i}",
                                  role="advocate", beat_id=f"b{i}",
                                  text=f"第{i}句", kind="line"))
    return st


# ── 建库 ────────────────────────────────────────────────────────────────────


def test_missing_directory_is_created_and_store_is_usable(tmp_path):
    """库目录不存在时自己建——首次部署/换实例不该需要人手 mkdir。"""
    st = GroupShowStore(tmp_path / "nope" / "deep" / "gs.db")
    try:
        assert st.available is True
        st.save_session(_state())
        assert st.load_session("s1") is not None
    finally:
        st._conn.close()   # noqa: SLF001


def test_opening_the_same_file_twice_is_safe(tmp_path):
    """两个实例开同一个库文件不能炸，且建表必须幂等（第二次不该 already exists）。

    双实例部署 + 看板进程 + 导演循环同时开库是常态。
    """
    path = tmp_path / "gs.db"
    a, b = GroupShowStore(path), GroupShowStore(path)
    try:
        assert a.available and b.available
        a.save_session(_state("shared"))
        assert b.load_session("shared") is not None
        b.append_event("shared", ShowEvent(seq=9, ts=1.0, speaker_account="a9",
                                           role="r", beat_id="b9", text="来自B"))
        assert [e["seq"] for e in a.events("shared")] == [1, 2, 3, 9]
    finally:
        a._conn.close(); b._conn.close()   # noqa: SLF001


def test_unusable_db_path_degrades_to_a_silent_no_op(tmp_path):
    """建库失败必须降级空转而不是抛——调用方不该为了落库去写 try。

    整个 group_show 里落库是唯一「可以没有」的能力，它抛异常就会把真发链路带走。
    """
    st = GroupShowStore(tmp_path)          # 目录当库文件 → connect 必失败
    assert st.available is False
    st.save_session(_state())
    st.append_event("s1", ShowEvent(seq=1, ts=1.0, speaker_account="a",
                                    role="r", beat_id="b", text="x"))
    st.update_status("s1", "done", ended_at=5.0)
    assert st.load_session("s1") is None
    assert st.events("s1") == []
    assert st.recent_sessions() == []


# ── 场次读写 ────────────────────────────────────────────────────────────────


def test_session_roundtrip_preserves_everything_needed_to_resume(store):
    """续演要用到的字段必须逐个原样读回：游标、状态、dry_run、演员表。

    少任何一个，续演就会「从头再演一遍」或者「用错演员表接着演」。
    """
    st = _state(beat_cursor=7, status="running", dry_run=False,
                platform="line", started_at=1700.0, ended_at=0.0)
    store.save_session(st)
    row = store.load_session("s1")

    assert row["session_id"] == "s1" and row["group_key"] == "g1"
    assert row["playbook_id"] == "pb_x"
    assert row["beat_cursor"] == 7 and row["status"] == "running"
    assert row["dry_run"] is False and row["platform"] == "line"
    assert row["started_at"] == 1700.0
    assert row["casting"].by_slot("advocate").display_name == "小美"
    assert row["casting"].by_slot("skeptic").platform == "line"
    assert row["casting"].unfilled == ("bystander",)


def test_saving_twice_updates_one_row_instead_of_duplicating(store):
    """反复 upsert 只更新同一行——导演循环每拍都会存一次，写成多行就没法续演。"""
    st = _state(beat_cursor=1)
    store.save_session(st)
    st.beat_cursor = 5
    st.status = "done"
    store.save_session(st)

    rows = store.recent_sessions()
    assert len(rows) == 1
    assert rows[0]["beat_cursor"] == 5 and rows[0]["status"] == "done"


def test_playbook_body_is_not_copied_into_the_db(store):
    """库里只存 playbook_id——拷一份剧本进库就会出现「库里旧剧本 vs 文件新剧本」双源。"""
    store.save_session(_state())
    with sqlite3.connect(store._db_path) as conn:   # noqa: SLF001
        raw = conn.execute("SELECT * FROM choreography_sessions").fetchone()
    assert "抛痛点" not in str(raw), "剧本正文不该进库"


def test_load_unknown_or_blank_session_returns_none(store):
    """查不到就明确返回 None，不能给一个空壳 dict 让调用方以为「有这场戏」。"""
    for sid in ("不存在", "", "   ", None):
        assert store.load_session(sid) is None


def test_blank_session_id_writes_nothing(store):
    """没有 session_id 的状态不落库——一行没有主键语义的孤儿行比丢它更麻烦。"""
    store.save_session(_state(sid=""))
    store.save_session(None)
    assert store.recent_sessions() == []


def test_recent_sessions_orders_by_start_time_and_filters_by_group(store):
    """看板要的是「最近的在最前」，并且能只看一个群。顺序错了运营就在看旧场次。"""
    store.save_session(_state("s_old", group_key="g1", started_at=100.0))
    store.save_session(_state("s_new", group_key="g1", started_at=900.0))
    store.save_session(_state("s_other", group_key="g2", started_at=500.0))

    assert [r["session_id"] for r in store.recent_sessions()] == \
           ["s_new", "s_other", "s_old"]
    assert [r["session_id"] for r in store.recent_sessions(group_key="g1")] == \
           ["s_new", "s_old"]
    assert store.recent_sessions(group_key="不存在的群") == []


def test_recent_sessions_limit_is_always_sane(store):
    """limit 传进任何垃圾值都要返回一个有界结果，绝不抛、也绝不一次拉全表。"""
    for i in range(12):
        store.save_session(_state(f"s{i}", started_at=float(i)))
    for bad in (0, -1, "abc", None, 10 ** 9, 3.9):
        rows = store.recent_sessions(limit=bad)
        assert 1 <= len(rows) <= 12


def test_recent_sessions_report_delivered_lines_not_beat_cursor(store):
    """列表行带 ``lines_sent``＝真送达数（line+media），跳拍不算。

    坐席盯进度要的是「群里实际出现了几条」；``beat_cursor`` 含跳拍，口径不同。
    首场灰度（2026-07-27）就是靠翻 events 表才知道戏演到哪——列表必须自带。
    """
    st = _state("s_live", n_events=2)                       # 2 条 line
    st.append_event(ShowEvent(seq=3, ts=1003.0, speaker_account="a1",
                              role="advocate", beat_id="b3", text="",
                              kind="skip"))                 # 跳拍不算送达
    st.append_event(ShowEvent(seq=4, ts=1004.0, speaker_account="a2",
                              role="skeptic", beat_id="b4", text="[图]",
                              kind="media"))                # 媒体算送达
    store.save_session(st)
    store.save_session(_state("s_empty", n_events=0))       # 一条都没发的场次
    rows = {r["session_id"]: r for r in store.recent_sessions()}
    assert rows["s_live"]["lines_sent"] == 3
    assert rows["s_empty"]["lines_sent"] == 0


# ── 状态机 ──────────────────────────────────────────────────────────────────


def test_update_status_keeps_the_existing_end_time_when_not_given(store):
    """只改状态时不得把已写好的收尾时间清成 0。

    ended_at 被误清 ＝ 时长统计与「这场戏演完没有」的判断一起失真，且不可恢复。
    """
    store.save_session(_state(status="running"))
    store.update_status("s1", "done", ended_at=2000.0)
    store.update_status("s1", "aborted")
    row = store.load_session("s1")
    assert row["status"] == "aborted" and row["ended_at"] == 2000.0


def test_unknown_status_is_still_recorded(store):
    """本库是运行时的镜子不是校验器：眼生的状态照写，丢状态比存个怪值更糟。"""
    store.save_session(_state())
    store.update_status("s1", "谁知道这是啥")
    assert store.load_session("s1")["status"] == "谁知道这是啥"


def test_status_update_on_missing_or_blank_target_is_a_no_op(store):
    """改一个不存在的场次不能创建幽灵行，也不能抛。"""
    store.update_status("查无此场", "done", ended_at=1.0)
    store.save_session(_state())
    store.update_status("s1", "")
    store.update_status(None, None)
    assert len(store.recent_sessions()) == 1
    assert store.load_session("s1")["status"] == "pending"


# ── 事件流 ──────────────────────────────────────────────────────────────────


def test_events_come_back_in_seq_order_regardless_of_write_order(store):
    """事件流必须按 seq 升序读回——回放/自然度评测都按这个顺序理解「谁先说」。"""
    for seq in (3, 1, 2):
        store.append_event("s1", ShowEvent(seq=seq, ts=float(seq),
                                           speaker_account=f"a{seq}", role="r",
                                           beat_id=f"b{seq}", text=f"第{seq}句"))
    assert [e["seq"] for e in store.events("s1")] == [1, 2, 3]
    assert [e["text"] for e in store.events("s1")] == ["第1句", "第2句", "第3句"]


def test_replaying_the_same_seq_overwrites_instead_of_duplicating(store):
    """同 (场次, seq) 重放是覆盖不是追加——崩溃重放/save 补写都会撞上它。

    退化成追加，回放里就会出现「同一句说了两遍」，而那正是我们最怕的观感。
    """
    ev = ShowEvent(seq=1, ts=1.0, speaker_account="a1", role="r",
                   beat_id="b1", text="原文")
    store.append_event("s1", ev)
    store.append_event("s1", ev)
    store.append_event("s1", ShowEvent(seq=1, ts=2.0, speaker_account="a1",
                                       role="r", beat_id="b1", text="改过的"))
    rows = store.events("s1")
    assert len(rows) == 1 and rows[0]["text"] == "改过的" and rows[0]["ts"] == 2.0


def test_save_session_backfills_events_that_append_missed(store):
    """save_session 顺带补齐事件——append 在崩溃前漏写时，续演仍拿得到完整历史。"""
    st = _state(n_events=4)
    store.append_event("s1", st.events[0])      # 只写了第一条
    store.save_session(st)
    assert [e["seq"] for e in store.events("s1")] == [1, 2, 3, 4]


def test_non_positive_seq_events_are_skipped_quietly(store):
    """seq<=0 的事件被跳过且不影响其它行——主键语义坏掉的行不该进库。"""
    store.append_event("s1", ShowEvent(seq=0, ts=1.0, speaker_account="a",
                                       role="r", beat_id="b", text="零号"))
    store.append_event("s1", ShowEvent(seq=-3, ts=1.0, speaker_account="a",
                                       role="r", beat_id="b", text="负号"))
    store.append_event("s1", ShowEvent(seq=1, ts=1.0, speaker_account="a",
                                       role="r", beat_id="b", text="正常"))
    assert [e["text"] for e in store.events("s1")] == ["正常"]


def test_event_streams_of_different_sessions_never_mix(store):
    """场次之间必须完全隔离——串场会让归因把 A 场的转化算到 B 场头上。"""
    store.save_session(_state("sA", n_events=2))
    store.save_session(_state("sB", n_events=3))
    assert len(store.events("sA")) == 2
    assert len(store.events("sB")) == 3


def test_garbage_event_inputs_never_raise(store):
    """None / 缺字段 / 类型不对的事件一律安静跳过或降级，不抛。"""
    class _Bare:
        pass

    store.append_event("s1", None)
    store.append_event(None, ShowEvent(seq=1, ts=1.0, speaker_account="a",
                                       role="r", beat_id="b", text="x"))
    store.append_event("s1", _Bare())
    store.append_event("s1", "不是事件")
    assert store.events("s1") == []
    assert store.events(None) == [] and store.events("") == []


def test_event_text_is_stored_verbatim(store):
    """台词原文必须逐字保存（含 emoji/换行/引号）——回放素材被清洗过就失去证据价值。"""
    weird = "第一行\n第二行 \"引号\" 😅 '单引号' 100%"
    store.append_event("s1", ShowEvent(seq=1, ts=1.0, speaker_account="a",
                                       role="r", beat_id="b", text=weird))
    assert store.events("s1")[0]["text"] == weird


# ── 演员表编解码 ────────────────────────────────────────────────────────────


def test_casting_survives_a_json_roundtrip():
    """演员表编解码必须等值——续演拿错演员表就是「换了个人接着演」。"""
    back = casting_from_json(casting_to_json(_casting()))
    assert [(m.slot, m.account_id, m.persona_id, m.display_name, m.platform)
            for m in back.members] == \
           [(m.slot, m.account_id, m.persona_id, m.display_name, m.platform)
            for m in _casting().members]
    assert back.unfilled == ("bystander",)


def test_broken_casting_json_degrades_to_an_empty_cast():
    """坏 JSON 一律降级为空演员表——读一行坏数据不该让整个看板 500。"""
    for bad in ("", "{", "null", "[]", b"\xff\xfe", None, 42, {"nope": 1}):
        assert casting_from_json(bad).members == ()


def test_casting_with_a_non_list_members_field_degrades_instead_of_raising():
    """``members`` 字段类型不对时也该降级为空演员表，而不是把调用方掀翻。

    这个函数是续演路径上读库的第一道解码，它抛异常＝这场戏读不出来、只能从头重演。
    """
    assert casting_from_json({"members": 7}).members == ()


def test_casting_accepts_the_legacy_list_form():
    """兼容早期「只有 members 数组」的存量数据——老库不该在升级后读不出人。"""
    got = casting_from_json('[{"slot": "advocate", "account_id": "a1"}]')
    assert got.by_slot("advocate").account_id == "a1"
    assert got.by_slot("advocate").platform == "telegram"


def test_members_without_a_slot_are_dropped():
    """没有 slot 的成员进不了演员表——slot 是 beats 的引用键，缺了它这个人无法登台。"""
    got = casting_from_json({"members": [{"account_id": "a1"},
                                         {"slot": "", "account_id": "a2"},
                                         "不是字典",
                                         {"slot": "skeptic"}]})
    assert [m.slot for m in got.members] == ["skeptic"]


def test_a_broken_casting_object_does_not_block_the_session_write(store):
    """演员表对象本身炸了，场次行仍要落下去——丢演员表远好过丢整场戏。"""
    class _Boom:
        @property
        def members(self):
            raise RuntimeError("casting 坏了")

    st = _state()
    st.casting = _Boom()
    store.save_session(st)
    row = store.load_session("s1")
    assert row is not None and row["cast"]["members"] == []
    assert len(store.events("s1")) == 3, "事件流不该被坏演员表连累"


# ── 并发与脏行 ──────────────────────────────────────────────────────────────


def test_concurrent_appends_from_many_threads_lose_nothing(store):
    """多线程并发写不能丢行也不能报错——web 线程/导演循环/RPA runner 各有自己的 loop。"""
    errors = []

    def _worker(base):
        try:
            for i in range(20):
                store.append_event("s1", ShowEvent(
                    seq=base * 100 + i + 1, ts=float(i), speaker_account=f"a{base}",
                    role="r", beat_id="b", text=f"{base}-{i}"))
        except Exception as exc:      # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=_worker, args=(k,)) for k in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert len(store.events("s1")) == 160


def test_concurrent_session_saves_do_not_corrupt_the_row(store):
    """并发 upsert 同一场次只能留下一行合法数据，不能写成半截状态。"""
    def _worker(cursor):
        store.save_session(_state(beat_cursor=cursor))

    threads = [threading.Thread(target=_worker, args=(c,)) for c in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    rows = store.recent_sessions()
    assert len(rows) == 1 and 0 <= rows[0]["beat_cursor"] <= 7


def test_one_corrupt_row_must_not_take_down_the_whole_session_list(store):
    """一行脏数据只能损失它自己，不能把整批读取带走。

    看板一次拉 20 场，只要有一行被外部工具/旧版本写坏，运营就会看到整块空白而不是
    19 场正常数据——而空白与「今天没有戏」在界面上长得一模一样。
    """
    store.save_session(_state("good", started_at=10.0))
    with sqlite3.connect(store._db_path) as conn:      # noqa: SLF001
        conn.execute(
            "INSERT INTO choreography_sessions (session_id, group_key, "
            "beat_cursor, started_at) VALUES ('bad', 'g1', 'abc', 20.0)")
        conn.commit()

    rows = store.recent_sessions()
    assert any(r["session_id"] == "good" for r in rows)


def test_one_corrupt_event_row_must_not_take_down_the_stream(store):
    """一条脏事件行不能让整场戏的历史读不出来——续演会因此从头重演。"""
    store.append_event("s1", ShowEvent(seq=1, ts=1.0, speaker_account="a",
                                       role="r", beat_id="b", text="正常"))
    with sqlite3.connect(store._db_path) as conn:      # noqa: SLF001
        conn.execute(
            "INSERT INTO choreography_events (session_id, seq, ts, text) "
            "VALUES ('s1', 2, 'oops', '脏行')")
        conn.commit()

    assert any(e["text"] == "正常" for e in store.events("s1"))


# ── 进程级单例 ──────────────────────────────────────────────────────────────


def test_configure_is_idempotent_and_returns_the_same_instance(tmp_path):
    """启动期重复装配返回同一实例——重复建连会让 WAL 文件与锁语义变复杂。"""
    a = configure_group_show_store(tmp_path / "one.db")
    b = configure_group_show_store(tmp_path / "one.db")
    assert a is b is get_group_show_store()
    a._conn.close()   # noqa: SLF001


def test_get_store_never_returns_none_even_when_the_db_is_broken(tmp_path):
    """取单例永不返回 None——调用方不必到处判空，落库这种旁路能力不该污染业务代码。"""
    st = get_group_show_store(tmp_path)       # 目录路径 → 建库必失败
    assert isinstance(st, GroupShowStore)
    assert st.available is False
    st.save_session(_state())                 # 照常调用，静默空转
    assert st.events("s1") == []


def test_reset_lets_the_next_call_point_at_a_new_db(tmp_path):
    """reset 之后必须能指向新库——测试之间不能互相看见对方的数据。"""
    first = get_group_show_store(tmp_path / "a.db")
    first.save_session(_state("only_in_a"))
    first._conn.close()      # noqa: SLF001
    reset_group_show_store()

    second = get_group_show_store(tmp_path / "b.db")
    try:
        assert second is not first
        assert second.load_session("only_in_a") is None
    finally:
        second._conn.close()   # noqa: SLF001


def test_default_db_path_stays_inside_config(tmp_path):
    """默认库路径必须留在 config/ 下——散落到别处会在双实例部署下串库。"""
    assert DEFAULT_DB_PATH.startswith("config/")


def test_reset_also_restores_the_default_path(tmp_path):
    """``reset`` 要把路径也归位，否则「重置」只重置了一半。

    残留的自定义路径会让下一次无参 ``get_group_show_store()`` 悄悄建到上一个实例
    的库上——这正是双实例部署里最难查的那种串库。
    """
    get_group_show_store(tmp_path / "custom.db")._conn.close()   # noqa: SLF001
    reset_group_show_store()
    from src.companion.group_show import store as store_mod
    assert store_mod._DB_PATH == DEFAULT_DB_PATH       # noqa: SLF001


def test_a_second_configure_with_a_different_path_actually_switches_db(tmp_path):
    """换路径 configure 必须真换库（这里曾经是个只咬人不出声的陷阱）。

    旧语义是「已建就返回既有实例」，于是第二次传新路径只改了模块级 ``_DB_PATH``、
    实例还绑在旧库：双实例部署里第二个实例会静默写进第一个实例的库，隔离失效且零报错。
    2026-07-26 真被咬到——导播台用例先把单例装到仓库根 ``config/group_show.db``，
    路由随后带实例 tmp 路径来 configure 拿到的仍是那一个，假场次写进了仓库库、
    下一轮 CI 起手就假红。显式 configure 是**声明意图**，路径变了就该换过去。
    """
    first = configure_group_show_store(tmp_path / "first.db")
    first.save_session(_state("before_switch"))
    second = configure_group_show_store(tmp_path / "second.db")
    assert second is not first, "换路径必须重建实例，不能继续绑旧库"
    assert not first.available, "旧实例应被关闭并转入空转，避免半死连接乱写"

    second.save_session(_state("after_switch"))
    second._conn.close()      # noqa: SLF001

    probe_first = GroupShowStore(tmp_path / "first.db")
    probe_second = GroupShowStore(tmp_path / "second.db")
    try:
        assert probe_first.load_session("before_switch") is not None
        assert probe_first.load_session("after_switch") is None
        assert probe_second.load_session("after_switch") is not None
    finally:
        probe_first._conn.close(); probe_second._conn.close()   # noqa: SLF001


def test_repeating_configure_with_the_same_path_is_still_idempotent(tmp_path):
    """同路径重复 configure 仍是纯幂等——启动期被调两次不该白白重连一次库。"""
    a = configure_group_show_store(tmp_path / "same.db")
    b = configure_group_show_store(tmp_path / "same.db")
    assert b is a and a.available
    a._conn.close()           # noqa: SLF001


# ── 补充：边界字段、内存库与端到端续演 ─────────────────────────────────────


def test_a_negative_beat_cursor_is_clamped_to_zero(store):
    """脏游标必须在入口夹住——负游标写进库会让续演从一个不存在的位置开始。

    ``ShowState.beat_cursor`` 是普通可写字段，运行期任何一处算错（比如出队逻辑被
    改坏）都会把负数带到这里；夹在入口，坏值最多损失精度而不会让整场戏读不出来。
    """
    store.save_session(_state(beat_cursor=-7))
    assert store.load_session("s1")["beat_cursor"] == 0


def test_dry_run_round_trips_in_both_directions(store):
    """**安全字段，两个方向都要验**：排练场次被恢复成真发，就是一场本不该发出去的
    戏真的发了出去。

    只验一个方向是不够的——``bool(row["dry_run"])`` 这种写法在两个方向上的失败模式
    不同，而危险的那一侧（True → False）恰恰是默认值掩盖不了的。
    """
    store.save_session(_state("rehearsal", dry_run=True))
    store.save_session(_state("live", dry_run=False))
    assert store.load_session("rehearsal")["dry_run"] is True
    assert store.load_session("live")["dry_run"] is False


def test_a_blank_event_kind_falls_back_to_line(store):
    """kind 缺失时回落 ``line``——留空会让自然度评测与刷屏闸都漏算这一条。"""
    store.append_event("s1", ShowEvent(seq=1, ts=1.0, speaker_account="a",
                                       role="r", beat_id="b", text="x", kind=""))
    assert store.events("s1")[0]["kind"] == "line"


def test_events_of_an_unknown_session_is_an_empty_list_not_none(store):
    """返回 ``[]`` 而不是 ``None``：调用方能直接 for 循环，少一处判空即少一处崩。"""
    assert store.events("从来没有过的场次") == []


def test_recent_sessions_carry_the_restored_casting_object(store):
    """看板列表要直接显示「这场谁演的」，所以列表项就得带还原好的 ``Casting``。

    只给 ``cast_json`` 会逼看板自己解码，那份解码逻辑迟早与本模块的语义分叉。
    """
    store.save_session(_state())
    row = store.recent_sessions()[0]
    assert row["casting"].by_slot("advocate").display_name == "小美"
    assert row["cast"]["unfilled"] == ["bystander"]


def test_casting_json_stays_human_readable(store):
    """显示名是中文，转义成 ``\\uXXXX`` 会让 DB 里这一列没法肉眼复盘。"""
    assert "小美" in casting_to_json(_casting())


def test_an_in_memory_store_skips_the_directory_dance():
    """``:memory:`` 是一条独立分支（不建目录、不开 WAL），临时/测试用途要能走通。"""
    st = GroupShowStore(":memory:")
    try:
        assert st.available is True
        st.save_session(_state())
        assert st.load_session("s1") is not None
        assert len(st.events("s1")) == 3
    finally:
        st._conn.close()      # noqa: SLF001


# ── 出席台账 ────────────────────────────────────────────────────────────────


def test_membership_roundtrip_feeds_the_planner(store):
    """记一笔 → 读出来就是 ``plan_attendance`` 要的 ``{群: [号]}``。"""
    store.record_membership("tg:-1001", "a01")
    store.record_membership("tg:-1001", "a02")
    store.record_membership("tg:-1002", "a01")
    assert store.memberships() == {"tg:-1001": ["a01", "a02"], "tg:-1002": ["a01"]}


def test_recording_the_same_join_twice_does_not_reset_the_first_time(store):
    """重复点「已加入」是常态（页面刷新、误触）。

    首次进群的时间是**事实**，不该被今天的一次误点刷掉——将来要靠它判断
    「这个号最近加群加得急不急」。
    """
    store.record_membership("tg:-1001", "a01", joined_at=1000.0)
    store.record_membership("tg:-1001", "a01", joined_at=9999.0)
    with store._lock:                    # noqa: SLF001 —— 直查列，公开 API 不暴露时间
        rows = store._conn.execute(      # noqa: SLF001
            "SELECT joined_at FROM choreography_memberships").fetchall()
    assert len(rows) == 1
    assert rows[0]["joined_at"] == 1000.0


def test_forgetting_a_membership_only_removes_our_bookkeeping(store):
    """撤销＝删账本行。这里没有任何退群动作——退群有痕迹且消不掉历史。"""
    store.record_membership("tg:-1001", "a01")
    store.record_membership("tg:-1001", "a02")
    store.forget_membership("tg:-1001", "a01")
    assert store.memberships() == {"tg:-1001": ["a02"]}


def test_memberships_are_scoped_per_platform(store):
    """同一个 ``group_key`` 在两个平台上是两个群，不能混进一张表。"""
    store.record_membership("g1", "a01", platform="telegram")
    store.record_membership("g1", "wa01", platform="whatsapp")
    assert store.memberships() == {"g1": ["a01"]}
    assert store.memberships(platform="whatsapp") == {"g1": ["wa01"]}


@pytest.mark.parametrize("group_key,account", [
    ("", "a01"), ("g1", ""), (None, "a01"), ("g1", None), ("  ", "a01"),
])
def test_blank_membership_keys_write_nothing(store, group_key, account):
    """空键不入账（前端传空、脚本拼错都可能来）——空群/空号的账本行毫无意义。"""
    store.record_membership(group_key, account)
    assert store.memberships() == {}


def test_membership_calls_on_a_dead_store_are_silent_no_ops(tmp_path):
    """库不可用时台账跟其它方法一样空转：控制台按钮点了没反应，但不报错。"""
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory", encoding="utf-8")
    st = GroupShowStore(blocker / "gs.db")
    assert st.available is False
    st.record_membership("g1", "a01")
    st.forget_membership("g1", "a01")
    assert st.memberships() == {}


def test_a_corrupt_membership_row_does_not_hide_the_rest(store):
    """一行脏数据不该让整本账消失——空白账本与「还没加过群」长得一模一样。"""
    store.record_membership("g1", "a01")
    with store._lock:                    # noqa: SLF001
        store._conn.execute(             # noqa: SLF001
            "INSERT INTO choreography_memberships "
            "(platform, group_key, account_id, joined_at, source) "
            "VALUES ('telegram', '', '', 0, 'manual')")
        store._conn.commit()             # noqa: SLF001
    assert store.memberships() == {"g1": ["a01"]}


def test_the_ledger_survives_a_reopen(store, tmp_path):
    """台账的全部价值在于**跨天**——进程重启后必须还在。"""
    store.record_membership("g1", "a01")
    path = store._db_path                # noqa: SLF001
    store._conn.close()                  # noqa: SLF001
    again = GroupShowStore(path)
    try:
        assert again.memberships() == {"g1": ["a01"]}
    finally:
        again._conn.close()              # noqa: SLF001


def test_an_old_db_without_the_ledger_table_gets_it_on_open(tmp_path):
    """老库（台账上线前建的）重开后必须自动补表，而不是每次调用都失败。"""
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    # 台账上线前的 schema：只有场次与事件两张表。
    conn.executescript("""
        CREATE TABLE choreography_sessions (
            session_id TEXT NOT NULL PRIMARY KEY,
            group_key TEXT NOT NULL DEFAULT '',
            platform TEXT NOT NULL DEFAULT 'telegram',
            playbook_id TEXT NOT NULL DEFAULT '',
            cast_json TEXT NOT NULL DEFAULT '{}',
            beat_cursor INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'pending',
            dry_run INTEGER NOT NULL DEFAULT 1,
            started_at REAL NOT NULL DEFAULT 0,
            ended_at REAL NOT NULL DEFAULT 0);
        CREATE TABLE choreography_events (
            session_id TEXT NOT NULL, seq INTEGER NOT NULL,
            ts REAL NOT NULL DEFAULT 0,
            speaker_account TEXT NOT NULL DEFAULT '',
            role TEXT NOT NULL DEFAULT '', beat_id TEXT NOT NULL DEFAULT '',
            text TEXT NOT NULL DEFAULT '', kind TEXT NOT NULL DEFAULT 'line',
            PRIMARY KEY (session_id, seq));
        INSERT INTO choreography_sessions (session_id) VALUES ('old_show');
    """)
    conn.commit()
    conn.close()

    st = GroupShowStore(path)
    try:
        assert st.available is True
        st.record_membership("g1", "a01")
        assert st.memberships() == {"g1": ["a01"]}
        assert st.load_session("old_show") is not None, "补表不该动老数据"
    finally:
        st._conn.close()                 # noqa: SLF001


def test_a_show_resumes_exactly_where_it_stopped(store):
    """**本模块存在的全部理由，端到端走一遍**：存 → 崩 → 读 → 接着演。

    上面的用例逐字段验了往返，这一条验的是那些字段**装回 ``ShowState`` 之后**仍然
    自洽：``current_beat`` 指向第 3 拍而不是回到开场，``next_seq`` 接着走而不是从 1
    重来（否则新事件会 upsert 覆盖掉已经说过的话）。这两个派生属性才是导演真正读的
    东西，只验列值验不到它们。
    """
    playbook = Playbook(id="pb_resume", name="续演", roles=(Role("advocate"),),
                        beats=tuple(Beat(id=f"b{i}", role="advocate",
                                         intent=f"意图{i}") for i in range(1, 5)))
    live = ShowState(session_id="s_resume", group_key="tg:-100777",
                     playbook=playbook, casting=_casting(), started_at=1700.0)
    for seq in (1, 2):
        live.append_event(ShowEvent(seq=seq, ts=1700.0 + seq, speaker_account="a1",
                                    role="advocate", beat_id=f"b{seq}",
                                    text=f"第{seq}句"))
    live.beat_cursor = 2
    live.status = "running"
    store.save_session(live)

    # ——— 进程在这里重启 ———
    row = store.load_session("s_resume")
    resumed = ShowState(
        session_id=row["session_id"], group_key=row["group_key"],
        playbook=playbook, casting=row["casting"], beat_cursor=row["beat_cursor"],
        status=row["status"], started_at=row["started_at"], dry_run=row["dry_run"],
        events=[ShowEvent(**{k: v for k, v in e.items() if k != "session_id"})
                for e in store.events("s_resume")],
    )

    assert resumed.current_beat.id == "b3", "必须接着第 3 拍演，而不是回到 b1"
    assert resumed.next_seq == 3, "序号要接着走，否则新事件会覆盖已说过的台词"
    assert [e.text for e in resumed.events] == ["第1句", "第2句"]
    assert resumed.casting.by_slot("advocate").account_id == "a1"
    assert resumed.status == "running"


# ── 角色台账与开口时刻（第三、第四条可检测轴的数据源）─────────────────────────


def _spoke(store, sid, group, acct, role, *, ts=1000.0, dry_run=False, kind="line"):
    """记一条「某号在某群以某角色开了口」——三条轴共用的最小事实。"""
    state = ShowState(session_id=sid, group_key=group, playbook=_playbook(),
                      casting=_casting(), dry_run=dry_run, started_at=ts)
    state.append_event(ShowEvent(seq=1, ts=ts, speaker_account=acct, role=role,
                                 beat_id="b1", text="hi", kind=kind))
    store.save_session(state)


def test_role_ledger_counts_distinct_groups_not_lines(store):
    """同一个群里说十句还是一句，对外看都只是「这个号在这个群当过推手」。

    按条数计会让「话痨号」被误判成高风险，而真正的把柄是**跨群的角色重复**。
    """
    _spoke(store, "s1", "g1", "a", "advocate", ts=1000.0)
    _spoke(store, "s2", "g1", "a", "advocate", ts=1100.0)
    _spoke(store, "s3", "g2", "a", "advocate", ts=1200.0)
    assert store.role_ledger() == {("a", "advocate"): 2}


def test_role_ledger_separates_slots_for_the_same_account(store):
    """同一个号在这群当推手、在那群当路人＝正常轮换，两个格子要分开记才看得出来。"""
    _spoke(store, "s1", "g1", "a", "advocate")
    _spoke(store, "s2", "g2", "a", "bystander")
    assert store.role_ledger() == {("a", "advocate"): 1, ("a", "bystander"): 1}


def test_role_ledger_applies_the_same_two_filters_as_the_speech_ledger(store):
    """排练不算暴露、真人插话不是我们的号——两条轴的口径必须完全一致。

    口径不一致会出现「演出矩阵说安全、角色矩阵说危险」这种自相矛盾的看板。
    """
    _spoke(store, "s1", "g1", "a", "advocate", dry_run=True)
    _spoke(store, "s2", "g2", "real_person", "advocate", kind="human")
    _spoke(store, "s3", "g3", "director", "advocate", kind="yield")
    _spoke(store, "s4", "g4", "a", "advocate")
    assert store.role_ledger() == {("a", "advocate"): 1}


def test_role_ledger_window_lets_old_typecasting_fade(store):
    """半年前一直演推手不该压着今天的排班——角色集中度和共现一样要随时间淡出。"""
    _spoke(store, "old", "g1", "a", "advocate", ts=1000.0)
    _spoke(store, "new", "g2", "a", "advocate", ts=9000.0)
    assert store.role_ledger() == {("a", "advocate"): 2}
    assert store.role_ledger(since=5000.0) == {("a", "advocate"): 1}


def test_role_ledger_is_empty_when_the_store_is_degraded(store):
    store._conn = None                   # noqa: SLF001 —— 模拟建库失败的降级实例
    assert store.role_ledger() == {}


def test_prior_slots_pins_each_account_to_its_latest_role_in_that_group(store):
    """群内角色粘性的台账：每个号取**这个群**里最近一次的角色，别的群不掺和。

    2026-07-27 双号灰度实录：两场之间 advocate/skeptic 恰好被主推轮转对调——同一批
    观众看着上一场泼冷水的号这一场安利。选角要能问到「它在这个群上次演的谁」。
    """
    _spoke(store, "s1", "g1", "a", "skeptic", ts=1000.0)
    _spoke(store, "s2", "g1", "a", "advocate", ts=2000.0)    # 本群最近一次
    _spoke(store, "s3", "g2", "a", "bystander", ts=9000.0)   # 别的群，不掺和
    _spoke(store, "s4", "g1", "b", "skeptic", ts=1500.0)
    assert store.prior_slots(group_key="g1") == {"a": "advocate", "b": "skeptic"}


def test_prior_slots_applies_the_same_filters_as_every_other_ledger(store):
    """排练不算暴露、真人插话不是我们的号——口径与 role_ledger 一字不差。

    排练算进去的话，一次 dry-run 就能把线上角色「钉」在一个从没对观众亮过相的槽上。
    """
    _spoke(store, "s1", "g1", "a", "advocate", dry_run=True, ts=9000.0)
    _spoke(store, "s2", "g1", "real_person", "advocate", kind="human", ts=9000.0)
    _spoke(store, "s3", "g1", "a", "skeptic", ts=1000.0)
    assert store.prior_slots(group_key="g1") == {"a": "skeptic"}


def test_prior_slots_needs_a_group_and_survives_degradation(store):
    """没有目标群＝没有粘性语义（空表）；降级库同样空表，绝不抛。"""
    _spoke(store, "s1", "g1", "a", "advocate")
    assert store.prior_slots(group_key="") == {}
    store._conn = None                   # noqa: SLF001
    assert store.prior_slots(group_key="g1") == {}


def test_last_spoke_at_takes_the_newest_moment_per_account(store):
    """闸门问的是「离上次开口过了多久」，所以只有最新那一刻有意义。"""
    _spoke(store, "s1", "g1", "a", "advocate", ts=1000.0)
    _spoke(store, "s2", "g2", "a", "bystander", ts=5000.0)
    _spoke(store, "s3", "g1", "b", "advocate", ts=2000.0)
    assert store.last_spoke_at() == {"a": 5000.0, "b": 2000.0}


def test_last_spoke_at_falls_back_to_the_session_start(store):
    """事件没记时刻的旧数据若按 0 处理，这个号会被判成「1970 年说过话」而永远畅通。"""
    state = ShowState(session_id="s1", group_key="g1", playbook=_playbook(),
                      casting=_casting(), dry_run=False, started_at=9000.0)
    state.append_event(ShowEvent(seq=1, ts=0.0, speaker_account="a", role="advocate",
                                 beat_id="b1", text="hi", kind="line"))
    store.save_session(state)
    assert store.last_spoke_at() == {"a": 9000.0}


def test_last_spoke_at_ignores_rehearsals_and_real_humans(store):
    """排练没发出去、群友插话不是我们的号——都不该占用同号跨群的冷却窗。"""
    _spoke(store, "s1", "g1", "a", "advocate", ts=9000.0, dry_run=True)
    _spoke(store, "s2", "g2", "real_person", "advocate", ts=9000.0, kind="human")
    _spoke(store, "s3", "g3", "a", "advocate", ts=1000.0)
    assert store.last_spoke_at() == {"a": 1000.0}


def test_last_spoke_at_is_empty_when_the_store_is_degraded(store):
    store._conn = None                   # noqa: SLF001
    assert store.last_spoke_at() == {}


def test_last_spoke_at_can_leave_the_target_group_out(store):
    """闸门问的是「有没有在**别的**群刚冒过头」。

    同一个群里连着演两拍是这场戏本身，不是跨群痕迹。不排除本群的话，一个刚在本群
    自动回复过的号会被自己挡住——这条拦截没有任何风险意义，只会让运营把闸门关掉。
    """
    _spoke(store, "s1", "g1", "a", "advocate", ts=9000.0)
    _spoke(store, "s2", "g2", "a", "advocate", ts=1000.0)
    assert store.last_spoke_at() == {"a": 9000.0}
    assert store.last_spoke_at(exclude_group="g1") == {"a": 1000.0}
    # 这个号只在本群说过话 → 排除之后就该彻底消失（＝放行），而不是留个 0
    _spoke(store, "s3", "g1", "b", "advocate", ts=9000.0)
    assert "b" not in store.last_spoke_at(exclude_group="g1")
