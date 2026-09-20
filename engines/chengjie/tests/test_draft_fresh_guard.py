"""B 线草稿「新入站过期」守卫（fresh_guard）门禁。

锁定（2026-08-03）：
  - 纯函数 superseded_by_inbound：grace 边界 / 相等时刻 / 坏输入一律不拦；
  - parse_fresh_guard_cfg：默认关；完整树与 l2_autosend 块两种形态都能解析，
    完整树顺带镜像 inbox.auto_draft.min_text_len；
  - find_superseding_inbound：只认「更晚、非空、与 peer_text 不同、过 min_text_len」
    的入站——相同文本 = auto_generate_draft 幂等跳过不会重拟（取消旧稿=客户零回复），
    过短文本不会触发拟稿回调，两类都必须放行；
  - AutosendWorker 集成：过期 → 跳过投递 + cancelled/superseded_by_inbound 原子处置 +
    total_superseded 计数且**不计 error**（不喂熔断器）；未过期/关闭/守卫异常/缺
    created_ts → 照常投递（守卫失败模式=放行）。

全部纯内存假件，不写仓库 config/、不碰生产库。
"""

from __future__ import annotations

import time

import pytest

from src.inbox.autosend_worker import AutosendWorker
from src.inbox.draft_fresh_guard import (
    DEFAULT_GRACE_SEC,
    find_superseding_inbound,
    parse_fresh_guard_cfg,
    superseded_by_inbound,
)

T0 = 1_700_000_000.0  # 固定基准时刻，测试不依赖真实时钟


# ── superseded_by_inbound 纯函数边界 ─────────────────────────────────

def test_superseded_basic():
    assert superseded_by_inbound(T0, T0 + 10, grace_sec=3.0) is True


def test_within_grace_not_superseded():
    assert superseded_by_inbound(T0, T0 + 2.0, grace_sec=3.0) is False
    # 恰好等于 draft_ts + grace：严格大于才算过期
    assert superseded_by_inbound(T0, T0 + 3.0, grace_sec=3.0) is False
    assert superseded_by_inbound(T0, T0 + 3.001, grace_sec=3.0) is True


def test_equal_ts_not_superseded():
    assert superseded_by_inbound(T0, T0) is False


def test_earlier_inbound_not_superseded():
    assert superseded_by_inbound(T0, T0 - 100) is False


def test_bad_inputs_never_block():
    assert superseded_by_inbound(None, T0) is False
    assert superseded_by_inbound(T0, None) is False
    assert superseded_by_inbound("abc", T0) is False
    assert superseded_by_inbound(T0, "abc") is False
    assert superseded_by_inbound(0, T0) is False
    assert superseded_by_inbound(T0, 0) is False
    assert superseded_by_inbound(-1, T0) is False
    assert superseded_by_inbound(T0, -1) is False


def test_bad_grace_falls_back_default():
    # grace 非数值 → 回落默认 3s：+2.9 不过期、+3.1 过期
    assert superseded_by_inbound(T0, T0 + DEFAULT_GRACE_SEC - 0.1,
                                 grace_sec="abc") is False
    assert superseded_by_inbound(T0, T0 + DEFAULT_GRACE_SEC + 0.1,
                                 grace_sec="abc") is True


def test_negative_grace_clamped_to_zero():
    assert superseded_by_inbound(T0, T0 + 0.1, grace_sec=-5) is True
    assert superseded_by_inbound(T0, T0, grace_sec=-5) is False


# ── parse_fresh_guard_cfg ────────────────────────────────────────────

def test_cfg_default_off():
    for raw in (None, {}, {"inbox": {}}, {"inbox": {"l2_autosend": {}}}, "junk"):
        cfg = parse_fresh_guard_cfg(raw)
        assert cfg["enabled"] is False
        assert cfg["grace_sec"] == pytest.approx(DEFAULT_GRACE_SEC)
        assert cfg["min_text_len"] == 0


def test_cfg_full_tree_with_min_len_mirror():
    cfg = parse_fresh_guard_cfg({
        "inbox": {
            "l2_autosend": {"fresh_guard": {"enabled": True, "grace_sec": 5}},
            "auto_draft": {"min_text_len": 2},
        },
    })
    assert cfg == {"enabled": True, "grace_sec": 5.0, "min_text_len": 2}


