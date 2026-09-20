# -*- coding: utf-8 -*-
"""出站收口点守卫门禁（实施91 · #97/#105/#106 三张 P1 击穿单的收口方案）。

三案同根＝守卫只装在部分出站路径。本门禁钉住：

1. 呼格三连纯函数语义（互换 / call_peer 近形 / 人设名呼格）——#105/#96 金标；
2. 铆定语言冲突检出（B67 explicit × 文字系统）——#106 金标；
3. 语音合成前收口（presynth_text_guard：interactive 豁免=「所打即所念」）；
4. **静态接线钉**：orch.send / A 线 _send_reply / TTSPipeline.synthesize /
   skill_manager 3b2 / lifecycle 翻译器注册——任何一处被静默摘除先红。
"""

from __future__ import annotations

import inspect

import pytest

from src.ai.sendpoint_guard import (
    _near_call_peer_variant,
    near_call_peer_fix,
    outbound_lang_pin,
    pin_script_conflict,
    presynth_text_guard,
    sendpoint_lang_pin_fix,
    sendpoint_vocative_pass,
    set_sendpoint_translator,
)
from src.eval.sendpoint_guard_eval import (
    INCIDENT_105_VOICE,
    LANG_PIN_GOLDENS,
    VOCATIVE_GOLDENS,
    LangPinSample,
    VocativeSample,
    evaluate_sendpoint_guard,
)


# ── 金标评测（#105/#106 原句） ───────────────────────────────────────────────

def test_golden_eval_passes():
    report = evaluate_sendpoint_guard()
    assert report["passed"], report["errors"]


def test_eval_detector_actually_detects():
    # 探测器有效性自证：塞一条「必须纠正但守卫接不到档案」的坏样本必 FAIL——
    # 评测不是摆设（篡改金标/守卫被摘除时这里先红）。
    bad_voc = [VocativeSample(
        "Good morning baba!", {"call_peer": "", "peer_calls_you": "",
                               "self_names": []},
        must_not_contain=["baba"], note="tamper-proof")]
    report = evaluate_sendpoint_guard(voc_samples=bad_voc, pin_samples=[])
    assert not report["passed"]
    bad_pin = [LangPinSample("哈哈这句是中文", "en", False, "tamper-proof")]
    report2 = evaluate_sendpoint_guard(voc_samples=[], pin_samples=bad_pin)
    assert not report2["passed"]


# ── call_peer 近形纠正（#105 补强） ─────────────────────────────────────────

def test_near_variant_narrow_judgement():
    assert _near_call_peer_variant("baba", "babe") is True
    assert _near_call_peer_variant("baby", "babe") is True
    assert _near_call_peer_variant("babe", "babe") is False   # 同词不算
    assert _near_call_peer_variant("bab", "babe") is False    # 长度不同
    assert _near_call_peer_variant("bobo", "babe") is False   # 差 2 字
    assert _near_call_peer_variant("宝贝a", "babe") is False  # 非纯拉丁
    assert _near_call_peer_variant("ba", "be") is False       # 过短（<3）


def test_near_fix_capitalization_preserved():
    out, hits = near_call_peer_fix("Baba, are you awake?", "babe")
    assert hits == ["Baba"]
    assert out.startswith("Babe,")


def test_near_fix_skips_peer_actually_named_variant():
    # 客户真名恰为近形（就叫 Baba）→ 呼格合法，不纠
    out, hits = near_call_peer_fix(
        "Good morning baba!", "babe", peer_names=["Baba"])
    assert hits == []
    assert out == "Good morning baba!"


def test_near_fix_meta_sentence_untouched():
    out, hits = near_call_peer_fix("don't call me baba anymore", "babe")
    assert hits == []


def test_vocative_pass_incident_105():
    out, meta = sendpoint_vocative_pass(
        INCIDENT_105_VOICE,
        {"call_peer": "babe", "peer_calls_you": "baba", "self_names": []})
    assert "baba" not in out and "babe" in out
    assert meta["swap_hits"] or meta["near_hits"]


def test_vocative_pass_empty_names_noop():
    src = "Good morning baba!"
    out, meta = sendpoint_vocative_pass(src, {})
    assert out == src


# ── 铆定语言（#106） ─────────────────────────────────────────────────────────

def test_pin_conflict_zh_family_exempt():
    # zh 家族（zh/zh-tw/yue）字形差异不在收口点判（翻译层管，实施89）
    assert pin_script_conflict("一起加油哦，我在这边等你", "zh-tw") is False
    assert pin_script_conflict("一起加油哦，我在这边等你", "yue") is False


def test_pin_conflict_ja_ko_text_not_flagged_for_cjk_pins():
    # 假名/谚文计入 CJK 家族——铆 ja/ko 时不误报
    assert pin_script_conflict("おはよう、今日も頑張ろうね", "ja") is False
    assert pin_script_conflict("좋은 아침이에요 오늘도 화이팅", "ko") is False


class _PinStore:
    def __init__(self, pin_map):
        self._m = dict(pin_map)

    def get_outbound_lang_if_set(self, cid):
        return self._m.get(cid, "")


