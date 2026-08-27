# -*- coding: utf-8 -*-
"""小智「手机扫码操控」路由（实施58 P4，2026-08-23）。

契约：
  POST /api/assistant/pair           → {ok, url, qr_b64, ttl_sec, lan_ok}
  GET  /xz?pair=<token>              → 核销 token → 建手机会话 → 当页落地
                                        （不 302，避免扫码 WebView 丢 cookie）
  GET  /xz                           → 手机操控页（复用 assistant-agent.js
                                        standalone 模式；无效会话出重扫提示页）
  GET  /api/assistant/pair/sessions  → 本人已连接手机列表
  POST /api/assistant/pair/revoke    → 踢下线（本人或 master）

细节：
- **二维码写局域网地址**：桌面壳常经 127.0.0.1 访问，QR 里的 host 必须是
  手机可达的 LAN IP——UDP connect 探测本机出口 IP（不真发包），失败回落
  请求 host。端口取请求端口（双实例各自正确）。
- 手机会话＝真实登录 session（同 uid/role，**无 jti**＝走兼容分支）+
  `xz_mobile` 标记；小智接口逐调用校验注册表（踢下线立即生效）；页面
  自身只挂小智接口，L2 起有确认卡与撤销兜底。
- qrcode 库缺席软降级：不给图只给 URL 文本（前端如实显示）。
"""

from __future__ import annotations

import base64
import io
import logging
import socket
from typing import Any, Dict

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse

from src.web.web_i18n import tr
from src.assistant import pairing
from src.web.routes.assistant_routes import _assistant_cfg, _session_user

logger = logging.getLogger(__name__)

_AGENT_JS_VER = "20260827a"  # 与 shared/assistant/assistant-agent.js VER 同步（门禁钉）


def _lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
        finally:
            s.close()
    except Exception:
        return ""


def _lan_reachable(host: str, port: int, timeout: float = 0.4) -> bool:
    """本机 hairpin 探 LAN 入口（portproxy 死了 = 手机扫码必失败）。"""
    h = str(host or "").strip()
    if not h or h in ("127.0.0.1", "::1", "localhost"):
        return True
    try:
        p = int(port or 0)
        if p <= 0:
            return False
        s = socket.create_connection((h, p), timeout=timeout)
        s.close()
        return True
    except Exception:
        return False


def is_prefetch_request(headers: Any) -> bool:
    """相机/微信预取：只探活不核销。headers 可以是 mapping 或 Starlette Headers。"""
    def _h(name: str) -> str:
        want = str(name or "").lower()
        try:
            v = headers.get(name) or headers.get(want)
            if v:
                return str(v)
            # 普通 dict 大小写敏感；相机/微信常见 Sec-Purpose / X-Purpose
            items = getattr(headers, "items", None)
            if callable(items):
                for k, val in items():
                    if str(k).lower() == want:
                        return str(val or "")
        except Exception:
            return ""
        return ""
    purpose = " ".join((_h("purpose"), _h("sec-purpose"), _h("x-purpose"),
                        _h("sec-fetch-purpose"))).lower()
    return any(x in purpose for x in ("prefetch", "preview", "prerender"))


def _qr_data_uri(url: str) -> str:
    try:
        import qrcode

        qr = qrcode.QRCode(box_size=6, border=2)
        qr.add_data(url)
        qr.make(fit=True)
        img = qr.make_image()
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return "data:image/png;base64," + base64.b64encode(
            buf.getvalue()).decode("ascii")
    except Exception:
        logger.debug("二维码生成失败（回落纯 URL）", exc_info=True)
        return ""


