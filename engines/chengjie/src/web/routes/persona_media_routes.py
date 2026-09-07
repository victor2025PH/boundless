"""每人设「相册/媒体」后台 API —— 上传/列表/改/删/试触发。

挂 ``/api/personas/{pid}/media*``。文件落 ``src/web/static/persona_albums/<pid>/``（经 /static
直服，供网格缩略图与前端预览），元数据（触发词/配文/权重/关系闸门/命中）落 DB
（``persona_media_store``）。回复链（image_autosend / skill_manager Stage 0）读同一份 store。

护栏：扩展名白名单（图 jpg/png/webp/gif、视频 mp4/mov/webm/m4v）、体积上限（图 10MB / 视频 50MB）、
视频时长上限（默认 3 分钟，仅当 ffprobe 可探时才拦；软失败不阻塞）、sha256 去重、
persona_id 目录消毒防穿越、写操作 viewer 只读拦截。文案经 ``tr`` 收口零 CJK。

视频上传附带元数据探测（ffprobe 拿时长/宽高）+ 抽帧生成封面缩略图（ffmpeg，落 ``*.thumb.jpg``）；
图片探宽高（PIL）。以上全部软失败——缺 ffmpeg/ffprobe/PIL 只是拿不到该项元数据，不影响上传落库。
"""

import hashlib
import json
import logging
import os
import re
import uuid
from pathlib import Path
from typing import Any, List

from fastapi import Depends, HTTPException, Request

import src.companion.media_auto_tag as _auto_tag
from src.companion.image_phash import (
    NEAR_DUP_MAX_HAMMING as _NEAR_DUP_MAX,
    nearest as _phash_nearest,
    phash_bytes as _phash_bytes,
)
from src.companion.media_ingest import process_photo_bytes as _process_photo
from src.companion.media_probe import (
    make_photo_thumbnail as _make_photo_thumbnail,
    make_video_thumbnail as _make_video_thumbnail,
    probe_image as _probe_image,
    probe_video as _probe_video,
)
from src.web.web_i18n import tr

logger = logging.getLogger("ai_chat_assistant.persona_media_routes")

# #67-①（0830 skuio 机实锤）：相册根改走数据根单一事实源——旧 Path(__file__)
# 推法在打包桌面态落进安装目录（更新即清空）。resolve_album_root 数据根优先、
# 裸引擎回落旧树；serving 由 admin.py 的 /static/persona_albums 双根挂载兜住。
from src.companion.media_paths import (
    LEGACY_ALBUM_ROOT as _ALBUM_ROOT_LEGACY,
    resolve_album_root as _resolve_album_root,
    sniff_media_bytes as _sniff_media,
)

#: 显式覆写口（测试 monkeypatch 既有契约 / 特殊部署）；None=每次按
#: resolve_album_root() 动态解析（env 热切换、多实例各归各的数据根）。
_ALBUM_ROOT: "Path | None" = None


def _album_root() -> Path:
    return _ALBUM_ROOT if _ALBUM_ROOT is not None else _resolve_album_root()

_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
_VIDEO_EXT = {".mp4", ".mov", ".webm", ".m4v"}
_MAX_PHOTO_BYTES = 10 * 1024 * 1024
_MAX_VIDEO_BYTES = 50 * 1024 * 1024
_MAX_VIDEO_DURATION_MS = 3 * 60 * 1000  # 视频时长上限（仅 ffprobe 可探时才拦）
_ROLE_VIEWER = "viewer"


def _safe_pid(pid: Any) -> str:
    """人设 id 收敛为安全目录名（防路径穿越）。"""
    s = re.sub(r"[^A-Za-z0-9_-]", "_", str(pid or ""))[:64]
    return s or "default"


def _media_type_for_ext(ext: str) -> str:
    e = (ext or "").lower()
    if e in _VIDEO_EXT:
        return "video"
    if e in _IMAGE_EXT:
        return "photo"
    return ""


def _as_str_list(raw: Any) -> List[str]:
    """触发词/标签解析：接受 JSON 数组 或 逗号/顿号/换行分隔的字符串。"""
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if str(x).strip()]
    s = str(raw).strip()
    if not s:
        return []
    if s[0] == "[":
        try:
            v = json.loads(s)
            if isinstance(v, list):
                return [str(x).strip() for x in v if str(x).strip()]
        except Exception:
            pass
    return [t.strip() for t in re.split(r"[,\n，、;；]", s) if t.strip()]


