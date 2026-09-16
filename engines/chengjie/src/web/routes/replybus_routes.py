"""platform/replybus 契约服务端实现 —— chengjie 承接大脑侧『决策回执』端点。

契约：platform/replybus/CONTRACT.md + reply_schema.json。产业链去重后的分工：
**TG-AI智控王 = 执行层**（持有 Telegram `.session`、真正发消息），**chengjie（本
引擎）= 承接大脑**（只决定『怎么回』）。智控王收到入站私信 → 组装消息信封 → 问
本端点『这条怎么回』→ 本端点返回决策 → 智控王在**自己的 session 上**执行发送。
本文件是这条同步问答通道在 chengjie 侧的落地（`POST decide` + `GET status`）。

★★★ 防双发铁律（CONTRACT.md §5，本文件不可逾越的红线）★★★
    chengjie 只返回决策，绝不代发。本端点实现里刻意不 import、不调用任何"发消息
    到 Telegram/WhatsApp/其它平台"的函数——已核实复用的 ``generate_persona_reply``
    （src/inbox/persona_reply.py）及其内部 ``SkillManager.generate_inbox_draft`` 主
    路径都明确是"零发送副作用"：产出仅是一段文本，是否真正外发完全由智控王侧
    决定。唯一的写副作用是 ``SkillManager`` 的情景记忆落库（记事实供下次对话用，
    不是发消息），与"发送"无关，不触碰本红线。响应体也**不含**、且永远不应加入
    sent/delivered 等"已发送"语义字段——见 ``_build_decision``。

★ 决策口径：为什么落 draft 而不是 send（详细理由见 ``_build_decision`` 文档字符串）。

★ 隐私边界（CONTRACT.md §6）★
    ``message.text``（入站私信原文）只作本次请求的临时处理载荷，本文件任何路径都
    不把它写进 print/logger/落盘——包括异常兜底路径（``exc_info=True`` 只落
    traceback，日志里从不拼接业务文本变量）。``generate_persona_reply`` 内部既有的
    历史落库/记忆行为是它自身既有职责，不在本文件改动范围内。
"""
from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import Depends, Request

from src.inbox.persona_reply import generate_persona_reply

logger = logging.getLogger(__name__)

# 服务端允许产出的决策动作。"fallback" 故意不在其中——按 CONTRACT.md §3/§4，
# fallback 只由 platform/replybus/client.py 在总线不可达/决策非法时本地合成；
# 服务端若返回它，瘦客户端的 action 白名单（client.py ACTIONS）会把它当"非法动作"
# 再收敛一次 fallback，等于绕一圈没有意义，故本端点从不产出它。"send" 目前也不
# 产出，理由见 _build_decision。
_ACTION_DRAFT = "draft"
_ACTION_SILENT = "silent"
# 2026-09-16 起：``replybus.send_domains``（配置列表）里列出的域，有效回复直接判 send。
# 默认空列表 = 行为与此前完全一致（永远 draft）。见 _build_decision / _send_allowed。
_ACTION_SEND = "send"


def _send_allowed(config_manager: Any) -> bool:
    """当前激活域是否在 ``replybus.send_domains`` 白名单里（显式、逐域、默认关）。

    这是「把某些场景升级为直发」那次独立产品决策的落点：智拓故事矿阵（story_matrix）
    的 Messenger 前台在自己一侧还有两道硬闸（无 AI 字样 / persona 与频道一致）且
    流程性回复全由其状态机处理，只把流程外自由聊天交给本端点——这类调用方拿到
    draft 等于拿到「不可用」。其余域不受影响。任何读取异常都按不允许处理。
    """
    try:
        cfg = getattr(config_manager, "config", None) or {}
        if not isinstance(cfg, dict):
            return False
        allow = (cfg.get("replybus") or {}).get("send_domains") or []
        if isinstance(allow, str):
            allow = [allow]
        allow = {str(x).strip().lower() for x in allow if str(x).strip()}
        if not allow:
            return False
        from src.utils.domain_policy import effective_domain_name
        return effective_domain_name(cfg).strip().lower() in allow
    except Exception:
        return False


