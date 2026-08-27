"""表情包（贴纸）后台 API + 出站发送（2026-08-17 主线）。

挂 ``/api/stickers/*``（包/条目管理、收藏入包、官方包播种）+
``/api/unified-inbox/send-sticker``（跨平台发送）。

存储分层（对齐 persona_media 模式）：二进制落
``src/web/static/sticker_packs/<pack>/``（/static 直服），元数据落
``sticker_store``（SQLite）；规范化管线 ``sticker_normalize``（纯函数）。

跨平台发送语义（单一事实源 ``sticker_store.resolve_send_plan``）：
- Telegram：原生贴纸（pyrogram ``send_sticker`` webp；动图 ``send_animation`` GIF）；
- WhatsApp：Node 边车握手（/health caps.sticker）→ 原生贴纸 webp；老边车回退图片；
- LINE：官方商店贴纸（条目带 line_package_id/line_sticker_id）→ okline 原生
  ``send_sticker``；自建文件贴纸回退图片；
- 其余平台：回退图片 png。
响应带 ``sent_as``（sticker|image），前端据此提示「已按图片发送」。

Feature flag：``inbox.stickers.enabled``（默认关；zhiliao overlay 灰度开）。
写操作 viewer 只读拦截；发送走 send-media 同族护栏（能力权限/账号态/发送闸门/
幂等 send_dedup/坐席接管记账）。文案经 ``tr`` 收口零 CJK。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import Depends, HTTPException, Request

from src.inbox.sticker_normalize import (
    ALLOWED_INPUT_EXTS,
    MAX_INPUT_BYTES,
    StickerNormalizeError,
    normalize_sticker,
)
from src.inbox.sticker_store import (
    COLLECTED_PACK_ID,
    PACK_CUSTOM,
    PACK_OFFICIAL,
    get_sticker_store,
    resolve_send_plan,
    safe_pack_dir,
    sibling_path,
    sticker_root,
    sticker_url,
)
from src.web.web_i18n import tr

logger = logging.getLogger("ai_chat_assistant.sticker_routes")

_ROLE_VIEWER = "viewer"

# 规范化失败机器码 → i18n 键
_NORM_ERR_KEYS = {
    "not_image": "err.stk.not_image",
    "too_large_input": "err.stk.too_large",
    "too_large_output": "err.stk.too_large",
    "encode_failed": "err.stk.encode_failed",
}

# LINE 官方贴纸静态预览 CDN（商店贴纸的公开静帧，播种 ID 包时作面板缩略图）
_LINE_CDN_PREVIEW = (
    "https://stickershop.line-scdn.net/stickershop/v1/sticker/{sid}/android/sticker.png"
)

# WhatsApp Node 边车贴纸能力握手缓存（新 send-media 分支随边车重启才生效；
# 未升级的老边车把 sticker 掉成 document 附件——比回退图片更糟，故先探后发）。
_WA_STK_CAPS: Dict[str, Any] = {"ts": 0.0, "ok": False}
_WA_CAPS_TTL_SEC = 300

_ENGINE_ROOT = Path(__file__).resolve().parents[3]
_OFFICIAL_MANIFEST = _ENGINE_ROOT / "assets" / "sticker_packs" / "official" / "manifest.json"


async def _wa_sticker_capable(config: Dict[str, Any]) -> bool:
    """探测 WhatsApp Node 边车是否具备原生贴纸分支（/health caps.sticker，5min 缓存）。"""
    now = time.time()
    if now - float(_WA_STK_CAPS.get("ts") or 0) < _WA_CAPS_TTL_SEC:
        return bool(_WA_STK_CAPS.get("ok"))
    ok = False
    try:
        from src.integrations.whatsapp_baileys_login import (
            _get_json, service_base_url,
        )
        res = await _get_json(f"{service_base_url(config)}/health", timeout=4.0)
        ok = bool(((res or {}).get("caps") or {}).get("sticker"))
    except Exception:
        logger.debug("[stickers] WA 边车能力探测失败（按不支持处理）", exc_info=True)
        ok = False
    _WA_STK_CAPS["ts"] = now
    _WA_STK_CAPS["ok"] = ok
    return ok


def _reset_wa_caps_cache() -> None:
    """单测钩子：清握手缓存。"""
    _WA_STK_CAPS["ts"] = 0.0
    _WA_STK_CAPS["ok"] = False


def register_sticker_routes(app, auth_dep, audit_store=None, config_manager=None):
    """挂载表情包 API。``auth_dep`` 为登录校验依赖；``audit_store`` 操作审计（可选）。"""

    def _store():
        return get_sticker_store()

    def _cfg(request: Request) -> Dict[str, Any]:
        cm = getattr(request.app.state, "config_manager", None) or config_manager
        return (getattr(cm, "config", None) or {}) if cm else {}

    def _stk_conf(request: Request) -> Dict[str, Any]:
        v = ((_cfg(request).get("inbox") or {}).get("stickers") or {})
        return v if isinstance(v, dict) else {}

    def _enabled(request: Request) -> bool:
        return bool(_stk_conf(request).get("enabled", False))

    def _require_enabled(request: Request) -> None:
        if not _enabled(request):
            raise HTTPException(403, tr(request, "err.stk.disabled"))

    def _require_write(request: Request) -> None:
        try:
            role = request.session.get("role", "")
        except Exception:
            role = ""
        if role == _ROLE_VIEWER:
            raise HTTPException(403, tr(request, "err.stk.readonly"))

    def _require_store(request: Request):
        st = _store()
        if st is None:
            raise HTTPException(503, tr(request, "err.stk.store_unavailable"))
        return st

    def _actor(request: Request) -> str:
        try:
            return str(request.session.get("username") or "web_admin")
        except Exception:
            return "web_admin"

    def _audit(request: Request, action: str, target: str = "", detail: str = "") -> None:
        if audit_store is None:
            return
        try:
            audit_store.log(_actor(request), action, target, "", detail)
        except Exception:
            logger.debug("[stickers] 审计写入失败（已忽略）", exc_info=True)

    def _limits(request: Request) -> Dict[str, int]:
        c = _stk_conf(request)
        try:
            max_packs = int(c.get("max_packs") or 50)
        except Exception:
            max_packs = 50
        try:
            max_per_pack = int(c.get("max_per_pack") or 200)
        except Exception:
            max_per_pack = 200
        return {"max_packs": max_packs, "max_per_pack": max_per_pack}

    def _slim(row: Dict[str, Any]) -> Dict[str, Any]:
        """条目瘦身给前端面板（不暴露本地绝对路径）。"""
        return {
            "id": row.get("id"), "pack_id": row.get("pack_id"),
            "url": row.get("url"), "emoji_tag": row.get("emoji_tag"),
            "keywords": row.get("keywords") or [],
            "animated": bool(row.get("animated")),
            "line_only": bool(row.get("line_package_id")) and not row.get("file_path"),
            "sort": row.get("sort"),
        }

    def _save_normalized(st, request: Request, pack_id: str, data: bytes,
                         *, source_ext: str, created_by: str,
                         emoji_tag: str = "",
                         keywords: Optional[List[str]] = None) -> Dict[str, Any]:
        """规范化 + 去重 + 落盘 + 落库（上传/收藏/官方播种共用）。

        ``emoji_tag``/``keywords`` 供官方清单条目携带检索标签（面板搜索按它们
        过滤；上传/收藏路径无标签来源，维持空）。
        返回 ``{"item": row}`` 或 ``{"deduped": row}``；失败抛 HTTPException。
        """
        try:
            norm = normalize_sticker(data, source_ext=source_ext)
        except StickerNormalizeError as ex:
            raise HTTPException(415, tr(
                request, _NORM_ERR_KEYS.get(ex.reason, "err.stk.not_image")))
        sha = hashlib.sha256(norm["webp"]).hexdigest()
        dup = st.find_by_sha(pack_id, sha)
        if dup is not None:
            # 自愈（2026-08-17 实锤）：「DB 行在、产物文件丢」——/static 下的
            # 贴纸产物是未跟踪文件，git clean/分支切换/误删会把它们清掉；此时
            # 幂等重播本该是修复动作，却被 sha 去重挡住＝永久裂图。dedup 命中
            # 时校验行内 file_path 仍在，丢了按原路径重写（webp/png/gif 全套，
            # 只写落盘根内，行不动）。
            try:
                _fp = str(dup.get("file_path") or "")
                _root = sticker_root().resolve()
                if _fp and not os.path.isfile(_fp) \
                        and _root in Path(_fp).resolve().parents:
                    Path(_fp).parent.mkdir(parents=True, exist_ok=True)
                    Path(_fp).write_bytes(norm["webp"])
                    _pngp = sibling_path(_fp, ".png")
                    if _pngp:
                        Path(_pngp).write_bytes(norm["png"])
                    if norm.get("gif"):
                        _gifp = sibling_path(_fp, ".gif")
                        if _gifp:
                            Path(_gifp).write_bytes(norm["gif"])
                    logger.info("[stickers] 自愈重写丢失产物 %s", _fp)
            except Exception:
                logger.warning("[stickers] 产物自愈失败 id=%s",
                               dup.get("id"), exc_info=True)
            return {"deduped": dup}
        safe_dir = safe_pack_dir(pack_id)
        d = sticker_root() / safe_dir
        sid = uuid.uuid4().hex
        try:
            d.mkdir(parents=True, exist_ok=True)
            webp_path = d / f"{sid}.webp"
            webp_path.write_bytes(norm["webp"])
            (d / f"{sid}.png").write_bytes(norm["png"])
            if norm.get("gif"):
                (d / f"{sid}.gif").write_bytes(norm["gif"])
        except Exception as ex:  # noqa: BLE001
            logger.warning("[stickers] 保存文件失败: %s", ex, exc_info=True)
            raise HTTPException(500, tr(request, "err.stk.save_failed",
                                        err=str(ex)[:200]))
        row = st.add_sticker(
            pack_id,
            file_path=str(webp_path), url=sticker_url(safe_dir, f"{sid}.webp"),
            emoji_tag=str(emoji_tag or ""),
            keywords=[str(x) for x in (keywords or []) if str(x).strip()],
            animated=bool(norm["animated"]), bytes_=len(norm["webp"]),
            width=int(norm["width"]), height=int(norm["height"]),
            sha256=sha, sort=st.pack_size(pack_id), created_by=created_by,
            sticker_id=sid)
        return {"item": row}

    def _unlink_contained(file_path: str) -> None:
        """只删落盘根内的文件（含 png/gif 兄弟件），防误删。"""
        if not file_path:
            return
        try:
            root = sticker_root().resolve()
            for cand in (file_path, sibling_path(file_path, ".png"),
                         sibling_path(file_path, ".gif")):
                if not cand:
                    continue
                fp = Path(cand).resolve()
                if fp.is_file() and root in fp.parents:
                    fp.unlink()
        except Exception:
            logger.debug("[stickers] 删除文件失败（已忽略）", exc_info=True)

    # ── 面板/管理 API ────────────────────────────────────────────────────

    @app.get("/api/stickers/status")
    async def api_stickers_status(request: Request, _=Depends(auth_dep)):
        """轻量能力探测（前端据此显隐贴纸 tab；关闭态不报错只报 false）。"""
        if not _enabled(request):
            return {"ok": True, "enabled": False}
        st = _store()
        counts = st.counts() if st is not None else {"packs": 0, "stickers": 0}
        return {"ok": True, "enabled": True, **counts}

    @app.get("/api/stickers/packs")
    async def api_stickers_packs(request: Request, _=Depends(auth_dep)):
        """面板一次拉全：包列表（含条目数/封面）+ 最近使用。关闭态回 enabled:false。"""
        if not _enabled(request):
            return {"ok": True, "enabled": False, "packs": [], "recent": []}
        st = _require_store(request)
        return {
            "ok": True, "enabled": True,
            "packs": st.list_packs(),
            "recent": [_slim(r) for r in st.recent(24)],
            "limits": _limits(request),
        }

    @app.post("/api/stickers/packs")
    async def api_stickers_pack_create(request: Request, _=Depends(auth_dep)):
        _require_enabled(request)
        _require_write(request)
        st = _require_store(request)
        body = await request.json()
        title = str((body or {}).get("title") or "").strip()
        if not title:
            raise HTTPException(400, tr(request, "err.ws.field_required",
                                        field="title"))
        if len(st.list_packs(include_disabled=True)) >= _limits(request)["max_packs"]:
            raise HTTPException(409, tr(request, "err.stk.pack_limit",
                                        n=_limits(request)["max_packs"]))
        pack = st.create_pack(title, kind=PACK_CUSTOM, created_by=_actor(request))
        _audit(request, "stk_pack_create", f"pack={pack.get('id')}", title)
        return {"ok": True, "pack": pack}

    @app.patch("/api/stickers/packs/{pack_id}")
    async def api_stickers_pack_update(pack_id: str, request: Request,
                                       _=Depends(auth_dep)):
        _require_enabled(request)
        _require_write(request)
        st = _require_store(request)
        if st.get_pack(pack_id) is None:
            raise HTTPException(404, tr(request, "err.stk.pack_not_found"))
        body = await request.json()
        fields: Dict[str, Any] = {}
        if isinstance(body, dict):
            for k in ("title", "sort", "enabled"):
                if k in body:
                    fields[k] = body[k]
        pack = st.update_pack(pack_id, **fields)
        _audit(request, "stk_pack_update", f"pack={pack_id}",
               ",".join(sorted(fields.keys())))
        return {"ok": True, "pack": pack}

    @app.delete("/api/stickers/packs/{pack_id}")
    async def api_stickers_pack_delete(pack_id: str, request: Request,
                                       _=Depends(auth_dep)):
        _require_enabled(request)
        _require_write(request)
        st = _require_store(request)
        pack = st.get_pack(pack_id)
        if pack is None:
            raise HTTPException(404, tr(request, "err.stk.pack_not_found"))
        for fp in st.delete_pack(pack_id):
            _unlink_contained(fp)
        _audit(request, "stk_pack_delete", f"pack={pack_id}",
               str(pack.get("title") or ""))
        return {"ok": True}

    @app.get("/api/stickers/packs/{pack_id}/items")
    async def api_stickers_items(pack_id: str, request: Request,
                                 _=Depends(auth_dep)):
        _require_enabled(request)
        st = _require_store(request)
        if st.get_pack(pack_id) is None:
            raise HTTPException(404, tr(request, "err.stk.pack_not_found"))
        return {"ok": True,
                "items": [_slim(r) for r in st.list_stickers(pack_id)]}

    @app.post("/api/stickers/packs/{pack_id}/items")
    async def api_stickers_upload(pack_id: str, request: Request,
                                  _=Depends(auth_dep)):
        """批量上传（multipart，字段名 file 可多个）。逐张规范化，
        单张失败不阻断整批；响应带成功/去重/失败清单。"""
        _require_enabled(request)
        _require_write(request)
        st = _require_store(request)
        pack = st.get_pack(pack_id)
        if pack is None:
            raise HTTPException(404, tr(request, "err.stk.pack_not_found"))
        form = await request.form()
        uploads = [u for u in form.getlist("file")
                   if u is not None and getattr(u, "filename", "")]
        if not uploads:
            raise HTTPException(400, tr(request, "err.pmedia.file_required"))
        max_per_pack = _limits(request)["max_per_pack"]
        actor = _actor(request)
        added: List[Dict[str, Any]] = []
        failed: List[Dict[str, Any]] = []
        deduped = 0
        for up in uploads:
            fname = str(up.filename or "")
            ext = os.path.splitext(fname)[1].lower()
            if ext not in ALLOWED_INPUT_EXTS:
                failed.append({"name": fname, "reason": "ext"})
                continue
            if st.pack_size(pack_id) >= max_per_pack:
                failed.append({"name": fname, "reason": "pack_full"})
                continue
            data = await up.read()
            if not data or len(data) > MAX_INPUT_BYTES:
                failed.append({"name": fname, "reason": "size"})
                continue
            try:
                out = _save_normalized(st, request, pack_id, data,
                                       source_ext=ext, created_by=actor)
            except HTTPException as ex:
                failed.append({"name": fname,
                               "reason": str(getattr(ex, "detail", "")) or "error"})
                continue
            if "deduped" in out:
                deduped += 1
            else:
                added.append(_slim(out["item"]))
        _audit(request, "stk_upload", f"pack={pack_id}",
               f"added={len(added)} deduped={deduped} failed={len(failed)}")
        return {"ok": True, "added": added, "deduped": deduped, "failed": failed}

    @app.delete("/api/stickers/packs/{pack_id}/items/{sid}")
    async def api_stickers_item_delete(pack_id: str, sid: str, request: Request,
                                       _=Depends(auth_dep)):
        _require_enabled(request)
        _require_write(request)
        st = _require_store(request)
        row = st.get(sid)
        if row is None or str(row.get("pack_id")) != str(pack_id):
            raise HTTPException(404, tr(request, "err.stk.not_found"))
        st.delete_sticker(sid)
        _unlink_contained(str(row.get("file_path") or ""))
        _audit(request, "stk_delete", f"pack={pack_id} id={sid}")
        return {"ok": True}

    @app.post("/api/stickers/collect")
    async def api_stickers_collect(request: Request, _=Depends(auth_dep)):
        """把一条入站贴纸（protocol_media 里的 webp/图片）收藏进表情包。

        Body: ``{media_ref, pack_id?}``；缺省进「收藏」包（首次自动建）。
        只认 protocol_media 根内的 /static 引用（路径穿越守卫与 media-download 同款）。
        """
        _require_enabled(request)
        _require_write(request)
        st = _require_store(request)
        body = await request.json()
        media_ref = str((body or {}).get("media_ref") or "")
        pack_id = str((body or {}).get("pack_id") or COLLECTED_PACK_ID)
        from src.inbox.media_guard import resolve_contained_path_any
        from src.integrations.protocol_bridge import (
            protocol_media_roots, static_media_ref_to_path,
        )
        cand = static_media_ref_to_path(media_ref)
        if not cand:
            raise HTTPException(404, tr(request, "err.inbox.media_not_found"))
        path = resolve_contained_path_any(protocol_media_roots(), cand)
        if not path or not os.path.isfile(path):
            raise HTTPException(404, tr(request, "err.inbox.media_not_found"))
        if st.get_pack(pack_id) is None:
            if pack_id != COLLECTED_PACK_ID:
                raise HTTPException(404, tr(request, "err.stk.pack_not_found"))
            st.create_pack(tr(request, "inbox.stk.pack_collected_title"),
                           kind=PACK_CUSTOM, pack_id=COLLECTED_PACK_ID,
                           created_by=_actor(request))
        if st.pack_size(pack_id) >= _limits(request)["max_per_pack"]:
            raise HTTPException(409, tr(request, "err.stk.pack_full"))
        try:
            with open(path, "rb") as fh:
                data = fh.read(MAX_INPUT_BYTES + 1)
        except Exception as ex:  # noqa: BLE001
            raise HTTPException(500, tr(request, "err.stk.save_failed",
                                        err=str(ex)[:200]))
        if not data or len(data) > MAX_INPUT_BYTES:
            raise HTTPException(415, tr(request, "err.stk.too_large"))
        out = _save_normalized(st, request, pack_id, data,
                               source_ext=os.path.splitext(path)[1].lower(),
                               created_by=_actor(request))
        _audit(request, "stk_collect", f"pack={pack_id}", media_ref[:120])
        try:
            from src.inbox.sticker_stats import get_sticker_stats
            get_sticker_stats().record_collect()
        except Exception:
            pass
        if "deduped" in out:
            return {"ok": True, "deduped": True, "item": _slim(out["deduped"])}
        return {"ok": True, "item": _slim(out["item"])}

    @app.post("/api/stickers/seed-official")
    async def api_stickers_seed_official(request: Request, _=Depends(auth_dep)):
        """播种官方表情包（kb_starter 模式）：读引擎 ``assets/sticker_packs/official/
        manifest.json``，幂等导入（已存在的包/条目跳过）。

        manifest 支持两类条目：``line_stickers``（LINE 商店贴纸纯 ID 映射，预览走
        LINE CDN 静帧，仅 LINE 会话可原生发送）与 ``files``（相对 manifest 目录的
        本地图片，经规范化管线入库，四平台可发）。
        """
        _require_enabled(request)
        _require_write(request)
        st = _require_store(request)
        if not _OFFICIAL_MANIFEST.is_file():
            raise HTTPException(404, tr(request, "err.stk.manifest_missing"))
        try:
            manifest = json.loads(_OFFICIAL_MANIFEST.read_text(encoding="utf-8"))
        except Exception as ex:  # noqa: BLE001
            raise HTTPException(500, tr(request, "err.stk.manifest_bad",
                                        err=str(ex)[:200]))
        actor = _actor(request)
        packs_new = 0
        items_new = 0
        for pk in (manifest.get("packs") or []):
            if not isinstance(pk, dict):
                continue
            pid = str(pk.get("id") or "").strip()
            title = str(pk.get("title") or pid).strip()
            if not pid:
                continue
            existed = st.get_pack(pid) is not None
            st.create_pack(title, kind=PACK_OFFICIAL, pack_id=pid,
                           created_by=actor, sort=int(pk.get("sort") or 0))
            if not existed:
                packs_new += 1
            # 区间写法（清单免手写上百行）：{"package_id","from","to"} 展开为逐条
            _line_items = list(pk.get("line_stickers") or [])
            _rng = pk.get("line_sticker_range")
            if isinstance(_rng, dict):
                try:
                    _pkg = str(_rng.get("package_id") or "").strip()
                    _lo = int(_rng.get("from"))
                    _hi = int(_rng.get("to"))
                    if _pkg and 0 < _hi - _lo < 500:
                        _line_items.extend(
                            {"package_id": _pkg, "sticker_id": str(s)}
                            for s in range(_lo, _hi + 1))
                except Exception:
                    logger.warning("[stickers] 官方包区间解析失败 pack=%s", pid)
            for i, ls in enumerate(_line_items):
                if not isinstance(ls, dict):
                    continue
                l_pkg = str(ls.get("package_id") or "").strip()
                l_sid = str(ls.get("sticker_id") or "").strip()
                if not (l_pkg and l_sid):
                    continue
                sid = f"{pid}-{l_sid}"
                if st.get(sid) is not None:
                    continue
                st.add_sticker(
                    pid, sticker_id=sid,
                    url=_LINE_CDN_PREVIEW.format(sid=l_sid),
                    emoji_tag=str(ls.get("emoji") or ""),
                    line_package_id=l_pkg, line_sticker_id=l_sid,
                    sort=i, created_by=actor)
                items_new += 1
            base_dir = _OFFICIAL_MANIFEST.parent
            for i, rel in enumerate(pk.get("files") or []):
                # 双形态：纯路径字符串（旧），或 {"file","emoji","keywords"}
                # 字典（带检索标签——面板搜索按 emoji_tag/keywords 过滤，
                # 纯路径导入的贴纸搜不到）。
                f_emoji, f_kw = "", []  # type: ignore[var-annotated]
                if isinstance(rel, dict):
                    f_emoji = str(rel.get("emoji") or "")
                    f_kw = [str(x) for x in (rel.get("keywords") or [])
                            if str(x).strip()]
                    rel = str(rel.get("file") or "")
                if not str(rel or "").strip():
                    continue
                fp = (base_dir / str(rel)).resolve()
                # 只认 manifest 目录内文件（防清单被改出目录穿越）
                if base_dir.resolve() not in fp.parents or not fp.is_file():
                    continue
                try:
                    data = fp.read_bytes()
                    out = _save_normalized(
                        st, request, pid, data,
                        source_ext=fp.suffix.lower(), created_by=actor,
                        emoji_tag=f_emoji, keywords=f_kw)
                    if "item" in out:
                        items_new += 1
                except HTTPException:
                    logger.warning("[stickers] 官方包文件导入失败 %s", fp)
        _audit(request, "stk_seed_official", "",
               f"packs+{packs_new} items+{items_new}")
        return {"ok": True, "packs_added": packs_new, "items_added": items_new}

    # ── 发送 ────────────────────────────────────────────────────────────

    @app.post("/api/unified-inbox/send-sticker")
    async def api_unified_inbox_send_sticker(request: Request,
                                             _=Depends(auth_dep)):
        """坐席从表情面板发送贴纸。Body:
        ``{platform, account_id, chat_key, sticker_id, client_msg_id?}``。

        平台映射见模块 docstring；护栏与 send-media 同族（能力权限/账号态/
        发送闸门预检/幂等/接管记账）。响应 ``{ok, sent_as, media_ref}``。
        """
        _require_enabled(request)
        body = await request.json()
        platform = str((body or {}).get("platform") or "").lower()
        account_id = str((body or {}).get("account_id") or "default")
        chat_key = str((body or {}).get("chat_key") or "")
        sticker_id = str((body or {}).get("sticker_id") or "")
        if not (platform and chat_key and sticker_id):
            raise HTTPException(400, tr(request, "err.ws.field_required",
                                        field="platform/chat_key/sticker_id"))
        from src.web.routes.unified_inbox_send_routes import (
            _deny_capability,
            _raise_if_account_blocked,
            _result_undelivered,
            _send_blocked_exc,
            _send_gate_exc,
        )
        _deny_capability(request, "chat.send_media")
        _raise_if_account_blocked(request, platform, account_id)
        st = _require_store(request)
        row = st.get(sticker_id)
        if row is None or not row.get("enabled"):
            raise HTTPException(404, tr(request, "err.stk.not_found"))

        from src.integrations.account_orchestrator import get_orchestrator
        orch = get_orchestrator()
        from src.inbox.send_dedup import get_send_dedup
        _client_msg_id = str((body or {}).get("client_msg_id") or "").strip()
        _dedup = get_send_dedup()
        _dedup_scope = f"sticker:{platform}:{account_id}:{chat_key}"

        from src.web.routes.unified_inbox_auth import _session_agent
        from src.web.routes.unified_inbox_services import _inbox_store
        from src.inbox.normalizer import conv_id as _conv_id

        def _record_agent(cid: str) -> None:
            try:
                ibx = _inbox_store(request)
                if ibx is not None:
                    _agent = _session_agent(request)
                    ibx.record_agent_send(
                        cid, _agent["agent_id"],
                        agent_name=_agent.get("display_name", ""))
                    from src.inbox.takeover_rearm import record_agent_takeover
                    record_agent_takeover(ibx, cid)
            except Exception:
                logger.debug("record_agent_send(sticker) 失败", exc_info=True)

        # ── LINE 官方商店贴纸（纯 ID，okline 原生）───────────────────
        line_native = (
            platform == "line" and str(row.get("line_package_id") or "")
            and str(row.get("line_sticker_id") or ""))
        if line_native:
            w = orch.worker_for("line", account_id)
            if w is None or not hasattr(w, "send_line_sticker"):
                raise HTTPException(501, tr(request, "err.stk.line_worker_na"))
            _gate_ex = _send_gate_exc(request, platform, account_id, chat_key,
                                      owned=True)
            if _gate_ex is not None:
                raise _gate_ex
            if not _dedup.reserve(_dedup_scope, _client_msg_id):
                return {"ok": True, "duplicate": True}
            try:
                res = await w.send_line_sticker(
                    chat_key, str(row["line_package_id"]),
                    str(row["line_sticker_id"]))
            except Exception as ex:  # noqa: BLE001
                _dedup.release(_dedup_scope, _client_msg_id)
                raise HTTPException(502, tr(request, "err.stk.send_failed",
                                            err=str(ex)[:200]))
            _undeliv = _result_undelivered(res)
            if _undeliv:
                _dedup.release(_dedup_scope, _client_msg_id)
                if _undeliv == "blocked":
                    raise _send_blocked_exc(
                        request, platform, account_id, chat_key,
                        reason=str(res.get("blocked") or ""))
                raise HTTPException(502, tr(
                    request, "err.inbox.send_not_delivered",
                    msg=str(res.get("error") or "")))
            # 镜像回写（编排器旁路——原生贴纸不走 send_media，需自行落线程）
            try:
                from src.integrations.protocol_bridge import (
                    emit_incoming, make_message,
                )
                emit_incoming(make_message(
                    platform=platform, account_id=account_id, chat_key=chat_key,
                    text="", direction="out",
                    msg_id=str((res or {}).get("message_id") or ""),
                    media_type="sticker", media_ref=str(row.get("url") or "")))
            except Exception:
                logger.debug("[stickers] LINE 贴纸镜像回写失败", exc_info=True)
            st.record_use(sticker_id)
            _record_agent(_conv_id(platform, account_id, chat_key))
            _audit(request, "stk_send", f"id={sticker_id}",
                   f"{platform}:{account_id} native_line")
            try:
                from src.inbox.sticker_stats import get_sticker_stats
                get_sticker_stats().record_send(platform, "sticker")
            except Exception:
                pass
            return {"ok": True, "sent_as": "sticker", "native": "line",
                    "media_ref": str(row.get("url") or "")}

        # ── 文件贴纸（TG/WA 原生；其余图片回退）──────────────────────
        file_path = str(row.get("file_path") or "")
        if not file_path or not os.path.isfile(file_path):
            # LINE 纯 ID 贴纸发到非 LINE 会话：无本地文件可回退
            raise HTTPException(409, tr(request, "err.stk.line_only"))
        if not orch.owns_media(platform, account_id):
            raise HTTPException(501, tr(request, "err.inbox.media_unsupported"))
        _gate_ex = _send_gate_exc(request, platform, account_id, chat_key,
                                  owned=True)
        if _gate_ex is not None:
            raise _gate_ex

        wa_capable = False
        if platform == "whatsapp":
            wa_capable = await _wa_sticker_capable(_cfg(request))
        plan = resolve_send_plan(platform, row, wa_sticker_capable=wa_capable)
        variant_path = file_path if plan["variant"] == "webp" else \
            sibling_path(file_path, f".{plan['variant']}")
        if not variant_path or not os.path.isfile(variant_path):
            variant_path = file_path  # 兄弟件缺失回落 webp 原件

        if not _dedup.reserve(_dedup_scope, _client_msg_id):
            return {"ok": True, "duplicate": True}
        try:
            with open(variant_path, "rb") as fh:
                data = fh.read()
            from src.integrations.protocol_bridge import save_outbound_media
            local, url, _mt = save_outbound_media(
                platform, account_id, os.path.basename(variant_path), data)
        except Exception as ex:  # noqa: BLE001
            _dedup.release(_dedup_scope, _client_msg_id)
            raise HTTPException(500, tr(request, "err.stk.save_failed",
                                        err=str(ex)[:200]))
        try:
            try:
                res = await orch.send_media(
                    platform, account_id, chat_key,
                    media_path=local, media_url=url,
                    media_type=plan["eff_type"], caption="",
                    mirror_media_type=plan["mirror_type"], origin="manual")
            except TypeError:
                # 旧编排器签名（无 mirror_media_type/origin，测试假编排器常见）
                res = await orch.send_media(
                    platform, account_id, chat_key,
                    media_path=local, media_url=url,
                    media_type=plan["eff_type"], caption="")
        except Exception as ex:  # noqa: BLE001
            _dedup.release(_dedup_scope, _client_msg_id)
            raise HTTPException(502, tr(request, "err.stk.send_failed",
                                        err=str(ex)[:200]))
        _undeliv = _result_undelivered(res)
        if _undeliv:
            _dedup.release(_dedup_scope, _client_msg_id)
            if _undeliv == "blocked":
                raise _send_blocked_exc(
                    request, platform, account_id, chat_key,
                    reason=str(res.get("blocked") or ""))
            raise HTTPException(502, tr(
                request, "err.inbox.send_not_delivered",
                msg=str(res.get("error") or res.get("error_kind") or "")))
        st.record_use(sticker_id)
        _record_agent(_conv_id(platform, account_id, chat_key))
        _audit(request, "stk_send", f"id={sticker_id}",
               f"{platform}:{account_id} as={plan['sent_as']}")
        try:
            from src.inbox.sticker_stats import get_sticker_stats
            get_sticker_stats().record_send(platform, plan["sent_as"])
        except Exception:
            pass
        return {"ok": True, "sent_as": plan["sent_as"], "media_ref": url,
                "result": res}
