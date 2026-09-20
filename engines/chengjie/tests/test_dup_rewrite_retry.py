# -*- coding: utf-8 -*-
"""防复读拦截后「换个说法」重试门禁（impl85 阶段3，工单#45 taihua009 02:11 实录）。

事故：客户 23 秒连发三条 → 合并重生成的回复与 89 秒前已发内容相似度 98% →
dup-guard 拦得对，但拦下后没有补救，这一轮空过（客户干等到 02:15 再开口才有回）。
承诺修法：拦截后用已发内容当负样本换说法重写一条，**重写稿再过同一守卫**，
通过才发；仍雷同/重写失败维持静默跳过（守卫只更严不放松）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from src.inbox.outbound_dup_guard import (  # noqa: E402
    attach_rewrite_fn,
    attempt_dup_rewrite,
    build_dup_rewrite_prompt,
    dup_guard_metrics_snapshot,
    resolve_guard_cfg,
)

NOW = 1_000_000.0
SENT = "Haha, that's true. Anyway how was your day today my friend?"
ROWS = [{"direction": "out", "ts": NOW - 60.0, "text": SENT}]
CFG = {"enabled": True, "window_sec": 180.0, "block_similar": True,
       "rewrite_retry": True}
HIT = {"level": "similar", "matched_text": SENT, "similarity": 0.98}
FRESH = "明天要是有空，我们去海边走走看看日落怎么样呀"


def _calls(fn):
    calls = []

    async def _wrapped(text, matched):
        calls.append((text, matched))
        return await fn(text, matched) if callable(fn) else fn
    return _wrapped, calls


async def test_rewrite_success_passes_guard_and_returns_text():
    async def rw(text, matched):
        assert matched == SENT           # 负样本必须是「刚发过的内容」
        return FRESH

    before = dict(dup_guard_metrics_snapshot().get("rewrite") or {})
    out = await attempt_dup_rewrite(
        text=SENT + " again", hit=HIT, rows=ROWS, cfg=CFG,
        rewrite_fn=rw, source="test", now=NOW)
    assert out == FRESH
    after = dup_guard_metrics_snapshot()["rewrite"]
    assert after.get("rescued", 0) == before.get("rescued", 0) + 1


async def test_rewrite_still_dup_keeps_skipping():
    async def rw(text, matched):
        return SENT                      # 重写稿与已发逐字同 → 再核必拦

    out = await attempt_dup_rewrite(
        text=SENT + " again", hit=HIT, rows=ROWS, cfg=CFG,
        rewrite_fn=rw, now=NOW)
    assert out is None
    assert dup_guard_metrics_snapshot()["rewrite"].get("still_dup", 0) >= 1


async def test_rewrite_failure_modes_all_keep_skipping():
    async def boom(text, matched):
        raise RuntimeError("llm down")

    async def empty(text, matched):
        return "   "

    async def same(text, matched):
        return text + "!!"               # 归一化后与原文相同（标点被剥）

    async def overlong(text, matched):
        return "x" * 2000

    for fn in (boom, empty, same, overlong):
        out = await attempt_dup_rewrite(
            text=SENT + " again", hit=HIT, rows=ROWS, cfg=CFG,
            rewrite_fn=fn, now=NOW)
        assert out is None, fn.__name__


async def test_rewrite_disabled_or_missing_fn_short_circuits():
    called = []

    async def rw(text, matched):
        called.append(1)
        return FRESH

    cfg_off = dict(CFG, rewrite_retry=False)
    assert await attempt_dup_rewrite(
        text=SENT, hit=HIT, rows=ROWS, cfg=cfg_off, rewrite_fn=rw, now=NOW) is None
    assert called == []                  # 关闭时绝不烧 LLM
    assert await attempt_dup_rewrite(
        text=SENT, hit=HIT, rows=ROWS, cfg=CFG, rewrite_fn=None, now=NOW) is None


def test_prompt_carries_negative_sample_and_language_pin():
    p = build_dup_rewrite_prompt("你今天过得怎么样呀朋友")
    assert "你今天过得怎么样呀朋友" in p
    assert "相同的语言" in p            # 防小模型换语言（外语会话改写成中文=穿帮）
    assert "只输出" in p


def test_resolve_cfg_rewrite_retry_default_on_with_kill_switch():
    assert resolve_guard_cfg({}).get("rewrite_retry") is True
    off = resolve_guard_cfg(
        {"inbox": {"outbound_dup_guard": {"rewrite_retry": False}}})
    assert off.get("rewrite_retry") is False


async def test_attach_rewrite_fn_wraps_ai_client():
    seen = {}

    class FakeAI:
        async def rewrite_local(self, system_prompt, user_text, **kw):
            seen["prompt"] = system_prompt
            seen["text"] = user_text
            return FRESH

    cfg = attach_rewrite_fn(dict(CFG), FakeAI())
    assert callable(cfg.get("rewrite_fn"))
    out = await cfg["rewrite_fn"]("hello world my friend", "matched-sample")
    assert out == FRESH
    assert "matched-sample" in seen["prompt"]
    assert seen["text"] == "hello world my friend"
    # ai_client 缺席 / 无 rewrite_local → 原样返回（旧行为）
    assert attach_rewrite_fn(dict(CFG), None).get("rewrite_fn") is None
    assert attach_rewrite_fn(dict(CFG), object()).get("rewrite_fn") is None
    # 不改调用方传入的 dict
    base = dict(CFG)
    attach_rewrite_fn(base, FakeAI())
    assert "rewrite_fn" not in base


# ── B 线 worker 行为（_try_dup_rewrite 直测） ─────────────────────────────────

class _FakeStore:
    def list_recent_messages(self, conv, limit=8):
        return list(ROWS)


class _FakeSvc:
    _store = _FakeStore()


def _worker(cfg):
    from src.inbox.autosend_worker import AutosendWorker
    return AutosendWorker(draft_service=_FakeSvc(),
                          config={"enabled": False}, dup_guard_cfg=cfg)


async def test_worker_try_dup_rewrite_success_and_counterless_on_fail():
    async def rw(text, matched):
        return FRESH

    w = _worker(dict(CFG, rewrite_fn=rw))
    item = {"conversation_id": "telegram:default:42"}
    out = await w._try_dup_rewrite(item, SENT + " again", HIT)
    assert out == FRESH

    w2 = _worker(dict(CFG))              # 未注入 rewrite_fn → 旧行为（跳过）
    assert await w2._try_dup_rewrite(item, SENT + " again", HIT) is None


# ── 接线 ratchet（两条链 + 两个装配点） ───────────────────────────────────────

def test_wiring_ratchet():
    tg = (REPO / "src/client/telegram_client.py").read_text(
        encoding="utf-8", errors="replace")
    assert "attempt_dup_rewrite" in tg
    assert "reply_final = _dg_rw" in tg          # 重写稿必须真的替换待发文本

    worker = (REPO / "src/inbox/autosend_worker.py").read_text(
        encoding="utf-8", errors="replace")
    assert "_try_dup_rewrite" in worker
    assert "total_dup_rewritten" in worker       # 得救数可观测

    helpers = (REPO / "src/inbox/autosend_helpers.py").read_text(
        encoding="utf-8", errors="replace")
    assert "attach_rewrite_fn" in helpers        # 热接线装配点
    boot = (REPO / "src/bootstrap/web_app.py").read_text(
        encoding="utf-8", errors="replace")
    assert "attach_rewrite_fn" in boot           # bootstrap 装配点
