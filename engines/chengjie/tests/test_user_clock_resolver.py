"""用户时区解析服务（IO 侧）门禁：`src/companion/user_clock_resolver`。

零真 DB、零网络——除「迁移列」一例用真 `InboxStore`（临时目录）确认 DDL 与
`patch_conv_meta` 白名单端到端可用外，全部走假 store（普通 Python 类 + 调用计数）。

守的核心不变量：
- 未启用时**一次 store 调用都不发**（每 tick 遍历几十个会话，门控失效就是白烧 IO）；
- 电话号码只认 WhatsApp 系平台（Telegram 数字 user_id 形似 E.164，误判 = 错国家 = 凌晨骚扰）；
- 两级缓存 + **负结果也缓存**（推不出来是常态，不缓存就每 tick 重扫消息表）；
- 任一 store 方法抛异常都不炸、优雅降级到「没有时区推断」＝上线前的旧行为。
"""

from __future__ import annotations

from datetime import datetime, timezone

from src.companion.user_clock import ACTIVITY_PRIOR, TRUST_ADVISORY, TRUST_NARROW, TRUST_REPLACE
from src.companion.user_clock_resolver import (
    CACHE_CAPACITY,
    clear_cache_for_tests,
    distribution,
    dump_stats,
    invalidate,
    reset_stats_for_tests,
    resolve_for_conversation,
    utc_hours_for_conversation,
)
from src.inbox.store import InboxStore

import pytest

CID = "telegram:acct1:peer1"
WA_CID = "whatsapp:acct1:66812345678"
CFG = {"enabled": True}
NOW = datetime(2026, 7, 28, 4, 0, tzinfo=timezone.utc).timestamp()
DAY0 = datetime(2026, 7, 27, 0, 0, tzinfo=timezone.utc).timestamp()

TZ_COLUMNS = (
    "tz_hint", "tz_confidence", "tz_source", "tz_country", "tz_offset", "tz_resolved_at",
)


@pytest.fixture(autouse=True)
def _clean_state():
    clear_cache_for_tests()
    reset_stats_for_tests()
    yield
    clear_cache_for_tests()
    reset_stats_for_tests()


# ---------------------------------------------------------------------------
# 假 store
# ---------------------------------------------------------------------------

class FakeInboxStore:
    """按 cid 分片的假 InboxStore，逐方法计调用次数、可指定哪些方法抛异常。"""

    def __init__(self, *, conversation=None, messages=None, meta=None, cid=CID, boom=()):
        self.convs = {cid: dict(conversation)} if conversation else {}
        # 刻意不 copy 消息行：测试要能塞 None / 缺键 / 脏 ts 这类垃圾行进去，
        # 消化垃圾是被测方（utc_hours_for_conversation）的职责，不是假 store 的。
        self.msgs = {cid: list(messages)} if messages else {}
        self.metas = {cid: dict(meta)} if meta else {}
        self.boom = set(boom)
        self.calls = {}
        self.patched = []

    def _hit(self, name):
        self.calls[name] = self.calls.get(name, 0) + 1
        if name in self.boom:
            raise RuntimeError(f"{name} boom")

    @property
    def total(self) -> int:
        return sum(self.calls.values())

    def get_conversation(self, conversation_id):
        self._hit("get_conversation")
        row = self.convs.get(conversation_id)
        return dict(row) if row else None

    def list_recent_messages(self, conversation_id, *, limit=50, before_ts=None):
        self._hit("list_recent_messages")
        rows = self.msgs.get(conversation_id) or []
        return [dict(r) if isinstance(r, dict) else r for r in rows[-int(limit):]]

    def get_conv_meta(self, conversation_id):
        self._hit("get_conv_meta")
        row = self.metas.get(conversation_id)
        return dict(row) if row is not None else None

    def patch_conv_meta(self, conversation_id, fields):
        self._hit("patch_conv_meta")
        self.patched.append((conversation_id, dict(fields)))
        self.metas.setdefault(conversation_id, {}).update(dict(fields))


class FakeEpisodicStore:
    """假 episodic store；``accept_source=False`` 模拟旧版签名（不认 source 入参）。"""

    def __init__(self, rows=(), *, accept_source=True, boom=False):
        self.rows = [dict(r) for r in rows]
        self.accept_source = accept_source
        self.boom = boom
        self.calls = []

    def list_rows(self, prefix="", limit=100, source=""):
        if source and not self.accept_source:
            raise TypeError("list_rows() got an unexpected keyword argument 'source'")
        self.calls.append({"prefix": prefix, "limit": limit, "source": source})
        if self.boom:
            raise RuntimeError("list_rows boom")
        if source:
            return [dict(r) for r in self.rows if r.get("source") == source]
        return [dict(r) for r in self.rows]


