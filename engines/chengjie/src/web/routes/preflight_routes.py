"""浏览器环境体检 API（P2，2026-07-31「人设切换失败」事故产品化沉淀）。

事故的教训：服务端全绿 ≠ 坐席的浏览器能用——写通道能不能过 CSRF 闸、SSE 通不通、
时钟偏不偏，都取决于**那台机器那个浏览器那条访问路径**。售前演示与私有化交付
恰恰跑在我们控制不了的环境里。本模块提供两个零副作用端点，配合
``golive_checklist.html`` 的「本机浏览器环境体检」段，把「演示当场翻车」变成
「进场先体检、红灯先处置」：

- ``POST /api/preflight/echo``  —— 写通道探针：**无任何副作用**，但要过完整的
  中间件链（鉴权 + CSRF）。响应回显本次请求靠哪张「通行证」放行
  （``request.state.csrf_ticket``，由 csrf_middleware 标注——单一事实源，
  不在此处复算判定）+ 凭证在场明细。诊断价值：``referer`` 放行＝这台浏览器
  没带显式凭证、随时会被隐私设置打回（正是事故形态的前夜）。
- ``GET /api/preflight/summary`` —— 服务器侧一览：server_ts（测时钟偏移）、
  前端构建戳、当前会话身份、AI 降级态（best-effort）。

鉴权＝任意登录用户（体检不含敏感信息；坐席自查同样需要）。
"""

from __future__ import annotations

import time
from pathlib import Path

from fastapi import Depends, Request


def register_preflight_routes(app, auth_dep):
    """注册浏览器环境体检端点。"""

    _build_file = (Path(__file__).resolve().parents[1]
                   / "static" / "workspace" / "ui-build.txt")

    @app.post("/api/preflight/echo")
    async def api_preflight_echo(request: Request, _=Depends(auth_dep)):
        """写通道无副作用探针（能到达这里＝已通过鉴权与 CSRF 闸）。"""
        ticket = str(getattr(request.state, "csrf_ticket", "") or "unknown")
        return {
            "ok": True,
            "ticket": ticket,
            "had": {
                "csrf_cookie": bool(request.cookies.get("csrf_token")),
                "csrf_header": bool(request.headers.get("X-CSRF-Token")),
                "origin": bool(request.headers.get("origin")),
                "referer": bool(request.headers.get("referer")),
                "bearer": request.headers.get("Authorization", "").startswith("Bearer "),
            },
        }

    @app.get("/api/preflight/summary")
    async def api_preflight_summary(request: Request, _=Depends(auth_dep)):
        """服务器侧体检一览（时钟基准/构建戳/会话身份/AI 降级态）。"""
        build = ""
        try:
            build = _build_file.read_text(encoding="utf-8").splitlines()[0].strip()
        except Exception:
            pass
        username = ""
        role = ""
        try:
            username = str(request.session.get("username", "") or "")
            role = str(request.session.get("role", "") or "")
        except Exception:
            pass
        ai_degraded = None
        try:
            # 与 /api/workspace/ai-runtime-status 同源：app.state.ai_client（无则不判）
            _ai = getattr(request.app.state, "ai_client", None)
            if _ai is not None and hasattr(_ai, "degradation_snapshot"):
                ai_degraded = bool((_ai.degradation_snapshot() or {}).get("degraded"))
        except Exception:
            pass
        return {
            "ok": True,
            "server_ts": time.time(),
            "build": build,
            "username": username,
            "role": role,
            "ai_degraded": ai_degraded,
        }
