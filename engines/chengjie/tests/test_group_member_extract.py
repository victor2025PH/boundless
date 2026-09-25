"""group_member_extract 测试：纯函数过滤/分片/归一化 + 异步提取编排（fake client）。"""
import time

import pytest

from src.companion.group_member_extract import (
    list_account_groups,
    local_midnight_ts,
    member_in_shard,
    run_extraction,
    score_member,
    should_keep,
    user_row,
)
from src.companion.group_members_store import (
    FILTER_ALL,
    FILTER_SPOKE,
    FILTER_SPOKE_NO_ADMIN,
    JOB_DONE,
    JOB_ERROR,
    GroupMembersStore,
)


# ── 纯函数 ────────────────────────────────────────────────────────────────

def test_should_keep_matrix():
    # spoke_no_admin：只留「发言 且 非管理员 且 非bot 非自己」
    assert should_keep(filter_mode=FILTER_SPOKE_NO_ADMIN, spoke=True,
                        is_admin=False, is_bot=False, is_self=False) is True
    assert should_keep(filter_mode=FILTER_SPOKE_NO_ADMIN, spoke=True,
                        is_admin=True, is_bot=False, is_self=False) is False
    assert should_keep(filter_mode=FILTER_SPOKE_NO_ADMIN, spoke=False,
                        is_admin=False, is_bot=False, is_self=False) is False
    # bot / 自己 / 注销 恒剔除（即便 all）
    assert should_keep(filter_mode=FILTER_ALL, spoke=True, is_admin=False,
                       is_bot=True, is_self=False) is False
    assert should_keep(filter_mode=FILTER_ALL, spoke=True, is_admin=False,
                       is_bot=False, is_self=True) is False
    assert should_keep(filter_mode=FILTER_ALL, spoke=False, is_admin=True,
                       is_bot=False, is_self=False, is_deleted=False) is True
    # spoke：管理员只要发言也留
    assert should_keep(filter_mode=FILTER_SPOKE, spoke=True, is_admin=True,
                       is_bot=False, is_self=False) is True


def test_member_in_shard():
    assert member_in_shard(100, 0, 1) is True          # 单号恒 True
    assert member_in_shard(100, 0, 2) is True           # 100 % 2 == 0
    assert member_in_shard(101, 0, 2) is False
    assert member_in_shard(101, 1, 2) is True
    assert member_in_shard("not-int", 0, 3) is True     # 不可解析 → 第 0 片
    assert member_in_shard("not-int", 1, 3) is False


def test_local_midnight_ts_is_past_midnight():
    now = time.time()
    mid = local_midnight_ts(now)
    assert mid <= now
    assert (now - mid) < 86400 + 3600  # 容夏令时


class _U:
    def __init__(self, id, username="", first_name="", last_name="", is_bot=False):
        self.id = id
        self.username = username
        self.first_name = first_name
        self.last_name = last_name
        self.is_bot = is_bot


def test_user_row_normalization():
    r = user_row(_U(42, "neo", "Neo", is_bot=False), group_id="-1", group_title="G",
                 spoke=True, is_admin=True, source_account_id="accA", job_id="j",
                 batch_id="b", now=123.0)
    assert r["user_id"] == "42" and r["username"] == "neo"
    assert r["is_admin"] is True and r["spoke"] is True
    assert r["last_spoke_ts"] == 123.0 and r["extracted_at"] == 123.0


def test_score_member_ranking():
    full = score_member(spoke=True, is_admin=False, is_bot=False,
                        username="u", first_name="F", last_name="L")   # 40+25+15+10+5
    admin = score_member(spoke=True, is_admin=True, is_bot=False,
                         username="u", first_name="F", last_name="L")  # 95-20
    bare = score_member(spoke=True, is_admin=False, is_bot=False)      # 40+25
    silent = score_member(spoke=False, is_admin=False, is_bot=False, username="u")  # 40+15
    bot = score_member(spoke=True, is_admin=False, is_bot=True, username="u")       # 0
    assert (full, admin, bare, silent, bot) == (95, 75, 65, 55, 0)
    assert full > admin > bare > silent > bot   # 可聊度：全档发言人 > 管理员 > 裸发言 > 沉默 > bot


# ── 异步编排（fake client）───────────────────────────────────────────────

class FloodWait(Exception):
    """名字必须是 FloodWait —— _is_floodwait 按类名判定。"""
    def __init__(self, value=1):
        super().__init__("flood")
        self.value = value


class _Member:
    def __init__(self, user):
        self.user = user


class _Msg:
    def __init__(self, from_user):
        self.from_user = from_user


class _Chat:
    def __init__(self, id, title):
        self.id = id
        self.title = title