def test_cfg_block_form():
    # worker 构造入参自解析路径：直接给 l2_autosend 子块
    cfg = parse_fresh_guard_cfg(
        {"fresh_guard": {"enabled": True, "grace_sec": 1.5}})
    assert cfg["enabled"] is True
    assert cfg["grace_sec"] == pytest.approx(1.5)
    assert cfg["min_text_len"] == 0  # 块形态拿不到全局树，按 0


def test_cfg_garbage_values():
    cfg = parse_fresh_guard_cfg({
        "inbox": {
            "l2_autosend": {"fresh_guard": {"enabled": 1, "grace_sec": "abc"}},
            "auto_draft": {"min_text_len": "junk"},
        },
    })
    assert cfg["enabled"] is True
    assert cfg["grace_sec"] == pytest.approx(DEFAULT_GRACE_SEC)
    assert cfg["min_text_len"] == 0
    # 负 grace 收口 0（任何更晚入站都算过期）
    cfg2 = parse_fresh_guard_cfg({"fresh_guard": {"grace_sec": -9}})
    assert cfg2["grace_sec"] == 0.0


# ── find_superseding_inbound ─────────────────────────────────────────

def _row(ts, text, direction="in", **kw):
    r = {"ts": ts, "text": text, "direction": direction}
    r.update(kw)
    return r


def test_find_newer_different_text_hits_newest():
    rows = [
        _row(T0 - 5, "你叫什么名字"),
        _row(T0 + 6, "现在几点了"),
        _row(T0 + 9, "在吗？怎么不说话"),
    ]
    hit = find_superseding_inbound(
        rows, draft_ts=T0, peer_text="你叫什么名字", grace_sec=3.0)
    assert hit is not None and hit["ts"] == T0 + 9


def test_find_identical_text_not_superseding():
    # 同文本 = stale_peer 幂等跳过不会重拟 → 取消旧稿即客户零回复，必须放行
    rows = [_row(T0 + 10, " 你叫什么名字 ")]  # 首尾空白也按相同处理
    assert find_superseding_inbound(
        rows, draft_ts=T0, peer_text="你叫什么名字", grace_sec=3.0) is None


def test_find_below_min_len_not_superseding():
    rows = [_row(T0 + 10, "?")]
    assert find_superseding_inbound(
        rows, draft_ts=T0, peer_text="你叫什么名字",
        grace_sec=3.0, min_text_len=2) is None
    # min_text_len=0（出货默认）→ 单字符也算插话
    assert find_superseding_inbound(
        rows, draft_ts=T0, peer_text="你叫什么名字",
        grace_sec=3.0, min_text_len=0) is not None


def test_find_media_only_empty_text_not_superseding():
    rows = [_row(T0 + 10, "", media_type="image", media_ref="x.jpg")]
    assert find_superseding_inbound(
        rows, draft_ts=T0, peer_text="你叫什么名字", grace_sec=3.0) is None


def test_find_outbound_rows_ignored():
    rows = [_row(T0 + 10, "我这边刚忙完呀", direction="out")]
    assert find_superseding_inbound(
        rows, draft_ts=T0, peer_text="你叫什么名字", grace_sec=3.0) is None


def test_find_within_grace_not_superseding():
    rows = [_row(T0 + 2.5, "现在几点了")]
    assert find_superseding_inbound(
        rows, draft_ts=T0, peer_text="你叫什么名字", grace_sec=3.0) is None


def test_find_bad_rows_never_block():
    assert find_superseding_inbound(
        None, draft_ts=T0, peer_text="x") is None
    assert find_superseding_inbound(
        [], draft_ts=T0, peer_text="x") is None
    assert find_superseding_inbound(
        ["junk", 42, None], draft_ts=T0, peer_text="x") is None
    assert find_superseding_inbound(
        [{"direction": "in", "ts": "abc", "text": "hi"}],
        draft_ts=T0, peer_text="x") is None
    # draft_ts 本身坏 → superseded_by_inbound 全 False → None
    assert find_superseding_inbound(
        [_row(T0 + 10, "现在几点了")], draft_ts=None, peer_text="x") is None


# ── AutosendWorker 集成（纯内存假件）─────────────────────────────────

