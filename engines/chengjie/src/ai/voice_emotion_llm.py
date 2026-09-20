# -*- coding: utf-8 -*-
"""关键词读不出情绪时，用一次小模型调用补判（**兜底层，不是主判**）。

为什么需要：``voice_emotion._TEXT_CUES`` 是确定性的，命中即准，但它只认写进词表的
说法。2026-09-20 实测英文人设的五级瀑布全部落空、每句都用同一个基线语气念出来，
老板一耳朵听出「太AI化」。词表补齐英文后常见句子能读出情绪了，剩下的长尾
（「I keep thinking about what you said in the car」这种没有任何情绪词、情绪全在
语境里的句子）只有模型判得动。

**三条自我约束**，否则这层弊大于利：

1. **只在词表没命中时才调**。命中时确定性结果一定更可信，且省掉一次调用——
   语音链是在线路径，每条回复都多等 0.5s 是能被听出来的。
2. **输出必须落在既有情绪枚举里**，越界一律丢弃。模型返回自由文本（"bittersweet"）
   对下游只是脏数据：``EmotionSpec`` 会把它规整成 neutral，反而比基线更平。
3. **任何失败都原样退回确定性结果**，绝不抛。判不出情绪的代价是「用基线语气念」，
   也就是改动前的行为；为它冒险中断一条语音得不偿失。

端点与熔断复用口语化链（``colloquial.llm_endpoints``）：同一批 LAN 机器、同一套
逐端点冷却，不新增一套要单独运维的东西。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Tuple

from src.ai.voice_emotion import EMOTIONS, EmotionSpec, coerce_emotion, text_cue_emotion

logger = logging.getLogger(__name__)

# 判不出来时模型该回的词：留一个明确出口，比逼它在 10 个情绪里硬选一个强。
_ABSTAIN = "unclear"
# 兜底层刻意不判 neutral：这层只在「基线兜底」时才跑，判 neutral 等于把语气压得比
# 基线更平——要么读出一个真情绪，要么弃权留给基线。
_ALLOWED: Tuple[str, ...] = tuple(e for e in EMOTIONS if e != "neutral")

_PROMPT = (
    "You label the emotional tone a person is expressing in their own message.\n"
    "Reply with EXACTLY ONE word from this list and nothing else:\n"
    + ", ".join(_ALLOWED) + f", {_ABSTAIN}\n\n"
    "Rules:\n"
    "- Label the SPEAKER's own feeling, not the topic and not the listener's.\n"
    f"- Politeness or small talk is not emotion. If the tone is flat, reply {_ABSTAIN}.\n"
    f"- If you are not confident, reply {_ABSTAIN}. Guessing is worse than abstaining.\n"
    "- No punctuation, no explanation, no quotes."
)

# 同一句话在重试/分条里会被问多次；小缓存足够吃掉这类重复，不做持久化。
_CACHE: Dict[str, str] = {}
_CACHE_MAX = 512


def _cache_get(key: str) -> Optional[str]:
    return _CACHE.get(key)


def _cache_put(key: str, val: str) -> None:
    if len(_CACHE) >= _CACHE_MAX:
        _CACHE.clear()          # 整清比 LRU 简单，缓存只为省重复调用，命中率无所谓
    _CACHE[key] = val


def reset_state() -> None:
    """清空补判缓存（测试用；与口语化链的 ``reset_state`` 同口径）。"""
    _CACHE.clear()


def llm_cfg(voice_cfg: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """``voice_cfg.emotion.llm`` 块（缺失 → 空 dict ＝ 关）。纯函数。"""
    emo = (voice_cfg or {}).get("emotion")
    if not isinstance(emo, dict):
        return {}
    blk = emo.get("llm")
    return blk if isinstance(blk, dict) else {}


def should_consult(
    spec: Any, text: str, voice_cfg: Optional[Dict[str, Any]],
) -> bool:
    """这条回复值不值得为情绪多花一次模型调用？（纯函数，可单测）

    三个否决：开关没开 / 文本太短（短句没语境，模型只会瞎猜）/ 词表已经读出情绪。
    """
    cfg = llm_cfg(voice_cfg)
    if not cfg.get("enabled"):
        return False
    t = str(text or "").strip()
    try:
        min_chars = int(cfg.get("min_chars", 16) or 16)
    except (TypeError, ValueError):
        min_chars = 16
    if len(t) < max(1, min_chars):
        return False
    return text_cue_emotion(t) is None


def parse_label(raw: Any) -> Optional[str]:
    """模型输出 → 合法情绪名；弃权/越界/啰嗦 → None（纯函数）。

    只容忍「一个词 + 标点」这一种啰嗦：多词输出说明模型在解释而不是在标注，
    从里面挑词等于替它猜，正是本层最该避免的事。
    """
    s = str(raw or "").strip().lower().strip(".。!！\"'“”`")
    if not s or " " in s or "\n" in s:
        return None
    return s if s in _ALLOWED else None


async def refine_emotion(
    spec: Any,
    text: str,
    *,
    voice_cfg: Optional[Dict[str, Any]] = None,
    colloquial_cfg: Optional[Dict[str, Any]] = None,
) -> Any:
    """词表没读出情绪时用模型补判；判不出/出任何岔子 → 原样返回 ``spec``。

    ``colloquial_cfg``＝``voice_cfg.colloquial``（借它的 ``llm_endpoints``）。
    """
    if not should_consult(spec, text, voice_cfg):
        return spec
    core = str(text or "").strip()
    cached = _cache_get(core)
    if cached is not None:
        return _apply(spec, cached) if cached else spec

    label = ""
    try:
        from src.ai.voice_colloquial_llm import (
            _endpoint_key,
            _ep_in_cooldown,
            _rewrite_via_endpoint,
            parse_llm_endpoints,
        )
        eps = parse_llm_endpoints(
            (colloquial_cfg or {}).get("llm_endpoints"))
        for ep in eps:
            if _ep_in_cooldown(_endpoint_key(ep)):
                continue
            try:
                raw = await _rewrite_via_endpoint(
                    ep, _PROMPT, core, temperature=0.0, max_tokens=8)
            except Exception as exc:
                logger.debug("[voice_emotion_llm] 端点 %s 异常: %s",
                             _endpoint_key(ep), exc)
                continue
            label = parse_label(raw) or ""
            break
    except Exception:
        logger.debug("[voice_emotion_llm] 补判异常（回落确定性结果）", exc_info=True)
        return spec

    _cache_put(core, label)
    if not label:
        return spec
    logger.info("[voice_emotion_llm] 补判 %s ← %r", label, core[:40])
    return _apply(spec, label)


def _apply(spec: Any, label: str) -> Any:
    """把补判出的情绪盖到原 spec 上，保留原强度/语速（只换「什么情绪」）。"""
    try:
        base = coerce_emotion(spec)
        return EmotionSpec(label, intensity=base.intensity, pace=base.pace)
    except Exception:
        return EmotionSpec(label)
