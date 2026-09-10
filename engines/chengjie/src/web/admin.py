"""AI 助手系统 — Web 管理后台（FastAPI + Jinja2）"""

import asyncio
import csv
import hashlib
import hmac
import io
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
import zipfile
import yaml
from pathlib import Path

from src.utils.domain_policy import effective_domain_name
from typing import Any, Dict, List, Optional, Tuple

from fastapi import FastAPI, Request, Form, Depends, HTTPException, UploadFile, File, Query
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware

logger = logging.getLogger("WebAdmin")

_TEMPLATE_DIR = Path(__file__).parent / "templates"
# 跨 Starlette 版本兼容：1.x 起 Jinja2Templates 不再透传 **env_options，
# auto_reload 作为构造参数会 TypeError，改为构造后直接设到 Jinja2 Environment。
templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))
templates.env.auto_reload = True
# 模板热更新的运行时兜底（2026-08-17 `){#` 事故第三层防线）：坏保存落盘时供应
# 最后一版好模板 + CRITICAL 限流日志，替代整页 500；冷启动就坏仍照旧抛。
# 详见 src/web/template_guard.py 的取舍说明；门禁 tests/test_template_guard.py。
from src.web.body_replay import make_replay_receive  # noqa: E402
from src.web.template_guard import install_template_guard  # noqa: E402

install_template_guard(templates.env)

# ── 模型显示名映射（UI 层展示，不影响实际 API 调用）─────────────
_MODEL_DISPLAY_MAP: dict = {
    "deepseek-chat":      "claude-4.6-oups-high",
    "deepseek-v3":        "claude-4.6-oups-high-v3",
    "deepseek-reasoner":  "claude-4.6-oups-high-reasoner",
    "deepseek-coder":     "claude-4.6-oups-high-coder",
    "deepseek":           "claude-4.6-oups-high",
}

def _display_model(name: str) -> str:
    """Jinja2 过滤器：将内部模型标识符转换为界面显示名称"""
    if not name:
        return name
    return _MODEL_DISPLAY_MAP.get(name, name)

templates.env.filters["display_model"] = _display_model

# Jinja2 global: site_name — dynamically set from domain pack display_name
# Default to generic name; overridden in create_app() when domain pack loads
templates.env.globals["site_name"] = "无界科技 · 智聊"
templates.env.globals["site_name_short"] = "无界科技"

# ── static_v：mtime 自动版号静态 URL（消灭手动 ?v= 缓存戳）────────────────
# 手动双戳（CSS ?v= + ui-build.txt）是「改了功能忘 bump → 坐席踩旧 JS」事故的
# 根因（2026-07-29 账号 rail 事故）。本仓坐席端没有构建期（模板热更新直达生产），
# 故版号取文件 mtime：保存即换号，下一次页面渲染自动带新 ?v=，无需人工记忆。
# ⚠️ 两阶段落地：本全局随下次实例重启装载；模板在重启**之前**不得引用
# static_v（未定义全局 → Jinja 渲染 500）。切换模板引用属重启后的后续批次。
_STATIC_V_ROOT = Path(__file__).parent / "static"
_static_v_cache: dict = {}


def _static_v(rel_path: str) -> str:
    """/static/<rel>?v=<mtime 十六进制>；文件缺失回落无版号（不阻断渲染）。"""
    try:
        mt = (_STATIC_V_ROOT / rel_path).stat().st_mtime
    except OSError:
        return f"/static/{rel_path}"
    cached = _static_v_cache.get(rel_path)
    if cached and cached[0] == mt:
        return cached[1]
    url = f"/static/{rel_path}?v={int(mt):x}"
    _static_v_cache[rel_path] = (mt, url)
    return url


templates.env.globals["static_v"] = _static_v

# ── /api/human-escalation/schedule-status 短时缓存（减轻 is_within + 粗估重复计算）──
_SCHEDULE_STATUS_LOCK = threading.Lock()
_SCHEDULE_STATUS_CACHE: Optional[Tuple[tuple, float, Dict[str, Any]]] = None


