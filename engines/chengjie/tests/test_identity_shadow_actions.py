"""身份影子人工处置门禁（dismiss / 证据包 / 多账号安全关联 / 记忆合流）。

钉住的语义（改 src/utils/identity_shadow_actions.py 必须先改这里）：
pair_key 字典序稳定、dismiss 持久化与过滤（保留 already_linked）、
memory_uids_for_peer 多账号分桶、peer_canonicals 裸键+后缀+`_rpa` 平台别名、
confirm_link_pair 把两侧全部账号分桶键+裸键链到同一 canonical，且传入
episodic_store 时把旧 canonical 的历史事实 merge_key 进共享 canonical
（P10：否则关联只对未来写入生效、旧记忆永远孤儿）。

重要：confirm_link_pair 只走人工确认路径，**绝不在扫描路径自动调用**
（identity_shadow / identity_shadow_scan / identity_shadow_periodic 均不导入本函数）。
"""

import sqlite3
from pathlib import Path

from src.utils.context_store import make_context_key
from src.utils.identity_shadow_actions import (
    build_pair_evidence,
    confirm_link_pair,
    dismiss_pair,
    filter_dismissed_pairs,
    is_dismissed,
    load_dismissed,
    memory_uids_for_peer,
    pair_key,
    peer_canonicals,
    save_dismissed,
)


# ── helpers ─────────────────────────────────────────────────────────────────

def _mk_inbox(path: Path, *, conversations=(), messages=()) -> Path:
    """最小 inbox.db：满足 collect_account_ids / fetch_message_snippets 查询。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE conversations (
          conversation_id TEXT PRIMARY KEY,
          platform TEXT,
          account_id TEXT,
          chat_key TEXT,
          display_name TEXT
        );
        CREATE TABLE messages (
          message_id TEXT PRIMARY KEY,
          conversation_id TEXT,
          direction TEXT,
          text TEXT,
          ts REAL,
          sender_name TEXT DEFAULT '',
          revoked INTEGER DEFAULT 0
        );
        """
    )
    for row in conversations:
        conn.execute(
            "INSERT INTO conversations "
            "(conversation_id, platform, account_id, chat_key, display_name) "
            "VALUES (?,?,?,?,?)",
            row,
        )
    for row in messages:
        # (message_id, conversation_id, direction, text, ts, sender_name?)
        if len(row) == 5:
            row = (*row, "")
        conn.execute(
            "INSERT INTO messages "
            "(message_id, conversation_id, direction, text, ts, sender_name) "
            "VALUES (?,?,?,?,?,?)",
            row,
        )
    conn.commit()
    conn.close()
    return path


class _FakeCPI:
    """最小 CPI：resolve/link 只记调用，不碰真实生产库。"""

    def __init__(self, canonical: str = "canon:test"):
        self.canonical = canonical
        self.resolve_calls = []
        self.link_calls = []

    def resolve(self, platform, uid):
        self.resolve_calls.append((platform, uid))
        return self.canonical

    def link(self, plat_a, uid_a, plat_b, uid_b):
        self.link_calls.append((plat_a, uid_a, plat_b, uid_b))
        return self.canonical


# ── 1. pair_key ─────────────────────────────────────────────────────────────

def test_pair_key_order_independent_and_empty_on_bad():
    k1 = pair_key("telegram", "111", "whatsapp", "639")
    k2 = pair_key("whatsapp", "639", "telegram", "111")
    assert k1 == k2 == "telegram:111|whatsapp:639"
    # 平台大小写归一
    assert pair_key("Telegram", "111", "WhatsApp", "639") == k1
    # 缺字段 → 空
    assert pair_key("", "111", "whatsapp", "639") == ""
    assert pair_key("telegram", "", "whatsapp", "639") == ""
    assert pair_key("telegram", "111", "", "639") == ""
    assert pair_key("telegram", "111", "whatsapp", "") == ""
    assert pair_key(None, "111", "whatsapp", "639") == ""


# ── 2. dismiss / filter ─────────────────────────────────────────────────────