class FakeClient:
    def __init__(self, *, chat, admins=(), history=(), members=(), fw_history=0):
        self.loop = None
        self.me = None
        self._chat = chat
        self._admins = list(admins)
        self._history = list(history)
        self._members = list(members)
        self._fw_history = fw_history

    async def get_chat(self, peer):
        return self._chat

    async def get_chat_members(self, chat_id, filter=None):
        # 有 filter（管理员枚举）→ 出 admins；无 filter（全员枚举）→ 出 members
        src = self._admins if filter is not None else self._members
        for u in src:
            yield _Member(u)

    async def get_chat_history(self, chat_id, limit=0):
        if self._fw_history > 0:
            self._fw_history -= 1
            raise FloodWait(1)
        for m in (self._history[:limit] if limit else self._history):
            yield m


async def _noop_sleep(_):
    return None


async def test_run_extraction_spoke_no_admin_excludes_admin_bot_self():
    st = GroupMembersStore(":memory:")
    u1 = _U(101, "a1", "A1")
    u2 = _U(102, "adm", "Adm")           # 管理员
    u3 = _U(103, "botx", is_bot=True)    # bot
    u4 = _U(104, "self")                 # 自己
    u5 = _U(105, "a5", "A5")
    client = FakeClient(
        chat=_Chat(-100999, "MyGroup"),
        admins=[u2],
        history=[_Msg(u1), _Msg(u2), _Msg(u3), _Msg(u5), _Msg(u4)],
    )
    job = st.create_job(group_id="-100999", account_ids=["accA"],
                        filter=FILTER_SPOKE_NO_ADMIN, daily_cap_per_account=100,
                        scan_limit=500)
    res = await run_extraction(client, st, job["job_id"], self_id=104,
                               sleep=_noop_sleep)
    assert res["ok"] is True
    ids = {m["user_id"] for m in st.list_members("-100999")}
    assert ids == {"101", "105"}       # 管理员/ bot / 自己 全剔除
    assert res["inserted"] == 2
    assert res["admins_excluded"] == 1
    j = st.get_job(job["job_id"])
    assert j["status"] == JOB_DONE
    assert j["group_title"] == "MyGroup"
    assert j["group_id"] == "-100999"  # 规范化为 chat.id
    # 入库即带可聊度分：u1(101) 发言+用户名 a1+名 A1 → 40+25+15+10 = 90
    m101 = [m for m in st.list_members("-100999") if m["user_id"] == "101"][0]
    assert m101["score"] == 90


async def test_run_extraction_respects_daily_cap():
    st = GroupMembersStore(":memory:")
    users = [_U(200 + i, "u%d" % i) for i in range(5)]
    client = FakeClient(chat=_Chat(-1, "G"), admins=[],
                        history=[_Msg(u) for u in users])
    job = st.create_job(group_id="-1", account_ids=["accA"],
                        filter=FILTER_SPOKE, daily_cap_per_account=2, scan_limit=500)
    res = await run_extraction(client, st, job["job_id"], sleep=_noop_sleep)
    assert res["ok"] is True
    assert st.count_members("-1") == 2          # 配额封顶 2
    # 再跑一次：今日配额已用满 → 直接 daily_cap_reached，不再新增
    job2 = st.create_job(group_id="-1", account_ids=["accA"],
                         filter=FILTER_SPOKE, daily_cap_per_account=2, scan_limit=500)
    res2 = await run_extraction(client, st, job2["job_id"], sleep=_noop_sleep)
    assert res2["reason"] == "daily_cap_reached"
    assert st.count_members("-1") == 2


async def test_run_extraction_floodwait_exhausted_marks_error():
    st = GroupMembersStore(":memory:")
    client = FakeClient(chat=_Chat(-1, "G"), admins=[],
                        history=[_Msg(_U(1, "x"))], fw_history=99)
    job = st.create_job(group_id="-1", account_ids=["accA"],
                        filter=FILTER_SPOKE, daily_cap_per_account=100, scan_limit=500)
    res = await run_extraction(client, st, job["job_id"], sleep=_noop_sleep)
    assert res["ok"] is False
    assert res["reason"] == "floodwait_exhausted"
    j = st.get_job(job["job_id"])
    assert j["status"] == JOB_ERROR
    assert j["floodwaits"] >= 1


