"""WebhookNotifier — L2 Webhook 通知（面向海外：Telegram / WhatsApp / Messenger）。

架构思路（优于"在 SLAWatcher 内直接 HTTP"方案）：
  - 订阅全局 EventBus，与 SLAWatcher / DraftService 完全解耦
  - 任何发布到 EventBus 的事件都可被拦截，无需改动上游代码
  - asyncio 原生 HTTP（在 executor 中），不阻塞事件循环
  - 内置速率限制：同一 (event_type, key) 每小时最多通知一次
  - 运行时可 reload()，配合 config/notify_webhooks.json 覆盖层免重启增删

支持事件：见 ``_EVENT_ALIASES``（含 autoreply_alert 自动回复熔断/配额告警）

支持格式（推荐海外渠道在前）：
  telegram  — Telegram Bot sendMessage（token + chat_id，海外首选，免企业认证）
  whatsapp  — WhatsApp Cloud API（Graph messages 端点 + Bearer token + 收件号）
  messenger — Messenger Send API（Graph me/messages + page access_token + PSID）
  json      — 原始 JSON body（万能：Zapier / n8n / 自建服务）
  dingtalk / feishu / wecom — 国内企业 IM（保留兼容，海外部署不用）

配置（config.yaml::notify.webhooks，或后台「告警渠道」面板 → notify_webhooks.json）：
  webhooks:
    - name: "tg-ops"
      format: "telegram"
      token: "123456:ABC..."        # Bot token（或直接给完整 url）
      target: "-1001234567890"       # chat_id（群/频道/个人）
      events: ["autoreply_alert", "sla_breach"]
    - name: "wa-ops"
      format: "whatsapp"
      url: "https://graph.facebook.com/v19.0/<PHONE_ID>/messages"
      token: "EAAB..."               # 永久/临时 access token（Bearer）
      target: "8613800000000"        # 收件人号码（含国家码，无 +）
      events: ["autoreply_alert"]
    - name: "msgr-ops"
      format: "messenger"
      token: "<PAGE_ACCESS_TOKEN>"   # 主页 access token（或完整 url）
      target: "<PSID>"               # 收件人 page-scoped id
      events: ["autoreply_alert"]
    - name: "my-server"
      url: "https://myserver.com/hook"
      format: "json"
      events: ["all"]                # "all" 匹配所有事件
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

# ─── 事件别名 → 实际 EventBus event_type + 条件 ───────────────────────────

_EVENT_ALIASES: Dict[str, Dict[str, Any]] = {
    "all": {"types": None},                               # 所有事件
    "L2_created": {"types": {"draft_created"}, "levels": {"L2"}},
    "L3_created": {"types": {"draft_created"}, "levels": {"L3"}},
    "L4_created": {"types": {"draft_created"}, "levels": {"L4"}},
    "draft_created": {"types": {"draft_created"}, "levels": None},
    "sla_breach": {"types": {"draft_sla_breach"}, "levels": None},
    # 二级升级（越过 escalate_hours）：比 breach 更高优先级，含 K2 永不处理的无主草稿
    # ——严重积压该主动外发（不等主管登录看铃铛），这是「响了有人听」的最后一公里。
    "sla_escalated": {"types": {"draft_sla_escalated"}, "levels": None},
    "backlog_summary": {"types": {"draft_backlog_summary"}, "levels": None},  # 积压聚合汇总
    "reassigned": {"types": {"draft_reassigned"}, "levels": None},
    "escalation": {"types": {"escalation"}, "levels": None},
    "reply_risk": {"types": {"human_reply_risk"}, "levels": None},  # M3
    "report": {"types": {"report"}, "levels": None},                # M2 简报推送
    "csat_alert": {"types": {"csat_alert"}, "levels": None},        # O2 智能预警
    "crm_sync":      {"types": {"draft_resolved"}, "levels": None},   # P1 外部 CRM 同步
    "anomaly":       {"types": {"anomaly_alert"}, "levels": None},   # S2 统计异常预警
    # P28：会话生命周期事件
    "new_message":   {"types": {"inbox_message"}, "levels": None},   # 新入站消息
    "conv_archived": {"types": {"conv_archived"}, "levels": None},   # 会话归档
    "conv_tagged":   {"types": {"conv_tagged"}, "levels": None},     # 标签变更
    "conv_note":     {"types": {"conv_note"}, "levels": None},       # 坐席注解（@提及）
    "queue_alert":   {"types": {"queue_alert"}, "levels": None},     # P29 队列告警
    "autoreply_alert": {"types": {"autoreply_alert"}, "levels": None},  # 协议自动回复熔断/配额
    "health_alert":  {"types": {"health_alert"}, "levels": None},    # D3 运行时健康告警
    "billing_alert": {"types": {"billing_alert"}, "levels": None},   # E3 计费异常（超席位/超额）
    "ops_report":    {"types": {"ops_report"}, "levels": None},       # H1 运营周报自动外发
    # 统一草稿引擎质量告警（记忆命中率/p95 延迟/风险分类回检）
    "draft_quality": {"types": {"draft_quality_alert"}, "levels": None},
    # AI 回复质量退化告警（草稿采纳/弃用率 + 高危量环比，基于处置结果口径）
    "ai_quality": {"types": {"ai_quality_alert"}, "levels": None},
    # 实时语音通话退化告警（主机健康/接通率/不可达，基于 RealtimeVoiceStats）
    "realtime_voice": {"types": {"realtime_voice_alert"}, "levels": None},
    # AvatarHub 语音克隆持续掉线（7852 不可达/未载入超阈值 → 克隆音色静默降级 edge）
    "avatar_voice": {"types": {"avatar_voice_alert"}, "levels": None},
    # 人工通过投递链静默断裂（坐席点了「发送」，一条都没真投递 → 客户什么也没收到）
    "human_deliver": {"types": {"human_deliver_alert"}, "levels": None},
    # 待审草稿长期无人处理（补 SLA 的 L1 盲区：L1 既不自动发也无逐条告警 → 无声烂掉）
    "draft_backlog": {"types": {"draft_backlog_alert"}, "levels": None},
    # CSRF 写请求拦截激增（前端宿主写通道断裂 / 反代漂移 / 跨站探测；2026-07-31 事故沉淀）
    "csrf_reject": {"types": {"csrf_reject_alert"}, "levels": None},
    # 原生通话主机持续不可用（MiniCPM-o 176:7860 掉线 → 打进来的电话全接不了）
    "tg_call": {"types": {"tg_call_alert"}, "levels": None},
    # 试用履约端停摆（厂商机是签发链单点：它不跑，领了试用的人永远停在「正在签发」）
    "trial_fulfiller": {"types": {"trial_fulfiller_alert"}, "levels": None},
    # 编排器受管 worker 崩溃告警（某账号 protocol/web worker 进 error 态 → 出站降级）
    "orchestrator_worker": {"types": {"orchestrator_worker_alert"}, "levels": None},
    # 记忆 key 漂移告警（裸 key 复发 → 记忆对引擎不可见）
    "memory_key_drift": {"types": {"memory_key_drift_alert"}, "levels": None},
    # 平台会话健康告警（P0-2：外部 worker 会话 needs_login/expired 主动 push → 立即外发）
    "platform_session": {"types": {"platform_session_alert"}, "levels": None},
    # 主机级关键告警镜像（云端 Key 失效/云端不可达/余额不足；host_alert 弹窗的远程副本，
    # 机主不在算力机前也能收到）
    "host_alert": {"types": {"host_alert"}, "levels": None},
    # 出站媒体承诺未兑现告警（Phase21a：AI 说发图/语音却频繁撤回 → 信任受损）
    "media_promise": {"types": {"media_promise_alert"}, "levels": None},
    # 口语化 LLM 持续连败（2026-07-15：九连败静默降级规则档，拟人化卖点静默流失）
    "colloquial_llm": {"types": {"colloquial_llm_alert"}, "levels": None},
    # 出站语音连发异常（2026-07-15 三连发事故指纹：同会话短窗多条语音=重复处理回归）
    "voice_burst": {"types": {"voice_burst_alert"}, "levels": None},
}

# ─── 告警受众分层（2026-07-31，产品化：终端客户能自己绑、看得懂）─────────────
# 事故教训：这套告警此前只有一张「手打逗号分隔别名」的订阅框——终端客户（买 AI 客服
# 系统的老板/客服主管）既不知道有哪些别名、也看不懂 csrf_reject/memory_key_drift 这类
# 技术黑话。按**受众**劈两层：
#   business  = 终端运营该收、大白话、能行动（客户在等、账号掉线、满意度跌…）；
#   technical = 开发者/技术支持向，终端不懂也不该处理（放面板「高级」折叠里）。
# label_key 是**大白话** i18n 键（cp-i18n.js，前端 T() 取词）。凡列进目录的别名都必须
# 在 _EVENT_ALIASES 内（门禁 test_alert_audience_catalog 钉住，防笔误/改名漂移）。
_BUSINESS_ALERTS: Dict[str, str] = {
    "platform_session": "cp.alert.platform_session",   # 账号掉线，需要重新登录
    "escalation":       "cp.alert.escalation",         # 客户等太久没人接
    "sla_breach":       "cp.alert.sla_breach",         # 回复超时
    "sla_escalated":    "cp.alert.sla_escalated",       # 严重超时升级
    "draft_backlog":    "cp.alert.draft_backlog",       # 待回复积压，客户在等
    "queue_alert":      "cp.alert.queue_alert",         # 会话排队超时
    "csat_alert":       "cp.alert.csat_alert",          # 客户满意度跌破
    "reply_risk":       "cp.alert.reply_risk",          # 高危回复预警
    "billing_alert":    "cp.alert.billing_alert",       # 席位/用量异常
}
_TECHNICAL_ALERTS: Dict[str, str] = {
    "csrf_reject":         "cp.alert.csrf_reject",
    "host_alert":          "cp.alert.host_alert",
    "health_alert":        "cp.alert.health_alert",
    "autoreply_alert":     "cp.alert.autoreply_alert",
    "draft_quality":       "cp.alert.draft_quality",
    "ai_quality":          "cp.alert.ai_quality",
    "realtime_voice":      "cp.alert.realtime_voice",
    "avatar_voice":        "cp.alert.avatar_voice",
    "human_deliver":       "cp.alert.human_deliver",
    "tg_call":             "cp.alert.tg_call",
    "orchestrator_worker": "cp.alert.orchestrator_worker",
    "memory_key_drift":    "cp.alert.memory_key_drift",
    "media_promise":       "cp.alert.media_promise",
    "colloquial_llm":      "cp.alert.colloquial_llm",
    "voice_burst":         "cp.alert.voice_burst",
    "anomaly":             "cp.alert.anomaly",
    "trial_fulfiller":     "cp.alert.trial_fulfiller",
}


def alert_catalog() -> Dict[str, List[Dict[str, str]]]:
    """按受众分组的告警目录（供「告警渠道」面板渲染大白话订阅清单）。

    返回 ``{"business":[{alias,label_key}], "technical":[...]}``。面板据此把订阅项
    分两组：业务默认展开、每条用大白话；技术折叠进「高级」。纯数据、无副作用。
    """
    return {
        "business": [{"alias": a, "label_key": k} for a, k in _BUSINESS_ALERTS.items()],
        "technical": [{"alias": a, "label_key": k} for a, k in _TECHNICAL_ALERTS.items()],
    }


def alert_audience(alias: str) -> str:
    """单个别名的受众（business/technical/other）。other=数据流/集成用，不进面板。"""
    if alias in _BUSINESS_ALERTS:
        return "business"
    if alias in _TECHNICAL_ALERTS:
        return "technical"
    return "other"


# ─── 速率限制 ────────────────────────────────────────────────────────────────

_RATE_WINDOW_SEC: float = 3600.0   # 同一 key 每小时最多一次


class _RateLimiter:
    def __init__(self, window_sec: float = _RATE_WINDOW_SEC) -> None:
        self._window = window_sec
        self._seen: Dict[str, float] = {}   # key → last_sent_ts

    def allow(self, key: str) -> bool:
        now = time.time()
        last = self._seen.get(key, 0.0)
        if now - last < self._window:
            return False
        self._seen[key] = now
        return True

    def cleanup(self) -> None:
        now = time.time()
        self._seen = {k: v for k, v in self._seen.items() if now - v < self._window * 2}


# ─── 格式化器 ────────────────────────────────────────────────────────────────

def _fmt_json(title: str, text: str, data: Dict[str, Any]) -> bytes:
    return json.dumps({"title": title, "text": text, "data": data}, ensure_ascii=False).encode()


def _fmt_dingtalk(title: str, text: str, data: Dict[str, Any]) -> bytes:
    payload = {
        "msgtype": "markdown",
        "markdown": {
            "title": title,
            "text": f"### {title}\n\n{text}",
        },
    }
    return json.dumps(payload, ensure_ascii=False).encode()


def _fmt_feishu(title: str, text: str, data: Dict[str, Any]) -> bytes:
    # 飞书自定义机器人 text 类型**不渲染 Markdown** → 必须先 _plainify，否则
    # 正文的 **粗体** / [文字](/admin/ops) 会原样显示成字面符号 + 相对链接死链
    # （2026-07-31 修：飞书是用户首选的「贴 URL 即用」渠道，此前直接塞原始 md）。
    payload = {
        "msg_type": "text",
        "content": {"text": f"{_plainify(title)}\n{_plainify(text)}".strip()},
    }
    return json.dumps(payload, ensure_ascii=False).encode()


def _fmt_wecom(title: str, text: str, data: Dict[str, Any]) -> bytes:
    payload = {
        "msgtype": "markdown",
        "markdown": {"content": f"**{title}**\n{text}"},
    }
    return json.dumps(payload, ensure_ascii=False).encode()


_FORMATTERS = {
    "json": _fmt_json,
    "dingtalk": _fmt_dingtalk,
    "feishu": _fmt_feishu,
    "wecom": _fmt_wecom,
}

# ─── 海外即时通讯渠道（Telegram / WhatsApp / Messenger）────────────────────────

CHAT_FORMATS = {"telegram", "whatsapp", "messenger"}

_MD_LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_MD_BOLD = re.compile(r"\*\*([^*]+)\*\*")


def _plainify(text: str, base_url: str = "") -> str:
    """把内部 Markdown（**粗体** / [文字](链接) / ### 标题）转成 IM 纯文本。
    相对链接（/workspace/...）默认只保留文字；给了 base_url 则拼成绝对地址。"""
    def _link(m: "re.Match") -> str:
        label, href = m.group(1), m.group(2)
        if href.startswith("http"):
            return f"{label}: {href}"
        if base_url and href.startswith("/"):
            return f"{label}: {base_url.rstrip('/')}{href}"
        return label
    s = _MD_LINK.sub(_link, text or "")
    s = _MD_BOLD.sub(r"\1", s)
    s = s.replace("### ", "")
    return s.strip()


def _resolve_chat_endpoint(fmt: str, url: str, token: str) -> str:
    """渠道端点解析：给了完整 url 直接用；否则按 token 推断标准端点。
    whatsapp 需要 phone-id，无法仅凭 token 推断，必须显式给 url。"""
    url = (url or "").strip()
    if url:
        return url
    token = (token or "").strip()
    if fmt == "telegram" and token:
        return f"https://api.telegram.org/bot{token}/sendMessage"
    if fmt == "messenger" and token:
        return ("https://graph.facebook.com/v19.0/me/messages"
                f"?access_token={urllib.parse.quote(token)}")
    return ""


def _build_chat_body(
    fmt: str, msg: str, target: str, token: str,
) -> Tuple[bytes, Dict[str, str]]:
    """构造海外 IM 渠道的请求体与请求头（whatsapp 走 Bearer 鉴权）。"""
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if fmt == "telegram":
        payload: Dict[str, Any] = {
            "chat_id": target, "text": msg,
            "disable_web_page_preview": True,
        }
    elif fmt == "whatsapp":
        if token:
            headers["Authorization"] = f"Bearer {token}"
        payload = {
            "messaging_product": "whatsapp", "to": target,
            "type": "text", "text": {"body": msg, "preview_url": False},
        }
    elif fmt == "messenger":
        payload = {
            "recipient": {"id": target},
            "messaging_type": "UPDATE",
            "message": {"text": msg},
        }
    else:
        payload = {"text": msg}
    return json.dumps(payload, ensure_ascii=False).encode(), headers


# ─── 钉钉签名 ────────────────────────────────────────────────────────────────

def _dingtalk_sign(secret: str) -> tuple:
    ts = str(round(time.time() * 1000))
    string_to_sign = f"{ts}\n{secret}"
    hmac_code = hmac.new(
        secret.encode("utf-8"), string_to_sign.encode("utf-8"), digestmod=hashlib.sha256
    ).digest()
    sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
    return ts, sign


# ─── 消息构建 ────────────────────────────────────────────────────────────────

def _build_message(event_type: str, data: Dict[str, Any]) -> tuple[str, str]:
    """根据 event_type + data 构造 (title, markdown_text)。"""
    if event_type == "draft_created":
        lv = data.get("autopilot_level", "?")
        plat = data.get("platform", "?")
        peer = str(data.get("peer_text") or data.get("peer_text_preview") or "")[:60]
        title = f"{'🚨' if lv=='L4' else '⚠️'} 新草稿 [{lv}]"
        text = (
            f"**平台**: {plat}\n"
            f"**风险等级**: {data.get('risk_level', '?')}\n"
            f"**客户消息**: {peer or '（无）'}\n"
            "[📋 前往审批](/workspace/drafts)"
        )

    elif event_type == "draft_sla_breach":
        lv = data.get("autopilot_level", "?")
        wait = data.get("wait_min", "?")
        peer = str(data.get("peer_text_preview") or "")[:60]
        title = f"⏰ 草稿 SLA 超时 [{lv}]"
        text = (
            f"**已等待**: {wait} 分钟（SLA={data.get('sla_hours','?')}h）\n"
            f"**客户消息**: {peer or '（无）'}\n"
            f"**平台**: {data.get('platform', '?')}\n"
            "[📋 前往审批](/workspace/drafts)"
        )

    elif event_type == "draft_sla_escalated":
        lv = data.get("autopilot_level", "?")
        wait = data.get("wait_min", "?")
        peer = str(data.get("peer_text_preview") or "")[:60]
        _owner = "🅾️ 无人认领" if data.get("unclaimed") else "已认领"
        title = f"🆘 草稿严重超时[{lv}]（{_owner}）"
        text = (
            f"**已等待**: {wait} 分钟（升级线={data.get('escalate_hours','?')}h）\n"
            f"**认领状态**: {_owner}\n"
            f"**客户消息**: {peer or '（无）'}\n"
            f"**平台**: {data.get('platform', '?')}\n"
            "[📋 立即处理](/workspace/drafts)"
        )

    elif event_type == "draft_backlog_summary":
        cnt = data.get("count", "?")
        title = f"📥 草稿积压汇总：{cnt} 条待处理"
        text = (
            f"**待处理**: {cnt} 条（L3={data.get('l3', '?')} · L4={data.get('l4', '?')}）\n"
            f"**SLA**: {data.get('sla_hours', '?')}h\n"
            "[📋 前往审批](/workspace/drafts)"
        )

    elif event_type == "draft_reassigned":
        title = "🔄 草稿已自动再分配"
        text = (
            f"**来自坐席**: {data.get('from_agent', '?')}\n"
            f"**转给主管**: {data.get('to_agent_name') or data.get('to_agent', '?')}\n"
            f"**原因**: {data.get('reason', 'agent_offline')}\n"
            "[📋 前往审批](/workspace/drafts)"
        )

    elif event_type == "human_reply_risk":
        title = f"⚠️ 坐席回复质量警告 [{data.get('risk_level', '?')}]"
        text = (
            f"**坐席**: {data.get('agent_id', '?')}\n"
            f"**风险等级**: {data.get('risk_level', '?')}\n"
            f"**原因**: {', '.join(data.get('risk_reasons') or []) or '—'}\n"
            f"**回复预览**: {data.get('text_preview', '')[:60]}\n"
            "[📋 查看审计](/workspace/agent-perf)"
        )

    elif event_type == "report":
        period = data.get("period", "daily")
        title = f"📊 {'今日' if period == 'daily' else '本周'}工作简报"
        text = str(data.get("text") or "")

    elif event_type == "anomaly_alert":
        # S2: 统计异常预警
        anomalies = data.get("anomalies") or []
        count = data.get("anomaly_count", 0)
        title = f"🔔 统计异常预警：{count} 项指标偏离基线"
        lines = []
        for a in anomalies:
            direction_icon = "📉" if a.get("direction") == "down" else "📈"
            lines.append(
                f"{direction_icon} **{a.get('label', a.get('metric'))}**: "
                f"当前 {a.get('current_fmt', '?')} vs 基线 {a.get('baseline_fmt', '?')} "
                f"（偏离 {a.get('deviation_score', 0):.1f}σ）"
            )
        text = "\n".join(lines) if lines else "异常详情见系统日志"

    elif event_type == "draft_resolved":
        # P1: CRM 同步用简洁摘要格式
        intent_s = data.get("intent") or "—"
        emotion_s = data.get("emotion") or "—"
        csat_s = f"{data['csat']:.1f}⭐" if data.get("csat") is not None else "待评"
        title = f"✅ 对话已处置 [{data.get('platform', '?')}] intent={intent_s}"
        text = (
            f"**草稿**: {data.get('draft_id', '?')}\n"
            f"**会话**: {data.get('conversation_id', '?')}\n"
            f"**坐席**: {data.get('agent_id', '?')} · {data.get('action', '?')}\n"
            f"**意图**: {intent_s} · **情绪**: {emotion_s}\n"
            f"**CSAT**: {csat_s} · **风险**: {data.get('risk_level', '?')}\n"
            f"**回复预览**: {data.get('text_preview', '')}"
        )

    elif event_type == "csat_alert":
        title = f"⚠️ 服务质量预警 [{data.get('condition', '?')}]"
        text = (
            f"**预警条件**: {data.get('condition', '?')}\n"
            f"**消息**: {data.get('message', '')}\n"
            f"**阈值**: {data.get('threshold', '?')}\n"
            "[📊 查看简报](/workspace/dashboard)"
        )

    elif event_type == "autoreply_alert":
        kind = str(data.get("kind") or "")
        kind_label = {
            "circuit_open": "熔断（连续失败）",
            "quota_hour": "小时配额耗尽",
            "quota_day": "每日配额耗尽",
        }.get(kind, kind)
        title = f"🚨 自动回复告警：{kind_label}"
        text = (
            f"**平台**: {data.get('platform', '?')}\n"
            f"**账号**: {data.get('account_id', '?')}\n"
            f"**详情**: {data.get('detail', '')}\n"
            "[👥 前往账号管理](/workspace/unified-inbox)"
        )

    elif event_type == "health_alert":
        light = str(data.get("light") or "")
        icon = {"red": "🔴", "yellow": "🟡"}.get(light, "🩺")
        if data.get("recovered"):
            title = "✅ 系统已恢复健康"
            text = (
                f"**状态**: 全部组件恢复正常\n"
                "[🩺 查看健康面板](/dashboard)"
            )
        else:
            probs = data.get("problems") or []
            lines = [f"- {p.get('name','?')}：{p.get('detail','')}" for p in probs[:8]]
            title = f"{icon} 系统健康告警（{light or '异常'}）"
            text = (
                f"**异常组件**: {len(probs)} 项\n"
                + "\n".join(lines) + "\n"
                "[🩺 查看健康面板](/dashboard)"
            )

    elif event_type == "billing_alert":
        anomalies = data.get("anomalies") or []
        if data.get("recovered"):
            title = "✅ 计费异常已解除"
            text = "**状态**: 席位/用量已回到授权额度内\n[💸 查看运营总览](/admin/ops)"
        else:
            lines = [f"- {a.get('message','')}" for a in anomalies[:8]]
            title = "💸 计费异常告警"
            text = (
                f"**异常**: {len(anomalies)} 项\n"
                + "\n".join(lines) + "\n"
                "[💸 查看运营总览](/admin/ops)"
            )

    elif event_type == "ops_report":
        days = data.get("days", 7)
        title = f"📰 运营周报（近 {days} 天）"
        lines = [f"- {h}" for h in (data.get("headline") or [])]
        cmp = data.get("compare") or {}
        ic = cmp.get("incidents_delta")
        pp = cmp.get("ai_share_delta_pp")
        cmp_parts = []
        if ic is not None:
            cmp_parts.append(f"事件 {ic:+d} 起")
        if pp is not None:
            cmp_parts.append(f"AI 占比 {pp:+.1f}pp")
        if cmp_parts:
            lines.append("- 环比上周：" + "、".join(cmp_parts))
        text = ("\n".join(lines) if lines else "本周无显著运营事件") + \
            "\n[📊 查看运营总览](/admin/ops)"

    elif event_type == "draft_quality_alert":
        if data.get("recovered"):
            title = "✅ 草稿质量已恢复"
            text = (
                "**状态**: 记忆命中率 / 生成延迟 / 风险分类均回到阈值内\n"
                "[📊 查看运营总览](/admin/ops)"
            )
        else:
            probs = data.get("problems") or []
            lines = [f"- {p.get('name','?')}：{p.get('detail','')}" for p in probs[:8]]
            icon = {"red": "🔴", "yellow": "🟡"}.get(str(data.get("light") or ""), "📉")
            title = f"{icon} 草稿质量告警（{data.get('light') or '异常'}）"
            text = (
                f"**异常**: {len(probs)} 项\n"
                + "\n".join(lines) + "\n"
                "[📊 查看运营总览](/admin/ops)"
            )

    elif event_type == "ai_quality_alert":
        if data.get("recovered"):
            title = "✅ AI 质量已恢复"
            text = (
                "**状态**: 草稿采纳/弃用率与高危量均回到阈值内\n"
                "[📊 查看运营总览](/admin/ops)"
            )
        else:
            probs = data.get("problems") or []
            lines = [f"- {p.get('name','?')}：{p.get('detail','')}" for p in probs[:5]]
            icon = {"red": "🔴", "yellow": "🟡"}.get(str(data.get("light") or ""), "🛡️")
            title = f"{icon} AI 质量告警（{data.get('light') or '异常'}）"
            text = (
                f"**异常**: {len(probs)} 项\n"
                + "\n".join(lines) + "\n"
                "[📊 查看运营总览](/admin/ops)"
            )

    elif event_type == "realtime_voice_alert":
        if data.get("recovered"):
            title = "✅ 实时语音已恢复"
            text = (
                "**状态**: 主机健康/接通率均回到阈值内\n"
                "[📊 查看运营总览](/admin/ops)"
            )
        else:
            probs = data.get("problems") or []
            lines = [f"- {p.get('name','?')}：{p.get('detail','')}" for p in probs[:5]]
            icon = {"red": "🔴", "yellow": "🟡"}.get(str(data.get("light") or ""), "📞")
            title = f"{icon} 实时语音告警（{data.get('light') or '异常'}）"
            text = (
                f"**异常**: {len(probs)} 项\n"
                + "\n".join(lines) + "\n"
                "[📊 查看运营总览](/admin/ops)"
            )

    elif event_type == "trial_fulfiller_alert":
        if data.get("recovered"):
            title = "✅ 试用签发链已恢复"
            text = (
                "**状态**: 厂商机履约端已在正常取待办，积压已清\n"
                "[📊 查看试用台账](/console/trial)"
            )
        else:
            kind = str(data.get("kind") or "stale")
            pending = int(data.get("pending") or 0)
            backlog = int(data.get("backlog_min") or 0)
            beat = int(data.get("heartbeat_min") or -1)
            # 三种故障的处置动作不同，标题就直接把动作说清，别让人再去猜
            if kind == "stuck":
                title = "🚨 试用签发在跑但清不掉活"
                why = (f"履约端心跳正常（{beat} 分钟前），但待办 {pending} 条、"
                       f"最老已等 {backlog} 分钟 —— 大概率是回填失败或签不出，"
                       "看 logs\\fulfill_trial\\ 当日日志")
            elif kind == "unreachable":
                title = "🚨 取不到试用台账（签发链状态未知）"
                why = ("厂商机读不到官网台账：本机网络或官网异常，也可能 ADMIN_KEY 失效。"
                       "此期间领了试用的人都拿不到授权")
            else:
                title = "🚨 试用签发链停摆"
                if data.get("never"):
                    why = ("履约端从未来取过待办 —— 常驻任务大概没装："
                           "scripts\\fulfill_trial_service.ps1 -Install")
                else:
                    why = (f"履约端已 {beat} 分钟没来取待办（任务被删/python 路径变了/"
                           "机器关了都会这样）")
                if pending > 0:
                    why += f"；当前 {pending} 条待办，最老已等 {backlog} 分钟"
            text = (
                f"**问题**: {why}\n"
                f"**站点**: {data.get('site') or '-'}\n"
                f"**持续**: {int(data.get('down_minutes') or 0)} 分钟"
                + ("（重提）" if data.get("reminder") else "") + "\n"
                "**影响**: 用户点了「免费领取」会一直停在「正在签发」，客户端不报错\n"
                "[📊 查看试用台账](/console/trial)"
            )

    elif event_type == "avatar_voice_alert":
        if data.get("recovered"):
            title = "✅ AvatarHub 语音克隆已恢复"
            text = (
                "**状态**: 7852 情感克隆服务已就绪，克隆音色恢复\n"
                "[📊 查看运营总览](/admin/ops)"
            )
        else:
            down_min = int(data.get("down_minutes") or 0)
            down_txt = (f"{down_min // 60} 小时 {down_min % 60} 分钟"
                        if down_min >= 60 else f"{down_min} 分钟")
            hang = bool(data.get("hang"))
            if hang:
                state = (f"半死（health 正常但真实合成连败 "
                         f"{int(data.get('fail_streak') or 0)} 次）")
            else:
                state = ("服务不可达" if not data.get("reachable")
                         else "在线但模型未载入")
            prefix = "⏰" if data.get("reminder") else "🎙️"
            verb = "合成挂死" if hang else "掉线"
            title = f"{prefix} AvatarHub 语音克隆{verb}（已 {down_txt}）"
            fix = ("EmotionTTSWatchdog 自动重启未能救回，请上机检查 "
                   "logs\\emotion_tts.out.log / GPU 状态"
                   if hang else
                   "请检查 emotion_tts 服务或手动拉起计划任务 EmotionTTS_Boot")
            text = (
                f"**状态**: {state}（{str(data.get('url') or '')}）\n"
                f"**影响**: 在线语音已降级 edge 通用声——聊天不中断，"
                "但克隆音色/情感语气不可用\n"
                f"**详情**: {str(data.get('error') or '')[:150]}\n"
                f"{fix}\n"
                "[📊 查看运营总览](/admin/ops)"
            )

    elif event_type == "draft_backlog_alert":
        if data.get("recovered"):
            title = "✅ 待审草稿积压已清空"
            text = (
                "**状态**: 超龄待审草稿已全部处理\n"
                "[📊 查看运营总览](/admin/ops)"
            )
        else:
            n = int(data.get("stale_count") or 0)
            oldest = float(data.get("oldest_hours") or 0)
            min_age = float(data.get("min_age_hours") or 24)
            uncovered = int(data.get("sla_uncovered") or 0)
            lv = data.get("by_level") or {}
            lv_txt = "、".join(f"{k}×{v}" for k, v in sorted(lv.items())) or "—"
            prefix = "⏰" if data.get("reminder") else "🚨"
            oldest_txt = (f"{oldest / 24:.1f} 天" if oldest >= 48 else f"{oldest:.0f} 小时")
            orphan = int(data.get("already_replied") or 0)
            orphan_txt = (
                f"**账目残留**: 另有 {orphan} 条已由人工回过、仅草稿行没人处置"
                "（不算客户在等，清掉即可）\n" if orphan else "")
            title = f"{prefix} {n} 条待审草稿超过 {min_age:.0f}h 客户仍在等（最老 {oldest_txt}）"
            text = (
                f"**分级**: {lv_txt}\n"
                f"**盲区**: 其中 {uncovered} 条**不在** SLA 逐条告警覆盖内"
                "（SLA 只看 L3/L4，而 L1＝必须人审那一档既不自动发也不告警）\n"
                f"{orphan_txt}"
                "**影响**: 客户的消息一直没被回复；老稿子即便再点「通过」也会被陈旧"
                "护栏拦下（原样发出会与当下情境脱节）\n"
                "**处置**: 工作台逐条「改写后发送」或「拒绝」清队列；长期缺人看队列"
                "考虑开 `inbox.sla_watcher.auto_expire_hours` 自动作废\n"
                "[📊 查看运营总览](/admin/ops)"
            )

    elif event_type == "csrf_reject_alert":
        if data.get("recovered"):
            title = "✅ CSRF 写请求拦截已平息"
            text = (
                "**状态**: 观察窗内不再出现新的 CSRF 拦截\n"
                "[📊 查看运营总览](/admin/ops)"
            )
        else:
            n = int(data.get("count") or 0)
            win = int(data.get("window_min") or 60)
            kinds = data.get("by_kind") or {}
            kinds_txt = "、".join(f"{k}×{v}" for k, v in sorted(kinds.items())) or "—"
            tops = data.get("top_paths") or {}
            tops_txt = "、".join(f"{p}×{v}" for p, v in sorted(
                tops.items(), key=lambda kv: -kv[1])) or "—"
            prefix = "⏰" if data.get("reminder") else "🚨"
            title = f"{prefix} CSRF 写请求拦截激增：{win} 分钟内 {n} 次"
            text = (
                f"**形态**: {kinds_txt}\n"
                f"**接口 Top**: {tops_txt}\n"
                "**读法**: `cookie_no_header`＝某前端宿主没带凭证（写通道断裂，"
                "2026-07 人设切换事故形态）；`origin/referer_mismatch`＝反代改写 "
                "Host 或跨站请求；`bare`＝脚本/隐私浏览器\n"
                "**处置**: 功能形态→查最近前端改动是否漏带 X-CSRF-Token；"
                "安全形态→核对访问入口与反代配置\n"
                "[📊 查看运营总览](/admin/ops)"
            )

    elif event_type == "human_deliver_alert":
        if data.get("recovered"):
            title = "✅ 人工通过投递链已恢复"
            text = (
                "**状态**: 坐席通过的草稿已能真正投递给客户\n"
                "[📊 查看运营总览](/admin/ops)"
            )
        else:
            bad_min = int(data.get("bad_minutes") or 0)
            bad_txt = (f"{bad_min // 60} 小时 {bad_min % 60} 分钟"
                       if bad_min >= 60 else f"{bad_min} 分钟")
            prefix = "⏰" if data.get("reminder") else "🚨"
            n = int(data.get("human_approved") or 0)
            title = f"{prefix} 坐席「发送」的草稿一条都没发出去（已 {bad_txt}）"
            text = (
                f"**现象**: 本进程内坐席已人工通过 {n} 条草稿，"
                "但真投递计数恒 0 且零失败＝**一次都没尝试投递**\n"
                "**影响**: 坐席以为发了、客户什么也没收到（此前曾整条链缺失，"
                "实测 14 天零人工投递）\n"
                "**排查**: 确认 `inbox.l2_autosend.deliver=true`，并查启动日志有无"
                "「人工通过投递回调注入失败」；`/api/drafts/autosend-status` 看 "
                "`deliver_enabled` 与 `total_human_delivered`\n"
                "[📊 查看运营总览](/admin/ops)"
            )

    elif event_type == "tg_call_alert":
        if data.get("recovered"):
            title = "✅ 原生通话主机已恢复"
            text = (
                "**状态**: 语音主机（MiniCPM-o）已就绪，来电可正常接听\n"
                "[📊 查看运营总览](/admin/ops)"
            )
        else:
            down_min = int(data.get("down_minutes") or 0)
            down_txt = (f"{down_min // 60} 小时 {down_min % 60} 分钟"
                        if down_min >= 60 else f"{down_min} 分钟")
            prefix = "⏰" if data.get("reminder") else "📞"
            state = ("服务不可达" if not data.get("reachable")
                     else "在线但模型未载入")
            title = f"{prefix} 原生通话主机不可用（已 {down_txt}）"
            text = (
                f"**状态**: {state}（{str(data.get('url') or '')}）\n"
                "**影响**: 打进来的电话全部接不了（决策走拒接+补偿），"
                "「她会接电话」的陪护体验静默失效\n"
                f"**详情**: {str(data.get('error') or '')[:150]}\n"
                "请检查 MiniCPM-o 语音主机或「启动引擎」载入模型\n"
                "[📊 查看运营总览](/admin/ops)"
            )

    elif event_type == "media_promise_alert":
        if data.get("recovered"):
            title = "✅ 媒体承诺兑现已恢复"
            text = (
                "**状态**: 发图/语音承诺已重新正常兑现\n"
                "[📊 查看运营总览](/admin/ops)"
            )
        else:
            net = int(data.get("net_retracted") or 0)
            ret = int(data.get("promise_retracted") or 0)
            ful = int(data.get("promise_fulfilled") or 0)
            vfb = data.get("voice_fallback") or {}
            prefix = "⏰" if data.get("reminder") else "📸"
            title = f"{prefix} 出站媒体承诺频繁未兑现（净撤回 {net}）"
            vfb_line = ""
            if vfb:
                vfb_line = ("**语音同期回落**: "
                            + "、".join(f"{k}×{v}" for k, v in vfb.items()) + "\n")
            text = (
                f"**现象**: AI 文本承诺发图/语音却撤回改发台阶话术（累计撤回 {ret}、"
                f"兑现 {ful}）\n"
                f"**影响**: 用户被反复放鸽子，拟人化信任受损\n"
                f"{vfb_line}"
                "**排查**: 发图后端(ComfyUI/相册)是否可用、7852 是否掉线、GPU 显存是否被挤\n"
                "[📊 查看运营总览](/admin/ops)"
            )

    elif event_type == "colloquial_llm_alert":
        if data.get("recovered"):
            title = "✅ 口语化 LLM 已恢复"
            text = (
                "**状态**: 本地口语化改写重新成功，语音活人感 A 档恢复\n"
                "[📊 查看运营总览](/admin/ops)"
            )
        else:
            down_min = int(data.get("down_minutes") or 0)
            down_txt = (f"{down_min // 60} 小时 {down_min % 60} 分钟"
                        if down_min >= 60 else f"{down_min} 分钟")
            prefix = "⏰" if data.get("reminder") else "🗣️"
            title = f"{prefix} 口语化 LLM 持续连败（已 {down_txt}）"
            text = (
                f"**状态**: 连续失败 {int(data.get('fail_streak') or 0)} 次，"
                "熔断-重试循环中\n"
                "**影响**: 语音口语化静默降级规则档——聊天不中断，"
                "但语音「活人感」明显下降\n"
                "**排查**: 本地 LLM 端点（ai.fallback / qwen3）是否存活、"
                "端口是否可达、显存是否被挤\n"
                "[📊 查看运营总览](/admin/ops)"
            )

    elif event_type == "voice_burst_alert":
        title = (f"🔁 出站语音连发异常 chat={str(data.get('chat_id') or '?')}"
                 f"（{int(data.get('count') or 0)} 条/"
                 f"{int(data.get('window_sec') or 0)}s）")
        text = (
            "**现象**: 同一会话短时间连发多条语音——2026-07-15 三连发事故的指纹\n"
            "**可能原因**: 消息去重/会话串行回归、或回复被异常拆成多条\n"
            "**排查**: 检查 app.log 中该 chat 的「执行 direct_chat」是否同 mid 出现多次\n"
            "[📊 查看运营总览](/admin/ops)"
        )

    elif event_type == "orchestrator_worker_alert":
        if data.get("recovered"):
            title = "✅ 编排器 worker 已恢复"
            text = (
                "**状态**: 受管账号 worker 均已回到运行态\n"
                "[📊 查看运营总览](/admin/ops)"
            )
        else:
            probs = data.get("problems") or []
            lines = [
                f"- {p.get('name', '?')}：{p.get('detail', '')}"
                f"（重启 {p.get('restarts', 0)} 次）"
                for p in probs[:5]
            ]
            icon = {"red": "🔴", "yellow": "🟡"}.get(str(data.get("light") or ""), "🧰")
            title = f"{icon} 编排器 worker 告警（{data.get('light') or '异常'}）"
            text = (
                f"**掉线 worker**: {len(probs)} 个（协议/网页号收发降级，出站回落适配器）\n"
                + "\n".join(lines) + "\n"
                "[📊 查看运营总览](/admin/ops)"
            )

    elif event_type == "platform_session_alert":
        plat = str(data.get("platform") or "?")
        acct = str(data.get("account_id") or "?")
        if data.get("recovered"):
            title = f"✅ {plat} 会话已恢复"
            text = (
                f"**账号**: {acct}\n"
                "**状态**: 已重新登录/授权，收发恢复\n"
                "[📊 查看运营总览](/admin/ops)"
            )
        elif data.get("reminder"):
            st = str(data.get("status") or "异常")
            down_min = int(data.get("down_minutes") or 0)
            down_txt = (f"{down_min // 60} 小时 {down_min % 60} 分钟"
                        if down_min >= 60 else f"{down_min} 分钟")
            title = f"⏰ {plat} 会话持续掉线（已 {down_txt}）"
            text = (
                f"**账号**: {acct}\n"
                f"**状态**: {st}（掉线告警后仍未恢复）\n"
                f"**详情**: {str(data.get('detail') or '')[:200]}\n"
                "该账号仍无法自动收发，请尽快人工重新登录\n"
                "[📊 查看运营总览](/admin/ops)"
            )
        else:
            st = str(data.get("status") or "异常")
            title = f"🔌 {plat} 会话掉线（{st}）"
            text = (
                f"**账号**: {acct}\n"
                f"**状态**: {st}\n"
                f"**详情**: {str(data.get('detail') or '')[:200]}\n"
                "该账号已无法自动收发，需人工重新登录\n"
                "[📊 查看运营总览](/admin/ops)"
            )

    elif event_type == "host_alert":
        # 主机级关键告警镜像（云端 Key/不可达/余额）：标题正文由 host_alert 组好，直接转发
        title = f"🖥️ {str(data.get('title') or '主机告警')}"
        text = (
            f"{str(data.get('message') or '')[:500]}\n"
            "[📊 查看运营总览](/admin/ops)"
        )

    elif event_type == "memory_key_drift_alert":
        if data.get("recovered"):
            title = "✅ 记忆 key 漂移已恢复"
            text = (
                "**状态**: 裸 key 已清零（记忆重新对收件箱引擎可见）\n"
                "[📊 查看运营总览](/admin/ops)"
            )
        else:
            probs = data.get("problems") or []
            lines = [f"- {p.get('name','?')}：{p.get('detail','')}" for p in probs[:5]]
            icon = {"red": "🔴", "yellow": "🟡"}.get(str(data.get("light") or ""), "🧭")
            title = f"{icon} 记忆 key 漂移告警（{data.get('light') or '异常'}）"
            text = (
                f"**异常**: {len(probs)} 项\n"
                + "\n".join(lines) + "\n"
                "[📊 查看运营总览](/admin/ops)"
            )

    elif event_type == "escalation":
        title = "🔔 升级告警"
        text = (
            f"**客户**: {data.get('name', '?')}\n"
            f"**等待**: {int((data.get('wait_sec') or 0)//60)} 分钟\n"
            f"**原因**: {data.get('reason', '?')}"
        )

    elif event_type == "queue_alert":
        # P29/P32：队列等待超时 → 坐席告警（warn/crit）。
        lvl = str(data.get("sla_level") or "")
        icon = "🔴" if lvl == "crit" else "🟠"
        who = data.get("to_agent_id") or "全体在线坐席"
        title = f"{icon} 队列等待超时（{lvl or '警告'}）"
        text = (
            f"**客户**: {data.get('display_name', '?')}\n"
            f"**平台**: {data.get('platform', '?')}\n"
            f"**已等待**: {data.get('wait_min', '?')} 分钟\n"
            f"**指派**: {who}\n"
            "[📋 前往工作台](/workspace/unified-inbox)"
        )

    else:
        title = f"[{event_type}] 事件"
        text = json.dumps(data, ensure_ascii=False)[:300]

    return title, text


# ─── 主类 ────────────────────────────────────────────────────────────────────

class WebhookNotifier:
    """L2：订阅 EventBus，对匹配事件发送企业 IM Webhook 通知。

    Usage::

        notifier = WebhookNotifier(config=cfg_dict)
        asyncio.ensure_future(notifier.run())
        notifier.stop()
    """

    def __init__(self, config: Optional[List[Dict[str, Any]]] = None) -> None:
        self._webhooks: List[Dict[str, Any]] = list(config or [])
        self._rate_limiter = _RateLimiter()
        self._stop_evt = asyncio.Event()
        self._running = False
        self.total_sent: int = 0
        self.total_errors: int = 0

        # 预处理：每条 webhook 配置展开事件匹配规则
        self._matchers: List[Dict[str, Any]] = []
        self._build_matchers()

    def _build_matchers(self) -> None:
        """（重）构建事件匹配规则；供 __init__ 与 reload() 复用。"""
        matchers: List[Dict[str, Any]] = []
        for wh in self._webhooks:
            if wh.get("enabled") is False:
                continue
            events = list(wh.get("events") or ["all"])
            for alias in events:
                rule = _EVENT_ALIASES.get(alias)
                if rule is None:
                    logger.warning("未知 webhook 事件别名: %s", alias)
                    continue
                matchers.append({
                    "url": str(wh.get("url") or ""),
                    "fmt": str(wh.get("format") or "json").lower(),
                    "secret": str(wh.get("secret") or ""),
                    "token": str(wh.get("token") or ""),
                    "target": str(wh.get("target") or wh.get("chat_id") or ""),
                    "name": str(wh.get("name") or "webhook"),
                    "types": rule["types"],          # None → 全部
                    "levels": rule.get("levels"),    # None → 全部
                })
        self._matchers = matchers

    def reload(self, config: Optional[List[Dict[str, Any]]] = None) -> None:
        """运行时热更 webhook 列表（配合后台「告警渠道」面板免重启增删）。"""
        self._webhooks = list(config or [])
        self._build_matchers()
        logger.info("WebhookNotifier 已热更（%d 个 webhook / %d 匹配规则）",
                    len(self._webhooks), len(self._matchers))

    # ── 生命周期 ──────────────────────────────────────────────────────────

    async def run(self) -> None:
        from src.integrations.shared.event_bus import get_event_bus
        self._running = True
        self._stop_evt.clear()
        bus = get_event_bus()
        queue = bus.subscribe()
        logger.info("WebhookNotifier 已启动（%d 个 webhook 端点）", len(self._webhooks))
        try:
            while not self._stop_evt.is_set():
                try:
                    evt = await asyncio.wait_for(queue.get(), timeout=2.0)
                    await self._dispatch(evt)
                except asyncio.TimeoutError:
                    pass  # 正常超时，继续检查 stop_evt
                except Exception:
                    logger.debug("WebhookNotifier event 处理异常（已忽略）", exc_info=True)
        finally:
            bus.unsubscribe(queue)
            self._running = False
            logger.info("WebhookNotifier 已停止")

    def stop(self) -> None:
        self._stop_evt.set()

    # ── 事件分发 ─────────────────────────────────────────────────────────

    async def _dispatch(self, evt: Dict[str, Any]) -> None:
        etype = str(evt.get("type") or "")
        data = dict(evt.get("data") or {})
        level = str(data.get("autopilot_level") or "")

        for m in self._matchers:
            # 匹配 event type
            if m["types"] is not None and etype not in m["types"]:
                continue
            # 匹配 autopilot_level
            if m["levels"] is not None and level not in m["levels"]:
                continue
            # 速率限制 key = (name, event_type, 判别符)。判别符默认 draft_id；无 draft
            # 语义的事件可显式带 rate_key（如 platform_session_alert 按 platform:account
            # 区分——否则多账号同小时掉线只有第一条能发出）。
            rate_key = (f"{m['name']}:{etype}:"
                        f"{data.get('draft_id') or data.get('rate_key') or ''}")
            if not self._rate_limiter.allow(rate_key):
                logger.debug("Webhook 速率限制跳过: %s", rate_key)
                continue

            await self._send(m, etype, data)

        # 定期清理速率限制记录
        self._rate_limiter.cleanup()

    async def _send(self, matcher: Dict[str, Any], etype: str, data: Dict[str, Any]) -> None:
        fmt = matcher["fmt"]
        secret = matcher["secret"]
        token = matcher.get("token") or ""
        target = matcher.get("target") or ""
        title, text = _build_message(etype, data)

        if fmt in CHAT_FORMATS:
            url = _resolve_chat_endpoint(fmt, matcher["url"], token)
            if not url:
                self.total_errors += 1
                logger.warning("Webhook 跳过 [%s]：%s 渠道缺少 url/token",
                               matcher["name"], fmt)
                return
            if not target:
                self.total_errors += 1
                logger.warning("Webhook 跳过 [%s]：%s 渠道缺少 target",
                               matcher["name"], fmt)
                return
            # title 也过 _plainify（防标题偶含 markdown；海外 IM 均为纯文本渠道）
            msg = f"{_plainify(title)}\n{_plainify(text)}".strip()
            body, headers = _build_chat_body(fmt, msg, target, token)
        else:
            url = matcher["url"]
            if not url:
                return
            formatter = _FORMATTERS.get(fmt, _fmt_json)
            body = formatter(title, text, data)
            headers = {"Content-Type": "application/json; charset=utf-8"}
            # 钉钉签名（可选，保留兼容）
            if fmt == "dingtalk" and secret:
                ts, sign = _dingtalk_sign(secret)
                sep = "&" if "?" in url else "?"
                url = f"{url}{sep}timestamp={ts}&sign={sign}"

        try:
            await asyncio.get_event_loop().run_in_executor(
                None, self._http_post, url, body, headers
            )
            self.total_sent += 1
            logger.info("Webhook 发送成功 [%s] %s", matcher["name"], etype)
        except Exception as exc:
            self.total_errors += 1
            logger.warning("Webhook 发送失败 [%s]: %s", matcher["name"], exc)

    @staticmethod
    def _http_post(url: str, body: bytes, headers: Optional[Dict[str, str]] = None) -> None:
        """同步 HTTP POST（在 executor 中运行，不阻塞事件循环）。"""
        req = urllib.request.Request(
            url,
            data=body,
            headers=headers or {"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()

    async def send_test(self, webhook: Dict[str, Any], etype: str = "autoreply_alert",
                        data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """对单条 webhook 配置即时发一条测试消息（不经速率限制），返回结果。"""
        rule = _EVENT_ALIASES.get((webhook.get("events") or ["all"])[0]) \
            or _EVENT_ALIASES["all"]
        m = {
            "url": str(webhook.get("url") or ""),
            "fmt": str(webhook.get("format") or "json").lower(),
            "secret": str(webhook.get("secret") or ""),
            "token": str(webhook.get("token") or ""),
            "target": str(webhook.get("target") or webhook.get("chat_id") or ""),
            "name": str(webhook.get("name") or "test"),
            "types": rule["types"], "levels": rule.get("levels"),
        }
        payload = data or {
            "kind": "circuit_open", "platform": "telegram",
            "account_id": "test-account", "detail": "这是一条测试告警（连通性检查）",
        }
        before_err = self.total_errors
        await self._send(m, etype, payload)
        ok = self.total_errors == before_err
        return {"ok": ok, "name": m["name"], "format": m["fmt"]}

    # ── 状态快照 ──────────────────────────────────────────────────────────

    def status_snapshot(self) -> Dict[str, Any]:
        return {
            "running": self._running,
            "webhooks": len(self._webhooks),
            "matchers": len(self._matchers),
            "total_sent": self.total_sent,
            "total_errors": self.total_errors,
        }