def _mobile_page_html(lang: str, uname: str, ver: str) -> str:
    zh = not str(lang or "").lower().startswith("en")
    tt = {
        "title": "小智 · 手机操控" if zh else "Assistant · Mobile Control",
        "hd": "🤖 小智手机操控" if zh else "🤖 Mobile Control",
        "who": ("已连接：" if zh else "Connected: ") + (uname or "-"),
        "hint": ("一句话交给小智：改设置前会先出确认卡，改完可撤销。"
                 if zh else
                 "Tell the assistant your goal; settings need confirm and "
                 "are undoable."),
        "ime": ("提示：点手机键盘上的 🎤 也能语音输入"
                if zh else "Tip: the keyboard mic also does voice input"),
        "a2hs": ("常用请「添加到主屏幕」：iPhone＝Safari 分享→添加到主屏幕；"
                 "安卓＝浏览器菜单→安装应用/添加到主屏幕"
                 if zh else
                 "Add to home screen: iOS Safari share menu / Android "
                 "browser menu → install app"),
    }
    # 纯静态骨架：交互全部由 assistant-agent.js standalone 模式接管
    return (
        "<!doctype html><html lang=\"" + ("zh" if zh else "en") + "\"><head>"
        "<meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1,"
        "viewport-fit=cover\">"
        # 真 PWA（2026-08-23 手机页体验包）：manifest + iOS A2HS 元信息——
        # 「添加到主屏幕」后是 standalone 全屏 app 体感而非浏览器书签。
        "<link rel=\"manifest\" href=\"/xz/manifest.webmanifest\">"
        "<meta name=\"theme-color\" content=\"#0d1020\">"
        "<meta name=\"apple-mobile-web-app-capable\" content=\"yes\">"
        "<meta name=\"apple-mobile-web-app-status-bar-style\" "
        "content=\"black-translucent\">"
        "<meta name=\"apple-mobile-web-app-title\" content=\""
        + ("小智" if zh else "Assistant") + "\">"
        "<link rel=\"apple-touch-icon\" href=\"/static/pwa/icon.svg\">"
        "<title>" + tt["title"] + "</title>"
        "<style>body{margin:0;font-family:system-ui,-apple-system,'Segoe UI',"
        "sans-serif;background:#0d1020;color:#e6e9f5;min-height:100vh}"
        ".xzm-hd{padding:14px 16px 6px;display:flex;flex-direction:column;gap:4px}"
        ".xzm-hd b{font-size:1.05rem}"
        ".xzm-hd .who{font-size:.72rem;color:#9aa3c0}"
        ".xzm-hint{margin:0 16px;padding:.55rem .7rem;border:1px solid #2a3152;"
        "border-radius:10px;font-size:.76rem;color:#b9c1de;background:#141a33}"
        ".xzm-ime{margin:6px 16px;font-size:.66rem;color:#7c86ad}"
        ".xzm-a2hs{position:fixed;left:0;right:0;bottom:0;padding:.45rem .9rem "
        "calc(.45rem + env(safe-area-inset-bottom));font-size:.62rem;"
        "color:#7c86ad;background:#0a0d1a;text-align:center}"
        "</style></head><body>"
        "<div class=\"xzm-hd\"><b>" + tt["hd"] + "</b>"
        "<span class=\"who\">" + tt["who"] + "</span></div>"
        "<div class=\"xzm-hint\">" + tt["hint"] + "</div>"
        "<div class=\"xzm-ime\">" + tt["ime"] + "</div>"
        "<div class=\"xzm-a2hs\">" + tt["a2hs"] + "</div>"
        "<script src=\"/assistant-shared/assistant-agent.js?v=" + ver +
        "\"></script>"
        "<script>if(window.XZAgent){window.XZAgent.init({standalone:true,"
        "shell:'workspace',lang:'" + ("zh" if zh else "en") + "'});}"
        "try{navigator.sendBeacon('/api/telemetry/ui-event',new Blob([JSON."
        "stringify({page:'/xz',action:'asb_pair_scan'})],{type:"
        "'application/json'}));}catch(e){}</script>"
        "</body></html>"
    )


def _preview_page_html(lang: str) -> str:
    zh = not str(lang or "").lower().startswith("en")
    msg = ("正在打开小智手机操控…" if zh else "Opening mobile control…")
    return ("<!doctype html><meta charset=\"utf-8\"><meta name=\"viewport\" "
            "content=\"width=device-width,initial-scale=1\">"
            "<title>小智</title><body style=\"margin:0;background:#0d1020;"
            "color:#e6e9f5;font-family:system-ui;display:flex;align-items:"
            "center;justify-content:center;min-height:100vh;font-size:.9rem\">"
            + msg + "</body>")


