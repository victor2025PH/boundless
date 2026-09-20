"""每人设「相册/媒体」后台 API —— 上传/列表/改/删/试触发。

挂 ``/api/personas/{pid}/media*``。文件落 ``src/web/static/persona_albums/<pid>/``（经 /static
直服，供网格缩略图与前端预览），元数据（触发词/配文/权重/关系闸门/命中）落 DB
（``persona_media_store``）。回复链（image_autosend / skill_manager Stage 0）读同一份 store。

护栏：扩展名白名单（图 jpg/png/webp/gif + HEIC/HEIF 后端可转时、视频 mp4/mov/webm/m4v）、
体积上限（默认图 25MB / 视频 100MB，``inbox.persona_media.limits`` 可配）、视频时长上限
（默认 5 分钟，仅当 ffprobe 可探时才拦；软失败不阻塞）、sha256 去重、persona_id 目录消毒防穿越、
写操作 viewer 只读拦截。文案经 ``tr`` 收口零 CJK。

上传口两种请求体（Q-40 A，2026-09-15，#316 追加——与聊天 send-media 同款）：
- **裸流**（新前端）：``Content-Type`` 非 multipart，body 就是文件本体，元数据走 query
  （``name / triggers / caption / tags / weight / min_bond_level / enabled / client_msg_id``）。
  边收边落盘（``src/web/media_stream.py`` 与 send-media 共用），``Content-Length`` 一到手就按上限
  413（带实际大小），不用等文件传完；超限吞流让浏览器读得到状态码。
- **multipart**（旧前端 / 脚本 / 测试）：Starlette 先卷进临时文件；保留兼容。
每条退出路径落一行 ``[pmedia] … reason= size= ms=``。

视频上传附带元数据探测（ffprobe 拿时长/宽高）+ 抽帧生成封面缩略图（ffmpeg，落 ``*.thumb.jpg``）；
图片探宽高（PIL）。以上全部软失败——缺 ffmpeg/ffprobe/PIL 只是拿不到该项元数据，不影响上传落库。
"""

import hashlib
import io
import json
import logging
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, List

from fastapi import Depends, HTTPException, Request
from fastapi.responses import JSONResponse

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
#: iPhone 相册原生格式——前端 canvas 先转 JPEG；转不动（非 Chromium / 老壳）才到后端，
#: 后端有 pillow-heif 就解码转 JPEG 落库（相册里只存 JPEG 一份），没有才 ext_not_allowed。
_HEIC_EXT = {".heic", ".heif"}
_MB = 1024 * 1024
# Q-40 B：默认上限图 25MB / 视频 100MB / 5 分钟（此前 10 / 50 / 3min）。运营可用
# ``inbox.persona_media.limits: {photo_mb, video_mb, video_max_sec}`` 覆写，夹紧 1–2048MB。
# 模块常量仍是缺省来源（测试 monkeypatch 契约不变）。
_MAX_PHOTO_BYTES = 25 * _MB
_MAX_VIDEO_BYTES = 100 * _MB
_MAX_VIDEO_DURATION_MS = 5 * 60 * 1000  # 视频时长上限（仅 ffprobe 可探时才拦）
_LIMIT_MIN_MB = 1
_LIMIT_MAX_MB = 2048
_LIMIT_MAX_SEC = 2 * 60 * 60
_ROLE_VIEWER = "viewer"


def _clamp_int(v: Any, lo: int, hi: int) -> "int | None":
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None
    return max(lo, min(hi, n))


def resolve_persona_media_limits(config: Any) -> dict:
    """相册上限（字节 / 毫秒）——``inbox.persona_media.limits`` 覆写，缺省回模块常量。绝不抛。

    返回 ``{"photo_bytes", "video_bytes", "video_ms", "photo_mb", "video_mb", "video_max_sec"}``。
    """
    try:
        lim = (((config or {}).get("inbox") or {}).get("persona_media") or {}).get("limits") or {}
    except Exception:
        lim = {}
    if not isinstance(lim, dict):
        lim = {}
    photo_mb = _clamp_int(lim.get("photo_mb"), _LIMIT_MIN_MB, _LIMIT_MAX_MB)
    video_mb = _clamp_int(lim.get("video_mb"), _LIMIT_MIN_MB, _LIMIT_MAX_MB)
    video_sec = _clamp_int(lim.get("video_max_sec"), 1, _LIMIT_MAX_SEC)
    photo_bytes = photo_mb * _MB if photo_mb is not None else int(_MAX_PHOTO_BYTES)
    video_bytes = video_mb * _MB if video_mb is not None else int(_MAX_VIDEO_BYTES)
    video_ms = video_sec * 1000 if video_sec is not None else int(_MAX_VIDEO_DURATION_MS)
    return {
        "photo_bytes": photo_bytes, "video_bytes": video_bytes, "video_ms": video_ms,
        "photo_mb": photo_bytes // _MB, "video_mb": video_bytes // _MB,
        "video_max_sec": video_ms // 1000,
    }