def _thai_inbound(day_start: float = DAY0):
    """「泰国用户」入站消息：本地 8-23 点按活跃度先验塑形，UTC 小时 = 本地 −7。

    总量刻意压到 115 条（< `utc_hours_for_conversation` 的 limit=120），这样最近窗口
    不会截断掉早间小时——否则直方图被削成半天、推断必然失准。
    """
    rows = []
    for local_hour in range(8, 24):
        count = max(1, round(ACTIVITY_PRIOR[local_hour] * 130))
        utc_hour = (local_hour - 7) % 24
        for i in range(count):
            rows.append({
                "direction": "in",
                "ts": day_start + utc_hour * 3600 + i * 20,
                "text": "hi",
            })
    rows.sort(key=lambda r: r["ts"])
    return rows


def _residence_row(content="我住在曼谷", created_at=100.0, source="user_stated"):
    return {"content": content, "created_at": created_at, "source": source}


def _fresh_meta(source="stated_city", tz_hint="Asia/Bangkok", **kw):
    meta = {
        "tz_hint": tz_hint, "tz_confidence": 0.92, "tz_source": source,
        "tz_country": "TH", "tz_offset": 7.0, "tz_resolved_at": NOW - 60,
    }
    meta.update(kw)
    return meta


# ---------------------------------------------------------------------------
# 配置门控：未启用 = 零 store 调用
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("cfg", [None, {}, {"enabled": False}, {"enabled": 0}, {"ttl_sec": 60}])
def test_disabled_returns_none_without_touching_any_store(cfg):
    store = FakeInboxStore(conversation={"platform": "whatsapp", "chat_key": "66812345678"},
                           messages=_thai_inbound())
    epi = FakeEpisodicStore([_residence_row()])
    assert resolve_for_conversation(
        CID, inbox_store=store, episodic_store=epi, memory_key="tg:1", cfg=cfg) is None
    assert store.total == 0            # 一次 store 调用都没发生
    assert store.calls == {}
    assert epi.calls == []
    assert dump_stats()["disabled"] == 1


def test_disabled_gate_precedes_even_a_broken_store():
    """门控在最前 → 连「store 是个会炸的对象」都不该被碰到。"""
    class ExplodingStore:
        def __getattr__(self, name):
            raise AssertionError(f"未启用时不该访问 store.{name}")

    assert resolve_for_conversation(CID, inbox_store=ExplodingStore(), cfg={}) is None
    assert dump_stats()["disabled"] == 1


def test_blank_conversation_id_is_a_noop():
    store = FakeInboxStore(messages=_thai_inbound())
    assert resolve_for_conversation("   ", inbox_store=store, cfg=CFG) is None
    assert store.total == 0


# ---------------------------------------------------------------------------
# 信号 1：自述城市
# ---------------------------------------------------------------------------

def test_stated_city_wins_and_persists_full_row():
    store = FakeInboxStore(conversation={"platform": "telegram", "chat_key": "5433982810",
                                         "language": "zh"})
    epi = FakeEpisodicStore([_residence_row()])
    clock = resolve_for_conversation(
        CID, inbox_store=store, episodic_store=epi, memory_key="tg:1", cfg=CFG, now=NOW)

    assert clock is not None
    assert clock.source == "stated_city"
    assert clock.trust == TRUST_REPLACE
    assert clock.tz_name == "Asia/Bangkok"
    assert clock.country == "TH"
    assert clock.offset_hours == pytest.approx(7.0)

    assert store.calls["patch_conv_meta"] == 1
    cid, fields = store.patched[-1]
    assert cid == CID
    assert set(fields) == set(TZ_COLUMNS)          # 六列一个不少、一个不多
    assert fields["tz_hint"] == "Asia/Bangkok"
    assert fields["tz_source"] == "stated_city"
    assert fields["tz_country"] == "TH"
    assert fields["tz_offset"] == pytest.approx(7.0)
    assert fields["tz_confidence"] == pytest.approx(0.92)
    assert fields["tz_resolved_at"] == pytest.approx(NOW)
    assert dump_stats()["resolved"] == 1
    assert dump_stats()["persist_ok"] == 1
    assert epi.calls[0]["source"] == "user_stated"  # 优先只认客户亲口说的
    assert epi.calls[0]["limit"] == 80


