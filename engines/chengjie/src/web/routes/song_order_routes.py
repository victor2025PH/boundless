"""专属歌订单路由（实施58 P2「点唱台」）。

Endpoints（全部 Depends(api_auth)；文案 tr() 键化，0 CJK）：
  GET  /api/singing/orders                   订单列表（?status= 过滤）+ 分状态计数
  GET  /api/singing/orders/{oid}/audio       试听成品
  POST /api/singing/orders/{oid}/approve     人审放行 → **就地投递**（复用编排器
                                             send_media 语音口 + 收件箱镜像 + 唱歌
                                             账本）→ delivered；发送失败留在 review
  POST /api/singing/orders/{oid}/reject      打回（记原因）

设计不变量：
- 投递走 autosend_song 同一条链（save_outbound_media → orch.send_media
  media_type=voice → 镜像 [唱歌]《专属》），坐席工作台可回听、媒体台账不缺行；
- 审批原子性在 store（WHERE status='review'）——双窗口只有一方成功，输方 409；
- 送达即记唱歌账本（与曲库共用日帽/冷却语义：专属歌也占当天唱歌次数）。
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Dict

from fastapi import Depends, HTTPException, Request
from fastapi.responses import FileResponse

from src.web.web_i18n import tr

logger = logging.getLogger(__name__)

_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")


def register_song_order_routes(app, api_auth, config_manager=None):
    from src.companion.song_orders import get_order_store

    def _cfg() -> Dict[str, Any]:
        try:
            return dict(getattr(config_manager, "config", None) or {})
        except Exception:
            return {}

    def _fail_human(request: Request, code: str) -> str:
        """失败码 → 运营人话（原始码/技术细节留 take_json，绝不给运营看 JSON）。"""
        c = str(code or "")
        if not c:
            return ""
        if c.startswith("lyrics_rejected"):
            return tr(request, "sg_fail_lyrics",
                      "lyrics failed quality gates (auto-retried 3 drafts)")
        if c.startswith("render_failed"):
            return tr(request, "sg_fail_render",
                      "singing takes failed QA after several tries")
        if c.startswith("voice_missing"):
            return tr(request, "sg_fail_voice",
                      "persona has no singing voice assigned")
        if c.startswith("stale_expired"):
            return tr(request, "sg_fail_stale", "expired before production")
        return c[:80]

    def _row_view(request: Request, r: Dict[str, Any]) -> Dict[str, Any]:
        take = {}
        try:
            take = json.loads(r.get("take_json") or "{}") or {}
        except Exception:
            pass
        return {
            "id": r.get("id"), "status": r.get("status"),
            "platform": r.get("platform"), "account_id": r.get("account_id"),
            "chat_key": r.get("chat_key"), "conv_id": r.get("conv_id"),
            "persona_id": r.get("persona_id"), "voice_key": r.get("voice_key"),
            "peer_name": r.get("peer_name"),
            "request_text": r.get("request_text"),
            "lyrics": r.get("lyrics"),
            "fail_reason": r.get("fail_reason"),
            "fail_human": _fail_human(request, r.get("fail_reason")),
            "created_ts": r.get("created_ts"), "updated_ts": r.get("updated_ts"),
            "has_audio": bool(r.get("audio_path")),
            "take": {k: take.get(k) for k in
                     ("seed", "sim", "hits", "dur", "attempt", "voice_match")},
        }

    @app.get("/api/singing/orders")
    async def api_song_orders(request: Request, status: str = "",
                              limit: int = 50, _=Depends(api_auth)):
        store = get_order_store()
        return {"ok": True,
                "orders": [_row_view(request, r) for r in store.list_orders(
                    status=status.strip(), limit=max(1, min(200, limit)))],
                "counts": store.counts()}

    @app.get("/api/singing/orders/{oid}/audio")
    async def api_song_order_audio(oid: int, request: Request,
                                   _=Depends(api_auth)):
        row = get_order_store().get(int(oid))
        if row is None:
            raise HTTPException(404, tr(request, "err.singing.no_order",
                                        "order not found"))
        p = Path(str(row.get("audio_path") or ""))
        if not row.get("audio_path") or not p.is_file():
            raise HTTPException(404, tr(request, "err.singing.audio_missing",
                                        "audio missing"))
        media = "audio/ogg" if p.suffix == ".ogg" else "audio/wav"
        return FileResponse(str(p), media_type=media, filename=p.name)

    @app.post("/api/singing/orders/{oid}/approve")
    async def api_song_order_approve(oid: int, request: Request,
                                     _=Depends(api_auth)):
        store = get_order_store()
        row = store.get(int(oid))
        if row is None:
            raise HTTPException(404, tr(request, "err.singing.no_order",
                                        "order not found"))
        if str(row.get("status")) != "review":
            raise HTTPException(409, tr(request, "err.singing.not_review",
                                        "order already resolved"))
        p = Path(str(row.get("audio_path") or ""))
        if not row.get("audio_path") or not p.is_file():
            raise HTTPException(404, tr(request, "err.singing.audio_missing",
                                        "audio missing"))
        cfg = _cfg()
        platform = str(row.get("platform") or "")
        account_id = str(row.get("account_id") or "")
        chat_key = str(row.get("chat_key") or "")
        try:
            from src.integrations.account_orchestrator import get_orchestrator
            orch = get_orchestrator(cfg)
        except Exception:
            raise HTTPException(503, tr(request, "err.singing.no_orch",
                                        "orchestrator unavailable"))
        if not orch.owns_media(platform, account_id):
            raise HTTPException(409, tr(request, "err.singing.not_managed",
                                        "account not managed for media"))
        try:
            data = p.read_bytes()
            from src.integrations.protocol_bridge import save_outbound_media
            local, url, _m = save_outbound_media(
                platform, account_id, f"song_custom_{oid}{p.suffix}", data)
        except Exception:
            logger.exception("[song_orders] outbound stage failed")
            raise HTTPException(500, tr(request, "err.singing.stage_failed",
                                        "stage failed"))
        first_line = next((ln.strip() for ln in
                           str(row.get("lyrics") or "").splitlines()
                           if ln.strip()), "")
        peer = str(row.get("peer_name") or "").strip()
        title = f"专属 · {peer}" if peer else "专属歌"
        inbox_text = f"[唱歌]《{title}》" + (f" ♪ {first_line[:40]}"
                                             if first_line else "")
        # 止损话术（实施58 P3-1）：唱歌嗓≠说话嗓的柔性铺垫，音色修复后可关
        from src.companion.song_stock import framing_caption, resolve_singing_cfg
        caption = framing_caption(resolve_singing_cfg(cfg),
                                  conv_id=str(row.get("conv_id") or ""))
        try:
            res = await orch.send_media(
                platform, account_id, chat_key,
                media_path=local, media_url=url,
                media_type="voice", caption=caption, inbox_text=inbox_text)
            sent = bool(isinstance(res, dict) and res.get("delivered"))
        except Exception:
            logger.exception("[song_orders] send failed")
            sent = False
        if not sent:
            raise HTTPException(502, tr(request, "err.singing.send_failed",
                                        "send failed, order kept in review"))
        if not store.resolve_review(int(oid), to_status="delivered"):
            # 送达已发生但状态被并发处置——如实记日志，不再回滚发送
            logger.warning("[song_orders] delivered but resolve raced oid=%s",
                           oid)
        try:
            from src.companion.song_stock import get_song_ledger, get_song_stats
            get_song_ledger().record(str(row.get("conv_id") or ""),
                                     f"custom_{oid}")
            get_song_stats().bump("custom_delivered")
        except Exception:
            pass
        try:
            actor = str(request.session.get("username") or "?")
        except Exception:
            actor = "?"
        logger.info("[song_orders] delivered oid=%s conv=%s by %s",
                    oid, row.get("conv_id"), actor)
        return {"ok": True, "id": int(oid), "status": "delivered"}

    @app.post("/api/singing/orders/{oid}/reject")
    async def api_song_order_reject(oid: int, request: Request,
                                    _=Depends(api_auth)):
        try:
            body = await request.json()
        except Exception:
            body = {}
        reason = str((body or {}).get("reason") or "")[:200]
        store = get_order_store()
        if store.get(int(oid)) is None:
            raise HTTPException(404, tr(request, "err.singing.no_order",
                                        "order not found"))
        if not store.resolve_review(int(oid), to_status="rejected",
                                    fail_reason=reason or "rejected"):
            raise HTTPException(409, tr(request, "err.singing.not_review",
                                        "order already resolved"))
        try:
            from src.companion.song_stock import get_song_stats
            get_song_stats().bump("custom_rejected")
        except Exception:
            pass
        return {"ok": True, "id": int(oid), "status": "rejected"}

    @app.post("/api/singing/orders/{oid}/retry")
    async def api_song_order_retry(oid: int, request: Request,
                                   _=Depends(api_auth)):
        """failed → pending（worker 下轮自动重拾）。重试=新一份 GPU 预算，
        计入当日订单帽语义（created_ts 重置）。"""
        store = get_order_store()
        if store.get(int(oid)) is None:
            raise HTTPException(404, tr(request, "err.singing.no_order",
                                        "order not found"))
        if not store.retry(int(oid)):
            raise HTTPException(409, tr(request, "err.singing.not_failed",
                                        "only failed orders can retry"))
        try:
            from src.companion.song_stock import get_song_stats
            get_song_stats().bump("custom_retry")
        except Exception:
            pass
        return {"ok": True, "id": int(oid), "status": "pending"}

    @app.post("/api/singing/orders/{oid}/delete")
    async def api_song_order_delete(oid: int, request: Request,
                                    _=Depends(api_auth)):
        """删终态单 + 清成品文件（活单拒删——别把制作中的单从 worker 脚下抽走）。"""
        store = get_order_store()
        if store.get(int(oid)) is None:
            raise HTTPException(404, tr(request, "err.singing.no_order",
                                        "order not found"))
        audio = store.delete_order(int(oid))
        if audio is None:
            raise HTTPException(409, tr(request, "err.singing.not_final",
                                        "active orders cannot be deleted"))
        if audio:
            try:
                Path(audio).unlink(missing_ok=True)
            except Exception:
                pass
        return {"ok": True, "id": int(oid), "status": "deleted"}

    @app.post("/api/singing/supply-request")
    async def api_singing_supply_request(request: Request,
                                         _=Depends(api_auth)):
        """补货申请：排队给夜批（每天 05:10 --force 重渲该曲目）。
        web 进程零 GPU 不变量——这里只写申请行，绝不现场渲染。"""
        try:
            body = await request.json()
        except Exception:
            body = {}
        tid = str((body or {}).get("template_id") or "").strip()
        if not _ID_RE.match(tid):
            raise HTTPException(400, tr(request, "err.singing.bad_id",
                                        "invalid id"))
        from src.companion.song_stock import (
            load_song_manifest, resolve_singing_cfg, templates_dir,
        )
        tdir = templates_dir(resolve_singing_cfg(_cfg()))
        if not any(t.id == tid for t in load_song_manifest(tdir)):
            raise HTTPException(404, tr(request, "err.singing.no_template",
                                        "template not found"))
        import time as _t
        f = tdir / "_supply_requests.json"
        rows = []
        try:
            if f.is_file():
                rows = json.loads(f.read_text(encoding="utf-8")) or []
        except Exception:
            rows = []
        if not any(r.get("template_id") == tid for r in rows
                   if isinstance(r, dict)):
            try:
                actor = str(request.session.get("username") or "?")
            except Exception:
                actor = "?"
            rows.append({"template_id": tid, "requested_at": _t.time(),
                         "by": actor})
            f.write_text(json.dumps(rows, ensure_ascii=False, indent=1),
                         encoding="utf-8")
        return {"ok": True, "template_id": tid, "queued": len(rows)}

    @app.get("/api/singing/supply-status")
    async def api_singing_supply_status(request: Request, _=Depends(api_auth)):
        """供给状态：夜批最近产出（factory.jsonl 尾） + 在队补货申请。"""
        from src.companion.song_stock import (
            data_root, resolve_singing_cfg, templates_dir,
        )
        scfg = resolve_singing_cfg(_cfg())
        tdir = templates_dir(scfg)
        pending = []
        try:
            f = tdir / "_supply_requests.json"
            if f.is_file():
                pending = [r for r in (json.loads(
                    f.read_text(encoding="utf-8")) or []) if isinstance(r, dict)]
        except Exception:
            pending = []
        recent = []
        try:
            jl = data_root() / "logs" / "song_factory" / "factory.jsonl"
            if jl.is_file():
                lines = jl.read_text(encoding="utf-8",
                                     errors="replace").splitlines()
                for ln in lines[-12:]:
                    try:
                        recent.append(json.loads(ln))
                    except Exception:
                        continue
        except Exception:
            recent = []
        return {"ok": True, "nightly_at": "05:10",
                "pending_requests": pending, "factory_recent": recent}