def test_dismiss_is_dismissed_and_filter(tmp_path):
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    r = dismiss_pair(
        cfg, "telegram", "111", "whatsapp", "639",
        reason="not_same", now=1000.0,
    )
    assert r["ok"] is True
    pk = r["pair_key"]
    assert pk == pair_key("telegram", "111", "whatsapp", "639")

    st = load_dismissed(cfg / "identity_shadow_dismissed.json")
    assert is_dismissed(st, "whatsapp", "639", "telegram", "111") is True
    assert is_dismissed(st, "telegram", "999", "whatsapp", "639") is False

    pairs = [
        {"a": {"platform": "telegram", "chat_key": "111"},
         "b": {"platform": "whatsapp", "chat_key": "639"},
         "already_linked": False},
        {"a": {"platform": "telegram", "chat_key": "111"},
         "b": {"platform": "whatsapp", "chat_key": "639"},
         "already_linked": True},  # 已关联：即使 dismissed 也保留
        {"a": {"platform": "line", "chat_key": "U1"},
         "b": {"platform": "telegram", "chat_key": "222"},
         "already_linked": False},
    ]
    kept = filter_dismissed_pairs(pairs, st)
    assert len(kept) == 2
    assert kept[0]["already_linked"] is True
    assert kept[1]["a"]["chat_key"] == "U1"


# ── 3. load / save roundtrip ────────────────────────────────────────────────

def test_load_dismissed_missing_and_roundtrip(tmp_path):
    missing = tmp_path / "nope.json"
    empty = load_dismissed(missing)
    assert empty == {"keys": {}, "updated_at": 0.0}

    path = tmp_path / "identity_shadow_dismissed.json"
    state = {
        "keys": {"telegram:1|whatsapp:2": {"ts": 42.0, "reason": "x"}},
        "updated_at": 42.0,
    }
    assert save_dismissed(path, state) is True
    loaded = load_dismissed(path)
    assert loaded["keys"] == state["keys"]
    assert loaded["updated_at"] == 42.0


# ── 4. memory_uids_for_peer ─────────────────────────────────────────────────

def test_memory_uids_for_peer_bare_plus_account_buckets():
    ck = "8921664288"
    accts = ["acct_a", "acct_b", "default"]
    uids = memory_uids_for_peer(accts, ck)
    # 裸键必在；default 经 make_context_key 仍是裸键（不重复）
    assert ck in uids
    assert make_context_key(ck, "acct_a") in uids
    assert make_context_key(ck, "acct_b") in uids
    # sorted 确定性
    assert uids == sorted(uids)
    assert set(uids) == {
        ck,
        make_context_key(ck, "acct_a"),
        make_context_key(ck, "acct_b"),
    }
    assert memory_uids_for_peer(["a"], "") == []
    assert memory_uids_for_peer([], ck) == [ck]


# ── 5. peer_canonicals ──────────────────────────────────────────────────────

def test_peer_canonicals_bare_and_suffix():
    links = {
        ("telegram", "111"): "c1",
        ("telegram", "acct_a:111"): "c1",
        ("telegram", "acct_b:111"): "c2",
        ("telegram", "222"): "c3",
        ("whatsapp", "111"): "c4",
        ("telegram", "acct_a:111x"): "c5",  # 后缀不匹配（非 :111）
    }
    got = peer_canonicals(links, "telegram", "111")
    assert got == {"c1", "c2"}
    assert peer_canonicals(links, "telegram", "") == set()
    assert peer_canonicals({}, "telegram", "111") == set()


def test_peer_canonicals_rpa_platform_alias():
    """AI Studio 遗留 `_rpa` 平台名与 inbox 名互认（读侧容错，防漏标已关联）。"""
    links = {
        ("whatsapp_rpa", "111"): "c1",
        ("whatsapp", "acct:111"): "c2",
        ("line_rpa", "111"): "c3",  # 其他平台不掺和
    }
    assert peer_canonicals(links, "whatsapp", "111") == {"c1", "c2"}
    assert peer_canonicals(links, "whatsapp_rpa", "111") == {"c1", "c2"}
    assert peer_canonicals(links, "line", "111") == {"c3"}


# ── 6. confirm_link_pair ────────────────────────────────────────────────────

