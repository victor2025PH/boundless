"""A 线语音失败「诚实回落」（纯函数，零 IO 零 LLM）。

实录事故（2026-08-02 21:38）：Telegram 原生回复链 TTS 合成失败时，文字回复
原样发出——包括 LLM 写的「语音这就来～」这类承诺句。语音永远不会来，
空头支票直接击穿真人感。B 线 autosend 早有承诺撤回
（``autosend_helpers._depromise_autosend_text``），A 线语音失败分支此前没有。

本模块补上 A 线版本，两个刻意收窄：
- **只走正则句级剥离**（不走 LLM 重写）——A 线失败出口在发送热路径上，
  多一次 LLM 往返得不偿失；词表**完全复用** ``outbound_promise_guard``
  （``_sentence_is_promise`` / ``_sentence_claim_kind`` / ``wants_media``），
  绝不自造第二套。
- **只剥 voice 类句子**——图片承诺归图片链管（skill_manager 5c2 /异步兑现
  可能真发出来，这里剥了反而把真话剥成谎）。

客户**点名要语音**（``wants_media(peer_text)=='voice'``）时，剥完再补一句
「诚实台阶」（口语化借口 + 补偿承诺，crc32(text) 确定性取池——同一条回复
重算恒定，缓存/重试友好）。台阶措辞刻意带「回头/等下」等远期词：不落
promise 词表，不会被任何撤回层二次剥掉（有门禁钉住）。

开关 ``telegram.voice_reply.honest_fallback.enabled``（**默认开**——与
media_promise_guard 同属出站正确性守卫家族）。
"""
from __future__ import annotations

import re
import zlib
from typing import Any, Dict, Optional, Tuple

from src.ai.outbound_promise_guard import (
    KIND_VOICE,
    _SENT_SPLIT_RE,
    _script_lang,
    _sentence_claim_kind,
    _sentence_is_promise,
    wants_media,
)

# ── 诚实台阶池（zh 5 条 / en 3 条；lang 非 zh 一律走 en 池）─────────────────────
# 措辞三要素：口语化借口（网/设备抽风）+ 先打字陪聊 + 远期补偿（回头/等下）。
# ⚠ 新增条目必须过 tests/test_voice_honest_fallback.py 的「台阶自身不构成
# 承诺/不被自己剥掉」门禁——否则守卫自噬。
_STEP_LINES: Dict[str, Tuple[str, ...]] = {
    "zh": (
        "语音这会儿发不出去，网卡得很，先打字陪你，回头好了给你补一条～",
        "哎呀，语音好像发不出去，先打字陪你聊，回头好了我再补～",
        "我这边语音抽风了，先打字回你哈，等下好了给你补上～",
        "网不太行，语音发不过去，先文字聊着，回头补给你一条～",
        "语音这边出了点小状况，先打字陪你，好了第一时间补一条给你😊",
    ),
    "en": (
        "ugh, voice notes won't go through right now — let me text you "
        "first, I'll make it up to you later~",
        "my voice notes are acting up, so I'll just type for now, "
        "promise I'll make it up to you 😅",
        "can't get voice to work right now, texting you instead — "
        "I owe you one~",
    ),
}

_CONTENT_RE = re.compile(r"[\w\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]")


def resolve_honest_fallback_cfg(
    config: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """取 ``telegram.voice_reply.honest_fallback`` 块（缺失/坏形返回空 dict）。"""
    try:
        blk = ((((config or {}).get("telegram") or {}).get("voice_reply")
                or {}).get("honest_fallback"))
        return dict(blk) if isinstance(blk, dict) else {}
    except Exception:
        return {}


def honest_fallback_enabled(config: Optional[Dict[str, Any]]) -> bool:
    """开关（**默认开**）：正确性守卫家族哲学——出站不许撒谎不是 feature。"""
    return bool(resolve_honest_fallback_cfg(config).get("enabled", True))


def resolve_fallback_lang(sample: str) -> str:
    """台阶池语言（只有 zh/en 两池）：复用 outbound_promise_guard 的
    文字系统粗分——ja/ko/拉丁语种统一落 en 池（比发错中文强）。"""
    return "zh" if _script_lang(sample) == "zh" else "en"


def pick_step_line(text: str, lang: str = "zh") -> str:
    """按 crc32(text) 从池确定性取一条台阶（同文本恒定、不同文本轮转）。"""
    pool = _STEP_LINES.get(lang if lang in _STEP_LINES else "en") or ()
    if not pool:
        return ""
    idx = zlib.crc32(str(text or "").encode("utf-8", "ignore")) % len(pool)
    return pool[idx]


def _strip_voice_lies(text: str, *, media_context: bool) -> Tuple[str, bool]:
    """句级剥离 **voice** 类承诺句（+ 客户在要语音时的「已发」断言句）。

    返回 ``(结果, 是否剥到)``；没剥到时原文**逐字节**原样返回（含首尾空白），
    调用方据第二元判改动，避免「只掉了个尾空格也算改写」的假阳性。
    剥后只剩标点/空白 → 视同剥空返回 ``("", True)``。
    """
    raw = str(text or "")
    if not raw.strip():
        return raw, False
    parts = _SENT_SPLIT_RE.split(raw)
    out = []
    dropped = False
    i = 0
    while i < len(parts):
        seg = parts[i]
        delim = parts[i + 1] if i + 1 < len(parts) else ""
        is_lie = False
        if seg.strip():
            if _sentence_is_promise(seg) == KIND_VOICE:
                is_lie = True
            elif media_context and _sentence_claim_kind(
                    seg, media_context=True) == KIND_VOICE:
                is_lie = True
        if is_lie:
            dropped = True
            i += 2
            continue  # 丢句 + 尾随定界符
        out.append(seg)
        if delim:
            out.append(delim)
        i += 2
    if not dropped:
        return raw, False
    res = "".join(out).strip()
    if res and not _CONTENT_RE.search(res):
        return "", True
    return res, True


def apply_voice_failure_fallback(
    text: str, peer_text: str, lang: str = "zh",
) -> Tuple[str, bool]:
    """语音尝试失败后的出站文字修正。返回 ``(新文本, 是否改动)``。

    a) 剥掉 voice 类承诺/断言句（句级删除，保留其余内容）；
    b) 客户点名要语音（``wants_media(peer_text)=='voice'``）→ 末尾补一句
       诚实台阶（crc32(text) 确定性取池；lang 非 zh 用 en 池）——语音没来
       这件事必须给交代，哪怕原文一句承诺都没写；
    c) 剥空且无台阶可追加 → 返回原文（宁可保守不发空消息）。
    """
    raw = str(text or "")
    if not raw.strip():
        return raw, False
    peer_wants_voice = wants_media(str(peer_text or "")) == KIND_VOICE
    stripped, dropped = _strip_voice_lies(raw, media_context=peer_wants_voice)
    if not peer_wants_voice:
        if not dropped or not stripped.strip():
            return raw, False       # 没剥到 / 剥空无追加 → 保守发原文
        return stripped, True
    step = pick_step_line(raw, lang=lang)
    if not step:                    # 池空（理论不可能）→ 退化为纯剥离语义
        if dropped and stripped.strip():
            return stripped, True
        return raw, False
    base = stripped.strip() if dropped else raw.strip()
    new_text = f"{base}\n{step}" if base else step
    return new_text, True


__all__ = [
    "apply_voice_failure_fallback", "honest_fallback_enabled",
    "pick_step_line", "resolve_fallback_lang", "resolve_honest_fallback_cfg",
]