class _FakeStore:
    def __init__(self, rows=None, raise_on_list=False):
        self.rows = list(rows or [])
        self.raise_on_list = raise_on_list
        self.cancelled = []  # (draft_id, status, decided_by)

    def list_recent_messages(self, conversation_id, *, limit=50, before_ts=None):
        if self.raise_on_list:
            raise RuntimeError("db exploded")
        return list(self.rows)

    def update_draft_status(self, draft_id, *, status, final_text="",
                            decided_by="", expected_statuses=("pending", "enriching")):
        self.cancelled.append((draft_id, status, decided_by))
        return True


class _FakeSvc:
    def __init__(self, store, drafts):
        self._store = store
        self._drafts = [dict(d) for d in drafts]
        self.resolved = []

    def list_drafts(self, status="pending", limit=200):
        return [dict(d) for d in self._drafts]

    def resolve_with_audit(self, draft_id, action, by=""):
        self.resolved.append(draft_id)
        return {"ok": True}


def _mk_draft(created_ts, peer_text="你叫什么名字", **kw):
    d = {
        "draft_id": "d1", "autopilot_level": "L2",
        "final_text": "我叫小婉呀～", "draft_text": "我叫小婉呀～",
        "platform": "telegram", "account_id": "tg1", "chat_key": "peer9",
        "conversation_id": "inbox:telegram:tg1:peer9",
        "created_ts": created_ts, "peer_text": peer_text,
    }
    d.update(kw)
    return d


def _mk_worker(svc, sends, config=None, **kw):
    async def _cb(platform, account_id, chat_key, text):
        sends.append((platform, account_id, chat_key, text))
        return {"ok": True}

    return AutosendWorker(
        draft_service=svc, send_callback=_cb, config=config or {}, **kw)


@pytest.mark.asyncio
async def test_worker_superseded_skips_and_cancels():
    now = time.time()
    store = _FakeStore(rows=[_row(now - 20, "现在几点了")])
    svc = _FakeSvc(store, [_mk_draft(now - 30)])
    sends = []
    w = _mk_worker(svc, sends,
                   config={"fresh_guard": {"enabled": True, "grace_sec": 3}})

    await w._tick()

    assert sends == []                    # 未投递
    assert svc.resolved == []             # 未 resolve（草稿没被标 approved）
    assert store.cancelled == [("d1", "cancelled", "superseded_by_inbound")]
    assert w.total_superseded == 1
    assert w.total_delivered == 0
    snap = w.status_snapshot()
    assert snap["total_superseded"] == 1
    assert snap["fresh_guard_enabled"] is True


@pytest.mark.asyncio
async def test_worker_skip_not_error():
    """跳过是竞态不是故障：不计 error、不推熔断器。"""
    now = time.time()
    store = _FakeStore(rows=[_row(now - 20, "现在几点了")])
    svc = _FakeSvc(store, [_mk_draft(now - 30)])
    sends = []
    w = _mk_worker(svc, sends,
                   config={"fresh_guard": {"enabled": True, "grace_sec": 3},
                           "circuit_threshold": 2})

    await w._tick()
    await w._tick()

    assert w.total_superseded == 2
    assert w.total_errors == 0
    assert w._consecutive_errors == 0
    assert w._circuit_open is False


@pytest.mark.asyncio
async def test_worker_fresh_draft_delivers():
    now = time.time()
    # 唯一入站早于拟稿（正是本稿在回的那句）→ 不过期，正常投递
    store = _FakeStore(rows=[_row(now - 40, "你叫什么名字")])
    svc = _FakeSvc(store, [_mk_draft(now - 30)])
    sends = []
    w = _mk_worker(svc, sends,
                   config={"fresh_guard": {"enabled": True, "grace_sec": 3}})

    await w._tick()

    assert len(sends) == 1
    assert svc.resolved == ["d1"]
    assert store.cancelled == []
    assert w.total_superseded == 0
    assert w.total_delivered == 1


@pytest.mark.asyncio
async def test_worker_disabled_zero_change():
    """默认关：即使存在更晚入站也照旧投递（零行为变更）。"""
    now = time.time()
    store = _FakeStore(rows=[_row(now - 20, "现在几点了")])
    svc = _FakeSvc(store, [_mk_draft(now - 30)])
    sends = []
    w = _mk_worker(svc, sends, config={})

    await w._tick()

    assert len(sends) == 1
    assert w.total_superseded == 0
    assert store.cancelled == []
    assert w.status_snapshot()["fresh_guard_enabled"] is False


