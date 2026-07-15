"""LLM 口语化改写（活人感 A 档，2026-07-14）——本地小模型深度口语化，规则档兜底。

背景（语音活人感诊断的最大单点杠杆）：规则档 ``voice_colloquial.colloquialize`` 零延迟、
确定性，但只做「书面词替换 + 句首迟疑词」，一段话中间仍是书面结构。LLM 档用 LAN 本地
模型（``ai.fallback`` 的 qwen3:30b，**不占云成本**）做**语境贴合**的口语化——拆生硬长句、
自然口语连接、语气词落在该落的地方，更接近「真人在说」。

把「非确定性 LLM + 有延迟」驯服成生产可用的四道工程护栏：
  1. **缓存**（进程级 LRU，键=原文+情绪+lead+style）：非确定性结果缓存化 → 同原文永远
     同口语版（对 TTS 缓存/预渲染键友好）+ 省重复调用。
  2. **端点熔断**：连续失败 ≥N 次进冷却期，冷却期直接回落（不让挂掉的端点每条拖满超时）。
  3. **输出消毒 + 校验**：剥元话语前缀/引号；长度[0.3x,1.8x]、语言（中文）、非空校验
     不过 → 判为异常回落（防 LLM 发挥过度/截断/输出解释/串语言）。
  4. **失败即回落**：任何异常/超时/校验不过 → ``None``，调用方回落规则档（绝不阻塞语音）。

短句 / 非中文 no-op 与规则档同口径（保预渲染命中 + 防中文口语词 garble 外语）。
``async``（在 TTS 合成 async 上下文调用）；``ai_client`` 可注入（测试）。

可单测纯函数：build_colloquial_prompt / sanitize_llm_output / _cache_key。
"""
from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
from collections import OrderedDict
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ── 情绪 → 语气词（喂给 LLM 的语气基调）─────────────────────────────────────
_EMOTION_TONE = {
    "neutral": "自然放松", "warm": "温暖亲切", "happy": "开心愉快",
    "playful": "俏皮活泼", "excited": "兴奋雀跃", "empathetic": "温柔共情",
    "apologetic": "诚恳", "calm": "平静舒缓", "sad": "低落轻声", "serious": "认真",
}


def build_colloquial_prompt(emotion: str = "neutral", lead: bool = True,
                            style: str = "", disfluency: bool = False) -> str:
    """构建口语化系统提示（纯函数）。``lead``＝是否允许句首口语连接（分条非首条关）；
    ``disfluency``＝允许一处轻口误自纠（⑥，调用方按文本 crc 低频开启）。"""
    tone = _EMOTION_TONE.get(str(emotion or "").strip().lower(), "自然放松")
    parts = [
        "你是口语改写助手。把用户给的一句话改写成【适合用语音说出来】的口语版本。",
        "严格要求：",
        "1. 保持原意和所有信息不变，绝对不能增加新信息、遗漏或篡改信息——"
        "数字、时间、日期、金额、人名、地名等关键信息必须原样保留（可用中文数字念法，但值不能变）；",
        "2. 把书面表达换成日常口语（如「因此」→「所以」、「是否」→「是不是」），拆开生硬的长句；",
        f"3. 语气{tone}；",
    ]
    if lead:
        parts.append("4. 可以在开头加一点点自然的口语连接（如「其实」「话说」），但别每句都加；")
    else:
        parts.append("4. 直接说正文，不要用语气词或开场白开头；")
    if str(style or "").strip():
        parts.append(f"5. 说话风格：{str(style).strip()}；")
    if disfluency:
        parts.append("6. 可以带至多一处很轻的自然口误自纠（如「明天…啊不对，后天」），要随意不刻意；")
    parts.append("只输出改写后的那一句话本身，不要加引号、不要解释、不要任何前后缀。")
    return "\n".join(parts)


# ── 输出消毒 / 校验（纯函数）─────────────────────────────────────────────────
_PREFIX_RE = re.compile(
    r"^\s*(口语版|口语|改写后?|结果|输出|回答|答案|译文)\s*[:：]\s*")