def test_stated_city_takes_latest_by_created_at():
    """搬家：后来那条（created_at 更大）必须盖住旧地址，且不依赖 list_rows 的排序。"""
    store = FakeInboxStore(conversation={"platform": "telegram", "language": "unknown"})
    rows = [_residence_row("我住在东京", created_at=500.0),
            _residence_row("我住在曼谷", created_at=100.0)]     # 刻意乱序喂
    clock = resolve_for_conversation(
        CID, inbox_store=store, episodic_store=rows and FakeEpisodicStore(rows),
        memory_key="tg:1", cfg=CFG, now=NOW)
    assert clock.tz_name == "Asia/Tokyo"
    assert clock.source == "stated_city"


def test_ai_inferred_residence_used_only_when_user_stated_empty():
    """`user_stated` 筛选返回空 → 退化为不带 source 的一次查询（拿到 ai_inferred 也算）。"""
    epi = FakeEpisodicStore([_residence_row("我住在首尔", source="ai_inferred")])
    store = FakeInboxStore(conversation={"platform": "telegram", "language": "unknown"})
    clock = resolve_for_conversation(
        CID, inbox_store=store, episodic_store=epi, memory_key="tg:1", cfg=CFG, now=NOW)
    assert clock is not None and clock.tz_name == "Asia/Seoul"
    assert [c["source"] for c in epi.calls] == ["user_stated", ""]   # 两次：先严后宽


def test_legacy_episodic_store_without_source_kwarg_still_works():
    epi = FakeEpisodicStore([_residence_row()], accept_source=False)
    store = FakeInboxStore(conversation={"platform": "telegram", "language": "unknown"})
    clock = resolve_for_conversation(
        CID, inbox_store=store, episodic_store=epi, memory_key="tg:1", cfg=CFG, now=NOW)
    assert clock is not None and clock.source == "stated_city"


def test_no_memory_key_means_no_episodic_query():
    epi = FakeEpisodicStore([_residence_row()])
    store = FakeInboxStore(conversation={"platform": "telegram", "language": "unknown"})
    assert resolve_for_conversation(
        CID, inbox_store=store, episodic_store=epi, cfg=CFG, now=NOW) is None
    assert epi.calls == []


def test_non_residence_memories_are_ignored():
    epi = FakeEpisodicStore([_residence_row("用户自称：小明"), _residence_row("喜欢猫")])
    store = FakeInboxStore(conversation={"platform": "telegram", "language": "unknown"})
    assert resolve_for_conversation(
        CID, inbox_store=store, episodic_store=epi, memory_key="tg:1", cfg=CFG, now=NOW) is None


# ---------------------------------------------------------------------------
# 信号 2：电话国码（平台护栏）
# ---------------------------------------------------------------------------

def test_whatsapp_chat_key_is_treated_as_phone():
    store = FakeInboxStore(
        cid=WA_CID,
        conversation={"platform": "whatsapp", "chat_key": "66812345678@s.whatsapp.net",
                      "language": "unknown"})
    clock = resolve_for_conversation(WA_CID, inbox_store=store, cfg=CFG, now=NOW)
    assert clock is not None
    assert clock.source == "phone_cc"
    assert clock.trust == TRUST_REPLACE
    assert clock.tz_name == "Asia/Bangkok"


@pytest.mark.parametrize("platform", ["telegram", "line", "messenger", "TELEGRAM", ""])
def test_non_whatsapp_platform_never_yields_phone_cc(platform):
    """重要护栏：同一串数字放在非 WhatsApp 平台上是 user_id，绝不能被当电话号码。"""
    store = FakeInboxStore(
        conversation={"platform": platform, "chat_key": "66812345678", "language": "unknown"})
    clock = resolve_for_conversation(CID, inbox_store=store, cfg=CFG, now=NOW)
    assert clock is None
    assert dump_stats()["unresolved"] == 1


def test_wa_alias_platform_also_accepted():
    store = FakeInboxStore(
        cid=WA_CID,
        conversation={"platform": " WA ", "chat_key": "+66 81 234 5678", "language": "unknown"})
    clock = resolve_for_conversation(WA_CID, inbox_store=store, cfg=CFG, now=NOW)
    assert clock is not None and clock.source == "phone_cc"


# ---------------------------------------------------------------------------
# 信号 3：行为推断
# ---------------------------------------------------------------------------

def test_behavior_inference_from_inbound_hours():
    store = FakeInboxStore(
        conversation={"platform": "telegram", "chat_key": "5433982810", "language": "unknown"},
        messages=_thai_inbound())
    clock = resolve_for_conversation(CID, inbox_store=store, cfg=CFG, now=NOW)
    assert clock is not None
    assert clock.source in {"behavior", "behavior_corroborated"}
    assert clock.offset_hours == pytest.approx(7.0)
    assert clock.trust in {TRUST_NARROW, TRUST_REPLACE}


