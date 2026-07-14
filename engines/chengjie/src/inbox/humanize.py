"""发送前拟人节奏协作器（已读 → 打字续挂 → 延迟）。

把「真人回复前的表演序列」收敛为**单一纯协作器**：先发平台「已读」回执，再在
打字延迟期间**周期性**挂「正在输入 / 正在录音」状态直到延迟耗尽。多条发送路径
（编排器全自动 autosend、L3 缓冲话术，未来原生 A 线回复）共用同一节奏，消除各自
内联的重复与不一致（此前只有 autosend 有打字续挂）。

为什么是「协作器」而非「策略类」：三条路径的**发送动作**完全不同（编排器 callback /
orch.send / pyrogram 直发），强行统一发送会引入耦合；而「发送前的拟人序列」是它们
**唯一真正相同**的部分，只抽这段、发送留在各调用方，是恰到好处的抽象边界。

设计：
- **纯协作器**：所有副作用经注入的 async 回调（mark_read / typing / sleep）完成，
  零平台耦合、零 IO import → 可确定性单测（假回调 + 假 sleep 驱动）。
- **best-effort**：mark_read / typing 回调异常都吞掉并继续——拟人增强绝不阻断发送。
- **action 语义**：语音回复传 ``record_audio`` → 打字状态显示「正在录音」，否则「正在输入」。
- **计数钩子**：``on_marked`` / ``on_typing`` 在对应回调**成功**后触发（供调用方累计指标），
  异常不触发。
"""
from __future__ import annotations

import logging
import random
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Optional

logger = logging.getLogger(__name__)

# Telegram chat action 约 5s 自动过期 → 打字延迟长于此值须周期续挂才不断续；
# WhatsApp presence 'composing' 亦受益于周期刷新。
DEFAULT_TYPING_REFRESH_SEC = 4.0

# 延迟低于此值时不挂打字状态（否则打字气泡闪一下消息就到，反而不真实）→ 直接静默等。
DEFAULT_MIN_TYPING_DELAY_SEC = 1.0

# 内容自适应延迟默认参数（真人「读+想+打字」时长模型）。
_DEFAULT_BASE_SEC = 1.0          # 读消息 + 起念的固定开销
_DEFAULT_PER_CHAR_SEC = 0.08     # 每字打字耗时（CJK 闲聊 ~8-12 字/秒的保守值）
_DEFAULT_JITTER = 0.2            # ±20% 随机抖动（避免同长度恒定时长露馅）
_DEFAULT_AROUSAL_SWING = 0.5     # arousal 对节奏的最大缩放幅度（±50%）


def _arousal_scale(arousal: Optional[float], swing: float = _DEFAULT_AROUSAL_SWING) -> float:
    """把回复文本的**激活度** arousal(0~1) 映射为打字速度缩放。

    真人「怎么打这条回复」：激活度高（兴奋/急切）→ 打字更快（scale<1）；激活度低
    （平静/斟酌/安慰）→ 更慢更深思（scale>1）。arousal=0.5（中性）→ scale=1.0。
    公式：``1 + (0.5 - arousal) × 2 × swing``，夹到 ``[1-swing, 1+swing]``。
    arousal=None（未知）→ 1.0（不缩放）。此信号取自回复自身（非客户消息），语义正确。
    """
    if arousal is None:
        return 1.0
    try:
        a = max(0.0, min(1.0, float(arousal)))
    except (TypeError, ValueError):
        return 1.0
    scale = 1.0 + (0.5 - a) * 2.0 * float(swing)
    return max(1.0 - swing, min(1.0 + swing, scale))


def estimate_thinking_delay(
    text: str,
    *,
    base_sec: float = _DEFAULT_BASE_SEC,
    per_char_sec: float = _DEFAULT_PER_CHAR_SEC,
    min_sec: float = 0.0,
    max_sec: float = 12.0,
    arousal: Optional[float] = None,
    jitter: float = _DEFAULT_JITTER,
    rng: Optional[Callable[[float, float], float]] = None,
) -> float:
    """按回复内容估「真人思考+打字」延迟（纯函数）。

    模型：``base_sec + per_char_sec × 字数``，再乘 ``arousal`` 节奏缩放、加 ±jitter 抖动，
    最后夹到 ``[min_sec, max_sec]``。字数越多越久（长回复本就要打更久）；``arousal`` 高
    （兴奋/急切）打字更快、低（平静/安慰）更慢——取自**回复自身**的激活度（语义正确，
    非客户情绪）。``rng`` 可注入以确定性单测。``max_sec<=0`` → 返回 0（关闭）。
    """
    _rng = rng or random.uniform
    if max_sec <= 0 or max_sec < min_sec:
        return 0.0
    n = len(str(text or "").strip())
    raw = float(base_sec) + float(per_char_sec) * n
    raw *= _arousal_scale(arousal)
    if jitter and jitter > 0:
        raw *= _rng(1.0 - jitter, 1.0 + jitter)
    return max(float(min_sec), min(float(max_sec), raw))