def test_confirm_link_pair_links_all_account_scoped_uids(tmp_path):
    """人工确认：两侧裸键 + 各账号分桶全部 link；扫描路径永不调用本函数。"""
    db = _mk_inbox(
        tmp_path / "inbox.db",
        conversations=[
            ("tg:a:111", "telegram", "acct_a", "111", "Alice"),
            ("tg:b:111", "telegram", "acct_b", "111", "Alice"),
            ("wa:d:639", "whatsapp", "default", "639", "Alice WA"),
            ("wa:x:639", "whatsapp", "wa_x", "639", "Alice WA"),
        ],
    )
    cpi = _FakeCPI(canonical="canon:alice")
    out = confirm_link_pair(
        cpi, db, "telegram", "111", "whatsapp", "639",
    )
    assert out["ok"] is True
    assert out["canonical_id"] == "canon:alice"
    assert out["pair_key"] == pair_key("telegram", "111", "whatsapp", "639")

    # P10 契约：关联前对两侧**每个 uid** 各 resolve 一次（捕获旧 canonical 供
    # 记忆合流；resolve 幂等注册默认映射）；canonical 取 telegram 侧 primary。
    uids_a = memory_uids_for_peer(["acct_a", "acct_b"], "111")
    uids_b = memory_uids_for_peer(["default", "wa_x"], "639")
    assert cpi.resolve_calls[0] == ("telegram", uids_a[0])
    assert set(cpi.resolve_calls) == (
        {("telegram", u) for u in uids_a} | {("whatsapp", u) for u in uids_b}
    )
    # 未传 episodic_store → 纯关联，合流字段为空但契约存在
    assert out["merged_rows"] == 0
    assert out["merged_from"] == []

    linked_uids = {(x["platform"], x["uid"]) for x in out["linked"]}
    # 两侧全部账号分桶 + 裸键都必须出现
    expect = {( "telegram", u) for u in uids_a} | {("whatsapp", u) for u in uids_b}
    assert linked_uids == expect

    # link 调用：同侧其余 uid + 对侧全部 uid，锚点 = primary (uids_a[0])
    primary = uids_a[0]
    link_set = set(cpi.link_calls)
    for uid in uids_a[1:]:
        assert ("telegram", primary, "telegram", uid) in link_set
    for uid in uids_b:
        assert ("telegram", primary, "whatsapp", uid) in link_set


class _StatefulCPI:
    """带状态的最小 CPI：resolve 返回已存/默认 canonical，link 真改写。"""

    def __init__(self):
        self.map = {}

    def resolve(self, platform, uid):
        return self.map.setdefault((platform, uid), f"{platform}:{uid}")

    def link(self, plat_a, uid_a, plat_b, uid_b):
        canon = self.resolve(plat_a, uid_a)
        self.map[(plat_b, uid_b)] = canon
        return canon


class _FakeEpisodicStore:
    """只记 merge_key 调用；返回行数模拟「B 侧有 2 条、A 侧分桶 1 条」。"""

    def __init__(self):
        self.calls = []

    def merge_key(self, old_key, new_key):
        self.calls.append((old_key, new_key))
        return 2 if old_key.startswith("whatsapp:") else 1


def test_confirm_link_pair_merges_episodic_history(tmp_path):
    """P10：确认关联 = 关联 + 历史记忆合流（旧 canonical → 共享 canonical）。

    同时钉住写侧平台名归一：`whatsapp_rpa` 传入按 `whatsapp` 处理。
    """
    db = _mk_inbox(
        tmp_path / "inbox.db",
        conversations=[
            ("tg:a:111", "telegram", "acct_a", "111", "Alice"),
            ("wa:d:639", "whatsapp", "default", "639", "Alice WA"),
        ],
    )
    cpi = _StatefulCPI()
    store = _FakeEpisodicStore()
    out = confirm_link_pair(
        cpi, db, "telegram", "111", "whatsapp_rpa", "639",
        episodic_store=store,
    )
    assert out["ok"] is True
    uids_a = memory_uids_for_peer(["acct_a"], "111")   # ['111', 'acct_a:111']
    uids_b = memory_uids_for_peer(["default"], "639")  # ['639']
    canon = out["canonical_id"]
    assert canon == f"telegram:{uids_a[0]}"
    # 写侧平台名归一：linked 里是 whatsapp 而非 whatsapp_rpa
    assert {x["platform"] for x in out["linked"]} == {"telegram", "whatsapp"}
    # 旧 canonical（除共享 canonical 自身）逐个并入，目标恒为共享 canonical
    expect_olds = (
        {f"telegram:{u}" for u in uids_a[1:]}
        | {f"whatsapp:{u}" for u in uids_b}
    )
    assert {old for old, _ in store.calls} == expect_olds
    assert all(new == canon for _, new in store.calls)
    # 行数汇总与来源明细
    want_rows = sum(2 if o.startswith("whatsapp:") else 1 for o in expect_olds)
    assert out["merged_rows"] == want_rows
    assert {m["from"] for m in out["merged_from"]} == expect_olds
    assert all(m["rows"] > 0 for m in out["merged_from"])
    # CPI 终态：两侧全部 uid 都指向共享 canonical
    for uid in uids_a:
        assert cpi.map[("telegram", uid)] == canon
    for uid in uids_b:
        assert cpi.map[("whatsapp", uid)] == canon