def test_behavior_plus_language_corroborates_to_real_iana_name():
    store = FakeInboxStore(
        conversation={"platform": "telegram", "chat_key": "5433982810", "language": "th"},
        messages=_thai_inbound())
    clock = resolve_for_conversation(CID, inbox_store=store, cfg=CFG, now=NOW)
    assert clock.source == "behavior_corroborated"
    assert clock.tz_name == "Asia/Bangkok"        # 白拿 DST 支持
    assert clock.trust == TRUST_REPLACE


def test_min_samples_override_from_cfg_blocks_behavior():
    store = FakeInboxStore(
        conversation={"platform": "telegram", "language": "unknown"},
        messages=_thai_inbound())
    clock = resolve_for_conversation(
        CID, inbox_store=store, cfg={"enabled": True, "min_samples": 100000}, now=NOW)
    assert clock is None
    clear_cache_for_tests()
    strict = resolve_for_conversation(
        CID, inbox_store=store, cfg={"enabled": True, "min_margin": 0.99}, now=NOW)
    assert strict is None                          # margin 门槛也可从 cfg 收紧


# ---------------------------------------------------------------------------
# 信号 4：语种兜底
# ---------------------------------------------------------------------------

def test_language_only_falls_back_to_advisory():
    store = FakeInboxStore(conversation={"platform": "telegram", "chat_key": "5433982810",
                                         "language": "th"})
    clock = resolve_for_conversation(CID, inbox_store=store, cfg=CFG, now=NOW)
    assert clock is not None
    assert clock.source == "lang_default"
    assert clock.trust == TRUST_ADVISORY
    assert clock.country == "TH"


def test_unknown_language_is_treated_as_absent():
    store = FakeInboxStore(conversation={"platform": "telegram", "language": "unknown"})
    assert resolve_for_conversation(CID, inbox_store=store, cfg=CFG, now=NOW) is None


# ---------------------------------------------------------------------------
# 两级缓存
# ---------------------------------------------------------------------------

def test_second_call_hits_process_cache_without_any_store_call():
    store = FakeInboxStore(conversation={"platform": "telegram", "language": "th"})
    first = resolve_for_conversation(CID, inbox_store=store, cfg=CFG, now=NOW)
    calls_after_first = dict(store.calls)
    second = resolve_for_conversation(CID, inbox_store=store, cfg=CFG, now=NOW + 5)
    assert second == first
    assert store.calls == calls_after_first        # 调用计数一动不动
    assert dump_stats()["cache_hit_mem"] == 1
    assert dump_stats()["resolved"] == 1           # 只推断了一次


def test_force_recomputes_and_skips_both_cache_layers():
    store = FakeInboxStore(conversation={"platform": "telegram", "language": "th"})
    resolve_for_conversation(CID, inbox_store=store, cfg=CFG, now=NOW)
    meta_reads = store.calls.get("get_conv_meta", 0)
    resolve_for_conversation(CID, inbox_store=store, cfg=CFG, now=NOW + 5, force=True)
    assert store.calls["get_conversation"] == 2                    # 真重算
    assert store.calls.get("get_conv_meta", 0) == meta_reads       # 连库缓存都没问
    assert dump_stats()["resolved"] == 2
    assert dump_stats()["cache_hit_mem"] == 0


def test_invalidate_forces_a_real_recompute_even_with_fresh_db_row():
    """invalidate 后必须真重推：光清进程缓存会被库里新鲜的 tz_* 挡回去（旧推断压住强信号）。"""
    store = FakeInboxStore(conversation={"platform": "telegram", "language": "th"})
    resolve_for_conversation(CID, inbox_store=store, cfg=CFG, now=NOW)
    assert store.metas[CID]["tz_resolved_at"] == pytest.approx(NOW)   # 库里已有新鲜副本
    invalidate(CID)
    resolve_for_conversation(CID, inbox_store=store, cfg=CFG, now=NOW + 5)
    assert store.calls["get_conversation"] == 2
    assert dump_stats()["resolved"] == 2
    assert dump_stats()["cache_hit_db"] == 0
    invalidate("")            # 空 cid 不炸
    invalidate(None)          # type: ignore[arg-type]


