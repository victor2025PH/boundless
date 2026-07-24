"""入站媒体 → 可喂 AI 的文本（图片 Vision / 语音 ASR / 视频抽帧）。

平台无关的**共享识别层**：原生 Telegram A 线、协议直发线（``protocol_autoreply``）、
收件箱全自动草稿链都可复用，避免"各写一遍"。

背景（本模块补的坑）：协议直发线 ``protocol_autoreply`` 此前只看入站 ``text``——
对方发来纯图片/纯语音（无 caption）时，直发线要么把它判为 ``incomplete`` 早退、
要么把 ``[图片]`` 占位喂给 AI 让其搪塞"我看不了图"。只有会话被收件箱"全自动
auto_ai"托管、走 ``autodraft_helpers`` 那条链时才有识图/转写/视频理解。本模块把
那套识别能力抽成平台无关函数，让直发线也能识别，与 Telegram / 全自动链口径一致。

铁律：所有识别失败一律软降级到占位符或原 caption，**绝不抛异常、绝不阻断回复主链**。
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

_IMAGE_KINDS = {"image", "photo", "sticker"}
_VOICE_KINDS = {"voice", "audio"}
_VIDEO_KINDS = {"video", "video_note", "animation", "gif"}

# 无描述时的占位文案（与 protocol_bridge / inbound_enrich 口径一致）
_PLACEHOLDER = {
    "image": "[图片]", "photo": "[图片]", "sticker": "[贴纸]",
    "voice": "[语音]", "audio": "[语音]",
    "video": "[视频]", "video_note": "[视频]", "animation": "[动图]", "gif": "[动图]",
    "document": "[文件]", "file": "[文件]",
}

# 只含媒体占位、无实质文本 → 视为"该识别但还没识别"。识别文本前缀（[图片内容] 等）
# 不算占位（那已经是识别结果）。
_PLACEHOLDER_TEXTS = frozenset({
    "[图片]", "[贴纸]", "[语音]", "[视频]", "[动图]", "[gif]", "[GIF]",
    "[文件]", "[媒体]", "[表情]", "[动态表情]",
})


def media_placeholder(media_type: str) -> str:
    return _PLACEHOLDER.get(str(media_type or "").lower(), "[媒体]")


def is_placeholder_only(text: str) -> bool:
    """入站文本是否只是裸媒体占位（无实质内容）——需要识别补全的信号。

    识别结果（``[图片内容] …`` / ``[视频内容] …`` 等带描述的）不算占位。
    """
    t = str(text or "").strip()
    if not t:
        return True
    if t in _PLACEHOLDER_TEXTS:
        return True
    # 形如 [xxx] 的短裸占位（≤8 字符、不含描述）
    return t.startswith("[") and t.endswith("]") and len(t) <= 8


# --- 懒建 ASR transcriber（宿主没建时的兜底） ---------------------------------
# 背景：TelegramClient.voice_transcriber 只在进程启动时按当时的
# voice_recognition.enabled 初始化；用户随后用自检卡/运营开关热开 ASR 时，
# 已运行进程里它仍是 None → 语音/视频音轨永远不转写，除非重启。
# 这里按当前配置懒建一个并缓存（配置指纹变了就重建），让"一键开启"即时生效。
_LAZY_VTR: Any = None
_LAZY_VTR_KEY: Optional[str] = None


def lazy_voice_transcriber(config: Optional[Dict[str, Any]]) -> Any:
    """配置启用 ASR 时返回（懒建+缓存的）transcriber；未启用/建失败返回 None。"""
    global _LAZY_VTR, _LAZY_VTR_KEY
    vr = (config or {}).get("voice_recognition") or {}
    if not vr.get("enabled", False):
        return None
    key = repr(sorted((str(k), repr(v)) for k, v in vr.items()))
    if _LAZY_VTR is not None and _LAZY_VTR_KEY == key:
        return _LAZY_VTR
    try:
        from src.voice_transcriber import VoiceTranscriberFactory
        _LAZY_VTR = VoiceTranscriberFactory.create_transcriber(vr)
        _LAZY_VTR_KEY = key
        logger.info("[media_enrich] ASR transcriber 懒建成功（热开关生效，无需重启）")
        return _LAZY_VTR
    except Exception:
        logger.debug("[media_enrich] ASR transcriber 懒建失败", exc_info=True)
        _LAZY_VTR = None
        _LAZY_VTR_KEY = None
        return None


def _resolve_local_path(media_ref: str) -> Optional[str]:
    """把 media_ref 解析成本进程可读的本地文件绝对路径；解析不到返回 None。

    优先 protocol 落地的 ``/static/protocol_media/...`` URL，其次通用解析
    （绝对路径 / file:// / 相对）。远程 http(s) URL 本层不下载 → None。
    """
    ref = str(media_ref or "").strip()
    if not ref:
        return None
    try:
        from src.integrations.protocol_bridge import static_media_ref_to_path
        p = static_media_ref_to_path(ref)
        if p and os.path.isfile(p):
            return p
    except Exception:
        logger.debug("[media_enrich] static_media_ref_to_path 解析失败", exc_info=True)
    try:
        from src.inbox.media_resolver import resolve_media_path
        return resolve_media_path({"media_ref": ref})
    except Exception:
        logger.debug("[media_enrich] resolve_media_path 解析失败", exc_info=True)
        return None


async def _describe_image(path: str, cfg: Dict[str, Any]) -> str:
    vision_cfg = (cfg or {}).get("vision") or {}
    if not vision_cfg.get("enabled", False):
        return ""
    try:
        from src.vision_client import VisionClient, has_any_vision_backend
    except Exception:
        return ""
    if not has_any_vision_backend(vision_cfg, vision_cfg):
        return ""
    text, _tag = await VisionClient.describe_image_with_ollama_zhipu_fallback(
        vision_cfg, vision_cfg, str(path), prompt=vision_cfg.get("prompt"))
    return (text or "").strip()[:2000]


async def _transcribe_voice(path: str, cfg: Dict[str, Any], voice_transcriber: Any) -> str:
    if voice_transcriber is None:
        return ""
    lang = str(((cfg or {}).get("voice_recognition") or {}).get("language", "auto") or "auto")
    txt = await voice_transcriber.transcribe_voice_message(str(path), lang)
    return (txt or "").strip()


async def _understand_video(path: str, cfg: Dict[str, Any], voice_transcriber: Any) -> str:
    from src.ai.inbound_video import understand_video_file
    out = await understand_video_file(
        str(path),
        vision_config=(cfg or {}).get("vision") or {},
        voice_transcriber=voice_transcriber,
        speech_emotion_config=(cfg or {}).get("speech_emotion") or {},
        voice_recognition_config=(cfg or {}).get("voice_recognition") or {},
    )
    return (out or "").strip()


async def enrich_inbound_media_text(
    *,
    media_type: str,
    media_ref: str,
    caption: str = "",
    config: Optional[Dict[str, Any]] = None,
    voice_transcriber: Any = None,
) -> Tuple[str, str]:
    """识别入站媒体，返回 ``(供 AI 的文本, 识别描述)``。

    - 图片/贴纸 → VisionClient（Ollama→智谱链），需 ``vision.enabled`` + 后端可用。
    - 语音/音频 → ``voice_transcriber.transcribe_voice_message``（转写即"对方说的话"）。
    - 视频/GIF → ``understand_video_file``（抽关键帧 + 音轨 ASR/SER）。
    - 识别不出 / 无后端 / 远程未下载 → 文本回落 ``caption`` 或占位符，描述空串。

    全程软失败，绝不抛异常。
    """
    cfg = config or {}
    mt = str(media_type or "").strip().lower()
    cap = str(caption or "").strip()
    if not mt and not media_ref:
        return cap, ""

    local = _resolve_local_path(media_ref)
    if not local:
        # 远程 URL 或文件不存在 → 无法识别，保留 caption 或占位
        return (cap or media_placeholder(mt)), ""

    # 宿主（TelegramClient）启动时未建 transcriber、但配置已（热）启用 → 懒建兜底
    if voice_transcriber is None and mt in (_VOICE_KINDS | _VIDEO_KINDS):
        voice_transcriber = lazy_voice_transcriber(cfg)

    desc = ""
    try:
        if mt in _IMAGE_KINDS:
            desc = await _describe_image(local, cfg)
        elif mt in _VOICE_KINDS:
            desc = await _transcribe_voice(local, cfg, voice_transcriber)
        elif mt in _VIDEO_KINDS:
            desc = await _understand_video(local, cfg, voice_transcriber)
    except Exception:
        logger.debug("[media_enrich] 识别失败 kind=%s", mt, exc_info=True)
        desc = ""

    desc = (desc or "").strip()
    if not desc:
        return (cap or media_placeholder(mt)), ""

    if mt in _VIDEO_KINDS:
        text = f"{cap}\n[视频内容] {desc}" if cap else f"[视频内容] {desc}"
    elif mt in _IMAGE_KINDS:
        text = f"{cap}\n[图片内容] {desc}" if cap else f"[图片内容] {desc}"
    elif mt in _VOICE_KINDS:
        # 语音转写即"对方说的话"，直接作为待回复正文（有 caption 少见，拼上）
        text = f"{cap}\n{desc}" if cap else desc
    else:
        text = cap or desc
    return text, desc


__all__ = [
    "enrich_inbound_media_text",
    "is_placeholder_only",
    "lazy_voice_transcriber",
    "media_placeholder",
]