def _apply_persona_overrides(
    block: Optional[Dict[str, Any]], persona_id: str,
) -> Dict[str, Any]:
    """把 ``block.persona_overrides[persona_id]`` 合并到顶层节奏参数上（纯函数）。

    人设化节奏：不同人设可有不同打字速度/上限/是否自适应（急性子 ``per_char_sec`` 小、
    慢热型大）。覆盖放在延迟块内的 ``persona_overrides`` 映射——避免改人设 schema/跨文件
    耦合，运营在同一处即可见节奏全貌。无 persona_id / 无对应覆盖 → 原样返回顶层默认
    （零行为变更）。返回的 dict 已移除 ``persona_overrides`` 自身键（不参与后续解析）。
    """
    b = dict(block or {})
    ov = b.pop("persona_overrides", None)
    if persona_id and isinstance(ov, dict):
        pov = ov.get(str(persona_id))
        if isinstance(pov, dict):
            b.update({k: v for k, v in pov.items() if k != "persona_overrides"})
    return b


def _derive_arousal(text: str) -> Optional[float]:
    """从回复文本估激活度 arousal（best-effort，语义正确：取自**回复自身**）。

    懒依赖 ``analyze_emotion``——仅在此装配点用一次，保持 estimate_thinking_delay 纯净。
    任何异常/不可用 → None（不缩放）。
    """
    t = str(text or "").strip()
    if not t:
        return None
    try:
        from src.utils.emotional_context import analyze_emotion
        emo = analyze_emotion(t) or {}
        a = emo.get("arousal")
        return float(a) if a is not None else None
    except Exception:
        return None


@dataclass
class PacingResult:
    """一次延迟解析的结果 + 诊断（供观测：目标 vs 实际 vs 已耗时 vs 激活度）。"""
    delay: float          # 本次**还需等待**的秒数（最终值）
    target: float         # 估出的目标思考时长（未扣已耗时；非自适应=随机值）
    elapsed: float        # 扣除的已耗时（秒）
    arousal: Optional[float]  # 回复激活度（自适应时用；None=未知/不适用）
    adaptive: bool        # 是否自适应模式
    enabled: bool         # 该延迟配置是否启用（max_sec>0）


def resolve_pacing(
    block: Optional[Dict[str, Any]],
    *,
    text: str = "",
    arousal: Optional[float] = None,
    elapsed_sec: float = 0.0,
    persona_id: str = "",
    rng: Optional[Callable[[float, float], float]] = None,
) -> PacingResult:
    """解析延迟配置为 ``PacingResult``（含诊断字段，供观测打点）。

    语义同 ``compute_pacing_delay``（后者是本函数 ``.delay`` 的薄封装，向后兼容）：
    - 先按 ``persona_id`` 合并 ``persona_overrides``（人设化节奏参数）。
    - ``max_sec<=0``/非法 → enabled=False，delay=0。
    - ``adaptive=false`` → ``uniform(min,max)``（不扣 elapsed）。
    - ``adaptive=true`` → 按长度 + 回复激活度估目标，再扣已耗时 ``max(0, 目标-已耗)``；
      ``arousal`` 未给则自动从 ``text`` 估（回复自身激活度，语义正确）。
    """
    b = _apply_persona_overrides(block, persona_id)
    _rng = rng or random.uniform
    try:
        min_sec = float(b.get("min_sec", 0) or 0)
        max_sec = float(b.get("max_sec", 0) or 0)
    except (TypeError, ValueError):
        return PacingResult(0.0, 0.0, 0.0, None, bool(b.get("adaptive", False)), False)
    adaptive = bool(b.get("adaptive", False))
    if max_sec <= 0 or max_sec < min_sec:
        return PacingResult(0.0, 0.0, 0.0, None, adaptive, False)
    if not adaptive:
        d = _rng(min_sec, max_sec)
        return PacingResult(d, d, 0.0, None, False, True)
    try:
        base = float(b.get("base_sec", _DEFAULT_BASE_SEC))
        per_char = float(b.get("per_char_sec", _DEFAULT_PER_CHAR_SEC))
        jitter = float(b.get("jitter", _DEFAULT_JITTER))
    except (TypeError, ValueError):
        base, per_char, jitter = _DEFAULT_BASE_SEC, _DEFAULT_PER_CHAR_SEC, _DEFAULT_JITTER
    if arousal is None:
        arousal = _derive_arousal(text)
    target = estimate_thinking_delay(
        text, base_sec=base, per_char_sec=per_char,
        min_sec=min_sec, max_sec=max_sec, arousal=arousal,
        jitter=jitter, rng=_rng)
    try:
        el = max(0.0, float(elapsed_sec or 0.0))
    except (TypeError, ValueError):
        el = 0.0
    delay = max(0.0, target - el) if el > 0 else target
    return PacingResult(delay, target, el, arousal, True, True)