def test_negative_result_is_cached_and_stops_message_rescan():
    store = FakeInboxStore(conversation={"platform": "telegram", "language": "unknown"})
    assert resolve_for_conversation(CID, inbox_store=store, cfg=CFG, now=NOW) is None
    scans = store.calls["list_recent_messages"]
    assert resolve_for_conversation(CID, inbox_store=store, cfg=CFG, now=NOW + 60) is None
    assert store.calls["list_recent_messages"] == scans   # 没有重扫消息
    assert dump_stats()["cache_hit_mem"] == 1
    assert dump_stats()["unresolved"] == 1
    # 负结果也落库（source 空 + confidence -1），这样跨重启同样不重扫
    _cid, fields = store.patched[-1]
    assert fields["tz_source"] == ""
    assert fields["tz_confidence"] == -1
    assert fields["tz_hint"] == ""
    assert fields["tz_resolved_at"] == pytest.approx(NOW)


def test_ttl_expiry_recomputes():
    store = FakeInboxStore(conversation={"platform": "telegram", "language": "th"})
    cfg = {"enabled": True, "ttl_sec": 100}
    resolve_for_conversation(CID, inbox_store=store, cfg=cfg, now=NOW)
    resolve_for_conversation(CID, inbox_store=store, cfg=cfg, now=NOW + 99)
    assert dump_stats()["cache_hit_mem"] == 1
    assert dump_stats()["resolved"] == 1
    resolve_for_conversation(CID, inbox_store=store, cfg=cfg, now=NOW + 101)
    assert dump_stats()["resolved"] == 2                 # 过期 → 重算
    assert store.calls["get_conversation"] == 2


def test_db_cache_rebuilds_without_scanning_messages():
    store = FakeInboxStore(conversation={"platform": "telegram", "language": "th"},
                           messages=_thai_inbound(), meta=_fresh_meta())
    clock = resolve_for_conversation(CID, inbox_store=store, cfg=CFG, now=NOW)
    assert clock is not None
    assert (clock.source, clock.tz_name, clock.trust) == (
        "stated_city", "Asia/Bangkok", TRUST_REPLACE)
    assert clock.confidence == pytest.approx(0.92)
    assert clock.country == "TH"
    assert clock.offset_hours == pytest.approx(7.0)
    assert store.calls.get("list_recent_messages", 0) == 0   # 没扫消息
    assert store.calls.get("get_conversation", 0) == 0       # 也没读会话
    assert store.calls.get("patch_conv_meta", 0) == 0        # 更没回写
    assert dump_stats()["cache_hit_db"] == 1
    assert dump_stats()["resolved"] == 0


def test_db_cache_keeps_original_resolved_at_so_ttl_still_expires():
    """回读不得给 TTL 续期，否则一个陈旧推断可以永不过期。"""
    cfg = {"enabled": True, "ttl_sec": 100}
    store = FakeInboxStore(conversation={"platform": "telegram", "language": "th"},
                           meta=_fresh_meta(tz_resolved_at=NOW - 90))
    assert resolve_for_conversation(CID, inbox_store=store, cfg=cfg, now=NOW) is not None
    assert dump_stats()["cache_hit_db"] == 1
    # 原 resolved_at 是 NOW-90，ttl=100 → NOW+11 就该过期重算（而非 NOW+100）
    assert resolve_for_conversation(CID, inbox_store=store, cfg=cfg, now=NOW + 11) is not None
    assert dump_stats()["resolved"] == 1


def test_db_negative_result_is_honored():
    store = FakeInboxStore(conversation={"platform": "telegram", "language": "th"},
                           meta=_fresh_meta(source="", tz_hint="", tz_confidence=-1,
                                            tz_country="", tz_offset=0))
    assert resolve_for_conversation(CID, inbox_store=store, cfg=CFG, now=NOW) is None
    assert dump_stats()["cache_hit_db"] == 1
    assert store.calls.get("get_conversation", 0) == 0


def test_stale_db_row_is_ignored():
    store = FakeInboxStore(conversation={"platform": "telegram", "language": "th"},
                           meta=_fresh_meta(tz_resolved_at=NOW - 999999))
    clock = resolve_for_conversation(CID, inbox_store=store, cfg=CFG, now=NOW)
    assert clock is not None and clock.source == "lang_default"   # 陈旧 → 重推
    assert dump_stats()["cache_hit_db"] == 0
    assert dump_stats()["resolved"] == 1


def test_zero_resolved_at_is_not_a_cache_hit():
    store = FakeInboxStore(conversation={"platform": "telegram", "language": "th"},
                           meta={"tz_hint": "", "tz_source": "", "tz_resolved_at": 0})
    assert resolve_for_conversation(CID, inbox_store=store, cfg=CFG, now=NOW) is not None
    assert dump_stats()["cache_hit_db"] == 0     # 从未推断过 ≠ 新鲜的负结果


