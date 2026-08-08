"""i18n 词典出口（两个互补端点，共用「web_i18n + packs 单一事实源」）。

① 静态壳前缀切片 ``GET /api/i18n/bundle``（D1，2026-08-01）：
   `/copilot/app.html`（单一前端 App，桌面壳 iframe 与网页 🧪 模式共用）是静态
   文件，拿不到模板注入的 `window.T` 词典；cp-goal 这类组件的 `inbox.goal.*` 键走
   `window.T` → 在静态壳里只能裸奔键名。本端点让静态宿主按需拉取键子集，装成
   `window.T/Tf` shim。需登录（api_auth）；prefix 必填、逗号分隔、每段 ≥4 字符
   （禁空前缀全量倾倒）；响应键数上限 800（切片语义，不是全量出口）。

② 整包外链 ``GET /i18n/ws-i18n.js``（P2 传输减重，2026-08-07）：
   `_i18n_bootstrap.html` 此前把整本合并词典 ``window.WS_I18N = {{ i18n|tojson }}``
   内联进**每一次**页面渲染——zh 词典 11,900+ 键 / tojson 后 ~877KB / ~200KB gzip，
   占工作台页 552KB 传输的三分之一强，且跨页逐字节相同。托管租户走 ~30KB/s 反向
   SSH 隧道，这一块每次导航白付 ~7s。现改**内容指纹版本化 URL** + immutable 一年
   缓存（与 CachedStaticFiles 对 `?v=` 资源同策略）——首次访问之后，全站任何页面
   导航不再传词典；指纹随词典热重载自动翻新，改词条=换 URL，永不读到陈旧字典。
   **刻意公开**（不挂鉴权）：登录页本身就需要词典，且词典内容=界面文案无敏感信息
   （与 /static 同级公开面）。
   **restart 安全契约**：`_i18n_bootstrap.html` 按 ``i18n_fp is defined`` 分支——旧
   进程（本端点未装载）渲染时上下文无 i18n_fp → 回落内联词典，行为与历史一致；
   新进程注入 i18n_fp → 走外链。模板热更新先于进程重启落地也不会 404。

⚠ 2026-08-07 撞车教训：两条线同小时在本文件各写了一个「i18n bundle」——①是静态壳
  切片、②是整包外链，用途正交但重名撞文件。合流保双端点；动本文件前先看意向板。
"""

from __future__ import annotations

import json
import logging

from fastapi import HTTPException, Request
from fastapi.responses import Response

from src.web.web_i18n import tr

logger = logging.getLogger(__name__)

_MAX_KEYS = 800
_MIN_PREFIX_LEN = 4
_LANGS = ("zh", "en", "vi")
_LOCALES = {"zh": "zh-CN", "en": "en-US", "vi": "vi-VN"}

# (lang, fp) → 已构建的整包响应字节。词典代际更替后旧键无人再引用，cap 兜底防积累。
_BUNDLE_CACHE: dict = {}


def _build_full_bundle(lang: str) -> tuple[bytes, str]:
    """构建 ``window.WS_I18N=...`` JS 字节（utf-8），返回 (body, fp)。"""
    from src.web.web_i18n import get_translations, get_translations_fingerprint

    fp = get_translations_fingerprint(lang)
    cached = _BUNDLE_CACHE.get((lang, fp))
    if cached is not None:
        return cached, fp
    d = get_translations(lang)
    try:
        blob = json.dumps(d, ensure_ascii=False, separators=(",", ":"))
        body = (f"window.WS_I18N={blob};"
                f'window.WS_LANG="{lang}";'
                f'window.WS_LOCALE="{_LOCALES[lang]}";').encode("utf-8")
    except UnicodeEncodeError:
        # 词条里混入孤立代理字符（历史上 EN 有过 \ud83d\udd04 成对代理转义）——
        # ensure_ascii 转义档永不编码失败，牺牲体积保可用。
        blob = json.dumps(d, ensure_ascii=True, separators=(",", ":"))
        body = (f"window.WS_I18N={blob};"
                f'window.WS_LANG="{lang}";'
                f'window.WS_LOCALE="{_LOCALES[lang]}";').encode("ascii")
        logger.warning("i18n bundle(%s) 含孤立代理字符，已回落 ensure_ascii（查词条里的 \\ud 转义）", lang)
    if len(_BUNDLE_CACHE) > 12:
        _BUNDLE_CACHE.clear()
    _BUNDLE_CACHE[(lang, fp)] = body
    return body, fp


def register_i18n_bundle_routes(app, *, api_auth) -> None:
    """挂载 ①静态壳切片端点（需登录） + ②整包外链端点（公开）。"""

    @app.get("/api/i18n/bundle")
    async def api_i18n_bundle(request: Request, prefix: str = "", lang: str = ""):
        api_auth(request)
        lg = str(lang or getattr(getattr(request, "state", None), "ui_lang", "") or "zh").lower()
        if lg not in _LANGS:
            lg = "zh"
        prefixes = [p.strip() for p in str(prefix or "").split(",")
                    if len(p.strip()) >= _MIN_PREFIX_LEN]
        if not prefixes:
            raise HTTPException(400, tr(request, "err.ws.field_required", field="prefix"))
        from src.web.web_i18n import get_translations
        merged = get_translations(lg) or {}
        out = {}
        for k, v in merged.items():
            if any(k.startswith(p) for p in prefixes):
                out[k] = v
                if len(out) >= _MAX_KEYS:
                    break
        return {"ok": True, "lang": lg, "count": len(out), "keys": out}

    @app.get("/i18n/ws-i18n.js")
    async def ws_i18n_bundle(request: Request):
        lang = (request.query_params.get("lang") or "zh").strip().lower()
        if lang not in _LANGS:
            lang = "zh"
        body, fp = _build_full_bundle(lang)
        return Response(
            content=body,
            media_type="application/javascript; charset=utf-8",
            headers={
                # URL 带内容指纹（模板注入 ?v=<fp>）→ 可以放心 immutable 一年；
                # 词条变化 = 指纹变化 = 新 URL，陈旧缓存自然失联。
                "Cache-Control": "public, max-age=31536000, immutable",
                "X-I18N-FP": fp,
            },
        )
