"""GroupMembersStore 单元测试（成员去重 / 过滤查询 / 每日配额计数 / 任务状态机）。"""
import time

from src.companion.group_members_store import (
    FILTER_SPOKE_NO_ADMIN,
    JOB_DONE,
    JOB_RUNNING,
    GroupMembersStore,
)


def _mk(**kw):
    row = {
        "group_id": "-100999", "user_id": "1", "username": "u", "first_name": "F",
        "last_name": "", "is_admin": False, "is_bot": False, "spoke": True,
        "last_spoke_ts": 0.0, "group_title": "G", "source_account_id": "accA",
        "job_id": "j1", "batch_id": "b1", "outreach_state": "none",
        "extracted_at": time.time(),
    }
    row.update(kw)
    return row


def test_record_members_dedup_by_group_user():
    st = GroupMembersStore(":memory:")
    ins, skip = st.record_members([_mk(user_id="1"), _mk(user_id="2")])
    assert (ins, skip) == (2, 0)
    # 同 (group_id, user_id) 再插 → 命中已存在，不重复
    ins2, skip2 = st.record_members([_mk(user_id="1"), _mk(user_id="3")])
    assert (ins2, skip2) == (1, 1)
    assert st.count_members("-100999") == 3


def test_record_members_skips_incomplete_rows():
    st = GroupMembersStore(":memory:")
    ins, skip = st.record_members([_mk(user_id=""), {"group_id": "", "user_id": "9"}])
    assert ins == 0


def test_list_and_count_filters():
    st = GroupMembersStore(":memory:")
    st.record_members([
        _mk(user_id="1", spoke=True, is_admin=False),   # 发言 非管理员
        _mk(user_id="2", spoke=True, is_admin=True),    # 发言 管理员
        _mk(user_id="3", spoke=False, is_admin=False),  # 没发言
    ])
    assert st.count_members("-100999") == 3
    assert st.count_members("-100999", only="spoke") == 2
    assert st.count_members("-100999", only="spoke_no_admin") == 1
    assert st.count_members("-100999", only="admins") == 1
    got = {m["user_id"] for m in st.list_members("-100999", only="spoke_no_admin")}
    assert got == {"1"}


def test_list_members_query_matches_name_or_username():
    st = GroupMembersStore(":memory:")
    st.record_members([
        _mk(user_id="1", username="alice", first_name="Alice"),
        _mk(user_id="2", username="bob", first_name="Bob"),
    ])
    got = {m["user_id"] for m in st.list_members("-100999", q="ali")}
    assert got == {"1"}


def test_list_members_sort_by_score_vs_recent():
    st = GroupMembersStore(":memory:")
    st.record_members([
        _mk(user_id="1", score=90, last_spoke_ts=1.0),
        _mk(user_id="2", score=50, last_spoke_ts=100.0),
        _mk(user_id="3", score=70, last_spoke_ts=50.0),
    ])
    by_score = [m["user_id"] for m in st.list_members("-100999", sort="score")]
    assert by_score == ["1", "3", "2"]        # 可聊度 90 > 70 > 50
    by_recent = [m["user_id"] for m in st.list_members("-100999", sort="recent")]
    assert by_recent == ["2", "3", "1"]       # 近发言 100 > 50 > 1（默认）


def test_count_extracted_since_is_per_account_daily_window():
    st = GroupMembersStore(":memory:")
    now = time.time()
    st.record_members([
        _mk(user_id="1", source_account_id="accA", extracted_at=now),
        _mk(user_id="2", source_account_id="accA", extracted_at=now),
        _mk(user_id="3", source_account_id="accB", extracted_at=now),
        _mk(user_id="4", source_account_id="accA", extracted_at=now - 3 * 86400),
    ])
    since = now - 86400
    assert st.count_extracted_since("accA", since) == 2   # 昨天那条不算
    assert st.count_extracted_since("accB", since) == 1
    assert st.count_extracted_since("accX", since) == 0


def test_job_crud_and_counters_and_stop():
    st = GroupMembersStore(":memory:")
    job = st.create_job(group_id="-100999", account_ids=["accA", "accB"],
                        filter=FILTER_SPOKE_NO_ADMIN, daily_cap_per_account=50,
                        scan_limit=1000, created_by="tester")
    jid = job["job_id"]
    assert job["status"] == JOB_RUNNING
    assert job["account_ids"] == ["accA", "accB"]
    assert job["filter"] == FILTER_SPOKE_NO_ADMIN

    st.bump_job_counters(jid, pulled=10, dedup=3, admins=2, floodwaits=1)
    st.bump_job_counters(jid, pulled=5)
    got = st.get_job(jid)
    assert got["pulled_total"] == 15 and got["dedup_skipped"] == 3
    assert got["admins_excluded"] == 2 and got["floodwaits"] == 1

    assert st.is_stop_requested(jid) is False
    st.request_stop(jid)
    assert st.is_stop_requested(jid) is True

    st.update_job(jid, status=JOB_DONE, finished_at=time.time())
    assert st.get_job(jid)["status"] == JOB_DONE
    # 非白名单字段不可改
    st.update_job(jid, pulled_total=99999)
    assert st.get_job(jid)["pulled_total"] == 15