@pytest.mark.parametrize("source,tz_hint,expected_trust", [
    ("stated_city", "Asia/Bangkok", TRUST_REPLACE),
    ("phone_cc", "Asia/Bangkok", TRUST_REPLACE),
    ("behavior_corroborated", "Asia/Bangkok", TRUST_REPLACE),
    ("behavior", "UTC+07:00", TRUST_NARROW),
    ("lang_default", "Asia/Bangkok", TRUST_ADVISORY),
    # 多时区国的 phone_cc 只定位到国家（tz 空）→ 原本就是 advisory，回读不得升格
    ("phone_cc", "", TRUST_ADVISORY),
    ("lang_default", "", TRUST_ADVISORY),
])
def test_trust_is_reconstructed_from_source(source, tz_hint, expected_trust):
    store = FakeInboxStore(meta=_fresh_meta(source=source, tz_hint=tz_hint))
    clock = resolve_for_conversation(CID, inbox_store=store, cfg=CFG, now=NOW)
    assert clock is not None
    assert clock.source == source
    assert clock.trust == expected_trust
    assert clock.tz_name == tz_hint


def test_unknown_db_source_is_recomputed_not_trusted():
    store = FakeInboxStore(conversation={"platform": "telegram", "language": "th"},
                           meta=_fresh_meta(source="from_the_future"))
    clock = resolve_for_conversation(CID, inbox_store=store, cfg=CFG, now=NOW)
    assert clock is not None
    assert clock.source == "lang_default"        # 不认的 source → 当没命中，重推
    assert dump_stats()["cache_hit_db"] == 0


# ---------------------------------------------------------------------------
# 进程缓存容量上限
# ---------------------------------------------------------------------------

def test_process_cache_has_a_hard_capacity():
    store = FakeInboxStore()          # 无会话/无消息 → 一律负结果，飞快
    for i in range(CACHE_CAPACITY + 100):
        resolve_for_conversation(f"telegram:acct1:peer{i}", inbox_store=store,
                                 cfg=CFG, now=NOW)
    assert sum(distribution().values()) == CACHE_CAPACITY
    assert distribution() == {"none": CACHE_CAPACITY}
    # 最旧的被淘汰（再问要重新走库/重算），最新的仍在进程缓存里
    reset_stats_for_tests()
    resolve_for_conversation("telegram:acct1:peer0", inbox_store=store, cfg=CFG, now=NOW)
    assert dump_stats()["cache_hit_mem"] == 0
    resolve_for_conversation(f"telegram:acct1:peer{CACHE_CAPACITY + 99}",
                             inbox_store=store, cfg=CFG, now=NOW)
    assert dump_stats()["cache_hit_mem"] == 1


def test_distribution_groups_by_source():
    wa = FakeInboxStore(cid=WA_CID,
                        conversation={"platform": "whatsapp", "chat_key": "66812345678",
                                      "language": "unknown"})
    resolve_for_conversation(WA_CID, inbox_store=wa, cfg=CFG, now=NOW)
    lang = FakeInboxStore(conversation={"platform": "telegram", "language": "th"})
    resolve_for_conversation(CID, inbox_store=lang, cfg=CFG, now=NOW)
    empty = FakeInboxStore(cid="telegram:acct1:peerX")
    resolve_for_conversation("telegram:acct1:peerX", inbox_store=empty, cfg=CFG, now=NOW)
    assert distribution() == {"phone_cc": 1, "lang_default": 1, "none": 1}


# ---------------------------------------------------------------------------
# 异常降级：每个 store 方法分别炸
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("broken", ["get_conversation", "list_recent_messages",
                                    "get_conv_meta", "patch_conv_meta"])
def test_each_broken_store_method_degrades_gracefully(broken):
    store = FakeInboxStore(
        cid=WA_CID,
        conversation={"platform": "whatsapp", "chat_key": "66812345678",
                      "language": "unknown"},
        messages=_thai_inbound(), boom=[broken])
    clock = resolve_for_conversation(WA_CID, inbox_store=store, cfg=CFG, now=NOW)
    assert store.calls.get(broken, 0) >= 1        # 真的走到了那个坏方法
    if broken == "get_conversation":
        assert clock is not None                  # 丢了号码/语种，行为推断照常出结果
        assert clock.source in {"behavior", "behavior_corroborated"}
    else:
        assert clock is not None and clock.source == "phone_cc"
    if broken == "patch_conv_meta":
        assert dump_stats()["persist_fail"] == 1
        assert dump_stats()["persist_ok"] == 0


