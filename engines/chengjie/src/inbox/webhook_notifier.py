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
    # #142：真发总闸自动过期恢复（pause_auto_resume_hours 到点自动开闸的群外通知）
    "autosend_gate": {"types": {"autosend_gate_alert"}, "levels": None},
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
    # 出图模型失踪（2026-08-22 事故：176 ComfyUI 模型目录被整树清空，生图链
    # 全灭数小时无人知——「曾部署过→清单里消失」才告警，新装机零误报）
    "image_models": {"types": {"image_models_alert"}, "levels": None},
    # hub 钉死的 TTS 引擎在目录里被标不可用（2026-08-22 事故：index_tts 掉登记 →
    # hub 静默换 fish 合成 = 客户听到另一个人的声音；引擎进程自身还是健康的）
    "hub_engine": {"types": {"hub_engine_alert"}, "levels": None},
    # LAN GPU 主机整机下线（嵌入/视觉/兜底 LLM/本地 MT 静默转移备点 → 冗余归零无人知，
    # 2026-08-01 176 两小时静默宕机实锤）
    "lan_gpu": {"types": {"lan_gpu_alert"}, "levels": None},
    # 人工通过投递链静默断裂（坐席点了「发送」，一条都没真投递 → 客户什么也没收到）
    "human_deliver": {"types": {"human_deliver_alert"}, "levels": None},
    # 报障群 AI 值守（2026-08-18）：新 P0/P1 工单 / 危机词压制转人工
    "bug_intake": {"types": {"bug_intake_alert"}, "levels": None},
    # AI 助手悬浮球报障（2026-08-19）：web 端坐席/运营经助手面板提交的工单
    "assistant_report": {"types": {"assistant_report_alert"}, "levels": None},
    # 待审草稿长期无人处理（补 SLA 的 L1 盲区：L1 既不自动发也无逐条告警 → 无声烂掉）
    "draft_backlog": {"types": {"draft_backlog_alert"}, "levels": None},
    # 账号真相真幽灵（P4b 2026-08-17）：会话库有、注册表没有（剔除 web / 桌面镜像）
    "accounts_truth": {"types": {"accounts_truth_alert"}, "levels": None},
    # 被埋会话（P0-198 2026-08-04）：会话归档着却有未读入站＝客户在等，而工作台所有
    # 默认视图都过滤 archived=1 → 没人看得见、也没有任何信号会响
    "buried_conv": {"types": {"buried_conv_alert"}, "levels": None},
    # 对方机器人守卫（P0 2026-08-03：主动触达问候 @SpamBot → 80 秒 8 轮 LLM 空转实锤）
    # ——检出 bot/复读/秒回熔断/预算封顶时通知（每会话每日至多一次）
    "bot_peer": {"types": {"bot_peer_alert"}, "levels": None},
    # 回复额度触顶聚合（P1 2026-08-12）：多个会话同日烧穿每日预算＝系统性状况
    # （额度配小/bot 波次），与逐会话 bot_peer 互补；含 near(≥80%) 提前量
    "reply_budget": {"types": {"reply_budget_alert"}, "levels": None},
    # 坐席字符额度水位聚合（2026-08-16）：达 quota_alert_pct 提醒线/超额的坐席
    # 聚合外发——enforce 硬闸默认关（软提醒先行），这是软提醒期运营对「谁快
    # 用满/谁已超额」的唯一主动信号；开硬闸后=拦截前的排额度依据
    "agent_quota": {"types": {"agent_quota_alert"}, "levels": None},
    # AI 对聊提醒（P0-6 2026-08-09：受管账号互聊是**允许的测试手段**，只标记+
    # 提醒、绝不拦截——风险只在「没人知道它在跑」，双边烧配额应发生在知情前提下）
    "ai_mutual_chat": {"types": {"ai_mutual_chat_alert"}, "levels": None},
    # 案例中心（2026-08-03 案例跟进页改造——「AI 判定需要人跟进」从看板变成推送）：
    # case_alert=severity≥2 开案/升级；case_backlog_alert=立案后无人认领/处理的
    # 积压巡检（危机级单独点名）。同一别名一次订阅两类都收。
    "cases": {"types": {"case_alert", "case_backlog_alert"}, "levels": None},
    # 实施93c：客户点了 CTA 追踪短链（引导模型的最热跟进信号——点击后几分钟
    # 内跟进 vs 隔天跟进是两种成交率；首点才发布，天然稀疏不刷屏）
    "cta_click": {"types": {"cta_clicked"}, "levels": None},
    # 账号接入链路停摆（某平台某方式发起 N 次成功 0 次；2026-07-25 LINE 扫码 100% 失败
    # 烂了多日无人知——ops 卡能看见但要有人开，这条把它变成推送）
    "login_funnel": {"types": {"login_funnel_alert"}, "levels": None},
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
    # 撤销设定复活（2026-08-04：运营删掉的人设设定被丰富管线/直改文件写回档案）
    "persona_retired": {"types": {"persona_retired_alert"}, "levels": None},
    # 口语化 LLM 持续连败（2026-07-15：九连败静默降级规则档，拟人化卖点静默流失）
    "colloquial_llm": {"types": {"colloquial_llm_alert"}, "levels": None},
    # 本地主链保险（2026-08-15）：ai.primary=local* 而 vLLM 连续探测失败 →
    # 单向热切 cloud（服务不断、隐私临时让位）；恢复通知提示可切回
    "ai_primary_guard": {"types": {"ai_primary_guard_alert"}, "levels": None},
    # 出站语音连发异常（2026-07-15 三连发事故指纹：同会话短窗多条语音=重复处理回归）
    "voice_burst": {"types": {"voice_burst_alert"}, "levels": None},
    # 语音出站断档（2026-08-02 实锤：hub 音色档 404 → strict 全拒发 5 天零告警——
    # 探针全绿也可能整链拒发；低流量下按滚动窗「有尝试全失败」判）
    "voice_outage": {"types": {"voice_outage_alert"}, "levels": None},
    # 入站漏球（P0 2026-08-05：客户最后一句既没被回也没拟稿——draft_backlog 只看
    # 「有稿没人处理」，「压根没稿」的丢球此前零信号；22:27 高意向消息整晚无人接实锤）
    "unanswered_inbound": {"types": {"unanswered_inbound_alert"}, "levels": None},
    # 审计高危操作（2026-08-05：批量删记忆/删模板等破坏性后台动作；store 写成功后
    # 60s 同 actor×action 节流推 EventBus——通道未接通时仍进铃铛/日志）
    "audit_danger": {"types": {"audit_danger_alert"}, "levels": None},
    # 营销目标达成（P0 2026-08-09：客户完成设定目标——付费/订阅/关系达标/摸底
    # 完成，goals.notify 扫描器发布；好消息逐条即时推，完成后 48h 是跟进黄金窗）
    "goal_complete": {"types": {"goal_completed_alert"}, "levels": None},
    # 营销目标失守日报（P1 2026-08-09：failed/expired **聚合**一条——坏消息逐条
    # 推=告警风暴；攒够条数或压龄 24h 才出账，按模板/账号分布给调参抓手）
    "goal_miss": {"types": {"goal_miss_alert"}, "levels": None},
    # 常备扫描循环停摆（P4 2026-08-09：目标结算/提醒 + 工作链推进的心跳停走
    # ——两者都曾挂死调度器**静默从未运行**，此类断线从此主动报警不靠人发现）
    "scan_stall": {"types": {"scan_loop_stall_alert"}, "levels": None},
    # 内嵌网页端选择器失配（2026-08-10：桌面壳把官方网页端嵌进 webview 注入脚本，
    # 官方一改版 DOM 就抓不到气泡/输入框 → 翻译与回流静默全断；与 platform_session
    # 正交：那个是登录态坏了要重登，这个是登录态好着但选择器要改）
    "inject_health": {"types": {"inject_health_alert"}, "levels": None},
    # 人工接管超时（驾驶舱 P0 2026-08-13：坐席接管会话后忘了交还——AI 对该客户
    # 持续停摆＝没人管；watchdog 按 takeover_remind 升级式提醒）
    "takeover": {"types": {"takeover_alert"}, "levels": None},
    # 托管代理生命周期（一键代理 P2 2026-08-21：到期预警/续期被拦/续期未入账/
    # 到期回收/库存水位——「代理过期了坐席还在用」与「地区卖空了没人补货」都
    # 必须有人被点名）
    "proxy_managed": {"types": {"proxy_managed_alert"}, "levels": None},
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
    "accounts_truth":   "cp.alert.accounts_truth",      # 有账号绕过登记在收发
    "buried_conv":      "cp.alert.buried_conv",         # 会话被归档埋掉，客户在等却看不见
    "cases":            "cp.alert.cases",               # 会话需要人工跟进（案例开案/升级）
    "bot_peer":         "cp.alert.bot_peer",            # 对方疑似机器人，AI 已自动停发/降档
    "login_funnel":     "cp.alert.login_funnel",        # 账号一直接不上
    "queue_alert":      "cp.alert.queue_alert",         # 会话排队超时
    "csat_alert":       "cp.alert.csat_alert",          # 客户满意度跌破
    "reply_risk":       "cp.alert.reply_risk",          # 高危回复预警
    "billing_alert":    "cp.alert.billing_alert",       # 席位/用量异常
    "persona_retired":  "cp.alert.persona_retired",     # 已删的人设设定被写回（运营在人设工作室可自行处置）
    # 2026-08-08：客户说了最后一句、系统既没回也没拟稿。此前它在 _EVENT_ALIASES 里
    # 存在（技术上可订阅），却**不在任何受众目录**→ alert_audience 归 other → 告警渠道
    # 面板从来没把这一项列出来。于是「没人订阅」不是运营疏忽，是产品没提供选项：本机
    # 实测 8 条真实漏球全程零外发。语义上它和 draft_backlog/buried_conv 是同一族
    # （客户在等 + 运营能行动），归 business。
    "unanswered_inbound": "cp.alert.unanswered_inbound",
    # 实施93c：客户点了引导链接＝购买意向最热时刻，运营/坐席立刻跟进
    "cta_click": "cp.alert.cta_click",
    # 目标达成＝老板/运营最想收的好消息（成交/订阅/关系达标），且需要立刻行动
    # （趁热跟进），天然 business。
    "goal_complete": "cp.alert.goal_complete",
    # 目标失守日报＝运营要调整打法的信号（哪类目标/哪个号在漏），能行动 → business。
    "goal_miss": "cp.alert.goal_miss",
    # AI 对聊提醒＝老板/运营可行动（是测试就忽略；不是就把任一侧切手动止损配额），
    # 与 bot_peer 同族的「对端身份」信号，天然 business。
    "ai_mutual_chat": "cp.alert.ai_mutual_chat",
    # 托管代理生命周期（2026-08-21 一键代理 P2）：到期/续费受阻要充值、库存告急要
    # 补货——处置人都是运营（钱+货），不是技术支持，归 business。
    "proxy_managed": "cp.alert.proxy_managed",
    # ── 2026-08-21 完备性棘轮补登（原属各自 lane 的登记债，掉进 other=面板不列）──
    # 人工接管超时：坐席接管后忘交还、客户没人管——主管/运营处置，business。
    "takeover": "cp.alert.takeover",
    # 回复额度触顶聚合：调额度/豁免入口都在设置页「回复额度守卫」卡，运营动作。
    "reply_budget": "cp.alert.reply_budget",
    # 坐席字符额度水位：谁快用满/谁超额＝排额度，运营动作。
    "agent_quota": "cp.alert.agent_quota",
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
    "image_models":        "cp.alert.image_models",
    "hub_engine":          "cp.alert.hub_engine",
    "human_deliver":       "cp.alert.human_deliver",
    "bug_intake":          "cp.alert.bug_intake",
    "assistant_report":    "cp.alert.assistant_report",
    "tg_call":             "cp.alert.tg_call",
    "orchestrator_worker": "cp.alert.orchestrator_worker",
    "memory_key_drift":    "cp.alert.memory_key_drift",
    "media_promise":       "cp.alert.media_promise",
    "colloquial_llm":      "cp.alert.colloquial_llm",
    "voice_burst":         "cp.alert.voice_burst",
    "anomaly":             "cp.alert.anomaly",
    "trial_fulfiller":     "cp.alert.trial_fulfiller",
    "audit_danger":        "cp.alert.audit_danger",
    # 2026-08-08 与 unanswered_inbound 同批补登：这两个同样只存在于 _EVENT_ALIASES、
    # 从未出现在面板。voice_outage 尤其讽刺——它正是为「音色档 404 → 整链拒发 5 天
    # 零告警」建的告警，自己却也没有订阅入口。
    "lan_gpu":             "cp.alert.lan_gpu",
    "voice_outage":        "cp.alert.voice_outage",
    # 常备循环心跳停走＝基建自愈信号（开发/技术支持处置），terminal 运营看不懂
    "scan_stall":          "cp.alert.scan_stall",
    # 选择器失配的修法是改 desktop_selector_profiles.json 覆写层（技术支持动作），
    # 终端运营看不懂「composer 选择器失效」，故归 technical。
    "inject_health":       "cp.alert.inject_health",
    # 本地主链保险单向热切 cloud（2026-08-21 完备性棘轮补登）：vLLM 探测连败 →
    # 自动切云端保服务——「起本地端点再切回」是基建动作，终端运营无从处置。
    "ai_primary_guard":    "cp.alert.ai_primary_guard",
}