@pytest.fixture()
def _pin_store():
    from src.integrations import protocol_bridge as pb
    store = _PinStore({"telegram:acct1:555": "en"})
    pb.register_inbox_store_getter(lambda: store)
    try:
        yield store
    finally:
        pb.register_inbox_store_getter(None)
        set_sendpoint_translator(None)


def test_outbound_lang_pin_reads_store(_pin_store):
    assert outbound_lang_pin("telegram", "acct1", "555") == "en"
    assert outbound_lang_pin("telegram", "acct1", "666") == ""


async def test_lang_pin_fix_translates(_pin_store):
    async def _fake_translator(platform, account_id, chat_key, text):
        return "Haha, nice framing!"

    set_sendpoint_translator(_fake_translator)
    out, act = await sendpoint_lang_pin_fix(
        "telegram", "acct1", "555", "哈哈，这构图还挺有味道的")
    assert act == "translated"
    assert out == "Haha, nice framing!"


async def test_lang_pin_fix_hold_semantics(_pin_store):
    async def _hold(platform, account_id, chat_key, text):
        return None

    set_sendpoint_translator(_hold)
    out, act = await sendpoint_lang_pin_fix(
        "telegram", "acct1", "555", "哈哈，这构图还挺有味道的")
    assert out is None and act == "hold"


async def test_lang_pin_fix_no_translator_passthru(_pin_store):
    set_sendpoint_translator(None)
    src = "哈哈，这构图还挺有味道的"
    out, act = await sendpoint_lang_pin_fix("telegram", "acct1", "555", src)
    assert out == src and act == "passthru"


async def test_lang_pin_fix_no_conflict_zero_cost(_pin_store):
    calls = []

    async def _fake(platform, account_id, chat_key, text):
        calls.append(text)
        return text

    set_sendpoint_translator(_fake)
    src = "Sounds great, see you tomorrow!"
    out, act = await sendpoint_lang_pin_fix("telegram", "acct1", "555", src)
    assert out == src and act == "" and calls == []   # 已是铆定语言：零翻译调用


async def test_lang_pin_fix_translator_exception_passthru(_pin_store):
    async def _boom(platform, account_id, chat_key, text):
        raise RuntimeError("translator down")

    set_sendpoint_translator(_boom)
    src = "哈哈，这构图还挺有味道的"
    out, act = await sendpoint_lang_pin_fix("telegram", "acct1", "555", src)
    assert out == src   # 翻译器炸了绝不拦断发送


# ── #154：铆定缺位时的出站历史回落 ───────────────────────────────────────────

class _HistStore:
    """只有出站历史、没有「发→X」行的会话（＝铆定被程序清空后的现场）。"""

    def __init__(self, msgs):
        self._msgs = list(msgs)

    def get_outbound_lang_if_set(self, cid):
        return ""

    def list_recent_messages(self, cid, limit=50):
        return self._msgs[-limit:]


def _hist(langs_texts):
    return [{"direction": "out", "text": t} for t in langs_texts]


@pytest.fixture()
def _hist_store_factory():
    from src.integrations import protocol_bridge as pb

    made = []

    def _mk(msgs):
        st = _HistStore(msgs)
        made.append(st)
        pb.register_inbox_store_getter(lambda: st)
        return st

    try:
        yield _mk
    finally:
        pb.register_inbox_store_getter(None)
        set_sendpoint_translator(None)


_EN_HIST = ["Good morning, hope you slept well.",
            "That sounds like a lovely plan for the weekend.",
            "I'll be around later tonight if you want to chat."]


def test_history_pin_needs_enough_consistent_samples(_hist_store_factory):
    from src.ai.sendpoint_guard import outbound_history_lang_pin
    _hist_store_factory(_hist(_EN_HIST))
    assert outbound_history_lang_pin("telegram", "7331682688", "8852939166") == "en"
    # 样本不足（<3）→ 不回落，收口点不动手
    _hist_store_factory(_hist(_EN_HIST[:2]))
    assert outbound_history_lang_pin("telegram", "7331682688", "8852939166") == ""


def test_history_pin_last_message_switched_language(_hist_store_factory):
    """最近一条已换语言 → 历史多数不作数（刚切语言的会话不许被拽回去）。"""
    from src.ai.sendpoint_guard import outbound_history_lang_pin
    _hist_store_factory(_hist(_EN_HIST + ["おはよう、今日もいい一日になりますように。"]))
    assert outbound_history_lang_pin("telegram", "7331682688", "8852939166") == ""


async def test_154_japanese_lines_rewritten_via_history_pin(_hist_store_factory):
    """#154 金标端到端：铆定被清空 + 一路英文出站历史 → 四条日语原文全被改写。"""
    from src.eval.sendpoint_guard_eval import INCIDENT_154_JA
    _hist_store_factory(_hist(_EN_HIST))

    async def _fake(platform, account_id, chat_key, text):
        return "Sleep well, I'll be here tomorrow."

    set_sendpoint_translator(_fake)
    for ja in INCIDENT_154_JA:
        out, act = await sendpoint_lang_pin_fix(
            "telegram", "7331682688", "8852939166", ja)
        assert act == "translated", ja
        assert out == "Sleep well, I'll be here tomorrow."


