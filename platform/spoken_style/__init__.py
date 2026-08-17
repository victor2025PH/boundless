# -*- coding: utf-8 -*-
"""spoken_style —— 真人感文本层交付包（AvatarHub → 智聊/ChatX 等产品复用）。

把「像真人说话」拆成四层，全部只动文本、与具体 LLM/TTS 解耦：
  L1 稳定 system 段  : system_blocks()  —— 人设卡 + 说话指纹 + 副语言 + 情绪标记协议
  L2 轮变尾注        : turn_tail_hint() —— 说话稿档位 + 意图感知篇幅（跟用户输入变）
  L3 出口清洁/渲染   : clean_reply()    —— 摘情绪标签、剥副语言标记，产出 TTS 参数建议
  L4 口语化改写(可选): colloquial_rewrite.build_rewrite_fn() —— 本地小模型整段重写，
                       失败/超时/事实锁不过 → None 原句直通（宁不改不可断流）

快速接入（伪代码）：
    import spoken_style as ss
    sys_prompt = "\\n\\n".join(ss.system_blocks(persona=卡, role="美月"))
    tail = ss.turn_tail_hint(user_text, level=2)          # 挂在本轮消息尾
    reply = 你的LLM(sys_prompt, history, user_text + tail)
    clean = ss.clean_reply(reply)
    tts(clean["text"], emotion=clean["emotion"])           # 见 emotion_instruct.py

冒烟：python smoke_test.py（零外网，注入式假 LLM，红绿双向标定）。
来源与同步：见 README.md「出处与单一真相」。
"""
from __future__ import annotations

from . import colloquial_rewrite, emo_tag, emotion_instruct, naturalness, persona
from .emotion_instruct import EMOTION_INSTRUCT, VALID_EMOTIONS, fmt_instruct
from .naturalness import (BROADCAST_HINT, EMO_TEXT_HINT, is_reactive_input,
                          paraling_prompt, reply_style_hint, spoken_style_hint,
                          strip_paralinguistic)
from .persona import (PersonaError, load_persona, persona_prompt,
                      persona_sanitize, tone_words)

__version__ = "0.1.0"


def system_blocks(persona_card: dict | None = None, role: str = "",
                  laugh: bool = False, emotion_tags: bool = True) -> list[str]:
    """稳定 system 段（整个会话不变，KV 缓存友好）。按需拼接：

    persona_card : 人设卡 dict（schema 见 persona.py），产出【人设卡】段
    role         : 有说话指纹的角色名（data/speech_prints.json），产出【说话指纹】段
    laugh        : 下游有真笑声素材可拼时才 True（假笑比没笑更毁真实感）
    emotion_tags : 让 LLM 自报句级情绪标记（emo_tag 协议，供 TTS 情绪驱动）
    """
    blocks: list[str] = []
    if persona_card:
        b = persona_prompt(persona_sanitize(persona_card))
        if b:
            blocks.append(b)
    if role:
        b = colloquial_rewrite.prompt_block(role)
        if b:
            blocks.append(b)
    blocks.append(paraling_prompt(laugh))
    if emotion_tags:
        blocks.append(emo_tag.PROMPT)
    return blocks


def turn_tail_hint(user_text: str, level: int = 2, flavor: float = 1.0) -> str:
    """轮变尾注（每轮跟用户输入变，挂在本轮消息之后；不要写进稳定 system）。

    level  : 自然度 0=播音 1=口语流畅 2=日常唠嗑(默认) 3=故事/陪伴
             故事/续讲意图时建议调用方把 2 升到 3（naturalness.STORY_INTENT_RE 可判）
    flavor : <0.5 走播报档（纯净稿，与口语提示互斥）
    """
    try:
        fl = 1.0 if flavor is None else float(flavor)
    except Exception:
        fl = 1.0
    if fl < 0.5:
        return BROADCAST_HINT
    style = spoken_style_hint(level) or EMO_TEXT_HINT
    return "\n" + style + reply_style_hint(user_text)


def clean_reply(text: str) -> dict:
    """出口清洁：LLM 原始回复 → 字幕/TTS 可用的干净文本 + 渲染参数建议。

    返回 {"text": 干净文本, "emotion": 英文情绪键或"", "intensity": 弱/中/强或"",
          "paraling": {"laugh":n,"big_laugh":n,"breath":n}, "pause_extra_ms": int}

    emotion 直接可喂 emotion_tts 的 /v1/tts/clone（键表见 emotion_instruct.py）。
    需要跨轮情绪余晖/会话态时改用 emo_tag.capture(text, session_id) 自己管理。
    """
    t = text or ""
    tag = emo_tag.classify_text(t)
    body = emo_tag.strip_tags(t)
    clean, ev = strip_paralinguistic(body)
    pause = min(naturalness.BREATH_EXTRA_MS * ev.get("breath", 0),
                naturalness.BREATH_PAUSE_MAX_MS)
    return {"text": clean,
            "emotion": tag.get("label", "") or "",
            "intensity": tag.get("intensity", "") or "",
            "paraling": ev, "pause_extra_ms": pause}