def test_confirm_link_pair_merge_failure_never_blocks(tmp_path):
    """merge_key 抛异常 → 关联照常成功，合流计数为 0（绝不阻断人工确认）。"""
    db = _mk_inbox(
        tmp_path / "inbox.db",
        conversations=[
            ("tg:a:111", "telegram", "acct_a", "111", "Alice"),
            ("wa:d:639", "whatsapp", "default", "639", "Alice WA"),
        ],
    )

    class _BoomStore:
        def merge_key(self, old_key, new_key):
            raise RuntimeError("db locked")

    out = confirm_link_pair(
        _StatefulCPI(), db, "telegram", "111", "whatsapp", "639",
        episodic_store=_BoomStore(),
    )
    assert out["ok"] is True
    assert out["merged_rows"] == 0
    assert out["merged_from"] == []


def test_confirm_link_pair_real_cpi_and_store_end_to_end(tmp_path):
    """真库集成：真实 CrossPlatformIdentity + EpisodicMemoryStore 走通
    确认关联 → 双侧读同一份记忆（含重复事实按 content_hash 去重）。"""
    from src.utils.cross_platform_identity import CrossPlatformIdentity
    from src.utils.episodic_memory_store import EpisodicMemoryStore

    db = _mk_inbox(
        tmp_path / "inbox.db",
        conversations=[
            ("tg:a:111", "telegram", "acct_a", "111", "Maj"),
            ("wa:d:639", "whatsapp", "default", "639", "Wisley"),
        ],
    )
    cpi = CrossPlatformIdentity(tmp_path / "bot.db")
    store = EpisodicMemoryStore(tmp_path / "bot.db")
    # 关联前：两侧各自 canonical 下有历史事实（一条内容双方重复）
    tg_canon = cpi.resolve("telegram", "acct_a:111")
    wa_canon = cpi.resolve("whatsapp", "639")
    store.add_fact(tg_canon, "对方住在马尼拉", "heuristic", source="user_stated")
    store.add_fact(wa_canon, "对方养了一只猫", "heuristic", source="user_stated")
    store.add_fact(wa_canon, "对方住在马尼拉", "heuristic", source="user_stated")

    out = confirm_link_pair(
        cpi, db, "telegram", "111", "whatsapp", "639", episodic_store=store,
    )
    assert out["ok"] is True
    canon = out["canonical_id"]
    assert canon == cpi.resolve("telegram", "111")  # primary=裸键（sorted 第一）
    # 双侧全部 uid 现在解析到共享 canonical
    assert cpi.resolve("whatsapp", "639") == canon
    assert cpi.resolve("telegram", "acct_a:111") == canon
    # 记忆合流：共享 canonical 下能读到两侧全部事实；重复内容只留一份
    texts = [r["content"] for r in store.list_rows(prefix=canon, limit=20)]
    assert sorted(texts) == ["对方住在马尼拉", "对方养了一只猫"]
    # 旧 canonical 清空（迁移或去重删除，无残留）
    assert store.count(tg_canon) == 0
    assert store.count(wa_canon) == 0
    # merged_rows = 真迁移行数（3 条源、1 条与目标重复 → ≥2 且 ≤3）
    assert 2 <= out["merged_rows"] <= 3
    store.close()
    cpi.close()


def test_confirm_link_pair_cluster_transitivity(tmp_path):
    """P10：某侧 uid 先前已与第三平台成簇 → 确认时整簇改挂共享 canonical。

    场景：wa 639 早先经 AI Studio 手动链了 line Uxyz（共享 canonical
    whatsapp:639）。现在人工确认 tg↔wa 同一人——不改挂 line 的话，
    历史被 merge 走、line 未来写入重新孤儿化（记忆分裂）。
    """
    from src.utils.cross_platform_identity import CrossPlatformIdentity
    from src.utils.episodic_memory_store import EpisodicMemoryStore

    db = _mk_inbox(
        tmp_path / "inbox.db",
        conversations=[
            ("tg:a:111", "telegram", "acct_a", "111", "Maj"),
            ("wa:d:639", "whatsapp", "default", "639", "Wisley"),
        ],
    )
    cpi = CrossPlatformIdentity(tmp_path / "bot.db")
    store = EpisodicMemoryStore(tmp_path / "bot.db")
    # 既有簇：line Uxyz ← whatsapp 639（共享 canonical whatsapp:639）
    old_shared = cpi.link("whatsapp", "639", "line", "Uxyz")
    assert old_shared == "whatsapp:639"
    store.add_fact(old_shared, "对方喜欢喝美式", "heuristic", source="user_stated")

    out = confirm_link_pair(
        cpi, db, "telegram", "111", "whatsapp", "639", episodic_store=store,
    )
    assert out["ok"] is True
    canon = out["canonical_id"]
    # 簇友 line 也被改挂到共享 canonical（传递性），未来写入不分裂
    assert cpi.resolve("line", "Uxyz") == canon
    assert {(r["platform"], r["uid"]) for r in out["cluster_relinked"]} \
        >= {("line", "Uxyz")}
    # 簇史随之合流
    texts = [r["content"] for r in store.list_rows(prefix=canon, limit=20)]
    assert "对方喜欢喝美式" in texts
    assert store.count(old_shared) == 0
    store.close()
    cpi.close()


