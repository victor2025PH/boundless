"""静态壳 i18n bundle（D1）——把服务端合并词典按前缀切片给**无 Jinja 渲染**的宿主。

背景：`/copilot/app.html`（单一前端 App，桌面壳 iframe 与网页 🧪 模式共用）是静态
文件，拿不到模板注入的 `window.T` 词典；cp-goal 这类组件的 `inbox.goal.*` 键走
`window.T` → 在静态壳里只能裸奔键名。本端点让静态宿主按需拉取键子集，装成
`window.T/Tf` shim——**单一事实源仍是 web_i18n + packs**（合并视图现读，pack 热更新
自然生效），绝不在前端复制第二份词典。

契约：
- ``GET /api/i18n/bundle?prefix=inbox.goal.[,更多前缀]&lang=zh|en|vi``
- 需登录（api_auth；静态壳 iframe 经 Bearer/cookie 均可）；
- prefix 必填、逗号分隔、**每段 ≥4 字符**（禁空前缀全量倾倒）；
- 响应键数上限 800（切片语义，不是全量出口）；lang 缺省跟随请求语言。
"""

from __future__ import annotations

from fastapi import HTTPException, Request

from src.web.web_i18n import tr

_MAX_KEYS = 800
_MIN_PREFIX_LEN = 4
_LANGS = ("zh", "en", "vi")


def register_i18n_bundle_routes(app, *, api_auth) -> None:
    """挂载静态壳 i18n bundle 端点。"""

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