@pytest.mark.asyncio
async def test_worker_guard_exception_lets_through():
    """守卫自身异常（store 查询爆炸）→ 放行投递，绝不断链。"""
    now = time.time()
    store = _FakeStore(raise_on_list=True)
    svc = _FakeSvc(store, [_mk_draft(now - 30)])
    sends = []
    w = _mk_worker(svc, sends,
                   config={"fresh_guard": {"enabled": True, "grace_sec": 3}})

    await w._tick()

    assert len(sends) == 1
    assert w.total_superseded == 0
    assert w.total_delivered == 1


@pytest.mark.asyncio
async def test_worker_identical_newer_inbound_delivers():
    """更晚但同文本的入站：不会重拟（stale_peer 幂等跳过）→ 必须照常投递。"""
    now = time.time()
    store = _FakeStore(rows=[_row(now - 20, "你叫什么名字")])
    svc = _FakeSvc(store, [_mk_draft(now - 30)])
    sends = []
    w = _mk_worker(svc, sends,
                   config={"fresh_guard": {"enabled": True, "grace_sec": 3}})

    await w._tick()

    assert len(sends) == 1
    assert w.total_superseded == 0


@pytest.mark.asyncio
async def test_worker_missing_created_ts_delivers():
    """草稿缺 created_ts → 判据基准缺失，宁可放过不误拦。"""
    now = time.time()
    store = _FakeStore(rows=[_row(now - 20, "现在几点了")])
    svc = _FakeSvc(store, [_mk_draft(0)])
    sends = []
    w = _mk_worker(svc, sends,
                   config={"fresh_guard": {"enabled": True, "grace_sec": 3}})

    await w._tick()

    assert len(sends) == 1
    assert w.total_superseded == 0


@pytest.mark.asyncio
async def test_worker_injected_cfg_wins_and_min_len_respected():
    """bootstrap 注入的 fresh_guard_cfg 优先于 config 块；min_text_len 镜像生效。"""
    now = time.time()
    # 更晚入站只有一个 "?"（低于 min_text_len=2）→ 不会触发新拟稿，必须放行
    store = _FakeStore(rows=[_row(now - 20, "?")])
    svc = _FakeSvc(store, [_mk_draft(now - 30)])
    sends = []
    w = _mk_worker(
        svc, sends,
        config={"fresh_guard": {"enabled": False}},  # 块里关
        fresh_guard_cfg={"enabled": True, "grace_sec": 3, "min_text_len": 2},
    )
    assert w.status_snapshot()["fresh_guard_enabled"] is True  # 注入赢

    await w._tick()

    assert len(sends) == 1  # min_len 豁免 → 投递
    assert w.total_superseded == 0

    # 换一条过 min_len 的插话 → 拦
    store.rows = [_row(now - 20, "现在几点了")]
    svc._drafts = [_mk_draft(now - 30)]
    await w._tick()
    assert len(sends) == 1  # 没有新投递
    assert w.total_superseded == 1


@pytest.mark.asyncio
async def test_worker_db_mark_only_mode_untouched():
    """无 send_callback（仅 DB 标记模式）：守卫不介入，resolve 照旧。"""
    now = time.time()
    store = _FakeStore(rows=[_row(now - 20, "现在几点了")])
    svc = _FakeSvc(store, [_mk_draft(now - 30)])
    w = AutosendWorker(
        draft_service=svc, send_callback=None,
        config={"fresh_guard": {"enabled": True, "grace_sec": 3}})

    await w._tick()

    assert svc.resolved == ["d1"]  # 标记链不受影响
    assert w.total_superseded == 0
    assert store.cancelled == []


# ── created_at 代龄刷新（2026-08-13 修「重拟稿被自己上一代钉死」死锁）──────────
#
# 事故（198/104 坐席机实录）：inbox 草稿按会话固定 source_id upsert，旧实现冲突
# 分支从不刷新 created_at → 行龄永远钉在该会话**第一次**拟稿时刻；本守卫拿它判
# 「入站晚于拟稿」，于是会话攒出第二条不同文本入站后，每一版新稿投递前都被判
# 过期作废（日志实锤：刚生成 1 秒的稿「草稿龄=118411.5s」），全自动永久哑火。
# 不变量：重新拟稿（显式带 created_at）＝新一代内容，行龄必须刷新；overlay
# 元数据 upsert（不带 created_at）不得动行龄——SLA/稿龄/积压巡检口径不受影响。
# （用 tmp_path 独立 sqlite，不碰仓库 config/ 与生产库。）