async def test_run_extraction_multi_account_shards_are_disjoint():
    """多号并行：号 A(shard0/2) 与号 B(shard1/2) 按 user_id%2 各拉不同批次，互不重叠。"""
    st = GroupMembersStore(":memory:")
    users = [_U(100 + i, "u%d" % i) for i in range(6)]  # id 100..105
    client = FakeClient(chat=_Chat(-1, "G"), admins=[],
                        history=[_Msg(u) for u in users])
    job = st.create_job(group_id="-1", account_ids=["A", "B"],
                        filter=FILTER_SPOKE, daily_cap_per_account=100, scan_limit=500)
    jid = job["job_id"]
    ra = await run_extraction(client, st, jid, account="A", shard_index=0,
                              num_shards=2, sleep=_noop_sleep)
    rb = await run_extraction(client, st, jid, account="B", shard_index=1,
                              num_shards=2, sleep=_noop_sleep)
    assert ra["ok"] and rb["ok"]
    rows = st.list_members("-1")
    by_src = {}
    for m in rows:
        by_src.setdefault(m["source_account_id"], set()).add(m["user_id"])
    assert by_src.get("A") == {"100", "102", "104"}   # 偶数 id 归 shard0=A
    assert by_src.get("B") == {"101", "103", "105"}   # 奇数 id 归 shard1=B
    assert len(rows) == 6                               # 6 人全入库、零重叠


async def test_run_extraction_respects_group_cap():
    st = GroupMembersStore(":memory:")
    users = [_U(300 + i, "g%d" % i) for i in range(6)]
    client = FakeClient(chat=_Chat(-7, "G"), admins=[], history=[_Msg(u) for u in users])
    job = st.create_job(group_id="-7", account_ids=["A"], filter=FILTER_SPOKE,
                        daily_cap_per_account=100, scan_limit=500, group_daily_cap=2)
    res = await run_extraction(client, st, job["job_id"], account="A",
                               shard_index=0, num_shards=1, sleep=_noop_sleep)
    assert res["ok"] and st.count_members("-7") == 2   # 每号额度高，但群配额封顶 2
    # 同群再来一单：群配额今日已满 → 0 新增
    job2 = st.create_job(group_id="-7", account_ids=["A"], filter=FILTER_SPOKE,
                         daily_cap_per_account=100, scan_limit=500, group_daily_cap=2)
    res2 = await run_extraction(client, st, job2["job_id"], account="A",
                                shard_index=0, num_shards=1, sleep=_noop_sleep)
    assert res2["reason"] == "daily_cap_reached" and st.count_members("-7") == 2


async def test_group_cap_apportioned_across_two_accounts():
    st = GroupMembersStore(":memory:")
    users = [_U(400 + i, "h%d" % i) for i in range(8)]
    client = FakeClient(chat=_Chat(-8, "G"), admins=[], history=[_Msg(u) for u in users])
    job = st.create_job(group_id="-8", account_ids=["A", "B"], filter=FILTER_SPOKE,
                        daily_cap_per_account=100, scan_limit=500, group_daily_cap=2)
    ra = await run_extraction(client, st, job["job_id"], account="A",
                              shard_index=0, num_shards=2, sleep=_noop_sleep)
    rb = await run_extraction(client, st, job["job_id"], account="B",
                              shard_index=1, num_shards=2, sleep=_noop_sleep)
    assert ra["ok"] and rb["ok"]
    # 群配额 2 按 2 分片均摊（每号 ceil(rem/2)=1）→ 合计恰 2，不超群配额
    assert st.count_members("-8") == 2


async def test_run_extraction_respects_global_cap():
    st = GroupMembersStore(":memory:")
    users = [_U(500 + i, "k%d" % i) for i in range(5)]
    client = FakeClient(chat=_Chat(-9, "G"), admins=[], history=[_Msg(u) for u in users])
    job = st.create_job(group_id="-9", account_ids=["A"], filter=FILTER_SPOKE,
                        daily_cap_per_account=100, scan_limit=500, global_daily_cap=1)
    res = await run_extraction(client, st, job["job_id"], account="A",
                               shard_index=0, num_shards=1, sleep=_noop_sleep)
    assert res["ok"] and st.count_members("-9") == 1   # 全局配额封顶 1


async def test_list_account_groups_filters_to_groups_only():
    pytest.importorskip("pyrogram")  # CI 只装 requirements-ci.txt，不含 pyrogram
    from pyrogram.enums import ChatType

    class _C:
        def __init__(self, id, type, title="", members_count=None):
            self.id = id
            self.type = type
            self.title = title
            self.members_count = members_count

    class _D:
        def __init__(self, chat):
            self.chat = chat

    class FakeDlgClient:
        def __init__(self, dialogs):
            self._d = dialogs

        async def get_dialogs(self):
            for d in self._d:
                yield d

    dialogs = [
        _D(_C(-100, ChatType.SUPERGROUP, "Super", 500)),
        _D(_C(555, ChatType.PRIVATE, "Alice")),           # 私聊 → 剔
        _D(_C(-200, ChatType.GROUP, "Basic", 30)),
        _D(_C(-300, ChatType.CHANNEL, "Chan")),           # 广播频道 → 剔
    ]
    groups = await list_account_groups(FakeDlgClient(dialogs))
    assert [g["id"] for g in groups] == ["-100", "-200"]   # 仅群/超级群
    assert groups[0]["title"] == "Super" and groups[0]["members"] == 500
