# -*- coding: utf-8 -*-
"""#171「已发假声明」守卫接线门禁（2026-09-05，WhatsApp Mizuki→John 实录）。

实录：AI「Oh, sorry — I just sent it, you should have it now.」→ 客户「I never got
it」→ AI「Hmm, that's weird — let me try sending it again for you.」全程零
send_media，真图 23 分钟后才到。词表层在 ``test_outbound_promise_guard.py``；
本文件守**真伪判据 + 两条链的接线**：

- ``media_pending.media_sent_within``（A 线 ``_media_sent_log`` 近窗判据）；
- ``autosend_helpers._media_sent_recently``（B 线 inbox 出站镜像近窗判据）；
- ``_depromise_autosend_text(sent_claim=True)`` 正则回落剥假声明 + 如实兜底；
- A 线 ``_apply_media_promise_guard``：近窗无真发 → 剥/兜底 + sent_claim 计数；
  近窗真发过 → 「刚发了」是真话原样放行；开关可关。
"""
from __future__ import annotations

import logging
import time
from types import SimpleNamespace

import pytest

from src.ai import media_pending as mp
from src.inbox import image_autosend as ia
from src.inbox.autosend_helpers import (
    _depromise_autosend_text,
    _media_sent_recently,
)

_SMcls = __import__(
    "src.skills.skill_manager", fromlist=["SkillManager"]
).SkillManager


class _SM:
    _promise_guard_cfg = _SMcls._promise_guard_cfg
    _apply_media_promise_guard = _SMcls._apply_media_promise_guard

    def __init__(self, pg_cfg=None):
        comp = {"selfie": {"enabled": True, "provider": {"enabled": False}}}
        if pg_cfg is not None:
            comp["media_promise_guard"] = pg_cfg
        self.config = SimpleNamespace(config={"companion": comp})
        self.logger = logging.getLogger("test_sent_claim")
        self.ai_client = None


_REAL1 = "Oh, sorry — I just sent it, you should have it now."
_REAL2 = "Hmm, that's weird — let me try sending it again for you."


# ── A 线判据：_media_sent_log 近窗 ─────────────────────────────────────────────
def test_media_sent_within_reads_media_log():
    now = 1_000_000.0
    assert mp.media_sent_within({}, 1800, now=now) is False
    ctx = {"_media_sent_log": [{"ts": now - 600, "note": "[图片] x"}]}
    assert mp.media_sent_within(ctx, 1800, now=now) is True     # 10 分钟前真发过
    assert mp.media_sent_within(ctx, 300, now=now) is False     # 窗口 5 分钟 → 超窗
    assert mp.last_media_sent_ts(ctx) == now - 600
    # 脏数据零抛
    assert mp.media_sent_within({"_media_sent_log": "junk"}, 1800, now=now) is False


# ── B 线判据：inbox 出站镜像近窗 ────────────────────────────────────────────────
class _Store:
    def __init__(self, rows):
        self.rows = rows

    def list_recent_messages(self, cid, limit=20):
        return list(self.rows)


def _assistant(rows):
    return SimpleNamespace(
        inbox_store=_Store(rows), logger=logging.getLogger("t"), ai_client=None)


def test_media_sent_recently_b_line():
    now = 2_000_000.0
    rows = [
        {"direction": "in", "text": "send me a pic", "ts": now - 900},
        {"direction": "out", "text": "[图片]", "media_type": "image", "ts": now - 600},
        {"direction": "out", "text": "hi", "ts": now - 100},
    ]
    a = _assistant(rows)
    assert _media_sent_recently(a, "whatsapp", "acc", "peer", kind="image",
                                window_sec=1800, now=now) is True
    assert _media_sent_recently(a, "whatsapp", "acc", "peer", kind="image",
                                window_sec=300, now=now) is False
    # 语音轨看 voice/audio，不认图片
    assert _media_sent_recently(a, "whatsapp", "acc", "peer", kind="voice",
                                window_sec=1800, now=now) is False
    # 客户发的图不算 AI 发过
    rows2 = [{"direction": "in", "media_type": "image", "ts": now - 60}]
    assert _media_sent_recently(_assistant(rows2), "whatsapp", "acc", "peer",
                                kind="image", window_sec=1800, now=now) is False
    # 无 store / 取数异常 → False（宁可多拦）
    assert _media_sent_recently(SimpleNamespace(inbox_store=None), "w", "a", "p") is False


