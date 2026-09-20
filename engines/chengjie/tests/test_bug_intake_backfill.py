# -*- coding: utf-8 -*-
"""实施74 阶段3 门禁（B123）：报障群断线补拉。

事故金标（0827 凌晨）：117 离线期报障群 03:32/03:36 两条消息漏登记。
本文件钉住：首见群不回放（防历史回灌）、差集回放升序、cap 截断水位不跳档、
自家/纯媒体行为剔除、关闭态零动作、拉取失败不动水位。
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Dict, List

import pytest

from src.ops import bug_intake_backfill as bfm
from src.ops.bug_intake_backfill import (
    is_seen,
    load_state,
    mark_seen,
    max_row_id,
    parse_backfill_cfg,
    plan_replay,
    run_backfill_once,
    save_state,
    seen_count,
)

_ENGINE_ROOT = Path(__file__).resolve().parents[1]

_CFG = {"bug_intake": {"enabled": True, "groups": ["-1004345824259"]}}


@pytest.fixture(autouse=True)
def _isolated_seen(tmp_path, monkeypatch):
    """已观察 mid 集每测独立落 tmp（与水位账本同目录），跨测零串味。"""
    monkeypatch.setattr(bfm, "_SEEN_PATH_OVERRIDE", tmp_path / "bug_intake_seen.json")
    bfm.reset_seen_for_tests()
    yield
    bfm.reset_seen_for_tests()


def _rows():
    """0827 断线窗口的简化重演：100 已见；101/102 = 漏网两条；103 自家；104 纯媒体。"""
    return [
        {"id": 104, "text": "", "has_media": True, "reporter_id": "u9",
         "reporter_name": "skuio", "outgoing": False},
        {"id": 103, "text": "收到，我们看下", "reporter_id": "me",
         "reporter_name": "值守", "outgoing": True},
        {"id": 102, "text": "另外语音也发不了", "reporter_id": "u9",
         "reporter_name": "skuio", "outgoing": False},
        {"id": 101, "text": "发图还是失败，报错截图稍后", "reporter_id": "u9",
         "reporter_name": "skuio", "outgoing": False},
        {"id": 100, "text": "昨天那个问题", "reporter_id": "u9",
         "reporter_name": "skuio", "outgoing": False},
    ]


def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


# ── 纯函数 ───────────────────────────────────────────────────────────────────

def test_plan_replay_first_seen_returns_nothing():
    assert plan_replay(_rows(), None) == []


def test_plan_replay_diff_ascending_skips_outgoing_and_media():
    got = plan_replay(_rows(), 100)
    assert [r["id"] for r in got] == [101, 102]


def test_plan_replay_cap_keeps_oldest():
    got = plan_replay(_rows(), 100, cap=1)
    assert [r["id"] for r in got] == [101]


def test_max_row_id():
    assert max_row_id(_rows()) == 104
    assert max_row_id([]) == 0


def test_parse_cfg_gates():
    assert parse_backfill_cfg(_CFG)["enabled"] is True
    assert parse_backfill_cfg({})["enabled"] is False
    assert parse_backfill_cfg(
        {"bug_intake": {"enabled": True, "groups": []}})["enabled"] is False
    assert parse_backfill_cfg(
        {"bug_intake": {"enabled": False, "groups": ["1"]}})["enabled"] is False
    assert parse_backfill_cfg(
        {"bug_intake": {"enabled": True, "groups": ["1"],
                        "backfill": {"enabled": False}}})["enabled"] is False
    got = parse_backfill_cfg(
        {"bug_intake": {"enabled": True, "groups": ["1"],
                        "backfill": {"interval_sec": 5, "cap": 999}}})
    assert got["interval_sec"] == 60 and got["cap"] == 100  # 夹紧


# ── 端到端（假 fetch/observe + tmp 账本）────────────────────────────────────

def _fetcher(rows: List[Dict[str, Any]], fail: bool = False):
    async def _f(chat_id: str, cap: int):
        if fail:
            raise RuntimeError("net down")
        return rows
    return _f


def test_first_seen_marks_watermark_without_replay(tmp_path):
    sp = tmp_path / "state.json"
    seen: List[Dict[str, Any]] = []

    def _obs(cfg, **kw):
        seen.append(kw)
        return {}

    s = _run(run_backfill_once(_CFG, _fetcher(_rows()), observe=_obs,
                               state_path=sp))
    assert s["first_seen"] == 1 and s["replayed"] == 0 and not seen
    assert load_state(sp)["-1004345824259"]["last_msg_id"] == 104


def test_missed_rows_replayed_ascending_and_watermark_advances(tmp_path):
    sp = tmp_path / "state.json"
    save_state({"-1004345824259": {"last_msg_id": 100, "ts": 1.0}}, sp)
    seen: List[Dict[str, Any]] = []

    def _obs(cfg, **kw):
        seen.append(kw)
        return {}

    s = _run(run_backfill_once(_CFG, _fetcher(_rows()), observe=_obs,
                               state_path=sp, account_id="6834964252"))
    assert s["replayed"] == 2
    assert [k["text"] for k in seen] == ["发图还是失败，报错截图稍后", "另外语音也发不了"]
    assert seen[0]["chat_id"] == "-1004345824259"
    assert seen[0]["account_id"] == "6834964252"
    assert s["media_skipped"] == 1  # 104 纯媒体行如实计数不装完整
    assert load_state(sp)["-1004345824259"]["last_msg_id"] == 104


def test_cap_truncation_does_not_skip_watermark(tmp_path):
    """cap 截断时水位只推到已回放最大 id——剩余下一轮继续，绝不跳档丢单。"""
    sp = tmp_path / "state.json"
    save_state({"-1004345824259": {"last_msg_id": 100, "ts": 1.0}}, sp)
    cfg = {"bug_intake": {"enabled": True, "groups": ["-1004345824259"],
                          "backfill": {"cap": 1}}}
    replayed: List[int] = []

    def _obs(c, **kw):
        replayed.append(kw["text"])
        return {}

    s = _run(run_backfill_once(cfg, _fetcher(_rows()), observe=_obs,
                               state_path=sp))
    assert s["replayed"] == 1
    assert load_state(sp)["-1004345824259"]["last_msg_id"] == 101
    # 第 2 轮补 102；满载轮（len==cap）保守只推到已回放 id，不跳到 top
    s2 = _run(run_backfill_once(cfg, _fetcher(_rows()), observe=_obs,
                                state_path=sp))
    assert s2["replayed"] == 1
    assert load_state(sp)["-1004345824259"]["last_msg_id"] == 102
    # 第 3 轮无漏网（103 自家/104 纯媒体不回放）→ 水位收口到 top
    s3 = _run(run_backfill_once(cfg, _fetcher(_rows()), observe=_obs,
                                state_path=sp))
    assert s3["replayed"] == 0
    assert load_state(sp)["-1004345824259"]["last_msg_id"] == 104


def test_disabled_config_is_noop(tmp_path):
    sp = tmp_path / "state.json"

    def _obs(c, **kw):
        raise AssertionError("不该被调")

    s = _run(run_backfill_once({}, _fetcher(_rows()), observe=_obs,
                               state_path=sp))
    assert s == {"groups": 0, "replayed": 0, "media_skipped": 0,
                 "first_seen": 0, "seen_skipped": 0}
    assert not sp.exists()


def test_fetch_failure_leaves_watermark_untouched(tmp_path):
    sp = tmp_path / "state.json"
    save_state({"-1004345824259": {"last_msg_id": 100, "ts": 1.0}}, sp)

    def _obs(c, **kw):
        raise AssertionError("不该被调")

    s = _run(run_backfill_once(_CFG, _fetcher([], fail=True), observe=_obs,
                               state_path=sp))
    assert s["replayed"] == 0
    assert load_state(sp)["-1004345824259"]["last_msg_id"] == 100


def test_observe_failure_skips_row_but_keeps_going(tmp_path):
    sp = tmp_path / "state.json"
    save_state({"-1004345824259": {"last_msg_id": 100, "ts": 1.0}}, sp)
    calls: List[str] = []

    def _obs(c, **kw):
        calls.append(kw["text"])
        if len(calls) == 1:
            raise RuntimeError("观察链异常")
        return {}

    s = _run(run_backfill_once(_CFG, _fetcher(_rows()), observe=_obs,
                               state_path=sp))
    assert len(calls) == 2 and s["replayed"] == 1
    # 首条失败但次条成功 → 水位仍推进（无漏网剩余时推到 top）
    assert load_state(sp)["-1004345824259"]["last_msg_id"] == 104


# ── J-5 A：已观察 mid 集（实时链 × 回放链共享）────────────────────────────────

def test_seen_ledger_roundtrip_and_persistence(tmp_path):
    assert not is_seen("-1", 5)
    assert mark_seen("-1", 5, now=10.0) is True
    assert mark_seen("-1", 5, now=11.0) is False        # 幂等：二次登记不算新
    assert is_seen("-1", 5)
    assert not is_seen("-1", 6)
    # 无效入参绝不抛也不登记
    assert mark_seen("", 5) is False and mark_seen("-1", 0) is False
    assert mark_seen("-1", "x") is False
    # 跨进程：清缓存后从盘上重读
    bfm.reset_seen_for_tests()
    assert is_seen("-1", 5)
    assert seen_count("-1") == 1
    assert (tmp_path / "bug_intake_seen.json").is_file()


def test_seen_ledger_bounded_drops_oldest(monkeypatch):
    monkeypatch.setattr(bfm, "_SEEN_MAX_PER_CHAT", 3)
    for m in (10, 11, 12, 13):
        mark_seen("-1", m, now=1.0)
    assert seen_count("-1") == 3
    assert not is_seen("-1", 10) and is_seen("-1", 13)


def test_realtime_seen_rows_are_not_replayed(tmp_path):
    """0904 21:0x 实录形态：实时链见过 101（被限频压下），backfill 不得再重喂；
    102 真漏网照常回放；水位照推到 top。"""
    sp = tmp_path / "state.json"
    save_state({"-1004345824259": {"last_msg_id": 100, "ts": 1.0}}, sp)
    mark_seen("-1004345824259", 101, now=2.0)
    calls: List[Dict[str, Any]] = []

    def _obs(c, **kw):
        calls.append(kw)
        return {}

    s = _run(run_backfill_once(_CFG, _fetcher(_rows()), observe=_obs,
                               state_path=sp))
    assert s["replayed"] == 1 and s["seen_skipped"] == 1
    assert [k["text"] for k in calls] == ["另外语音也发不了"]
    assert calls[0]["msg_id"] == 102                     # mid 透传 observe
    assert is_seen("-1004345824259", 102)                # 回放成功也登记
    assert load_state(sp)["-1004345824259"]["last_msg_id"] == 104


def test_replayed_rows_not_replayed_twice_when_watermark_lags(tmp_path):
    """水位没推进（如进程在 save_state 前崩）→ 下一轮同样的差集，已观察集兜住。"""
    sp = tmp_path / "state.json"
    save_state({"-1004345824259": {"last_msg_id": 100, "ts": 1.0}}, sp)
    calls: List[int] = []

    def _obs(c, **kw):
        calls.append(kw["msg_id"])
        return {}

    _run(run_backfill_once(_CFG, _fetcher(_rows()), observe=_obs, state_path=sp))
    save_state({"-1004345824259": {"last_msg_id": 100, "ts": 1.0}}, sp)  # 模拟水位回退
    s2 = _run(run_backfill_once(_CFG, _fetcher(_rows()), observe=_obs,
                                state_path=sp))
    assert calls == [101, 102]
    assert s2["replayed"] == 0 and s2["seen_skipped"] == 2


def test_reporter_name_joins_last_name_like_realtime_chain(tmp_path):
    """0904 实录：实时链记「skuio 花无缺」、backfill 记「skuio」→ 行带 last_name
    时拼全名；无 last_name 原样。"""
    sp = tmp_path / "state.json"
    save_state({"-1004345824259": {"last_msg_id": 100, "ts": 1.0}}, sp)
    rows = _rows()
    for r in rows:
        if r["id"] == 101:
            r["reporter_last_name"] = "花无缺"
    names: List[str] = []

    def _obs(c, **kw):
        names.append(kw["reporter_name"])
        return {}

    _run(run_backfill_once(_CFG, _fetcher(rows), observe=_obs, state_path=sp))
    assert names == ["skuio 花无缺", "skuio"]
    assert bfm._row_reporter_name(
        {"reporter_name": "skuio 花无缺", "reporter_last_name": "花无缺"}
    ) == "skuio 花无缺"                                    # 已含姓不重复拼


# ── 接线契约（静态）─────────────────────────────────────────────────────────

def test_telegram_client_wired():
    src = (_ENGINE_ROOT / "src" / "client" / "telegram_client.py").read_text(
        encoding="utf-8")
    assert "def _bug_intake_backfill_loop(" in src
    assert "create_task(self._bug_intake_backfill_loop())" in src
