"""出站语音质量闸门：识别「合成截断/坏音」，宁可回落也不发怪声。

背景（2026-07-15 事故）：克隆 TTS（IndexTTS/CosyVoice 系）遇特殊标点/符号会
早停——20 字文本只合成出 ~1 秒音频，用户听到的是半截杂音（"乱码语音"）。
既有闸门只查「过长」（total > max_seconds×1.5），没有「过短=截断」检查，
坏音直接过闸发出。

判定思路（纯函数，零依赖）：真人/TTS 语速有物理下限——每个「可发声单位」
（CJK 字 / 拉丁词 / 数字串）至少要 ~0.1s 才能念出来。实测中文对话 3.5~5.5 字/s
（0.18~0.29 s/字）、日语最快 ~8 mora/s（0.125 s/字）；阈值取 0.10 s/单位，
比一切真实语速都快 → 只有真截断才会低于它，天然零误杀。

时长未知（duration_sec<=0，如探测失败）→ 放行（无法判定就不拦，与旧行为一致）。
"""
from __future__ import annotations

import re
from typing import Any, Dict, Optional, Tuple

# CJK 统一表意文字 + 日文假名 + 韩文音节（每字≈一个发声单位）
_CJK_RE = re.compile(
    r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af]")
# 拉丁词 / 数字串（每词≈一个发声单位；比按字母数更贴近英语语速口径）
_WORD_RE = re.compile(r"[A-Za-z]+|\d+")

# 默认阈值（config telegram.voice_reply.quality_gate.* 可覆盖）
DEFAULT_MIN_SEC_PER_UNIT = 0.10   # 低于此语速物理上不可能 → 判截断
DEFAULT_MIN_UNITS = 6             # 文本太短不判（"嗯嗯。"合出 0.8s 属正常）


def speakable_units(text: str) -> int:
    """数「可发声单位」：CJK/假名/谚文按字、拉丁与数字按词。

    emoji / 标点 / 空白不计——合成前清洗（``clean_text_for_tts``）本就会剔除
    它们，按原文数会高估时长预期造成误杀。纯函数。
    """
    t = str(text or "")
    if not t:
        return 0
    return len(_CJK_RE.findall(t)) + len(_WORD_RE.findall(t))


def looks_truncated(
    text: str,
    duration_sec: float,
    *,
    min_sec_per_unit: float = DEFAULT_MIN_SEC_PER_UNIT,
    min_units: int = DEFAULT_MIN_UNITS,
) -> Tuple[bool, str]:
    """判定合成音频是否截断/坏音。返回 ``(is_bad, reason)``。

    - ``duration_sec <= 0``（未测得）→ 不判（``(False, "no_duration")``）。
    - 文本 < ``min_units`` 个发声单位 → 不判（短语短音属正常）。
    - ``duration_sec < units × min_sec_per_unit`` → 截断。
    """
    try:
        dur = float(duration_sec)
    except (TypeError, ValueError):
        return False, "no_duration"
    if dur <= 0:
        return False, "no_duration"
    units = speakable_units(text)
    if units < max(1, int(min_units)):
        return False, "text_too_short_to_judge"
    floor = units * float(min_sec_per_unit)
    if dur < floor:
        return True, (
            f"duration {dur:.1f}s < floor {floor:.1f}s "
            f"({units} units × {min_sec_per_unit:.2f}s)")
    return False, "ok"


def resolve_quality_gate(vr_cfg: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """解析 ``quality_gate`` 配置块（含安全默认；解析失败回默认值）。

    默认**开**：闸门只拦物理上不可能的语速（真截断），正常语音零误杀；
    这是 bug 防线而非新功能，与「宁缺毋滥」运营方针一致。可 ``enabled: false`` 关。
    """
    qg = (vr_cfg or {}).get("quality_gate")
    qg = qg if isinstance(qg, dict) else {}
    try:
        per_unit = float(qg.get("min_sec_per_unit", DEFAULT_MIN_SEC_PER_UNIT))
    except (TypeError, ValueError):
        per_unit = DEFAULT_MIN_SEC_PER_UNIT
    try:
        min_units = int(qg.get("min_units", DEFAULT_MIN_UNITS))
    except (TypeError, ValueError):
        min_units = DEFAULT_MIN_UNITS
    return {
        "enabled": bool(qg.get("enabled", True)),
        "min_sec_per_unit": per_unit,
        "min_units": min_units,
    }


__all__ = [
    "speakable_units", "looks_truncated", "resolve_quality_gate",
    "DEFAULT_MIN_SEC_PER_UNIT", "DEFAULT_MIN_UNITS",
]