def _ai_ready(app: Any) -> bool:
    """决策管线（人设/话术）是否已加载——供 ``GET /api/replybus/status`` 探测。

    探测口径与 ``generate_persona_reply`` 内部的产线发现逻辑保持一致（单一事实源：
    有 ``skill_manager.ai_client`` 或直连 ``ai_client`` 任一即视为就绪），只读属性
    存在性、不发起任何真实 AI/网络调用，可安全地在生产热路径上随时探测。
    """
    try:
        state = getattr(app, "state", None)
        sm = getattr(state, "skill_manager", None)
        if sm is None:
            tc = getattr(state, "telegram_client", None)
            sm = getattr(tc, "skill_manager", None) if tc is not None else None
        if sm is not None and getattr(sm, "ai_client", None) is not None:
            return True
        return getattr(state, "ai_client", None) is not None
    except Exception:
        return False


def _build_decision(result: Dict[str, Any], *, allow_send: bool = False) -> Dict[str, Any]:
    """把 ``generate_persona_reply`` 的产出映射为 replybus 决策响应（纯函数，可单测）。

    决策口径（对齐 CONTRACT.md §5"执行权归智控王"）：

    - ``allow_send`` 为真（激活域在 ``replybus.send_domains`` 白名单）且有效回复 →
      ``action="send"``；这是 2026-09-16 为 story_matrix 域做的显式产品决策，默认关。
    - ``ok`` 真且 ``reply`` 非空 → ``action="draft"``：**刻意不直接判 send**。通读
      CONTRACT.md/reply_schema.json 全文，没有找到"承接大脑刚接线阶段应默认直发"
      的证据——相反 §0/§5 通篇强调 chengjie 对智控王是"增强项而非必需项"、执行权
      始终归智控王。人设/话术产线（``SkillManager`` 统一规则引擎）刚接上这条线，
      质量尚未经生产验证，先落"草稿"让智控王侧/人工二次确认更安全；是否要把某些
      高置信度场景升级为直发 send，应该是后续一次独立、显式的产品决策，不该隐含
      在"接线"这一步里悄悄发生。
    - 其余（``ok`` 假 / ``reply`` 为空）→ ``action="silent"``：没算出可用回复，不是
      "需要转人工"（``handoff``）——转人工通常意味着识别出某种需要人工介入的信号
      （高风险/强烈意图等），而本端点当前没有做这层判断，故不产出 handoff，安静
      即可。

    响应绝不包含 sent/delivered 等"已发送"语义字段（防双发红线，见模块 docstring）。
    """
    result = result if isinstance(result, dict) else {}
    reply = str(result.get("reply") or "").strip()
    if not (result.get("ok") and reply):
        reason = str(result.get("detail") or "").strip() or "no_reply_generated"
        return {"action": _ACTION_SILENT, "reason": reason[:120]}

    decision: Dict[str, Any] = {
        "action": _ACTION_SEND if allow_send else _ACTION_DRAFT,
        "text": reply,
    }
    persona = str(result.get("persona") or "").strip()
    if persona:
        decision["persona"] = persona
    # intent 是 SkillManager 识别出的短意图码（如 greeting/sales_inquiry），不含用户
    # 原文，拿来填 reason 正好对应契约"决策理由短码……禁止携带入站原文"的定义。
    intent = str(result.get("intent") or "").strip()
    if intent:
        decision["reason"] = intent
    return decision


