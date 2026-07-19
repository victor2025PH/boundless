"""platform/leadbus 契约的承接端（服务端一侧）：POST /api/leadbus/ingest、GET /api/leadbus/status。

leadbus 是获客产品（huoke/zhituo 等）→ 承接中台（chengjie）之间的线索交接总线，契约见
``platform/leadbus/lead_schema.json``（信封结构）与 ``platform/leadbus/client.py``
（瘦客户端 ``LeadBusClient.publish()``：可降级、联不通就落本地 outbox 待重投，永不抛异常）。

本路由是该契约在服务端一侧的镜像实现：复用 ``ingest_incoming``（M6① protocol↔收件箱桥接的
统一落库入口）把一条线索**转译**成统一收件箱里的一条入站消息，使其进入既有的会话列表 →
坐席认领 → SLA 全套后续流程。坐席真正的认领/分配是正交的后置步骤，不在本路由职责内
（``assign_hint`` 目前只是原样接收、随信封透传给 ``ingest_incoming(source=...)``，不触发
任何自动分配逻辑）。

语义提醒：leadbus 信封传的是「捕获到一个潜在线索（一个人）」，不是「一条聊天消息」——
本路由据此把 ``text`` 合成为占位文案而非转发任何真实聊天正文，详见 ``api_leadbus_ingest``
内的注释。

鉴权：全程 ``Depends(api_auth)``。已确认本项目里唯一的「机器/worker 对服务端」入站先例
（``/api/internal/protocol/ingest``，Baileys 等 Node worker 推送消息用）用的也是同一个
``api_auth``（其 `_api_auth` 同时支持后台 session 与静态 ``Authorization: Bearer <token>``，
后者即本项目的机器对机器鉴权方式）——没有发现另一套专门给机器对机器用的鉴权惯例，故沿用。
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

from fastapi import Depends, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)


def register_leadbus_routes(app, *, api_auth, config_manager=None) -> None:
    """挂载 leadbus 承接端点。

    ``config_manager``：当前未使用，仅为与同类 ``register_*_routes``（如
    ``register_care_routes``）保持一致的签名，留作后续按域/人设配置做分流的余地。
    """

    def _inbox_store(request: Request):
        """统一收件箱 store：与 unified_inbox_desktop_routes / unified_inbox_services
        同一取法——由 main.py 在启动时挂到 app.state，未挂载（或测试环境未设置）时为 None。
        """
        return getattr(request.app.state, "inbox_store", None)

    def _parse_envelope_ts(raw: Any) -> float:
        """leadbus 信封的 ``ts`` 是 ISO8601 UTC 字符串（client.py 用
        ``time.strftime(...) + "Z"`` 自动补），与 deep_persona 里同款「Z→+00:00」宽松解析
        保持一致写法。解析失败/缺失都回落当前时间——ts 只影响会话预览排序，非关键业务字段，
        不应因为格式问题让整条线索 ingest 失败。
        """
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            return float(raw)
        s = str(raw or "").strip()
        if not s:
            return time.time()
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except Exception:
            return time.time()

    @app.get("/api/leadbus/status")
    async def api_leadbus_status(request: Request, _=Depends(api_auth)):
        """承接入口是否就绪（对应 lead_schema.json#status_endpoint）。

        ``LeadBusClient.available()`` 会读本端点的 ``ready`` 字段（并自行 setdefault
        ``available=True``，因为 HTTP 能连到就算 available）——这里只需老实回答「入口本身
        是否具备落库能力」，即统一收件箱 store 是否已挂载。
        """
        return {"ready": _inbox_store(request) is not None}

    @app.post("/api/leadbus/ingest")
    async def api_leadbus_ingest(request: Request, _=Depends(api_auth)):
        """接收 platform/leadbus 信封，转译为一条入站消息落进统一收件箱。

        响应形状对应 lead_schema.json#response（``available`` 由客户端
        ``LeadBusClient.publish()`` 的 ``setdefault`` 兜底补上，本端点不需要填）。

        - 信封缺最小必填字段 → HTTP 400 + ``{"ok": False, "reason": ...}``（明确的客户端错误，
          不应被当作「总线暂时不可用」去排队重投——但即便如此，client.py 也只把「非
          available」一律落 outbox 重投，重投同一条坏信封仍会 400，属于上游获客产品自身
          数据问题，需人工排查，不是本端点能兜底的范围）。
        - store 不可用 / ``ingest_incoming`` 抛异常 → 软降级 ``{"ready": False}``，
          不 500（与 deferred_outbox_routes 对「依赖缺失」的降级风格一致）——这类才是
          「总线侧暂时没准备好」，客户端会落本地 outbox 待总线恢复后自动补投，线索不丢。
        """
        try:
            body = await request.json()
        except Exception:
            body = {}

        envelope = (body or {}).get("lead")
        if not isinstance(envelope, dict):
            return JSONResponse(status_code=400, content={
                "ok": False, "reason": "missing_envelope",
                "message": "body.lead 必填且须为对象",
            })

        source = envelope.get("source")
        source = source if isinstance(source, dict) else {}
        lead = envelope.get("lead")
        lead = lead if isinstance(lead, dict) else {}

        product = str(source.get("product") or "").strip()
        platform = str(source.get("platform") or "").strip()
        external_id = str(lead.get("external_id") or "").strip()
        if not product or not platform or not external_id:
            return JSONResponse(status_code=400, content={
                "ok": False, "reason": "missing_required_field",
                "message": "lead.source.product / lead.source.platform / "
                           "lead.lead.external_id 必填",
            })

        store = _inbox_store(request)
        if store is None:
            return {"ready": False}

        lead_id = str(envelope.get("lead_id") or "")
        handle = str(lead.get("handle") or "")
        profile = lead.get("profile") if isinstance(lead.get("profile"), dict) else {}
        assign_hint = envelope.get("assign_hint")
        assign_hint = assign_hint if isinstance(assign_hint, dict) else {}

        # 字段映射取舍（按任务要求逐条说明设计判断）：
        # - chat_key = external_id 原样（如 "tg:123"），不拆分平台前缀出来——保持简单，
        #   且与 client.py 的幂等/去重语义一致（同一 external_id 落同一条收件箱会话）。
        # - account_id：ingest_incoming 要求账号维度落库，但 leadbus 信封没有账号概念
        #   （获客侧常见多账号池轮转捕获，线索本身不归属某个具体账号）。用 source.product
        #   （获客产品线，如 zhituo）顶替——近似「这条线索来自哪条产品线的获客账号池」，
        #   比留空或瞎填更有业务意义，也让同一产品线的线索会话在收件箱里聚在一起。
        # - text：leadbus 语义是「捕获到一个潜在线索」，不是「收到一条聊天消息」——这里
        #   没有真实聊天正文可转发，故合成一句占位文案，只用于会话列表预览/首条消息展示，
        #   不代表任何用户真实发言；坐席据此一眼可辨认「这是待激活的线索」而非常规对话。
        # - assign_hint/profile/campaign/lead_id：目前只原样透传进 ingest_incoming 的
        #   source 参数（供本次调用内部的 chat_type 推断等复用同一 source 字典），
        #   *不*会被持久化成可查询的独立列——InboxMessage/InboxConversation 当前的表结构
        #   只认 reply_to/mentions/sender_id/sender_name 等已知键，没有给 leadbus 专属字段
        #   开对应列。也就是说这里的“记录”仅止于“接收、不丢弃、原样带过这一次调用”，不是
        #   「以后可在收件箱数据库里查到这些字段」。若后续需要真正的可查询留痕/坐席分配，
        #   需要新开列或旁路存储，超出本次「只新增一个路由文件」的边界，留给后续任务。
        text = f"[线索捕获] {handle or external_id}"

        try:
            from src.integrations.protocol_bridge import ingest_incoming
            conversation_id = ingest_incoming(
                store,
                platform=platform,
                account_id=product,
                chat_key=external_id,
                name=handle,
                text=text,
                ts=_parse_envelope_ts(envelope.get("ts")),
                direction="in",
                source={
                    "leadbus": True,
                    "lead_id": lead_id,
                    "campaign": str(source.get("campaign") or ""),
                    "profile": profile,
                    "assign_hint": assign_hint,
                },
            )
        except Exception:
            logger.warning("[leadbus] ingest_incoming 落库失败", exc_info=True)
            return {"ready": False}

        return {
            "ready": True,
            "assigned": conversation_id is not None,
            "lead_id": lead_id,
            "session_id": conversation_id,
        }


__all__ = ["register_leadbus_routes"]