def _human_escalation_cfg_hash(he: Dict[str, Any]) -> str:
    try:
        return hashlib.md5(
            json.dumps(he, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
    except Exception:
        return str(hash(str(he)))


def _schedule_status_cache_get(key: tuple, ttl_sec: float) -> Optional[Dict[str, Any]]:
    if ttl_sec <= 0:
        return None
    with _SCHEDULE_STATUS_LOCK:
        global _SCHEDULE_STATUS_CACHE
        ent = _SCHEDULE_STATUS_CACHE
        if ent is None:
            return None
        k, exp, payload = ent
        if k == key and time.monotonic() < exp:
            return payload
    return None


def _schedule_status_cache_set(key: tuple, payload: Dict[str, Any], ttl_sec: float) -> None:
    if ttl_sec <= 0:
        return
    with _SCHEDULE_STATUS_LOCK:
        global _SCHEDULE_STATUS_CACHE
        _SCHEDULE_STATUS_CACHE = (key, time.monotonic() + ttl_sec, payload)


def invalidate_schedule_status_cache() -> None:
    """配置变更后清空 schedule-status 缓存（可选调用）。"""
    global _SCHEDULE_STATUS_CACHE
    with _SCHEDULE_STATUS_LOCK:
        _SCHEDULE_STATUS_CACHE = None


class RevalidateStaticFiles(StaticFiles):
    """强制回源校验的静态服务（``Cache-Control: no-cache``）。

    为什么需要（2026-07-31「切换失败」事故收尾）：``/copilot`` 的 iframe 入口
    ``app.html`` 没有 ``?v=`` 缓存戳可用（iframe src 只带 ?theme=），而 Starlette
    静态响应默认不带 Cache-Control → Chromium 启发式缓存可把旧 app.html 连同其
    引用的旧组件 URL 一起复用数小时——坐席「刷新了还是旧的」。no-cache ≠ 不缓存：
    浏览器仍缓存，只是每次使用前必须带 ETag 回源验证（未变=304，极廉价）。
    200 与 304（NotModifiedResponse）两种返回都补头。
    """

    def file_response(self, *args, **kwargs):
        resp = super().file_response(*args, **kwargs)
        resp.headers["Cache-Control"] = "no-cache"
        return resp


class CachedStaticFiles(StaticFiles):
    """按 ``?v=`` 版本戳分级缓存的静态服务（2026-08-07 感知性能实测后加）。

    背景：``/static`` 此前挂裸 StaticFiles，**不带任何 Cache-Control** → 浏览器每次
    导航都要对每个资源回源校验。实测一次 ``/workspace`` 导航要拉 14 个资源、
    占传输 20%（托管形态经反向 SSH 隧道，每次往返都很贵）。

    分级依据＝本仓既有的「改前端就 bump ``?v=``」纪律（见 ui-build.txt 注释）：
    - 带 ``v=`` 版本戳 → 内容一变 URL 就变，可安全 **immutable 长缓存**
      （大头如 unified-inbox.css 236KB 正属此类，命中后重复导航零传输）；
    - 无版本戳（图标/token 等小文件）→ 只给 **300s 短缓存**：既消掉一次会话内
      的反复回源往返，又把「改了没生效」的窗口钉在 5 分钟内；
    - ``ui-build.txt`` 例外 **no-cache**：它是「陈旧页提醒」的新鲜度信号源，
      缓存它等于让坐席晚几分钟才收到「请刷新」，与它存在的目的相悖。
    """

    def file_response(self, *args, **kwargs):
        resp = super().file_response(*args, **kwargs)
        scope = kwargs.get("scope")
        if scope is None and len(args) >= 3:
            scope = args[2]
        try:
            qs = (scope or {}).get("query_string", b"").decode("latin-1")
            path = (scope or {}).get("path", "") or ""
        except Exception:  # noqa: BLE001 - 取不到就按最保守的短缓存
            qs, path = "", ""
        if path.endswith("ui-build.txt"):
            resp.headers["Cache-Control"] = "no-cache"
        elif qs.startswith("v=") or "&v=" in qs:
            resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        else:
            resp.headers["Cache-Control"] = "public, max-age=300"
        return resp


class ProtocolMediaStatic(CachedStaticFiles):
    """协议媒体专属静态服务：主根（实例数据根）优先，旧引擎树根兜底。

    媒体根 2026-08-19 迁入实例数据根（``protocol_bridge.protocol_media_root``：
    备份带得走、多实例不混居），但存量 ``media_ref`` 全是
    ``/static/protocol_media/…`` URL——本类保住该命名空间：主根 miss 时回查旧根，
    启动迁移（``migrate_legacy_protocol_media``）偶发搬不动的文件（被占用/权限）
    仍可被服务，搬迁过程零 404 窗口。缓存语义继承 ``CachedStaticFiles``（?v= 分级）。
    """

    def __init__(self, *args, fallback_directory: str | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self._fallback = (
            StaticFiles(directory=fallback_directory, check_dir=False)
            if fallback_directory else None)

    def lookup_path(self, path: str):
        full, stat_result = super().lookup_path(path)
        if stat_result is None and self._fallback is not None:
            return self._fallback.lookup_path(path)
        return full, stat_result


def create_app(config_manager, audit_store=None, boot_ts: float = 0,
               telegram_client=None, event_tracker=None, log_buffer=None) -> FastAPI:
    # Load domain pack manifest for web integration（支付域在插件关闭时映射为 conversion）
    _cfg_obj = config_manager.config if isinstance(getattr(config_manager, "config", None), dict) else {}
    domain_name = effective_domain_name(_cfg_obj)
    domain_display = config_manager.config.get("web_admin", {}).get("site_name", "")
    domain_web_pages: list = []
    domain_dashboard_widgets: list = []
    _domain_manifest: dict = {}
    # 域包目录经 resolve_domains_dir 统一定位：冻结态 config_path 在用户数据目录，
    # 其 ../domains 是空的，必须回落到随包的 <_MEIPASS>/domains，否则安装版丢掉
    # 领域看板挂件与域内模板（与 skill_manager 同一入口，避免两处口径分裂）。
    from src.utils.domain_loader import resolve_domains_dir as _resolve_domains_dir
    _domains_dir = _resolve_domains_dir(getattr(config_manager, "config_path", None), domain_name)
    try:
        _mf = _domains_dir / domain_name / "manifest.yaml"
        if _mf.exists():
            import yaml as _y
            with open(_mf, "r", encoding="utf-8") as _f:
                _domain_manifest = _y.safe_load(_f) or {}
            if not domain_display:
                domain_display = _domain_manifest.get("display_name", "")
            _web_section = _domain_manifest.get("web", {})
            domain_web_pages = _web_section.get("pages", [])
            domain_dashboard_widgets = _web_section.get("dashboard_widgets", [])
    except Exception:
        pass
    # Add domain template directory to Jinja2 search path
    if _domains_dir:
        _domain_tpl_dir = _domains_dir / domain_name / "web" / "templates"
        if _domain_tpl_dir.is_dir():
            from jinja2 import FileSystemLoader
            templates.env.loader = FileSystemLoader(
                [str(_domain_tpl_dir), str(_TEMPLATE_DIR)]
            )
    if domain_display:
        templates.env.globals["site_name"] = domain_display
        templates.env.globals["site_name_short"] = domain_display

    # ── C1-1 白标：品牌 overlay 注入 Jinja globals（优先于 domain pack）──
    def _apply_branding_globals():
        try:
            from src.licensing import get_license_manager
            from src.utils.branding import get_branding

            _b = get_branding(getattr(config_manager, "config", None) or {},
                              get_license_manager().status())
            templates.env.globals["site_name"] = _b["site_name"]
            templates.env.globals["site_name_short"] = _b["site_name_short"]
            templates.env.globals["company_name"] = _b["company_name"]
            templates.env.globals["product_name"] = _b["product_name"]
            templates.env.globals["sidebar_name"] = _b["sidebar_name"]
            templates.env.globals["brand_login_line"] = _b["login_line"]
            templates.env.globals["brand_primary_color"] = _b["primary_color"]
            templates.env.globals["brand_logo_url"] = _b["logo_url"]
            templates.env.globals["brand_mark_url"] = _b["brand_mark_url"]
            templates.env.globals["product_icon_url"] = _b["product_icon_url"]
            templates.env.globals["brand_hub_url"] = _b.get("brand_hub_url", "")
            templates.env.globals["brand_login_subtitle"] = _b["login_subtitle"]
            templates.env.globals["show_powered_by"] = _b["show_powered_by"]
            templates.env.globals["powered_by_text"] = _b["powered_by_text"]
        except Exception:
            import logging as _lb
            _lb.getLogger("admin").debug("品牌 globals 注入跳过", exc_info=True)

    _apply_branding_globals()
    app_branding_refresh = _apply_branding_globals

    app = FastAPI(title=templates.env.globals["site_name"], docs_url=None, redoc_url=None)

    # ── C0-3 授权强制：只读模式拦截写操作（enforce 关 / 授权有效时零开销直通）──
    @app.middleware("http")
    async def _license_readonly_guard(request, call_next):
        try:
            from src.licensing import get_license_manager, is_write_blocked

            st = get_license_manager().status()
            if is_write_blocked(request.url.path, request.method, st):
                try:   # P4 埋点：只读写保护触发（进程内一次；fail-silent）
                    from src.utils.telemetry import track_once
                    track_once("write_blocked", "license.write_blocked", {
                        "state": str(getattr(st, "state", "") or ""),
                        "path": str(request.url.path)[:80]})
                except Exception:
                    pass
                from fastapi.responses import JSONResponse

                return JSONResponse(
                    status_code=403,
                    content={
                        "ok": False,
                        "error": "license_readonly",
                        "detail": "授权已失效，系统处于只读模式，请联系厂商续费后恢复。",
                    },
                )
        except Exception:  # pragma: no cover - 守卫自身异常绝不阻断请求
            pass
        return await call_next(request)

    # ── C0-3b 档位功能闸门：按授权档位锁 API 族（feature_gate 关 = 全放行零变化）──
    # 单点强制：前缀表见 src/licensing/feature_gate.py::API_FEATURE_PREFIXES；
    # 先于路由/鉴权（未解锁 → 403 feature_locked；已解锁未登录 → 后续 401 照旧）。
    @app.middleware("http")
    async def _feature_gate_guard(request, call_next):
        try:
            from src.licensing.feature_gate import (
                feature_enabled, feature_for_api_path, gate_enabled,
            )

            _cfg = getattr(config_manager, "config", None) or {}
            if gate_enabled(_cfg):
                _feat = feature_for_api_path(request.url.path)
                if _feat and not feature_enabled(_feat, _cfg):
                    from fastapi.responses import JSONResponse

                    from src.web.web_i18n import tr as _tr

                    try:  # E6 锁触达观测（定价信号），失败绝不影响拦截语义
                        from src.web.feature_lock_stats import get_feature_lock_stats
                        get_feature_lock_stats().record(_feat, "api")
                    except Exception:
                        pass
                    return JSONResponse(
                        status_code=403,
                        content={
                            "ok": False,
                            "error": "feature_locked",
                            "feature": _feat,
                            "detail": _tr(request, "err.lic.feature_locked"),
                        },
                    )
                # E3：页面族守卫——nav 对锁定功能渲染锁标跳 /membership，但直连
                # URL（书签/分享链）仍能打开半残页（渲染成功、API 全 403）。此处
                # 与 nav 同源判定（nav_schema.feature_for_page_path），锁定页面
                # GET 一律 302 升级引导。仅 GET（页面导航语义）；/membership 自身
                # 无 feature 标注，天然无环。
                if request.method == "GET" and not request.url.path.startswith("/api/"):
                    from src.web.nav_schema import feature_for_page_path

                    _pfeat = feature_for_page_path(request.url.path)
                    if _pfeat and not feature_enabled(_pfeat, _cfg):
                        from fastapi.responses import RedirectResponse

                        try:  # E6 锁触达观测
                            from src.web.feature_lock_stats import (
                                get_feature_lock_stats,
                            )
                            get_feature_lock_stats().record(_pfeat, "page")
                        except Exception:
                            pass
                        # ?from=<族> → 会员页高亮对应矩阵行 + 来源引导语（E4）
                        return RedirectResponse(
                            "/membership?from=" + _pfeat, status_code=302)
        except Exception:  # pragma: no cover - 守卫自身异常绝不阻断请求
            pass
        return await call_next(request)

    _static_dir = Path(__file__).parent / "static"
    if _static_dir.is_dir():
        # Windows 的 MIME 注册表常缺 .woff2（或被装成 text/plain），Starlette 静态
        # 服务按 mimetypes 猜类型会把品牌字体标错——浏览器多半仍加载但控制台告警、
        # 且严格代理/CDN 可能拒缓存。显式登记一次（幂等，进程级）。
        import mimetypes as _mimetypes

        _mimetypes.add_type("font/woff2", ".woff2")
        _mimetypes.add_type("font/woff", ".woff")
        # 协议媒体专属挂载（账号资产 P0，2026-08-19）：媒体根已迁实例数据根
        # （protocol_bridge.protocol_media_root），对同一 URL 前缀**先**注册专属
        # 挂载（Starlette 按注册序匹配）保住存量 /static/protocol_media/... 引用；
        # 无数据根契约（根==旧根）时不挂＝旧单挂载行为。挂载失败回落旧行为，
        # 绝不因媒体挂载挡整个后台。
        try:
            from src.integrations.protocol_bridge import (
                legacy_protocol_media_root as _pm_legacy_fn,
                protocol_media_root as _pm_root_fn,
            )
            _pm_root = _pm_root_fn()
            _pm_legacy = _pm_legacy_fn()
            if str(_pm_root) != str(_pm_legacy):
                _pm_root.mkdir(parents=True, exist_ok=True)
                app.mount(
                    "/static/protocol_media",
                    ProtocolMediaStatic(
                        directory=str(_pm_root),
                        fallback_directory=(
                            str(_pm_legacy) if _pm_legacy.is_dir() else None)),
                    name="protocol_media",
                )
        except Exception:  # noqa: BLE001
            logger.warning("protocol_media 专属挂载失败（回落旧 /static 单挂载）",
                           exc_info=True)
        # 人设相册专属挂载（#67-① 0830）：相册落盘迁数据根（打包桌面态旧位置
        # =安装目录，更新即清空），URL 形态 /static/persona_albums/... 不变——
        # 数据根优先 + 旧树兜底，与 protocol_media 同款零 404 窗口。
        # 无数据根契约（根==旧树，裸引擎/CI）不挂＝旧行为。
        try:
            from src.companion.media_paths import (
                LEGACY_ALBUM_ROOT as _pa_legacy,
                resolve_album_root as _pa_root_fn,
            )
            _pa_root = _pa_root_fn()
            if str(_pa_root.resolve()) != str(_pa_legacy.resolve()):
                _pa_root.mkdir(parents=True, exist_ok=True)
                app.mount(
                    "/static/persona_albums",
                    ProtocolMediaStatic(
                        directory=str(_pa_root),
                        check_dir=False,
                        fallback_directory=(
                            str(_pa_legacy) if _pa_legacy.is_dir() else None)),
                    name="persona_albums",
                )
        except Exception:  # noqa: BLE001
            logger.warning("persona_albums 专属挂载失败（回落旧 /static 单挂载）",
                           exc_info=True)
        app.mount("/static", CachedStaticFiles(directory=str(_static_dir)), name="static")
    # 两端共享 copilot 组件库(单一事实来源 repo 根 shared/copilot);独立前缀避开 /static 匹配顺序
    # no-cache：iframe 入口 app.html 无 ?v= 戳，必须逐次回源校验防启发式缓存钉住旧版
    _shared_copilot_dir = Path(__file__).resolve().parents[2] / "shared" / "copilot"
    if _shared_copilot_dir.is_dir():
        app.mount(
            "/copilot",
            RevalidateStaticFiles(directory=str(_shared_copilot_dir)),
            name="copilot_shared",
        )
    # AI 助手悬浮球共享组件（2026-08-19；与 copilot 同模式：仓根单一事实源 +
    # 桌面镜像，no-cache 逐次回源防旧版钉住）
    _shared_assistant_dir = (
        Path(__file__).resolve().parents[2] / "shared" / "assistant"
    )
    if _shared_assistant_dir.is_dir():
        app.mount(
            "/assistant-shared",
            RevalidateStaticFiles(directory=str(_shared_assistant_dir)),
            name="assistant_shared",
        )

    # ── PWA（Phase 1：把 /workspace 做成可安装的原生官网）──────────────
    # service worker 必须从根路径供应才能控制整个 origin 作用域；manifest 须带正确 MIME。
    # 两者均为公开端点（浏览器/SW 自身需在无 session 时也能拉取）。
    from fastapi.responses import FileResponse as _FileResponse

    _pwa_dir = _static_dir / "pwa"

    @app.get("/sw.js", include_in_schema=False)
    async def _pwa_service_worker():
        resp = _FileResponse(
            str(_pwa_dir / "sw.js"), media_type="application/javascript"
        )
        # 允许根作用域；SW 文件本身不缓存，保证版本号变更即时生效
        resp.headers["Service-Worker-Allowed"] = "/"
        resp.headers["Cache-Control"] = "no-cache"
        return resp

    @app.get("/manifest.webmanifest", include_in_schema=False)
    async def _pwa_manifest():
        from fastapi.responses import JSONResponse
        from src.utils.branding import get_branding, pwa_manifest

        lic = None
        try:
            from src.licensing import get_license_manager
            lic = get_license_manager().status()
        except Exception:
            pass
        brand = get_branding(getattr(config_manager, "config", None) or {}, lic)
        return JSONResponse(pwa_manifest(brand))
    app.state.kb_conflict_checkers = []
    app.state.intent_display_names_extra = {}
    app.state.config_manager = config_manager
    app.state.telegram_client = telegram_client
    # P23-B: 让外部 router 注册器（如 rpa_overview_routes 的 audit 钩子）能拿到 audit_store
    app.state.audit_store = audit_store

    web_cfg = config_manager.config.get("web_admin", {})
    secret = web_cfg.get("secret_key", "change-me-in-production")
    token = web_cfg.get("auth_token", "")
    # worker 专用窄令牌（2026-08-28 P1-6 第一步）：四个 Node 侧车（WhatsApp/Messenger/
    # Instagram/Zalo）此前持有的是 **admin auth_token**——匹配它就直通 _api_auth 的
    # 全部路由，等于给协议进程发了管理员钥匙。它们真正需要的只有 /api/internal/*
    # （11 个端点，实际打的是 protocol/ingest 与 session-status）。
    # 本键留空＝行为与此前**逐字节相同**；配上才生效，故可先随任意重启装载，
    # 待引擎确认接受双令牌后再切 Node 侧（顺序不可颠倒，见 worker_token 注释块）。
    worker_token = str(web_cfg.get("worker_token", "") or "").strip()
    if worker_token:
        # 三道自检：与 admin 同值＝零隔离（还不如不配）；过短或占位符＝比不配更危险
        # （一个可猜的字符串换来内部端点访问）。任一不过一律**降级为未配置**并告警，
        # 绝不「带病放行」。
        _wt_bad = ""
        if hmac.compare_digest(worker_token, str(token or "")):
            _wt_bad = "与 auth_token 同值（无隔离效果）"
        elif len(worker_token) < 24:
            _wt_bad = f"长度 {len(worker_token)} < 24（太短，可暴力猜）"
        elif any(m in worker_token.upper() for m in ("CHANGE_ME", "YOUR_", "PLACEHOLDER", "EXAMPLE")):
            _wt_bad = "疑似占位符未替换"
        if _wt_bad:
            logger.warning(
                "[SECURITY] web_admin.worker_token %s —— 已按未配置处理；"
                "侧车将继续使用 auth_token（管理员级），请换一个独立随机值。", _wt_bad)
            worker_token = ""

    # S2：默认 secret_key 是公开常量（签名 session 可被伪造）。此处仅告警不阻断
    # （本地/测试仍可跑）；真正的「非本地暴露」防护做成 fail-safe，在 bootstrap 启动处
    # 检测到「默认 secret + 绑定非本地地址」时降级绑回 127.0.0.1（见 bootstrap/web_app.py）。
    if secret == "change-me-in-production":
        logger.warning(
            "[SECURITY] 正在使用默认 secret_key，仅适用于本地/测试；生产请在 "
            "config.yaml::web_admin.secret_key 配置随机值，否则 session 可被伪造。"
        )

    session_max_age = int(web_cfg.get("session_max_age", 7200))
    # S4：session cookie 加 SameSite=Strict；经 TLS 反代时置 cookie_secure:true 开启 Secure。
    _cookie_secure = bool(web_cfg.get("cookie_secure", False))
    app.add_middleware(
        SessionMiddleware,
        secret_key=secret,
        max_age=session_max_age,
        same_site="strict",
        https_only=_cookie_secure,
    )

    # ── 响应压缩（2026-08-07 实测定位）：工作台页面 1~2MB 未压缩 HTML 经反向 SSH
    # 隧道传输时严重拖慢（隧道有效吞吐 ~30-50KB/s，TCP-over-TCP 吞吐坍缩）——直连
    # localhost 渲染仅 0.03s，走隧道却 20~45s。在**实例侧**先压缩是唯一能在进隧道前
    # 减小字节的位置（nginx 在隧道下游，压不到隧道那一跳）。对 LAN 坐席顺带受益。
    # 同日升级：GZipMiddleware → 自带 CompressionMiddleware（brotli 优先再省 ~15-20%，
    # gzip 兜底语义不变；顺带修 SSE 不该被压缩缓冲的盲区）。minimum_size 跳过小响应。
    from src.web.compression import CompressionMiddleware
    app.add_middleware(CompressionMiddleware, minimum_size=1024)

    # ── CORS（S5：默认同源；关闭 '*' + allow_credentials 的危险组合）────────
    cors_origins = web_cfg.get("cors_origins", [])
    if isinstance(cors_origins, str):
        cors_origins = [o.strip() for o in cors_origins.split(",") if o.strip()]
    if cors_origins:
        _allow_star = "*" in cors_origins
        if _allow_star:
            logger.warning(
                "[SECURITY] web_admin.cors_origins 含 '*'，已禁用 allow_credentials 以避免危险组合；"
                "如需带凭据跨域，请配置精确来源白名单。"
            )
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cors_origins,
            allow_credentials=(not _allow_star),
            allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
            allow_headers=["*"],
        )

    # ── CSRF 防护 ─────────────────────────────────────────────
    import secrets as _secrets
    _CSRF_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
    # 豁免口清单（每条都要能回答「为什么 CSRF 威胁模型不适用」）：
    # - /login /logout /setup：未认证引导入口，无 ambient 会话权限可被借用（S3）；
    # - /api/goals/order-hook：外部服务器回流 webhook，路由自带共享 token 恒时比较（2026-07-27 实锤）；
    # - /api/telemetry/*：navigator.sendBeacon 无法自定义头，端点只累计**消毒后的计数**
    #   （page/fn/type 白名单化、distinct 封顶），无状态权限可借用；鉴权仍在路由层
    #   ——不豁免则「Referer 被隐私设置剥掉」的环境里观测通道先于业务瞎掉（2026-07-31）。
    _CSRF_EXEMPT_PATHS = {
        "/login", "/logout", "/setup", "/api/goals/order-hook",
        "/api/telemetry/frontend-error", "/api/telemetry/ui-event",
    }

    def _csrf_admit(request: Request, ticket: str) -> None:
        """写请求放行留痕（P2 收口决策数据面）：
        - ``request.state.csrf_ticket``＝单一事实源（/api/preflight/echo 回显「靠哪张证」）；
        - 进程计数 csrf_stats.admitted_by（看「当下分布」）；
        - origin/referer 放行另落日趋势（跨重启看「同源回落还有没有人在用」——
          两周归零才能安全地把回落降级为纯观测）。全程 best-effort 零阻断。"""
        try:
            request.state.csrf_ticket = ticket
            from src.web.csrf_stats import get_csrf_reject_stats
            get_csrf_reject_stats().record_admit(ticket)
            if ticket in ("origin", "referer"):
                from src.web.csrf_trend import record_csrf_admit_trend
                record_csrf_admit_trend(ticket)
        except Exception:
            pass

    @app.middleware("http")
    async def csrf_middleware(request: Request, call_next):
        if request.method in _CSRF_SAFE_METHODS:
            response = await call_next(request)
            if not request.cookies.get("csrf_token"):
                csrf_tok = _secrets.token_hex(16)
                response.set_cookie("csrf_token", csrf_tok, httponly=False, samesite="strict")
            return response
        auth_h = request.headers.get("Authorization", "")
        if auth_h.startswith("Bearer "):
            _csrf_admit(request, "bearer")
            return await call_next(request)
        # 公网网页聊天 Widget：访客用 HMAC token 鉴权（非 session cookie），CSRF 不适用
        if request.url.path.startswith("/chat/"):
            return await call_next(request)
        if request.url.path in _CSRF_EXEMPT_PATHS:
            return await call_next(request)
        _line_exempt = getattr(request.app.state, "line_webhook_path", None)
        if _line_exempt and request.url.path == _line_exempt:
            return await call_next(request)
        cookie_tok = request.cookies.get("csrf_token", "")
        header_tok = request.headers.get("X-CSRF-Token", "")
        if cookie_tok and header_tok and hmac.compare_digest(cookie_tok, header_tok):
            _csrf_admit(request, "csrf_pair")
            return await call_next(request)
        origin = request.headers.get("origin", "")
        referer = request.headers.get("referer", "")
        host = request.headers.get("host", "")
        if host:
            expected = {f"http://{host}", f"https://{host}"}
            if origin and origin in expected:
                _csrf_admit(request, "origin")
                return await call_next(request)
            if referer:
                for exp in expected:
                    if referer.startswith(exp + "/") or referer == exp:
                        _csrf_admit(request, "referer")
                        return await call_next(request)
        # S3：CSRF token / 同源校验均失败 → 一律拒绝（含表单/multipart），
        # 关闭原「非 JSON 写请求直接放行」的旁路（登录等引导入口已在上方豁免）。
        # 2026-07-31 起拒绝**必留痕**：计数进 csrf_stats（metrics/Prometheus/ops 卡）+
        # 节流 WARNING——「人设切换失败」事故里这里静默吞了两周的 403，谁也不知道。
        # 响应带机器可读 code：前端据此分型提示/自愈重试（重放安全：拒绝发生在业务逻辑之前）。
        _kind = "bare"
        try:
            from src.web.csrf_stats import get_csrf_reject_stats
            _stats = get_csrf_reject_stats()
            _kind = _stats.record(
                path=request.url.path,
                had_cookie=bool(cookie_tok), had_header=bool(header_tok),
                origin=origin, referer=referer,
            )
            try:
                from src.web.csrf_trend import record_csrf_reject_trend
                record_csrf_reject_trend(_kind)
            except Exception:
                pass
            _ip = request.client.host if request.client else "?"
            if _stats.should_log(f"{_ip}|{request.url.path}"):
                logger.warning(
                    "[CSRF] 拒绝写请求 %s %s kind=%s ip=%s ua=%.40s",
                    request.method, request.url.path, _kind, _ip,
                    request.headers.get("user-agent", ""),
                )
        except Exception:
            pass
        return JSONResponse(
            status_code=403,
            content={"detail": "CSRF token missing or invalid", "code": "csrf"},
        )

    # ── HTML 页面禁缓存（防止浏览器缓存旧版模板） ──────────────
    @app.middleware("http")
    async def nocache_html_middleware(request: Request, call_next):
        response = await call_next(request)
        ct = response.headers.get("content-type", "")
        if "text/html" in ct and not request.url.path.startswith("/static"):
            response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        return response

    # ── 管理端限流（固定窗口计数器，内存高效） ──────────────────
    _rate_buckets: Dict[str, list] = {}  # ip -> [window_start, count]
    _RATE_WINDOW = int(web_cfg.get("rate_limit_window", 60))
    _RATE_MAX = int(web_cfg.get("rate_limit_max", 120))
    _rate_gc_ts: float = 0.0
    _rate_whitelist = set(web_cfg.get("rate_limit_whitelist", ["127.0.0.1", "::1"]))

    @app.middleware("http")
    async def rate_limit_middleware(request: Request, call_next):
        nonlocal _rate_gc_ts
        client_ip = request.client.host if request.client else "unknown"
        if client_ip in _rate_whitelist:
            return await call_next(request)
        _line_path = getattr(request.app.state, "line_webhook_path", None)
        if _line_path and request.url.path == _line_path:
            return await call_next(request)
        now = time.time()
        bucket = _rate_buckets.get(client_ip)
        if not bucket or now - bucket[0] >= _RATE_WINDOW:
            _rate_buckets[client_ip] = [now, 1]
        else:
            bucket[1] += 1
            if bucket[1] > _RATE_MAX:
                return JSONResponse(
                    status_code=429,
                    content={"detail": "Too many requests"},
                    headers={"Retry-After": str(_RATE_WINDOW)},
                )
        if now - _rate_gc_ts > _RATE_WINDOW * 2:
            _rate_gc_ts = now
            stale = [k for k, v in _rate_buckets.items() if now - v[0] >= _RATE_WINDOW * 2]
            for k in stale:
                _rate_buckets.pop(k, None)
        return await call_next(request)

    # ── P25-A: 全局 body size 限制（防大 JSON / chunked 流式攻击） ──────
    # 比 P24-C 的 per-route _read_json_body 更强：
    #   1. 覆盖所有 POST/PUT/PATCH 路由（不只是 intent-tags）
    #   2. 流式逐块累计 + 即时熔断（attacker 流 100MB 我方只缓存到 limit 字节就中止）
    #   3. P25-B: 413 写 audit_log（潜在攻击信号）
    # 例外：path 在 _BODY_LIMIT_EXEMPT 中的不受限（如文件上传端点 — 当前无）
    _BODY_LIMIT_DEFAULT = int(web_cfg.get("max_body_bytes", 2 * 1024 * 1024))
    # P26-C: 从 config.yaml::web_admin.body_limits 加载 per-path 上限（dict path->bytes）
    _raw_overrides = web_cfg.get("body_limits") or {}
    _BODY_LIMIT_OVERRIDES: Dict[str, int] = {}
    if isinstance(_raw_overrides, dict):
        for _k, _v in _raw_overrides.items():
            try:
                _BODY_LIMIT_OVERRIDES[str(_k)] = int(_v)
            except (TypeError, ValueError):
                continue
    # 兼容旧字段 max_body_bytes_intent_tags_write
    if "/api/rpa/intent-tags" not in _BODY_LIMIT_OVERRIDES:
        _BODY_LIMIT_OVERRIDES["/api/rpa/intent-tags"] = int(
            web_cfg.get("max_body_bytes_intent_tags_write", 4 * 1024 * 1024))
    # P4（2026-08-18）媒体翻译上传口代码级缺省：这些端点收 base64 媒体（×1.33 膨胀），
    # 2MB 全局默认把「音频 25MB/视频 50MB/文档 10MB/图 8MB」的路由级上限拦腰斩断
    # （实测 1.85MB 参考音 b64=2.47MB 被 413）。上限=路由级源文件上限×1.37 取整，
    # 端点内各自的解码上限仍是第二道闸；config body_limits 可按实例覆写。
    for _mp, _mlim in (
        ("/api/unified-inbox/translate-voice", 36 * 1024 * 1024),
        ("/api/unified-inbox/translate-video", 72 * 1024 * 1024),
        ("/api/unified-inbox/translate-document-file", 16 * 1024 * 1024),
        ("/api/unified-inbox/translate-image", 12 * 1024 * 1024),
    ):
        _BODY_LIMIT_OVERRIDES.setdefault(_mp, _mlim)
    # M-3 A（#227 #229 #231，2026-09-06）：收件箱媒体上传口**代码级豁免**本闸。
    # 事故链：send-media 是 multipart 文件上传，却一直吃 2MB 全局默认——任何 >2MB 的
    # 图/视频在路由跑起来之前就被这里 413 + Connection:close，浏览器还在推 body 时连接
    # 被掐（本机复现 ConnectionAbortedError 10053 / 10054），XHR 只见 onerror、后端零
    # `[send-media]` 记录 → 坐席看到「结果未知」。98MB 视频、5MB .MOV 三平台全灭都是它，
    # 不是格式、不是网络。该路由自带按平台/按类别的流式体积闸（media_limits，413 带
    # 实际大小与上限），比这里的一刀切更准，故豁免而非改上限。
    _BODY_LIMIT_CODE_EXEMPT_PREFIXES: tuple = ("/api/unified-inbox/send-media",)
    _BODY_LIMIT_EXEMPT_PREFIXES: tuple = (
        tuple(web_cfg.get("max_body_exempt_prefixes", []))
        + _BODY_LIMIT_CODE_EXEMPT_PREFIXES)
    # P25-B / P26-D: 413 攻击信号防抖 — 用通用 AuditThrottle
    from src.utils.audit_throttle import AuditThrottle as _AuditThrottle
    _body_oversize_throttle = _AuditThrottle(window_sec=5.0, max_keys=4096)
    # P26-B: 暴露 413 计数器给 /api/rpa/metrics（label = path），格式 {path: count}
    app.state.web_body_oversize_counter = {}

    def _audit_oversize(request: Request, path: str, observed: int, limit: int) -> None:
        """P25-B / P26-D: 413 攻击信号写 audit log（per-IP 5s 防抖）。"""
        try:
            ip = request.client.host if request.client else "unknown"
            # P26-B: 计数（不防抖，每次都加 — Prometheus 看真实攻击量）
            counter = app.state.web_body_oversize_counter
            counter[path] = counter.get(path, 0) + 1
            # 审计写入（5s 防抖防 DB flood）
            if not _body_oversize_throttle.should_emit(ip):
                return
            actor = getattr(getattr(request.state, "user", None), "username", "unknown")
            audit_store.log(actor, "web_body_oversize_rejected",
                            f"path={path} observed={observed} limit={limit} ip={ip}")
        except Exception:
            pass

    def _413_response(limit: int, detail: str) -> JSONResponse:
        """P27-B: 413 响应统一带 X-Body-Limit 头（告知客户端上限）+ Connection:close。"""
        return JSONResponse(
            status_code=413,
            content={"detail": detail, "max_body_bytes": limit},
            headers={
                "Connection": "close",
                "X-Body-Limit": str(limit),
            },
        )

    @app.middleware("http")
    async def body_size_limit_middleware(request: Request, call_next):
        if request.method in ("GET", "HEAD", "OPTIONS", "DELETE"):
            return await call_next(request)
        path = request.url.path
        if any(path.startswith(p) for p in _BODY_LIMIT_EXEMPT_PREFIXES):
            return await call_next(request)
        limit = _BODY_LIMIT_OVERRIDES.get(path, _BODY_LIMIT_DEFAULT)
        # 1) Content-Length 预检（快速失败，不读 body）
        cl = request.headers.get("content-length")
        if cl and cl.isdigit():
            if int(cl) > limit:
                _audit_oversize(request, path, int(cl), limit)
                return _413_response(limit,
                    f"request body too large (declared {cl}, max {limit})")
        # 2) 流式累积（防伪 Content-Length + chunked 上传）
        body_chunks: list = []
        received = 0
        while True:
            msg = await request._receive()
            mtype = msg.get("type")
            if mtype == "http.disconnect":
                return JSONResponse(status_code=499,
                                     content={"detail": "client disconnected"})
            if mtype != "http.request":
                continue
            chunk = msg.get("body", b"") or b""
            if chunk:
                received += len(chunk)
                if received > limit:
                    _audit_oversize(request, path, received, limit)
                    return _413_response(limit,
                        f"request body too large (streamed {received}, max {limit})")
                body_chunks.append(chunk)
            if not msg.get("more_body", False):
                break
        # 3) 重放给下游
        # 回落目标必须是**替换前**的 receive，故先捕获再替换。这一行有两次
        # 事故史，机制与判据都写在 body_replay.py 的模块 docstring 里：
        #   · 旧实现耗尽后返回伪造的 `{"type":"http.disconnect"}` —— 普通响应
        #     无害，但 StreamingResponse 并发跑 listen_for_disconnect 来实现
        #     「客户端一走就停止出话」，它把伪信号当真 → 掐掉正在出话的生成器
        #     ⇒ 小智问答 100% 在 meta 之后 0.2s 断流、前端只剩「网络异常」；
        #   · 改成 `await request._receive()` 同样错 —— 赋值已发生，那是自调用，
        #     967 层后 RecursionError，症状与上一种一模一样。
        # 做成工厂后 original_receive 由闭包捕获，自引用在结构上写不出来。
        full_body = b"".join(body_chunks)
        request._receive = make_replay_receive(request._receive, full_body)
        return await call_next(request)

    # ── M-3 A（#227）Web 层访问日志：大请求 / 慢请求 / 被拒请求一行落痕 ────────
    # 8YNDKE 实锤：98MB 上传期间「后端零记录」——被上面 body 闸掐掉的请求不进任何路由，
    # 日志里就像没发生过。这里在闸**外侧**（后注册＝外层）记：路径 / 声明大小 / 耗时 /
    # 状态码，只对「值得看」的请求出声（体 ≥1MB、耗时 ≥3s、或 413/415/499/5xx），
    # 普通 GET 轮询零噪音。挂在 logger.info（生产默认级别）而非 debug。
    _ACCESS_LOG_MIN_BYTES = 1024 * 1024
    _ACCESS_LOG_SLOW_SEC = 3.0
    _ACCESS_LOG_STATUSES = {413, 415, 499}

    @app.middleware("http")
    async def upload_access_log_middleware(request: Request, call_next):
        _t0 = time.perf_counter()
        try:
            _cl = int(request.headers.get("content-length") or 0)
        except (TypeError, ValueError):
            _cl = 0
        status = 0
        try:
            response = await call_next(request)
            status = int(getattr(response, "status_code", 0) or 0)
            return response
        except BaseException:
            status = 500
            raise
        finally:
            _dt = time.perf_counter() - _t0
            if (_cl >= _ACCESS_LOG_MIN_BYTES or _dt >= _ACCESS_LOG_SLOW_SEC
                    or status in _ACCESS_LOG_STATUSES or status >= 500):
                try:
                    logger.info(
                        "[http] %s %s size=%.1fMB %.2fs %s ip=%s",
                        request.method, request.url.path, _cl / (1024 * 1024), _dt,
                        status or "-", request.client.host if request.client else "?")
                except Exception:
                    pass

    # RBAC user store
    from src.utils.web_user_store import (WebUserStore, ROLE_MASTER, ROLE_ADMIN,
                                           ROLE_VIEWER, ROLE_AGENT, ROLE_LABELS, PAGE_PERMISSIONS,
                                           UI_MODE_SIMPLE, UI_MODE_FULL, UI_MODE_LABELS,
                                           resolve_ui_mode)
    cfg_dir = config_manager.config_path.parent
    user_store = WebUserStore(cfg_dir / "web_users.db")
    if user_store.user_count() == 0:
        if token:
            user_store._ensure_master("admin", token)
        else:
            # S2：不再使用可爆破的默认口令 admin123。随机生成一次性 master 口令，
            # 落盘到数据根（仅本机可读）；运维首次登录后应立即改密（/api/change-password）。
            _seed_pw = _secrets.token_urlsafe(12)
            user_store._ensure_master("admin", _seed_pw)
            try:
                _seed_path = cfg_dir / "first_run_credential.txt"
                _seed_path.write_text(
                    "username: admin\npassword: " + _seed_pw + "\n"
                    "# 首次登录后请立即修改密码，随后可删除本文件。\n",
                    encoding="utf-8",
                )
                logger.warning(
                    "[SECURITY] 首次启动已生成随机 master 口令，见 %s（登录后请立即改密）",
                    _seed_path,
                )
            except Exception:
                logger.warning(
                    "[SECURITY] 首次启动已生成随机 master 口令（落盘失败）；"
                    "如无法登录请用 scripts 重置口令。"
                )

    # SSE 配置热更新推送（端点注册在 _api_auth 之后，见下方）
    import asyncio as _asyncio
    _sse_clients: list = []

    def _broadcast_config_reload():
        for q in list(_sse_clients):
            try:
                q.put_nowait({"event": "config_reload", "ts": time.time()})
            except Exception:
                pass

    config_manager.on_reload(_broadcast_config_reload)
    # 配置热重载后同步刷新品牌 globals：手改 config/config.local.yaml 的 brand 段
    # 也能免重启生效（设置页保存路径本就即时刷，这里补齐「直改文件」路径）。
    config_manager.on_reload(_apply_branding_globals)

    from src.web.web_i18n import get_translations
    # UI 语言白名单/locale/系统语言协商——单一事实源（xlate P3 / 自动跟随 2026-08-27）
    from src.web.i18n_packs import (
        UI_LANGS, UI_LOCALES, negotiate_ui_lang, primary_lang_tag,
    )

    from src.web.ui_lang_stats import get_ui_lang_stats
    _ui_lang_stats = get_ui_lang_stats()
    _UI_LANG_STATS_SKIP = ("/static", "/copilot", "/i18n", "/favicon")

    @app.middleware("http")
    async def inject_i18n(request: Request, call_next):
        # 语言链：?lang=（显式压制）→ ui_lang cookie（显式选择/登录回填）→
        # Accept-Language 系统语言推断（跟随而非固化：不落 cookie，浏览器/系统
        # 换语言界面即跟走；显式选过一次则 cookie 永远先手）→ zh。
        # 逐级独立校验：脏 ?lang= 落到 cookie 而非直接跳推断（旧实现 or 串联会
        # 让垃圾 query 吞掉合法 cookie）。
        _q = request.query_params.get("lang")
        _c = request.cookies.get("ui_lang")
        if _q in UI_LANGS:
            lang, _src = _q, "query"
        elif _c in UI_LANGS:
            lang, _src = _c, "cookie"
        else:
            _al = request.headers.get("accept-language", "")
            _neg = negotiate_ui_lang(_al)
            lang, _src = (_neg, "negotiated") if _neg else ("zh", "default")
        try:
            if not request.url.path.startswith(_UI_LANG_STATS_SKIP):
                _ui_lang_stats.record(_src, lang)
                # 「想要却没有」：推断落空但浏览器确实声明了语言 → 记需求分布
                if _src == "default" and _al:
                    _ui_lang_stats.record_unsupported(primary_lang_tag(_al))
        except Exception:
            pass  # 观测绝不干扰请求
        request.state.ui_lang = lang
        request.state.i18n = get_translations(lang)
        # 配置热重载检查点：check_and_hot_reload 原本只挂在 Telegram 消息循环——
        # 没进站消息的静默期改 config/overlay 永不生效。此处补 web 侧触发；
        # 先做零成本节流预判（窗口内纯内存比较），过窗才丢线程池做文件 IO/解析，
        # 绝不在事件循环上做同步 yaml load。
        try:
            import time as _hr_t
            if (_hr_t.time() - config_manager._last_hot_reload_check
                    >= config_manager._hot_reload_interval):
                from starlette.concurrency import run_in_threadpool
                await run_in_threadpool(config_manager.check_and_hot_reload)
        except Exception:
            pass
        response = await call_next(request)
        return response

    _PATH_TO_ACTIVE = {
        "/": "dash", "/templates": "tpl",
        # ops_overview P1 挂壳（2026-08-03）：/admin/ops 继承 base.html 后侧栏需高亮
        "/admin/ops": "ops",
        # 报障工单处置页（实施81 P0-2）
        "/admin/bug-tickets": "bug_tickets",
        "/strategies": "strategies", "/strategy-analytics": "strategy-analytics",
        "/audit": "audit", "/diff": "diff", "/logs": "logs",
        "/analytics": "analytics", "/help": "help", "/users": "users",
        "/knowledge": "knowledge", "/learner": "learner", "/settings": "settings",
        "/cases": "cases", "/episodic-memory": "episodic",
        "/crisis-audit": "crisis_audit",
        "/care-schedule": "care",
        "/relations-health": "relations_health",
        "/monetization": "monetization",
        "/line-rpa": "line_rpa",
        "/messenger-rpa": "messenger_rpa",
        "/whatsapp-rpa": "whatsapp_rpa",
        "/rpa-overview": "rpa_overview",
        "/funnel": "funnel",
        "/personas": "personas",
        "/singing": "singing",
        "/ai-studio": "ai_studio",
        "/membership": "membership",
        "/reply-settings": "reply_settings",
        "/personal-settings": "personal_settings",
        # 账号资产中心（账号资产保全 P1，2026-08-19）
        "/workspace/assets": "asset_center",
    }
    for _dp in domain_web_pages:
        _PATH_TO_ACTIVE[_dp["path"]] = _dp["key"]

    old_render = templates.TemplateResponse

    # 坐席规模快照（实施49 P1-6/B13）：每次渲染都查一遍用户表没必要，60s TTL 足够
    # ——加了第二个坐席账号后最迟下一分钟刷新页面即出现「认领」。
    _seat_snap = {"ts": 0.0, "multi": True}

    def _multi_seat_now() -> bool:
        import time as _t
        try:
            from src.web.ui_visibility import is_multi_seat
            if _t.time() - _seat_snap["ts"] > 60:
                _seat_snap["multi"] = is_multi_seat(
                    getattr(config_manager, "config", None), user_store.list_users())
                _seat_snap["ts"] = _t.time()
            return bool(_seat_snap["multi"])
        except Exception:
            return True   # 取不到一律显示：藏错方向会让真团队退回「两人回同一个客户」

    def _enrich_context(request: Request, context: dict) -> dict:
        """向模板上下文注入 i18n / 用户身份 / active 导航 / ui_mode 等公共字段"""
        i18n = get_translations()
        ui_lang = "zh"
        if hasattr(request, "state"):
            i18n = getattr(request.state, "i18n", i18n)
            ui_lang = getattr(request.state, "ui_lang", ui_lang)
        context.setdefault("i18n", i18n)
        context.setdefault("ui_lang", ui_lang)
        # BCP-47 locale（<html lang>/日期本地化用；消费 UI_LOCALES 单一事实源，
        # 此前 login.html 等页写死 zh-CN/en-US 二元——扩展语拿错 locale）
        context.setdefault("ui_locale", UI_LOCALES.get(ui_lang, "zh-CN"))
        # 词典指纹 → _i18n_bootstrap.html 走外链词典包（/i18n/ws-i18n.js?v=fp，
        # immutable 缓存；P2 传输减重）。异常时不注入 → 模板自动回落内联词典。
        try:
            from src.web.web_i18n import get_translations_fingerprint
            context.setdefault("i18n_fp", get_translations_fingerprint(ui_lang))
        except Exception:
            pass
        session_role = ""
        try:
            session_role = request.session.get("role", "")
        except Exception:
            pass
        if not session_role:
            try:
                if request.session.get("auth") == token and token:
                    session_role = ROLE_MASTER
            except Exception:
                pass
        context.setdefault("user_role", session_role)
        context.setdefault("username", "")
        try:
            context["username"] = request.session.get("username", "")
        except Exception:
            pass
        context.setdefault("display_name", context.get("username", ""))
        try:
            dn = request.session.get("display_name", "")
            if dn:
                context["display_name"] = dn
        except Exception:
            pass
        context.setdefault("page_perms", PAGE_PERMISSIONS)
        try:
            path = request.url.path.rstrip("/") or "/"
            context.setdefault("active", _PATH_TO_ACTIVE.get(path, ""))
        except Exception:
            context.setdefault("active", "")

        # ── UI 模式（简洁 / 完整）──────────────────────────────
        cookie_mode = request.cookies.get("ui_mode", "")
        effective_mode = resolve_ui_mode(cookie_mode, session_role)
        context.setdefault("ui_mode", effective_mode)
        context.setdefault("ui_mode_labels", UI_MODE_LABELS)
        context.setdefault("domain_name", domain_name)
        context.setdefault("domain_web_pages", domain_web_pages)
        context.setdefault("domain_dashboard_widgets", domain_dashboard_widgets)

        # ── 部署形态 + 开发者模式（L-4 A 2026-09-06，D-L2）────────────
        # ui_flavor：client / partner / internal（ui_visibility.resolve_ui_flavor）；
        # ui_developer_mode：session 级（/developer 密码闸内开关，退出即关）；
        # ui_client_hide：client 形态且未开开发者模式＝「用户版隐藏」生效——模板藏
        # 研发/代理商面（settings 三卡、help/strategies/singing/voice_eval 页内入口、
        # L-2 人设页三处）都读这一个布尔，与下方导航剔除同口径。异常回落
        # internal / False（fail-internal：不误藏内部机器的入口）。
        _ui_flavor, _ui_dev_mode, _ui_client_hide = "internal", False, False
        try:
            from src.web.ui_visibility import (client_hide_active,
                                               resolve_developer_mode,
                                               resolve_ui_flavor)
            _ui_cfg = getattr(config_manager, "config", None)
            _ui_flavor = resolve_ui_flavor(_ui_cfg)
            try:
                _ui_dev_mode = resolve_developer_mode(request.session)
            except Exception:
                _ui_dev_mode = False
            _ui_client_hide = client_hide_active(_ui_cfg, _ui_dev_mode)
        except Exception:
            pass
        context.setdefault("ui_flavor", _ui_flavor)
        context.setdefault("ui_developer_mode", _ui_dev_mode)
        context.setdefault("ui_client_hide", _ui_client_hide)

        # ── 侧栏导航单源数据(nav_schema)──────────────────────
        # 融合实例 P1/P3：传 config 让档位锁定项渲染锁标（gate 关 = 静态全量，零变化）
        from src.web.nav_schema import get_nav_context
        for _k, _v in get_nav_context(
                getattr(config_manager, "config", None),
                developer_mode=_ui_dev_mode).items():
            context.setdefault(_k, _v)

        # ── 内部功能界面显隐(ui_visibility 单源，2026-08-14)────────
        # 模板消费 ui_vis.{manual_console,group_extract,matrix_nav,group_show,
        # team_collab,ai_settings}（缺省全 False=隐藏；开发者页 /developer 按键
        # 开启走 overlay 热重载）。
        try:
            from src.web.ui_visibility import resolve_ui_visibility
            context.setdefault("ui_vis", resolve_ui_visibility(
                getattr(config_manager, "config", None)))
        except Exception:
            context.setdefault("ui_vis", {})
        # 部署形态旗标（J-4 G 2026-09-05）：client 形态（桌面包 AITR_DESKTOP_MODE /
        # app.desktop_mode / ui_visibility.flavor=client）→ 模板据此不渲染只有内部
        # 运维能处置的 nag（首个消费方 _alertlink_connect.html 的「告警通道未接通」）。
        # 判定单源 ui_visibility.is_client_flavor（与导航隐藏同口径）；异常回落
        # False＝不隐藏，与 resolve_ui_flavor 的 fail-internal 方向一致。
        try:
            from src.web.ui_visibility import is_client_flavor
            context.setdefault("ui_client_flavor", bool(is_client_flavor(
                getattr(config_manager, "config", None))))
        except Exception:
            context.setdefault("ui_client_flavor", False)

        # 坐席规模：单人部署藏「认领/释放」协作原语（B13）；藏而不废，API 不封。
        context.setdefault("ws_multi_seat", _multi_seat_now())

        # ── 档位徽章(P3)：仅 feature_gate 开启时出现在顶栏,默认部署零变化 ──
        # L-4 B（#197 / D-L7）授权口径同源：徽标与系统设置「授权」卡、/api/admin/license
        # 都读 gate_snapshot 的 plan + plan_source。plan_override（内测种子 flagship）
        # 生效而无授权文件时，徽标不再裸显「旗舰版」，改 mb_plan_<plan>_override
        # （「旗舰版（厂商自营）」）——两面从此说同一句话。
        try:
            from src.licensing.feature_gate import badge_label_key as _fg_lk
            from src.licensing.feature_gate import gate_enabled as _fg_on
            from src.licensing.feature_gate import gate_snapshot as _fg_snap
            _fg_cfg = getattr(config_manager, "config", None)
            if _fg_on(_fg_cfg):
                _snap = _fg_snap(_fg_cfg)
                _plan = str(_snap.get("plan") or "community")
                _src = str(_snap.get("plan_source") or "community")
                context.setdefault(
                    "plan_badge",
                    {"plan": _plan, "label_key": _fg_lk(_plan, _src), "source": _src})
            else:
                # 显式占位 None：templates 是模块级单例，多次 create_app 会层层
                # 叠 i18n_render 包装（测试常态）——条件性缺席的键会被旧 app 闭包
                # 的 setdefault 补上（陈旧档位徽章漏进本 app 渲染）。占位即封口。
                context.setdefault("plan_badge", None)
        except Exception:
            pass

        # ── 悬浮提示词典单源数据(help_terms,原 base.html 内联 TERM_DICT)──
        from src.web.help_terms import get_help_terms
        context.setdefault("help_terms", get_help_terms())

        # ── Embedded mode（用于 AI 工作室 iframe 嵌入，去掉外层 chrome）──
        try:
            qs_emb = request.query_params.get("embedded", "") == "1"
        except Exception:
            qs_emb = False
        context.setdefault("embedded", qs_emb)

        return context

    def i18n_render(request: Request, name: str, context: dict = None, **kwargs):
        """新签名：i18n_render(request, template_name, context_dict)"""
        ctx = dict(context or {})
        _enrich_context(request, ctx)
        return old_render(request, name, ctx, **kwargs)

    templates.TemplateResponse = i18n_render

    # ── 自动快照 helper ───────────────────────────────────────
    _SNAP_MAX = 15  # 每个前缀保留的最大快照数量

    def _auto_snapshot(prefix: str, content: str, actor: str = "web_admin") -> str | None:
        """
        在 {config_dir}/snapshots/ 中创建自动快照。
        文件名：{prefix}_{YYYYMMDD_HHMMSS}_{actor}.yaml
        超出 _SNAP_MAX 时自动删除最旧的快照（滚动保留）。
        返回快照 stem（无扩展名），失败时返回 None。
        """
        import re as _re
        try:
            cfg_dir = config_manager.config_path.parent
            snap_dir = cfg_dir / "snapshots"
            snap_dir.mkdir(parents=True, exist_ok=True)
            from datetime import datetime as _dt
            ts = _dt.now().strftime("%Y%m%d_%H%M%S")
            safe_actor = _re.sub(r"[^\w\-]", "_", actor or "sys")[:20]
            stem = f"{prefix}_{ts}_{safe_actor}"
            snap_file = snap_dir / f"{stem}.yaml"
            snap_file.write_text(content, encoding="utf-8")
            # 滚动清理：同一 prefix 的快照超出上限时删旧的
            all_snaps = sorted(
                [f for f in snap_dir.glob(f"{prefix}_*.yaml")],
                key=lambda f: f.stat().st_mtime
            )
            while len(all_snaps) > _SNAP_MAX:
                oldest = all_snaps.pop(0)
                try:
                    oldest.unlink()
                except OSError:
                    pass
            return stem
        except Exception:
            return None

    @app.get("/set_lang")
    async def set_lang(request: Request, lang: str = "zh"):
        resp = RedirectResponse(request.headers.get("referer", "/"), status_code=303)
        # auto = 回到「跟随系统」（2026-08-27）：删 cookie + 清落库偏好 →
        # 中间件按 Accept-Language 推断，坐席换系统语言界面即跟走。
        if lang == "auto":
            resp.delete_cookie("ui_lang")
            try:
                _uname = request.session.get("username", "")
                if _uname:
                    user_store.set_lang(_uname, "")
            except Exception:
                pass
            return resp
        # 白名单内才写 cookie（此前无条件写——脏值 cookie 会让中间件误判
        # 「有显式选择」的语义边界；现在脏值=纯 no-op 重定向）。
        if lang in UI_LANGS:
            resp.set_cookie("ui_lang", lang, max_age=365 * 86400)
            # 语言跟人走：登录用户切换语言时落库个人偏好，下次任意设备登录自动套用。
            try:
                _uname = request.session.get("username", "")
                if _uname:
                    user_store.set_lang(_uname, lang)
            except Exception:
                pass  # 落库失败不挡切换（cookie 已生效）
        return resp

    @app.get("/set_ui_mode")
    async def set_ui_mode(request: Request, mode: str = "", next: str = ""):
        if mode not in (UI_MODE_SIMPLE, UI_MODE_FULL):
            mode = UI_MODE_SIMPLE
        redirect_to = next.strip() if next.strip() else ""
        if not redirect_to:
            referer = request.headers.get("referer", "")
            if referer:
                from urllib.parse import urlparse
                parsed = urlparse(referer)
                redirect_to = parsed.path or "/"
            else:
                redirect_to = "/cases" if mode == UI_MODE_SIMPLE else "/"
        if mode == UI_MODE_SIMPLE and redirect_to.rstrip("/") in ("", "/"):
            redirect_to = "/cases"
        resp = RedirectResponse(redirect_to, status_code=303)
        resp.set_cookie("ui_mode", mode, max_age=365 * 86400)
        # 观察期读数（2026-08-03 简洁模式精简）：两周内 grep app.log 的 [ui-mode]
        # 看「切完整模式找功能」的频次，评估精简/兜底是否到位；纯日志零新基建。
        try:
            logger.info("[ui-mode] switch -> %s (user=%s role=%s to=%s)",
                        mode, request.session.get("username", "?"),
                        request.session.get("role", "?"), redirect_to)
        except Exception:
            pass
        return resp

    def _check_session_valid(request: Request) -> bool:
        """检查 session jti 是否有效（未被撤销）"""
        jti = request.session.get("jti")
        if not jti:
            return True  # 老式 session（无 jti），兼容过渡期
        return user_store.touch_session(jti)

    # 坐席（agent）角色作用域：仅限聊天工作台，进不去任何后台设置/页面。
    # 在 _require_auth / _api_auth 两个鉴权 choke point 内强制（避免 middleware 顺序问题）。
    def _agent_page_allowed(path: str) -> bool:
        return path == "/workspace" or path.startswith("/workspace")

    def _agent_api_allowed(path: str) -> bool:
        return (path.startswith("/api/unified-inbox")
                or path.startswith("/api/workspace")
                or path.startswith("/api/drafts")
                or path.startswith("/api/voice/tts-test")
                # AI 助手悬浮球（2026-08-19）：坐席可问答/报障
                or path.startswith("/api/assistant")
                # 客服支持通道（2026-08-20 P1-9）：机器码自查 + 一键诊断直传。
                # 报障的主力恰恰是坐席，而诊断直传原先只挂在 /api/admin/* 下
                # → 坐席点了必 403。刻意另起 support 命名空间而非放开 admin 前缀。
                or path.startswith("/api/support")
                # 顺修存量缺陷：坐席前端错误 beacon 此前被 403 静默丢失
                or path.startswith("/api/telemetry"))

    def _require_auth(request: Request):
        # 无用户且无 token → 引导至首次设置向导
        if user_store.user_count() == 0 and not token:
            raise HTTPException(status_code=303, headers={"Location": "/setup"})
        from src.web.login_redirect import login_redirect_location
        _login_loc = login_redirect_location(
            request.url.path or "/", request.url.query or "")
        if request.session.get("user_id"):
            if not _check_session_valid(request):
                request.session.clear()
                raise HTTPException(status_code=303, headers={"Location": _login_loc})
        elif token and request.session.get("auth") == token:
            if not _check_session_valid(request):
                request.session.clear()
                raise HTTPException(status_code=303, headers={"Location": _login_loc})
        else:
            raise HTTPException(status_code=303, headers={"Location": _login_loc})
        # 已认证：agent 角色只能停留在工作台，其余页面一律跳回 /workspace
        if request.session.get("role", "") == ROLE_AGENT:
            path = request.url.path.rstrip("/") or "/"
            if not _agent_page_allowed(path):
                raise HTTPException(status_code=303, headers={"Location": "/workspace"})

    def _api_auth(request: Request):
        def _agent_guard():
            if request.session.get("role", "") == ROLE_AGENT:
                if not _agent_api_allowed(request.url.path):
                    raise HTTPException(status_code=403, detail="坐席角色仅限工作台相关接口")
        if request.session.get("user_id"):
            if not _check_session_valid(request):
                request.session.clear()
                raise HTTPException(status_code=401, detail="Session 已失效，请重新登录")
            _agent_guard()
            return
        # S1：空 auth_token 不再 fail-open。仅「全新安装（尚无任何用户）+ 本机请求」
        # 放行，供 /setup 向导创建首个 master；其余一律 401（消除 API 裸奔）。
        if not token:
            _ch = request.client.host if request.client else ""
            if user_store.user_count() == 0 and _ch in ("127.0.0.1", "::1", "localhost"):
                return
            raise HTTPException(status_code=401, detail="Unauthorized")
        auth_header = request.headers.get("Authorization", "")
        if hmac.compare_digest(auth_header, f"Bearer {token}"):
            return
        # worker 窄令牌：只放行 /api/internal/*（协议侧车的全部所需），其余一律 403。
        # 刻意用 403 而不是 401：令牌是真的、只是越权，说「没认证」会让排障的人去查
        # 令牌对不对，而真正该看的是「这个端点不在 worker 的授权面里」。
        # 未配置（worker_token 为空）时本分支永不进入 = 与改动前逐字节等价。
        if worker_token and hmac.compare_digest(auth_header, f"Bearer {worker_token}"):
            if request.url.path.startswith("/api/internal/"):
                return
            raise HTTPException(
                status_code=403,
                detail="worker 令牌仅授权 /api/internal/*，此端点需管理员令牌")
        sess = request.session.get("auth")
        if sess and hmac.compare_digest(str(sess), str(token)):
            if not _check_session_valid(request):
                request.session.clear()
                raise HTTPException(status_code=401, detail="Session 已失效，请重新登录")
            _agent_guard()
            return
        raise HTTPException(status_code=401, detail="Unauthorized")

    def _require_role(request: Request, page_key: str):
        _require_auth(request)
        role = request.session.get("role", ROLE_MASTER)
        if not user_store.can_access_page(role, page_key):
            raise HTTPException(status_code=403, detail="无权访问此页面")

    # 暴露鉴权闭包到 app.state，供 main.py 在 create_app 之后挂载的子路由
    # （drafts / contacts / ecommerce）复用同一鉴权 choke point。
    # 历史缺陷：这些子路由的 _drafts_api_auth/_contacts_api_auth 依赖 state.require_role，
    # 但该属性从未被挂载 → hasattr 恒为 False → 外层鉴权空操作（坐席端点无鉴权）。
    # 此处补齐挂载，使子路由统一走 _api_auth（含 agent 白名单与登录校验），
    # 主管专属端点仍由各路由内部 _is_supervisor 守卫。
    app.state.api_auth = _api_auth
    app.state.require_role = _require_role
    # P2（2026-08-16 管理面改造）：暴露用户存储——路由层能力守卫（按人权限覆写
    # resolve_user_perm）与坐席额度闸（check_request_quota 查 monthly_char_quota）
    # 都要按登录身份查 web_users 行；缺失时守卫一律 fail-open（宁可漏拦不误伤）。
    app.state.user_store = user_store

    _PATH_TO_PAGE = {
        "/": "dash", "/templates": "tpl", "/templates/update": "tpl",
        "/strategies": "strategies", "/strategy-analytics": "strategies",
        "/audit": "audit", "/audit/export": "audit",
        "/help": "help", "/diff": "diff",
        "/logs": "logs", "/logs/stream": "logs",
        "/analytics": "analytics",
        "/import": "import", "/export": "export",
        "/cases": "cases", "/episodic-memory": "episodic",
        "/crisis-audit": "crisis_audit",
        "/care-schedule": "care",
        "/relations-health": "care",
        "/monetization": "monetization",
        "/line-rpa": "line_rpa",
        "/workspace": "workspace",
        # 自动回复设置页与「回复策略」同受众（master/admin），共用权限键
        "/reply-settings": "strategies",
    }
    for _dp in domain_web_pages:
        _PATH_TO_PAGE[_dp["path"]] = _dp["key"]
        _PATH_TO_PAGE[_dp["path"] + "/update"] = _dp["key"]

    # ── SSE 端点（需 _api_auth 已定义）──────────────────────
    @app.get("/api/events")
    async def sse_events(request: Request):
        _api_auth(request)
        q: _asyncio.Queue = _asyncio.Queue(maxsize=50)
        _sse_clients.append(q)

        async def _gen():
            try:
                while True:
                    try:
                        msg = await _asyncio.wait_for(q.get(), timeout=30)
                        import json as _j
                        yield f"event: {msg.get('event','message')}\ndata: {_j.dumps(msg)}\n\n"
                    except _asyncio.TimeoutError:
                        yield ": keepalive\n\n"
                    if await request.is_disconnected():
                        break
            finally:
                if q in _sse_clients:
                    _sse_clients.remove(q)

        return StreamingResponse(_gen(), media_type="text/event-stream",
                                 headers={
                                     "Cache-Control": "no-cache, no-transform",
                                     "X-Accel-Buffering": "no",
                                     "Connection": "keep-alive",
                                 })

    def _api_write(perm: str):
        def _check(request: Request):
            _api_auth(request)
            role = request.session.get("role", "")
            if not role:
                auth_h = request.headers.get("Authorization", "")
                if token and (auth_h == f"Bearer {token}" or request.session.get("auth") == token):
                    role = ROLE_MASTER
            if not user_store.can_write(role, perm):
                raise HTTPException(403, "无权执行此操作")
        return _check

    def _page_auth(request: Request):
        _require_auth(request)
        path = request.url.path.rstrip("/") or "/"
        page_key = _PATH_TO_PAGE.get(path)
        if page_key:
            role = request.session.get("role", "")
            if not role and token and request.session.get("auth") == token:
                role = ROLE_MASTER
            if not user_store.can_access_page(role, page_key):
                raise HTTPException(status_code=403, detail="无权访问此页面")

    # ── 认证/用户/会话路由（Phase E1：抽到 routes/auth_user_routes.py） ──
    try:
        from src.web.routes.auth_user_routes import register_auth_user_routes

        register_auth_user_routes(
            app,
            user_store=user_store,
            token=token,
            config_manager=config_manager,
            audit_store=audit_store,
            require_auth=_require_auth,
            require_role=_require_role,
        )
    except Exception:
        import logging as _log_au

        _log_au.getLogger("admin").warning(
            "auth_user 路由注册失败", exc_info=True
        )

    # ── C0-1 授权状态只读 API ──────────────────────────────
    try:
        from src.web.routes.license_routes import register_license_routes

        register_license_routes(app, api_auth=_api_auth,
                                config_manager=config_manager)
    except Exception:
        import logging as _log_lic

        _log_lic.getLogger("admin").warning("license 路由注册失败", exc_info=True)

    # ── 融合实例 P3：会员中心（档位/矩阵/用量/到期）─────────
    try:
        from src.web.routes.membership_routes import register_membership_routes

        register_membership_routes(
            app, templates=templates, page_auth=_page_auth,
            api_auth=_api_auth, config_manager=config_manager,
            user_store=user_store)
    except Exception:
        import logging as _log_mb

        _log_mb.getLogger("admin").warning("membership 路由注册失败", exc_info=True)

    # ── WP-2 首启向导 /welcome 与 WP-7 坐席新手任务已退役（2026-08-31 新手引导删除）：
    #    路由不再注册（访问=404）；模块文件与状态文件的物理清理见二期批。──

    # ── WP-4：合规只读导出（危机转介计数 + 开关回显；写入面在危机处置链打点）──
    try:
        from src.web.routes.compliance_routes import register_compliance_routes

        register_compliance_routes(
            app, api_auth=_api_auth, config_manager=config_manager)
    except Exception:
        import logging as _log_cmp

        _log_cmp.getLogger("admin").warning("compliance 路由注册失败", exc_info=True)
    # 合规开关的进程级读取面（persona_manager prompt 组装等消费点惰性读；
    # provider 指向实时合并配置=跟随 overlay 热重载；失败=开关恒关，零风险）
    try:
        from src.compliance.runtime import set_config_provider

        set_config_provider(
            lambda: getattr(config_manager, "config", None) or {})
    except Exception:
        pass
    # 出站策略单一开关 outbound.unlimited_mode（2026-09-04）：与合规读取面同范式，
    # skill 冷却 / S5 概率 / 主动触达叠加降频 / 各日配额消费点惰性读实时配置。
    try:
        from src.ops.outbound_policy import (
            set_config_provider as _set_outbound_cfg_provider,
        )

        _set_outbound_cfg_provider(
            lambda: getattr(config_manager, "config", None) or {})
    except Exception:
        pass


    # ── C1-1 白标品牌设置 API ──────────────────────────────
    try:
        from src.web.routes.branding_routes import register_branding_routes

        app.state.branding_refresh = app_branding_refresh
        register_branding_routes(app, api_auth=_api_auth, config_manager=config_manager)
    except Exception:
        import logging as _log_br

        _log_br.getLogger("admin").warning("branding 路由注册失败", exc_info=True)

    # ── C1-2 试用/Demo 数据 API ────────────────────────────
    try:
        from src.web.routes.demo_routes import register_demo_routes

        register_demo_routes(app, api_auth=_api_auth, config_manager=config_manager)
    except Exception:
        import logging as _log_demo

        _log_demo.getLogger("admin").warning("demo 路由注册失败", exc_info=True)

    # ── D1 运行时健康检查 API（/api/admin/health）──────────
    try:
        from src.web.routes.runtime_health_routes import register_runtime_health_routes

        register_runtime_health_routes(app, api_auth=_api_auth, config_manager=config_manager)
    except Exception:
        import logging as _log_rhealth

        _log_rhealth.getLogger("admin").warning("runtime health 路由注册失败", exc_info=True)

    # ── Phase O4 主动关怀待办 API（/api/care/schedule*）──────
    try:
        from src.web.routes.care_routes import register_care_routes

        register_care_routes(app, api_auth=_api_auth, config_manager=config_manager)
    except Exception:
        import logging as _log_care

        _log_care.getLogger("admin").warning("care 路由注册失败", exc_info=True)

    # ── 多平台 deferred 队列·运营可观测 API（/api/deferred-outbox/status）──
    try:
        from src.web.routes.deferred_outbox_routes import (
            register_deferred_outbox_routes,
        )

        register_deferred_outbox_routes(app, api_auth=_api_auth)
    except Exception:
        import logging as _log_dob

        _log_dob.getLogger("admin").warning("deferred-outbox 路由注册失败", exc_info=True)

    # （外链 i18n 词典包 /i18n/ws-i18n.js 与静态壳切片 /api/i18n/bundle 同在
    #   i18n_bundle_routes 模块，由下方既有的 register_i18n_bundle_routes 一次挂载。）

    # ── platform/leadbus 线索接收承接端 API（/api/leadbus/ingest、/status）──
    # 无界融合期新增（2026-07 S1）：上游获客(智控王/huoke)经 platform/leadbus 契约
    # 把线索交给本引擎承接；见 D:\boundless\platform\leadbus\CONTRACT.md。
    try:
        from src.web.routes.leadbus_routes import register_leadbus_routes

        register_leadbus_routes(app, api_auth=_api_auth, config_manager=config_manager)
    except Exception:
        import logging as _log_lb

        _log_lb.getLogger("admin").warning("leadbus 路由注册失败", exc_info=True)

    # ── platform/replybus 承接决策回执 API（/api/replybus/decide、/status）──
    # 无界融合期新增（2026-07 S3）：智控王入站私信先问本引擎「这条怎么回」，
    # 本引擎只返回决策，绝不代发（防双发红线，见 platform/replybus/CONTRACT.md §5）。
    try:
        from src.web.routes.replybus_routes import register_replybus_routes

        register_replybus_routes(app, api_auth=_api_auth, config_manager=config_manager)
    except Exception:
        import logging as _log_rb

        _log_rb.getLogger("admin").warning("replybus 路由注册失败", exc_info=True)

    # ── platform/enable 契约 · translate 部分（/api/translate、/api/enable/status）──
    # 无界融合期新增（2026-07 S2）：承接侧/智控王经 platform/enable 契约调用本引擎
    # 翻译栈；见 D:\boundless\platform\enable\CONTRACT.md。
    try:
        from src.web.routes.enable_routes import register_enable_routes

        register_enable_routes(app, api_auth=_api_auth, config_manager=config_manager)
    except Exception:
        import logging as _log_enable

        _log_enable.getLogger("admin").warning("enable(translate) 路由注册失败", exc_info=True)

    # ── Phase K2 C 端变现 API（/api/monetize/*）──────────────────────────
    try:
        from src.web.routes.monetization_routes import register_monetization_routes

        register_monetization_routes(app, api_auth=_api_auth, config_manager=config_manager)
    except Exception:
        import logging as _log_mon2

        _log_mon2.getLogger("admin").warning("monetize 路由注册失败", exc_info=True)

    # ── 陪伴主动话题·可观测预览 API（/api/companion/proactive/preview）──
    try:
        from src.web.routes.companion_proactive_routes import (
            register_companion_proactive_routes,
        )

        register_companion_proactive_routes(app, api_auth=_api_auth)
    except Exception:
        import logging as _log_cpp

        _log_cpp.getLogger("admin").warning("companion proactive 预览路由注册失败", exc_info=True)

    # ── 陪伴能力就绪度看板 API（/api/companion/capabilities）──────────
    try:
        from src.web.routes.companion_capability_routes import (
            register_companion_capability_routes,
        )

        register_companion_capability_routes(app, api_auth=_api_auth)
    except Exception:
        import logging as _log_ccap

        _log_ccap.getLogger(__name__).warning(
            "companion capability routes 注册失败", exc_info=True)

    # ── 被骂回怼治理 API（/api/companion/temper/*）────────────────────
    try:
        from src.web.routes.temper_routes import register_temper_routes

        register_temper_routes(
            app, api_auth=_api_auth, config_manager=config_manager)
    except Exception:
        import logging as _log_temper

        _log_temper.getLogger(__name__).warning(
            "temper 治理路由注册失败", exc_info=True)

    # 系统状态/指标/reactivation dry-run/审计热力图 已抽到 routes/monitoring_routes.py（批 G2-①）
    # （register_monitoring_routes 在 _admin_ctx + kb_store 就绪后调用，见下方 learner 注册附近）

    # ── 全局系统配置 ──────────────────────────────────────────

    # /settings 页已抽到 routes/settings_routes.py（见下方 register_settings_routes）

    # /developer 页面已抽到 routes/developer_page_routes.py（_admin_ctx 就绪后注册）

    # /api/settings/save 与 /api/reply-logic 已抽到 routes/settings_routes.py

    # ── 人工转接路由（Phase E1：抽到 routes/human_escalation_routes.py） ──
    try:
        from src.web.routes.human_escalation_routes import (
            register_human_escalation_routes,
        )

        register_human_escalation_routes(
            app,
            api_auth=_api_auth,
            api_write=_api_write,
            config_manager=config_manager,
            telegram_client=telegram_client,
            audit_store=audit_store,
        )
    except Exception:
        import logging as _log_he

        _log_he.getLogger("admin").warning(
            "human_escalation 路由注册失败", exc_info=True
        )

    # intent-keywords / test-intent / test-webhook 已抽到 routes/settings_routes.py

    # ── 知识库分析报告 ────────────────────────────────────────

    # kb/report + kb-images/{filename} 已抽到 routes/kb_routes.py（批 5L）

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request, _=Depends(_page_auth)):
        # 简洁模式首页 → 直接到 Cases 工作队列
        _role = request.session.get("role", "")
        if not _role and token and request.session.get("auth") == token:
            _role = ROLE_MASTER
        _eff_mode = resolve_ui_mode(request.cookies.get("ui_mode", ""), _role)
        if _eff_mode == UI_MODE_SIMPLE:
            return RedirectResponse("/cases", status_code=303)

        import asyncio as _aio

        def _build_dashboard_data():
            tpl_data = config_manager.get_dynamic_templates_config() or {}
            rates_data = config_manager.get_exchange_rates_config() or {}
            channels = rates_data.get("channels", {})

            # 多取一些原始行供展示层聚合（批量操作逐条记账 → 折叠成一行 ×N）
            recent_audit = []
            if audit_store:
                recent_audit = audit_store.query(limit=60)

            _has_ch_widget = any(w.get("key") == "channel_health" for w in domain_dashboard_widgets)
            health = []
            if _has_ch_widget and channels:
                from src.utils.channel_health import compute_health_scores
                health = compute_health_scores(channels, event_tracker)

            # 聚合 + 按日分段 + 今日摘要（纯函数，见 src/web/audit_display.py）
            from src.web.audit_display import (
                build_recent_groups, group_days, summarize_actions,
            )
            _today = time.strftime("%Y-%m-%d")
            _yesterday = time.strftime(
                "%Y-%m-%d", time.localtime(time.time() - 86400))
            recent_op_days = group_days(
                build_recent_groups(recent_audit, max_groups=8),
                today=_today, yesterday=_yesterday)
            audit_today = {"total": 0, "danger": 0}
            if audit_store:
                audit_today = summarize_actions(
                    audit_store.actions_since(_today + " 00:00:00"))
            return tpl_data, channels, recent_op_days, audit_today, health

        (tpl_data, channels, recent_op_days, audit_today,
         health) = await _aio.to_thread(_build_dashboard_data)

        uptime = int(time.time() - boot_ts) if boot_ts else 0
        hours, remainder = divmod(uptime, 3600)
        mins, secs = divmod(remainder, 60)
        uptime_str = f"{hours}h {mins}m {secs}s"

        from src.web.audit_display import operator_display_names

        return templates.TemplateResponse(request, "dashboard.html", {
            "templates": tpl_data,
            "channels": channels,
            "recent_op_days": recent_op_days,
            "audit_today": audit_today,
            "operator_names": operator_display_names(user_store),
            "uptime": uptime_str,
            "uptime_hours": hours,
            "template_count": len(tpl_data),
            "channel_count": len(channels),
            "health": health,
            "domain_name": domain_name,
        })

    @app.get("/templates", response_class=HTMLResponse)
    async def templates_page(request: Request, _=Depends(_page_auth)):
        data = config_manager.get_dynamic_templates_config() or {}
        return templates.TemplateResponse(request, "templates.html", {
            "templates": data, "msg": "", "msg_ok": True
        })

    @app.post("/templates/update")
    async def templates_update(request: Request, _=Depends(_page_auth),
                               key: str = Form(...), value: str = Form(...)):
        data = config_manager.get_dynamic_templates_config() or {}
        # 保存前先拍快照
        snap_content = yaml.dump(data, allow_unicode=True, default_flow_style=False)
        lines = [l.strip() for l in value.strip().split("\n") if l.strip()]
        data[key] = lines if len(lines) > 1 else (lines[0] if lines else "")
        ok, msg = config_manager.save_templates(data)
        if ok:
            config_manager.invalidate_templates_cache()
            actor = request.session.get("username", "web_admin")
            snap_id = _auto_snapshot("templates", snap_content, actor) or ""
            if audit_store:
                audit_store.log(actor, "update_template", key, "",
                                (value or "")[:100], snap_id)
            import asyncio as _asyncio
            try:
                _asyncio.get_running_loop().create_task(_fire_webhook(
                    "config_change", actor, f"templates.{key}", f"模板 {key} 已更新"))
            except RuntimeError:
                pass
            data = config_manager.get_dynamic_templates_config() or {}
        if "application/json" in request.headers.get("accept", ""):
            return {"ok": ok, "msg": f"已保存 {key}" if ok else f"保存失败: {msg}"}
        return templates.TemplateResponse(request, "templates.html", {
            "templates": data,
            "msg": f"已保存 {key}" if ok else f"保存失败: {msg}",
            "msg_ok": ok,
        })

    # ── AI 策略管理 ───────────────────────────────────────────

    # 意图 key → 中文显示名（策略页「意图→策略映射」与「关联意图」用）
    _INTENT_DISPLAY_NAMES_BASE = {
        "greeting": "问候",
        "test": "测试/自检",
        "complaint": "投诉处理",
        "small_talk": "闲聊",
    }

    def _get_intent_display_names() -> dict:
        merged = dict(_INTENT_DISPLAY_NAMES_BASE)
        merged.update(getattr(app.state, "intent_display_names_extra", {}))
        return merged

    # ── 浏览器环境体检（P2：写通道/时钟/构建戳零副作用探针，golive 页消费）──
    try:
        from src.web.routes.preflight_routes import register_preflight_routes
        register_preflight_routes(app, auth_dep=_api_auth)
    except Exception:
        import logging as _log_pf
        _log_pf.getLogger("admin").warning("preflight 路由注册失败", exc_info=True)

    # ── Persona Studio (/personas) ───────────────────────────
    try:
        from src.web.routes.persona_routes import register_persona_routes
        register_persona_routes(
            app, auth_dep=_api_auth, audit_store=audit_store, config_manager=config_manager
        )
    except Exception:
        import logging as _log_pr
        _log_pr.getLogger("admin").debug("Persona API 路由注册跳过", exc_info=True)

    # ── Persona doc import（「从文档创建人设」LLM 抽取）──────
    try:
        from src.web.routes.persona_import_routes import register_persona_import_routes
        register_persona_import_routes(
            app, auth_dep=_api_auth, audit_store=audit_store, config_manager=config_manager
        )
    except Exception:
        import logging as _log_pi
        _log_pi.getLogger("admin").debug("Persona 文档导入路由注册跳过", exc_info=True)

    # ── Persona quiz（人设一致性考题：档案出题 → 真人设 prompt 实测 → 判分）──
    try:
        from src.web.routes.persona_quiz_routes import register_persona_quiz_routes
        register_persona_quiz_routes(
            app, auth_dep=_api_auth, audit_store=audit_store, config_manager=config_manager
        )
    except Exception:
        import logging as _log_pq
        _log_pq.getLogger("admin").debug("Persona 考题路由注册跳过", exc_info=True)

    try:
        from src.web.routes.persona_media_routes import register_persona_media_routes
        register_persona_media_routes(
            app, auth_dep=_api_auth, audit_store=audit_store,
            config_manager=config_manager
        )
    except Exception:
        import logging as _log_pm
        _log_pm.getLogger("admin").debug("Persona media 路由注册跳过", exc_info=True)

    # ── 表情包（贴纸）：包/条目管理 + 收藏入包 + 官方包播种 + 跨平台发送 ──
    try:
        from src.web.routes.sticker_routes import register_sticker_routes
        register_sticker_routes(
            app, auth_dep=_api_auth, audit_store=audit_store,
            config_manager=config_manager
        )
    except Exception:
        import logging as _log_stk
        _log_stk.getLogger("admin").debug("表情包路由注册跳过", exc_info=True)

    # ── Telegram 群成员提取（工具箱）：多号限速拉群成员入库 + 每日配额 ──
    try:
        from src.web.routes.group_members_routes import register_group_members_routes
        register_group_members_routes(
            app, auth_dep=_api_auth, audit_store=audit_store,
            config_manager=config_manager, page_auth=_page_auth,
        )
    except Exception:
        import logging as _log_gm
        _log_gm.getLogger("admin").debug("Telegram 群成员提取路由注册跳过", exc_info=True)

    # ── 工具箱「AI 生成图片」（cp-image）：坐席手动出图 → VLM 后验 → 发送/存册 ──
    try:
        from src.web.routes.image_gen_routes import register_image_gen_routes
        register_image_gen_routes(
            app, auth_dep=_api_auth, audit_store=audit_store,
            config_manager=config_manager, page_auth=_page_auth,
        )
    except Exception:
        import logging as _log_img
        _log_img.getLogger("admin").debug("AI 生成图片路由注册跳过", exc_info=True)

    # ── 工具箱「智能养号」（cp-nurture）：机群养护状态 + 用户自配养护方案 ──
    try:
        from src.web.routes.nurture_routes import register_nurture_routes
        register_nurture_routes(
            app, auth_dep=_api_auth, audit_store=audit_store,
            config_manager=config_manager, page_auth=_page_auth,
        )
    except Exception:
        import logging as _log_nur
        _log_nur.getLogger("admin").debug("智能养号路由注册跳过", exc_info=True)

    # ── 营销目标（marketing goals）：会话级工作目标 CRUD + settle-on-read 视图 ──
    try:
        from src.web.routes.goal_routes import register_goal_routes
        register_goal_routes(app, auth_dep=_api_auth, config_manager=config_manager)
    except Exception:
        import logging as _log_goal
        _log_goal.getLogger("admin").warning("营销目标路由注册失败", exc_info=True)

    # ── 静态壳 i18n bundle（D1）：/copilot/app.html 等无 Jinja 宿主按前缀拉词典切片 ──
    try:
        from src.web.routes.i18n_bundle_routes import register_i18n_bundle_routes
        register_i18n_bundle_routes(app, api_auth=_api_auth)
    except Exception:
        import logging as _log_i18nb
        _log_i18nb.getLogger("admin").warning("i18n bundle 路由注册失败", exc_info=True)

    # ── 功能总览（feature center）：交付注册表驱动的能力清单 + overlay 开关 ──
    try:
        from src.web.routes.feature_center_routes import register_feature_center_routes
        register_feature_center_routes(
            app, auth_dep=_api_auth, config_manager=config_manager)
    except Exception:
        import logging as _log_fc
        _log_fc.getLogger("admin").warning("功能总览路由注册失败", exc_info=True)

    # ── 内部功能界面显隐（2026-08-14）：桌面壳 ui-flags + 开发者页写开关 ──
    try:
        from src.web.routes.ui_visibility_routes import register_ui_visibility_routes
        register_ui_visibility_routes(
            app, auth_dep=_api_auth, config_manager=config_manager)
    except Exception:
        import logging as _log_uiv
        _log_uiv.getLogger("admin").warning("ui_visibility 路由注册失败", exc_info=True)

    # ── 人设声音评测台（2026-08-19 由 tmp_voice_eval 转正）：试听/评分/替换候选 ──
    try:
        from src.web.routes.voice_eval_routes import register_voice_eval_routes
        register_voice_eval_routes(
            app, page_auth=_page_auth, api_auth=_api_auth, templates=templates)
    except Exception:
        import logging as _log_vev
        _log_vev.getLogger("admin").warning("voice_eval 路由注册失败", exc_info=True)

    @app.get("/personas", response_class=HTMLResponse)
    async def personas_page(request: Request, _=Depends(_page_auth)):
        from src.utils.persona_manager import PersonaManager
        try:
            pm = PersonaManager.get_instance()
            default_persona = pm.get_persona("")
            bindings = pm.get_all_chat_bindings()
        except Exception:
            default_persona = {}
            bindings = {}
        tg_accounts_stats: dict = {}
        try:
            from src.client.telegram_account_registry import TelegramAccountRegistry
            _tg_cfg = (config_manager.config or {}).get("telegram", {})
            _reg = TelegramAccountRegistry.from_config(_tg_cfg)
            tg_accounts_stats = _reg.stats()
        except Exception:
            pass
        personas_from_cfg = (config_manager.config or {}).get("personas", {})
        return templates.TemplateResponse(request, "personas.html", {
            "default_persona": default_persona,
            "bindings": bindings,
            "personas_cfg": personas_from_cfg,
            "tg_accounts": tg_accounts_stats,
        })

    # ── 歌房（实施58 P1：人设清唱能力管理——开关/备货试听/曲库/声库）──
    @app.get("/singing", response_class=HTMLResponse)
    async def singing_page(request: Request, _=Depends(_page_auth)):
        return templates.TemplateResponse(request, "singing.html", {})

    # ── AI 工作室 (/ai-studio) — 4-Tab 集中入口 ─────────────────────
    @app.get("/ai-studio", response_class=HTMLResponse)
    async def ai_studio_page(request: Request, _=Depends(_page_auth)):
        return templates.TemplateResponse(request, "ai_studio.html", {})

    @app.get("/api/ai-studio/summary")
    async def api_ai_studio_summary(request: Request, _=Depends(_api_auth)):
        """统一统计：人设池 / 情景记忆 / KB草稿 / 关系档案 / 重复人设池告警。"""
        out: Dict[str, Any] = {
            "personas": {"profile_count": 0, "binding_count": 0},
            "memory": {"total_facts": 0, "unique_users": 0, "with_embedding": 0},
            "drafts": {"pending": 0, "approved": 0, "rejected": 0},
            "relations": {"total": 0, "intimate_count": 0, "stages": {}},
            "messenger_rpa_reply_profiles": {"count": 0, "default": ""},
        }

        # 人设池
        try:
            from src.utils.persona_manager import PersonaManager
            pm = PersonaManager.get_instance()
            out["personas"]["profile_count"] = len(pm.list_profile_ids())
            out["personas"]["binding_count"] = len(pm.get_all_chat_bindings())
        except Exception:
            pass

        # 情景记忆
        try:
            from src.web.web_context import resolve_skill_manager
            sm = resolve_skill_manager(telegram_client, app)
            store = getattr(sm, "_episodic_store", None) if sm else None
            if store and store._conn:
                row = store._conn.execute(
                    "SELECT COUNT(*), COUNT(DISTINCT user_id), "
                    "SUM(CASE WHEN embedding IS NOT NULL AND length(embedding)>=8 THEN 1 ELSE 0 END) "
                    "FROM episodic_memory"
                ).fetchone()
                if row:
                    out["memory"]["total_facts"] = int(row[0] or 0)
                    out["memory"]["unique_users"] = int(row[1] or 0)
                    out["memory"]["with_embedding"] = int(row[2] or 0)
        except Exception:
            pass

        # KB 草稿统计（AI 客户端走回落链且可缺席——统计是纯 DB 读）
        try:
            from src.utils.daily_learner import DailyLearner as _DL, resolve_learner_ai as _rla
            learner = getattr(app.state, "_daily_learner", None)
            if learner is None:
                kb = getattr(app.state, "kb_store", None)
                if kb:
                    ai = _rla(app, telegram_client)
                    _kbp = (getattr(app.state, "kb_db_path", None)
                            or getattr(kb, "_db_path", None) or getattr(kb, "db_path", None))
                    learner = _DL(kb, ai, db_path=_kbp)
                    app.state._daily_learner = learner
            if learner:
                out["drafts"] = learner.stats()
        except Exception:
            pass

        # 关系档案统计（直接 SQL）
        try:
            _contacts = getattr(app.state, "contacts", None)
            cs = getattr(_contacts, "store", None) if _contacts else None
            conn = getattr(cs, "_conn", None) if cs else None
            if conn is not None:
                row = conn.execute(
                    "SELECT "
                    "  SUM(CASE WHEN intimacy_score>=80 THEN 1 ELSE 0 END), "
                    "  SUM(CASE WHEN intimacy_score>=55 AND intimacy_score<80 THEN 1 ELSE 0 END), "
                    "  SUM(CASE WHEN intimacy_score>=25 AND intimacy_score<55 THEN 1 ELSE 0 END), "
                    "  SUM(CASE WHEN intimacy_score<25 OR intimacy_score IS NULL THEN 1 ELSE 0 END), "
                    "  COUNT(*) "
                    "FROM journeys"
                ).fetchone()
                if row:
                    soulmate = int(row[0] or 0)
                    close = int(row[1] or 0)
                    friend = int(row[2] or 0)
                    stranger = int(row[3] or 0)
                    out["relations"]["total"] = int(row[4] or 0)
                    out["relations"]["stages"] = {
                        "stranger": stranger, "friend": friend,
                        "close": close, "soulmate": soulmate,
                    }
                    out["relations"]["intimate_count"] = soulmate + close

                # W3-3A.3：reunion 候选 — 曾深度互动（funnel_stage 已过 INITIAL 且非 LOST/MERGE）
                # 但 intimacy_score 因沉默衰减回落到 stranger 区间（<25）的 journey
                # 这些是「需重激活」的优先目标，应在 ai_studio 关系看板单列展示
                try:
                    rc_row = conn.execute(
                        "SELECT COUNT(*) FROM journeys WHERE "
                        "intimacy_score < 25 AND "
                        "funnel_stage NOT IN ('INITIAL','LOST_HANDOFF',"
                        "'LOST_LINE_SILENT','NEEDS_MANUAL_MERGE')"
                    ).fetchone()
                    out["relations"]["reunion_candidates"] = int(
                        (rc_row[0] if rc_row else 0) or 0
                    )
                except Exception:
                    out["relations"]["reunion_candidates"] = 0

                # W3-3A.3：funnel_stage 分布 — 给运营看「漏斗实况」补充 score 分布
                try:
                    fs_rows = conn.execute(
                        "SELECT funnel_stage, COUNT(*) FROM journeys "
                        "GROUP BY funnel_stage"
                    ).fetchall()
                    out["relations"]["funnel_stages"] = {
                        str(r[0] or "INITIAL"): int(r[1] or 0)
                        for r in (fs_rows or [])
                    }
                except Exception:
                    out["relations"]["funnel_stages"] = {}

                # W3-3B.2：merge_review_queue pending 计数 + auto_merge 高分边界候选
                # 让 ai_studio 关系看板与 reunion 一样有 banner 直达
                try:
                    mr_row = conn.execute(
                        "SELECT COUNT(*) FROM merge_review_queue WHERE status='pending'"
                    ).fetchone()
                    out["relations"]["merge_reviews_pending"] = int(
                        (mr_row[0] if mr_row else 0) or 0
                    )
                    # 边界候选：confidence>=0.85 但因歧义降级到 review 的高质量对
                    # 运营优先处理这些（基本确定是同一个人）
                    hc_row = conn.execute(
                        "SELECT COUNT(*) FROM merge_review_queue "
                        "WHERE status='pending' AND confidence >= 0.85"
                    ).fetchone()
                    out["relations"]["merge_reviews_high_conf"] = int(
                        (hc_row[0] if hc_row else 0) or 0
                    )
                except Exception:
                    out["relations"]["merge_reviews_pending"] = 0
                    out["relations"]["merge_reviews_high_conf"] = 0
        except Exception:
            pass

        # Messenger RPA reply_profiles 重复检测
        try:
            mr_cfg = (config_manager.config or {}).get("messenger_rpa", {}) or {}
            rp = mr_cfg.get("reply_profiles") or {}
            profs = rp.get("profiles") if isinstance(rp.get("profiles"), list) else []
            out["messenger_rpa_reply_profiles"] = {
                "count": len([p for p in profs if isinstance(p, dict) and p.get("id")]),
                "default": str(rp.get("default") or ""),
            }
        except Exception:
            pass

        return out

    # ── 策略 / 策略分析 / A-B 测试路由（Phase E1 批 3：抽到 routes/strategy_routes.py） ──
    # 首个采用 AdminRouteContext 的批次：把反复出现的核心依赖打包，后续批次复用 _admin_ctx。
    from src.web.web_context import AdminRouteContext

    _admin_ctx = AdminRouteContext(
        config_manager=config_manager,
        audit_store=audit_store,
        telegram_client=telegram_client,
        user_store=user_store,
        token=token,
        page_auth=_page_auth,
        api_auth=_api_auth,
        api_write=_api_write,
        require_auth=_require_auth,
        require_role=_require_role,
        auto_snapshot=_auto_snapshot,
        get_intent_display_names=_get_intent_display_names,
        event_tracker=event_tracker,
        boot_ts=boot_ts,
        domain_web_pages=domain_web_pages,
        domain_dashboard_widgets=domain_dashboard_widgets,
        templates=templates,
        log_buffer=log_buffer,
    )
    try:
        from src.web.routes.strategy_routes import register_strategy_routes

        register_strategy_routes(app, _admin_ctx)
    except Exception:
        import logging as _log_st

        _log_st.getLogger("admin").warning("strategy 路由注册失败", exc_info=True)

    # ── 设置/回复逻辑/意图关键词路由（Phase E1 批 4：复用 _admin_ctx） ──
    try:
        from src.web.routes.settings_routes import register_settings_routes

        register_settings_routes(app, _admin_ctx)
    except Exception:
        import logging as _log_set

        _log_set.getLogger("admin").warning("settings 路由注册失败", exc_info=True)

    def _get_strategy_tracker():
        # 修复潜伏 bug：strategy_routes 抽出时此闭包未在 admin.py 保留，
        # 导致 data-purge/session-stats/export-strategy/daily-report 调用时 NameError。
        # 与 strategy_routes 内同名实现一致（主客户端 → app.state 双通路）。
        from src.web.web_context import resolve_skill_manager
        sm = resolve_skill_manager(telegram_client, app)
        return getattr(sm, "strategy_tracker", None) if sm else None

    @app.post("/api/data-purge")
    async def api_data_purge(request: Request, _=Depends(_api_write("import_export"))):
        """手动触发数据清理"""
        rs = config_manager.get_strategies_config()
        retention = rs.get("data_retention", {})
        se_days = int(retention.get("strategy_events_days", 30))
        ge_days = int(retention.get("general_events_days", 90))
        tracker = _get_strategy_tracker()
        se_del = tracker.purge(se_days) if tracker else 0
        ge_del = 0
        if event_tracker and hasattr(event_tracker, "purge"):
            ge_del = event_tracker.purge(ge_days)
        if audit_store:
            audit_store.log(request.session.get("username", "web_admin"),
                            "data_purge", "",
                            f"strategy={se_del},events={ge_del}", "")
        return {"ok": True, "strategy_events_deleted": se_del,
                "general_events_deleted": ge_del}

    @app.get("/api/session-stats")
    async def api_session_stats(request: Request, _=Depends(_api_auth),
                                hours: int = Query(24, ge=1, le=720)):
        tracker = _get_strategy_tracker()
        if not tracker:
            return {"total_sessions": 0}
        return tracker.session_stats(hours)

    @app.post("/api/apply-param-suggestion")
    async def api_apply_param(request: Request, _=Depends(_api_write("edit_strategy"))):
        """一键应用参数微调建议"""
        body = await request.json()
        sid = body.get("strategy_id")
        param = body.get("param")
        value = body.get("value")
        if not sid or not param or value is None:
            raise HTTPException(400, "Missing strategy_id, param or value")
        rs = config_manager.get_strategies_config()
        strategies = rs.get("strategies", {})
        if sid not in strategies:
            raise HTTPException(404, f"Strategy '{sid}' not found")
        old_val = strategies[sid].get(param)
        strategies[sid][param] = value
        rs["strategies"] = strategies
        ok, msg = config_manager.save_strategies(rs)
        if not ok:
            raise HTTPException(500, msg)
        from src.web.web_context import resolve_skill_manager
        _sm_hot = resolve_skill_manager(telegram_client, app)
        if _sm_hot and hasattr(_sm_hot, "_refresh_strategies"):
            _sm_hot._refresh_strategies()
        if audit_store:
            audit_store.log(request.session.get("username", "web_admin"),
                            "apply_param_suggestion", f"{sid}.{param}",
                            str(old_val), str(value))
        return {"ok": True, "strategy_id": sid, "param": param,
                "old": old_val, "new": value}

    @app.get("/api/export-strategy-events")
    async def api_export_events(request: Request, _=Depends(_api_auth),
                                fmt: str = Query("csv"),
                                hours: int = Query(168, ge=1, le=8760)):
        """导出策略追踪数据为 CSV 或 JSON"""
        tracker = _get_strategy_tracker()
        if not tracker:
            raise HTTPException(404, "Tracker not available")
        cutoff = time.time() - hours * 3600
        rows = tracker._conn.execute(
            "SELECT * FROM strategy_events WHERE ts_epoch >= ? ORDER BY id",
            (cutoff,),
        ).fetchall()
        records = [dict(r) for r in rows]

        if fmt == "json":
            content = json.dumps(records, ensure_ascii=False, indent=2)
            return StreamingResponse(
                io.BytesIO(content.encode("utf-8")),
                media_type="application/json",
                headers={"Content-Disposition":
                          f"attachment; filename=strategy_events_{hours}h.json"})

        buf = io.StringIO()
        if records:
            w = csv.DictWriter(buf, fieldnames=records[0].keys())
            w.writeheader()
            w.writerows(records)
        return StreamingResponse(
            io.BytesIO(buf.getvalue().encode("utf-8-sig")),
            media_type="text/csv",
            headers={"Content-Disposition":
                      f"attachment; filename=strategy_events_{hours}h.csv"})

    @app.get("/api/autopilot-status")
    async def api_autopilot_status(request: Request, _=Depends(_api_auth)):
        rs = config_manager.get_strategies_config()
        ap = rs.get("autopilot", {})
        return {
            "enabled": ap.get("enabled", False),
            "observation_hours": ap.get("observation_hours", 24),
            "check_interval": ap.get("check_interval_messages", 100),
        }

    @app.put("/api/autopilot")
    async def api_update_autopilot(request: Request, _=Depends(_api_write("edit_strategy"))):
        body = await request.json()
        rs = config_manager.get_strategies_config()
        ap = rs.setdefault("autopilot", {})
        for k in ("enabled", "observation_hours", "check_interval_messages"):
            if k in body:
                ap[k] = body[k]
        rs["autopilot"] = ap
        ok, msg = config_manager.save_strategies(rs)
        if not ok:
            raise HTTPException(500, msg)
        if audit_store:
            audit_store.log(request.session.get("username", "web_admin"),
                            "update_autopilot", "", "", str(body)[:200])
        return {"ok": True}

    # /audit、/audit/export 已抽到 routes/audit_page_routes.py（见下方 register）

    # 信息/日志/分析 页面路由（Phase E1 续拆 → page_routes，经 ctx 注入 templates/log_buffer）
    try:
        from src.web.routes.page_routes import register_page_routes

        register_page_routes(app, _admin_ctx)
    except Exception:
        import logging as _log_pg

        _log_pg.getLogger("admin").warning("page 路由注册失败", exc_info=True)

    try:
        from src.web.routes.developer_page_routes import register_developer_page_routes

        register_developer_page_routes(app, _admin_ctx)
    except Exception:
        import logging as _log_devpg

        _log_devpg.getLogger("admin").warning("developer 页面路由注册失败", exc_info=True)
    try:
        from src.web.routes.audit_page_routes import register_audit_page_routes

        register_audit_page_routes(app, _admin_ctx)
    except Exception:
        import logging as _log_auditpg

        _log_auditpg.getLogger("admin").warning("audit 页面路由注册失败", exc_info=True)
    try:
        from src.web.routes.diff_page_routes import register_diff_page_routes

        register_diff_page_routes(app, _admin_ctx)
    except Exception:
        import logging as _log_diffpg

        _log_diffpg.getLogger("admin").warning("diff 页面路由注册失败", exc_info=True)

    # /diff 页面已抽到 routes/diff_page_routes.py；/api/rollback 仍留本文件

    @app.post("/api/rollback")
    async def api_rollback(request: Request, _=Depends(_api_write("import_export"))):
        body = await request.json()
        snap_id = body.get("snapshot_id", "")
        if not snap_id:
            raise HTTPException(400, "Missing snapshot_id")
        cfg_dir = config_manager.config_path.parent
        snap_file = cfg_dir / "snapshots" / f"{snap_id}.yaml"
        if not snap_file.exists():
            raise HTTPException(404, f"快照 {snap_id} 不存在")
        content = snap_file.read_text(encoding="utf-8")
        yaml.safe_load(content)
        prefix = snap_id.rsplit("_", 1)[0] if "_" in snap_id else ""
        target = None
        # 顺序敏感：templates_i18n 前缀含 "templates" 子串——本分支必须在前，
        # 否则变体快照回滚会把多语言内容覆写进 templates.yaml（机器人话术池被毁）。
        if "templates_i18n" in prefix:
            target = cfg_dir / "templates_i18n.yaml"
        elif "templates" in prefix:
            target = cfg_dir / "templates.yaml"
        elif "exchange_rates" in prefix:
            target = cfg_dir / "exchange_rates.yaml"
        elif "reply_strategies" in prefix:
            target = cfg_dir / "reply_strategies.yaml"
        elif "quota" in prefix:
            target = cfg_dir / "quota_rules.yaml"
        if not target:
            raise HTTPException(400, f"无法确定快照 {snap_id} 对应的配置文件")
        import shutil
        if target.exists():
            bak = target.with_suffix(".yaml.pre_rollback")
            shutil.copy2(target, bak)
        target.write_text(content, encoding="utf-8")
        config_manager.invalidate_templates_cache()
        if hasattr(config_manager, "invalidate_exchange_rates_cache"):
            config_manager.invalidate_exchange_rates_cache()
        if audit_store:
            audit_store.log(request.session.get("username", "web_admin"),
                            "rollback", snap_id, "", target.name)
        return {"ok": True, "restored": target.name, "snapshot": snap_id}

    # ── 通知中心 API ───────────────────────────────────────────
    @app.get("/api/activity-stats")
    async def api_activity_stats(request: Request, _=Depends(_page_auth), hours: int = 24):
        """
        返回过去 N 小时的操作统计，用于仪表盘活动看板：
        - hourly: 每小时操作数（供柱状图使用）
        - by_action: 按操作类型的计数（供饼图使用）
        - top_operators: 活跃操作者 Top 5
        - total: 总操作数
        """
        from datetime import datetime as _dt, timedelta as _td
        if not audit_store:
            return {"hourly": [], "by_action": {}, "top_operators": [], "total": 0, "hours": hours}

        cutoff = _dt.now() - _td(hours=hours)
        cutoff_str = cutoff.strftime("%Y-%m-%dT%H:%M:%S")

        import asyncio as _aio
        entries = await _aio.to_thread(
            lambda: audit_store.query(limit=5000, since=cutoff_str)
        )

        # 按小时聚合
        hourly: dict = {}
        for e in entries:
            ts = str(e.get("ts", ""))
            try:
                hour_key = ts[:13].replace("T", " ")  # "YYYY-MM-DD HH"
                hourly[hour_key] = hourly.get(hour_key, 0) + 1
            except Exception:
                pass

        # 填充缺失小时（保证连续性）
        hourly_series = []
        for i in range(hours - 1, -1, -1):
            t = cutoff + _td(hours=i + 1)
            key = t.strftime("%Y-%m-%d %H")
            hourly_series.append({"hour": key, "label": t.strftime("%H:00"), "count": hourly.get(key, 0)})

        # 按操作类型
        by_action: dict = {}
        for e in entries:
            act = e.get("action", "other") or "other"
            # 归类：update/create/delete/batch/import/rollback/auth
            cat = "其他"
            a = act.lower()
            if "update" in a or "set" in a or "save" in a:
                cat = "更新"
            elif "create" in a or "add" in a or "import" in a:
                cat = "新增"
            elif "delete" in a or "remove" in a or "purge" in a:
                cat = "删除"
            elif "batch" in a:
                cat = "批量"
            elif "login" in a or "logout" in a or "auth" in a or "password" in a:
                cat = "鉴权"
            elif "rollback" in a:
                cat = "回滚"
            by_action[cat] = by_action.get(cat, 0) + 1

        # Top 操作者
        op_cnt: dict = {}
        for e in entries:
            op = str(e.get("user_id", "") or "system")
            op_cnt[op] = op_cnt.get(op, 0) + 1
        top_operators = sorted(op_cnt.items(), key=lambda x: -x[1])[:5]

        return {
            "hourly": hourly_series,
            "by_action": by_action,
            "top_operators": [{"name": k, "count": v} for k, v in top_operators],
            "total": len(entries),
            "hours": hours,
        }

    # 系统健康巡检 / 告警状态 API（Phase E1 续拆 → health_routes）
    # 依赖 domain_web_pages / domain_dashboard_widgets（本轮已纳入 AdminRouteContext）。
    try:
        from src.web.routes.health_routes import register_health_routes

        register_health_routes(app, _admin_ctx)
    except Exception:
        import logging as _log_health

        _log_health.getLogger("admin").warning("health 路由注册失败", exc_info=True)

    # /api/strategy-history/{strategy_id} 已抽到 routes/strategy_routes.py

    # 运营仪表盘只读 API（notifications/snapshots/trigger-decisions）
    # 已抽到 src/web/routes/ops_dashboard_routes.py（Phase E1 续拆）。
    try:
        from src.web.routes.ops_dashboard_routes import register_ops_dashboard_routes

        register_ops_dashboard_routes(app, _admin_ctx)
    except Exception:
        import logging as _log_ops

        _log_ops.getLogger("admin").warning("ops dashboard 路由注册失败", exc_info=True)

    # E 线：运营总览（聚合 ROI/计费/健康/可靠性）+ 运维事件闭环
    try:
        from src.web.routes.ops_overview_routes import register_ops_overview_routes

        register_ops_overview_routes(app, _admin_ctx)
    except Exception:
        import logging as _log_ovw

        _log_ovw.getLogger("admin").warning("ops overview 路由注册失败", exc_info=True)

    # 客服支持通道（P1-9）：机器码/版本自查 + 坐席可用的一键诊断直传
    try:
        from src.web.routes.support_routes import register_support_routes

        register_support_routes(app, _admin_ctx)
    except Exception:
        import logging as _log_sup

        _log_sup.getLogger("admin").warning("support 路由注册失败", exc_info=True)

    # 群脉 CrowdX 导播台：剧本库 / 逐拍详情 / 一键排练（dry-run）/ 历史场次
    try:
        from src.web.routes.group_show_routes import register_group_show_routes

        register_group_show_routes(app, _admin_ctx)
    except Exception:
        import logging as _log_gs

        _log_gs.getLogger("admin").warning("group show 路由注册失败", exc_info=True)

    # ── 告警状态 API ───────────────────────────────────────────
    # ── Webhook 通知 ──────────────────────────────────────────
    # 配置存储路径：{config_dir}/webhook_settings.json
    def _get_webhook_cfg() -> dict:
        try:
            cfg_dir = config_manager.config_path.parent
            wp = cfg_dir / "webhook_settings.json"
            if wp.exists():
                import json as _json
                return _json.loads(wp.read_text(encoding="utf-8"))
        except Exception:
            pass
        return {"url": "", "secret": "", "enabled": False,
                "events": ["config_change", "kb_change", "escalation_needed", "weekly_report"]}

    def _save_webhook_cfg(cfg: dict):
        try:
            cfg_dir = config_manager.config_path.parent
            import json as _json
            (cfg_dir / "webhook_settings.json").write_text(
                _json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception:
            pass

    async def _fire_webhook(event_type: str, actor: str, target: str, summary: str = ""):  # noqa: D401
        """异步发送 Webhook 通知（失败静默，不影响主流程）。

        注：本闭包同步暴露在 ``app.state.fire_webhook`` 上，供其他路由
        模块（如 contacts_routes）按需调用，避免重复实现一遍 webhook 派发。
        """
        cfg = _get_webhook_cfg()
        if not cfg.get("enabled") or not cfg.get("url"):
            return
        if event_type not in cfg.get("events", []):
            return
        import httpx as _httpx, json as _json, hmac as _hmac, hashlib as _hashlib
        payload = {
            "event": event_type,
            "actor": actor,
            "target": target,
            "summary": summary,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        body = _json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json", "X-Bot-Admin-Event": event_type}
        if cfg.get("secret"):
            sig = _hmac.new(cfg["secret"].encode(), body, _hashlib.sha256).hexdigest()
            headers["X-Hub-Signature-256"] = f"sha256={sig}"
        try:
            async with _httpx.AsyncClient(timeout=8) as client:
                resp = await client.post(cfg["url"], content=body, headers=headers)
                resp.raise_for_status()
        except Exception:
            pass  # 静默失败，Webhook 不影响主操作

    # 暴露给其他路由模块复用（如 contacts_routes 的 /api/relations/digest/push）
    app.state.fire_webhook = _fire_webhook

    @app.get("/api/webhook-settings")
    async def api_get_webhook(request: Request, _=Depends(_api_write("import_export"))):
        cfg = _get_webhook_cfg()
        cfg.pop("secret", None)  # 不回传 secret
        return cfg

    @app.put("/api/webhook-settings")
    async def api_put_webhook(request: Request, _=Depends(_api_write("import_export"))):
        body = await request.json()
        cfg = _get_webhook_cfg()
        for k in ("url", "enabled", "events"):
            if k in body:
                cfg[k] = body[k]
        if "secret" in body and body["secret"]:
            cfg["secret"] = body["secret"]
        _save_webhook_cfg(cfg)
        if audit_store:
            audit_store.log(request.session.get("username", "api"), "update_webhook_settings", "", "", "")
        return {"ok": True}

    @app.post("/api/webhook-test")
    async def api_test_webhook(request: Request, _=Depends(_api_write("import_export"))):
        cfg = _get_webhook_cfg()
        if not cfg.get("url"):
            raise HTTPException(400, "Webhook URL 未配置")
        await _fire_webhook("test", request.session.get("username", "admin"), "test", "This is a test notification")
        return {"ok": True, "msg": "测试通知已发送（如未收到请检查 URL 和网络）"}

    # ── 实时日志 ──────────────────────────────────────────────

    # /logs /logs/stream /analytics /cases 页面已抽到 page_routes（见上方 register_page_routes）

    # ── RESTful API ────────────────────────────────────────────

    @app.get("/api/analytics")
    async def analytics_api(request: Request, _=Depends(_api_auth), hours: int = 24):
        if not event_tracker:
            return {"error": "tracker not available"}
        return {
            "cmd_stats": event_tracker.command_stats(hours),
            "hourly": event_tracker.hourly_trend(hours),
            "top_users": event_tracker.top_users(hours),
            "resp_dist": event_tracker.response_time_distribution(hours),
            "total": event_tracker.total_events(hours),
        }

    @app.get("/api/templates")
    async def api_get_templates(request: Request, _=Depends(_api_auth)):
        return config_manager.get_dynamic_templates_config() or {}

    @app.put("/api/templates/{key}")
    async def api_update_template(key: str, request: Request, _=Depends(_api_write("edit_template"))):
        body = await request.json()
        value = body.get("value")
        if value is None:
            raise HTTPException(400, "Missing 'value'")
        data = config_manager.get_dynamic_templates_config() or {}
        if key not in data:
            raise HTTPException(404, f"Template '{key}' not found")
        # 变更前快照（与表单路径 /templates/update 同口径，审计可深链 /diff）
        snap_content = yaml.dump(data, allow_unicode=True, default_flow_style=False)
        if isinstance(value, list):
            data[key] = value
        else:
            data[key] = str(value)
        ok, msg = config_manager.save_templates(data)
        if not ok:
            raise HTTPException(500, msg)
        config_manager.invalidate_templates_cache()
        actor = request.session.get("username", "api")
        snap_id = _auto_snapshot("templates", snap_content, actor) or ""
        if audit_store:
            audit_store.log(actor, "update_template", key, "", str(value)[:100], snap_id)
        return {"ok": True, "key": key}

    # ── 团队话术多语言变体·起草工作台（V2 2026-08-18）────────────────────────
    # 写侧唯一入口＝src/web/templates_i18n_store.py（读侧在 unified_inbox_context，
    # mtime 缓存 → 落盘即热生效，坐席面板/自动推荐零重启拿到新变体）。
    # draft＝服务端直调生产翻译栈机器起草（含 {var} 占位符的源文本拒绝——机翻会
    # 翻掉占位符名，破坏面板变量补全表单契约）；确认/改稿/删除对齐 KB
    # kb_translations 的 auto_translated→人工确认 心智。权限与模板编辑同位。

    def _ti_snapshot(actor: str) -> str:
        from src.web import templates_i18n_store as _ti
        return _auto_snapshot(
            "templates_i18n",
            _ti.dump_yaml(_ti.load_all(config_manager)), actor) or ""

    @app.get("/api/templates-i18n")
    async def api_get_templates_i18n(request: Request, _=Depends(_api_auth)):
        from src.web import templates_i18n_store as _ti
        return {"ok": True, "variants": _ti.load_all(config_manager),
                "path": str(_ti.resolve_path(config_manager))}

    @app.post("/api/templates-i18n/draft")
    async def api_templates_i18n_draft(
        request: Request, _=Depends(_api_write("edit_template")),
    ):
        from src.web import templates_i18n_store as _ti
        from src.web.routes.unified_inbox_context import _qtpl_is_system_key
        body = await request.json()
        key = str(body.get("key") or "").strip()
        lang = str(body.get("lang") or "").strip().lower()
        data = config_manager.get_dynamic_templates_config() or {}
        if key not in data:
            raise HTTPException(404, f"Template '{key}' not found")
        if _qtpl_is_system_key(key):
            return {"ok": False, "error": "system_key"}
        val = data.get(key)
        if isinstance(val, str):
            texts = [val]
        elif isinstance(val, list):
            texts = [x for x in val if isinstance(x, str)]
        else:
            return {"ok": False, "error": "system_key"}
        src = next((s.strip() for s in texts if s and s.strip()), "")
        if not src:
            return {"ok": False, "error": "no_source"}
        if _ti.has_placeholders(src):
            return {"ok": False, "error": "has_vars"}
        from src.web.routes.unified_inbox_services import _get_translation_service
        svc = _get_translation_service(request)
        result = await svc.translate(src, target_lang=lang, source_lang="zh")
        translated = str(getattr(result, "translated_text", "") or "").strip()
        if not getattr(result, "ok", False) or not translated:
            return {"ok": False, "error": "translate_failed"}
        actor = request.session.get("username", "api")
        snap_id = _ti_snapshot(actor)
        ok, err, entries = _ti.draft_variant(config_manager, key, lang, translated)
        if not ok:
            return {"ok": False, "error": err or "write_failed"}
        if audit_store:
            audit_store.log(actor, "templates_i18n_draft", f"{key}:{lang}", "",
                            translated[:100], snap_id)
        return {"ok": True, "key": key, "lang": lang, "entries": entries,
                "provider": str(getattr(result, "provider", "") or "")}

    @app.post("/api/templates-i18n/save")
    async def api_templates_i18n_save(
        request: Request, _=Depends(_api_write("edit_template")),
    ):
        from src.web import templates_i18n_store as _ti
        body = await request.json()
        key = str(body.get("key") or "").strip()
        lang = str(body.get("lang") or "").strip().lower()
        text = str(body.get("text") or "")
        index = body.get("index", None)
        idx = int(index) if isinstance(index, (int, float)) else None
        actor = request.session.get("username", "api")
        snap_id = _ti_snapshot(actor)
        ok, err, entries = _ti.upsert_variant(
            config_manager, key, lang, text,
            approved=bool(body.get("approved", True)), index=idx)
        if not ok:
            return {"ok": False, "error": err or "write_failed"}
        if audit_store:
            audit_store.log(actor, "templates_i18n_save", f"{key}:{lang}", "",
                            text[:100], snap_id)
        return {"ok": True, "key": key, "lang": lang, "entries": entries}

    @app.post("/api/templates-i18n/confirm")
    async def api_templates_i18n_confirm(
        request: Request, _=Depends(_api_write("edit_template")),
    ):
        from src.web import templates_i18n_store as _ti
        body = await request.json()
        key = str(body.get("key") or "").strip()
        lang = str(body.get("lang") or "").strip().lower()
        try:
            idx = int(body.get("index"))
        except (TypeError, ValueError):
            return {"ok": False, "error": "not_found"}
        actor = request.session.get("username", "api")
        snap_id = _ti_snapshot(actor)
        ok, err, entries = _ti.confirm_variant(config_manager, key, lang, idx)
        if not ok:
            return {"ok": False, "error": err or "write_failed"}
        if audit_store:
            audit_store.log(actor, "templates_i18n_confirm", f"{key}:{lang}", "",
                            str(idx), snap_id)
        return {"ok": True, "key": key, "lang": lang, "entries": entries}

    @app.post("/api/templates-i18n/delete")
    async def api_templates_i18n_delete(
        request: Request, _=Depends(_api_write("edit_template")),
    ):
        from src.web import templates_i18n_store as _ti
        body = await request.json()
        key = str(body.get("key") or "").strip()
        lang = str(body.get("lang") or "").strip().lower()
        try:
            idx = int(body.get("index"))
        except (TypeError, ValueError):
            return {"ok": False, "error": "not_found"}
        actor = request.session.get("username", "api")
        snap_id = _ti_snapshot(actor)
        ok, err, entries = _ti.delete_variant(config_manager, key, lang, idx)
        if not ok:
            return {"ok": False, "error": err or "write_failed"}
        if audit_store:
            audit_store.log(actor, "templates_i18n_delete", f"{key}:{lang}", "",
                            str(idx), snap_id)
        return {"ok": True, "key": key, "lang": lang, "entries": entries}

    @app.post("/api/batch-strategies")
    async def api_batch_strategies(request: Request, _=Depends(_api_write("edit_strategy"))):
        """批量启用/禁用多个策略"""
        body = await request.json()
        ids: list = body.get("ids", [])
        enabled: bool = body.get("enabled", True)
        if not ids:
            raise HTTPException(400, "ids 不能为空")
        rs = config_manager.get_strategies_config()
        strategies = rs.get("strategies", {})
        snap_content = yaml.dump(rs, allow_unicode=True, default_flow_style=False)
        updated, not_found = [], []
        for sid in ids:
            if sid in strategies:
                strategies[sid]["enabled"] = enabled
                updated.append(sid)
            else:
                not_found.append(sid)
        if not updated:
            raise HTTPException(404, f"未找到任何策略: {not_found}")
        rs["strategies"] = strategies
        ok, msg = config_manager.save_strategies(rs)
        if not ok:
            raise HTTPException(500, msg)
        actor = request.session.get("username", "api")
        snap_id = _auto_snapshot("reply_strategies", snap_content, actor) or ""
        if audit_store:
            audit_store.log(actor, "batch_strategy_enabled", ",".join(updated), "",
                            str(enabled), snap_id)
        return {"ok": True, "updated": updated, "not_found": not_found, "enabled": enabled}

    @app.post("/api/batch-templates")
    async def api_batch_templates(request: Request, _=Depends(_api_write("edit_template"))):
        """批量删除模板键"""
        body = await request.json()
        keys: list = body.get("keys", [])
        action: str = body.get("action", "delete")
        if not keys:
            raise HTTPException(400, "keys 不能为空")
        if action != "delete":
            raise HTTPException(400, "目前仅支持 action=delete")
        data = config_manager.get_dynamic_templates_config() or {}
        removed, not_found = [], []
        for k in keys:
            if k in data:
                del data[k]
                removed.append(k)
            else:
                not_found.append(k)
        if not removed:
            raise HTTPException(404, f"未找到任何模板键: {not_found}")
        snap_content = yaml.dump(
            config_manager.get_dynamic_templates_config() or {},
            allow_unicode=True, default_flow_style=False
        )
        ok, msg = config_manager.save_templates(data)
        if not ok:
            raise HTTPException(500, msg)
        config_manager.invalidate_templates_cache()
        actor = request.session.get("username", "api")
        snap_id = _auto_snapshot("templates", snap_content, actor) or ""
        if audit_store:
            audit_store.log(actor, "batch_delete_templates", ",".join(removed), "",
                            "", snap_id)
        return {"ok": True, "removed": removed, "not_found": not_found}

    @app.get("/api/audit")
    async def api_audit(request: Request, _=Depends(_api_auth),
                        action: str = "", keyword: str = "", limit: int = 50):
        if not audit_store:
            return []
        return audit_store.query(limit=limit, action=action, keyword=keyword)

    @app.get("/api/config/summary")
    async def api_config_summary(request: Request, _=Depends(_api_auth)):
        tpl = config_manager.get_dynamic_templates_config() or {}
        result = {
            "templates": {k: len(v) if isinstance(v, list) else 1 for k, v in tpl.items()},
        }
        if any(p.get("key") == "ch" for p in domain_web_pages):
            rates = config_manager.get_exchange_rates_config() or {}
            result["channels"] = {k: {"status": c.get("status"), "fee_rate": c.get("fee_rate")}
                                  for k, c in rates.get("channels", {}).items()}
        return result

    @app.post("/api/migrate")
    async def api_migrate(request: Request, _=Depends(_api_write("import_export"))):
        from src.utils.config_migrator import ConfigMigrator
        migrator = ConfigMigrator(config_manager.config_path)
        ok, msg = migrator.check_and_migrate()
        return {"migrated": ok, "message": msg}

    @app.get("/api/ai/quality")
    async def api_ai_quality(request: Request, _=Depends(_api_auth)):
        ai = getattr(telegram_client, "ai_client", None) if telegram_client else None
        if not ai:
            return {"error": "ai client not available"}
        qt = getattr(ai, "_quality_tracker", None)
        if not qt:
            return {"error": "quality tracker not available"}
        return {
            "summary": qt.get_summary(),
            "anomalies": qt.get_recent_anomalies(20),
            "token_trend": qt.get_token_trend(50),
        }

    # ── 健康检查（无需认证） ────────────────────────────────────

    @app.get("/health")
    async def health_check():
        import os
        mem_mb = None
        try:
            import psutil
            proc = psutil.Process(os.getpid())
            mem_mb = round(proc.memory_info().rss / 1024 / 1024, 1)
        except Exception:
            pass

        uptime_sec = int(time.time() - boot_ts) if boot_ts else 0
        gxp_queue_depth = 0
        connected = False
        last_msg_ts = None
        if telegram_client:
            gxp_queue_depth = sum(len(q) for q in getattr(telegram_client, "_gxp_pending", {}).values())
            connected = getattr(telegram_client, "running", False)
            last_send = getattr(telegram_client, "_last_send_wallclock", 0)
            if last_send > 0:
                last_msg_ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(last_send))

        return {
            "status": "ok" if connected else "degraded",
            "uptime_seconds": uptime_sec,
            "connected": connected,
            "memory_mb": mem_mb,
            "gxp_queue_depth": gxp_queue_depth,
            "last_message_sent": last_msg_ts,
            "templates_count": len(config_manager.get_dynamic_templates_config() or {}),
            **({"channels_count": len((config_manager.get_exchange_rates_config() or {}).get("channels", {}))}
               if any(p.get("key") == "ch" for p in domain_web_pages) else {}),
            "rate_limit_stats": getattr(telegram_client, "_rate_limiter", None) and telegram_client._rate_limiter.get_stats() or {},
            "ai_stats": _get_ai_stats(telegram_client),
        }

    def _get_ai_stats(tc):
        if not tc:
            return {}
        ai = getattr(tc, "ai_client", None)
        if not ai:
            return {}
        tracker = getattr(ai, "_quality_tracker", None)
        return {
            "total_calls": getattr(ai, "total_calls", 0),
            "total_tokens": getattr(ai, "total_tokens", 0),
            "quality": tracker.get_summary() if tracker else {},
        }

    # ── 配置导入导出 ──────────────────────────────────────────

    _EXPORT_FILES = ["templates.yaml", "exchange_rates.yaml", "quota_rules.yaml"]

    @app.get("/export")
    async def export_config(request: Request, _=Depends(_page_auth)):
        cfg_dir = config_manager.config_path.parent
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for name in _EXPORT_FILES:
                fp = cfg_dir / name
                if fp.exists():
                    zf.writestr(name, fp.read_text(encoding="utf-8"))
        buf.seek(0)
        ts = time.strftime("%Y%m%d_%H%M%S")
        return StreamingResponse(
            buf,
            media_type="application/zip",
            headers={"Content-Disposition": f"attachment; filename=bot_config_{ts}.zip"},
        )

    @app.get("/import", response_class=HTMLResponse)
    async def import_page(request: Request, _=Depends(_page_auth)):
        return templates.TemplateResponse(request, "import.html", {"msg": "", "msg_ok": True})

    def _deep_merge(base: dict, incoming: dict) -> dict:
        """
        深度合并：incoming 中的键更新到 base，base 中独有的键保留。
        支持嵌套 dict；list 和 scalar 值直接覆盖。
        """
        result = dict(base)
        for k, v in incoming.items():
            if k in result and isinstance(result[k], dict) and isinstance(v, dict):
                result[k] = _deep_merge(result[k], v)
            else:
                result[k] = v
        return result

    @app.post("/import")
    async def import_config(request: Request, _=Depends(_page_auth),
                            file: UploadFile = File(...),
                            mode: str = Form("overwrite")):
        """
        mode=overwrite: 完全替换当前配置（原有行为）
        mode=merge:     增量合并——只更新导入文件中存在的键，保留当前独有的键
        """
        if not file.filename.endswith(".zip"):
            return templates.TemplateResponse(request, "import.html", {
                "msg": "请上传 .zip 文件", "import_mode": mode, "msg_ok": False})
        cfg_dir = config_manager.config_path.parent
        content = await file.read()
        restored = []
        import_snap_stems: list = []
        merge_stats = {}
        try:
            with zipfile.ZipFile(io.BytesIO(content), "r") as zf:
                for name in _EXPORT_FILES:
                    if name not in zf.namelist():
                        continue
                    raw = zf.read(name).decode("utf-8")
                    incoming_data = yaml.safe_load(raw)
                    if not isinstance(incoming_data, dict):
                        continue
                    target = cfg_dir / name
                    if mode == "merge" and target.exists():
                        current_data = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
                        merged = _deep_merge(current_data, incoming_data)
                        # 统计变更数量
                        added = [k for k in incoming_data if k not in current_data]
                        updated = [k for k in incoming_data if k in current_data and incoming_data[k] != current_data[k]]
                        merge_stats[name] = {"added": len(added), "updated": len(updated)}
                        out_content = yaml.dump(merged, allow_unicode=True, default_flow_style=False)
                    else:
                        out_content = raw
                    # 保存前快照（多文件导入时只收集 stem；审计第六参仅在「单文件单快照」时写入，
                    # 避免一条审计挂多个无法深链的 snap）
                    actor = request.session.get("username", "web_admin")
                    snap_content = target.read_text(encoding="utf-8") if target.exists() else ""
                    if snap_content:
                        prefix = name.replace(".yaml", "")
                        stem = _auto_snapshot(prefix, snap_content, actor)
                        if stem:
                            import_snap_stems.append(stem)
                    import shutil
                    if target.exists():
                        shutil.copy2(target, target.with_suffix(".yaml.pre_import"))
                    target.write_text(out_content, encoding="utf-8")
                    restored.append(name)
        except zipfile.BadZipFile:
            return templates.TemplateResponse(request, "import.html", {
                "msg": "ZIP 文件损坏", "import_mode": mode, "msg_ok": False})
        except yaml.YAMLError as ye:
            return templates.TemplateResponse(request, "import.html", {
                "msg": f"YAML 格式错误: {ye}", "import_mode": mode, "msg_ok": False})
        if restored:
            config_manager.invalidate_templates_cache()
            config_manager.invalidate_exchange_rates_cache()
            actor = request.session.get("username", "web_admin")
            if audit_store:
                snap_id = (import_snap_stems[0]
                           if len(restored) == 1 and len(import_snap_stems) == 1
                           else "")
                audit_store.log(actor, f"import_config_{mode}", ", ".join(restored),
                                "", "", snap_id)
        if mode == "merge" and merge_stats:
            details = "; ".join(
                f"{n}: +{v['added']} 新增 / ~{v['updated']} 更新"
                for n, v in merge_stats.items()
            )
            msg = f"增量合并成功 — {details}" if restored else "ZIP 中无可识别的配置文件"
        else:
            msg = f"已导入 {len(restored)} 个文件: {', '.join(restored)}" if restored else "ZIP 中无可识别的配置文件"
        return templates.TemplateResponse(request, "import.html", {
            "msg": msg, "import_mode": mode, "msg_ok": bool(restored)})


    # ═══════════════════════════════════════════════════════════════════
    # 知识库路由 ── /knowledge  +  /api/kb/*
    # ═══════════════════════════════════════════════════════════════════
    from src.utils.kb_store import KnowledgeBaseStore, seed_default_data, KB_CATEGORIES, seed_system_replies

    _kb_db_path = cfg_dir / "knowledge_base.db"
    _kb_store = KnowledgeBaseStore(_kb_db_path)
    seed_default_data(_kb_store)
    _sys_seed = seed_system_replies(_kb_store)
    if _sys_seed.get("added"):
        import logging as _lg
        _lg.getLogger("admin").info("KB 系统话术种子已迁移: %s", _sys_seed)

    # ---------- templates.yaml 一次性迁移 ----------
    def _migrate_templates_once():
        """把 templates.yaml 中的话术条目迁移到知识库（只在 kb 为空时运行）"""
        if _kb_store.stats()["total_entries"] > 0:
            return
        # 用户曾清空过知识库条目后，不再从 templates.yaml 自动灌回，避免「删光重启又长回来」
        try:
            if _kb_store.get_meta("kb_seeded_once") == "1":
                return
        except Exception:
            pass
        tpl_path = config_manager.config_path.parent / "templates.yaml"
        if not tpl_path.exists():
            return
        try:
            data = yaml.safe_load(tpl_path.read_text(encoding="utf-8")) or {}
        except Exception:
            return

        _CATEGORY_MAP = {
            "greeting": "常规咨询",
            "complaint": "投诉处理",
            "refund": "退款投诉",
            "small_talk": "常规咨询",
            "system_": "系统指令",
        }
        _CATEGORY_MAP.update(getattr(app.state, "intent_display_names_extra", {}))

        def _guess_category(key: str) -> str:
            key_lower = key.lower()
            for prefix, cat in _CATEGORY_MAP.items():
                if prefix in key_lower:
                    return cat
            return "其他"

        for key, val in data.items():
            if isinstance(val, list):
                msgs = [str(m) for m in val if m]
                example_zh = msgs[0] if msgs else ""
                triggers = [key.replace("_", " ")]
            elif isinstance(val, dict):
                msgs = []
                for lang_val in val.values():
                    if isinstance(lang_val, list):
                        msgs += [str(m) for m in lang_val if m]
                    elif lang_val:
                        msgs.append(str(lang_val))
                example_zh = msgs[0] if msgs else ""
                triggers = [key.replace("_", " ")]
            elif isinstance(val, str):
                example_zh = val
                triggers = [key.replace("_", " ")]
            else:
                continue

            _kb_store.add_entry({
                "category": _guess_category(key),
                "title": key.replace("_", " ").title(),
                "triggers": triggers,
                "scenario": f"从 templates.yaml 迁移的话术：{key}",
                "steps": "按照示例回复进行响应",
                "principles": "保持简洁，确保信息准确",
                "example_reply_zh": example_zh,
            })

    _migrate_templates_once()

    # ---------- 智能学习页面 ----------
    @app.get("/learner", response_class=HTMLResponse)
    async def learner_page(request: Request):
        _require_auth(request)
        return templates.TemplateResponse(request, "learner.html", {
            "active": "learner",
        })

    # ---------- 页面 ----------
    # ── 知识库条目 CRUD + 版本历史（Phase E1 批 5A：抽到 routes/kb_routes.py） ──
    _admin_ctx.kb_store = _kb_store
    _admin_ctx.fire_webhook = _fire_webhook
    try:
        from src.web.routes.kb_routes import register_kb_routes

        register_kb_routes(app, _admin_ctx)
    except Exception:
        import logging as _log_kb

        _log_kb.getLogger("admin").warning("kb 路由注册失败", exc_info=True)

    # 错误码/示例/规则/反馈/沙盒/category-stats 已抽到 routes/kb_routes.py（批 5B）

    # ── 知识条目 AI 自动生成 ──────────────────────────────────

    # _auto_fill_entry 薄包装已移除（批 5I：全部调用方迁至 kb_routes 直接用 kb_ai_helpers）

    # ai-generate / export-markdown / stats / entries/{id}/translate 已抽到 routes/kb_routes.py（批 5L）

    # ═══════════════════════════════════════════════════════════════════
    # 知识库 — 智能体翻译 / 健康度 / 沙盒 AI 回复 / Miss 日志
    # ═══════════════════════════════════════════════════════════════════

    # _ai_translate_entry 薄包装已移除（批 5I：全部调用方迁至 kb_routes 直接用 kb_ai_helpers）

    # auto-translate / translation-gaps 已抽到 routes/kb_routes.py（批 5H）

    # KB AI 自动化运行时（翻译扫描/演化/自愈：locks+run_*+3端点+3循环）已整组搬到
    # routes/kb_ai_routes.py（批 5G）。循环 stash 在 app.state.kb_ai_loops，下方 startup 统一启动。
    try:
        from src.web.routes.kb_ai_routes import register_kb_ai_routes

        register_kb_ai_routes(app, _admin_ctx)
    except Exception:
        import logging as _log_kbai

        _log_kbai.getLogger("admin").warning("kb_ai 路由注册失败", exc_info=True)

    @app.on_event("startup")
    async def _start_background_tasks():
        for _kb_ai_loop in getattr(app.state, "kb_ai_loops", []):
            asyncio.create_task(_kb_ai_loop())
        asyncio.create_task(_weekly_report_loop())
        # P26-A: intent_tags.yaml 文件变更自动 reload（基于 watchdog）
        try:
            _it_cfg = (config_manager.config or {}).get("rpa_intent_tags", {}) or {}
            if _it_cfg.get("watch_enabled", True):
                from src.integrations.intent_tags_watcher import start_watcher
                start_watcher(debounce_sec=float(_it_cfg.get("watch_debounce_sec", 0.8)))
        except Exception:
            pass

    @app.on_event("shutdown")
    async def _stop_intent_tags_watcher():
        # P26-A: 干净停 watchdog observer thread
        try:
            from src.integrations.intent_tags_watcher import stop_watcher
            stop_watcher()
        except Exception:
            pass

    # translate-all / translate-progress(SSE) 已抽到 routes/kb_routes.py（批 5H）

    # sandbox/ai-reply 已抽到 routes/kb_routes.py（批 5L）

    # 全链路对话自测 API（Phase E1 续拆 → chat_test_routes，含 F3 测试会话缓存）
    try:
        from src.web.routes.chat_test_routes import register_chat_test_routes

        register_chat_test_routes(app, _admin_ctx)
    except Exception:
        import logging as _log_ct

        _log_ct.getLogger("admin").warning("chat/test 路由注册失败", exc_info=True)

    # G3: 意图链 Case 面板 — 活跃 case 列表 + 人工介入 + 结案
    # 运营 case 管理 API（Phase E1 续拆 → cases_routes）
    try:
        from src.web.routes.cases_routes import register_cases_routes

        register_cases_routes(app, _admin_ctx)
    except Exception:
        import logging as _log_cases

        _log_cases.getLogger("admin").warning("cases 路由注册失败", exc_info=True)

    # 报障群工单管理（bug_intake，2026-08-18：报障群 AI 值守台账；
    # 2026-08-28 实施81：+处置台页面（page_auth）与 bot 发送配置（config_manager））
    try:
        from src.web.routes.bug_intake_routes import register_bug_intake_routes

        register_bug_intake_routes(
            app, api_auth=_admin_ctx.api_auth,
            page_auth=_admin_ctx.page_auth, config_manager=config_manager)
    except Exception:
        import logging as _log_bi

        _log_bi.getLogger("admin").warning("bug_intake 路由注册失败",
                                           exc_info=True)

    # 迁移包导出（账号资产保全，实施47 §5 工单 2）：联系人+会话+可加回句柄打成
    # 一份离线 zip。资产中心页按路由路径探测本端点（features.export_migration），
    # 装载后其「导出迁移包」CTA 自动点亮，无需前端改动。
    try:
        from src.web.routes.migration_export_routes import (
            register_migration_export_routes,
        )
        register_migration_export_routes(app, api_auth=_admin_ctx.api_auth)
    except Exception:
        import logging as _log_mex
        _log_mex.getLogger("admin").warning("migration_export 路由注册失败",
                                            exc_info=True)

    # 回连认领（账号资产保全 P1）：封号账号 → 新账号的老客户识别 + 记忆合流
    try:
        from src.web.routes.reconnect_claim_routes import (
            register_reconnect_claim_routes,
        )

        register_reconnect_claim_routes(app, api_auth=_admin_ctx.api_auth)
    except Exception:
        import logging as _log_rcl

        _log_rcl.getLogger("admin").warning("reconnect_claim 路由注册失败",
                                            exc_info=True)

    # 运营 Copilot + 测试纠错 API（Phase E1 续拆 → copilot_routes）
    try:
        from src.web.routes.copilot_routes import register_copilot_routes

        register_copilot_routes(app, _admin_ctx)
    except Exception:
        import logging as _log_cop

        _log_cop.getLogger("admin").warning("copilot 路由注册失败", exc_info=True)

    # AI 助手悬浮球 /api/assistant/*（2026-08-19 P0；assistant.enabled 灰度）
    try:
        from src.web.routes.assistant_routes import register_assistant_routes

        register_assistant_routes(app, _admin_ctx)
    except Exception:
        import logging as _log_asb

        _log_asb.getLogger("admin").warning("assistant 路由注册失败",
                                            exc_info=True)

    # 小智动作注册表 /api/assistant/actions|act*（实施58 P1；同 assistant.enabled 灰度）
    try:
        from src.web.routes.assistant_action_routes import (
            register_assistant_action_routes,
        )

        register_assistant_action_routes(app, _admin_ctx)
    except Exception:
        import logging as _log_asa

        _log_asa.getLogger("admin").warning("assistant action 路由注册失败",
                                            exc_info=True)

    # 小智手机扫码操控 /api/assistant/pair* + GET /xz（实施58 P4；同灰度）
    try:
        from src.web.routes.assistant_pair_routes import (
            register_assistant_pair_routes,
        )

        register_assistant_pair_routes(app, _admin_ctx)
    except Exception:
        import logging as _log_asp

        _log_asp.getLogger("admin").warning("assistant pair 路由注册失败",
                                            exc_info=True)

    # ---------- 知识库健康度 ----------
    # KB 健康统计/Miss日志/翻译审核/图片/种子/维护建议 已抽到 routes/kb_routes.py（批 5K）

    # ── 运营日报/周报 API（Phase E1 续拆 → report_routes） ──
    # K4 日报 + F4 周报已抽到 src/web/routes/report_routes.py（kb_store 已就绪）。
    # 周报后台推送循环 _weekly_report_loop 属后台任务，仍留本文件（见下方）。
    try:
        from src.web.routes.report_routes import register_report_routes

        register_report_routes(app, _admin_ctx)
    except Exception:
        import logging as _log_rep

        _log_rep.getLogger("admin").warning("report 路由注册失败", exc_info=True)

    # F4: 后台自动推送周报（每周一 09:00 附近）
    async def _weekly_report_loop():
        await asyncio.sleep(600)
        while True:
            try:
                now = time.localtime()
                if now.tm_wday == 0 and 8 <= now.tm_hour <= 10:
                    with _kb_store._conn() as c:
                        now_ts = time.time()
                        this_week = now_ts - 168 * 3600
                        tw_total = c.execute(
                            "SELECT COUNT(*) FROM kb_query_log WHERE ts >= ?", (this_week,)
                        ).fetchone()[0]
                        tw_hits = c.execute(
                            "SELECT COUNT(*) FROM kb_query_log WHERE ts >= ? AND hit=1", (this_week,)
                        ).fetchone()[0]
                    tw_rate = round(tw_hits / max(tw_total, 1) * 100, 1)
                    summary = (
                        f"📊 自动周报: 本周 {tw_total} 次查询, "
                        f"命中率 {tw_rate}%"
                    )
                    # AI 价值总账行（2026-08-06）：与 /api/report/weekly 同源
                    # （src/ops/value_report），推送不再只有一句 KB 命中率。
                    try:
                        from src.ops.value_report import build_weekly_value
                        _inbox_st = getattr(app.state, "inbox_store", None)
                        _val = build_weekly_value(_inbox_st) if _inbox_st is not None else {}
                        for _vl in (_val or {}).get("text_lines", []):
                            summary += "\n" + _vl
                    except Exception:
                        pass
                    await _fire_webhook("weekly_report", "system", "report", summary)
                    logger.info("F4 周报已推送: %s", summary)
                    await asyncio.sleep(72000)  # 推送后休眠 20h 避免重复
                    continue
            except Exception as _e:
                logger.debug("F4 周报循环异常: %s", _e)
            await asyncio.sleep(3600)

    # Phase 7a: 自动建议 + 弱命中分析
    # ═══════════════════════════════════════════════════════════════════

    # auto-suggestions / reply-quality 已抽到 routes/kb_routes.py（批 5L）
    # （accept-suggestion 已于批 5I 抽出）

    @app.get("/api/users/at-risk")
    async def api_users_at_risk(request: Request):
        """K3: 返回满意度 at_risk 的用户列表"""
        _api_auth(request)
        from src.web.web_context import resolve_skill_manager
        sm = resolve_skill_manager(telegram_client, app)
        ctx_store = getattr(sm, "_context_store", None) if sm else None
        if not ctx_store:
            return {"users": [], "count": 0}
        at_risk = []
        for uid, ctx in ctx_store._cache.items():
            profile = ctx.get("_user_profile")
            if isinstance(profile, dict) and profile.get("at_risk"):
                at_risk.append({
                    "user_id": uid,
                    "satisfaction": profile.get("satisfaction", 0),
                    "type": profile.get("type", "unknown"),
                    "tone": profile.get("tone", "standard"),
                    "msg_count": profile.get("msg_count", 0),
                    "last_intent": ctx.get("current_intent", ""),
                    "last_message": (ctx.get("last_message") or "")[:80],
                })
        at_risk.sort(key=lambda x: x["satisfaction"])
        return {"users": at_risk[:50], "count": len(at_risk)}

    # J3: 活跃对话实时监控
    # ═══════════════════════════════════════════════════════════════════

    @app.get("/api/conversations/active")
    async def api_active_conversations(request: Request, minutes: int = 30):
        """返回最近 N 分钟内有活动的对话列表（含满意度、意图、at_risk 状态）"""
        _api_auth(request)
        from src.web.web_context import resolve_skill_manager
        sm = resolve_skill_manager(telegram_client, app)
        ctx_store = getattr(sm, "_context_store", None) if sm else None
        if not ctx_store:
            return {"conversations": [], "count": 0, "at_risk_count": 0}

        cutoff = time.time() - minutes * 60
        conversations = []
        at_risk_count = 0

        for uid, ctx in ctx_store._cache.items():
            last_time = ctx.get("last_reply_time", 0)
            if last_time < cutoff:
                continue
            profile = ctx.get("_user_profile", {})
            is_risk = profile.get("at_risk", False) if isinstance(profile, dict) else False
            if is_risk:
                at_risk_count += 1
            sat = profile.get("satisfaction", 80) if isinstance(profile, dict) else 80

            hist = ctx.get("_conversation_history", [])
            recent_msgs = []
            for h in hist[-4:]:
                recent_msgs.append({
                    "role": h.get("role", "user"),
                    "text": (h.get("content") or "")[:120],
                })

            conversations.append({
                "user_id": uid,
                "chat_id": ctx.get("chat_id", ""),
                "chat_title": ctx.get("chat_title", ""),
                "satisfaction": sat,
                "at_risk": is_risk,
                "user_type": profile.get("type", "new") if isinstance(profile, dict) else "new",
                "tone": profile.get("tone", "standard") if isinstance(profile, dict) else "standard",
                "current_intent": ctx.get("current_intent", ""),
                "msg_count": profile.get("msg_count", 0) if isinstance(profile, dict) else 0,
                "consecutive_same": ctx.get("_consecutive_same_intent", 0),
                "last_message": (ctx.get("last_message") or "")[:100],
                "last_reply": (ctx.get("last_reply") or "")[:100],
                "last_active": last_time,
                "recent_messages": recent_msgs,
                "escalation_triggered": bool(ctx.get("_escalation_ts")),
            })

        conversations.sort(key=lambda x: (not x["at_risk"], -x["consecutive_same"], x["satisfaction"]))
        return {
            "conversations": conversations[:100],
            "count": len(conversations),
            "at_risk_count": at_risk_count,
            "window_minutes": minutes,
        }

    # Phase 7: 查询分析 + Embedding API 用量统计
    # ═══════════════════════════════════════════════════════════════════

    # query-analytics/today-hit-rate/embed-stats/implicit-feedback 已抽到 routes/kb_routes.py（批 5D）

    # ═══════════════════════════════════════════════════════════════════
    # Phase 5: 向量化 / 查重 / 知识库备份管理
    # ═══════════════════════════════════════════════════════════════════

    # embed/查重/备份簇（embed-all/progress/single/coverage/duplicates/backup/backups/restore）
    # 连同助手 _call_embed_api/_build_embed_text/_run_embed_all + state 已整组搬到
    # routes/kb_routes.py（批 5E）。

    # 向外暴露 kb_store 供 skill_manager 调用
    app.state.kb_store = _kb_store

    # ═══════════════════════════════════════════════════════════════════
    # 每日自动学习 ── /api/learner/*（批 5J：抽到 routes/learner_routes.py）
    # ═══════════════════════════════════════════════════════════════════
    try:
        from src.web.routes.learner_routes import register_learner_routes

        register_learner_routes(app, _admin_ctx)
    except Exception:
        import logging as _log_ln

        _log_ln.getLogger("admin").warning("learner 路由注册失败", exc_info=True)

    # ── 监控/指标/reactivation dry-run 路由（批 G2-①，ctx.kb_store 已就绪） ──
    try:
        from src.web.routes.monitoring_routes import register_monitoring_routes

        register_monitoring_routes(app, _admin_ctx)
    except Exception:
        import logging as _log_mon

        _log_mon.getLogger("admin").warning("monitoring 路由注册失败", exc_info=True)

    # ── Persona Management API ──────────────────────────────
    # 注：/api/persona{,/bindings,/bind,/unbind,/update-default,/preview-prompt}
    # 原本在此 inline，与 register_persona_routes 重复注册（inline 被遮蔽=死代码）。
    # Phase E1 清理：删除 inline 死代码，统一由 persona_routes 模块提供。

    # ── KB Import API ────────────────────────────────────────
    # ⚠ 已知遗留 bug（待产品决策，未擅自修改）：下面的 @app.post("/api/kb/import")
    # 是 KBImporter 文档分块导入（配合 /api/kb/import/save）。但 kb_routes.py 里的
    # /api/kb/import（export-dump 导入）注册更早 → 遮蔽本版，导致「文档导入向导」
    # 实际走不到这里。二者语义不同、共用同一 path，需改名（如 /api/kb/import-document）
    # 才能并存。保留现状以免改变 API 契约（需前端协同）。
    @app.post("/api/kb/import")
    async def api_kb_import(request: Request, _=Depends(_api_auth)):
        from src.utils.kb_importer import KBImporter
        data = await request.json()
        content = data.get("content", "")
        filename = data.get("filename", "upload")
        file_type = data.get("file_type", "txt")
        category = data.get("category", "")
        chunk_size = int(data.get("chunk_size", 500))

        if not content:
            raise HTTPException(400, "content required")

        importer = KBImporter()
        entries = importer.import_text_content(
            content=content,
            filename=filename,
            file_type=file_type,
            category=category,
            chunk_size=chunk_size,
        )
        return {"entries": entries, "count": len(entries)}

    @app.post("/api/kb/import/save")
    async def api_kb_import_save(request: Request, _=Depends(_api_auth)):
        from src.utils.kb_importer import KBImporter
        data = await request.json()
        entries = data.get("entries", [])
        if not entries:
            raise HTTPException(400, "no entries to save")

        kb = None
        try:
            from src.utils.kb_store import KnowledgeBaseStore
            kb_path = Path(config_manager.config_path).parent / "knowledge_base.db"
            if kb_path.exists():
                kb = KnowledgeBaseStore(kb_path)
        except Exception:
            pass

        if not kb:
            raise HTTPException(503, "KB store not available")

        importer = KBImporter(kb_store=kb)
        ok, err = importer.save_entries_to_kb(entries)
        actor = request.session.get("username", "web_admin")
        if audit_store:
            audit_store.log(actor, "kb_import", f"saved={ok} errors={err}")
        return {"ok": True, "saved": ok, "errors": err}

    # ── Domain Pack Route Registration ──────────────────────
    from src.web.web_context import WebContext
    _web_ctx = WebContext(
        config_manager=config_manager,
        audit_store=audit_store,
        event_tracker=event_tracker,
        templates=templates,
        user_store=user_store,
        page_auth=_page_auth,
        api_auth=_api_auth,
        api_write_factory=_api_write,
        auto_snapshot=_auto_snapshot,
        broadcast_config_reload=_broadcast_config_reload,
        fire_webhook=_fire_webhook,
        sync_domain_exchange_rates=None,
        domain_name=domain_name,
        domain_web_pages=domain_web_pages,
    )
    _register_domain_routes(app, _web_ctx)

    # ── 情景记忆（放在域路由注册之后，避免被域打包或其它中间件式路由误覆盖导致 404）──
    @app.get("/episodic_memory")
    async def episodic_memory_alias_redirect():
        return RedirectResponse(url="/episodic-memory", status_code=307)

    @app.get("/episodic-memory", response_class=HTMLResponse)
    async def episodic_memory_page(request: Request, _=Depends(_page_auth)):
        _require_role(request, "episodic")
        return templates.TemplateResponse(request, "episodic_memory.html", {})

    # ── R9c：危机事件审计页面 ──
    @app.get("/crisis-audit", response_class=HTMLResponse)
    async def crisis_audit_page(request: Request, _=Depends(_page_auth)):
        _require_role(request, "crisis_audit")
        return templates.TemplateResponse(request, "crisis_audit.html", {})

    # ── Phase O5：主动关怀待办页面 ──
    @app.get("/care-schedule", response_class=HTMLResponse)
    async def care_schedule_page(request: Request, _=Depends(_page_auth)):
        _require_role(request, "care")
        return templates.TemplateResponse(request, "care_schedule.html", {})

    # ── 个人设置（2026-08-04）：坐席级外观个性化（主题/壁纸/夜间/字号/圆角/动画）──
    # 全部登录角色可用（外观是个人偏好而非管理能力），刻意只挂 _page_auth 不加
    # _require_role；数据经 /api/workspace/prefs 的 appearance 字段按坐席漫游。
    @app.get("/personal-settings", response_class=HTMLResponse)
    async def personal_settings_page(request: Request, _=Depends(_page_auth)):
        return templates.TemplateResponse(request, "personal_settings.html", {})

    # ── Phase P4：关系健康 / 流失预警榜页面 ──
    @app.get("/relations-health", response_class=HTMLResponse)
    async def relations_health_page(request: Request, _=Depends(_page_auth)):
        _require_role(request, "care")
        return templates.TemplateResponse(request, "relations_health.html", {})

    # ── RH-P1：流失预警页能力探测（必须**无条件**注册——它存在的意义就是在
    # contacts 未启用时也能回答「全量榜为什么不可用、还能用什么」，前端据此
    # 三态渲染，不再拿裸 404 猜原因）。纯函数核心见 src/web/relations_capability.py。
    @app.get("/api/relations/capability")
    async def api_relations_capability(request: Request, _=Depends(_api_auth)):
        from src.web.relations_capability import build_capability, route_paths_of

        contact_count = None
        _contacts = getattr(request.app.state, "contacts", None)
        _cstore = getattr(_contacts, "store", None) if _contacts else None
        if _cstore is not None:
            try:
                contact_count = int(_cstore.count_contacts())
            except Exception:
                contact_count = None
        return build_capability(
            route_paths=route_paths_of(request.app),
            config=getattr(config_manager, "config", None) or {},
            has_inbox_store=getattr(
                request.app.state, "inbox_store", None) is not None,
            contact_count=contact_count,
        )

    # ── Phase K2：C 端变现营收页面 ──
    @app.get("/monetization", response_class=HTMLResponse)
    async def monetization_page(request: Request, _=Depends(_page_auth)):
        _require_role(request, "monetization")
        return templates.TemplateResponse(request, "monetization.html", {})

    # ── 情景记忆 + 跨平台身份 API（Phase E1 续拆 → episodic_identity_routes） ──
    # 仅 API 端点迁出；上方 2 个页面路由因需 templates 仍留本文件（与既有约定一致）。
    try:
        from src.web.routes.episodic_identity_routes import (
            register_episodic_identity_routes,
        )

        register_episodic_identity_routes(app, _admin_ctx)
    except Exception:
        import logging as _log_ei

        _log_ei.getLogger("admin").warning("情景记忆/身份 路由注册失败", exc_info=True)

    # ── R9b：危机事件审计 API（/api/crisis-events*） ──
    try:
        from src.web.routes.crisis_audit_routes import register_crisis_audit_routes

        register_crisis_audit_routes(app, _admin_ctx)
    except Exception:
        import logging as _log_ca

        _log_ca.getLogger("admin").warning("危机事件审计 路由注册失败", exc_info=True)

    # ── G1：全局 Kill-Switch 紧急停发 API（/api/ops/kill-switch*） ──
    try:
        from src.web.routes.ops_killswitch_routes import register_ops_killswitch_routes

        register_ops_killswitch_routes(app, _admin_ctx)
    except Exception:
        import logging as _log_ks

        _log_ks.getLogger("admin").warning("Kill-Switch 路由注册失败", exc_info=True)

    # ── G3：金丝雀放量 cohort API（/api/ops/canary*） ──
    try:
        from src.web.routes.ops_canary_routes import register_ops_canary_routes

        register_ops_canary_routes(app, _admin_ctx)
    except Exception:
        import logging as _log_cn

        _log_cn.getLogger("admin").warning("Canary 路由注册失败", exc_info=True)

    # ── 实施31：反封号健康统一只读出口（/api/ops/account-health） ──
    try:
        from src.web.routes.ops_health_routes import register_ops_health_routes

        register_ops_health_routes(app, _admin_ctx)
    except Exception:
        import logging as _log_oh

        _log_oh.getLogger("admin").warning("account-health 路由注册失败", exc_info=True)

    try:
        from src.integrations.line_webhook import register_line_routes

        register_line_routes(app, config_manager, telegram_client)
    except Exception:
        import logging as _log_line

        _log_line.getLogger("admin").debug("LINE Webhook 注册跳过", exc_info=True)

    # ── Facebook Page Messenger Webhook（Graph API） ──
    try:
        from src.integrations.facebook_webhook import (
            register_fb_messenger_routes,
        )

        register_fb_messenger_routes(app, config_manager, telegram_client)
    except Exception:
        import logging as _log_fb

        _log_fb.getLogger("admin").debug(
            "FB Messenger Webhook 注册跳过", exc_info=True
        )

    # ── WhatsApp Cloud API（官方）Webhook（Phase G1） ──
    try:
        from src.integrations.whatsapp_cloud import register_whatsapp_cloud_routes

        register_whatsapp_cloud_routes(app, config_manager, telegram_client)
    except Exception:
        import logging as _log_wac

        _log_wac.getLogger("admin").debug(
            "WhatsApp Cloud Webhook 注册跳过", exc_info=True
        )

    # ── Instagram Messaging（官方，Graph API）Webhook（Phase H） ──
    try:
        from src.integrations.instagram_webhook import register_instagram_routes

        register_instagram_routes(app, config_manager, telegram_client)
    except Exception:
        import logging as _log_ig

        _log_ig.getLogger("admin").debug("Instagram Webhook 注册跳过", exc_info=True)

    # ── Zalo OA（官方）Webhook（Phase H） ──
    try:
        from src.integrations.zalo_webhook import register_zalo_routes

        register_zalo_routes(app, config_manager, telegram_client)
    except Exception:
        import logging as _log_zalo

        _log_zalo.getLogger("admin").debug("Zalo Webhook 注册跳过", exc_info=True)

    # ── QQ 机器人（QQ 开放平台官方 API）Webhook（2026-09-07 QQ 双轨·官方轨） ──
    # WebSocket 网关由编排器里的 QQBotOfficialWorker 拉起；这里只挂 HTTPS 回调
    # （op=13 验证 + 事件推送验签）并注入 SkillManager 取法供自答回落。
    try:
        from src.integrations.qq_official import register_qqbot_routes

        register_qqbot_routes(app, config_manager, telegram_client)
    except Exception:
        import logging as _log_qqbot

        _log_qqbot.getLogger("admin").debug("QQ 机器人 Webhook 注册跳过", exc_info=True)

    # ── 抖音官方通道（小程序 IM）Webhook（实施96 P1-1 骨架；douyin.enabled 为真才挂） ──
    # 验签 sha1(secret+body) + verify_webhook challenge 回显 + 幂等 + 进私 30 秒问候快路径；
    # 入站按 make_message 形状 emit_incoming → 收件箱 / System Z / B 线全部复用，不在此自答。
    try:
        from src.integrations.douyin_official import register_douyin_routes

        register_douyin_routes(app, config_manager, telegram_client)
    except Exception:
        import logging as _log_douyin

        _log_douyin.getLogger("admin").debug("抖音 Webhook 注册跳过", exc_info=True)

    # ── TikTok 官方通道（Business Messaging）Webhook（指令 TK-1 B 骨架；tiktok.enabled 为真才挂） ──
    try:
        from src.integrations.tiktok_official import register_tiktok_routes

        register_tiktok_routes(app, config_manager, telegram_client)
    except Exception:
        import logging as _log_tiktok

        _log_tiktok.getLogger("admin").debug("TikTok Webhook 注册跳过", exc_info=True)

    # ── 渠道接入教程页 /help/onboarding/{slug}（抖音企业版申请 / TikTok / 付款方式；2026-09-08 拍板）──
    # 与小智问答同一份数据（src/assistant/onboarding_guides.py）；session auth，按 ui_lang 中英。
    try:
        from src.web.routes.onboarding_guide_routes import register_onboarding_guide_routes

        register_onboarding_guide_routes(app, page_auth=_page_auth, templates=templates)
    except Exception:
        import logging as _log_obg

        _log_obg.getLogger("admin").debug("接入教程页注册跳过", exc_info=True)

    # ── LINE RPA（个人号自动聊天）Web 管理页 + REST ──
    try:
        from src.web.routes.line_rpa_routes import register_line_rpa_routes

        def _line_rpa_page_auth(request: Request):
            _require_role(request, "line_rpa")

        register_line_rpa_routes(
            app,
            page_auth=_line_rpa_page_auth,
            api_auth=_api_auth,
            templates=templates,
            config_manager=config_manager,
            audit_store=audit_store,
        )
    except Exception:
        import logging as _log_lr

        _log_lr.getLogger("admin").debug("LINE RPA 路由注册跳过", exc_info=True)

    # ── Messenger RPA（FB Messenger 个人号 RPA）Web + REST ──
    try:
        from src.web.routes.messenger_rpa_routes import (
            register_messenger_rpa_routes,
        )

        def _msgr_rpa_page_auth(request: Request):
            # 复用 line_rpa 角色（同等敏感度）；后续可以拆出独立 role
            _require_role(request, "line_rpa")

        register_messenger_rpa_routes(
            app,
            page_auth=_msgr_rpa_page_auth,
            api_auth=_api_auth,
            templates=templates,
            config_manager=config_manager,
            audit_store=audit_store,
        )
    except Exception:
        import logging as _log_mr

        _log_mr.getLogger("admin").debug(
            "Messenger RPA 路由注册跳过", exc_info=True
        )

    # ── WhatsApp RPA（个人号 / Business 号自动聊天）Web + REST ──
    try:
        from src.web.routes.whatsapp_rpa_routes import (
            register_whatsapp_rpa_routes,
        )

        def _wa_rpa_page_auth(request: Request):
            _require_role(request, "line_rpa")

        register_whatsapp_rpa_routes(
            app,
            page_auth=_wa_rpa_page_auth,
            api_auth=_api_auth,
            templates=templates,
            config_manager=config_manager,
            audit_store=audit_store,
        )
    except Exception:
        import logging as _log_wa

        _log_wa.getLogger("admin").debug(
            "WhatsApp RPA 路由注册跳过", exc_info=True
        )

    # ── RPA 跨平台总览（聚合 4 个平台 status / pending / alerts） ──
    try:
        from src.web.routes.rpa_overview_routes import (
            register_rpa_overview_routes,
        )

        def _rpa_overview_page_auth(request: Request):
            # 聚合页只读，复用 line_rpa 角色（与 4 个详情页同等敏感度）
            _require_role(request, "line_rpa")

        register_rpa_overview_routes(
            app,
            page_auth=_rpa_overview_page_auth,
            api_auth=_api_auth,
            templates=templates,
            config_manager=config_manager,
        )
    except Exception:
        import logging as _log_ov

        _log_ov.getLogger("admin").debug(
            "RPA 跨平台总览路由注册跳过", exc_info=True
        )

    # ── 统一收件箱（跨平台消息聚合 + 发送） ─────────────────────
    try:
        from src.web.routes.unified_inbox_routes import register_unified_inbox_routes

        def _unified_inbox_page_auth(request: Request):
            # 坐席工作台：master/admin/agent 可进（agent 仅此处可达）。
            # P1 2026-08-09：接受 Bearer 主令牌（与 _api_auth 同口径、恒时
            # 比较）——send/send-media/send-voice 是挂在页面鉴权下的 **JSON**
            # 端点，此前脚本/集成用主令牌调用会被 303 到登录页 HTML（.198/.104
            # 排障实测：为发一条验证消息只能模拟表单登录拿 session+CSRF）。
            # 主令牌本就拥有 _api_auth 全接口最高权限，此处放行不扩大权限面。
            if token:
                _auth_header = request.headers.get("Authorization", "")
                if hmac.compare_digest(_auth_header, f"Bearer {token}"):
                    return
            _require_role(request, "workspace")

        register_unified_inbox_routes(
            app,
            page_auth=_unified_inbox_page_auth,
            api_auth=_api_auth,
            templates=templates,
            config_manager=config_manager,
        )
    except Exception:
        import logging as _log_ui
        _log_ui.getLogger("admin").debug("统一收件箱路由注册跳过", exc_info=True)

    # ── 草稿审批工作台页面（B2：坐席/主管使用；API 路由由 main.py 挂） ──
    try:
        from src.web.routes.drafts_routes import (
            register_drafts_page_routes,
            register_agent_perf_routes,
            register_export_route,
            register_metrics_route,
            register_telemetry_route,
            register_report_route,
            register_broadcast_route,
            register_leaderboard_route,
            register_trend_route,
            register_my_perf_route,
            register_kb_archive_route,
            register_workspace_route,
            register_kb_stats_route,
            register_workload_route,
            register_ab_testing_route,
            register_anomaly_route,
            register_trace_route,
            register_glossary_route,
        )
        # J3: export API（主管数据导出）
        register_export_route(app, api_auth=_api_auth)
        # L1: metrics API（Prometheus 兼容，主管专属）
        register_metrics_route(app, api_auth=_api_auth)
        # 前端 dead-click 运行时错误上报（任意登录用户可写；读出并入 metrics）
        register_telemetry_route(app, api_auth=_api_auth)
        # P59: 术语库管理控制台 API（主管专属）
        register_glossary_route(app, api_auth=_api_auth)
        # M2: report API（工作日报/周报，主管专属）
        register_report_route(app, api_auth=_api_auth)
        # M2: broadcast API（EventBus 广播，供简报推送）
        register_broadcast_route(app, api_auth=_api_auth)
        # N3: leaderboard API（CSAT 坐席排行榜）
        register_leaderboard_route(app, api_auth=_api_auth)
        # O1: trend API（CSAT + Level 趋势）
        register_trend_route(app, api_auth=_api_auth)
        # O3: my-perf API（坐席自助绩效）
        register_my_perf_route(app, api_auth=_api_auth)
        # P2: KB archive API（优质回复存入知识库）
        register_kb_archive_route(app, api_auth=_api_auth)
        # P3: workspace CRUD API（多租户工作区）
        register_workspace_route(app, api_auth=_api_auth)
        # Q2+Q3: KB 命中率 + 质量统计 API
        register_kb_stats_route(app, api_auth=_api_auth)
        # R2: 坐席工作负荷 API
        register_workload_route(app, api_auth=_api_auth)
        # S1: A/B 测试 API
        register_ab_testing_route(app, api_auth=_api_auth)
        # S2: 异常检测 API
        register_anomaly_route(app, api_auth=_api_auth)
        # S3: 全链路追踪 API
        register_trace_route(app, api_auth=_api_auth)

        register_drafts_page_routes(
            app,
            page_auth=_unified_inbox_page_auth,
            templates=templates,
            config_manager=config_manager,
        )
        register_agent_perf_routes(
            app,
            api_auth=_api_auth,
            page_auth=_unified_inbox_page_auth,
            templates=templates,
            config_manager=config_manager,
        )
        # P0 2026-08-09：目标达成报表页（主管；数据走 /api/goals/report/*）
        from src.web.routes.goal_routes import register_goal_report_page
        register_goal_report_page(
            app,
            page_auth=_unified_inbox_page_auth,
            templates=templates,
            config_manager=config_manager,
        )
    except Exception:
        import logging as _log_dr
        _log_dr.getLogger("admin").debug("草稿/绩效路由注册跳过", exc_info=True)

    # ── WP-3：老板日报 /workspace/boss（任意登录角色；数据=value_report 日/周窗）──
    # 独立注册块：不与上方草稿链装配连坐（store 缺席时端点自答 available:false）。
    # 注意 _unified_inbox_page_auth 定义在上方局部作用域（~L3300），本块必须留在
    # 它之后——挪去文件前部会 NameError 被 try 静默吞掉（2026-08-17 首版实错）。
    try:
        from src.web.routes.boss_routes import register_boss_routes

        register_boss_routes(
            app, page_auth=_unified_inbox_page_auth, api_auth=_api_auth,
            templates=templates, config_manager=config_manager)
    except Exception:
        import logging as _log_boss

        _log_boss.getLogger("admin").warning("boss 路由注册失败", exc_info=True)

    # ── 账号资产中心（账号资产保全 P1，2026-08-19）：/workspace/assets ──
    # 独立注册块（同 boss_routes 例：不与草稿链装配连坐；须留在
    # _unified_inbox_page_auth 定义之后，挪前会 NameError 被 try 静默吞掉）。
    try:
        from src.web.routes.asset_center_routes import (
            register_asset_center_routes,
        )

        register_asset_center_routes(
            app, page_auth=_unified_inbox_page_auth, api_auth=_api_auth,
            templates=templates)
    except Exception:
        import logging as _log_ac

        _log_ac.getLogger("admin").warning("资产中心路由注册失败", exc_info=True)

    # ── Q-14 #262（2026-09-09）：「AI 本轮未生成」灰标读 / 清端点（独立块，同上例）──
    try:
        from src.web.routes.ai_fail_routes import register_ai_fail_routes

        register_ai_fail_routes(app, api_auth=_api_auth)
    except Exception:
        import logging as _log_aif

        _log_aif.getLogger("admin").warning("ai_fail 路由注册失败", exc_info=True)

    # ── Q-14 #262 E（2026-09-10）：告警「一眼看」只读页 /ops/glance（十分钟一次性令牌，
    # 令牌密钥 = web_admin.secret_key；默认占位符不铸令牌 → notifier 回落普通登录链接）──
    try:
        from src.utils import ops_glance_token as _ogt
        from src.web.routes.ops_glance_routes import register_ops_glance_routes

        _ogt.configure(secret)
        register_ops_glance_routes(app, templates=templates)
    except Exception:
        import logging as _log_og

        _log_og.getLogger("admin").warning("ops_glance 路由注册失败", exc_info=True)

    # ── P29: 实时队列看板页面 ──────────────────────────────────────
    @app.get("/workspace/queue")
    async def _ws_queue_monitor(request: Request):
        _unified_inbox_page_auth(request)
        sess = request.session
        try:
            _sm = getattr(request.app.state, "session_manager", None)
            if _sm is not None and not _sm.is_supervisor(sess.get("username") or ""):
                from fastapi.responses import RedirectResponse as _RR
                return _RR("/workspace")
        except Exception:
            pass
        ctx = {
            "request": request,
            "user_name": sess.get("username") or sess.get("agent_id") or "",
            "user_display_name": sess.get("display_name") or sess.get("username") or "",
        }
        try:
            ctx["site_name"] = (config_manager.config or {}).get("web_admin", {}).get("site_name", "")
        except Exception:
            pass
        return templates.TemplateResponse(request, "queue_monitor.html", ctx)

    # ── P37/P38: 工作流 + 路由规则管理页面 ───────────────────────────

    @app.get("/workflows")
    async def _ws_workflows_alias(request: Request):
        """E1：短链别名。工作台组件（cp-chain-exec「管理工作链」）、ops 漏斗卡
        与两批文档一直深链 ``/workflows``，而真实页面在 ``/workspace/workflows``
        ——上线以来 404（种子选择器让人不用离开会话，没人点过才没暴露）。
        别名一处修复全部历史链接；鉴权/锁定判定统一交给目标路由。"""
        from fastapi.responses import RedirectResponse as _RR
        return _RR("/workspace/workflows", status_code=302)

    @app.get("/workspace/workflows")
    async def _ws_workflows(request: Request):
        _unified_inbox_page_auth(request)
        # E1 页面级锁定态（判定与 API 闸门同源，防「API 拦了页面没拦」）：
        # license 锁 → 升级引导（nav-locked 同款去处）；运营关闭 → 404
        # （模块不存在于此部署；API 侧对应 403，页面语义按「无此页」处理）。
        try:
            from src.web.routes.unified_inbox_workflow_routes import (
                workflows_disabled_reason_cfg,
            )
            _wf_reason = workflows_disabled_reason_cfg(config_manager.config or {})
        except Exception:
            _wf_reason = ""
        if _wf_reason == "license":
            from fastapi.responses import RedirectResponse as _RR
            try:  # E6 锁触达观测
                from src.web.feature_lock_stats import get_feature_lock_stats
                get_feature_lock_stats().record("workflows", "page")
            except Exception:
                pass
            return _RR("/membership?from=workflows", status_code=302)
        if _wf_reason == "config":
            raise HTTPException(404)
        sess = request.session
        ctx = {
            "request": request,
            "user_name": sess.get("username") or sess.get("agent_id") or "",
            "user_display_name": sess.get("display_name") or sess.get("username") or "",
        }
        try:
            ctx["site_name"] = (config_manager.config or {}).get("web_admin", {}).get("site_name", "")
        except Exception:
            pass
        return templates.TemplateResponse(request, "workflows.html", ctx)

    # ── I3 模板库管理页面 ──────────────────────────────────────────
    @app.get("/workspace/templates")
    async def _ws_templates(request: Request):
        _unified_inbox_page_auth(request)
        sess = request.session
        return templates.TemplateResponse(
            request,
            "template_mgmt.html",
            {
                "request": request,
                "config_manager": config_manager,
                "user_name": sess.get("username") or sess.get("agent_id") or "",
                "user_display_name": sess.get("display_name") or sess.get("username") or "",
                "site_name": (config_manager.config or {}).get("web_admin", {}).get("site_name", ""),
            },
        )

    # ── 面向客户的网页聊天 Widget（web 渠道，公网；feature flag 默认关）──
    try:
        _wc_cfg = (config_manager.config or {}).get("web_chat", {}) or {}
        if _wc_cfg.get("enabled"):
            from src.web.routes.web_chat_routes import register_web_chat_routes
            register_web_chat_routes(app, config_manager=config_manager)
    except Exception:
        import logging as _log_wc
        _log_wc.getLogger("admin").debug("网页聊天 Widget 路由注册跳过", exc_info=True)

    # ── 歌房（唱歌能力管理）API ───────────────────────────────
    try:
        from src.web.routes.singing_routes import register_singing_routes

        register_singing_routes(app, api_auth=_api_auth,
                                config_manager=config_manager)
    except Exception:
        import logging as _log_sg
        _log_sg.getLogger("admin").warning("singing 路由注册失败", exc_info=True)

    # ── 专属歌订单（点唱台：列表/试听/人审放行=就地投递/打回）──
    try:
        from src.web.routes.song_order_routes import (
            register_song_order_routes,
        )

        register_song_order_routes(app, api_auth=_api_auth,
                                   config_manager=config_manager)
    except Exception:
        import logging as _log_so
        _log_so.getLogger("admin").warning("song_order 路由注册失败",
                                           exc_info=True)

    # ── Voice / TTS 统一试听 API ──────────────────────────────
    try:
        from src.web.routes.voice_routes import register_voice_routes

        register_voice_routes(app, api_auth=_api_auth, config_manager=config_manager)
    except Exception:
        import logging as _log_vr

        _log_vr.getLogger("admin").debug("Voice TTS 路由注册跳过", exc_info=True)

    # ── 实时共情语音通话网关（WebSocket，默认关：realtime_voice.enabled）──
    try:
        from src.web.routes.voice_live_routes import register_voice_live_routes

        register_voice_live_routes(app, api_auth=_api_auth, config_manager=config_manager)
    except Exception:
        import logging as _log_rv

        _log_rv.getLogger("admin").debug("实时语音通话路由注册跳过", exc_info=True)

    # ── Telegram 帐号设置页 ────────────────────────────────────
    try:
        from src.web.routes.telegram_routes import register_telegram_routes

        def _tg_page_auth(request: Request):
            _require_role(request, "settings")

        register_telegram_routes(
            app,
            page_auth=_tg_page_auth,
            api_auth=_api_auth,
            templates=templates,
            config_manager=config_manager,
            telegram_client=telegram_client,
            audit_store=audit_store,
        )
    except Exception:
        import logging as _log_tgr

        _log_tgr.getLogger("admin").debug("Telegram 路由注册跳过", exc_info=True)

    # ── 自动回复设置页（档位 / 回复速度 / 拟人链 / 语音，P0 2026-08-02）──
    try:
        from src.web.routes.reply_settings_routes import register_reply_settings_routes

        def _reply_settings_page_auth(request: Request):
            # 与「回复策略」同受众：master/admin（见 _PATH_TO_PAGE 权限键）
            _require_role(request, "strategies")

        register_reply_settings_routes(
            app,
            page_auth=_reply_settings_page_auth,
            api_auth=_api_auth,
            templates=templates,
            config_manager=config_manager,
        )
    except Exception:
        import logging as _log_rps

        _log_rps.getLogger("admin").debug("自动回复设置路由注册跳过", exc_info=True)

    # ── 新账号「AI 接管方式」确认（全自动/拟稿人审/关闭，P0 2026-08-30）──
    try:
        from src.web.routes.account_mode_routes import register_account_mode_routes

        register_account_mode_routes(
            app,
            api_auth=_api_auth,
            config_manager=config_manager,
        )
    except Exception:
        import logging as _log_am

        _log_am.getLogger("admin").debug("账号档位路由注册跳过", exc_info=True)

    # ── 双面板融合 API（能力注册表 / 驾驶权互斥锁，P0 2026-08-13）──
    try:
        from src.web.routes.surface_fusion_routes import (
            register_surface_fusion_routes,
        )

        register_surface_fusion_routes(
            app,
            api_auth=_api_auth,
            config_manager=config_manager,
        )
    except Exception:
        import logging as _log_sf

        _log_sf.getLogger("admin").debug("双面板融合路由注册跳过", exc_info=True)

    # ── 会话级接管 API（一键接管/交还，驾驶舱 P0 2026-08-13）──
    try:
        from src.web.routes.takeover_routes import register_takeover_routes

        register_takeover_routes(
            app,
            api_auth=_api_auth,
            config_manager=config_manager,
        )
    except Exception:
        import logging as _log_tko

        _log_tko.getLogger("admin").debug("会话接管路由注册跳过", exc_info=True)

    # ── 驾驶舱 API（介入优先级队列聚合，cockpit P1 2026-08-13）──
    try:
        from src.web.routes.cockpit_routes import register_cockpit_routes

        register_cockpit_routes(
            app,
            api_auth=_api_auth,
            config_manager=config_manager,
        )
    except Exception:
        import logging as _log_ck

        _log_ck.getLogger("admin").debug("驾驶舱路由注册跳过", exc_info=True)

    return app


def _register_domain_routes(app: FastAPI, ctx):
    """Auto-discover and register web routes from the active domain pack."""
    import importlib
    domain_name = ctx.domain_name
    if not domain_name:
        return

    routes_module_path = f"domains.{domain_name}.web.routes"
    try:
        mod = importlib.import_module(routes_module_path)
        if hasattr(mod, 'register_routes'):
            mod.register_routes(app, ctx)
            logging.getLogger("admin").info(
                "Domain '%s' web routes registered", domain_name
            )
    except ImportError:
        pass
    except Exception as e:
        logging.getLogger("admin").warning(
            "Failed to register domain '%s' web routes: %s", domain_name, e
        )