def compute_pacing_delay(
    block: Optional[Dict[str, Any]],
    *,
    text: str = "",
    arousal: Optional[float] = None,
    elapsed_sec: float = 0.0,
    persona_id: str = "",
    rng: Optional[Callable[[float, float], float]] = None,
) -> float:
    """把一段延迟配置解析为本次**还需等待**的秒数（``resolve_pacing(...).delay`` 的薄封装）。

    单一装配点：autosend 与原生 A 线回复共用。诊断字段（目标/已耗/激活度）见 resolve_pacing。
    """
    return resolve_pacing(
        block, text=text, arousal=arousal, elapsed_sec=elapsed_sec,
        persona_id=persona_id, rng=rng).delay

MarkReadFn = Callable[[], Awaitable[Any]]
TypingFn = Callable[[str], Awaitable[Any]]
SleepFn = Callable[[float], Awaitable[Any]]


async def run_presend_humanization(
    *,
    delay: float = 0.0,
    action: str = "typing",
    mark_read: Optional[MarkReadFn] = None,
    typing: Optional[TypingFn] = None,
    sleep: SleepFn,
    refresh_sec: float = DEFAULT_TYPING_REFRESH_SEC,
    min_typing_delay: float = DEFAULT_MIN_TYPING_DELAY_SEC,
    on_marked: Optional[Callable[[], None]] = None,
    on_typing: Optional[Callable[[], None]] = None,
) -> None:
    """执行「已读 → 打字续挂 → 延迟」拟人序列。

    顺序：
      1. 若给了 ``mark_read`` → 调用一次（先看）。
      2. 若 ``delay > 0`` → 分片等待，每片先挂一次 ``typing``（若给了）再睡
         ``min(refresh_sec, 剩余)``，续挂到延迟耗尽（打字气泡不断续）。
      3. ``delay <= 0`` → 不挂打字（避免气泡闪一下消息就到，反而不真实）。
      4. ``0 < delay < min_typing_delay`` → 只静默睡完，不挂打字（气泡一闪即逝更假；
         如自适应扣除已耗时后只剩零点几秒）。

    绝不抛：mark_read / typing 异常仅记 debug 并继续；``sleep`` 由调用方保证可靠
    （测试注入即时返回的假 sleep）。
    """
    if mark_read is not None:
        try:
            await mark_read()
            if on_marked is not None:
                on_marked()
        except Exception:
            logger.debug("[humanize] mark_read 失败", exc_info=True)

    remaining = max(0.0, float(delay))
    if remaining <= 0:
        return
    # 超短延迟护栏：低于阈值只静默等，不挂打字（避免气泡一闪即逝的机械感）。
    if remaining < max(0.0, float(min_typing_delay)):
        await sleep(remaining)
        return
    step_max = max(0.5, float(refresh_sec))
    while remaining > 0:
        if typing is not None:
            try:
                await typing(action)
                if on_typing is not None:
                    on_typing()
            except Exception:
                logger.debug("[humanize] typing 失败", exc_info=True)
        step = min(step_max, remaining)
        await sleep(step)
        remaining -= step


__all__ = [
    "run_presend_humanization",
    "estimate_thinking_delay",
    "compute_pacing_delay",
    "resolve_pacing",
    "PacingResult",
    "DEFAULT_TYPING_REFRESH_SEC",
    "DEFAULT_MIN_TYPING_DELAY_SEC",
]
