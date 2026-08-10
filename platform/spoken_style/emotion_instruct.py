# -*- coding: utf-8 -*-
"""情绪 → CosyVoice3 instruct 措辞映射（TTS 侧约定，单一真相在 emotion_tts_server）。

来源：engines/avatarhub emotion_tts_server.py（2026-08-10 快照，逐字一致）。
117 机本身跑着同一份 emotion_tts_server（:8010），这里是「客户端如何驱动它」的约定：

  ① 有明确情绪标签 → POST /v1/tts/clone 带 emotion=<label>，服务端自己拼 instruct2；
  ② 有自定义演绎要求 → POST /v1/tts/instruct 带 instruct=<自由中文描述>（优先级高于 emotion）；
  ③ 情绪标签哪里来 → emo_tag.PROMPT 让 LLM 在回复开头自报 [情绪:标签|强度]，
     emo_tag.capture() 摘走并映射到本表的英文键（LABELS 见 emo_tag.py）。

实战注意（源仓结论）：instruct 演绎会轻微牺牲音色相似度——追求音色最像时
用 neutral 走 zero_shot，把情绪交给「说话稿」文本本身承载（写法即演法）。
"""
from __future__ import annotations

EMOTION_INSTRUCT = {
    "neutral":   "",
    "happy":     "用开心愉快的语气说",
    "sad":       "用悲伤难过的语气说",
    "angry":     "用愤怒生气的语气说",
    "fearful":   "用恐惧害怕的语气说",
    "surprised": "用惊讶的语气说",
    "disgusted":  "用厌恶的语气说",
    "gentle":    "用温柔轻柔的语气说",
    "excited":   "用兴奋激动的语气说",
    "calm":      "用平静沉着的语气说",
    "serious":   "用严肃认真的语气说",
}

VALID_EMOTIONS = frozenset(EMOTION_INSTRUCT)


def fmt_instruct(emotion: str, custom_instruct: str = "") -> str:
    """CosyVoice3 instruct2 格式：'You are a helpful assistant. [描述]<|endofprompt|>'。
    服务端已内置同款拼法；本函数供直连底层引擎或自检对账用。"""
    desc = custom_instruct or EMOTION_INSTRUCT.get(emotion, "")
    if not desc:
        return ""
    return f"You are a helpful assistant. {desc}<|endofprompt|>"