# 元话语标记：LLM 若加了解释段，从这些标记处截断（只保留改写正文）
_META_SPLIT_RE = re.compile(r"(?:\n\s*\n|解释[:：]|说明[:：]|注[:：]|原文[:：]|---)")
_QUOTE_CHARS = "「」『』“”\"'‘’"


def sanitize_llm_output(
    raw: str, original: str, *, max_expand: float = 1.8,
) -> Optional[str]:
    """消毒 + 校验 LLM 口语化输出。异常（空/超长/超短/串语言/元话语）→ None。纯函数。"""
    from src.ai.voice_colloquial import _is_chinese_dominant

    t = str(raw or "").strip()
    if not t:
        return None
    t = _PREFIX_RE.sub("", t).strip()
    # 元话语段截断（防「改写正文\n\n解释：...」把解释念出来）
    m = _META_SPLIT_RE.search(t)
    if m:
        t = t[:m.start()].strip()
    t = t.strip(_QUOTE_CHARS).strip()
    if not t:
        return None
    core = str(original or "").strip()
    if not core:
        return None
    # 长度守卫：过短=截断/丢信息，过长=发挥过度/夹带解释（+8 给短文本余量）
    if len(t) < len(core) * 0.3:
        return None
    if len(t) > len(core) * float(max_expand) + 8:
        return None
    # 语言守卫：原文中文而输出串了别的语言 → 拒（防 garble）
    if not _is_chinese_dominant(t):
        return None
    return t


# ── 缓存（进程级 LRU）────────────────────────────────────────────────────────
_CACHE: "OrderedDict[str, str]" = OrderedDict()
_CACHE_MAX = 512
_CACHE_LOCK = threading.Lock()


def _cache_key(text: str, emotion: str, lead: bool, style: str,
               disfluency: bool = False) -> str:
    raw = (f"{text}\x1f{emotion}\x1f{int(bool(lead))}\x1f{style}"
           f"\x1f{int(bool(disfluency))}")
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _cache_get(key: str) -> Optional[str]:
    with _CACHE_LOCK:
        if key in _CACHE:
            _CACHE.move_to_end(key)
            return _CACHE[key]
    return None


def _cache_put(key: str, value: str) -> None:
    with _CACHE_LOCK:
        _CACHE[key] = value
        _CACHE.move_to_end(key)
        while len(_CACHE) > _CACHE_MAX:
            _CACHE.popitem(last=False)


# ── 端点熔断（连续失败 → 冷却期直接回落，不让挂掉的端点每条拖满超时）──────────
_FAIL_THRESHOLD = 3
_COOLDOWN_SEC = 60.0
_fail_streak = 0
_cooldown_until = 0.0
_last_ok_ts = 0.0     # 最近一次成功改写（wall clock，健康信号用）
_last_fail_ts = 0.0   # 最近一次失败（wall clock，健康信号用）
_CB_LOCK = threading.Lock()


def _in_cooldown() -> bool:
    with _CB_LOCK:
        return time.monotonic() < _cooldown_until


def _record_success() -> None:
    global _fail_streak, _cooldown_until, _last_ok_ts
    with _CB_LOCK:
        _fail_streak = 0
        _cooldown_until = 0.0
        _last_ok_ts = time.time()


def _record_failure() -> None:
    global _fail_streak, _cooldown_until, _last_fail_ts
    with _CB_LOCK:
        _fail_streak += 1
        _last_fail_ts = time.time()
        if _fail_streak >= _FAIL_THRESHOLD:
            _cooldown_until = time.monotonic() + _COOLDOWN_SEC
            logger.info("[voice_colloquial_llm] 连续 %d 次失败 → 冷却 %ds（回落规则档）",
                        _fail_streak, int(_COOLDOWN_SEC))


