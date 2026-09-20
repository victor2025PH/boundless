# -*- coding: utf-8 -*-
"""专属歌订单库 + 定制意图检测契约（实施58 P2）。"""
from __future__ import annotations

import time
from pathlib import Path

from src.companion.song_orders import (
    SongOrderStore,
    detect_custom_song_request,
    resolve_custom_cfg,
)


def _store(tmp_path: Path) -> SongOrderStore:
    return SongOrderStore(path=tmp_path / "orders.db")


def _mk(store, **kw):
    base = dict(platform="telegram", account_id="a1", chat_key="c1",
                persona_id="chen_meiling", voice_key="warm_f",
                peer_name="阿泽", request_text="给我写首歌",
                facts=["我上周去了海边", "最近在学吉他"])
    base.update(kw)
    return store.create_order(**base)


# ── 意图检测 ─────────────────────────────────────────────────────────────────

def test_detect_positive():
    for t in ("给我写首歌吧", "帮我写一首歌", "能不能作一首关于我们的歌",
              "唱一首属于我们的歌", "write me a song please",
              "把我们的故事写进歌里"):
        assert detect_custom_song_request(t), t


def test_detect_negative():
    for t in ("别写歌了", "唱什么唱", "我在写歌", "今天天气不错",
              "你会唱歌吗", "唱首歌听听"):
        assert not detect_custom_song_request(t), t


# ── 状态机 ───────────────────────────────────────────────────────────────────

def test_create_and_dedup_active(tmp_path):
    s = _store(tmp_path)
    oid = _mk(s)
    assert oid
    assert _mk(s) is None                      # 同会话活单去重
    assert _mk(s, chat_key="c2")               # 另一会话可下单
    row = s.get(oid)
    assert row["status"] == "pending"
    assert row["conv_id"] == "telegram:a1:c1"
    assert "海边" in row["facts_json"]


def test_daily_cap(tmp_path):
    s = _store(tmp_path)
    assert _mk(s, chat_key="c1", daily_cap=2)
    assert _mk(s, chat_key="c2", daily_cap=2)
    assert _mk(s, chat_key="c3", daily_cap=2) is None
    assert _mk(s, chat_key="c3", daily_cap=0)  # 0=不限帽


def test_claim_is_atomic_and_fifo(tmp_path):
    s = _store(tmp_path)
    o1 = _mk(s, chat_key="c1")
    _mk(s, chat_key="c2")
    got = s.claim_next_pending()
    assert got["id"] == o1
    assert s.get(o1)["status"] == "rendering"
    got2 = s.claim_next_pending()
    assert got2["id"] != o1


def test_full_lifecycle(tmp_path):
    s = _store(tmp_path)
    oid = _mk(s)
    s.claim_next_pending()
    assert s.set_review(oid, lyrics="四句\n四句\n四句\n四句",
                        take={"seed": 1}, audio_path="x.ogg")
    assert s.get(oid)["status"] == "review"
    # 双窗口并发处置：只有一方成功
    assert s.resolve_review(oid, to_status="delivered")
    assert not s.resolve_review(oid, to_status="rejected")
    assert s.get(oid)["status"] == "delivered"
    assert s.get(oid)["delivered_ts"] > 0


def test_reject_and_fail_paths(tmp_path):
    s = _store(tmp_path)
    o1 = _mk(s, chat_key="c1")
    s.claim_next_pending()
    s.set_review(o1, lyrics="x", take={}, audio_path="a.ogg")
    assert s.resolve_review(o1, to_status="rejected", fail_reason="不像")
    o2 = _mk(s, chat_key="c2")
    s.claim_next_pending()
    assert s.set_failed(o2, "render_failed")
    assert s.get(o2)["fail_reason"] == "render_failed"
    # 终态不可再 fail
    assert not s.set_failed(o1, "late")


def test_stale_active_expires_and_unblocks(tmp_path):
    s = _store(tmp_path)
    old = time.time() - 4 * 86400
    oid = _mk(s, now=old)
    assert oid
    # 4 天后同会话再下单：旧活单自动过期，新单放行
    oid2 = _mk(s)
    assert oid2 and oid2 != oid
    assert s.get(oid)["status"] == "failed"
    assert s.get(oid)["fail_reason"] == "stale_expired"


def test_counts_and_list(tmp_path):
    s = _store(tmp_path)
    _mk(s, chat_key="c1")
    _mk(s, chat_key="c2")
    s.claim_next_pending()
    c = s.counts()
    assert c.get("pending") == 1 and c.get("rendering") == 1
    rows = s.list_orders(status="pending")
    assert len(rows) == 1


def test_retry_only_from_failed(tmp_path):
    s = _store(tmp_path)
    oid = _mk(s)
    assert not s.retry(oid)                    # pending 不可重试
    s.claim_next_pending()
    s.set_failed(oid, "render_failed")
    assert s.retry(oid)
    row = s.get(oid)
    assert row["status"] == "pending" and row["fail_reason"] == ""
    # 重拾后可再走全生命周期
    got = s.claim_next_pending()
    assert got["id"] == oid


def test_set_failed_keeps_meta_in_take(tmp_path):
    s = _store(tmp_path)
    oid = _mk(s)
    s.claim_next_pending()
    s.set_failed(oid, "lyrics_rejected", take={"attempts": [{"n": 1}]})
    row = s.get(oid)
    assert row["fail_reason"] == "lyrics_rejected"        # 短码
    assert "attempts" in row["take_json"]                 # 细节留档


def test_delete_only_final_states(tmp_path):
    s = _store(tmp_path)
    o1 = _mk(s, chat_key="c1")
    assert s.delete_order(o1) is None          # 活单拒删
    s.claim_next_pending()
    s.set_review(o1, lyrics="x", take={}, audio_path="a.ogg")
    assert s.delete_order(o1) is None          # review 也算活单
    s.resolve_review(o1, to_status="rejected")
    assert s.delete_order(o1) == "a.ogg"       # 终态可删，回音频路径
    assert s.get(o1) is None


def test_resolve_custom_cfg_defaults():
    assert resolve_custom_cfg({}) == {
        "enabled": False, "review": True, "daily_orders_cap": 10}
    got = resolve_custom_cfg({"companion": {"singing": {"custom": {
        "enabled": True, "review": False, "daily_orders_cap": 3}}}})
    assert got == {"enabled": True, "review": False, "daily_orders_cap": 3}