async def test_history_pin_does_not_fire_on_consistent_language(_hist_store_factory):
    """日语会话（历史也是日语）发日语＝零冲突，绝不误动手。"""
    ja_hist = ["おはよう、よく眠れた？", "それは楽しそうな週末だね。",
               "夜にまた話そうね。"]
    _hist_store_factory(_hist(ja_hist))
    calls = []

    async def _fake(platform, account_id, chat_key, text):
        calls.append(text)
        return text

    set_sendpoint_translator(_fake)
    out, act = await sendpoint_lang_pin_fix(
        "telegram", "7331682688", "8852939166", "おやすみ、また明日ね")
    assert out == "おやすみ、また明日ね" and act == "" and calls == []


def test_explicit_pin_still_wins_over_history(_pin_store):
    """显式铆定在场时不看历史（_pin_store 无 list_recent_messages 也照样解析）。"""
    from src.ai.sendpoint_guard import outbound_lang_pin
    assert outbound_lang_pin("telegram", "acct1", "555") == "en"


# ── 语音合成前收口（#105 语音面） ────────────────────────────────────────────

def test_presynth_interactive_verbatim(monkeypatch):
    # 坐席手打逐字链（interactive=True）：「所打即所念」——一个字不动
    src = "Good morning baba, I'm 我 here."
    assert presynth_text_guard(src, persona_id="p1", interactive=True) == src


def test_presynth_vocative_and_lang_mix(monkeypatch):
    import src.ai.sendpoint_guard as spg
    monkeypatch.setattr(
        spg, "resolve_sendpoint_names",
        lambda config, platform, account_id, chat_key="", persona_id="",
        registry=None: {"call_peer": "babe", "peer_calls_you": "baba",
                        "self_names": []})
    out = presynth_text_guard(
        "Good morning baba! I'm 我 the one still here.", persona_id="p1")
    assert "baba" not in out and "babe" in out
    assert "我" not in out   # 混语残字同步剥除（#97 语音面）


def test_presynth_no_names_still_strips_lang_mix(monkeypatch):
    import src.ai.sendpoint_guard as spg
    monkeypatch.setattr(
        spg, "resolve_sendpoint_names",
        lambda *a, **k: {})
    out = presynth_text_guard(
        "I'm 我 the one who's still here, still listening.", persona_id="")
    assert "我" not in out


# ── 静态接线钉（防守卫被静默摘除） ───────────────────────────────────────────

def test_wiring_orchestrator_send():
    from src.integrations.account_orchestrator import AccountOrchestrator
    src = inspect.getsource(AccountOrchestrator.send)
    assert "sendpoint_vocative_pass" in src, "orch.send 呼格收口被摘（#105）"
    assert "sendpoint_lang_pin_fix" in src, "orch.send 铆定收口被摘（#106）"
    assert "lang_pin_hold" in src, "orch.send HOLD 语义被摘（无兜底纪律）"
    assert "sendpoint_lang_mix_pass" in src, "orch.send 混语收口被摘（#97）"


def test_wiring_a_line_send_reply():
    from src.client.sender import TelegramSenderMixin
    src = inspect.getsource(TelegramSenderMixin._send_reply)
    assert "sendpoint_vocative_pass" in src, "A 线呼格收口被摘（#105/#96）"
    assert "sendpoint_lang_pin_fix" in src, "A 线铆定收口被摘（#106）"


def test_wiring_tts_presynth():
    from src.ai.tts_pipeline import TTSPipeline
    src = inspect.getsource(TTSPipeline.synthesize)
    assert "presynth_text_guard" in src, "语音合成前收口被摘（#105 主修）"
    assert "not interactive" in src, "interactive 豁免（所打即所念）被摘"


def test_wiring_skill_manager_b67_pin():
    from src.skills.skill_manager import SkillManager
    src = inspect.getsource(SkillManager._handle_message_guarded)
    assert "outbound_lang_pin" in src, "3b2 B67 生成端铆定被摘（#106 root fix）"


def test_wiring_lifecycle_registers_translator():
    import io
    from pathlib import Path
    p = Path(__file__).resolve().parents[1] / "src" / "bootstrap" / "lifecycle.py"
    src = io.open(p, encoding="utf-8").read()
    assert "set_sendpoint_translator" in src, "收口点翻译器注册被摘（#106）"


def test_resolve_cfg_has_sendpoint_keys():
    from src.ai.outbound_text_guard import resolve_cfg
    cfg = resolve_cfg(None)
    assert cfg.get("vocative") is True and cfg.get("lang_pin") is True
    off = resolve_cfg({"companion": {"outbound_text_guard": {
        "vocative": False, "lang_pin": False}}})
    assert off.get("vocative") is False and off.get("lang_pin") is False
