# -*- coding: utf-8 -*-
"""#97（实施91）出站收口点混语兜底门禁。

击穿实锤：0830 21:47「I'm 我 the one who's still here, still listening.」
（v1.0.63 已带 #64 出稿口修复仍出站——deferred/主动链不经出稿口、英文客户
不触发翻译出口）。修法＝混语确定性剥除装到全链共过的 send 收口点：

- ``AccountOrchestrator.send``（B 线 autosend / 主动触达 / 关怀 / 唤醒全部
  自动链；origin=manual 人工文本绝不动）；
- A 线 ``sender._send_reply`` 经 ``outbound_quality_pass``。

金标（含两代事故原句）在 ``src/eval/lang_mix_eval.py``，本文件消费其 passed。
"""

from __future__ import annotations

import os
import tempfile

import pytest

from src.ai.outbound_text_guard import sendpoint_lang_mix_pass
from src.ai.outbound_quality import outbound_quality_pass, reset_outbound_guard

INCIDENT_0830 = "I'm 我 the one who's still here, still listening."
INCIDENT_0826 = "I'm 我, and I'm not going anywhere."


# ── 收口点纯函数 ─────────────────────────────────────────────────────────────

def test_sendpoint_strips_incident_strings():
    for s in (INCIDENT_0830, INCIDENT_0826):
        out, act = sendpoint_lang_mix_pass(s)
        assert act == "hard_stripped"
        assert "我" not in out
        assert "still" in out or "going" in out  # 句子本体保留


def test_sendpoint_keeps_short_text_safety_valve():
    # 剥后过短安全阀（与出稿口同口径，测试钉死的旧行为）：保留原文
    out, act = sendpoint_lang_mix_pass("im 我")
    assert out == "im 我" and act == ""  # latin<6 根本不判 hard


def test_sendpoint_soft_observe_only():
    s = ("老师今天让我们把这句英文抄十遍背下来：Practice makes perfect and "
         "never give up！我抄到手都酸了哈哈")
    out, act = sendpoint_lang_mix_pass(s)
    assert out == s and act == "soft"


def test_sendpoint_clean_and_empty_passthrough():
    assert sendpoint_lang_mix_pass("明天见，路上小心呀") == ("明天见，路上小心呀", "")
    assert sendpoint_lang_mix_pass("") == ("", "")
    assert sendpoint_lang_mix_pass("See you tomorrow!") == ("See you tomorrow!", "")


# ── 金标评测门禁（src/eval/lang_mix_eval.py） ────────────────────────────────

def test_lang_mix_eval_golden_passes():
    from src.eval.lang_mix_eval import evaluate_lang_mix
    report = evaluate_lang_mix()
    assert report["passed"], report["errors"]


def test_lang_mix_eval_detector_actually_detects():
    # 探测器有效性自证：喂一条「必须剥但按 keep 断言」的假金标 → 必 FAIL
    from src.eval.lang_mix_eval import LangMixSample, evaluate_lang_mix
    bad = [LangMixSample(INCIDENT_0830, "keep", "tampered")]
    report = evaluate_lang_mix(bad)
    assert not report["passed"]


# ── A 线发送口（outbound_quality_pass） ──────────────────────────────────────

def test_quality_pass_strips_lang_mix():
    reset_outbound_guard()
    out = outbound_quality_pass(INCIDENT_0830, chat_id="c1", persona_name="")
    assert "我" not in out and "still listening" in out


def test_quality_pass_keeps_legit_mixed():
    reset_outbound_guard()
    s = "我买了新手机（iPhone），OK 吧？"
    assert outbound_quality_pass(s, chat_id="c2", persona_name="") == s


def test_quality_pass_lang_mix_optout():
    reset_outbound_guard()
    out = outbound_quality_pass(
        INCIDENT_0830, chat_id="c3", persona_name="", lang_mix=False)
    assert out == INCIDENT_0830


# ── 编排器收口点（orch.send 全自动链） ───────────────────────────────────────