def _vram_lines(hosts: Any, top: int = 2) -> str:
    """显存归因行：直接点名「谁占着、能不能让路」，省掉翻 GPU 面板那一步。

    共享给两族告警（``hub_engine_alert`` 的「引擎离线」与 ``voice_outage_alert``
    的「在岗但每发必超时」）——它们是**同一个根因的两种表现**：显存不够时，
    hub 要么把引擎泊掉（离线，目录 available:false），要么让它带着满卡硬跑
    （在岗但 36~69s 出话，预算 30s ⇒ 每发必超时）。运维要做的动作完全一样
    （泊掉可让路的服务 / 上机清无主进程），所以这段话必须一字不差地出现在两处，
    不能只有其中一个告警带根因（2026-08-22 事故里响的恰好是没带根因那半边）。

    **读不到归因时回落成手查指引而不是留空**：归因本身要打一次 hub HTTP，凌晨
    显存打满时那次调用恰恰最容易超时——「最需要根因的时刻正好取不到根因」。
    留空等于把运维推回「绿灯但没声音，无从下手」的原点，所以宁可给一条要自己
    敲的命令，也不能一个字都不说。
    """
    out = ""
    for host in (hosts or [])[:max(1, int(top))]:
        if not isinstance(host, dict):
            continue
        # 三档限定词对应三种证据强度，混排成同一个裸数字必然误导（2026-08-22 两版
        # 都栽在这条线上，方向相反）：无限定＝本机**实测**驻留；「约」＝remote 只报
        # 标称预算（hub 没现场采样过它们，按它算「泊这俩就够」会发现不够）；
        # 「疑似」＝服务在线但 hub 把占用归不到它，额度是从同台无主池里估的上限
        # ——值得先试一把泊车，但不能当承诺。实测确为 0 的服务在上游已剔除。
        cands = list(host.get("parkable") or [])[:3]

        def _who(svc: Any) -> str:
            # hub 的 label 是「短名 (技术细节)」，硬截 22 字会切在括号中间，出来
            # 像「唱歌工作室 (AI 翻唱·YingMusic 疑似5.6G」——告警长得像坏了，人就
            # 不信它。太长时取括号前的短名（那正是给人看的部分）。
            raw = str(svc.get("label") or svc.get("name") or "")
            if len(raw) > 22:
                raw = (raw.split(" (", 1)[0] or raw)[:22]
            tag = ("疑似" if svc.get("unattributed")
                   else ("约" if svc.get("est") else ""))
            return f"{raw} {tag}{int(svc.get('mem_mb') or 0) / 1024:.1f}G"

        who = "、".join(_who(s) for s in cands)
        # 无主显存单独点名：它不可泊车，却常是最大一块（实测 176 两个孤儿
        # python.exe 合计 8.6G）。不说的话运维泊完可让路的那些、发现还是不够，
        # 线索就断了。**但若上列里有「疑似」项，这笔与它们重叠**——那些服务正是
        # 因为占用归不到自己才被标疑似，额度就是从这个池子里估的。不点出重叠，
        # 运维会把 8.6G 和 5.6G 相加去规划，算出来的余量是假的。
        extras_gb = int(host.get("extras_mb") or 0) / 1024
        overlap = ("，其中一部分疑似就是上列服务，先试泊车再上机"
                   if any(s.get("unattributed") for s in cands) else
                   "，泊车腾不出来，需上机排查")
        orphan = (f"；另有 {extras_gb:.1f}G 无主进程占用（非服务{overlap}）"
                  if extras_gb >= 1.0 else "")
        # 常驻大模型单列：它常是最大一笔且**能动**（hub 会驱逐 LAN 大模型让路），
        # 与「无主进程」必须分开说——后者要上机杀，前者不用。混成一句话，运维会
        # 把可以自动让路的那部分也当成要上机处理的死账。
        llm_gb = int(host.get("llm_mb") or 0) / 1024
        llm = (f"；另有常驻大模型 {llm_gb:.1f}G（ollama 驻留，"
               f"hub 会自动驱逐让路，无需上机）" if llm_gb >= 1.0 else "")
        if not who and not orphan and not llm:
            continue
        free_gb = int(host.get("free_mb") or 0) / 1024
        out += (f"**显存({host.get('ip') or 'hub'})**: 空闲 "
                f"{free_gb:.1f}G，可让路：{who or '无'}{orphan}{llm}\n")
    return out or ("**显存**: 本次未取到归因（hub 打满时这一跳也会超时）——"
                   "手查 `GET /api/gpu/overview`，看哪些 parkable 服务在占卡\n")


def _is_hub_reason(reasons: Any) -> bool:
    """失败原因是否指向共用合成层（hub），决定该不该渲染显存归因段。

    单链臂的原因可能是设备端的（``send:``/``share_skip_`` 前缀＝那台手机上的
    share intent 失败），显存归因对它毫无意义：给了只会把运维引到 GPU 面板前
    白站一轮。watchdog 侧同样只对 hub 原因去取归因数据，但两边的判断服务于
    不同问题——它决定「要不要花一次 HTTP」，这里决定「这段话该不该出现」，
    所以不能靠 ``vram_hosts`` 空不空来推断（空既可能是「与显存无关」也可能是
    「相关但没取到」，而后者恰恰必须出手查指引）。
    """
    try:
        keys = list(reasons.keys()) if isinstance(reasons, dict) else []
    except Exception:
        return False
    return any(m in str(k) for k in keys
               for m in ("hub_", "synth_failed", "engine_mismatch"))


