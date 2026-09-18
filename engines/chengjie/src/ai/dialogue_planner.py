"""Dialogue Planner（AI Live OS Agent4，2026-07-24）——「表演规划」Agent。

把散在三处的「真人节奏」逻辑收敛为一个**纯函数规划器**：
  - 分条：``voice_clone_client.pack_voice_parts``（split_send 参数）
  - 条间停顿：情绪缩放的 gap_factor（本模块 ``emotion_gap_factor``，核心增量）
  - 开场思考延迟：``humanize.estimate_thinking_delay``（供不自带延迟的消费方用）
  - 每条语气引导/笑点提示（首条允许句首连接；笑只在文本自带笑意时）

产出一份 ``PerformanceScript``：这条回复分几条、条间节奏怎么伸缩、开场想多久、
每条带不带笑——发送链（A 线 sender / 未来 B 线 autosend）消费它，而不是各自内联算节奏。

**核心洞察**：真人的节奏本身带情绪——兴奋时抢着说（短促连发、停顿短），低落时话慢
而拖沓（停顿长）。固定 gap_factor 做不到这点。本 Agent 让节奏随情绪伸缩 = 真人感。

设计：纯函数 + 确定性（同输入同脚本，对 TTS 缓存/预渲染键安全）、零 IO、可单测。
门控：``avatar_voice.dialogue_planner.enabled``（消费方读；关=回落各自原内联节奏）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

# 情绪 → 条间节奏缩放系数（>1 = 停顿更长/更慢，<1 = 更短促/更快）。
# 真人：兴奋抢话、开心轻快；低落/共情话慢而停顿长。中性/温暖 = 基准。
_EMOTION_GAP_MULT = {
    "excited": 0.82, "happy": 0.90, "playful": 0.92,
    "neutral": 1.00, "warm": 1.00, "calm": 1.12,
    "empathetic": 1.20, "sad": 1.30, "serious": 1.08, "apologetic": 1.15,
}
_GAP_FACTOR_MIN, _GAP_FACTOR_MAX = 0.7, 1.6

# 情绪 → 是否倾向带笑点（仅在文本自带笑意时才加；没笑点硬笑=恐怖谷，刻意不做）。
_LAUGH_EMOTIONS = {"happy", "playful", "excited"}
_LAUGH_CUE = ("哈哈", "嘻嘻", "嘿嘿", "笑死", "太逗", "hhh", "lol")


def emotion_gap_factor(emotion: str, base_factor: float = 1.1,
                       intensity: float = 0.6) -> float:
    """情绪缩放的条间 ``gap_factor``（纯函数）。

    ``intensity`` 越高，节奏偏离基准越多（0.6＝基准力度，1.0＝放大到 1.5 幅）。
    结果夹到 ``[0.7, 1.6]`` 防过度（过短=抢话机械，过长=冷场）。
    """
    mult = _EMOTION_GAP_MULT.get(str(emotion or "").strip().lower(), 1.0)
    try:
        inten = max(0.0, min(1.0, float(intensity)))
    except (TypeError, ValueError):
        inten = 0.6
    scaled = 1.0 + (mult - 1.0) * (0.5 + inten)      # inten 0→半幅, 1→1.5 幅
    val = float(base_factor) * scaled
    return max(_GAP_FACTOR_MIN, min(_GAP_FACTOR_MAX, val))


def _has_laugh_cue(text: str) -> bool:
    t = str(text or "")
    return any(c in t for c in _LAUGH_CUE)


@dataclass
class PerformancePart:
    """一条语音的表演元数据。"""
    index: int
    text: str
    is_first: bool
    is_last: bool
    lead: bool           # 允许句首口语连接（仅首条 True，防连发都「其实，」开头）
    laugh_hint: bool     # 该条可带笑（文本自带笑意 && 情绪对路）


@dataclass
class PerformanceScript:
    """一条回复的完整「表演脚本」。"""
    parts: List[PerformancePart]
    should_split: bool
    gap_factor: float
    gap_jitter: Tuple[float, float]
    thinking_delay_sec: float
    emotion: str
    single_text: str     # 不分条时的整段文本

    @property
    def part_texts(self) -> List[str]:
        return [p.text for p in self.parts]


def plan_voice_performance(
    text: str,
    *,
    emotion: str = "neutral",
    intensity: float = 0.6,
    split_cfg: Optional[Dict[str, Any]] = None,
    arousal: Optional[float] = None,
    humanize_cfg: Optional[Dict[str, Any]] = None,
) -> PerformanceScript:
    """规划一条语音回复的「表演脚本」（纯函数）。

    参数
    ----
    text        : 待发送（口语化后）的合成文本
    emotion     : EmotionSpec.emotion（neutral/warm/happy/sad/…）
    intensity   : EmotionSpec.intensity（0~1，情绪对节奏的影响力度）
    split_cfg   : ``telegram.voice_reply.split_send`` / 同结构分条配置
    arousal     : 回复文本激活度（喂 estimate_thinking_delay 的节奏缩放）
    humanize_cfg: ``telegram.reply_humanize.thinking_delay`` 同结构（min/max_sec）

    返回 ``PerformanceScript``：分条文本 + 情绪缩放条间节奏 + 开场思考延迟 + 每条笑点提示。
    """
    core = str(text or "").strip()
    sc = split_cfg or {}
    emo = str(emotion or "neutral").strip().lower() or "neutral"

    min_total = int(sc.get("min_total_chars", 24) or 24)
    part_max = int(sc.get("part_max_chars", 40) or 40)
    max_parts = int(sc.get("max_parts", 3) or 3)
    min_tail = int(sc.get("min_tail_chars", 8) or 0)

    # 条间节奏：情绪缩放（本 Agent 核心增量）+ 配置 jitter
    base_gf = float(sc.get("gap_factor", 1.1) or 1.1)
    gap_factor = emotion_gap_factor(emo, base_gf, intensity)
    jit = sc.get("gap_jitter_sec") or [1.0, 2.5]
    try:
        gap_jitter: Tuple[float, float] = (float(jit[0]), float(jit[1]))
    except Exception:
        gap_jitter = (1.0, 2.5)

    # 分条决策（短回复/切不出第二条 → 单条，与原路径同口径）
    parts_txt: List[str] = [core] if core else []
    should_split = False
    try:
        from src.ai.voice_clone_client import effective_min_total as _emt
        _min_total_eff = _emt(core, min_total)      # 2026-09-12：拉丁文按 3 倍字符折算
    except Exception:
        _min_total_eff = min_total
    if core and len(core) >= _min_total_eff:
        try:
            from src.ai.voice_clone_client import pack_voice_parts
            packed = pack_voice_parts(
                core, part_max_chars=part_max, max_parts=max_parts,
                min_tail_chars=min_tail)
            if len(packed) >= 2:
                parts_txt = packed
                should_split = True
        except Exception:
            parts_txt = [core]

    n = len(parts_txt)
    laugh_ok = emo in _LAUGH_EMOTIONS
    parts = [
        PerformancePart(
            index=i, text=t, is_first=(i == 0), is_last=(i == n - 1),
            lead=(i == 0), laugh_hint=(laugh_ok and _has_laugh_cue(t)),
        )
        for i, t in enumerate(parts_txt)
    ]

    # 开场思考延迟（复用 humanize 模型；纯计算，消费方决定是否真 sleep——A 线已由
    # humanize 协作器另行 sleep，勿双延迟；此字段供 B 线/其它消费方与测试参考）。
    thinking = 0.0
    try:
        from src.inbox.humanize import estimate_thinking_delay
        hc = humanize_cfg or {}
        thinking = estimate_thinking_delay(
            core,
            min_sec=float(hc.get("min_sec", 0.0) or 0.0),
            max_sec=float(hc.get("max_sec", 12.0) or 12.0),
            arousal=arousal)
    except Exception:
        thinking = 0.0

    return PerformanceScript(
        parts=parts, should_split=should_split, gap_factor=gap_factor,
        gap_jitter=gap_jitter, thinking_delay_sec=thinking, emotion=emo,
        single_text=core)


__all__ = [
    "PerformancePart", "PerformanceScript",
    "plan_voice_performance", "emotion_gap_factor",
]
