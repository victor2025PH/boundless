"""platform/enable 赋能能力网关契约 · translate 部分（chengjie 侧服务端实现）。

对应 platform/enable/enable_schema.json 的 ``translate`` 定义与
platform/enable/client.py 的 ``EnableClient.translate()`` 调用面：

- ``POST /api/translate``：{text, to_lang, from_lang?} → {text, detected_lang, chars}。
- ``GET /api/enable/status``：chengjie 翻译栈就绪探针（``translate_ready``），供运维/
  上游按能力粒度探测（当前 platform/enable/client.py 的 ``status()``/``available()``
  仍只探 avatarhub，见 CONTRACT.md 归属表；本端点先落地 chengjie 侧的对等探针，
  接线到瘦客户端属于该契约的下一阶段）。

TranslationService 复用既有单例获取方式 ``unified_inbox_services._get_translation_service``
（经 ``request.app.state.translation_service`` 懒建/缓存，ai_client 取自
``request.app.state.ai_client``）——与 unified-inbox 翻译路由共享同一实例/缓存/术语库
配置，不新造一条初始化路径。

隐私红线（enable_schema.json 明文要求）：``text`` 原文只作本次请求的临时载荷，本模块
不打印、不写日志、不落任何持久化存储；计量只用字符数（``chars``）。
"""
from __future__ import annotations

import logging

from fastapi import Depends, HTTPException, Request

from src.web.routes.unified_inbox_services import _get_translation_service
from src.workspace.inbound_translate import _engines_available

logger = logging.getLogger(__name__)


def register_enable_routes(app, *, api_auth, config_manager=None) -> None:
    """挂载 platform/enable translate 契约的两个端点。

    ``config_manager`` 当前未使用——保留形参只是为了和 care_routes/deferred_outbox_routes
    等姊妹路由的注册签名一致，方便 admin.py 用同一套 kwargs 统一调用。
    """

    async def _body(request: Request) -> dict:
        try:
            return await request.json() or {}
        except Exception:
            return {}

    @app.post("/api/translate")
    async def api_enable_translate(request: Request, _=Depends(api_auth)):
        """契约映射：调 TranslationService.translate() 后按 ok 分两条路径收敛响应。

        关键设计（契约要求把这段说明放进代码注释）：``result.ok is False``
        （例如未配置任何翻译 provider/key，见 TranslationService.translate 的
        provider_unavailable 分支）时**依然返回 HTTP 200**，body 为
        ``{"available": false, "error": ...}``，而不是 4xx/5xx。

        原因：platform/enable/client.py 用 HTTP 传输层结果（连接失败/超时/HTTP
        错误码）判断"引擎完全不可达"；用 200 响应体里的 ``available`` 字段判断
        "引擎在线但这次能力不可用"——这是调用方两条不同的降级路径：不可达可能值得
        重试或告警，不可用应直接按 CONTRACT.md §4 退化发原文，重试没有意义。若这里
        对 ok=False 也返回非 2xx，"翻译栈健康但没配 key"与"chengjie 整个挂了"在
        客户端观测上就会变得无法区分。
        """
        body = await _body(request)
        text = str(body.get("text") or "")
        to_lang = str(body.get("to_lang") or "").strip()
        from_lang = str(body.get("from_lang") or "").strip()
        if not text.strip():
            raise HTTPException(status_code=400, detail="text 必填")
        if not to_lang:
            raise HTTPException(status_code=400, detail="to_lang 必填")

        svc = _get_translation_service(request)
        try:
            result = await svc.translate(text, target_lang=to_lang, source_lang=from_lang)
        except Exception as ex:  # noqa: BLE001
            # 契约保证 translate() 不抛（无 provider 时走 ok=False 分支）；这里只
            # 兜底真正意外的引擎异常，绝不把原文写进日志（隐私红线），只记异常类型名。
            logger.warning("[enable/translate] 引擎调用异常，已降级：%s", type(ex).__name__)
            return {"available": False, "error": "translate_failed"}

        if not result.ok:
            return {"available": False, "error": result.error or "translate_failed"}
        return {
            "text": result.translated_text,
            "detected_lang": result.source_lang,
            "chars": len(text),
        }

    @app.get("/api/enable/status")
    async def api_enable_status(request: Request, _=Depends(api_auth)):
        """翻译能力就绪探针。``translate_ready`` 复用 EngineRouter.any_available()
        （经 src.workspace.inbound_translate._engines_available 的现成封装——与入站
        自动翻译 B8 三态判定同一套"引擎可用"定义，不另造一份判断逻辑）。"""
        svc = _get_translation_service(request)
        return {"available": True, "translate_ready": _engines_available(svc)}


__all__ = ["register_enable_routes"]
