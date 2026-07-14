# -*- coding: utf-8 -*-
"""service_auth.py — GPU 子服务统一访问控制 + CORS 收敛（被各服务 import）。

用法（在每个 *_server.py / *_api.py 创建 app 后一行接入）：

    import service_auth
    app = FastAPI(...)
    service_auth.secure(app, name="fish_tts")     # 鉴权中间件 + 收敛 CORS

设计要点：
  * 默认**关闭**：未设 AVATARHUB_SERVICE_TOKEN 也未设 AVATARHUB_SERVICE_ALLOW_IPS
    时完全不拦截（保持历史行为，零破坏）。
  * 启用后放行任一来源即可：回环 / 正确 X-AH-Svc 令牌 / 源 IP 命中白名单。
  * /health 与 OPTIONS 始终放行（健康探测、CORS 预检不受影响）。
  * CORS 由 '*' 收敛到 hub 来源 + 本机（app_config.service_cors_origins）。
仅依赖 fastapi（各服务本就具备）与 app_config（纯标准库）。
"""
from __future__ import annotations

import hmac
import os
from pathlib import Path

try:
    import app_config
except Exception:                      # 极端兜底：app_config 不可用时退化为不拦截
    app_config = None

_LOOPBACK = {"127.0.0.1", "::1", "localhost", "::ffff:127.0.0.1"}

# 自给自足兜底：当 app_config 缺失或为旧版(无 service_token/allow_ips)时，本模块
# 直接读 env + secrets/service_token.txt。这样可把本文件单独拷到"旧版 app_config"
# 的远端服务机(如 167/184)即可启用鉴权，无需同步改其 app_config.py。
_BASE = Path(__file__).resolve().parent
_TOKEN_FILE = _BASE / "secrets" / "service_token.txt"
_ALLOW_IPS_FILE = _BASE / "secrets" / "service_allow_ips.txt"


def _token_direct() -> str:
    ev = os.environ.get("AVATARHUB_SERVICE_TOKEN", "").strip()
    if ev:
        return ev
    try:
        if _TOKEN_FILE.exists():
            return _TOKEN_FILE.read_text(encoding="utf-8").strip()
    except Exception:
        pass
    return ""


def _ips_direct() -> set:
    """免令牌直连的源 IP：env(逗号分隔) ∪ secrets/service_allow_ips.txt(逗号或换行分隔)。
    文件式便于远端"只放行 hub IP"——hub 调用永不被挡(零 hub 重启)，其它机仍需令牌。"""
    raw = os.environ.get("AVATARHUB_SERVICE_ALLOW_IPS", "").strip()
    ips = {ip.strip() for ip in raw.split(",") if ip.strip()}
    try:
        if _ALLOW_IPS_FILE.exists():
            for tok in _ALLOW_IPS_FILE.read_text(encoding="utf-8").replace(",", "\n").split("\n"):
                tok = tok.strip()
                if tok and not tok.startswith("#"):
                    ips.add(tok)
    except Exception:
        pass
    return ips


def _cfg():
    tok = ""
    ips: set = set()
    # 优先用 app_config(hub 同机口径一致)；任一项取不到则回落到 env/令牌文件直读。
    if app_config is not None:
        try:
            tok = (app_config.service_token() or "") if hasattr(app_config, "service_token") else ""
        except Exception:
            tok = ""
        try:
            ips = (app_config.service_allow_ips() or set()) if hasattr(app_config, "service_allow_ips") else set()
        except Exception:
            ips = set()
    if not tok:
        tok = _token_direct()
    if not ips:
        ips = _ips_direct()
    return tok, ips, bool(tok or ips)


def secure(app, *, name: str = "svc", open_paths=("/health",), add_cors: bool = True):
    """给 FastAPI app 装上鉴权中间件 +（可选）收敛 CORS。返回是否启用了鉴权。

    add_cors=False：服务已自带 CORSMiddleware 时跳过，避免重复中间件导致响应头冲突
    （server-to-server 调用本不依赖 CORS；仅在服务无 CORS 时补一道 '*' 收敛）。"""
    from fastapi.responses import JSONResponse

    # 1) CORS 收敛（即便鉴权未开，也把 '*' 换成白名单，挡住跨源页面读取响应）
    if add_cors:
        from fastapi.middleware.cors import CORSMiddleware
        try:
            origins = app_config.service_cors_origins() if (app_config and hasattr(app_config, "service_cors_origins")) else ["http://127.0.0.1:9000"]
        except Exception:
            origins = ["http://127.0.0.1:9000"]
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
            allow_headers=["*"],
            expose_headers=["*"],
        )

    tok, allow_ips, enabled = _cfg()
    open_set = set(open_paths)

    # 鉴权探针：harden_remote -Mode verify/drill 专用；走同一套中间件策略，
    # hub 白名单→200、他机无令牌→401、带 X-AH-Svc→200。不放进 open_paths。
    @app.get("/__authprobe")
    async def _authprobe():
        return {"ok": True, "service": name, "auth": enabled}

    @app.middleware("http")
    async def _svc_auth(request, call_next):
        # 运行时重读配置：允许不重启服务即生效（env 在启动时已固定，这里主要兼容文件令牌轮换）
        token, ips, on = _cfg()
        if on and request.method != "OPTIONS" and request.url.path not in open_set:
            host = (request.client.host if request.client else "") or ""
            ok = host in _LOOPBACK
            if not ok and ips and host in ips:
                ok = True
            if not ok and token:
                sent = request.headers.get("X-AH-Svc", "")
                ok = bool(sent) and hmac.compare_digest(str(sent), str(token))
            if not ok:
                return JSONResponse(
                    {"ok": False, "detail": "服务访问被拒：需 X-AH-Svc 令牌或在白名单内（GPU 服务面加固）。"},
                    status_code=401)
        return await call_next(request)

    return enabled


def ws_authorized(ws) -> bool:
    """WebSocket 握手鉴权。FastAPI 的 `@app.middleware("http")`（即 secure() 装的中间件）
    **不覆盖 WebSocket**，故 WS 端点需在 handler 内显式调用本函数放行/拒绝。

    与 HTTP 中间件同一套策略：
      * 未启用鉴权(无令牌且无白名单) → 直接放行（向后兼容，零破坏）；
      * 启用后按 回环 / 白名单 IP / X-AH-Svc 令牌 任一命中即放行。
    浏览器 WebSocket API 无法设置自定义请求头 → 额外允许 `?svc=<令牌>` 查询参数兜底
    （供手机端页面等浏览器 WS 客户端携带令牌）。返回 True=放行。"""
    token, ips, on = _cfg()
    if not on:
        return True
    try:
        host = (ws.client.host if getattr(ws, "client", None) else "") or ""
    except Exception:
        host = ""
    if host in _LOOPBACK:
        return True
    if ips and host in ips:
        return True
    if token:
        sent = ""
        try:
            sent = ws.headers.get("X-AH-Svc", "") or ""
        except Exception:
            sent = ""
        if not sent:
            try:
                sent = ws.query_params.get("svc", "") or ""
            except Exception:
                sent = ""
        if sent and hmac.compare_digest(str(sent), str(token)):
            return True
    return False
