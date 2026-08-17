"""A 线语音失败「诚实回落」门禁（``src/ai/voice_honest_fallback.py``，2026-08-02）。

背景=实录事故（今天 21:38）：TTS 合成失败后文字回复原样发出，包括 LLM 写的
「语音这就来～」——空头支票。守卫语义与 media_promise_guard 同族：出站不许撒谎。

覆盖：voice 承诺/断言句被剥（词表复用 outbound_promise_guard，含事故原话的
新模式）/ 点名要语音时追加诚实台阶 / 非点名不追加 / 英文池 / 空文本与剥空的
保守路径 / 开关语义 / 台阶池自身不构成承诺（防守卫自噬）/ 确定性取池。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.ai import outbound_promise_guard as pg  # noqa: E402
from src.ai import voice_honest_fallback as vhf  # noqa: E402

_PEER_ASKS_VOICE = "发条语音呗"
_ALL_STEP_LINES = tuple(vhf._STEP_LINES["zh"]) + tuple(vhf._STEP_LINES["en"])


# ── 承诺句被剥（句级删除，其余保留）───────────────────────────────


def test_voice_promise_stripped_rest_kept():
    txt = "今天好开心呀！我去录一条语音给你～你吃饭了没？"
    out, changed = vhf.apply_voice_failure_fallback(txt, "")
    assert changed is True
    assert "录一条语音" not in out
    assert "今天好开心" in out and "吃饭" in out


def test_incident_phrase_is_detected_and_stripped():
    """2026-08-02 21:38 事故原话形态：「语音这就来～」——旧词表只认「发/录+语音」
    结构全漏，新窄模式（在 outbound_promise_guard 单一词表里）必须钉住。"""
    assert pg.detect_media_promise("语音这就来～") == "voice"
    assert pg.detect_media_promise("语音马上到！") == "voice"
    out, changed = vhf.apply_voice_failure_fallback(
        "好呀！语音这就来～等等哈", "")
    assert changed is True
    assert "这就来" not in out
    assert "好呀" in out and "等等哈" in out


def test_narrow_pattern_no_collateral_damage():
    """新模式不许误伤：疑问/否认/远期/普通提及语音都不算承诺。"""
    for t in (
        "语音来了吗？",              # 疑问
        "语音发不了啦，别闹",         # 否认
        "改天语音这就来找你玩",       # 远期词豁免
        "我喜欢听你的语音",           # 普通提及
        "刚才那条语音你听了没",       # 过去指涉
    ):
        assert pg.detect_media_promise(t) != "voice", t


def test_image_promise_is_not_this_modules_business():
    # 图片承诺归图片链管（可能真兑现）——语音失败不许顺手剥它
    txt = "等我拍一张给你～"
    out, changed = vhf.apply_voice_failure_fallback(txt, "")
    assert (out, changed) == (txt, False)
    # 客户点名要语音时：图片句保留 + 追加语音台阶
    out2, changed2 = vhf.apply_voice_failure_fallback(txt, _PEER_ASKS_VOICE)
    assert changed2 is True and "拍一张" in out2


def test_voice_claim_stripped_only_in_voice_context():
    txt = "语音发你了哈，听听看"
    # 客户没在要语音 → claim 不判（防误伤），原文放行
    out, changed = vhf.apply_voice_failure_fallback(txt, "你吃了吗")
    assert (out, changed) == (txt, False)
    # 客户在要语音 → 「已发」断言=谎，剥掉 + 台阶
    out2, changed2 = vhf.apply_voice_failure_fallback(txt, _PEER_ASKS_VOICE)
    assert changed2 is True and "发你了" not in out2


# ── 诚实台阶（点名要语音才追加）───────────────────────────────────


def test_step_appended_when_peer_asks_voice():
    txt = "在忙呢，刚回来～"
    out, changed = vhf.apply_voice_failure_fallback(txt, "想听你的声音，发条语音呗")
    assert changed is True
    assert out.startswith("在忙呢，刚回来～\n")
    assert any(line in out for line in vhf._STEP_LINES["zh"])
    # 确定性：同文本重算恒定
    out2, _ = vhf.apply_voice_failure_fallback(txt, _PEER_ASKS_VOICE)
    assert out2 == out


def test_no_append_when_peer_not_asking():
    txt = "在忙呢，刚回来～"
    assert vhf.apply_voice_failure_fallback(txt, "你吃了吗") == (txt, False)
    assert vhf.apply_voice_failure_fallback(txt, "") == (txt, False)


def test_english_pool_for_non_zh():
    txt = "one sec, talking to my mom"
    lang = vhf.resolve_fallback_lang(txt)
    assert lang == "en"
    out, changed = vhf.apply_voice_failure_fallback(
        txt, "send me a voice message", lang=lang)
    assert changed is True
    assert any(line in out for line in vhf._STEP_LINES["en"])
    assert not any(line in out for line in vhf._STEP_LINES["zh"])


def test_resolve_fallback_lang_scripts():
    assert vhf.resolve_fallback_lang("在忙呢") == "zh"
    assert vhf.resolve_fallback_lang("hello there") == "en"
    # ja/ko 只有 zh/en 两池 → 落 en（比发错中文强）
    assert vhf.resolve_fallback_lang("ボイス送るね") == "en"
    assert vhf.resolve_fallback_lang("음성 보낼게") == "en"


def test_pick_step_line_deterministic_and_pool_scoped():
    a = vhf.pick_step_line("同一条文本", "zh")
    assert a == vhf.pick_step_line("同一条文本", "zh")
    assert a in vhf._STEP_LINES["zh"]
    assert vhf.pick_step_line("same text", "en") in vhf._STEP_LINES["en"]
    # 未知语言码回落 en 池
    assert vhf.pick_step_line("x", "ja") in vhf._STEP_LINES["en"]


# ── 保守边界（宁可发原文不发空/不误改）───────────────────────────


def test_empty_text_conservative():
    assert vhf.apply_voice_failure_fallback("", _PEER_ASKS_VOICE) == ("", False)
    assert vhf.apply_voice_failure_fallback("   ", _PEER_ASKS_VOICE) == ("   ", False)


def test_stripped_empty_without_request_returns_original():
    txt = "我去录一条语音给你哈～"          # 整条都是承诺 → 剥空
    assert vhf.apply_voice_failure_fallback(txt, "") == (txt, False)


def test_stripped_empty_with_request_returns_step_alone():
    txt = "我去录一条语音给你哈～"
    out, changed = vhf.apply_voice_failure_fallback(txt, _PEER_ASKS_VOICE)
    assert changed is True
    assert out in vhf._STEP_LINES["zh"]     # 台阶独立成句，不发空消息


def test_untouched_text_returned_verbatim():
    txt = "  首尾空白也不许动。\n"          # 没剥到东西 → 逐字节原样（防假阳性改写）
    assert vhf.apply_voice_failure_fallback(txt, "") == (txt, False)


# ── 开关（默认开=正确性守卫家族哲学）─────────────────────────────


def test_toggle_semantics():
    assert vhf.honest_fallback_enabled({}) is True
    assert vhf.honest_fallback_enabled(None) is True
    assert vhf.honest_fallback_enabled({"telegram": {"voice_reply": {
        "honest_fallback": {"enabled": False}}}}) is False
    assert vhf.honest_fallback_enabled({"telegram": {"voice_reply": {
        "honest_fallback": "garbage"}}}) is True   # 坏形回落默认
    assert vhf.resolve_honest_fallback_cfg(
        {"telegram": {"voice_reply": {"honest_fallback": {"enabled": False}}}}
    ) == {"enabled": False}


def test_caller_sites_are_unwired_no_fallback_discipline():
    """无兜底纪律钉（2026-08-17 老板拍板，docs/实施33 v2）：语音失败**不再**改发
    文字——telegram_client 同步路径与 sender text-first 的「诚实回落改写+文字替发」
    接线必须保持拆除态（谁翻回来先红）；失败信号位与 delivery_block 处置必须在场。
    纯函数模块本体保留（本文件其余测试仍测它），只是生产链不许再消费。"""
    root = Path(__file__).parent.parent / "src" / "client"
    tc = (root / "telegram_client.py").read_text(encoding="utf-8")
    assert "apply_voice_failure_fallback" not in tc
    assert "fail_state=_voice_fail_state" in tc          # 失败信号位保留
    assert "delivery_block" in tc                        # 拦截+弹窗处置在场
    sender = (root / "sender.py").read_text(encoding="utf-8")
    assert "apply_voice_failure_fallback" not in sender
    assert 'fail_state["synth_failed"] = True' in sender  # 失败信号位保留
    assert "delivery_block" in sender


# ── 台阶池自身安全（防守卫自噬/被别的撤回层二次剥）────────────────


@pytest.mark.parametrize("line", _ALL_STEP_LINES)
def test_step_lines_are_not_promises(line):
    assert pg.detect_media_promise(line) == "", line


@pytest.mark.parametrize("line", _ALL_STEP_LINES)
def test_step_lines_survive_own_stripper(line):
    res, dropped = vhf._strip_voice_lies(line, media_context=True)
    assert dropped is False and res == line