def test_broken_episodic_store_does_not_block_other_signals():
    store = FakeInboxStore(cid=WA_CID,
                           conversation={"platform": "whatsapp", "chat_key": "66812345678",
                                         "language": "unknown"})
    clock = resolve_for_conversation(WA_CID, inbox_store=store,
                                     episodic_store=FakeEpisodicStore(boom=True),
                                     memory_key="tg:1", cfg=CFG, now=NOW)
    assert clock is not None and clock.source == "phone_cc"


def test_totally_broken_store_object_returns_none_without_raising():
    class Useless:
        pass

    assert resolve_for_conversation(CID, inbox_store=Useless(), cfg=CFG, now=NOW) is None
    assert resolve_for_conversation(CID, inbox_store=None, cfg=CFG, now=NOW + 1) is None


def test_garbage_inputs_never_raise():
    store = FakeInboxStore(conversation={"platform": "telegram", "language": "th"})
    for junk in (None, "", 0, -1, 3.5, [], {}, object(), True):
        resolve_for_conversation(junk, inbox_store=store, episodic_store=junk,  # type: ignore[arg-type]
                                 memory_key=junk, cfg=junk, now=junk, force=junk)  # type: ignore[arg-type]
        utc_hours_for_conversation(junk, junk, limit=junk)  # type: ignore[arg-type]
        invalidate(junk)  # type: ignore[arg-type]
    for bad_cfg in ({"enabled": True, "ttl_sec": "x"}, {"enabled": True, "min_samples": None},
                    {"enabled": True, "min_margin": []}):
        resolve_for_conversation(CID, inbox_store=store, cfg=bad_cfg, now=NOW)
        clear_cache_for_tests()
    assert isinstance(dump_stats(), dict)


# ---------------------------------------------------------------------------
# utc_hours_for_conversation
# ---------------------------------------------------------------------------

def test_utc_hours_only_counts_inbound_with_positive_ts():
    store = FakeInboxStore(messages=[
        {"direction": "in", "ts": DAY0 + 3 * 3600},     # UTC 03:00
        {"direction": "out", "ts": DAY0 + 4 * 3600},    # 出站 → 不算（那是我们自己的节奏）
        {"direction": "in", "ts": 0},                   # ts<=0 → 跳过
        {"direction": "in", "ts": -5},
        {"direction": "in", "ts": DAY0 + 21 * 3600},    # UTC 21:00
        {"direction": "", "ts": DAY0 + 5 * 3600},
        {"ts": DAY0 + 6 * 3600},
        {"direction": "in", "ts": "not-a-number"},
        None,
    ])
    assert utc_hours_for_conversation(store, CID) == [3, 21]


def test_utc_hours_passes_limit_and_returns_empty_on_error():
    store = FakeInboxStore(messages=[{"direction": "in", "ts": DAY0 + h * 3600}
                                     for h in range(24)])
    assert len(utc_hours_for_conversation(store, CID, limit=5)) == 5
    assert len(utc_hours_for_conversation(store, CID)) == 24
    assert utc_hours_for_conversation(store, "no-such-cid") == []
    broken = FakeInboxStore(messages=[{"direction": "in", "ts": DAY0}],
                            boom=["list_recent_messages"])
    assert utc_hours_for_conversation(broken, CID) == []


def test_utc_hours_are_utc_not_server_local():
    """铁律：行为推断吃的是 **UTC** 小时（服务器本地会把 +8 的偏移混进统计）。"""
    ts = datetime(2026, 7, 28, 17, 30, tzinfo=timezone.utc).timestamp()
    store = FakeInboxStore(messages=[{"direction": "in", "ts": ts}])
    assert utc_hours_for_conversation(store, CID) == [17]


# ---------------------------------------------------------------------------
# 观测
# ---------------------------------------------------------------------------

def test_dump_stats_field_contract_and_isolation():
    snapshot = dump_stats()
    assert set(snapshot) == {"cache_hit_mem", "cache_hit_db", "resolved", "unresolved",
                             "persist_ok", "persist_fail", "disabled"}
    assert all(v == 0 for v in snapshot.values())
    snapshot["resolved"] = 999                 # 返回的是拷贝
    assert dump_stats()["resolved"] == 0


# ---------------------------------------------------------------------------
# 真 InboxStore：迁移列 + patch 白名单端到端
# ---------------------------------------------------------------------------

