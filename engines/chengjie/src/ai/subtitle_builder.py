"""P4（2026-08-18）：分段时间戳 → 双语 SRT 字幕构建。

上游＝ASR 分段（``voice_translate`` 响应 ``segments``：[{start,end,text}]，秒），
本模块逐段翻译（复用 TranslationService：术语/缓存/引擎路由）并格式化为标准 SRT。

诚实边界：
- **只吃真时间戳**：segments 缺失/为空 → ok=False（调用方按「无字幕可产」处理），
  绝不按字数均分时间轴伪造；
- 单段翻译失败 → 该 cue 只保留原文行（best-effort，整体仍 ok）；
- 双语形态＝原文行在上、译文行在下（SRT 多行 cue 合法，与 translate_subtitle
  的 bilingual 语义一致）。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_MAX_SEGMENTS = 2000


def format_srt_time(sec: float) -> str:
    """秒 → SRT 时间戳 ``HH:MM:SS,mmm``（负值按 0；毫秒四舍五入进位安全）。"""
    ms_total = max(0, int(round(float(sec or 0.0) * 1000)))
    h, rem = divmod(ms_total, 3600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def segments_to_srt(segments: List[Dict[str, Any]],
                    translations: Optional[List[str]] = None,
                    *, bilingual: bool = True) -> str:
    """分段（+可选逐段译文）→ SRT 文本。translations[i] 空串＝该段无译文只出原文。"""
    lines: List[str] = []
    idx = 0
    for i, seg in enumerate(segments):
        text = str((seg or {}).get("text") or "").strip()
        if not text:
            continue
        idx += 1
        start = format_srt_time(float(seg.get("start") or 0.0))
        end = format_srt_time(float(seg.get("end") or 0.0))
        dst = (translations[i] if translations and i < len(translations) else "") or ""
        lines.append(str(idx))
        lines.append(f"{start} --> {end}")
        if dst and bilingual:
            lines.append(text)
            lines.append(dst)
        elif dst:
            lines.append(dst)
        else:
            lines.append(text)
        lines.append("")
    return "\n".join(lines)


async def build_bilingual_srt(
    segments: List[Dict[str, Any]],
    xlate: Any,
    *,
    target_lang: str = "zh",
    source_lang: str = "",
    style: str = "chat",
    engine: str = "",
    bilingual: bool = True,
    max_concurrency: int = 4,
) -> Dict[str, Any]:
    """逐段翻译 + SRT 组装。返回 {ok, srt_text?, stats, reason?}。"""
    segs = [s for s in (segments or []) if str((s or {}).get("text") or "").strip()]
    if not segs:
        return {"ok": False, "reason": "no_segments",
                "message": "无分段时间戳（ASR 服务未启用 verbose_json 或结果来自缓存）"}
    if len(segs) > _MAX_SEGMENTS:
        return {"ok": False, "reason": "too_many_segments",
                "message": f"分段过多（上限 {_MAX_SEGMENTS}）"}

    sem = asyncio.Semaphore(max(1, int(max_concurrency)))
    dsts: List[str] = [""] * len(segs)
    stats = {"total": len(segs), "translated": 0, "failed": 0, "cached": 0}

    async def _do(i: int) -> None:
        async with sem:
            try:
                res = await xlate.translate(
                    segs[i]["text"], target_lang=target_lang,
                    source_lang=source_lang, style=style, engine=engine)
            except Exception:
                stats["failed"] += 1
                logger.debug("[srt-build] 段翻译异常（保留原文）", exc_info=True)
                return
        dst = (res.translated_text or "").strip() if res.ok else ""
        if res.ok and dst and dst != segs[i]["text"]:
            dsts[i] = dst
            stats["translated"] += 1
            if getattr(res, "cached", False):
                stats["cached"] += 1
        else:
            stats["failed"] += 1 if not (res.ok and dst) else 0

    await asyncio.gather(*(_do(i) for i in range(len(segs))))
    return {"ok": True,
            "srt_text": segments_to_srt(segs, dsts, bilingual=bilingual),
            "stats": stats}


__all__ = ["format_srt_time", "segments_to_srt", "build_bilingual_srt"]