def _expired_page_html(lang: str) -> str:
    """过期/被踢页升级（2026-08-23）：一句红字 → 图文自助排查卡。
    扫码失败的两大真因（不同 WiFi / 码过期两分钟）直接写成检查清单。"""
    zh = not str(lang or "").lower().startswith("en")
    if zh:
        head = "连接已断开"
        steps = (
            ("1", "手机和电脑连的是同一个 WiFi 吗？", "手机用流量/热点打不开这个地址"),
            ("2", "二维码只有 2 分钟有效、扫一次就作废", "回电脑 → 小智面板 → 「📱 手机操控」→ 重新生成"),
            ("3", "电脑刚重启过？", "所有手机都会掉线，重扫一次就好"),
        )
        btn = "我重新扫好了 · 刷新"
    else:
        head = "Disconnected"
        steps = (
            ("1", "Same WiFi on phone and PC?", "Cellular/hotspot cannot reach this address"),
            ("2", "QR lives 2 minutes, single use", "PC → assistant panel → Phone control → Regenerate"),
            ("3", "PC restarted?", "All phones drop; just rescan"),
        )
        btn = "Rescanned · Refresh"
    rows = "".join(
        "<div style=\"display:flex;gap:10px;align-items:flex-start;"
        "margin:10px 0;text-align:left\">"
        "<span style=\"flex-shrink:0;width:22px;height:22px;border-radius:50%;"
        "background:#4f6ef7;color:#fff;font-size:.72rem;display:flex;"
        "align-items:center;justify-content:center;font-weight:700\">"
        + n + "</span><div><b style=\"font-size:.8rem\">" + a
        + "</b><div style=\"font-size:.7rem;color:#9aa3c0;margin-top:2px\">"
        + b2 + "</div></div></div>"
        for (n, a, b2) in steps)
    return ("<!doctype html><meta charset=\"utf-8\"><meta name=\"viewport\" "
            "content=\"width=device-width,initial-scale=1\">"
            "<body style=\"margin:0;background:#0d1020;color:#e6e9f5;"
            "font-family:system-ui;display:flex;align-items:center;"
            "justify-content:center;min-height:100vh;padding:24px;"
            "box-sizing:border-box\">"
            "<div style=\"max-width:340px;width:100%\">"
            "<div style=\"font-size:2rem;text-align:center\">📵</div>"
            "<h3 style=\"text-align:center;margin:.4rem 0 1rem\">" + head
            + "</h3>" + rows +
            "<button onclick=\"location.replace('/xz')\" style=\"margin-top:"
            "14px;width:100%;padding:.6rem;border:none;border-radius:10px;"
            "background:#4f6ef7;color:#fff;font-size:.85rem;font-weight:600;"
            "font-family:inherit\">" + btn + "</button></div></body>")


