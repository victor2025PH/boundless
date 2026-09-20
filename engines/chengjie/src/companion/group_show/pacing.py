# -*- coding: utf-8 -*-
"""群戏节奏引擎 —— 让一场戏「发得像人」而不是「发得像定时任务」。

**要解决的事故**：台词写得再自然，只要发送间隔整齐（每 60 秒一条），或者一条 80 字的
长回复在上一条落地后 2 秒就冒出来，群里任何一个老手都能一眼看出这是脚本。时间维度上的
穿帮不需要读内容，肉眼扫一下时间戳就够——它是比文本更难伪装、也更常被忽略的识别特征。

**真人发言的时间由三段构成**，本模块照着拆：

1. **阅读**上一条要花时间（越长看越久）；
2. **打字**要花时间（越长打越久，且中文/移动端比想象中慢）；
3. **想不想现在说**是随机的——真人是「泊松过程」：多数时候间隔不长，偶尔卡很久
   （去倒了杯水、在开会、刷到别的群）。所以抖动用**指数分布**而不是均匀分布：
   均匀分布产生的是「在 40~80 秒之间徘徊」，那依然整齐得反常；指数分布产生的是
   「大部分挺快，偶尔来一个长尾」，那才是真人聊天记录的形状。

**确定性**：抖动全部走调用方传入的 :class:`random.Random`（或 ``seed``），
同 seed 必然同结果——排练（dry-run）才能复现、才能对节奏打分做回归。本模块是纯函数，
无 I/O、无全局可变状态。
"""
from __future__ import annotations

import random
from typing import Any, List, Optional, Sequence

from src.companion.group_show.playbook import Beat, PACE_BASE_SECONDS

# ── 常量（都是可解释的经验值，不是魔数） ──────────────────────────────────────

#: 打字速度（字/秒）。3.5 是中文移动端输入的偏乐观估计——真人还要选词、改字。
DEFAULT_CHARS_PER_SEC = 3.5

#: 阅读速度（字/秒）。读比打快得多，但群里是「扫一眼」而非精读，故取保守值。
READING_CHARS_PER_SEC = 12.0

#: 基础间隔中交给随机抖动的份额（其余为确定性部分）。
#: 取 0.45 是折中：全随机会让 chatty/normal/slow 三档在单条上失去区分度，
#: 全确定性又回到「整齐得反常」。均值上仍然等于 base，档位语义不被稀释。
JITTER_SHARE = 0.45

#: 单次间隔的上下界（秒）。下界防指数分布抽到接近 0 的「秒回长文」，
#: 上界防长尾抽出半小时把整场戏拖散。
MIN_INTERVAL_SECONDS = 5.0
MAX_INTERVAL_SECONDS = 600.0

#: 排期时台词还没生成，用这个字数估打字/阅读时间（约等于一句群聊短句）。
NOMINAL_TEXT_LEN = 28


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    if v != v or v in (float("inf"), float("-inf")):  # NaN / inf
        return default
    return v


def base_seconds_for_pace(pace: Any) -> float:
    """语速档 → 基础间隔秒；未知/非法档回落 ``normal``。

    剧本是人手写的 YAML，``pace: 快`` 这种写法迟早会出现——排期是运行期热路径，
    这里宁可安静降级也不抛。真正的拼写检查在 ``validate_playbook``（离线校验）里做。
    """
    key = str(pace or "").strip().lower()
    return float(PACE_BASE_SECONDS.get(key, PACE_BASE_SECONDS["normal"]))


def typing_seconds(text_len: Any, *, chars_per_sec: float = DEFAULT_CHARS_PER_SEC) -> float:
    """打这么多字需要多久（秒）。

    :param text_len: 待发文本长度（字符数）；负数/非数按 0 处理。
    :param chars_per_sec: 打字速度，``<= 0`` 时回落 :data:`DEFAULT_CHARS_PER_SEC`。
    """
    length = max(0.0, _to_float(text_len, 0.0))
    cps = _to_float(chars_per_sec, DEFAULT_CHARS_PER_SEC)
    if cps <= 0:
        cps = DEFAULT_CHARS_PER_SEC
    return length / cps