def health_signal() -> dict:
    """健康信号快照（HealthWatchdog 巡检消费，2026-07-15 九连败静默事故修复）。

    此前端点长期挂掉只会「每 60s 熔断-重试」无限循环，日志有记录但无人被通知，
    语音口语化静默降级规则档。信号口径与 avatar_voice_stats.hang_signal 对齐：
    连败数 + 最近成/败时刻，阈值判断留在 watchdog（可配置、可测试）。
    """
    with _CB_LOCK:
        return {
            "fail_streak": int(_fail_streak),
            "in_cooldown": time.monotonic() < _cooldown_until,
            "last_ok_ts": float(_last_ok_ts),
            "last_fail_ts": float(_last_fail_ts),
        }


# ── 本地 AIClient 懒加载（默认关，开了才构造一次；测试可注入）──────────────────
_AI_CLIENT: Optional[Any] = None
_AI_LOCK = threading.Lock()


def _get_ai_client(injected: Optional[Any] = None) -> Optional[Any]:
    global _AI_CLIENT
    if injected is not None:
        return injected
    if _AI_CLIENT is None:
        with _AI_LOCK:
            if _AI_CLIENT is None:
                try:
                    from src.ai.ai_client import AIClient
                    from src.utils.config_manager import ConfigManager
                    _AI_CLIENT = AIClient(ConfigManager())
                except Exception as exc:
                    logger.warning("[voice_colloquial_llm] AIClient 懒加载失败: %s", exc)
                    return None
    return _AI_CLIENT


async def llm_colloquialize(
    text: str,
    *,
    ai_client: Optional[Any] = None,
    emotion: str = "neutral",
    lead: bool = True,
    style: str = "",
    min_chars: int = 12,
    timeout_sec: float = 8.0,
    max_expand: float = 1.8,
    disfluency: bool = False,
) -> Optional[str]:
    """本地 LLM 口语化。命中缓存直接返回；失败/超时/熔断/校验不过 → None（回落规则档）。

    短句（<min_chars）/ 非中文 → None（与规则档同口径 no-op）。
    ``disfluency``＝本条允许一处轻口误自纠（⑥，调用方按 crc 低频开启；进缓存键）。
    """
    core = str(text or "").strip()
    if len(core) < max(1, int(min_chars)):
        return None
    from src.ai.voice_colloquial import _is_chinese_dominant
    if not _is_chinese_dominant(core):
        return None

    key = _cache_key(core, emotion, bool(lead), style, bool(disfluency))
    hit = _cache_get(key)
    if hit is not None:
        return hit or None          # 缓存空串＝已知无有效改写 → 回落规则档

    if _in_cooldown():
        return None                 # 端点冷却期：秒回落，不调 LLM

    client = _get_ai_client(ai_client)
    if client is None or not hasattr(client, "rewrite_local"):
        return None

    system = build_colloquial_prompt(emotion, bool(lead), style,
                                     disfluency=bool(disfluency))
    try:
        raw = await client.rewrite_local(system, core, timeout_sec=timeout_sec)
    except Exception as exc:
        logger.debug("[voice_colloquial_llm] rewrite_local 异常: %s", exc)
        raw = None

    out = sanitize_llm_output(raw, core, max_expand=max_expand) if raw else None
    if out and out != core:
        _record_success()
        _cache_put(key, out)
        return out
    # 校验不过 / 空 / 与原文相同：记一次失败（推进熔断），缓存空串短期不重试同句
    _record_failure()
    _cache_put(key, "")
    return None


def reset_state() -> None:
    """清空缓存 + 熔断 + 懒加载客户端（测试用）。"""
    global _AI_CLIENT, _fail_streak, _cooldown_until, _last_ok_ts, _last_fail_ts
    with _CACHE_LOCK:
        _CACHE.clear()
    with _CB_LOCK:
        _fail_streak = 0
        _cooldown_until = 0.0
        _last_ok_ts = 0.0
        _last_fail_ts = 0.0
    with _AI_LOCK:
        _AI_CLIENT = None


__all__ = [
    "llm_colloquialize", "build_colloquial_prompt", "sanitize_llm_output",
    "health_signal", "reset_state",
]
