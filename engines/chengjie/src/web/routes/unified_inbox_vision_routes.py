"""统一收件箱 · 入站图片问答（P2 2026-08-19，「乱码识图」事故链第三期）。

「问这张图」：坐席对入站图片提一个具体问题（前端按识别类型给预设，可自由改写），
VisionClient 带问题重问该图——客户「你看这里都有啥」这类时刻，坐席不再只能肉眼看图、
AI 也有了可引用的答案来源。

安全面（与 translate-message-media 同口径）：媒体优先按 conversation_id+message_id
从 store 反查受信 ref，客户端直传 ref 仅作回落且过 media.base_dirs 围栏；
识图失败如实报错（no_cloud_fallback 纪律），绝不静默编造。
"""

from __future__ import annotations

import logging
import os

from fastapi import Depends, HTTPException, Request

from src.web.web_i18n import tr

logger = logging.getLogger(__name__)

#: 可问答的媒体类型（VisionClient 只吃静态图；视频/语音各有专用链路）
_ASKABLE_KINDS = {"image", "photo", "img", "sticker"}


def register_vision_routes(app, *, api_auth) -> None:
    """挂载图片问答端点。"""

    @app.post("/api/unified-inbox/ask-image")
    async def api_unified_inbox_ask_image(request: Request, _=Depends(api_auth)):
        # 与翻译集群共享能力闸/store 反查/路径围栏（同一重启窗口装载，无版本错配面）
        from src.web.routes.unified_inbox_translate_routes import (
            _deny_capability,
            _lookup_stored_media,
            _media_base_dirs,
            _within_base_dirs,
        )

        # 坐席主动触发的 AI 消耗，与「识图翻译」同一能力闸（词表现无 vision 专属键，
        # 新造键要动整个权限注册面——刻意复用，语义为「可用 AI 理解/翻译内容」）。
        _deny_capability(request, "ai.translate")

        body = await request.json()
        conversation_id = str(body.get("conversation_id") or "")
        message_id = str(body.get("message_id") or "")
        question = " ".join(str(body.get("question") or "").split())[:200]
        if not question:
            raise HTTPException(400, tr(request, "err.imgask.question_required"))

        media_type, media_ref = _lookup_stored_media(request, conversation_id, message_id)
        if not media_ref:
            media_ref = str(body.get("media_ref") or "")
            media_type = media_type or str(body.get("media_type") or "")
        kind = str(media_type or "").strip().lower()
        if kind and kind not in _ASKABLE_KINDS:
            raise HTTPException(400, tr(request, "err.imgask.not_image"))

        base_dirs = _media_base_dirs(request)
        local = ""
        try:
            from src.integrations.protocol_bridge import (
                protocol_media_roots, static_media_ref_to_path,
            )
            p = static_media_ref_to_path(media_ref)
            if p:
                local = p
                # 双根：解析器可能在旧引擎树根命中（边车历史写点），白名单必须同口径
                base_dirs = base_dirs + [str(r) for r in protocol_media_roots()]
        except Exception:
            logger.debug("[ask-image] protocol 媒体路径映射失败", exc_info=True)
        if not local:
            try:
                from src.inbox.media_resolver import resolve_media_path
                local = resolve_media_path({"media_ref": media_ref}) or ""
            except Exception:
                local = ""
        if not local or not os.path.isfile(local) or not _within_base_dirs(local, base_dirs):
            raise HTTPException(404, tr(request, "err.imgask.media_unavailable"))

        cm = getattr(request.app.state, "config_manager", None)
        vision_cfg = dict(((getattr(cm, "config", None) or {}).get("vision")) or {})
        if not vision_cfg.get("enabled", False):
            raise HTTPException(503, tr(request, "err.imgask.vision_off"))

        from src.inbox.media_enrich import build_ask_image_prompt
        from src.vision_client import VisionClient

        try:
            text, tag = await VisionClient.describe_image_with_ollama_zhipu_fallback(
                vision_cfg, vision_cfg, local,
                prompt=build_ask_image_prompt(question))
        except Exception:
            logger.warning("[ask-image] VLM 调用异常", exc_info=True)
            text, tag = "", ""
        answer = (text or "").strip()[:1000]
        if not answer:
            raise HTTPException(502, tr(request, "err.imgask.failed"))
        logger.info("[ask-image] ok conv=%s msg=%s q=%d字 a=%d字 via=%s",
                    conversation_id, message_id, len(question), len(answer), tag)
        return {"ok": True, "answer": answer, "provider": tag}