# ── B 线撤回：正则回落（无 LLM）剥假声明 + 如实兜底 ────────────────────────────
@pytest.mark.asyncio
async def test_depromise_sent_claim_regex_fallback_and_honest_deflection():
    a = _assistant([])
    # 整条都是假声明 → 如实兜底（不是「卖关子」）
    out = await _depromise_autosend_text(a, _REAL1, "image", sent_claim=True)
    assert "sent it" not in out and "should have it" not in out
    assert "didn't" in out and "curious" not in out
    out2 = await _depromise_autosend_text(a, _REAL2, "image", sent_claim=True)
    assert "again" not in out2 and out2.strip()
    # 混合句：剥假声明留其余
    out3 = await _depromise_autosend_text(
        a, _REAL1 + " Anyway, how was your day?", "image", sent_claim=True)
    assert "how was your day" in out3 and "sent it" not in out3
    # sent_claim=False 维持旧行为：不吃语境的假声明句不会被剥（由调用方判定后才传 True）
    out4 = await _depromise_autosend_text(a, _REAL1, "image", sent_claim=False)
    assert out4 == _REAL1


# ── A 线接线：无真发→剥/兜底+计数；真发过→放行；开关可关 ───────────────────────
def _snap(key):
    return int(ia.metrics_snapshot().get(key, 0) or 0)


def test_a_line_sent_claim_retracted_when_no_recent_media():
    sm = _SM()
    ctx = {}   # 无 _media_sent_log → 近窗无真发
    d0, r0 = _snap("sent_claim_detected"), _snap("sent_claim_retracted")
    out = sm._apply_media_promise_guard(_REAL1, ctx, user_text="I never got it")
    assert "sent it" not in out and "should have it" not in out
    assert out.strip() and "curious" not in out          # 如实兜底，不卖关子
    assert _snap("sent_claim_detected") == d0 + 1
    assert _snap("sent_claim_retracted") == r0 + 1
    assert ctx.get("_photo_promise_streak") == 1        # 空头计数照累（P1 熔断）
    # 实录②：补发承诺预设「发过」→ 同样剥
    out2 = sm._apply_media_promise_guard(_REAL2, {}, user_text="I never got it")
    assert "again" not in out2 and out2.strip()
    # 混合句只剥假声明句
    out3 = sm._apply_media_promise_guard(
        _REAL1 + " Anyway, how was your day?", {}, user_text="hmm")
    assert "how was your day" in out3 and "sent it" not in out3


def test_a_line_sent_claim_is_truth_when_media_sent_recently():
    sm = _SM()
    ctx = {"_media_sent_log": [{"ts": time.time() - 120, "note": "[图片]"}]}
    d0 = _snap("sent_claim_detected")
    # 2 分钟前真发过图 → 「我刚发给你了」是真话，一字不动
    assert sm._apply_media_promise_guard("我刚发给你了呀，看看～", ctx) == "我刚发给你了呀，看看～"
    assert sm._apply_media_promise_guard(_REAL1, ctx) == _REAL1
    assert _snap("sent_claim_detected") == d0
    # 窄窗（1 分钟）→ 2 分钟前的真发不算 → 判谎
    sm2 = _SM(pg_cfg={"sent_claim": {"window_min": 1}})
    out = sm2._apply_media_promise_guard(_REAL1, dict(ctx))
    assert out != _REAL1 and "sent it" not in out


def test_a_line_sent_claim_switch_off_and_promise_path_untouched():
    # 子开关关 → 旧行为（假声明原样放行——由运营显式选择）
    sm = _SM(pg_cfg={"sent_claim": {"enabled": False}})
    assert sm._apply_media_promise_guard(_REAL1, {}) == _REAL1
    # 将发承诺仍走 promise 计数，不串到 sent_claim 桶
    sm2 = _SM()
    p0, s0 = _snap("promise_detected"), _snap("sent_claim_detected")
    out = sm2._apply_media_promise_guard("等我拍一张给你哈～", {})
    assert "拍一张" not in out
    assert _snap("promise_detected") == p0 + 1
    assert _snap("sent_claim_detected") == s0
    # 正常回复零改动
    normal = "宝贝想我了没？我刚下班～"
    assert sm2._apply_media_promise_guard(normal, {}) == normal


def test_record_sent_claim_event_keys():
    d0 = _snap("sent_claim_fulfilled")
    ia.record_sent_claim_event("fulfilled")
    assert _snap("sent_claim_fulfilled") == d0 + 1