def test_migration_adds_tz_columns_with_expected_defaults(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    try:
        assert store.migration_errors == 0
        store.patch_conv_meta(CID, {"churn_risk": "low"})     # 建 meta 行（不碰 tz_*）
        meta = store.get_conv_meta(CID)
        assert meta is not None
        for col in TZ_COLUMNS:
            assert col in meta, f"迁移没加上 {col}"
        assert meta["tz_hint"] == ""
        assert meta["tz_source"] == ""
        assert meta["tz_country"] == ""
        assert meta["tz_confidence"] == -1
        assert meta["tz_offset"] == 0
        assert meta["tz_resolved_at"] == 0
    finally:
        store.close()


def test_existing_db_gains_tz_columns_on_reopen(tmp_path, monkeypatch):
    """在位升级路径（生产实例下次重启真实走的那条）：老库重开必须补上 6 列且零迁移错误。

    2026-08-02 起迁移记账键＝**SQL 内容哈希**（schema_migrations_sha），与列表位置
    彻底解耦——「截断到首个 tz 迁移之前」的前缀库重开后，缺的条目按内容补跑即可，
    不再要求 tz 迁移钉在列表末尾（旧的下标记账时代才需要那个位置不变量；tz 之后
    已合法追加 T-mood / peer_bot_guard 等新列）。
    """
    from src.inbox import store as store_mod

    full = list(store_mod._MIGRATIONS)
    first_tz = next(i for i, sql in enumerate(full) if "ADD COLUMN tz_" in sql)
    legacy = full[:first_tz]                      # 前缀库：tz 及其后的迁移都没跑过
    assert len([sql for sql in full[first_tz:] if "ADD COLUMN tz_" in sql]) == 6

    db = tmp_path / "legacy.db"
    monkeypatch.setattr(store_mod, "_MIGRATIONS", legacy)
    old_store = store_mod.InboxStore(db)
    try:
        old_store.patch_conv_meta(CID, {"churn_risk": "low"})
        assert "tz_hint" not in old_store.get_conv_meta(CID)   # 老库确实还没这列
    finally:
        old_store.close()

    monkeypatch.undo()
    upgraded = store_mod.InboxStore(db)
    try:
        assert upgraded.migration_errors == 0
        meta = upgraded.get_conv_meta(CID)
        for col in TZ_COLUMNS:
            assert col in meta, f"升级后仍缺列 {col}"
        assert meta["churn_risk"] == "low"        # 存量数据毫发无损
        assert meta["tz_confidence"] == -1
        upgraded.patch_conv_meta(CID, {"tz_source": "phone_cc"})
        assert upgraded.get_conv_meta(CID)["tz_source"] == "phone_cc"
    finally:
        upgraded.close()


def test_real_store_patch_roundtrip_and_whitelist(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    try:
        store.patch_conv_meta(CID, {
            "tz_hint": "Asia/Bangkok", "tz_confidence": 0.92, "tz_source": "stated_city",
            "tz_country": "TH", "tz_offset": 7.0, "tz_resolved_at": NOW,
            "definitely_not_a_column": "drop me",     # 白名单外 → 静默丢弃，不炸
        })
        meta = store.get_conv_meta(CID)
        assert meta["tz_hint"] == "Asia/Bangkok"
        assert meta["tz_confidence"] == pytest.approx(0.92)
        assert meta["tz_source"] == "stated_city"
        assert meta["tz_country"] == "TH"
        assert meta["tz_offset"] == pytest.approx(7.0)
        assert meta["tz_resolved_at"] == pytest.approx(NOW)
        assert "definitely_not_a_column" not in meta
        store.patch_conv_meta(CID, {"tz_source": "", "tz_confidence": -1})   # 负结果覆写
        assert store.get_conv_meta(CID)["tz_source"] == ""
    finally:
        store.close()


def test_end_to_end_against_real_store(tmp_path):
    """真 store 全程：推断 → 落库 → 清进程缓存 → 从库重建（列名与白名单必须真对得上）。"""
    store = InboxStore(tmp_path / "inbox.db")
    try:
        epi = FakeEpisodicStore([_residence_row()])
        first = resolve_for_conversation(CID, inbox_store=store, episodic_store=epi,
                                        memory_key="tg:1", cfg=CFG, now=NOW)
        assert first is not None and first.source == "stated_city"
        assert dump_stats()["persist_ok"] == 1
        assert store.get_conv_meta(CID)["tz_hint"] == "Asia/Bangkok"

        clear_cache_for_tests()                    # 模拟重启：进程缓存空，库里还在
        rebuilt = resolve_for_conversation(CID, inbox_store=store, cfg=CFG, now=NOW + 60)
        assert rebuilt is not None
        assert (rebuilt.tz_name, rebuilt.source, rebuilt.trust) == (
            "Asia/Bangkok", "stated_city", TRUST_REPLACE)
        assert dump_stats()["cache_hit_db"] == 1
        assert dump_stats()["resolved"] == 1       # 没有重新推断
    finally:
        store.close()