class _CaptureWorker:
    def __init__(self, account, config):
        self.sent = []

    async def start(self):
        pass

    async def stop(self):
        pass

    async def healthy(self):
        return True

    def status(self):
        return {"type": "fake_send"}

    async def send(self, chat_key, text):
        self.sent.append((chat_key, text))
        return {"delivered": True, "message_id": "m1"}


@pytest.fixture
def _orch_env(monkeypatch):
    from src.integrations import account_orchestrator as orch_mod
    from src.integrations.account_orchestrator import (
        AccountOrchestrator, account_key)
    from src.integrations.account_registry import AccountRegistry
    import src.integrations.protocol_bridge as pb

    monkeypatch.setattr(pb, "emit_incoming", lambda *a, **k: None)
    orch_mod._WORKER_FACTORIES.pop("telegram:protocol", None)
    orch_mod.register_worker(
        "telegram", "protocol", lambda a, c: _CaptureWorker(a, c))
    registry = AccountRegistry(os.path.join(tempfile.mkdtemp(), "acc.db"))
    registry.upsert("telegram", "1", mode="protocol", status="online")
    yield orch_mod, AccountOrchestrator, account_key, registry
    orch_mod._WORKER_FACTORIES.pop("telegram:protocol", None)


async def _started(AccountOrchestrator, account_key, registry, config=None):
    o = AccountOrchestrator(registry=registry, config=config or {})
    await o.sync()
    return o, o._managed[account_key("telegram", "1")].worker


@pytest.mark.asyncio
async def test_orch_send_auto_strips_lang_mix(_orch_env):
    _, AccountOrchestrator, account_key, registry = _orch_env
    o, worker = await _started(AccountOrchestrator, account_key, registry)
    res = await o.send("telegram", "1", "chat1", INCIDENT_0830, origin="auto")
    assert res.get("delivered") is True
    assert len(worker.sent) == 1
    sent_text = worker.sent[0][1]
    assert "我" not in sent_text and "still listening" in sent_text


@pytest.mark.asyncio
async def test_orch_send_manual_never_mutates(_orch_env):
    # 坐席人工文本（含刻意中英混写）一个字不动
    _, AccountOrchestrator, account_key, registry = _orch_env
    o, worker = await _started(AccountOrchestrator, account_key, registry)
    res = await o.send(
        "telegram", "1", "chat1", INCIDENT_0830, origin="manual")
    assert res.get("delivered") is True
    assert worker.sent[0][1] == INCIDENT_0830


@pytest.mark.asyncio
async def test_orch_send_respects_guard_config_off(_orch_env):
    _, AccountOrchestrator, account_key, registry = _orch_env
    cfg = {"companion": {"outbound_text_guard": {"enabled": False}}}
    o, worker = await _started(
        AccountOrchestrator, account_key, registry, config=cfg)
    await o.send("telegram", "1", "chat1", INCIDENT_0830, origin="auto")
    assert worker.sent[0][1] == INCIDENT_0830


@pytest.mark.asyncio
async def test_orch_send_legit_text_untouched(_orch_env):
    _, AccountOrchestrator, account_key, registry = _orch_env
    o, worker = await _started(AccountOrchestrator, account_key, registry)
    s = "我买了新手机（iPhone），OK 吧？"
    await o.send("telegram", "1", "chat1", s, origin="auto")
    assert worker.sent[0][1] == s


# ── 静态接线钉（防止收口点被静默摘除） ───────────────────────────────────────

def test_static_wiring_pins():
    import inspect
    from src.integrations.account_orchestrator import AccountOrchestrator
    src_send = inspect.getsource(AccountOrchestrator.send)
    assert "sendpoint_lang_mix_pass" in src_send, \
        "orch.send 的混语收口点被移除（#97 击穿防线）"
    assert 'origin or "auto") != "manual"' in src_send.replace("'", '"'), \
        "orch.send 混语收口点必须豁免人工路径（origin=manual）"

    import src.ai.outbound_quality as oq
    src_q = inspect.getsource(oq.outbound_quality_pass)
    assert "sendpoint_lang_mix_pass" in src_q, \
        "A 线发送口（outbound_quality_pass）的混语收口点被移除（#97）"