def test_upsert_created_at_explicit_refresh_and_metadata_preserve(tmp_path):
    from src.inbox.store import InboxStore

    store = InboxStore(tmp_path / "inbox.db")
    try:
        did = store.upsert_draft({
            "source_kind": "inbox", "source_id": "conv1",
            "conversation_id": "conv1", "platform": "telegram",
            "peer_text": "第一条", "draft_text": "第一代回复",
            "status": "pending", "created_at": T0,
        })
        assert float(store.get_draft(did)["created_at"]) == pytest.approx(T0)

        # overlay 元数据回写（不带 created_at）→ 行龄保持第一代
        store.upsert_draft({
            "source_kind": "inbox", "source_id": "conv1",
            "risk_level": "medium", "status": "pending",
        })
        assert float(store.get_draft(did)["created_at"]) == pytest.approx(T0)

        # 重新拟稿（显式带 created_at）→ 行龄刷新到新一代
        store.upsert_draft({
            "source_kind": "inbox", "source_id": "conv1",
            "conversation_id": "conv1", "peer_text": "第二条不同的话",
            "draft_text": "第二代回复", "status": "pending",
            "created_at": T0 + 500,
        })
        assert float(store.get_draft(did)["created_at"]) == pytest.approx(T0 + 500)
    finally:
        store.close()


def test_regenerated_draft_not_pinned_to_first_generation(tmp_path):
    """死锁回归钉：老会话行重拟后 created_at 必须是本代时刻。

    复刻线上时序——第一代很久以前、客户连发两条不同的话触发 stale_peer 重拟：
    修复前新稿沿用第一代 created_at，守卫把「上一条已被本稿覆盖的旧入站」判成
    超越性插话 → 作废本稿 → 会话从此永远发不出；修复后本代时刻新鲜，
    拟稿前的旧入站不再构成过期，真正的拟稿后插话仍要拦得住。
    """
    from src.inbox.store import InboxStore
    from src.inbox.drafts import DraftService

    store = InboxStore(tmp_path / "inbox.db")
    try:
        svc = DraftService(inbox_store=store)
        conv = {"conversation_id": "telegram:acc:peer1", "platform": "telegram",
                "account_id": "acc", "chat_key": "peer1"}
        d1 = svc.auto_generate_draft(conv, "第一条问题", automation_mode="auto_ai")
        assert d1
        # 把行龄倒填成很久以前（等价于线上跑了两天的会话行）
        store.upsert_draft({
            "source_kind": "inbox", "source_id": "telegram:acc:peer1",
            "created_at": T0, "status": "pending",
        })
        assert float(store.get_draft(d1)["created_at"]) == pytest.approx(T0)

        # 客户又发了不同的话 → stale_peer 作废旧稿 + 同一行重拟
        d2 = svc.auto_generate_draft(conv, "第二条不同的话", automation_mode="auto_ai")
        assert d2 == d1  # 每会话幂等键：同一行
        gen_ts = float(store.get_draft(d2)["created_at"])
        assert time.time() - gen_ts < 30, "重拟必须刷新 created_at，否则守卫死锁"

        # 守卫视角：拟稿**之前**的旧入站（已被本稿覆盖）不构成过期……
        rows_pre = [
            _row(gen_ts - 60, "第一条问题"),
            _row(gen_ts - 1, "第二条不同的话"),
        ]
        assert find_superseding_inbound(
            rows_pre, draft_ts=gen_ts, peer_text="第二条不同的话",
            grace_sec=3.0) is None
        # ……而拟稿**之后**的真插话仍要拦得住（守卫本职不受修复影响）
        rows_post = rows_pre + [_row(gen_ts + 10, "第三条又来了")]
        hit = find_superseding_inbound(
            rows_post, draft_ts=gen_ts, peer_text="第二条不同的话",
            grace_sec=3.0)
        assert hit is not None and hit["ts"] == gen_ts + 10
    finally:
        store.close()
