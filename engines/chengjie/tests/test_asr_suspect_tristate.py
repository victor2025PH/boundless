# -*- coding: utf-8 -*-
"""ASR P1 语音转写三态（好 / 可疑 / 无）门禁。

钉住：``transcript_suspect`` 判据 / 登记表按会话+文本核对与 TTL / ``apply_to_user_context``
清键语义 / skill_manager 与 context_store 的键契约 / ai_client 「可能听错 · 先确认」块 /
autodraft 触发侧 opt-in 封顶 review + ``asr_suspect`` L1 原因码（默认关不封顶）。
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.inbox import asr_suspect as sus

_SRC = Path(__file__).resolve().parents[1] / "src"


@pytest.fixture(autouse=True)
def _reset():
    sus._reset_for_tests()
    yield
    sus._reset_for_tests()


# ── 判据 ────────────────────────────────────────────────────────────────────
def test_transcript_suspect_rules():
    assert sus.transcript_suspect({"lang_suspect": True}, "섬멸에서") == "lang_conflict"
    assert sus.transcript_suspect({"low_confidence": True}, "大造成呢") == "low_confidence"
    assert sus.transcript_suspect({"avg_logprob": -1.3}, "大造成呢") == "low_confidence"
    assert sus.transcript_suspect({"no_speech_prob": 0.62}, "谢谢") == "no_speech"
    assert sus.transcript_suspect({"duration": 1.4, "language_probability": 0.42}, "Thank you.") == "short_unsure"
    # 正常：09-12 实测 11s 片段 p=0.98 lp=-0.22 nsp=0
    assert sus.transcript_suspect({"language": "zh", "language_probability": 0.98,
                                   "avg_logprob": -0.22, "no_speech_prob": 0.0,
                                   "duration": 11.0}, "现在开始测试") == ""
    # 缺证据不判 / 空文本不判 / 坏输入不抛
    assert sus.transcript_suspect({}, "你好") == "" and sus.transcript_suspect(None, "你好") == ""
    assert sus.transcript_suspect({"low_confidence": True}, "") == ""
    assert sus.transcript_suspect({"avg_logprob": "bad"}, "x") == ""
    # 优先级：语种冲突 > 低置信
    assert sus.transcript_suspect({"lang_suspect": True, "low_confidence": True}, "x") == "lang_conflict"


# ── 登记表 ──────────────────────────────────────────────────────────────────
def test_registry_matches_conversation_and_text():
    sus.note("wa:a:1", "low_confidence", "你那边几点怎么会说大造成呢")
    assert sus.peek("wa:a:1") == "low_confidence"
    # 同一条（含 [语音转录] 前缀 / 标点差异）→ 命中
    assert sus.peek("wa:a:1", "[语音转录] 你那边几点，怎么会说大造成呢？") == "low_confidence"
    # 客户紧接着发了文字 → 不再顶着上一条语音的标记
    assert sus.peek("wa:a:1", "我是说打造呢") == ""
    assert sus.peek("wa:a:2", "你那边几点怎么会说大造成呢") == ""
    # 不可疑 → 清旧登记
    sus.note("wa:a:1", "", "随便")
    assert sus.peek("wa:a:1") == ""


def test_registry_ttl_and_cap(monkeypatch):
    sus.note("c1", "no_speech", "嗯")
    monkeypatch.setattr(sus.time, "time", lambda: 10 ** 12)
    assert sus.peek("c1") == ""              # 过期
    monkeypatch.undo()
    for i in range(sus._MAX + 50):
        sus.note(f"k{i}", "low_confidence", "t")
    assert len(sus._REG) <= sus._MAX


def test_note_from_meta_respects_hint_switch():
    cfg_on = {"voice_recognition": {"suspect_hint": True}}
    cfg_off = {"voice_recognition": {"suspect_hint": False}}
    assert sus.note_from_meta("c", {"low_confidence": True}, "大造成", cfg_on) == "low_confidence"
    assert sus.peek("c", "大造成") == "low_confidence"
    assert sus.note_from_meta("c", {"low_confidence": True}, "大造成", cfg_off) == ""
    assert sus.peek("c", "大造成") == ""          # 关了开关连旧登记也清
    assert sus.hint_enabled({}) is True and sus.hold_review_enabled({}) is False
    assert sus.hold_review_enabled({"voice_recognition": {"suspect_hold_review": True}}) is True


def test_apply_to_user_context_sets_and_clears():
    uc = {"_voice_asr_suspect": "stale"}
    sus.note("c1", "low_confidence", "大造成呢")
    assert sus.apply_to_user_context(uc, conversation_id="c1", text="大造成呢") == "low_confidence"
    assert uc["_voice_asr_suspect"] == "low_confidence"
    # 文本不匹配 → 清掉陈年键
    assert sus.apply_to_user_context(uc, conversation_id="c1", text="另一句话") == ""
    assert "_voice_asr_suspect" not in uc
    # A 线显式值优先
    assert sus.apply_to_user_context(uc, conversation_id="", text="", explicit="short_unsure") == "short_unsure"
    assert uc["_voice_asr_suspect"] == "short_unsure"


# ── 键契约（skill_manager / context_store / ai_client）──────────────────────
def test_context_key_contracts():
    from src.skills.skill_manager import _TURN_SIGNAL_KEYS, clear_turn_signal_keys
    from src.utils.context_store import _NON_PERSIST
    assert sus.CONTEXT_KEY in _TURN_SIGNAL_KEYS          # 每轮先清（防陈年标记驻留）
    assert sus.CONTEXT_KEY in _NON_PERSIST                # 不落盘
    uc = {sus.CONTEXT_KEY: "x", "channel": "tg"}
    clear_turn_signal_keys(uc)
    assert sus.CONTEXT_KEY not in uc and uc["channel"] == "tg"
    sm_src = (_SRC / "skills" / "skill_manager.py").read_text(encoding="utf-8")
    # A 线：context → user_context 透传表含该键；B 线：起草前查登记表
    assert re.search(r'"_voice_asr_suspect",\s*\n\s*#', sm_src) or '"_voice_asr_suspect",' in sm_src
    assert "apply_to_user_context as _asr_sus_apply" in sm_src
    ai_src = (_SRC / "ai" / "ai_client.py").read_text(encoding="utf-8")
    assert 'elif context.get("_voice_asr_suspect"):' in ai_src
    assert "【语音可能听错 · 先确认】" in ai_src
    tg_src = (_SRC / "client" / "telegram_client.py").read_text(encoding="utf-8")
    assert "'_voice_asr_suspect': message_data.get('_voice_asr_suspect')" in tg_src


# ── autodraft 触发侧：opt-in 封顶 review + L1 原因码 ─────────────────────────
_AD_LOGGER = logging.getLogger("test.asr_suspect.autodraft")


def _cb(app_config, caplog):
    caplog.set_level(logging.INFO, logger=_AD_LOGGER.name)
    from src.inbox.autodraft_helpers import AutoDraftConfig, make_auto_draft_cb
    cfg = AutoDraftConfig(mode="auto_ai", min_len=0, skip=set(), platform_ceilings={},
                          skip_groups=False, enrich=False)
    ds = MagicMock()
    ds.auto_generate_draft.return_value = "d1"
    store = MagicMock()
    store.get_automation_mode_if_set.return_value = None
    store.get_conv_tags.return_value = []
    store.get_app_setting.return_value = ""
    store.list_recent_messages.return_value = []
    cb = make_auto_draft_cb(cfg, ds, store, MagicMock(), MagicMock(), _AD_LOGGER,
                            app_config=app_config)
    return cb, ds, store


def test_autodraft_caps_review_when_suspect_and_opted_in(caplog):
    from src.inbox import l1_reason
    l1_reason._reset_for_tests()
    sus.note("wa:a:1", "low_confidence", "你那边几点怎么会说大造成呢")
    cb, ds, store = _cb({"voice_recognition": {"suspect_hold_review": True}}, caplog)
    cb({"platform": "whatsapp", "conversation_id": "wa:a:1", "account_id": "a", "chat_key": "1"},
       "你那边几点怎么会说大造成呢")
    ds.auto_generate_draft.assert_called_once()
    assert ds.auto_generate_draft.call_args.kwargs["automation_mode"] == "review"
    msgs = [r.getMessage() for r in caplog.records]
    assert any("[policy] conv=wa:a:1 asr_suspect=low_confidence forced=L1" in m for m in msgs)
    assert l1_reason.peek("wa:a:1") == "asr_suspect"
    assert l1_reason.peek("d1") == "asr_suspect"
    # 审计行带原因码
    assert any(c.kwargs.get("reason") == "asr_suspect"
               for c in store.record_draft_audit.call_args_list)


def test_autodraft_default_off_only_prompt_hint(caplog):
    sus.note("wa:a:1", "low_confidence", "你那边几点怎么会说大造成呢")
    cb, ds, _store = _cb({"voice_recognition": {}}, caplog)
    cb({"platform": "whatsapp", "conversation_id": "wa:a:1", "account_id": "a", "chat_key": "1"},
       "你那边几点怎么会说大造成呢")
    assert ds.auto_generate_draft.call_args.kwargs["automation_mode"] == "auto_ai"
    assert not any("asr_suspect=" in r.getMessage() for r in caplog.records)


def test_autodraft_no_cap_when_text_is_a_newer_message(caplog):
    sus.note("wa:a:1", "low_confidence", "你那边几点怎么会说大造成呢")
    cb, ds, _store = _cb({"voice_recognition": {"suspect_hold_review": True}}, caplog)
    cb({"platform": "whatsapp", "conversation_id": "wa:a:1", "account_id": "a", "chat_key": "1"},
       "我是说打造")   # 客户紧接着的文字消息：不背上一条语音的锅
    assert ds.auto_generate_draft.call_args.kwargs["automation_mode"] == "auto_ai"


def test_risk_hold_outranks_asr_suspect_reason(caplog):
    from src.inbox import l1_reason
    l1_reason._reset_for_tests()
    sus.note("tg:a:1", "low_confidence", "hello there")
    cb, ds, store = _cb({"voice_recognition": {"suspect_hold_review": True}}, caplog)
    store.get_app_setting.return_value = json.dumps(
        {"reason": "privacy", "hit": "phone", "set_ts": 10 ** 12, "ttl_h": 24})
    cb({"platform": "tg", "conversation_id": "tg:a:1", "account_id": "a", "chat_key": "1"},
       "hello there")
    assert ds.auto_generate_draft.call_args.kwargs["automation_mode"] == "review"
    assert l1_reason.peek("tg:a:1") == "risk_hold"