def _heif_available() -> bool:
    """后端 HEIC 兜底是否可用（``pillow-heif`` 可选依赖，import 失败即不可用）。"""
    try:
        import pillow_heif  # type: ignore  # noqa: F401
        from PIL import Image  # type: ignore  # noqa: F401
        return True
    except Exception:
        return False


def _heic_to_jpeg(data: bytes, quality: int = 90) -> bytes:
    """HEIC/HEIF 字节 → JPEG 字节（方向位已应用，EXIF 不带出）。失败回 ``b""``。"""
    try:
        import pillow_heif  # type: ignore
        from PIL import Image, ImageOps  # type: ignore
        try:
            pillow_heif.register_heif_opener()
        except Exception:
            pass
        with Image.open(io.BytesIO(data)) as im:
            im = ImageOps.exif_transpose(im)
            if im.mode not in ("RGB", "L"):
                im = im.convert("RGB")
            out = io.BytesIO()
            im.save(out, format="JPEG", quality=int(quality), optimize=True)
            return out.getvalue()
    except Exception:
        logger.debug("[pmedia] HEIC 解码失败", exc_info=True)
        return b""


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            c = fh.read(_MB)
            if not c:
                break
            h.update(c)
    return h.hexdigest()


def _unlink_quiet(p: "Path | str | None") -> None:
    if not p:
        return
    try:
        Path(p).unlink()
    except FileNotFoundError:
        pass
    except Exception:
        logger.debug("[pmedia] 临时文件未能删除 path=%s", p, exc_info=True)


class MediaReject(HTTPException):
    """相册写操作的**结构化拒绝**（Q-35 #316 4HK54G，2026-09-12）。

    此前每个拒绝分支 ``raise HTTPException(4xx, 文案)`` → 前端只拿到 ``{detail}``，
    ``pmaUpload`` 逐文件只 ``fail++``，报障时「几张失败、为什么」两边都答不上。现在每个
    拒绝分支带一个**机器可读** ``reason``（``too_large`` / ``ext_not_allowed`` / ``bad_content`` /
    ``too_long`` / ``empty_file`` / ``file_required`` / ``save_failed`` / ``readonly`` /
    ``store_unavailable`` / ``persona_not_found`` / ``not_found``），经 ``_media_reject_handler``
    回 ``{ok:false, reason, detail, ...extra}``——``detail`` 键**保留**（旧前端 ``r.json().detail``
    仍可读），``extra`` 放限值等数字（``limit_mb`` / ``ext`` / ``max_sec``）供前端拼人话。
    """

    def __init__(self, status_code: int, reason: str, detail: str,
                 *, extra: "dict | None" = None):
        super().__init__(status_code=int(status_code), detail=str(detail or ""))
        self.reason = str(reason or "rejected")
        self.extra = dict(extra or {})

    def body(self) -> dict:
        out = {"ok": False, "reason": self.reason, "detail": self.detail}
        for k, v in self.extra.items():
            out.setdefault(str(k), v)
        return out