def register_persona_media_routes(app, auth_dep, audit_store=None, config_manager=None):
    """挂载每人设相册后台 API。``auth_dep`` 为登录校验依赖；``audit_store`` 为操作审计（可选）。"""

    def _store():
        from src.companion.persona_media_store import get_persona_media_store
        return get_persona_media_store()

    # #67-① 存量迁移（幂等 best-effort）：旧引擎树/安装目录相册 → 数据根，
    # 并把 DB file_path 改指新位置（发送链读 DB 绝对路径，不改写=白复制）。
    try:
        from src.companion.media_paths import migrate_legacy_album_tree
        migrate_legacy_album_tree(_store())
    except Exception:
        logger.debug("[pmedia] 相册存量迁移跳过", exc_info=True)

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
            logger.debug("[pmedia] 审计写入失败（已忽略）", exc_info=True)

    def _require_store(request: Request):
        st = _store()
        if st is None:
            raise HTTPException(503, tr(request, "err.pmedia.store_unavailable"))
        return st

    def _require_write(request: Request):
        try:
            role = request.session.get("role", "")
        except Exception:
            role = ""
        if role == _ROLE_VIEWER:
            raise HTTPException(403, tr(request, "err.persona.readonly_no_edit"))

    def _require_persona(request: Request, pid: str):
        from src.utils.persona_manager import PersonaManager
        p = PersonaManager.get_instance().get_persona_by_id(str(pid))
        if p is None:
            raise HTTPException(404, tr(request, "err.pmedia.persona_not_found", name=pid))
        return p

    def _owned_row(request: Request, st, pid: str, mid: str):
        row = st.get(str(mid))
        if row is None or str(row.get("persona_id")) != str(pid):
            raise HTTPException(404, tr(request, "err.pmedia.not_found"))
        return row

    def _vision_cfg() -> dict:
        try:
            cfg = getattr(config_manager, "config", None) or {}
        except Exception:
            cfg = {}
        return dict(cfg.get("vision") or {})

    def _album_ai_cfg() -> dict:
        try:
            cfg = getattr(config_manager, "config", None) or {}
        except Exception:
            cfg = {}
        return _auto_tag.resolve_album_ai_cfg(cfg)

    def _face_ref_for_tagging(pid: str, aicfg: dict) -> str:
        """脸一致性抽检的基准照路径（face_check 关/无基准照 → ""＝跳过）。"""
        if not aicfg.get("face_check", True):
            return ""
        try:
            p = _find_face_ref(pid)
            return str(p) if p is not None else ""
        except Exception:
            return ""

    @app.get("/api/personas/{pid}/media")
    async def list_persona_media(pid: str, request: Request, _=Depends(auth_dep)):
        """列出该人设全部媒体条目 + 统计 + AI 打标覆盖摘要（实施90）。"""
        st = _require_store(request)
        items = st.list(str(pid))
        ai = {"tagged": 0, "pending": 0, "failed": 0, "skipped": 0,
              "untagged": 0, "thumbs_missing": 0}
        for it in items:
            s = str(it.get("tag_status") or "")
            key = s if s in ("tagged", "pending", "failed", "skipped") else "untagged"
            ai[key] += 1
            if (str(it.get("media_type") or "photo") == "photo"
                    and not str(it.get("thumb_url") or "")):
                ai["thumbs_missing"] += 1
        snap = _auto_tag.stats_snapshot()
        ai["batch_active"] = bool(snap.get("batch_active"))
        _aicfg = _album_ai_cfg()
        ai["enabled"] = bool(_aicfg.get("enabled"))
        ai["auto_on_upload"] = bool(_aicfg.get("enabled")) and bool(_aicfg.get("auto_on_upload"))
        # #238：识图能不能打 + 最近一次失败原因——前端据此说「识图服务暂不可用」而不是显示 0/144
        probe = _auto_tag.vision_probe(_vision_cfg())
        ai["vision_ready"] = bool(probe.get("ready"))
        ai["vision_reason"] = str(probe.get("reason") or "")
        ai["last_error"] = str(snap.get("last_error") or "")
        return {"items": items, "stats": st.stats(str(pid)), "ai": ai}

    @app.post("/api/personas/{pid}/speech-print")
    async def save_persona_speech_print(pid: str, request: Request,
                                        _=Depends(auth_dep)):
        """WP-6/P1-1：把该人设的说话指纹条目写进实例 overlay（数据区，升级不丢）。

        Body: ``{entry: {print, catch?, example?, guide?}}``；键=人设口称名
        （resolve_spoken_name，与 spoken_style 运行时逐字同口径）。打包态出厂件
        只读也能写（这正是本端点存在的理由）；校验/净化在
        ``speech_prints_overlay.validate_entry``。
        """
        _require_write(request)
        p = _require_persona(request, pid)
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "invalid JSON body")
        from src.ai.speech_prints_overlay import save_entry
        from src.utils.persona_manager import PersonaManager
        spoken = str(PersonaManager.resolve_spoken_name(p) or "").strip()
        if not spoken:
            raise HTTPException(400, tr(request, "err.ws.field_required",
                                        field="spoken_name"))
        try:
            clean = save_entry(spoken, (body or {}).get("entry"))
        except ValueError as e:
            raise HTTPException(400, str(e))
        _audit(request, "pmedia_speech_print_save", f"{pid}:{spoken}",
               json.dumps(clean, ensure_ascii=False)[:400])
        return {"ok": True, "spoken_name": spoken, "entry": clean}

    @app.get("/api/personas/{pid}/stock-readiness")
    async def persona_stock_readiness(pid: str, request: Request,
                                      _=Depends(auth_dep)):
        """「上线准备」面板（#189 起）：档案/声音/相册/绑定账号 四项清单 + 台词库/
        说话指纹两项 internal 聚合（只读）。

        逻辑全在 ``src.companion.persona_stock``（可单测纯聚合）；本路由只喂
        真实源（PersonaManager 档案 + 相册供给快照 + 实例配置 + 绑定用量）。
        """
        p = _require_persona(request, pid)
        from src.companion.media_gap import collect_scene_supply
        from src.companion.persona_stock import (
            collect_binding_usage, collect_stock_readiness)

        cfg = getattr(config_manager, "config", None) or {}
        scfg = (cfg.get("companion") or {}).get("selfie") or {}
        try:
            supply = collect_scene_supply(scfg)
        except Exception:
            logger.debug("[persona-stock] 相册供给读取失败（按空计）",
                         exc_info=True)
            supply = {}
        try:
            from src.utils.persona_manager import PersonaManager
            binding = collect_binding_usage(str(pid), PersonaManager.get_instance(), cfg)
        except Exception:
            logger.debug("[persona-stock] 绑定用量读取失败（按 0 计）", exc_info=True)
            binding = {"account_count": 0, "chat_count": 0, "is_default": False, "accounts": []}
        out = collect_stock_readiness(str(pid), p, cfg, supply=supply, binding=binding)
        out["ok"] = True
        return out

    @app.post("/api/personas/{pid}/media")
    async def upload_persona_media(pid: str, request: Request, _=Depends(auth_dep)):
        """上传一张图/一段视频到该人设相册（multipart: file + 可选 triggers/caption/tags/...）。"""
        _require_write(request)
        st = _require_store(request)
        _require_persona(request, pid)
        form = await request.form()
        upload = form.get("file")
        if upload is None or not getattr(upload, "filename", ""):
            raise HTTPException(400, tr(request, "err.pmedia.file_required"))
        ext = os.path.splitext(str(upload.filename))[1].lower()
        mtype = _media_type_for_ext(ext)
        if not mtype:
            raise HTTPException(400, tr(request, "err.pmedia.ext_not_allowed", ext=ext or "?"))
        data = await upload.read()
        if not data:
            raise HTTPException(400, tr(request, "err.inbox.empty_file"))
        limit = _MAX_VIDEO_BYTES if mtype == "video" else _MAX_PHOTO_BYTES
        if len(data) > limit:
            raise HTTPException(
                413, tr(request, "err.pmedia.too_large", mb=limit // (1024 * 1024)))
        # #67-②（0830 两机实锤）+ 媒体产物验证纪律：内容级校验——此前上传
        # 成功只看扩展名+体积，损坏文件原样落盘，到 AI 发图才在 pyrogram 处
        # 爆 decode 失败。magic bytes 不过=当场 400（诚实拒收），绝不静默存坏。
        _bad = _sniff_media(data, ext)
        if _bad:
            raise HTTPException(400, tr(
                request, "err.pmedia.bad_content", why=_bad))
        sha = hashlib.sha256(data).hexdigest()
        dup = st.find_by_sha(str(pid), sha)
        if dup is not None:
            return {"ok": True, "item": dup, "deduped": True}
        # 实施90 照片入库预处理（全软失败）：EXIF 抽提示→剥离（隐私）+ pHash
        # 近重复预警（sha 只能抓字节级重传；重编码/缩放的"同一张"靠指纹）。
        near_dup = None
        exif_hints: dict = {}
        phash = ""
        if mtype == "photo":
            processed = _process_photo(data, ext)
            data = processed.get("bytes") or data
            exif_hints = processed.get("hints") or {}
            phash = _phash_bytes(data)
            if phash:
                nid, ndist = _phash_nearest(phash, st.phashes(str(pid)))
                if nid and ndist <= _NEAR_DUP_MAX:
                    near_dup = {"id": nid, "distance": ndist}
        safe = _safe_pid(pid)
        d = _album_root() / safe
        try:
            d.mkdir(parents=True, exist_ok=True)
            name = f"{uuid.uuid4().hex}{ext}"
            fpath = (d / name).resolve()
            fpath.write_bytes(data)
            # 落盘后回读验证（纪律：写成功≠内容对）：首段字节与内存不一致
            # =写入层损坏，当场删除报错，绝不让坏文件带着「上传成功」活下来。
            _head = fpath.read_bytes()[:16] if fpath.stat().st_size else b""
            if _head != bytes(data[:16]):
                try:
                    fpath.unlink()
                except Exception:
                    pass
                raise HTTPException(500, tr(
                    request, "err.pmedia.save_failed", err="write verify failed"))
        except HTTPException:
            raise
        except Exception as ex:  # noqa: BLE001
            logger.warning("[pmedia] 保存文件失败: %s", ex, exc_info=True)
            raise HTTPException(500, tr(request, "err.pmedia.save_failed", err=str(ex)[:200]))
        url = f"/static/persona_albums/{safe}/{name}"
        # 元数据探测 + 视频护栏/封面（全部软失败，缺 ffmpeg/ffprobe/PIL 不阻塞上传）。
        width = height = duration_ms = 0
        thumb_url = ""
        if mtype == "video":
            meta = _probe_video(str(fpath)) or {}
            width = int(meta.get("width") or 0)
            height = int(meta.get("height") or 0)
            duration_ms = int(meta.get("duration_ms") or 0)
            if 0 < _MAX_VIDEO_DURATION_MS < duration_ms:
                try:
                    fpath.unlink()
                except Exception:
                    pass
                raise HTTPException(413, tr(
                    request, "err.pmedia.too_long",
                    sec=_MAX_VIDEO_DURATION_MS // 1000))
            thumb_name = f"{name}.thumb.jpg"
            at_sec = min(1.0, (duration_ms / 1000.0) / 2.0) if duration_ms > 0 else 0.0
            if _make_video_thumbnail(str(fpath), str(d / thumb_name), at_sec=at_sec):
                thumb_url = f"/static/persona_albums/{safe}/{thumb_name}"
        else:
            meta = _probe_image(str(fpath)) or {}
            width = int(meta.get("width") or 0)
            height = int(meta.get("height") or 0)
            # 照片缩略图（实施90）：网格此前直载原图＝窄壳灰块一片；与视频封面
            # 同款落 <name>.thumb.webp，前端 thumb_url 优先。
            thumb_name = f"{name}.thumb.webp"
            if _make_photo_thumbnail(str(fpath), str(d / thumb_name)):
                thumb_url = f"/static/persona_albums/{safe}/{thumb_name}"
        try:
            weight = int(form.get("weight") or 1)
        except Exception:
            weight = 1
        try:
            min_bond = int(form.get("min_bond_level") or 0)
        except Exception:
            min_bond = 0
        enabled = str(form.get("enabled", "1")).strip().lower() not in ("0", "false", "no", "")
        try:
            actor = str(request.session.get("username") or "")
        except Exception:
            actor = ""
        aicfg = _album_ai_cfg()
        want_tag = bool(aicfg.get("enabled")) and bool(aicfg.get("auto_on_upload"))
        # #238（D-N3）：想打标但识图打不动（无端点 / 起不来 / 熔断中）→ 不排必败任务、条目留
        # untagged 让之后「AI 补标」only_missing 能捞回来，并把原因回给前端明说。
        probe = _auto_tag.vision_probe(_vision_cfg()) if want_tag else {"ready": False, "reason": ""}
        will_tag = want_tag and bool(probe.get("ready"))
        row = st.add(
            str(pid), mtype, str(fpath), url, thumb_url=thumb_url,
            triggers=_as_str_list(form.get("triggers")),
            caption=str(form.get("caption") or "").strip(),
            tags=_as_str_list(form.get("tags")),
            weight=weight, enabled=enabled, min_bond_level=min_bond,
            bytes_=len(data), width=width, height=height,
            duration_ms=duration_ms, sha256=sha, created_by=actor,
            phash=phash, tag_status=(_auto_tag.TAG_PENDING if will_tag else ""))
        mid = str(row.get("id") or "")
        # EXIF 结论级提示（月份/国别/季节——绝无原始坐标）先落 auto_meta，
        # 打标任务读它做旁证；未开打标也留着（人工排查/以后补标可用）。
        if exif_hints and any(v for v in exif_hints.values()):
            got = st.set_auto_tag(mid, auto_meta={"exif": exif_hints})
            row = got or row
        if will_tag:
            _auto_tag.schedule_tag(st, mid, _vision_cfg(),
                                   face_ref=_face_ref_for_tagging(pid, aicfg))
        logger.info("[pmedia] 上传 pid=%s type=%s id=%s bytes=%d dur=%dms "
                    "phash=%s neardup=%s autotag=%s vision=%s",
                    pid, mtype, mid, len(data), duration_ms,
                    "y" if phash else "n",
                    (near_dup or {}).get("id", "-"), will_tag,
                    "ready" if probe.get("ready") else (probe.get("reason") or ("off" if not want_tag else "-")))
        _audit(request, "pmedia_upload", f"pid={pid} id={mid}",
               f"type={mtype} bytes={len(data)}")
        out = {"ok": True, "item": row, "autotag": will_tag,
               "vision_ready": bool(probe.get("ready")) if want_tag else None,
               "vision_reason": str(probe.get("reason") or "") if want_tag else ""}
        if near_dup:
            out["near_dup"] = near_dup
        return out

    @app.patch("/api/personas/{pid}/media/{mid}")
    async def update_persona_media(pid: str, mid: str, request: Request, _=Depends(auth_dep)):
        """改条目元数据（触发词/配文/多语配文/标签/启停/权重/关系闸门）。"""
        _require_write(request)
        st = _require_store(request)
        _owned_row(request, st, pid, mid)
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(400, tr(request, "err.pmedia.bad_body"))
        fields: dict = {}
        if "triggers" in body:
            fields["triggers"] = _as_str_list(body.get("triggers"))
        if "tags" in body:
            fields["tags"] = _as_str_list(body.get("tags"))
        if "caption" in body:
            fields["caption"] = str(body.get("caption") or "")
        if isinstance(body.get("caption_i18n"), dict):
            fields["caption_i18n"] = {
                str(k): str(v) for k, v in body["caption_i18n"].items()}
        if "enabled" in body:
            fields["enabled"] = bool(body.get("enabled"))
        if "weight" in body:
            try:
                fields["weight"] = int(body.get("weight") or 1)
            except Exception:
                pass
        if "min_bond_level" in body:
            try:
                fields["min_bond_level"] = int(body.get("min_bond_level") or 0)
            except Exception:
                pass
        item = st.update(str(mid), **fields)
        _audit(request, "pmedia_update", f"pid={pid} id={mid}",
               ",".join(sorted(fields.keys())))
        return {"ok": True, "item": item}

    @app.delete("/api/personas/{pid}/media/{mid}")
    async def delete_persona_media(pid: str, mid: str, request: Request, _=Depends(auth_dep)):
        """删条目（DB 行 + 磁盘文件；仅删相册根目录内的文件，防误删）。"""
        _require_write(request)
        st = _require_store(request)
        row = _owned_row(request, st, pid, mid)
        st.delete(str(mid))
        # 迁移期文件可能在新根或旧树（#67-①），两个根内都算合法删除面。
        roots = {_album_root().resolve(), _ALBUM_ROOT_LEGACY.resolve()}
        for cand in (str(row.get("file_path") or ""),
                     str(row.get("file_path") or "") + ".thumb.jpg"):
            if not cand:
                continue
            try:
                fp = Path(cand).resolve()
                if fp.is_file() and any(r in fp.parents for r in roots):
                    fp.unlink()
            except Exception:
                logger.debug("[pmedia] 删除文件失败（已忽略）", exc_info=True)
        _audit(request, "pmedia_delete", f"pid={pid} id={mid}",
               str(row.get("media_type") or ""))
        return {"ok": True}

    @app.post("/api/personas/{pid}/media/test")
    async def test_persona_media_trigger(pid: str, request: Request, _=Depends(auth_dep)):
        """「试触发」：输入一句话，返回会命中的池（keyword/generic/none）+ 全部候选（不随机）。

        实施90：带上与真发同口径的时段/季节/地点上下文——每个候选回 ``blocked_by``
        （会话级冷却/防复读门与具体客户绑定，预览不判）。
        """
        st = _require_store(request)
        body = await request.json()
        text = str((body or {}).get("text") or "")
        from src.ai.companion_selfie import detect_selfie_request
        from src.companion.persona_media import explain_match
        # 与真实链路同口径：通用池仅在「泛化要照片/自拍」请求时才作候选。
        generic_ok = bool(detect_selfie_request(text))
        rows = st.list(str(pid), enabled_only=True)
        cc: dict = {}
        geo = {"season": "", "country": ""}
        try:
            cfg = getattr(config_manager, "config", None) or {}
            scfg = ((cfg.get("companion") or {}).get("selfie") or {})
            from src.inbox.image_autosend import resolve_consistency_cfg
            cc = resolve_consistency_cfg(scfg if isinstance(scfg, dict) else {})
            if cc.get("season_gate") or cc.get("place_gate"):
                from src.companion.media_taxonomy import persona_geo_context
                geo = persona_geo_context(str(pid))
        except Exception:
            cc = {}
        now_season = (str(geo.get("season") or "")
                      if cc.get("season_gate") else "")
        home_country = (str(geo.get("country") or "")
                        if cc.get("place_gate") else "")
        out = explain_match(
            rows, text, generic_ok=generic_ok,
            now_hour=cc.get("now_hour"),
            now_season=now_season, home_country=home_country)
        out["generic_ok"] = generic_ok
        out["context"] = {"now_hour": cc.get("now_hour"),
                          "now_season": now_season,
                          "home_country": home_country}
        return out

    # ── 实施90：AI 补标（VLM 打标 + 缩略图/pHash 资产补齐）────────────────────
    # 手动补标是运营显式动作，不受 album_ai.enabled 闸（与回填 CLI 同语义）；
    # enabled 只管「上传即自动打标」。批任务单工位互斥，进度靠列表轮询
    # tag_status/thumb_url 变化。

    @app.post("/api/personas/{pid}/media/retag-all")
    async def retag_all_persona_media(pid: str, request: Request,
                                      _=Depends(auth_dep)):
        """整册补标：缺缩略图/pHash 先补齐，未打标（或全部）条目排队打标。"""
        _require_write(request)
        st = _require_store(request)
        _require_persona(request, pid)
        try:
            body = await request.json()
        except Exception:
            body = {}
        only_missing = bool((body or {}).get("only_missing", True))
        _aicfg = _album_ai_cfg()
        res = _auto_tag.run_batch(
            st, _vision_cfg(), persona_id=str(pid),
            only_missing=only_missing,
            limit=int(_aicfg.get("max_batch") or 200),
            face_ref=_face_ref_for_tagging(pid, _aicfg),
            publish_root=str(_ALBUM_ROOT),
            url_base="/static/persona_albums")
        _audit(request, "pmedia_retag_all", f"pid={pid}",
               f"queued={res.get('queued')} assets={res.get('assets')} "
               f"only_missing={only_missing}")
        # #238：补标点击必落 backend.log（F35X38：13:31–13:44 零条补标日志，无法判断用户点过没有）
        logger.info("[pmedia] 补标 pid=%s queued=%s assets=%s only_missing=%s vision=%s",
                    pid, res.get("queued"), res.get("assets"), only_missing,
                    "ready" if res.get("vision_ready", True) else (res.get("vision_reason") or "-"))
        return res

    @app.post("/api/personas/{pid}/media/{mid}/retag")
    async def retag_persona_media(pid: str, mid: str, request: Request,
                                  _=Depends(auth_dep)):
        """单条重新打标（换图/纠错后用；强制重打不看 only_missing）。"""
        _require_write(request)
        st = _require_store(request)
        _owned_row(request, st, pid, mid)
        st.set_auto_tag(str(mid), tag_status=_auto_tag.TAG_PENDING)
        ok = _auto_tag.schedule_tag(
            st, str(mid), _vision_cfg(),
            face_ref=_face_ref_for_tagging(pid, _album_ai_cfg()))
        _audit(request, "pmedia_retag", f"pid={pid} id={mid}")
        return {"ok": bool(ok), "item": st.get(str(mid))}

    # ── B119（实施74）：发图全局闸自查面 ─────────────────────────────────────
    # 事故（0826 _580/_581）：人设级相册开着、全局 companion.selfie.enabled 关着
    # → AI 推脱不发图；全局闸只在 config，人设页无状态显示无入口，用户无法自查。

    @app.get("/api/personas/selfie-gate")
    async def get_selfie_gate(request: Request, _=Depends(auth_dep)):
        """全局发图总闸（companion.selfie.enabled）状态——相册面板自查用。"""
        cfg = {}
        try:
            cfg = getattr(config_manager, "config", None) or {}
        except Exception:
            cfg = {}
        enabled = bool(((cfg.get("companion") or {}).get("selfie") or {})
                       .get("enabled", False))
        writable = bool(config_manager is not None
                        and hasattr(config_manager, "set_overlay_flag"))
        return {"ok": True, "enabled": enabled, "writable": writable}

    @app.post("/api/personas/selfie-gate")
    async def set_selfie_gate(request: Request, _=Depends(auth_dep)):
        """一键开/关全局发图总闸（写 overlay + 审计，与能力开关同语义）。"""
        _require_write(request)
        if config_manager is None or not hasattr(config_manager, "set_overlay_flag"):
            raise HTTPException(503, tr(request, "err.pmedia.gate_not_writable"))
        try:
            body = await request.json()
        except Exception:
            body = {}
        value = bool((body or {}).get("value"))
        ok, msg = config_manager.set_overlay_flag(
            "companion.selfie.enabled", value)
        if not ok:
            raise HTTPException(500, str(msg or "overlay write failed"))
        _audit(request, "pmedia_selfie_gate", f"value={value}",
               "global companion.selfie.enabled via album panel (B119)")
        return {"ok": True, "enabled": value}

    # ── face_ref：PuLID 锁脸基准照管理（生成链 reference_image() 按约定名读取）──────

    def _face_album_dir(pid: str) -> Path:
        """锁脸相册目录：companion.selfie.provider.album_dir/<pid>（与 SelfieProvider 同口径）。

        相对路径以**仓库根**（config 目录的上一级）为基准——主程序 cwd=仓库根，
        Web 线程与其一致；config_manager 缺失时回落 cwd。
        """
        rel = "assets/persona_media"
        root = Path.cwd()
        try:
            if config_manager is not None:
                cfg = getattr(config_manager, "config", None) or {}
                rel = str((((cfg.get("companion") or {}).get("selfie") or {})
                           .get("provider") or {}).get("album_dir") or rel)
                cp = getattr(config_manager, "config_path", None)
                if cp:
                    root = Path(cp).resolve().parent.parent
        except Exception:
            logger.debug("[pmedia] album_dir 解析失败，用默认", exc_info=True)
        base = Path(rel)
        if not base.is_absolute():
            base = root / base
        return base / _safe_pid(pid)

    def _find_face_ref(pid: str) -> Path | None:
        d = _face_album_dir(pid)
        try:
            if d.is_dir():
                for p in sorted(d.iterdir()):
                    if p.is_file() and p.stem.lower() == "face_ref" \
                            and p.suffix.lower() in _IMAGE_EXT:
                        return p
        except Exception:
            pass
        return None

    @app.get("/api/personas/face-refs")
    async def list_face_refs(_=Depends(auth_dep)):
        """批量拉取全部人设的锁脸头像状态（人设卡片一次拉全量，替代逐卡请求的 N+1）。"""
        refs: dict = {}
        try:
            from src.utils.persona_manager import PersonaManager
            pm = PersonaManager.get_instance()
            for pid in pm.list_profile_ids():
                p = _find_face_ref(pid)
                if p is None:
                    continue
                try:
                    mtime = p.stat().st_mtime
                except Exception:
                    mtime = 0
                refs[pid] = {
                    "url": f"/api/personas/{pid}/face-ref/image?v={int(mtime)}",
                    "mtime": mtime,
                }
        except Exception:
            logger.debug("[pmedia] face-refs 批量状态拉取失败（已忽略）", exc_info=True)
            return {"ok": True, "refs": {}}
        return {"ok": True, "refs": refs}

    @app.get("/api/personas/{pid}/face-ref")
    async def get_face_ref(pid: str, request: Request, _=Depends(auth_dep)):
        """锁脸基准照状态：是否存在 + 预览端点 + 修改时间。"""
        _require_persona(request, pid)
        p = _find_face_ref(pid)
        if p is None:
            return {"exists": False}
        try:
            mtime = p.stat().st_mtime
        except Exception:
            mtime = 0
        return {"exists": True, "filename": p.name, "mtime": mtime,
                "image_url": f"/api/personas/{pid}/face-ref/image?v={int(mtime)}"}

    @app.get("/api/personas/{pid}/face-ref/image")
    async def get_face_ref_image(pid: str, request: Request, _=Depends(auth_dep)):
        """直出基准照字节（相册目录不在 /static 下，经本端点预览）。"""
        _require_persona(request, pid)
        p = _find_face_ref(pid)
        if p is None:
            raise HTTPException(404, tr(request, "err.pmedia.not_found"))
        from fastapi.responses import FileResponse
        return FileResponse(str(p))

    @app.post("/api/personas/{pid}/face-ref")
    async def upload_face_ref(pid: str, request: Request, _=Depends(auth_dep)):
        """上传/替换锁脸基准照（multipart: file）。建议用清晰正脸真人照；替换后
        所有后续生成（自拍/生活照）立即换锁这张脸，无需重启。"""
        _require_write(request)
        _require_persona(request, pid)
        form = await request.form()
        upload = form.get("file")
        if upload is None or not getattr(upload, "filename", ""):
            raise HTTPException(400, tr(request, "err.pmedia.file_required"))
        ext = os.path.splitext(str(upload.filename))[1].lower()
        if ext not in _IMAGE_EXT or ext == ".gif":
            raise HTTPException(400, tr(request, "err.pmedia.ext_not_allowed", ext=ext or "?"))
        data = await upload.read()
        if not data:
            raise HTTPException(400, tr(request, "err.inbox.empty_file"))
        if len(data) > _MAX_PHOTO_BYTES:
            raise HTTPException(413, tr(
                request, "err.pmedia.too_large", mb=_MAX_PHOTO_BYTES // (1024 * 1024)))
        d = _face_album_dir(pid)
        try:
            d.mkdir(parents=True, exist_ok=True)
            # 清掉旧 face_ref.*（不同扩展名并存会让 reference_image 选择歧义）
            old = _find_face_ref(pid)
            while old is not None:
                old.unlink()
                old = _find_face_ref(pid)
            fpath = d / f"face_ref{ext}"
            fpath.write_bytes(data)
        except Exception as ex:  # noqa: BLE001
            logger.warning("[pmedia] face_ref 保存失败: %s", ex, exc_info=True)
            raise HTTPException(500, tr(request, "err.pmedia.save_failed", err=str(ex)[:200]))
        _audit(request, "pmedia_face_ref_set", f"pid={pid}", f"bytes={len(data)}")
        logger.info("[pmedia] face_ref 已更新 pid=%s bytes=%d", pid, len(data))
        try:
            mtime = fpath.stat().st_mtime
        except Exception:
            mtime = 0
        return {"ok": True, "filename": fpath.name,
                "image_url": f"/api/personas/{pid}/face-ref/image?v={int(mtime)}"}

    @app.delete("/api/personas/{pid}/face-ref")
    async def delete_face_ref(pid: str, request: Request, _=Depends(auth_dep)):
        """删除锁脸基准照（生成链回落相册第一张为参考；相册也空则纯文生图）。"""
        _require_write(request)
        _require_persona(request, pid)
        p = _find_face_ref(pid)
        if p is None:
            return {"ok": True, "existed": False}
        try:
            p.unlink()
        except Exception as ex:  # noqa: BLE001
            raise HTTPException(500, tr(request, "err.pmedia.save_failed", err=str(ex)[:200]))
        _audit(request, "pmedia_face_ref_del", f"pid={pid}")
        return {"ok": True, "existed": True}