def reading_seconds(text_len: Any, *, chars_per_sec: float = READING_CHARS_PER_SEC) -> float:
    """读完上一条需要多久（秒）——没上一条（长度 0）就不占时间。"""
    length = max(0.0, _to_float(text_len, 0.0))
    cps = _to_float(chars_per_sec, READING_CHARS_PER_SEC)
    if cps <= 0:
        cps = READING_CHARS_PER_SEC
    return length / cps


def beat_interval_seconds(
    beat: Any,
    *,
    prev_text_len: Any = 0,
    next_text_len: Any = 0,
    rng: Optional[random.Random] = None,
) -> float:
    """这一拍距上一拍应该隔多久（秒）。

    ``间隔 = 基础档位(确定性部分 + 指数抖动) + 读上一条的时间 + 打本条的时间``，
    最后夹在 ``[MIN_INTERVAL_SECONDS, MAX_INTERVAL_SECONDS]``。

    指数抖动的均值刻意等于 ``base * JITTER_SHARE``，因此**整体间隔的期望恰好是**
    ``base + 阅读 + 打字``——抖动只改变形状（长尾），不偷偷改变档位语义。

    :param beat: :class:`Beat`（只读它的 ``pace``；给 ``None`` 或没有 ``pace``
        的对象也不会崩，按 ``normal`` 算）。
    :param prev_text_len: 上一条群消息的长度，用于估阅读时间。
    :param next_text_len: 本拍将要发出的文本长度，用于估打字时间。
    :param rng: :class:`random.Random` 实例；**传它才可复现**。缺省时现开一个
        无种子实例（真发场景要的就是不可预测）。
    """
    base = base_seconds_for_pace(getattr(beat, "pace", None))
    r = rng if rng is not None else random.Random()

    # expovariate(1.0) 均值为 1 → 乘上份额后均值恰为 base * JITTER_SHARE
    jitter = r.expovariate(1.0) * base * JITTER_SHARE
    total = (
        base * (1.0 - JITTER_SHARE)
        + jitter
        + reading_seconds(prev_text_len)
        + typing_seconds(next_text_len)
    )
    return max(MIN_INTERVAL_SECONDS, min(MAX_INTERVAL_SECONDS, total))


def plan_schedule(
    beats: Sequence[Beat],
    *,
    text_lens: Optional[Sequence[Any]] = None,
    seed: int = 0,
) -> List[float]:
    """给整套 beats 排出累计时刻表（秒，相对开场）。

    首拍恒为 ``0.0``（开场那条即刻发），其后逐拍累加 :func:`beat_interval_seconds`。
    因为每步间隔都 ``>= MIN_INTERVAL_SECONDS``，结果**严格递增**。

    :param beats: 本场的节拍序列（空序列 → 空表）。
    :param text_lens: 与 ``beats`` 对齐的台词长度；排期通常发生在台词生成**之前**，
        缺省/缺项按 :data:`NOMINAL_TEXT_LEN` 估算（而不是按 0，否则排出来的表会
        明显偏紧，跟真发时的观感对不上）。
    :param seed: 抖动种子——**同 seed 同结果**，排练可复现、可回归。
    """
    items = list(beats or [])
    if not items:
        return []

    lens: List[float] = []
    for i in range(len(items)):
        raw: Any = NOMINAL_TEXT_LEN
        if text_lens is not None and i < len(text_lens):
            raw = text_lens[i]
        lens.append(max(0.0, _to_float(raw, float(NOMINAL_TEXT_LEN))))

    rng = random.Random(seed)
    schedule: List[float] = [0.0]
    cursor = 0.0
    for i in range(1, len(items)):
        cursor += beat_interval_seconds(
            items[i],
            prev_text_len=lens[i - 1],
            next_text_len=lens[i],
            rng=rng,
        )
        schedule.append(cursor)
    return schedule


__all__ = [
    "DEFAULT_CHARS_PER_SEC",
    "READING_CHARS_PER_SEC",
    "JITTER_SHARE",
    "MIN_INTERVAL_SECONDS",
    "MAX_INTERVAL_SECONDS",
    "NOMINAL_TEXT_LEN",
    "base_seconds_for_pace",
    "typing_seconds",
    "reading_seconds",
    "beat_interval_seconds",
    "plan_schedule",
]