async def _media_reject_handler(request: Request, exc: MediaReject) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content=exc.body())


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

    # Q-35 #316：结构化拒绝出口（{ok:false, reason, detail}）。子类 handler 按 MRO 先于
    # 默认 HTTPException handler 命中；其他路由的裸 HTTPException 语义不变。
    try:
        app.add_exception_handler(MediaReject, _media_reject_handler)
    except Exception:
        logger.debug("[pmedia] MediaReject handler 挂载失败（回落 {detail} 形状）", exc_info=True)

    def _reject(request: Request, status: int, reason: str, key: str, *,
                pid: str = "", name: str = "", size: int = -1,
                extra: "dict | None" = None, t0: float = 0.0, mode: str = "",
                **fmt: Any) -> MediaReject:
        """造一个结构化拒绝并**落一行日志**——4HK54G 事故里被拒的上传在服务端零痕迹
        （raise 发生在成功日志之前），排障只能猜。日志只带文件名 / 体积 / reason / 耗时，不带正文。"""
        detail = tr(request, key, **fmt)
        try:
            logger.info("[pmedia] 上传拒绝 pid=%s name=%s status=%d reason=%s size=%s ms=%d mode=%s",
                        pid or "-", (str(name or "-").replace("\n", " ")[:80]),
                        int(status), reason, (size if size >= 0 else "-"),
                        int((time.perf_counter() - t0) * 1000) if t0 else 0, mode or "-")
        except Exception:
            pass
        return MediaReject(status, reason, detail, extra=extra)

    def _cfg() -> dict:
        try:
            return getattr(config_manager, "config", None) or {}
        except Exception:
            return {}

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
            raise MediaReject(503, "store_unavailable",
                              tr(request, "err.pmedia.store_unavailable"))
        return st

    def _require_write(request: Request):
        try:
            role = request.session.get("role", "")
        except Exception:
            role = ""
        if role == _ROLE_VIEWER:
            raise MediaReject(403, "readonly", tr(request, "err.persona.readonly_no_edit"))

    def _require_persona(request: Request, pid: str):
        from src.utils.persona_manager import PersonaManager
        p = PersonaManager.get_instance().get_persona_by_id(str(pid))
        if p is None:
            raise MediaReject(404, "persona_not_found",
                              tr(request, "err.pmedia.persona_not_found", name=pid),
                              extra={"pid": str(pid)})
        return p

    def _owned_row(request: Request, st, pid: str, mid: str):
        row = st.get(str(mid))
        if row is None or str(row.get("persona_id")) != str(pid):
            raise MediaReject(404, "not_found", tr(request, "err.pmedia.not_found"))
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
        ai["apply"] = str(_aicfg.get("apply") or "auto")
        from src.companion.persona_media import album_trigger_counts
        counts = album_trigger_counts(items)
        return {"items": items, "stats": st.stats(str(pid)), "ai": ai, "counts": counts}

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

    @app.get("/api/personas/media-caps")
    async def persona_media_caps(request: Request, _=Depends(auth_dep)):
        """相册上传能力 / 上限（Q-40）：前端选文件即按同一数字预检、结果条写上限。

        ``album_stream_upload=True`` ＝ 本后端接受裸流分支（旧后端无此端点 → 前端仍 multipart，
        模板热更先于后端重启的中间态自洽）。``heic_backend`` ＝ 后端能兜底 HEIC 转 JPEG。
        """
        lim = resolve_persona_media_limits(_cfg())
        return {
            "ok": True, "album_stream_upload": True,
            "photo_max_mb": lim["photo_mb"], "video_max_mb": lim["video_mb"],
            "video_max_sec": lim["video_max_sec"],
            "image_exts": sorted(_IMAGE_EXT), "video_exts": sorted(_VIDEO_EXT),
            "heic_backend": bool(_heif_available()),
        }

    @app.post("/api/personas/{pid}/media")
    async def upload_persona_media(pid: str, request: Request, _=Depends(auth_dep)):
        """上传一张图/一段视频到该人设相册。

        裸流：``Content-Type`` 非 multipart → body 即文件，元数据走 query（``name`` 必填）。
        multipart：``file`` + 可选 ``triggers/caption/tags/weight/min_bond_level/enabled``（旧路保留）。
        """
        from src.web.media_stream import (
            DRAIN_MAX, StreamRecvError, declared_length, drain_stream, read_head,
            stream_to_file, upload_file_chunks,
        )
        t0 = time.perf_counter()
        _ctype = str(request.headers.get("content-type") or "").lower()
        _multipart = _ctype.startswith("multipart/")
        mode = "multipart" if _multipart else "stream"
        declared = 0 if _multipart else declared_length(request)

        async def _drain_req(chunks: Any) -> int:
            # 裸流被拒时把 body 读掉再回状态码——服务端一响应就关连接，浏览器只见 onerror。
            if _multipart:
                return 0
            return await drain_stream(chunks, min(declared or DRAIN_MAX, DRAIN_MAX))

        try:
            _require_write(request)
            st = _require_store(request)
            _require_persona(request, pid)
        except MediaReject:
            await _drain_req(request.stream())
            raise
        if _multipart:
            form = await request.form()
            upload = form.get("file")
            if upload is None or not getattr(upload, "filename", ""):
                raise _reject(request, 400, "file_required", "err.pmedia.file_required",
                              pid=pid, t0=t0, mode=mode)
            fname = str(upload.filename or "")
            fields: Any = form
            chunks: Any = upload_file_chunks(upload)
        else:
            fields = request.query_params
            fname = str(fields.get("name") or "")
            chunks = request.stream()
            if not fname:
                await _drain_req(chunks)
                raise _reject(request, 400, "file_required", "err.pmedia.file_required",
                              pid=pid, t0=t0, mode=mode)
        ext = os.path.splitext(fname)[1].lower()
        is_heic = ext in _HEIC_EXT
        mtype = _media_type_for_ext(ext)
        if not mtype and is_heic and _heif_available():
            mtype = "photo"   # 后端兜底：落库前转 JPEG（相册只存 JPEG 一份）
        if not mtype:
            # 手机相册常见：iPhone HEIC / 截图 .PNG 之外的 .heif / .avif / .mkv——
            # 此前 400 无日志无原因，「后端只见两张成功之后无上传行」正是这种形状。
            # HEIC 且前后端都转不动 → 文案明说「请在手机相册导出为 JPEG」。
            await _drain_req(chunks)
            _key = "err.pmedia.heic_export_jpeg" if is_heic else "err.pmedia.ext_not_allowed"
            raise _reject(request, 400, "ext_not_allowed", _key,
                          pid=pid, name=fname, size=declared if declared else -1, t0=t0, mode=mode,
                          ext=ext or "?",
                          extra={"ext": ext or "", "allowed": sorted(_IMAGE_EXT | _VIDEO_EXT),
                                 "hint": "export_jpeg" if is_heic else ""})
        lim = resolve_persona_media_limits(_cfg())
        limit = int(lim["video_bytes"] if mtype == "video" else lim["photo_bytes"])
        limit_mb = limit // _MB
        if declared and declared > limit:
            # 裸流：Content-Length 就是文件大小，传完之前就能给出精确的 413
            await _drain_req(chunks)
            raise _reject(request, 413, "too_large", "err.pmedia.too_large",
                          pid=pid, name=fname, size=declared, t0=t0, mode=mode, mb=limit_mb,
                          extra={"limit_mb": limit_mb, "bytes": declared, "media_type": mtype,
                                 "over_mb": round(max(0, declared - limit) / _MB, 1)})
        _it = chunks.__aiter__()
        head = await read_head(_it)
        if not head:
            raise _reject(request, 400, "empty_file", "err.inbox.empty_file",
                          pid=pid, name=fname, size=0, t0=t0, mode=mode)
        safe = _safe_pid(pid)
        d = _album_root() / safe
        from starlette.concurrency import run_in_threadpool
        try:
            await run_in_threadpool(d.mkdir, parents=True, exist_ok=True)
        except Exception as ex:  # noqa: BLE001
            await _drain_req(_it)
            raise _reject(request, 500, "save_failed", "err.pmedia.save_failed",
                          pid=pid, name=fname, size=len(head), t0=t0, mode=mode, err=str(ex)[:200],
                          extra={"err": str(ex)[:200]})
        # 流式落盘到临时 .part（与 send-media 同一段代码）；超限即停、吞流、413。
        part = d / f".up_{uuid.uuid4().hex}.part"
        try:
            size, overflow = await stream_to_file(_it, str(part), head=head, cap_bytes=limit)
        except StreamRecvError as sex:
            _unlink_quiet(part)
            raise _reject(request, 502, "recv_error", "err.pmedia.save_failed",
                          pid=pid, name=fname, size=sex.received, t0=t0, mode=mode,
                          err=f"{type(sex.cause).__name__}: {str(sex.cause)[:120]}",
                          extra={"err": type(sex.cause).__name__})
        if overflow:
            _unlink_quiet(part)
            drained = await drain_stream(_it, DRAIN_MAX)
            known = declared if (mode == "stream" and declared) else size
            raise _reject(request, 413, "too_large", "err.pmedia.too_large",
                          pid=pid, name=fname, size=known, t0=t0, mode=mode, mb=limit_mb,
                          extra={"limit_mb": limit_mb, "bytes": known, "media_type": mtype,
                                 "over_mb": round(max(0, known - limit) / _MB, 1),
                                 "drained": drained})
        # ── 内容 / 去重 / 入库 ───────────────────────────────────────────────
        # #67-②（0830 两机实锤）+ 媒体产物验证纪律：magic bytes 不过=当场 400（诚实拒收），
        # 绝不静默存坏。照片整读（≤25MB）走原预处理链；视频不进内存，按头判、按文件算 sha。
        data = b""
        heic_converted = False
        exif_hints: dict = {}
        phash = ""
        near_dup = None
        try:
            if mtype == "photo":
                data = await run_in_threadpool(part.read_bytes)
                _unlink_quiet(part)
                if is_heic:
                    conv = await run_in_threadpool(_heic_to_jpeg, data)
                    if not conv:
                        raise _reject(request, 400, "bad_content", "err.pmedia.bad_content",
                                      pid=pid, name=fname, size=size, t0=t0, mode=mode,
                                      why="heic decode failed", extra={"why": "heic decode failed"})
                    data = conv
                    ext = ".jpg"
                    heic_converted = True
                _bad = _sniff_media(data, ext)
                if _bad:
                    raise _reject(request, 400, "bad_content", "err.pmedia.bad_content",
                                  pid=pid, name=fname, size=size, t0=t0, mode=mode, why=_bad,
                                  extra={"why": str(_bad)})
                sha = hashlib.sha256(data).hexdigest()
                dup = st.find_by_sha(str(pid), sha)
                if dup is not None:
                    logger.info("[pmedia] 上传 pid=%s name=%s reason=deduped size=%d ms=%d mode=%s",
                                pid, fname.replace("\n", " ")[:80], size,
                                int((time.perf_counter() - t0) * 1000), mode)
                    return {"ok": True, "item": dup, "deduped": True}
                # 实施90 照片入库预处理（全软失败）：EXIF 抽提示→剥离（隐私）+ pHash
                # 近重复预警（sha 只能抓字节级重传；重编码/缩放的"同一张"靠指纹）。
                processed = _process_photo(data, ext)
                data = processed.get("bytes") or data
                exif_hints = processed.get("hints") or {}
                phash = _phash_bytes(data)
                if phash:
                    nid, ndist = _phash_nearest(phash, st.phashes(str(pid)))
                    if nid and ndist <= _NEAR_DUP_MAX:
                        near_dup = {"id": nid, "distance": ndist}
                name = f"{uuid.uuid4().hex}{ext}"
                fpath = (d / name).resolve()
                await run_in_threadpool(fpath.write_bytes, data)
                size = len(data)
                verify_head = bytes(data[:16])
            else:
                _bad = _sniff_media(head, ext)
                if _bad:
                    _unlink_quiet(part)
                    raise _reject(request, 400, "bad_content", "err.pmedia.bad_content",
                                  pid=pid, name=fname, size=size, t0=t0, mode=mode, why=_bad,
                                  extra={"why": str(_bad)})
                sha = await run_in_threadpool(_sha256_file, str(part))
                dup = st.find_by_sha(str(pid), sha)
                if dup is not None:
                    _unlink_quiet(part)
                    logger.info("[pmedia] 上传 pid=%s name=%s reason=deduped size=%d ms=%d mode=%s",
                                pid, fname.replace("\n", " ")[:80], size,
                                int((time.perf_counter() - t0) * 1000), mode)
                    return {"ok": True, "item": dup, "deduped": True}
                name = f"{uuid.uuid4().hex}{ext}"
                fpath = (d / name).resolve()
                await run_in_threadpool(os.replace, str(part), str(fpath))
                verify_head = bytes(head[:16])
            # 落盘后回读验证（纪律：写成功≠内容对）：首段字节与内存不一致
            # =写入层损坏，当场删除报错，绝不让坏文件带着「上传成功」活下来。
            with open(fpath, "rb") as _fh:
                _head = _fh.read(16)
            if _head != verify_head:
                _unlink_quiet(fpath)
                raise _reject(request, 500, "save_failed", "err.pmedia.save_failed",
                              pid=pid, name=fname, size=size, t0=t0, mode=mode,
                              err="write verify failed", extra={"err": "write verify failed"})
        except HTTPException:
            _unlink_quiet(part)
            raise
        except Exception as ex:  # noqa: BLE001
            _unlink_quiet(part)
            logger.warning("[pmedia] 保存文件失败: %s", ex, exc_info=True)
            raise _reject(request, 500, "save_failed", "err.pmedia.save_failed",
                          pid=pid, name=fname, size=size, t0=t0, mode=mode, err=str(ex)[:200],
                          extra={"err": str(ex)[:200]})
        url = f"/static/persona_albums/{safe}/{name}"
        # 元数据探测 + 视频护栏/封面（全部软失败，缺 ffmpeg/ffprobe/PIL 不阻塞上传）。
        width = height = duration_ms = 0
        thumb_url = ""
        if mtype == "video":
            meta = await run_in_threadpool(_probe_video, str(fpath)) or {}
            width = int(meta.get("width") or 0)
            height = int(meta.get("height") or 0)
            duration_ms = int(meta.get("duration_ms") or 0)
            max_ms = int(lim["video_ms"])
            if 0 < max_ms < duration_ms:
                _unlink_quiet(fpath)
                raise _reject(request, 413, "too_long", "err.pmedia.too_long",
                              pid=pid, name=fname, size=size, t0=t0, mode=mode,
                              sec=max_ms // 1000,
                              extra={"max_sec": max_ms // 1000, "duration_ms": duration_ms})
            thumb_name = f"{name}.thumb.jpg"
            at_sec = min(1.0, (duration_ms / 1000.0) / 2.0) if duration_ms > 0 else 0.0
            if await run_in_threadpool(_make_video_thumbnail, str(fpath), str(d / thumb_name),
                                       at_sec=at_sec):
                thumb_url = f"/static/persona_albums/{safe}/{thumb_name}"
        else:
            meta = _probe_image(str(fpath)) or {}
            width = int(meta.get("width") or 0)
            height = int(meta.get("height") or 0)
            # 照片缩略图（实施90）：网格此前直载原图＝窄壳灰块一片；与视频封面
            # 同款落 <name>.thumb.webp，前端 thumb_url 优先。
            thumb_name = f"{name}.thumb.webp"
            if await run_in_threadpool(_make_photo_thumbnail, str(fpath), str(d / thumb_name)):
                thumb_url = f"/static/persona_albums/{safe}/{thumb_name}"
        try:
            weight = int(fields.get("weight") or 1)
        except Exception:
            weight = 1
        try:
            min_bond = int(fields.get("min_bond_level") or 0)
        except Exception:
            min_bond = 0
        _en_raw = fields.get("enabled")
        enabled = str("1" if _en_raw is None else _en_raw).strip().lower() not in ("0", "false", "no", "")
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
            triggers=_as_str_list(fields.get("triggers")),
            caption=str(fields.get("caption") or "").strip(),
            tags=_as_str_list(fields.get("tags")),
            weight=weight, enabled=enabled, min_bond_level=min_bond,
            bytes_=size, width=width, height=height,
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
        logger.info("[pmedia] 上传 pid=%s type=%s id=%s name=%s reason=ok size=%d ms=%d mode=%s "
                    "declared=%d dur=%dms phash=%s neardup=%s heic=%s autotag=%s vision=%s",
                    pid, mtype, mid, fname.replace("\n", " ")[:80], size,
                    int((time.perf_counter() - t0) * 1000), mode, declared, duration_ms,
                    "y" if phash else "n",
                    (near_dup or {}).get("id", "-"), "y" if heic_converted else "n", will_tag,
                    "ready" if probe.get("ready") else (probe.get("reason") or ("off" if not want_tag else "-")))
        _audit(request, "pmedia_upload", f"pid={pid} id={mid}",
               f"type={mtype} bytes={size}")
        out = {"ok": True, "item": row, "autotag": will_tag,
               "vision_ready": bool(probe.get("ready")) if want_tag else None,
               "vision_reason": str(probe.get("reason") or "") if want_tag else ""}
        if near_dup:
            out["near_dup"] = near_dup
        if heic_converted:
            out["converted"] = "heic->jpeg"
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
        # Q-6 E：场景 kind 写入 tags kind:*（白名单字段）；识图结论同步 auto_meta.scene_kind。
        if "scene_kind" in body:
            kind = str(body.get("scene_kind") or "").strip().lower()
            if kind not in ("selfie", "indoor", "outdoor", "food", "pet", "other", ""):
                raise HTTPException(400, tr(request, "err.pmedia.bad_body"))
            row0 = st.get(str(mid)) or {}
            tags = [str(t) for t in (fields.get("tags") if "tags" in fields else (row0.get("tags") or [])) if str(t or "").strip()]
            tags = [t for t in tags if not str(t).startswith("kind:")]
            if kind:
                tags.append(f"kind:{kind}")
            fields["tags"] = tags
            try:
                am = dict(row0.get("auto_meta") or {}) if isinstance(row0.get("auto_meta"), dict) else {}
                am["scene_kind"] = kind
                st.set_auto_tag(str(mid), auto_meta=am)
            except Exception:
                logger.debug("[pmedia] scene_kind auto_meta 同步跳过", exc_info=True)
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

    @app.post("/api/personas/{pid}/media/adopt-suggest")
    async def adopt_suggest_persona_media(pid: str, request: Request,
                                          _=Depends(auth_dep)):
        """Q-6 E：一键采纳建议词 → 写入正式 triggers（已确认）。ids 空＝全部有建议的条目。"""
        _require_write(request)
        st = _require_store(request)
        _require_persona(request, pid)
        try:
            body = await request.json()
        except Exception:
            body = {}
        want = {str(x) for x in ((body or {}).get("ids") or []) if str(x or "").strip()}
        n = 0
        for row in st.list(str(pid)) or []:
            mid = str(row.get("id") or "")
            if want and mid not in want:
                continue
            trg = [str(x).strip() for x in (row.get("triggers") or []) if str(x or "").strip()]
            am = row.get("auto_meta") if isinstance(row.get("auto_meta"), dict) else {}
            sug = [str(x).strip() for x in ((am or {}).get("triggers_suggest") or []) if str(x or "").strip()]
            if not sug:
                continue
            merged = list(trg)
            for s in sug:
                if s not in merged:
                    merged.append(s)
            if merged != trg:
                st.update(mid, triggers=merged)
                n += 1
        from src.companion.persona_media import album_trigger_counts
        items = st.list(str(pid)) or []
        _audit(request, "pmedia_adopt_suggest", f"pid={pid}", f"n={n}")
        return {"ok": True, "adopted": n, "counts": album_trigger_counts(items)}

    @app.post("/api/personas/{pid}/media/batch-triggers")
    async def batch_triggers_persona_media(pid: str, request: Request,
                                           _=Depends(auth_dep)):
        """Q-6 E：多选批量加触发词。Body ``{ids, add: ["风景"]}``。"""
        _require_write(request)
        st = _require_store(request)
        _require_persona(request, pid)
        try:
            body = await request.json()
        except Exception:
            body = {}
        ids = [str(x) for x in ((body or {}).get("ids") or []) if str(x or "").strip()]
        add = _as_str_list((body or {}).get("add"))
        if not ids or not add:
            raise HTTPException(400, tr(request, "err.pmedia.bad_body"))
        n = 0
        for mid in ids:
            try:
                row = _owned_row(request, st, pid, mid)
            except HTTPException:
                continue
            trg = [str(x).strip() for x in (row.get("triggers") or []) if str(x or "").strip()]
            merged = list(trg)
            for s in add:
                if s and s not in merged:
                    merged.append(s)
            if merged != trg:
                st.update(mid, triggers=merged)
                n += 1
        _audit(request, "pmedia_batch_triggers", f"pid={pid}", f"n={n}")
        return {"ok": True, "updated": n}

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
            raise _reject(request, 400, "file_required", "err.pmedia.file_required", pid=pid)
        fname = str(upload.filename or "")
        ext = os.path.splitext(fname)[1].lower()
        if ext not in _IMAGE_EXT or ext == ".gif":
            raise _reject(request, 400, "ext_not_allowed", "err.pmedia.ext_not_allowed",
                          pid=pid, name=fname, ext=ext or "?",
                          extra={"ext": ext or "", "allowed": sorted(_IMAGE_EXT - {".gif"})})
        data = await upload.read()
        if not data:
            raise _reject(request, 400, "empty_file", "err.inbox.empty_file",
                          pid=pid, name=fname, size=0)
        if len(data) > _MAX_PHOTO_BYTES:
            raise _reject(request, 413, "too_large", "err.pmedia.too_large",
                          pid=pid, name=fname, size=len(data),
                          mb=_MAX_PHOTO_BYTES // (1024 * 1024),
                          extra={"limit_mb": _MAX_PHOTO_BYTES // (1024 * 1024),
                                 "bytes": len(data), "media_type": "photo"})
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
            raise _reject(request, 500, "save_failed", "err.pmedia.save_failed",
                          pid=pid, name=fname, size=len(data), err=str(ex)[:200],
                          extra={"err": str(ex)[:200]})
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
