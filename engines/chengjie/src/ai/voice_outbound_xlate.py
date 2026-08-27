"""P0-V2（2026-08-19）：坐席语音「译声」——打中文、发目标语语音。

单一职责：把「发声前按目标语翻译」收成一个小函数，tts-test（试听）与
send-voice（发送）两端共用——试听=发送契约要求同入参得到同一份 spoken 文本。

铁律（与出站文本翻译同哲学）：
- **fail-open**：翻译异常/失败/译文为空/identity（源语==目标语）一律回落念
  原文，绝不因翻译层故障阻塞语音发送；是否真的译过由 ``meta["translated"]``
  如实上报（前端徽标/收件箱镜像按真值渲染，绝不谎报「已译」）。
- 译后文本仍走「原文直念」链（pre_colloquialized=True）：翻译是坐席显式
  意图，译文本身**不再**进口语化改写（改写译文＝又一个「念出别的话」入口，
  2026-08-10 事故同款机制）。
- 本函数不解析 'auto'：调用方（路由层，有 request/会话上下文）先把 'auto'
  解析成具体语种码再传入——试听与发送各自现场解析，语言漂移由试听复用
  sidecar 的 target_lang 维度兜底（不符→回落现场翻译+合成）。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Tuple

logger = logging.getLogger(__name__)


async def resolve_spoken_text(
    text: str, target_lang: str, translate_service: Any,
) -> Tuple[str, Dict[str, Any]]:
    """(原文, 目标语, TranslationService) → (spoken 文本, meta)。

    meta = {translated, target_lang, source_lang, provider, reason}；
    未译时 spoken==原文且 reason 说明原因（no_target/identity/translate_failed…）。
    """
    meta: Dict[str, Any] = {
        "translated": False, "target_lang": "", "source_lang": "",
        "provider": "", "reason": "",
    }
    src_text = str(text or "").strip()
    tl = str(target_lang or "").strip().lower()
    if not src_text:
        meta["reason"] = "no_text"
        return src_text, meta
    if not tl or tl == "unknown":
        meta["reason"] = "no_target"
        return src_text, meta
    if translate_service is None:
        meta["reason"] = "no_service"
        return src_text, meta
    try:
        res = await translate_service.translate(
            src_text, target_lang=tl, style="chat")
    except Exception:
        logger.debug("[voice-xlate] 翻译异常（回落念原文）", exc_info=True)
        meta["reason"] = "translate_error"
        return src_text, meta
    out = (getattr(res, "translated_text", "") or "").strip()
    if not getattr(res, "ok", False) or not out:
        meta["reason"] = "translate_failed"
        return src_text, meta
    if out == src_text or str(getattr(res, "provider", "") or "") == "identity":
        meta["reason"] = "identity"
        return src_text, meta
    meta.update({
        "translated": True,
        "target_lang": tl,
        "source_lang": str(getattr(res, "source_lang", "") or ""),
        "provider": str(getattr(res, "provider", "") or ""),
    })
    return out, meta


__all__ = ["resolve_spoken_text"]
