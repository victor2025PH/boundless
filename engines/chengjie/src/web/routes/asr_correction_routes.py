"""语音转写改正 + 改正台账读口（ASR P2，2026-09-12）。

``register_asr_correction_routes(app, *, api_auth, config_manager)``，由 unified_inbox_routes
在账号路由之后挂载：

- ``POST /api/unified-inbox/asr-correction``
  body ``{platform, account_id, chat_key, message_id, corrected_text}``
  → ① 写改正台账（``asr_corrections.jsonl``，评测金标 + 热词候选的数据源）
    ② 回写消息正文（``update_message_text(only_if_empty=False)``：坐席台/时间线/后续起草
       上下文都看到人改过的话）
    ③ 逐条元数据 ``asr_meta.mark_corrected``（清可疑标记、记改正人）
    ④ 清 ``asr_suspect`` 登记（该条不再走「先确认」话术）
    ⑤ 转写缓存按音频内容覆写成改正文本（同一音频再被识别直接得到人改的话）
  只对**入站语音/音频**行生效；越权 / 不存在 / 非语音分别 404 / 400。
- ``GET /api/admin/asr-corrections?limit=N`` → 最近 N 条改正 + 差异片段热词候选。

所有响应文案走 ``tr(request, "err.*")``（CJK 棘轮账本 0）。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import HTTPException, Request

from src.inbox.normalizer import conv_id
from src.web.routes.unified_inbox_auth import _session_agent
from src.web.web_i18n import tr

logger = logging.getLogger(__name__)

_VOICE_KINDS = ("voice", "audio")


def _inbox_store(request: Request) -> Any:
    return getattr(request.app.state, "inbox_store", None)


def _override_transcript_cache(request: Request, cfg_root: Dict[str, Any],
                               audio_path: str, corrected: str) -> bool:
    """把该音频在转写缓存里的结果覆写成改正文本（配置语言 + auto 两个键）。best-effort。"""
    if not audio_path or not corrected:
        return False
    try:
        from src.inbox.media_enrich import lazy_voice_transcriber
        from src.voice_transcriber import get_transcript_cache
        tc = getattr(request.app.state, "telegram_client", None)
        vtr = getattr(tc, "voice_transcriber", None) if tc is not None else None
        if vtr is None:
            vtr = lazy_voice_transcriber(cfg_root)
        if vtr is None or not hasattr(vtr, "_cache_lookup_key"):
            return False
        langs = {"auto", str(((cfg_root or {}).get("voice_recognition") or {}).get("language") or "auto")}
        n = 0
        for lang in langs:
            k = vtr._cache_lookup_key(audio_path, lang)
            if k and get_transcript_cache().put(k, corrected, {"provider": "human_correction"}, ""):
                n += 1
        return n > 0
    except Exception:
        logger.debug("[asr-correction] 转写缓存覆写失败（忽略）", exc_info=True)
        return False


def register_asr_correction_routes(app, *, api_auth, config_manager=None) -> None:

    def _cfg_root() -> Dict[str, Any]:
        try:
            c = getattr(config_manager, "config", None)
            return c if isinstance(c, dict) else {}
        except Exception:
            return {}

    @app.post("/api/unified-inbox/asr-correction")
    async def api_asr_correction(request: Request):
        api_auth(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        body = body if isinstance(body, dict) else {}
        platform = str(body.get("platform") or "").strip().lower()
        account_id = str(body.get("account_id") or "default").strip() or "default"
        chat_key = str(body.get("chat_key") or "").strip()
        message_id = str(body.get("message_id") or "").strip()
        corrected = str(body.get("corrected_text") or "").strip()
        if not platform or not chat_key:
            raise HTTPException(400, tr(request, "err.ws.platform_chatkey_required"))
        if not message_id:
            raise HTTPException(400, tr(request, "err.ws.field_required", field="message_id"))
        if not corrected:
            raise HTTPException(400, tr(request, "err.ws.field_required", field="corrected_text"))
        if len(corrected) > 2000:
            raise HTTPException(400, tr(request, "err.asr.text_too_long"))
        store = _inbox_store(request)
        if store is None:
            raise HTTPException(503, tr(request, "err.svc.inbox_not_ready"))
        cid = conv_id(platform, account_id, chat_key)
        row: Optional[Dict[str, Any]] = None
        try:
            row = store.get_message(message_id)
        except Exception:
            row = None
        if not row or str(row.get("conversation_id") or "") != cid:
            raise HTTPException(404, tr(request, "err.asr.message_not_found"))
        if str(row.get("direction") or "in") != "in" or \
                str(row.get("media_type") or "").lower() not in _VOICE_KINDS:
            raise HTTPException(400, tr(request, "err.asr.not_voice"))

        machine_text = str(row.get("text") or "")
        media_ref = str(row.get("media_ref") or "")
        audio_path = ""
        try:
            from src.integrations.protocol_bridge import static_media_ref_to_path
            audio_path = str(static_media_ref_to_path(media_ref) or "") if media_ref else ""
        except Exception:
            audio_path = ""
        agent = _session_agent(request)
        cfg_root = _cfg_root()

        # ① 台账
        ledger_ok = False
        audio_sha1 = ""
        try:
            from src.inbox.asr_corrections import (
                append_correction, build_correction, corrections_path,
            )
            rec = build_correction(
                conversation_id=cid, message_id=message_id, media_ref=media_ref,
                audio_path=audio_path, machine_text=machine_text, corrected_text=corrected,
                lang=str(row.get("source_lang") or ""), agent=str(agent.get("agent_id") or ""),
                platform=platform)
            audio_sha1 = str(rec.get("audio_sha1") or "")
            p = corrections_path(config_manager)
            ledger_ok = bool(p) and append_correction(p, rec)
        except Exception:
            logger.debug("[asr-correction] 台账写入失败（忽略）", exc_info=True)
        # ② 消息正文
        text_updated = False
        try:
            text_updated = bool(store.update_message_text(
                cid, message_id=message_id, text=corrected, only_if_empty=False))
        except Exception:
            logger.debug("[asr-correction] 消息回写失败（忽略）", exc_info=True)
        # ③ 逐条元数据 ④ 可疑登记 ⑤ 转写缓存
        meta_ok = False
        try:
            from src.inbox.asr_meta import mark_corrected
            meta_ok = mark_corrected(store, cid, message_id, corrected_text=corrected,
                                     agent=str(agent.get("agent_id") or ""),
                                     machine_text=machine_text,
                                     platform_msg_id=str(row.get("platform_msg_id") or "")) is not None
        except Exception:
            meta_ok = False
        try:
            from src.inbox.asr_suspect import note as _sus_note
            _sus_note(cid, "", corrected)
        except Exception:
            pass
        cache_updated = _override_transcript_cache(request, cfg_root, audio_path, corrected)
        try:
            from src.ai.asr_stats import get_asr_stats
            get_asr_stats().record_event("correction")
        except Exception:
            pass
        logger.info("[asr-correction] conv=%s mid=%s by=%s ledger=%s text=%s meta=%s cache=%s | %r → %r",
                    cid, message_id, agent.get("agent_id"), ledger_ok, text_updated, meta_ok,
                    cache_updated, machine_text[:40], corrected[:40])
        return {
            "ok": True, "conversation_id": cid, "message_id": message_id,
            "machine_text": machine_text, "corrected_text": corrected,
            "audio_sha1": audio_sha1, "ledger": ledger_ok, "text_updated": text_updated,
            "meta_updated": meta_ok, "cache_updated": cache_updated,
        }

    @app.get("/api/admin/asr-corrections")
    async def api_asr_corrections_list(request: Request, limit: int = 100):
        api_auth(request)
        from src.inbox.asr_corrections import corrections_path, hotword_candidates, iter_corrections
        p = corrections_path(config_manager)
        allrecs = iter_corrections(p) if p else []
        lim = max(1, min(500, int(limit or 100)))
        items = []
        for r in allrecs[-lim:]:
            d = dict(r)
            d.pop("audio_path", None)      # 服务器本地路径不下发
            items.append(d)
        return {
            "ok": True, "total": len(allrecs), "items": list(reversed(items)),
            "hotword_candidates": hotword_candidates(allrecs)[:50],
            "ledger_path": str(p) if p else "",
        }