def register_assistant_pair_routes(app, ctx) -> None:
    _api_auth = ctx.api_auth
    config_manager = ctx.config_manager

    def _cfg() -> Dict[str, Any]:
        c = getattr(config_manager, "config", None)
        return c if isinstance(c, dict) else {}

    def _enabled_or_403(request: Request) -> None:
        if not _assistant_cfg(_cfg()).get("enabled"):
            raise HTTPException(403, tr(request, "asb.err.disabled"))

    def _lang(request: Request) -> str:
        return str(request.cookies.get("ui_lang") or "zh")

    # ------------------------------------------------------------ 签发
    @app.post("/api/assistant/pair")
    async def api_assistant_pair(request: Request):
        _api_auth(request)
        _enabled_or_403(request)
        uid, uname, role = _session_user(request)
        token = pairing.issue_pair_token(uid, uname, role)
        host = _lan_ip() or str(request.url.hostname or "")
        port = request.url.port or 80
        base = "http://" + host + ((":" + str(port)) if port else "")
        url = base + "/xz?pair=" + token
        lan_ok = _lan_reachable(host, port)
        return {"ok": True, "url": url, "qr_b64": _qr_data_uri(url),
                "ttl_sec": int(pairing.PAIR_TTL_SEC),
                "lan_ok": bool(lan_ok), "lan_host": host}

    # ------------------------------------------------------------ 落地/页面
    @app.get("/xz")
    async def xz_mobile_page(request: Request):
        tok = str(request.query_params.get("pair") or "").strip()
        if tok:
            # 相机/微信预取只探活：不核销、不建会话，避免废码。
            if is_prefetch_request(request.headers):
                if pairing.peek_pair_token(tok):
                    return HTMLResponse(_preview_page_html(_lang(request)),
                                        status_code=200)
                return HTMLResponse(_expired_page_html(_lang(request)),
                                    status_code=410)
            ent = pairing.consume_pair_token(tok)
            if not ent:
                return HTMLResponse(_expired_page_html(_lang(request)),
                                    status_code=410)
            msid = str(ent.get("msid") or "")
            if not msid:
                msid = pairing.register_mobile(
                    ent["uid"], ent["uname"], ent["role"],
                    ua=str(request.headers.get("user-agent") or ""))
                pairing.attach_msid(tok, msid)
            request.session["user_id"] = ent["uid"]
            request.session["username"] = ent["uname"]
            request.session["role"] = ent["role"]
            request.session["display_name"] = ent["uname"]
            request.session["xz_mobile"] = msid
            # 不 302：扫码 WebView 常丢 302 上的 Set-Cookie（SameSite=Strict
            # + 相机来源）。同一响应当页落地，后续同源 XHR 才能带上会话。
            uname = str(ent.get("uname") or "")
            return HTMLResponse(_mobile_page_html(_lang(request), uname,
                                                  _AGENT_JS_VER))
        if not request.session.get("user_id"):
            return HTMLResponse(_expired_page_html(_lang(request)),
                                status_code=401)
        msid = str(request.session.get("xz_mobile") or "")
        if msid and not pairing.mobile_ok(msid):
            request.session.clear()
            return HTMLResponse(_expired_page_html(_lang(request)),
                                status_code=401)
        uname = str(request.session.get("display_name")
                    or request.session.get("username") or "")
        return HTMLResponse(_mobile_page_html(_lang(request), uname,
                                              _AGENT_JS_VER))

    # ------------------------------------------------------------ PWA
    @app.get("/xz/manifest.webmanifest")
    async def xz_manifest(request: Request):
        """手机操控页 manifest（免鉴权：纯静态元数据零敏感字段；会话过期时
        manifest 拉取失败会破坏安装性）。图标复用 /static/pwa 的 SVG。"""
        zh = not _lang(request).lower().startswith("en")
        from fastapi.responses import JSONResponse

        return JSONResponse(
            {
                "name": "小智 · 手机操控" if zh else "Assistant Mobile",
                "short_name": "小智" if zh else "Assistant",
                "start_url": "/xz",
                "scope": "/xz",
                "display": "standalone",
                "background_color": "#0d1020",
                "theme_color": "#0d1020",
                "icons": [
                    {"src": "/static/pwa/icon.svg", "sizes": "any",
                     "type": "image/svg+xml", "purpose": "any"},
                    {"src": "/static/pwa/icon-maskable.svg", "sizes": "any",
                     "type": "image/svg+xml", "purpose": "maskable"},
                ],
            },
            media_type="application/manifest+json",
        )

    # ------------------------------------------------------------ 管理
    @app.get("/api/assistant/pair/sessions")
    async def api_assistant_pair_sessions(request: Request):
        _api_auth(request)
        _enabled_or_403(request)
        uid, _uname, role = _session_user(request)
        rows = pairing.list_mobile(None if role == "master" else uid)
        return {"ok": True, "sessions": rows}

    @app.post("/api/assistant/pair/revoke")
    async def api_assistant_pair_revoke(request: Request):
        _api_auth(request)
        _enabled_or_403(request)
        uid, _uname, role = _session_user(request)
        data = await request.json()
        msid = str(data.get("msid") or "")
        rows = {r["msid"]: r for r in pairing.list_mobile(None)}
        ent = rows.get(msid)
        if not ent:
            raise HTTPException(404, tr(request, "asb.pair.missing"))
        if role != "master" and ent.get("uid") != uid:
            raise HTTPException(403, tr(request, "asb.act.forbidden"))
        pairing.revoke_mobile(msid)
        return {"ok": True}