# 语音链 source → 人话（voice_outage 单链告警的标题里要点名是哪条链；裸 key
# 如 mr_rpa 对运营是黑话）。缺登记时原样回落 key，绝不因新链未登记而不报。
_VOICE_SOURCE_LABELS: Dict[str, str] = {
    "aline": "Telegram 原生语音回复",
    "autosend": "全自动回复语音（B 线）",
    "manual": "坐席手动发语音",
    "wa_rpa": "WhatsApp 手机端语音",
    "mr_rpa": "Messenger 手机端语音",
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

    elif event_type == "cta_clicked":
        # 实施93c：客户首次点开引导链接——最热跟进窗口，文案指路立刻开聊
        _tn = str(data.get("target_name") or data.get("target_id") or "?")
        title = "🔥 客户点了引导链接"
        text = (
            f"**目标**: {_tn}\n"
            f"**会话**: {data.get('conversation_id', '?')}\n"
            "点击后几分钟内是最热跟进窗口——"
            "[💬 立即打开会话](/workspace)"
        )

    elif event_type == "bug_intake_alert":
        if str(data.get("kind") or "") == "crisis":
            title = "🧨 报障群危机词（AI 已压制，需人工接管）"
            text = (
                f"**群**: {data.get('chat_id', '?')}\n"
                f"**用户**: {data.get('reporter', '?')}\n"
                f"**原话**: {str(data.get('text') or '')[:120]}\n"
                "[📥 前往收件箱群组动态](/workspace/inbox)"
            )
        else:
            _sev = str(data.get("severity") or "?")
            title = (f"{'🚨' if _sev == 'P0' else '🐞'} 报障群新工单 "
                     f"#{data.get('ticket_id', '?')} [{_sev}]")
            _rc = int(data.get("report_count") or 1)
            text = (
                f"**标题**: {str(data.get('title') or '')[:120]}\n"
                f"**报告人**: {data.get('reporter', '?')}"
                + (f"（第 {_rc} 人）" if _rc > 1 else "") + "\n"
                f"**群**: {data.get('chat_id', '?')}"
            )

    elif event_type == "assistant_report_alert":
        _sev = str(data.get("severity") or "P2")
        title = (f"{'🚨' if _sev == 'P0' else '🛎️'} 助手面板报障 "
                 f"#{data.get('ticket_id', '?')} [{_sev}]"
                 + ("（并单+1）" if data.get("dup") else ""))
        # 截图提示 + 处置入口（2026-08-27）：原文案只有描述/人/页面，客服既
        # 不知道有没有图、也没有可点的去处——收到通知却不知道下一步做什么，
        # 等于把「最后一公里」接通了又停在路口。
        _shots = int(data.get("shots") or 0)
        text = (
            f"**描述**: {str(data.get('text') or '')[:120]}\n"
            f"**报告人**: {data.get('reporter', '?')}（web）\n"
            f"**页面**: {data.get('page', '?')}\n"
            + (f"**附件**: 📎 {_shots} 张截图\n" if _shots else "")
            + "[📋 前往处置](/admin/ops)"
        )

    elif event_type == "case_alert":
        _src_names = {
            "crisis": "危机信号", "human_request": "客户要求人工",
            "ai_doubt": "怀疑机器人", "media_complaint": "质疑照片/媒体",
            "escalation": "满意度升级", "intent_chain": "意图链风险",
        }
        _src = _src_names.get(str(data.get("source") or ""),
                              str(data.get("source") or "?"))
        _sev = int(data.get("severity") or 1)
        _act = "严重度升级" if data.get("action") == "upgraded" else "新开案"
        # P8：同源历史处置上下文——接警的人开局就知道旧招用过没用
        _prior = data.get("prior_resolutions") or {}
        _pb = _prior.get("buckets") or {}
        _prior_txt = ""
        if _pb:
            _bn = {"soothed": "安抚", "handoff": "转人工",
                   "false_alarm": "判误报", "empty": "未填原因", "other": "其他"}
            _hist = "、".join(
                f"{_bn.get(k, k)}×{v}" for k, v in sorted(_pb.items()))
            _ago = _prior.get("last_closed_ago_hours")
            _ago_txt = f"，最近一次 {_ago}h 前" if _ago is not None else ""
            _prior_txt = (f"**历史处置**: {_hist}{_ago_txt}"
                          "——同类问题处置后又立案，考虑换打法\n")
        title = f"{'🚨' if _sev >= 3 else '🟠'} 需要人工跟进：{_src}"
        text = (
            f"**动作**: {_act}（severity {_sev}）\n"
            f"**用户**: {str(data.get('user_id') or '?')[-8:]}\n"
            f"**案号**: {data.get('case_id', '?')}\n"
            f"**最近消息**: {str(data.get('last_message') or '')[:60] or '（无）'}\n"
            f"{_prior_txt}"
            "[🗂 打开案例跟进](/cases)"
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
        # WP-3 日报档：同通道 period="daily" 标记换标题；周报渲染逐字节不变
        if str(data.get("period") or "") == "daily":
            title = "📰 运营日报（近 24 小时）"
        else:
            title = f"📰 运营周报（近 {days} 天）"
        lines = [f"- {h}" for h in (data.get("headline") or [])]
        # AI 价值总账行（2026-08-06，health_watchdog 装配随报文携带；上限防撑爆推送）
        lines.extend(f"- {vl}" for vl in (data.get("value_lines") or [])[:6])
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

    elif event_type == "unanswered_inbound_alert":
        if data.get("recovered"):
            title = "✅ 入站漏球已清"
            text = (
                "**状态**: 所有客户的最后一条消息都已有回复或有稿在审\n"
                "[📥 打开工作台](/workspace/unified-inbox)"
            )
        else:
            cnt = data.get("count", "?")
            oldest = data.get("oldest_hours", "?")
            samples = data.get("samples") or []
            lines = [
                f"- {s.get('platform','?')}:"
                f"{str(s.get('conversation_id') or '?')[-12:]}"
                f"（已等 {s.get('age_hours','?')}h）"
                for s in samples[:5]
            ]
            title = f"📭 客户在等：{cnt} 个会话没人回、也没有草稿"
            text = (
                f"**数量**: {cnt} 个（最老 {oldest}h）"
                + ("（重提）" if data.get("reminder") else "") + "\n"
                + ("\n".join(lines) + "\n" if lines else "")
                + "**含义**: 最后一条是客户消息，超时无任何回复且无待审稿——"
                "拟稿链可能丢球（风控静默拦下/生成失败/触发漏了）\n"
                "[📥 打开工作台](/workspace/unified-inbox)"
            )

    elif event_type == "image_models_alert":
        if data.get("recovered"):
            title = "✅ 出图模型已恢复"
            text = (
                "**状态**: ComfyUI 模型清单已重新可用，生图链恢复\n"
                "[📊 查看运营总览](/admin/ops)"
            )
        else:
            title = ("🖼️ 出图模型失踪（重提）" if data.get("reminder")
                     else "🖼️ 出图模型失踪")
            text = (
                f"**服务**: {data.get('url') or 'ComfyUI'}\n"
                f"**清单**: checkpoints {int(data.get('ckpts') or 0)} 项 / "
                f"unet {int(data.get('unets') or 0)} 项（此前曾部署过 FLUX）\n"
                "**影响**: 现场生图（手动出图/生成链）将全数失败，相册链不受影响\n"
                "**排查**: 检查出图机 ComfyUI models\\checkpoints 目录是否被误删"
                "（2026-08-22 曾发生整树清空）；恢复模型文件后本告警自动解除\n"
                "[📊 查看运营总览](/admin/ops)"
            )

    elif event_type == "hub_engine_alert":
        eng = str(data.get("engine") or "")[:60]
        if data.get("recovered"):
            title = "✅ hub 语音引擎已重新上岗"
            text = (
                f"**引擎**: `{eng}` 在 hub 目录里恢复 `available:true`，"
                "合成不再被顶包\n"
                "[📊 查看运营总览](/admin/ops)"
            )
        else:
            down_min = int(data.get("down_minutes") or 0)
            down_txt = (f"{down_min // 60} 小时 {down_min % 60} 分钟"
                        if down_min >= 60 else f"{down_min} 分钟")
            prefix = "⏰" if data.get("reminder") else "🎚️"
            title = f"{prefix} hub 语音引擎不可用（已 {down_txt}）"
            if data.get("strict"):
                impact = ("`voice_consistency=strict` → 该人设语音**整链拒发**"
                          "（客户收不到语音，退化成纯文字）")
            else:
                impact = ("`voice_consistency=lenient` → hub **静默换别的引擎**"
                          "合成，客户听到的是**另一个人的声音**（比收不到更糟）")
            avail = [str(x)[:24] for x in (data.get("available_engines") or [])]
            avail_txt = ("，目前在岗：" + "、".join(avail[:6])) if avail else ""
            # 自动唤醒结果：受理了就告诉运维「可能不用起床」，失败了才是真要人上
            wake = str(data.get("wake") or "")
            if wake.startswith("accepted"):
                    wake_txt = ("**已自动尝试唤醒**: 已受理，成了的话下一轮巡检会补发"
                                "恢复通知（冷载实测 ~35s）——先别动手\n")
            elif wake.startswith("failed"):
                reason = wake.split(":", 1)[-1][:40]
                wake_txt = f"**已自动尝试唤醒**: 失败（{reason}）→ 需要人工介入\n"
            else:
                wake_txt = ""
            vram_txt = _vram_lines(data.get("vram_hosts"))
            text = (
                f"**引擎**: `{eng}` 在 hub 引擎目录标着 `available:false`{avail_txt}\n"
                f"**服务**: {data.get('url') or 'hub'}\n"
                f"**影响**: {impact}\n"
                f"{wake_txt}{vram_txt}"
                "**排查**: 别去重启引擎进程——2026-08-22 实测**引擎自身 `/health` "
                "200、`model_loaded:true`，hub 目录却仍标不可用**，hub 的路由只认"
                "目录这一票。到 hub 侧重新登记上岗（`POST /api/engine/start`，或"
                "运营面板拉起该引擎），再看 `GET /api/engines` 是否转 "
                "`available:true`；上面点名的 parkable 服务泊车即可腾显存\n"
                "[📊 查看运营总览](/admin/ops)"
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
            rescue_broken = [str(t) for t in (data.get("rescue_broken") or [])]
            if rescue_broken:
                # 救援链本身已死：默认指引「等看门狗/手动拉任务」会误导——
                # 任务是 Disabled/缺失，拉不起来，必须先恢复任务本体
                fix = ("⚠️ 救援链已停用：计划任务 "
                       + "、".join(rescue_broken)
                       + " 处于禁用/缺失，自动拉起**不会发生**——"
                         "请先 `schtasks /Change /TN <任务名> /ENABLE` "
                         "恢复后再拉起服务（若集群仍在代码模式，先与占用方确认）")
            elif hang:
                fix = ("EmotionTTSWatchdog 自动重启未能救回，请上机检查 "
                       "logs\\emotion_tts.out.log / GPU 状态")
            else:
                fix = "请检查 emotion_tts 服务或手动拉起计划任务 EmotionTTS_Boot"
            text = (
                f"**状态**: {state}（{str(data.get('url') or '')}）\n"
                f"**影响**: 在线语音已降级 edge 通用声——聊天不中断，"
                "但克隆音色/情感语气不可用\n"
                f"**详情**: {str(data.get('error') or '')[:150]}\n"
                f"{fix}\n"
                "[📊 查看运营总览](/admin/ops)"
            )

    elif event_type == "voice_outage_alert":
        _src = str(data.get("source") or "").strip()
        _src_txt = _VOICE_SOURCE_LABELS.get(_src, _src)
        # 缩窗自陈：标题里的「24h」在台账事件挤满时是假的（更早的失败已被挤出
        # deque），而「窗内 0 成功」的分母就是这些事件。不说清楚，运维会按 24h
        # 估断档时长、并误以为「早上那段是好的」。
        _win_note = ""
        if data.get("window_truncated"):
            try:
                _eff = float(data.get("effective_window_hours") or 0)
            except (TypeError, ValueError):
                _eff = 0.0
            if _eff > 0:
                _win_note = (
                    f"**窗口提醒**: 语音台账事件已满，这个窗实际只覆盖 {_eff:.1f}h"
                    "——更早的失败已看不到，别据此认为「之前那段是好的」\n")
        if data.get("recovered"):
            title = ("✅ 语音出站已恢复" if not _src
                     else f"✅ 语音出站已恢复（{_src_txt}）")
            text = (
                "**状态**: 观察窗内重新有语音条成功发出\n"
                "[📊 查看运营总览](/admin/ops)"
            )
        elif _src:
            # 单链臂：其他链正常，所以**全局告警不会响**——这条是唯一信号，
            # 标题必须点名是哪条链，否则运维会去查「语音是不是全挂了」然后
            # 发现「Telegram 好着呢」，把真实故障（WhatsApp 全灭）当误报关掉。
            n = int(data.get("attempts") or 0)
            win = int(data.get("window_hours") or 24)
            reasons = data.get("top_reasons") or {}
            r_txt = "、".join(f"{k}×{v}" for k, v in sorted(
                reasons.items(), key=lambda kv: -int(kv[1] or 0))) or "—"
            prefix = "⏰" if data.get("reminder") else "🔇"
            title = (f"{prefix} {_src_txt}的语音全发不出："
                     f"{win}h 内 {n} 次尝试 0 成功")
            text = (
                f"**范围**: 只有这一条链全灭，其他语音链在同一窗口内有成功——"
                "所以全局「语音出站断档」告警**不会**响，这条是唯一信号\n"
                f"**失败原因 Top**: {r_txt}\n"
                "**影响**: 该链该发语音的回复全部回落文字（或压根没发出），"
                "其他平台的客户听不出异常\n"
                "**排查**: 先按上面的原因分辨是**这条链自己的活**"
                "（`send:`/`share_skip_` 前缀＝设备端 share intent/收件人定位失败，"
                "去看那台设备）还是**共用的合成层**（`hub_` 前缀/`synth_failed`"
                "＝所有链都会中，只是别的链此刻恰好没发语音）\n"
                # 单链臂在多链下常常是**唯一**会响的那条（其他链有成功 → 全局臂
                # 闭嘴），所以合成层根因也必须在这里给全，不能只在全局臂里说；
                # 但设备端原因不渲染显存段（见 `_is_hub_reason`）。
                f"{_vram_lines(data.get('vram_hosts')) if _is_hub_reason(reasons) else ''}"
                f"{_win_note}"
                "[📊 查看运营总览](/admin/ops)"
            )
        else:
            n = int(data.get("attempts") or 0)
            win = int(data.get("window_hours") or 24)
            streak = int(data.get("consecutive_fails") or 0)
            reasons = data.get("top_reasons") or {}
            r_txt = "、".join(f"{k}×{v}" for k, v in sorted(
                reasons.items(), key=lambda kv: -int(kv[1] or 0))) or "—"
            try:
                last_ok_h = float(data.get("last_ok_hours", -1))
            except (TypeError, ValueError):
                last_ok_h = -1.0
            ok_txt = (f"{last_ok_h:.1f} 小时前" if last_ok_h >= 0
                      else "本进程内无成功记录")
            prefix = "⏰" if data.get("reminder") else "🔇"
            title = f"{prefix} 语音出站连续失败：{win}h 内 {n} 次尝试 0 成功"
            # 引擎冒名与「hub 挂了」的处置完全相反：后者去看 /health，前者 /health
            # 正绿着（2026-08-22 事故就是被这条通用排查语引偏，潜伏两小时）。
            offline = next(
                (str(k) for k in reasons if "hub_engine_offline" in str(k)), "")
            mismatch = next(
                (str(k) for k in reasons if "hub_engine_mismatch" in str(k)), "")
            timing = next(
                (str(k) for k in reasons if "hub_synth_timing_out" in str(k)), "")
            if timing:
                # 最刁的一档：目录 available:true + 引擎 /health 200 + model_loaded
                # 全绿，**所有读数都对而结论全错**。不点名「别看那两个绿灯」，运维
                # 会照着上面 offline/mismatch 的排查语去查目录，然后困惑到放弃。
                eng = timing.split(":", 1)[1] if ":" in timing else timing
                fix = (
                    f"**⚠️ 引擎在岗但答不上来**: `{eng[:60]}` 连续合成超时，本进程"
                    "已熔断开路、**主动跳过** hub（客户立刻拿到文字，不再白等一轮"
                    "预算）\n"
                    "**排查**: 目录 `available:true` 与引擎 `/health` 200 **此刻都是"
                    "绿的，别在那两处找原因**——2026-08-22 实测同卡显存 97% 满"
                    "（唱歌 5.7G + 10G 无主 python 进程 + 两个 ollama 模型常驻）→ "
                    "8 字短句要 36~69s，而预算 `timeout_sec=30` ⇒ 每一发都超时\n"
                    # 归因随告警一起给：这一档的根因**几乎总是**显存，而「自己去看
                    # GPU 面板」这一步发生在凌晨、发生在人还没睡醒的时候。
                    # 引擎离线那族告警早就带这段了，唯独最需要它的这族没带。
                    f"{_vram_lines(data.get('vram_hosts'))}"
                    "**处置**: 上面点名的 parkable 服务（唱歌/出图）先泊掉；"
                    "无主进程占的泊车腾不出来、要上机排查；也可以把质量轨引擎挪到"
                    "别的卡。根因不解决，语音会一直缺席\n"
                    "**注意**: 熔断每 5 分钟自动放一发探路，成了就自动恢复——"
                    "所以「过一会儿好了」不等于问题解决了\n")
            elif offline:
                # 合成前预检拦下：根因最明确的一档，直接点名该开哪个引擎
                eng = offline.split(":", 1)[1] if ":" in offline else offline
                fix = (
                    f"**⚠️ 引擎不可用**: hub 引擎目录里 `{eng[:60]}` 标着 "
                    "`available:false`，再合成必被换成别的引擎（=别人的声音）\n"
                    "**排查**: 别去重启引擎进程——2026-08-22 实测**引擎自身 "
                    "`/health` 200、`model_loaded:true`，hub 目录却仍标不可用**，"
                    "hub 的路由只认目录这一票。到 hub 侧把它重新登记上岗"
                    "（`POST /api/engine/start`，或运营面板拉起该引擎），再看 "
                    "`GET /api/engines` 里它是否转 `available:true`\n"
                    f"{_vram_lines(data.get('vram_hosts'))}"
                    "**显存吃紧时**: 先把上面点名的 parkable 服务泊车让位\n")
            elif mismatch:
                detail = mismatch.split(":", 1)[1] if ":" in mismatch else mismatch
                fix = (
                    f"**⚠️ 引擎冒名**: {detail[:120]}\n"
                    "**排查**: hub `/health` 此时多半是绿的——它把点名的引擎**换成了"
                    "另一个**（prefer 语义，响应信封不带引擎字段）。查 hub 引擎目录"
                    "里目标引擎是否 `available:false`，恢复后语音自动回来；"
                    "临时放行可置 `avatar_voice.hub_fish.verify_engine: false`"
                    "（会真的把别人的声音发给客户，仅排障用）\n")
            else:
                fix = (
                    "**排查**: 常见原因＝hub 音色档缺失(404)/7852 掉线/参考音丢失；"
                    "查 app.log 关键字 `TTS failed`，再看 `/api/voice/avatar-status`\n")
            text = (
                f"**连败**: {streak} 次；最近一次成功: {ok_txt}\n"
                f"**失败原因 Top**: {r_txt}\n"
                "**影响**: 该发语音的回复全部回落文字发出（低流量下 7852 探针"
                "可能全绿），克隆声卖点静默失效\n"
                f"{fix}"
                f"{_win_note}"
                "[📊 查看运营总览](/admin/ops)"
            )

    elif event_type == "lan_gpu_alert":
        host = str(data.get("host") or data.get("url") or "-")
        if data.get("recovered"):
            title = f"✅ LAN GPU 主机已恢复（{host}）"
            text = (
                "**状态**: 主机重新可达，嵌入/视觉/兜底 LLM/本地 MT 将自动回主路\n"
                "[📊 查看运营总览](/admin/ops)"
            )
        else:
            down_min = int(data.get("down_minutes") or 0)
            down_txt = (f"{down_min // 60} 小时 {down_min % 60} 分钟"
                        if down_min >= 60 else f"{down_min} 分钟")
            prefix = "⏰" if data.get("reminder") else "🖥️"
            title = f"{prefix} LAN GPU 主机不可达（{host}，已 {down_txt}）"
            text = (
                f"**状态**: Ollama /api/version 探测失败（{str(data.get('url') or '')}）\n"
                "**影响**: 各链路已静默转移备点，业务不中断——但本地算力冗余归零，"
                "云端再出问题将没有第二道防线\n"
                f"**详情**: {str(data.get('error') or '')[:150]}\n"
                "请到现场检查主机电源/系统/Ollama 服务\n"
                "[📊 查看运营总览](/admin/ops)"
            )

    elif event_type == "case_backlog_alert":
        if data.get("recovered"):
            title = "✅ 案例积压已清"
            text = (
                "**状态**: 无人认领的待跟进案例已全部处理\n"
                "[🗂 查看案例跟进](/cases)"
            )
        else:
            _urgent = int(data.get("urgent_count") or 0)
            _stale = int(data.get("stale_count") or 0)
            _media = int(data.get("media_stale_count") or 0)
            _oldest = float(data.get("oldest_hours") or 0)
            _srcs = data.get("by_source") or {}
            _src_txt = "、".join(f"{k}×{v}" for k, v in sorted(_srcs.items())) or "—"
            _prefix = ("🚨" if _urgent
                       else ("📸" if _media
                             else ("⏰" if data.get("reminder") else "🟠")))
            _urgent_txt = (f"**危机级无人认领**: {_urgent} 条（最优先处理）\n"
                           if _urgent else "")
            # P5 媒体档：穿帮风险单独点名——客户在等解释，拖越久越像心虚
            _media_txt = (
                f"**媒体质疑超龄**: {_media} 条未认领超过 "
                f"{float(data.get('media_stale_hours') or 4):.0f}h"
                f"（最老 {float(data.get('media_oldest_hours') or 0):.1f}h；"
                "穿帮风险，客户在等解释）\n" if _media else "")
            _media_title = f" / 媒体 {_media} 条" if _media else ""
            title = (f"{_prefix} 案例无人跟进：危机 {_urgent} 条"
                     f"{_media_title} / 超龄 {_stale} 条")
            text = (
                f"{_urgent_txt}"
                f"{_media_txt}"
                f"**超龄未认领**: {_stale} 条（超过 {float(data.get('min_age_hours') or 4):.0f}h）\n"
                f"**最老**: {_oldest:.1f} 小时\n"
                f"**来源**: {_src_txt}\n"
                "[🗂 立即处理](/cases)"
            )

    elif event_type == "bot_peer_alert":
        _who = str(data.get("display_name") or data.get("username")
                   or data.get("conversation_id") or "?")
        _rs = str(data.get("reason") or "?")
        _rs_txt = {
            "tg_is_bot": "Telegram 官方标记为 bot",
            "tg_chat_type_bot": "会话类型为 bot",
            "tg_username_bot": "username 以 bot 结尾（Bot API 专属）",
            "tg_inline_keyboard": "消息带按钮键盘（只有 bot 能发）",
            "suspected_bot": "内容特征疑似自动化（菜单话术/链接刷屏/索取循环）",
            "inbound_repeat": "对方连续多条相同内容",
            "instant_echo": "秒回×同文（自动应答特征）",
            "daily_budget": "今日自动回复达预算上限",
        }.get(_rs, _rs)
        title = f"🤖 检测到机器人对方：{_who}"
        text = (
            f"**会话**: {data.get('conversation_id', '?')}\n"
            f"**判定**: {_rs_txt}\n"
            f"**证据**: {str(data.get('evidence') or '')[:80] or '—'}\n"
            f"**处置**: {data.get('action') or '已跳过自动回复'}"
            "（入站照常进收件箱，坐席可手动交互；误判可在收件箱改回档位）\n"
            "[📥 打开收件箱](/workspace/inbox)"
        )

    elif event_type == "ai_mutual_chat_alert":
        convs = data.get("conversations") or []
        wh = int(data.get("window_hours") or 24)
        _rows = []
        for c in convs[:6]:
            _tag = "（对端是本机受管账号）" if c.get("managed_peer") else ""
            _rows.append(
                f"- `{c.get('conversation_id', '?')}` 入 {int(c.get('n_in') or 0)}"
                f" / 出 {int(c.get('n_out') or 0)}{_tag}")
        title = (f"🔁 AI 对聊提醒：{len(convs)} 个全自动会话 "
                 f"{wh}h 内双向高频互发")
        text = (
            "\n".join(_rows) + "\n"
            "**说明**: 这通常是两个受管账号在互聊（测试属预期——系统只提醒，"
            "未做任何拦截）\n"
            "**若非刻意测试**: 把任一侧会话切「手动」即停；持续互聊会消耗"
            "双边 AI 配额\n"
            "[📥 打开收件箱](/workspace/inbox)"
        )

    elif event_type == "accounts_truth_alert":
        if data.get("recovered"):
            title = "✅ 账号真相：幽灵账号已清零"
            text = (
                "**状态**: 会话库里不再有未登记账号\n"
                "[📊 查看运营总览](/admin/ops)"
            )
        else:
            n = int(data.get("ghost_count") or 0)
            samples = data.get("samples") or []
            samp = "、".join(str(s) for s in samples[:6]) or "—"
            prefix = "⏰" if data.get("reminder") else "🚨"
            title = f"{prefix} {n} 个账号绕过注册表在写会话"
            text = (
                f"**样本**: {samp}\n"
                "**含义**: 聊天库里出现了账号注册表没有的号——不是已退出/桌面镜像"
                "（那些在册），是接入链漏登记。ops「账号真相」卡的「仅目录」应同步亮黄。\n"
                "**处置**: 查该平台的 ingest/登录路径是否 upsert 注册表；"
                "收件箱账号抽屉「历史」区可查看会话/清未读。\n"
                "[📊 查看运营总览](/admin/ops) · [📥 打开收件箱](/workspace/inbox)"
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

    elif event_type == "proxy_managed_alert":
        kind = str(data.get("kind") or "")
        n = int(data.get("count") or 0)
        subs = data.get("subs") or []
        smp = "、".join(
            f"{s.get('country', '?')}/{s.get('kind', '?')}（剩{s.get('remaining_days', '?')}天）"
            for s in subs[:3]) or "—"
        if kind == "expiring":
            title = f"⏳ {n} 条托管代理即将到期（未开自动续期）"
            text = (
                f"**明细**: {smp}\n"
                "**影响**: 到期即解绑，该账号回到无代理/手动代理状态，"
                "登录出口会变（有风控风险）\n"
                "**处置**: 会员页充值后在接入弹窗重新一键开通，"
                "或运营开 `proxies.managed.auto_renew`\n"
            )
        elif kind == "renew_blocked":
            reasons = data.get("reasons") or {}
            r_txt = "、".join(f"{k}×{v}" for k, v in sorted(reasons.items())) or "—"
            need = int(data.get("need_tokens") or 0)
            title = f"🚨 {n} 条托管代理想续费但续不了（{r_txt}）"
            text = (
                f"**缺口**: 共需 {need} Token（insufficient=余额不足需充值；"
                "no_pricing=价格档被下架需运营恢复配置）\n"
                f"**明细**: {smp}\n"
                "**影响**: 到期后代理解绑、账号出口漂移\n"
                "**处置**: 充值 Token 或恢复 `proxies.managed.pricing` 对应档位\n"
            )
        elif kind == "renew_unbilled":
            title = f"🟠 {n} 条托管代理已续期但扣费未入账（待对账）"
            text = (
                "**状态**: 服务已续上（先交付后扣费的刻意方向），钱没收到\n"
                "**处置**: `python tools/proxy_managed_review.py` 列出待对账行，"
                "人工补记或核销\n"
            )
        elif kind == "expired":
            countries = data.get("countries") or {}
            c_txt = "、".join(f"{k}×{v}" for k, v in sorted(countries.items())) or "—"
            title = f"⛔ {n} 条托管代理已到期回收"
            text = (
                f"**地区**: {c_txt}\n"
                "**状态**: 代理已解绑归还库存；对应账号如仍在用会走无代理直连\n"
                "**处置**: 需要续用的账号在接入弹窗重新一键开通\n"
            )
        elif kind == "low_stock":
            low = data.get("low") or {}
            low_txt = "、".join(f"{k} 剩{v}条" for k, v in sorted(low.items())) or "—"
            title = f"📦 托管代理库存告急（阈值 {int(data.get('min_stock') or 0)}）"
            text = (
                f"**水位**: {low_txt}\n"
                "**影响**: 卖空地区的一键开通会显示「暂无库存」，用户流失点\n"
                "**处置**: 采购后在接入弹窗「手动添加代理」批量灌入对应地区库存\n"
            )
        elif kind == "vendor_balance_low":
            title = (f"💳 代理供应商预付余额告急："
                     f"{data.get('balance', '?')}（提醒线 {data.get('min_alert', '?')}）")
            text = (
                "**影响**: 即买即用模式下余额就是库存——烧穿后所有地区的一键开通"
                "都会开始失败\n"
                "**处置**: 去供应商后台充值（USDT），到账后本告警自动清零\n"
            )
        else:
            title = "🌐 托管代理事件"
            text = f"**detail**: {json.dumps(data, ensure_ascii=False)[:400]}\n"
        text += "[📊 查看运营总览](/admin/ops)"

    elif event_type == "reply_budget_alert":
        if data.get("recovered"):
            title = "✅ 回复额度触顶已清零"
            text = (
                "**状态**: 今日触顶的会话已全部豁免或跨日恢复\n"
                "[🛡️ 打开自动回复设置](/reply-settings)"
            )
        else:
            n = int(data.get("exhausted_count") or 0)
            hard = int(data.get("hard_count") or 0)
            near = int(data.get("near_count") or 0)
            lim = int(data.get("budget_limit") or 0)
            prefix = "⏰" if data.get("reminder") else "🚨"
            samples = data.get("samples") or []
            smp_txt = "、".join(
                f"{s.get('title', '?')}（{s.get('used', '?')}轮）"
                for s in samples[:3]) or "—"
            near_txt = (f"**接近额度**: 另有 {near} 个会话已用 ≥80%（提前量，"
                        "可先豁免或调大额度）\n" if near else "")
            title = f"{prefix} {n} 个会话今日烧穿回复额度（{lim} 轮/日）"
            text = (
                f"**硬停**: {hard} 个已超 2× 硬顶（拟稿也停了）；其余软停转人审\n"
                f"**用量最高**: {smp_txt}\n"
                f"{near_txt}"
                "**影响**: 触顶会话今日不再自动回复（真人会话仍拟稿待人审）；"
                "多会话同日触顶＝额度配小了或撞上机器人波次\n"
                "**处置**: 设置页「回复额度守卫」卡逐会话「今日继续」豁免，"
                "或调大每日额度；确认是 bot 波次则维持拦截\n"
                "[🛡️ 打开自动回复设置](/reply-settings)"
            )

    elif event_type == "agent_quota_alert":
        if data.get("recovered"):
            title = "✅ 坐席字符额度水位已回落"
            text = (
                "**状态**: 此前接近/超额的坐席已全部回落（调额或月初账本重置）\n"
                "[👥 打开用户管理](/users)"
            )
        else:
            w = int(data.get("warn_count") or 0)
            o = int(data.get("over_count") or 0)
            month = str(data.get("month") or "")
            prefix = "⏰" if data.get("reminder") else "🚨"
            over_txt = "、".join(
                f"{r.get('username', '?')}（{r.get('used', '?')}/{r.get('quota', '?')}，{r.get('pct', '?')}%）"
                for r in (data.get("over") or [])[:3]) or "—"
            warn_txt = "、".join(
                f"{r.get('username', '?')}（{r.get('used', '?')}/{r.get('quota', '?')}，{r.get('pct', '?')}%）"
                for r in (data.get("warn") or [])[:3]) or "—"
            title = f"{prefix} 坐席字符额度：{o} 人超额、{w} 人接近上限（{month}）"
            text = (
                f"**超额 Top**: {over_txt}\n"
                f"**接近 Top**: {warn_txt}\n"
                "**影响**: enforce 硬闸开启时，超额坐席的手动翻译/语音合成会被拦"
                "（硬闸默认关＝当前仅软提醒，消耗仍在发生）\n"
                "**处置**: 用户管理页调整该坐席的月度字符额度，或提醒其节流；"
                "额度按自然月自动重置\n"
                "[👥 打开用户管理](/users)"
            )

    elif event_type == "goal_completed_alert":
        _who = str(data.get("contact_name") or data.get("chat_key")
                   or data.get("conversation_id") or "客户")
        _tname = str(data.get("template_name") or data.get("template") or "目标")
        _title_txt = str(data.get("title") or _tname)
        _acct = f"{data.get('platform', '?')}:{data.get('account_id', '?')}"
        _kind_txt = {
            "order": "官网订单回流",
            "manual": "坐席拍板成交",
            "auto": "信号自动结算（权益/阶段/回话达标）",
        }.get(str(data.get("result_kind") or ""), "—")
        _amount = data.get("amount")
        _amount_txt = (f"**金额**: {_amount}\n" if _amount is not None else "")
        _prod = str(data.get("product") or "")
        _prod_txt = (f"**产品**: {_prod}\n" if _prod else "")
        _days = data.get("days_to_done")
        _days_txt = (f"，用时 {_days} 天" if _days is not None else "")
        # 摸底要点（P3 2026-08-18：goals.notify.include_profile 显式开启才随
        # payload 出现——画像值出境到外部 IM 是 opt-in，缺省推送不含客户画像）
        _brief = str(data.get("slots_brief") or "")
        _brief_txt = (f"**摸底要点**: {_brief}\n" if _brief else "")
        # 会话深链（收到推送的人 10 秒内能落到现场跟进；conv 含冒号属合法
        # query 值，仍统一 quote 防脏 id 破坏 markdown 链接）
        _conv = str(data.get("conversation_id") or "")
        _conv_link = (
            f"[💬 打开会话](/workspace?conv={urllib.parse.quote(_conv)}"
            "&card=goal) · " if _conv else "")
        title = f"🎯 目标达成：{_who} 完成「{_title_txt}」"
        text = (
            f"**账号**: {_acct}\n"
            f"**目标**: {_tname}{_days_txt}\n"
            f"**完成方式**: {_kind_txt}\n"
            f"{_amount_txt}"
            f"{_prod_txt}"
            f"{_brief_txt}"
            "**建议**: 完成后 48h 是跟进黄金窗——发个感谢/追加关怀，"
            "转化类可顺手启动留存链\n"
            f"{_conv_link}"
            "[🎯 打开目标达成报表](/workspace/goal-report?status=done&days=7)"
        )

    elif event_type == "scan_loop_stall_alert":
        _loop_names = {"goal_scan": "目标结算/提醒扫描",
                       "workflow_autorun": "工作链推进"}
        _ln = _loop_names.get(str(data.get("loop") or ""),
                              str(data.get("loop") or "?"))
        if data.get("recovered"):
            title = f"✅ 常备循环已恢复：{_ln}"
            text = "**状态**: 心跳恢复跳动\n[📊 查看运营总览](/admin/ops)"
        else:
            _sm = float(data.get("stalled_min") or 0)
            _mounted = data.get("mounted")
            prefix = "⏰" if data.get("reminder") else "🚨"
            _cause = ("循环未挂载（bootstrap 启动失败/旧进程）"
                      if _mounted is False else "心跳停走（事件循环卡死/异常退出）")
            title = f"{prefix} 常备循环停摆：{_ln}（{_sm:.0f} 分钟无心跳）"
            text = (
                f"**判定**: {_cause}\n"
                "**影响**: 目标完成不被发现/提醒不发（goal_scan）或工作链步骤"
                "不推进（workflow_autorun）——都是静默失效，不修没人会替你发现\n"
                "**处置**: 查 app.log 里循环启动行与异常栈；重启实例可临时恢复\n"
                "[📊 查看运营总览](/admin/ops)"
            )

    elif event_type == "goal_miss_alert":
        _n = int(data.get("count") or 0)
        _f = int(data.get("failed") or 0)
        _e = int(data.get("expired") or 0)
        _bt = data.get("by_template") or {}
        _ba = data.get("by_account") or {}
        _bt_txt = "、".join(
            f"{k}×{v}" for k, v in sorted(
                _bt.items(), key=lambda kv: -int(kv[1]))[:4]) or "—"
        _ba_txt = "、".join(
            f"{k}×{v}" for k, v in sorted(
                _ba.items(), key=lambda kv: -int(kv[1]))[:4]) or "—"
        _tri = data.get("triage") or {}
        _tri_txt = ""
        if _tri:
            _tri_txt = (
                f"**三分法**: 已开价未成交 {int(_tri.get('offered') or 0)}"
                f" / 有来有回 {int(_tri.get('engaged') or 0)}"
                f" / 没聊起来 {int(_tri.get('silent') or 0)}\n")
        title = f"📉 目标失守日报：{_n} 个目标未达成（失败 {_f} / 到期 {_e}）"
        text = (
            f"**按类型**: {_bt_txt}\n"
            f"**按账号**: {_ba_txt}\n"
            f"{_tri_txt}"
            "**建议**: 「已开价未成交」先人工复核有没有站外成交漏标（官网单在"
            "站外，系统看不见）；「没聊起来」复盘话术/名单；集中在个别账号则查"
            "该号会话质量\n"
            "[🎯 看这批未达成客户](/workspace/goal-report?status=ended&days=3)"
        )

    elif event_type == "buried_conv_alert":
        if data.get("recovered"):
            title = "✅ 被埋会话已清空"
            text = (
                "**状态**: 归档区里已没有「客户在等」的会话\n"
                "[📥 打开收件箱](/workspace/inbox)"
            )
        else:
            n = int(data.get("buried_count") or 0)
            unread = int(data.get("total_unread") or 0)
            oldest = float(data.get("oldest_hours") or 0)
            auto_n = int(data.get("auto_archived") or 0)
            manual_n = int(data.get("manual_archived") or 0)
            prefix = "⏰" if data.get("reminder") else "🚨"
            oldest_txt = (f"{oldest / 24:.1f} 天" if oldest >= 48 else f"{oldest:.0f} 小时")
            # 人工 vs 自动的处置完全不同，故在正文里分开点名而不是合成一个数字
            src_txt = "、".join(
                x for x in (
                    f"人工归档 {manual_n} 个" if manual_n else "",
                    f"自动归档 {auto_n} 个" if auto_n else "",
                ) if x
            ) or "—"
            title = f"{prefix} {n} 个会话被归档埋掉了（{unread} 条未读没人看见）"
            text = (
                f"**来源**: {src_txt}\n"
                f"**最近活动**: 距今 {oldest_txt}\n"
                "**影响**: 归档会话不出现在任何默认视图里，客户还在发消息但坐席看不到、"
                "也不会有未读提示\n"
                "**处置**: 收件箱「更多 → 归档」逐个确认，还在服务的点「取消归档」；"
                "若多为自动归档，说明策略把活跃会话判死了（调 "
                "`inbox.report.auto_archive.idle_hours` 或关掉 `enabled`）\n"
                "[📥 打开收件箱](/workspace/inbox)"
            )

    elif event_type == "login_funnel_alert":
        _key = str(data.get("key") or "?")
        if data.get("recovered"):
            title = f"✅ 账号接入已恢复：{_key}"
            text = (
                "**状态**: 该平台该接入方式已有账号成功登录\n"
                "[📊 查看运营总览](/admin/ops)"
            )
        else:
            started = int(data.get("started") or 0)
            qr_shown = int(data.get("qr_shown") or 0)
            reasons = data.get("reasons") or {}
            r_txt = "、".join(f"{k}×{v}" for k, v in sorted(
                reasons.items(), key=lambda kv: -int(kv[1] or 0))) or "—"
            prefix = "⏰" if data.get("reminder") else "🚨"
            # 「二维码/登录窗根本没出来」和「出来了但没人扫通」是两种完全不同的故障，
            # 摆在标题下第一行，收到告警的人不必先去翻日志判断该找谁。
            stage = ("登录入口就没起来（连二维码/登录窗都没出现）" if qr_shown == 0
                     else f"登录入口正常（{qr_shown} 次出现）但无人走到授权")
            title = f"{prefix} 账号接不上：{_key} 发起 {started} 次、成功 0 次"
            text = (
                f"**卡在哪**: {stage}\n"
                f"**失败归因**: {r_txt}\n"
                "**怎么判**: 归因集中在 `checkpoint` / `two_factor` / `password_error` "
                "＝账号侧被风控或凭据问题，别查代码；集中在 `service_down` / "
                "`creds_missing` / `session_timeout` ＝接入服务或配置侧\n"
                "[📊 查看接入漏斗](/admin/ops)"
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

    elif event_type == "persona_retired_alert":
        if data.get("recovered"):
            title = "✅ 撤销设定冲突已清理"
            text = (
                "**状态**: 人设档案与撤销清单不再冲突\n"
                "[🎭 人设工作室](/personas)"
            )
        else:
            _pids = data.get("personas") or list((data.get("conflicts") or {}).keys())
            _tot = int(data.get("total") or 0)
            prefix = "⏰" if data.get("reminder") else "🧟"
            title = f"{prefix} 已删除的人设设定被写回（{len(_pids)} 个人设 {_tot} 处）"
            _rows = []
            for _pid, _hits in sorted((data.get("conflicts") or {}).items())[:5]:
                _terms = "、".join(sorted({str(h.get("term") or "") for h in _hits if h.get("term")}))
                _rows.append(f"- `{_pid}`: {_terms}（{len(_hits)} 处）")
            text = (
                "**情况**: 运营删过的设定（retired_facts 锚词）又出现在档案字段里"
                "——多半是批量丰富/直改文件把旧内容写了回来\n"
                + ("\n".join(_rows) + "\n" if _rows else "")
                + "**处置**: 人设工作室 → 该人设 → 预览 → 内容排查，删掉复活内容；"
                "若是有意恢复设定，请先删除对应撤销钉子\n"
                "[🎭 人设工作室](/personas)"
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

    elif event_type == "ai_primary_guard_alert":
        _apg_kind = str(data.get("kind") or "")
        if _apg_kind == "lock_enforced":
            # 老板锁（2026-08-22）：配置被越权改动 → 装载点强制回锁值
            title = "🔒 主链锁生效：越权改档已被强制纠正"
            text = (
                f"**检测**: ai.primary 被改为 {str(data.get('from_mode') or '?')}，"
                f"与 primary_lock={str(data.get('lock') or '?')} 不符\n"
                f"**处置**: 装载时按锁强制生效（现 {str(data.get('effective') or '?')}）"
                "并回写 overlay\n"
                "**追责**: 见实例 logs/ai_primary_audit.jsonl（谁在何时经何途径改的）\n"
                "[📊 查看运营总览](/admin/ops)"
            )
        elif _apg_kind == "lock_rejected":
            title = "🔒 主链切换请求被锁拒绝"
            text = (
                f"**请求**: 切到 {str(data.get('requested') or '?')}"
                f"（操作者: {str(data.get('actor') or '?')}）\n"
                f"**锁**: primary_lock={str(data.get('lock') or '?')}"
                "（老板令 2026-08-22；解除需老板拍板）\n"
                "[📊 查看运营总览](/admin/ops)"
            )
        elif data.get("recovered"):
            title = "✅ 本地主链已恢复可用（仍在 cloud 档）"
            text = (
                "**状态**: 本地端点探测恢复；保险为单向降级，未自动切回\n"
                "**下一步**: 主链档位由 ai.primary_lock 治理（2026-08-22 起锁 cloud），"
                "恢复本地档需老板解锁后经治理接口切换，严禁自动切回\n"
                "[📊 查看运营总览](/admin/ops)"
            )
        else:
            title = "🛟 本地主链保险触发：已热切 cloud"
            text = (
                f"**原档位**: {str(data.get('from_mode') or '?')} → cloud"
                "（单向降级，聊天服务不断）\n"
                f"**端点**: {str(data.get('base_url') or '?')} 连续 "
                f"{int(data.get('fail_count') or 0)} 次探测失败"
                f"（首败至今 ~{int(data.get('down_minutes') or 0)} 分钟）\n"
                "**影响**: 用户内容临时走云端；本地隐私档待人工/执行器恢复\n"
                "**排查**: 173 vLLM（journalctl -u vllm / WSL VM 是否被回收）、"
                "keepwarm 日志（176）\n"
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

    elif event_type == "audit_danger_alert":
        act = str(data.get("action") or "?")
        who = str(data.get("user_id") or "?")
        tgt = str(data.get("target") or "").strip()
        title = f"⚠️ 高危后台操作：{act}（by {who}）"
        lines = [
            f"**操作人**: {who}",
            f"**动作**: `{act}`",
        ]
        if tgt:
            lines.append(f"**对象**: {tgt[:120]}")
        snap = str(data.get("snapshot_id") or "").strip()
        if snap:
            lines.append(f"**快照**: [{snap}](/diff?a={snap}&b=__current__)")
        lines.append("[📋 打开操作记录](/audit?family=danger)")
        text = "\n".join(lines)

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

    elif event_type == "inject_health_alert":
        plat = str(data.get("platform") or "?")
        acct = str(data.get("account_id") or "?")
        down_min = int(data.get("down_minutes") or 0)
        down_txt = (f"{down_min // 60} 小时 {down_min % 60} 分钟"
                    if down_min >= 60 else f"{down_min} 分钟")
        field = str(data.get("field") or "bubble")
        st = str(data.get("status") or "")
        # 元素在但抓不到内容 vs 元素压根找不到，是两种修法：前者改「怎么取值」，
        # 后者改「怎么定位」。文案必须分开，否则运维照着改错地方。
        _WHAT = {
            "mismatch_text": "气泡元素找到了，但一条都取不出文字/媒体",
            "mismatch_ingest": "气泡取到了，但取不到消息 id / 会话 id（回流全丢）",
            "mismatch_composer": "输入框定位不到（无法发送）",
        }
        what = _WHAT.get(st, f"页面结构对不上（{st or '未知'}）")
        miss = [str(x) for x in (data.get("missing_selectors") or [])][:6]
        title = f"🧩 {plat} 网页端选择器失配（已 {down_txt}）"
        text = (
            f"**账号**: {acct}\n"
            f"**症状**: {what}\n"
            f"**待校准字段**: `{field}`"
            + (f"　**缺失选择器**: {', '.join(miss)}" if miss else "") + "\n"
            + ("**注意**: 该账号走的是通用兜底选择器（未命中平台档）\n"
               if data.get("generic") else "")
            + "官方网页端很可能改版了：翻译与消息回流此刻是静默失效的。"
            "请在 `config/desktop_selector_profiles.json` 覆写该字段（热生效、无需重启）\n"
            "[📊 查看运营总览](/admin/ops)"
        )

    elif event_type == "takeover_alert":
        # 人工接管超时（驾驶舱 P0）：接管期间该客户的 AI 是全停的——忘了交还
        # ＝这个客户没人管。文案必须直指动作：去交还或继续处理。
        cid = str(data.get("conversation_id") or "?")
        by = str(data.get("by") or "?")
        el_min = int(data.get("elapsed_min") or 0)
        el_txt = (f"{el_min // 60} 小时 {el_min % 60} 分钟"
                  if el_min >= 60 else f"{el_min} 分钟")
        title = f"🤝 人工接管已持续 {el_txt}，记得交还 AI"
        text = (
            f"**会话**: {cid}\n"
            f"**接管人**: {by}\n"
            "接管期间该会话 AI 完全停发——若已处理完请在工作台会话头点"
            "「交还 AI」；仍在处理可忽略本提醒\n"
            "[📥 打开统一收件箱](/workspace/inbox)"
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
        elif str(data.get("status") or "") == "inbox_stalled":
            # 半死态（P0 2026-08-04）：账号登录在线（掉线文案会误导运维去查登录态），
            # 但侧栏有未读、进线程读取持续全失败＝看得到读不到（E2EE 未解密等）。
            down_min = int(data.get("down_minutes") or 0)
            down_txt = (f"{down_min // 60} 小时 {down_min % 60} 分钟"
                        if down_min >= 60 else f"{down_min} 分钟")
            title = f"🕳️ {plat} 会话半死：在线但读不到新消息（已 {down_txt}）"
            text = (
                f"**账号**: {acct}\n"
                f"**侧栏未读**: {int(data.get('unread') or 0)} 条（一条都没进平台）\n"
                f"**详情**: {str(data.get('detail') or '')[:200]}\n"
                "登录态正常但消息内容读不到——多为 E2EE 设备密钥未随会话恢复，"
                "请在服务器上对该账号完整重新登录（不能只靠 cookie 恢复）\n"
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

    elif event_type == "autosend_gate_alert":
        # #142 件三：真发总闸自动过期恢复（pause_auto_resume_hours 到点）——
        # 群外通知让「闸被自动打开了」这件事有人知道，而不是又一次静默翻转。
        _by = str(data.get("paused_by") or "?")
        _src = str(data.get("paused_source") or "?")
        title = "▶️ 真发总闸已自动恢复"
        text = (
            f"**暂停时长**: {data.get('paused_hours', '?')} 小时（达到配置的自动过期线）\n"
            f"**当时是谁关的**: {_by}（来源 {_src}）\n"
            f"**已恢复开关**: {'、'.join(data.get('paths') or []) or '?'}\n"
            "全自动会话已恢复 AI 自动发送。若这次暂停仍需要，请重新关闭总闸"
            "（会重新计时）。\n"
            "[⚙️ 前往自动回复设置](/workspace/reply-settings)"
        )

    else:
        title = f"未分类事件：{event_type}"
        _kv = "、".join(f"{k}={str(v)[:40]}" for k, v in list(data.items())[:6])
        text = ("该事件暂未做中文说明（请在 _CARD_META / _build_message 补词典）。\n"
                f"原始数据：{_kv or '（空）'}")

    return title, text


# ─── 中文卡片（统一表单式：级别·类别 / 说明 / 来源·时间·原始事件）──────────────
# 每条 Telegram 告警都套这层壳，解决坐席/老板端「看不懂、没分类、没来源」三连：
# 顶部一眼看级别+类别，底部标明来自哪个系统 + 原始事件名（排查用）；正文沿用各
# 事件既有的中文说明（多数已很详细，不重写，只加表头/表尾）。级别六档：
# 🔴 严重 / 🟠 警告 / 🔵 提示 / ✅ 恢复(动态) / 💰 业务 / 📊 日报。
_CARD_META: Dict[str, Tuple[str, str]] = {
    "host_alert": ("🔴 严重", "服务器"),
    "health_alert": ("🔴 严重", "服务器"),
    "scan_loop_stall_alert": ("🔴 严重", "服务器"),
    "platform_session_alert": ("🔴 严重", "链路"),
    "orchestrator_worker_alert": ("🔴 严重", "链路"),
    "autoreply_alert": ("🔴 严重", "链路"),
    "voice_outage_alert": ("🔴 严重", "算力"),
    "realtime_voice_alert": ("🔴 严重", "算力"),
    "human_deliver_alert": ("🔴 严重", "运营"),
    "draft_sla_escalated": ("🔴 严重", "运营"),
    "lan_gpu_alert": ("🟠 警告", "算力"),
    "avatar_voice_alert": ("🟠 警告", "算力"),
    "image_models_alert": ("🟠 警告", "算力"),
    "hub_engine_alert": ("🟠 警告", "算力"),
    "tg_call_alert": ("🟠 警告", "算力"),
    "colloquial_llm_alert": ("🔵 提示", "算力"),
    "ai_primary_guard_alert": ("🟠 警告", "算力"),
    "bot_peer_alert": ("🟠 警告", "链路"),
    "inject_health_alert": ("🟠 警告", "链路"),
    "login_funnel_alert": ("🟠 警告", "链路"),
    "ai_mutual_chat_alert": ("🔵 提示", "链路"),
    "draft_backlog_alert": ("🟠 警告", "运营"),
    "accounts_truth_alert": ("🟠 警告", "运营"),
    "draft_backlog_summary": ("🟠 警告", "运营"),
    "takeover_alert": ("🟠 警告", "运营"),
    "autosend_gate_alert": ("🟠 警告", "运营"),
    "reply_budget_alert": ("🟠 警告", "运营"),
    "agent_quota_alert": ("🟠 警告", "运营"),
    "draft_sla_breach": ("🟠 警告", "运营"),
    "escalation": ("🟠 警告", "运营"),
    "queue_alert": ("🟠 警告", "运营"),
    "case_alert": ("🟠 警告", "运营"),
    "case_backlog_alert": ("🔵 提示", "运营"),
    "buried_conv_alert": ("🟠 警告", "运营"),
    "unanswered_inbound_alert": ("🔵 提示", "运营"),
    "draft_reassigned": ("🔵 提示", "运营"),
    "draft_created": ("🔵 提示", "运营"),
    "draft_resolved": ("🔵 提示", "运营"),
    "anomaly_alert": ("🟠 警告", "运营"),
    "report": ("📊 日报", "运营"),
    "ops_report": ("📊 日报", "运营"),
    "billing_alert": ("🟠 警告", "成本"),
    "human_reply_risk": ("🟠 警告", "安全"),
    "csrf_reject_alert": ("🟠 警告", "安全"),
    "audit_danger_alert": ("🟠 警告", "安全"),
    "csat_alert": ("🟠 警告", "业务"),
    "trial_fulfiller_alert": ("🟠 警告", "业务"),
    "goal_completed_alert": ("💰 业务", "业务"),
    "goal_miss_alert": ("📊 日报", "业务"),
    "cta_clicked": ("💰 业务", "业务"),
    "draft_quality_alert": ("🟠 警告", "质量"),
    "ai_quality_alert": ("🟠 警告", "质量"),
    "media_promise_alert": ("🟠 警告", "质量"),
    "voice_burst_alert": ("🟠 警告", "质量"),
    "persona_retired_alert": ("🔵 提示", "质量"),
    "memory_key_drift_alert": ("🔵 提示", "质量"),
    "proxy_managed_alert": ("🟠 警告", "运营"),
}

_CARD_RULE = "━━━━━━━━━━━━━━"


def _build_card(event_type: str, data: Dict[str, Any], title: str, text: str,
                base_url: str = "") -> str:
    """把已有中文标题+正文套成统一「表单式」卡片（仅 Telegram 等 IM 渠道用）。

    顶部：级别 · 类别（自解释、可扫读）；正文：事件既有中文说明；
    表尾：来源系统 + 时间 + 原始事件名（排查锚点）。不改各事件正文语义。
    """
    sev, cat = _CARD_META.get(event_type, ("🔵 提示", "其它"))
    if data.get("recovered"):
        sev = "✅ 恢复"
    body_title = _plainify(title, base_url)
    body = _plainify(text, base_url)
    ts = time.strftime("%m-%d %H:%M", time.localtime())
    parts = [
        f"{sev} · {cat}",
        _CARD_RULE,
        f"📌 {body_title}" if body_title else "",
        body,
        _CARD_RULE,
        f"🏷️ 来源：承接引擎(智聊)　🕒 {ts}",
        f"🏳️ 原始事件：{event_type}（仅排查用）",
    ]
    return "\n".join(p for p in parts if p).strip()


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
        # 「责任坐席副本」（P2 2026-08-18）：payload 自带 agent_chat_id（目前只有
        # goals.notify 按绑定/开关解析注入）→ 借首个命中的 telegram 渠道的 bot
        # 加发一份到该坐席；每事件至多一份，管理员渠道那份照发不受影响。
        agent_copy_pending = str(data.get("agent_chat_id") or "").strip()
        # 「逐目标点名收件人」（P3 2026-08-18）：payload 自带 extra_chat_ids
        # （goals params.notify_extra 解析产物）→ 与坐席副本同机制借首个命中的
        # telegram 渠道逐个加发；每事件至多一轮，与渠道主号/坐席副本去重。
        extra_copies_pending = [
            str(x or "").strip()
            for x in (data.get("extra_chat_ids") or [])
            if str(x or "").strip()][:5]

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
            if ((agent_copy_pending or extra_copies_pending)
                    and m["fmt"] == "telegram"):
                sent_to = {str(m.get("target") or "")}
                if agent_copy_pending:
                    if agent_copy_pending not in sent_to:
                        # 与渠道同号（老板自己建的目标）＝渠道那份已覆盖，跳过防双推
                        await self._send(
                            {**m, "name": m["name"] + "+agent",
                             "target": agent_copy_pending},
                            etype, data)
                        sent_to.add(agent_copy_pending)
                    agent_copy_pending = ""   # 每事件只发一份坐席副本
                for chat in extra_copies_pending:
                    if chat in sent_to:
                        continue
                    await self._send(
                        {**m, "name": m["name"] + "+extra", "target": chat},
                        etype, data)
                    sent_to.add(chat)
                extra_copies_pending = []     # 每事件只发一轮点名副本

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
                self.last_error = "missing url/token"
                logger.warning("Webhook 跳过 [%s]：%s 渠道缺少 url/token",
                               matcher["name"], fmt)
                return
            if not target:
                self.total_errors += 1
                self.last_error = "missing target/chat_id"
                logger.warning("Webhook 跳过 [%s]：%s 渠道缺少 target",
                               matcher["name"], fmt)
                return
            # Telegram 套统一中文卡片（级别·类别 / 说明 / 来源·时间·原始事件）；
            # 其余 IM（whatsapp/messenger）保持纯文本拼接。
            if fmt == "telegram":
                msg = _build_card(etype, data, title, text, matcher.get("base_url", ""))
            else:
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
            # 失败现场（B24 2026-08-21）：send_test 据此把真实原因带回给面板，
            # 终结「测试失败：HTTP 200」这类甩状态码的报错
            self.last_error = str(exc)[:300]
            logger.warning("Webhook 发送失败 [%s]: %s", matcher["name"], exc)

    @staticmethod
    def _http_post(url: str, body: bytes, headers: Optional[Dict[str, str]] = None) -> None:
        """同步 HTTP POST（在 executor 中运行，不阻塞事件循环）。

        HTTPError 富化（B24 2026-08-21）：Telegram Bot API 的失败原因在响应体
        ``description`` 字段（如 "Bad Request: chat not found"），urllib 的
        HTTPError 消息只有 "HTTP Error 400"——不读体就永远只能对用户甩状态码。
        """
        req = urllib.request.Request(
            url,
            data=body,
            headers=headers or {"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                resp.read()
        except urllib.error.HTTPError as e:
            desc = ""
            try:
                raw = e.read()
                desc = str(json.loads(raw.decode("utf-8", "replace"))
                           .get("description") or "")[:200]
            except Exception:
                pass
            raise RuntimeError(
                f"HTTP {e.code}" + (f": {desc}" if desc else f" {e.reason}")
            ) from None

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
        self.last_error = ""
        await self._send(m, etype, payload)
        ok = self.total_errors == before_err
        out: Dict[str, Any] = {"ok": ok, "name": m["name"], "format": m["fmt"]}
        if not ok:
            # 原始失败原因（路由层负责译成人话；B24：绝不让面板只看到状态码）
            out["error_raw"] = str(getattr(self, "last_error", "") or "")
        return out

    # ── 状态快照 ──────────────────────────────────────────────────────────

    def status_snapshot(self) -> Dict[str, Any]:
        snap = {
            "running": self._running,
            "webhooks": len(self._webhooks),
            "matchers": len(self._matchers),
            "total_sent": self.total_sent,
            "total_errors": self.total_errors,
        }
        try:
            # 非密钥字段指纹：供告警链路自检比对「进程装载的配置 vs 磁盘文件真相」
            # （手改文件不经面板 → 不会热更，这里一比即现形）。软失败不阻断快照。
            from src.integrations.alert_link_audit import config_fingerprint
            snap["config_fp"] = config_fingerprint(self._webhooks)
        except Exception:
            pass
        return snap