def test_migrate_score_onto_preexisting_db_without_score(tmp_path):
    """回归：旧库（无 score 列）打开新 store 不得崩（2026-08-12 生产事故）。

    根因＝依赖 score 的索引曾放在 _DDL，executescript 对旧表引用未迁入的列 →
    `no such column: score` → 建库抛异常 → store=None → 功能静默不可用。
    此测试用真实**文件** DB（非 :memory:，那是全新表 DDL 自带 score 才漏掉本路径）。
    """
    import sqlite3

    db = str(tmp_path / "gm_old.db")
    conn = sqlite3.connect(db)
    # 模拟 P0/旧结构：tg_group_members 没有 score 列
    conn.execute(
        "CREATE TABLE tg_group_members ("
        " group_id TEXT NOT NULL, user_id TEXT NOT NULL,"
        " username TEXT NOT NULL DEFAULT '', first_name TEXT NOT NULL DEFAULT '',"
        " last_name TEXT NOT NULL DEFAULT '', is_admin INTEGER NOT NULL DEFAULT 0,"
        " is_bot INTEGER NOT NULL DEFAULT 0, spoke INTEGER NOT NULL DEFAULT 0,"
        " last_spoke_ts REAL NOT NULL DEFAULT 0, group_title TEXT NOT NULL DEFAULT '',"
        " source_account_id TEXT NOT NULL DEFAULT '', job_id TEXT NOT NULL DEFAULT '',"
        " batch_id TEXT NOT NULL DEFAULT '', outreach_state TEXT NOT NULL DEFAULT 'none',"
        " extracted_at REAL NOT NULL DEFAULT 0, PRIMARY KEY (group_id, user_id))")
    conn.commit()
    conn.close()

    # 新 store 打开旧库：不得抛（旧 bug 会在 __init__ 崩），且迁移补上 score + 建索引
    st = GroupMembersStore(db)
    st.record_members([_mk(user_id="1", score=90), _mk(user_id="2", score=40)])
    ordered = [m["user_id"] for m in st.list_members("-100999", sort="score")]
    assert ordered == ["1", "2"]                       # 按分排序可用 = score 列真的迁进来了


def test_count_extracted_group_and_global_windows():
    st = GroupMembersStore(":memory:")
    now = time.time()
    st.record_members([
        _mk(group_id="-1", user_id="1", extracted_at=now),
        _mk(group_id="-1", user_id="2", extracted_at=now),
        _mk(group_id="-2", user_id="3", extracted_at=now),
        _mk(group_id="-1", user_id="4", extracted_at=now - 3 * 86400),
    ])
    since = now - 86400
    assert st.count_extracted_for_group_since("-1", since) == 2   # 昨天那条不算
    assert st.count_extracted_for_group_since("-2", since) == 1
    assert st.count_extracted_all_since(since) == 3


def test_create_job_persists_cap_fields():
    st = GroupMembersStore(":memory:")
    job = st.create_job(group_id="-1", account_ids=["accA"],
                        group_daily_cap=500, global_daily_cap=2000)
    got = st.get_job(job["job_id"])
    assert got["group_daily_cap"] == 500 and got["global_daily_cap"] == 2000


def test_create_job_persists_shard_fields():
    st = GroupMembersStore(":memory:")
    job = st.create_job(group_id="-1", account_ids=["accB"],
                        filter=FILTER_SPOKE_NO_ADMIN, shard_index=1, num_shards=3,
                        batch_ref="gmbatch_x")
    got = st.get_job(job["job_id"])
    assert got["shard_index"] == 1 and got["num_shards"] == 3
    assert got["batch_ref"] == "gmbatch_x"


def test_group_summaries_and_stats():
    st = GroupMembersStore(":memory:")
    assert st.stats()["active"] is False
    st.record_members([
        _mk(group_id="-1", user_id="1", spoke=True, is_admin=True, group_title="A"),
        _mk(group_id="-1", user_id="2", spoke=True, is_admin=False, group_title="A"),
        _mk(group_id="-2", user_id="3", spoke=False, is_admin=False, group_title="B"),
    ])
    sums = {s["group_id"]: s for s in st.group_summaries()}
    assert sums["-1"]["total"] == 2 and sums["-1"]["spoke"] == 2 and sums["-1"]["admins"] == 1
    assert sums["-2"]["total"] == 1
    stats = st.stats()
    assert stats["active"] is True and stats["members_total"] == 3 and stats["groups"] == 2
