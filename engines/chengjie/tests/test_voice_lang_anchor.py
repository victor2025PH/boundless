# -*- coding: utf-8 -*-
"""#95（实施91）语音轮语言锚门禁。

实锤（0830 停电期，会话 Jenny 全程中文客户）：ASR 中继瘫痪 → 回落转写器把
中文语音转出韩语乱码「섬멸에서 섬미꼬야」（whisper 错语言解码幻觉）→ 语言
跟随采信 → AI 韩语回话「아, 뭐라고하는 거야~…」+ TTS 按韩语念给中文客户；
同会话另有越南语出站「anh muốn chạy qua tìm em luôn đó」同机制。

不变量（与 #74 图片轮语言锚同族、与 WA 线 voice_lang_suspect 同契约）：
**单条语音转写的语种不得独立翻转会话语言**；换语言只由客户文字消息或连续
两条同语种语音坐实；被隔离的轮次置 ``_voice_lang_suspect`` → prompt 走
「听不清请确认」块（宁可确认，绝不照可疑转写自信作答/切语言）。
"""

from __future__ import annotations

from src.ai.lang_policy import voice_turn_lang_suspect

INCIDENT_KO = "섬멸에서 섬미꼬야"
INCIDENT_VI = "anh muốn chạy qua tìm em luôn đó"


# ── 纯函数判定 ───────────────────────────────────────────────────────────────

def test_incident_korean_garbage_isolated():
    # 中文会话 + 韩语乱码转写 → 隔离（事故原句金标）
    assert voice_turn_lang_suspect(
        INCIDENT_KO, stable_lang="zh", prev_user_text="今天有空吗") is True


def test_incident_vietnamese_isolated():
    assert voice_turn_lang_suspect(
        INCIDENT_VI, stable_lang="zh", prev_user_text="在忙什么呢") is True


def test_matching_lang_passes():
    # 转写语种 == 会话语言 → 正常放行（18:41 同会话正常中文语音的形态）
    assert voice_turn_lang_suspect(
        "我今天想去海边走走", stable_lang="zh") is False


def test_zh_family_fold_no_false_positive():
    # 繁体/粤语会话来一条 zh 检测结果不算翻转（检测端不产变体码）
    assert voice_turn_lang_suspect(
        "我今天想去海边走走", stable_lang="zh-tw") is False
    assert voice_turn_lang_suspect(
        "我今天想去海边走走", stable_lang="yue") is False


def test_consecutive_same_lang_confirms_switch():
    # 连续两条同语种 → 坐实真实换语言，放行（坐实豁免通道）
    assert voice_turn_lang_suspect(
        "Can we talk in English from now on please",
        stable_lang="zh",
        prev_user_text="I will send you the details in English") is False


def test_single_foreign_turn_isolated_even_if_fluent():
    # 单条流利外语（whisper 幻觉常产出流利句子）仍隔离——坐实要第二条
    assert voice_turn_lang_suspect(
        "Can we talk in English from now on please",
        stable_lang="zh", prev_user_text="好啊晚上见") is True


def test_no_stable_lang_passes():
    # 新会话无稳定语言可对照 → 放行（锚的是「相对稳定语言的冲突」）
    assert voice_turn_lang_suspect(INCIDENT_KO, stable_lang="") is False


def test_empty_or_undetectable_transcript_passes():
    assert voice_turn_lang_suspect("", stable_lang="zh") is False
    assert voice_turn_lang_suspect("123 456", stable_lang="zh") is False


# ── prompt 消费端（澄清块） ──────────────────────────────────────────────────

def _cfg(domain: str = "companion"):
    class _Cfg:
        config_path = None
        config = {"domain": domain, "web_admin": {"site_name": "T"}, "ai": {},
                  "inbox": {"reply_style": {"bubbles": {"enabled": False}}}}

        def get_ai_config(self):
            return {}

    return _Cfg()


def test_prompt_clarify_block_renders_on_suspect():
    from src.ai.ai_client import AIClient
    client = AIClient(_cfg())
    out = client._build_context_prompt({
        "channel": "telegram",
        "_peer_message_is_voice": True,
        "_voice_lang_suspect": True,
    })
    assert "语音听不清" in out and "澄清" in out
    assert "绝对不要因此改变回复语言" in out


def test_prompt_no_clarify_without_suspect():
    from src.ai.ai_client import AIClient
    client = AIClient(_cfg())
    out = client._build_context_prompt({
        "channel": "telegram",
        "_peer_message_is_voice": True,
    })
    assert "语音听不清" not in out


# ── A 线接线钉（防止锚被静默摘除） ───────────────────────────────────────────

def test_static_wiring_pin_process_message():
    # process_message 只是并发锁薄壳，真身在 _handle_message_guarded——
    # 钉真身（锁壳结构由 test_group_context_split 家族另钉）。
    import inspect
    from src.skills.skill_manager import SkillManager
    src = inspect.getsource(SkillManager._handle_message_guarded)
    assert "voice_turn_lang_suspect" in src, \
        "A 线 3b 的语音轮语言锚被移除（#95）"
    assert '_voice_lang_suspect"] = True' in src.replace("'", '"'), \
        "语音轮语言锚必须置 _voice_lang_suspect（prompt 澄清块靠它）"