def register_replybus_routes(app, *, api_auth, config_manager=None) -> None:
    """挂载 platform/replybus 决策回执端点：``POST decide`` + ``GET status``。

    注册范式与 care_routes.py / deferred_outbox_routes.py 一致；鉴权复用后台既有
    ``api_auth``（Bearer token 或 session，天然兼容机器对机器调用——只要调用方带
    ``Authorization: Bearer <token>``）。

    ✅ 2026-07-20 已修复（原"已知对接缺口"记录见下方存档，问题已不存在）：
    platform/replybus/client.py（智控王侧瘦客户端）现已支持 ``auth_token`` 构造
    参数 / ``BOUNDLESS_BUS_TOKEN`` 环境变量，配置后会在每次 decide()/status() 请求
    上带 ``Authorization: Bearer <token>`` 头。已用真实 HTTP（非 TestClient 内存
    调用）端到端验证：起一个真绑端口的 FastAPI 实例（本文件 register_replybus_
    routes + 校验 Bearer 的 api_auth），配置 ``BOUNDLESS_BUS_TOKEN`` 后，智控王侧
    ``PrivateMessageHandler._shadow_log_task`` 的真实调用能正确带上头并通过鉴权；
    未配置/配错 token 时按预期收到 401 → 客户端 fail-soft 收敛为 fallback，不 500、
    不抛异常。两侧配置需保持一致（同一个 token 字符串），部署时留意。

    <details>原缺口记录存档（问题描述已过期，仅保留历史上下文）：曾经
    platform/replybus/client.py 不发送任何 Authorization 头，若部署配置了
    ``web_admin.auth_token``，会导致 decide() 每次都 401 后静默 fallback，总线形同
    虚设——该问题已通过上述修复解决。</details>
    """

    @app.post("/api/replybus/decide")
    async def api_replybus_decide(request: Request, _=Depends(api_auth)):
        """执行层（智控王）问『这条入站私信怎么回』；本端点只回决策，绝不代发。

        body：``{"message": {platform, account?, external_id, text, msg_id?,
        session_id?, context_hint?}}``（见 reply_schema.json）。
        返回：``{"action": "draft"|"silent", "text"?, "persona"?, "reason"?}``
        （不含 available——由瘦客户端 setdefault 填入；不含任何 sent/delivered 语义）。

        任何解析/生成异常都兜底为 silent，绝不让异常穿到 FastAPI 层变成 500：对话
        热路径上"没答案"远比"服务端出错"对调用方更友好——client.py 两种情形都会
        fail-soft，但 500 会污染服务端错误日志/告警，silent 不会。
        """
        try:
            body = await request.json()
        except Exception:
            return {"action": _ACTION_SILENT, "reason": "bad_request_body"}

        message = body.get("message") if isinstance(body, dict) else None
        if not isinstance(message, dict):
            return {"action": _ACTION_SILENT, "reason": "missing_message_envelope"}

        platform = str(message.get("platform") or "").strip()
        external_id = str(message.get("external_id") or "").strip()
        # message.text 只作本次请求的临时处理载荷（CONTRACT.md §6 隐私红线）：下面
        # 这行是本文件唯一读取它的地方，读完只转手传给 generate_persona_reply，
        # 全程不打印、不写日志、不额外落盘。
        last_inbound = str(message.get("text") or "").strip()
        if not platform or not external_id or not last_inbound:
            return {"action": _ACTION_SILENT, "reason": "invalid_envelope"}

        context_hint = message.get("context_hint")
        context_hint = context_hint if isinstance(context_hint, dict) else {}
        persona_id = str(context_hint.get("persona") or "").strip()
        # session_id → conversation_id：契约定义 session_id 就是"串联同一对话多轮
        # 决策上下文"的线索 id；generate_persona_reply 恰好有同语义的 conversation_id
        # 形参（供深度人设 store 层解锁关系画像/回指）。留空时两边行为不变，顺手传
        # 入不改变任何既有默认行为，故未在字段映射任务里单列也一并接上。
        session_id = str(message.get("session_id") or "").strip()

        # 不传 history：reply_schema.json 定义的信封里没有历史消息列表这个字段
        # （只有单条入站 text），故按任务要求传空列表，交由 generate_persona_reply
        # 自身的"无历史"分支处理（该分支只影响 KB/记忆窗口拼装，不影响是否报错）。
        try:
            result = await generate_persona_reply(
                app=request.app,
                platform=platform,
                chat_key=external_id,
                last_inbound=last_inbound,
                history=[],
                persona_id=persona_id,
                conversation_id=session_id,
            )
        except Exception:
            logger.warning("[replybus] decide 生成异常，兜底 silent", exc_info=True)
            return {"action": _ACTION_SILENT, "reason": "generate_failed"}

        decision = _build_decision(result, allow_send=_send_allowed(config_manager))
        logger.debug(
            "[replybus] decide platform=%s persona=%s action=%s",
            platform, persona_id or "-", decision.get("action"),
        )
        return decision

    @app.get("/api/replybus/status")
    async def api_replybus_status(request: Request, _=Depends(api_auth)):
        """决策口可达性探测：``{"ready": bool}``（``available`` 由瘦客户端 setdefault 填入）。"""
        return {"ready": _ai_ready(request.app)}


__all__ = ["register_replybus_routes"]