# ── 6b. 合流观测累计（totals）───────────────────────────────────────────────

def test_record_merge_event_accumulates(tmp_path):
    """P11：confirm / manual_link 各计一类；行数与簇改挂累加；last 带最近一笔。"""
    from src.utils.identity_shadow_actions import (
        load_totals, record_merge_event, totals_path,
    )
    t1 = record_merge_event(
        tmp_path, "confirm", merged_rows=3, cluster_relinked=1,
        canonical="telegram:111", now=100.0)
    assert (t1["confirm_pairs"], t1["manual_links"]) == (1, 0)
    t2 = record_merge_event(
        tmp_path, "manual_link", merged_rows=2, canonical="telegram:222",
        now=200.0)
    assert (t2["confirm_pairs"], t2["manual_links"]) == (1, 1)
    assert t2["merged_rows"] == 5
    assert t2["cluster_relinked"] == 1
    assert t2["last"]["kind"] == "manual_link"
    assert t2["last"]["canonical"] == "telegram:222"
    # 落盘可回读；坏值钳到 0
    got = load_totals(totals_path(tmp_path))
    assert got["merged_rows"] == 5
    assert got["last"]["kind"] == "manual_link"


def test_load_totals_bad_file_returns_zero(tmp_path):
    from src.utils.identity_shadow_actions import load_totals, totals_path
    p = totals_path(tmp_path)
    p.write_text("not json{{", encoding="utf-8")
    got = load_totals(p)
    assert got["confirm_pairs"] == 0
    assert got["merged_rows"] == 0
    assert got["last"] == {}


# ── 7. build_pair_evidence ──────────────────────────────────────────────────

def test_build_pair_evidence_truncated_snippets(tmp_path):
    long_text = "字" * 200
    db = _mk_inbox(
        tmp_path / "inbox.db",
        conversations=[
            ("tg:1", "telegram", "acct_a", "111", "Alice"),
            ("wa:1", "whatsapp", "wa_x", "639", "Bob"),
        ],
        messages=[
            ("m1", "tg:1", "in", long_text, 10.0, "Alice"),
            ("m2", "tg:1", "out", "short reply", 11.0, ""),
            ("m3", "tg:1", "in", "", 12.0, ""),  # 空文本跳过
            ("m4", "wa:1", "in", "hello from wa", 20.0, "Bob"),
        ],
    )
    pair = {
        "a": {"platform": "telegram", "chat_key": "111",
              "display_name": "Alice", "phone": "", "username": ""},
        "b": {"platform": "whatsapp", "chat_key": "639",
              "display_name": "Bob", "phone": "639", "username": ""},
        "tier": "high",
        "evidence": ["phone:639"],
        "already_linked": False,
    }
    ev = build_pair_evidence(db, pair, msg_limit=5)
    assert ev["pair_key"] == pair_key("telegram", "111", "whatsapp", "639")
    assert ev["tier"] == "high"
    assert ev["evidence"] == ["phone:639"]
    assert ev["a"]["account_ids"] == ["acct_a"]
    assert ev["b"]["account_ids"] == ["wa_x"]

    # telegram 侧：空文本被跳过；长文本按实现截到 157 + "…"
    a_msgs = ev["a"]["messages"]
    assert len(a_msgs) == 2
    # ts 降序：short reply (11) 在前，长文 (10) 在后
    assert a_msgs[0]["text"] == "short reply"
    assert a_msgs[1]["text"].endswith("…")
    assert len(a_msgs[1]["text"]) == 158
    assert a_msgs[1]["text"][:157] == "字" * 157

    b_msgs = ev["b"]["messages"]
    assert len(b_msgs) == 1
    assert b_msgs[0]["text"] == "hello from wa"
