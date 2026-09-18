"""告警投递端到端回归（E9）。

现有测试分两段、各自 mock 了中间层：
  - health_watchdog 测试 mock 了 EventBus，只断言"发了 *_alert 事件"；
  - webhook_notifier 测试只覆盖匹配/格式化/限流，未驱动 run() 真订阅真投递。

本文件补上**整条链**：真 EventBus → WebhookNotifier.run() 真订阅 → publish →
_dispatch → 真 _http_post。覆盖两条新告警（draft_quality / memory_key_drift），
并以 watchdog 驱动跑一遍全栈，确保"告警发了就一定投得出去"，而非"发了没人收"。

为什么值得：notifier 靠 _EVENT_ALIASES 把别名映到 event_type，若哪天别名漏了、
或 run()/_dispatch 链路改坏，单段测试都不会红——只有端到端能抓到"静默丢告警"。
"""

from __future__ import annotations

import asyncio
import json
import types

import pytest

from src.inbox.webhook_notifier import WebhookNotifier


# ── 共享脚手架 ────────────────────────────────────────────────────────────────
# 注：本文件 full_stack 用例会往全局 MetricsStore 单例灌 draft 指标，其前后隔离
# 已由 conftest 的 autouse _reset_metrics_store_singleton 统一兜底，无需本地 fixture。


def _reset_bus():
    """重置 EventBus 单例，隔离用例（notifier.run 与 publish 共用此单例）。"""
    from src.integrations.shared import event_bus as eb
    eb._bus = None
    return eb.get_event_bus()


def _patch_http(monkeypatch):
    """把真 HTTP POST 换成内存捕获（保留 _send→_http_post 整条投递路径）。"""
    captured: list = []

    def _fake_post(url, body, headers=None):
        captured.append({
            "url": url,
            "body": body.decode("utf-8") if isinstance(body, (bytes, bytearray)) else body,
            "headers": headers or {},
        })

    monkeypatch.setattr(WebhookNotifier, "_http_post", staticmethod(_fake_post))
    return captured


async def _drain_until(captured, n=1, timeout=2.0):
    """轮询等待捕获到至少 n 条投递（_http_post 在 executor 线程里回填）。"""
    deadline = asyncio.get_event_loop().time() + timeout
    while len(captured) < n and asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(0.02)
    return captured


async def _run_notifier(events):
    """起一个订阅了 ``events`` 的 json notifier，返回 (notifier, task)。"""
    notifier = WebhookNotifier([{
        "name": "ops-json", "format": "json",
        "url": "https://example.test/hook", "events": events,
    }])
    task = asyncio.create_task(notifier.run())
    await asyncio.sleep(0.05)  # 让 run() 先完成 bus.subscribe()
    return notifier, task


async def _stop(notifier, task):
    # 直接 cancel：中断 run() 里 wait_for(queue.get(), 2.0) 的阻塞（finally 仍会
    # bus.unsubscribe），避免每例空等一个超时周期——把整文件耗时压到秒级。
    notifier.stop()
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, asyncio.TimeoutError):
        pass


# ── Level A：直接 publish 告警 → 投递 ─────────────────────────────────────────

@pytest.mark.parametrize("alias,etype,needle", [
    ("draft_quality", "draft_quality_alert", "草稿质量告警"),
    ("memory_key_drift", "memory_key_drift_alert", "记忆 key 漂移告警"),
])
async def test_alert_delivered_end_to_end(monkeypatch, alias, etype, needle):
    bus = _reset_bus()
    captured = _patch_http(monkeypatch)
    notifier, task = await _run_notifier([alias])
    try:
        bus.publish(etype, {
            "light": "red",
            "problems": [{"id": "x", "name": "测试项", "detail": "细节"}],
        })
        await _drain_until(captured, 1)
    finally:
        await _stop(notifier, task)

    assert captured, f"{etype} 应经 webhook 投递（真链路）"
    payload = json.loads(captured[0]["body"])
    assert needle in payload["title"]
    assert captured[0]["url"] == "https://example.test/hook"


async def test_all_alias_catches_new_alert_types(monkeypatch):
    """events:["all"] 的渠道应同时收到两类新告警（防别名遗漏导致静默丢失）。"""
    bus = _reset_bus()
    captured = _patch_http(monkeypatch)
    notifier, task = await _run_notifier(["all"])
    try:
        bus.publish("draft_quality_alert", {"light": "yellow", "problems": []})
        bus.publish("memory_key_drift_alert", {"light": "yellow", "problems": []})
        await _drain_until(captured, 2)
    finally:
        await _stop(notifier, task)

    titles = [json.loads(c["body"])["title"] for c in captured]
    assert any("草稿质量" in t for t in titles)
    assert any("记忆 key 漂移" in t for t in titles)


# 代码里真正会 publish 的告警 → (订阅别名, event_type, 样本 payload)。
# 来源（rg 'publish("...")' 发布点）：
#   escalation       → unified_inbox_workflow_routes
#   autoreply_alert  → protocol_autoreply
#   draft_sla_breach → sla_watcher（别名 sla_breach）
#   csat_alert/anomaly_alert/queue_alert → scheduled_reporter（anomaly_alert 别名 anomaly）
#   billing_alert/draft_quality_alert/memory_key_drift_alert/health_alert → health_watchdog
#   human_reply_risk → drafts（别名 reply_risk）
# 新增发布点务必同步维护此表 + _EVENT_ALIASES + _build_message（守卫测试会兜底）。
_EMITTED_ALERTS = [
    ("escalation", "escalation",
     {"name": "客户A", "wait_sec": 600, "reason": "无人认领"}),
    ("autoreply_alert", "autoreply_alert",
     {"kind": "circuit_open", "platform": "telegram", "account_id": "a1", "detail": "连续失败"}),
    ("sla_breach", "draft_sla_breach",
     {"autopilot_level": "L3", "wait_min": 30, "sla_hours": 4, "platform": "telegram"}),
    ("csat_alert", "csat_alert",
     {"condition": "low_csat", "message": "CSAT 跌破", "threshold": 3.0}),
    ("anomaly", "anomaly_alert",
     {"anomaly_count": 1, "anomalies": [{"label": "草稿量", "metric": "drafts",
      "current_fmt": "5", "baseline_fmt": "50", "deviation_score": 3.2, "direction": "down"}]}),
    ("queue_alert", "queue_alert",
     {"display_name": "客户B", "platform": "whatsapp", "wait_min": 12.5,
      "sla_level": "crit", "to_agent_id": "u9"}),
    ("billing_alert", "billing_alert", {"anomalies": [{"message": "超席位"}]}),
    ("health_alert", "health_alert",
     {"light": "red", "problems": [{"name": "DB", "detail": "连接失败"}]}),
    ("reply_risk", "human_reply_risk",
     {"agent_id": "u1", "risk_level": "high", "risk_reasons": ["辱骂"], "text_preview": "..."}),
    # #160 I-1（2026-09-04 v2）：风险扣稿全放行、只写台账；stop_contact/self_harm 命中
    # 即时推值守群（不拦只报），rate_key 按 account:conv
    ("autosend_shadow", "autosend_shadow_alert",
     {"reason": "stop_contact", "platform": "telegram", "account_id": "a1",
      "conv_key": "6834964252", "draft_id": "inbox:telegram:a1:6834964252",
      "would_hold_level": "L4", "risk_hits": ["stop messaging me"], "stage": "peer",
      "rate_key": "a1:6834964252"}),
    # 实施93c：客户首点 CTA 追踪短链（cta_links.handle_click 首点发布）——
    # 引导模型最热跟进信号，business 受众，webhook 直推老板/运营
    ("cta_click", "cta_clicked",
     {"conversation_id": "telegram:a1:5433982810", "target_id": "t1",
      "target_name": "官网落地页", "token": "abc12345", "ts": 1756000000.0}),
    # 2026-08-18：报障群 AI 值守（bug_intake）——新 P0/P1 工单 + 危机词压制转人工
    ("bug_intake", "bug_intake_alert",
     {"kind": "ticket", "chat_id": "-1004290740529", "ticket_id": 7,
      "severity": "P1", "title": "消息发不出去", "reporter": "张三",
      "report_count": 2, "rate_key": "bug_intake:ticket:-1004290740529"}),
    ("bug_intake", "bug_intake_alert",
     {"kind": "crisis", "chat_id": "-1004290740529", "reporter": "李四",
      "text": "你们是骗子我要退款", "severity": "P0",
      "rate_key": "bug_intake:crisis:-1004290740529"}),
    # 2026-08-19：AI 助手悬浮球报障（assistant_routes /api/assistant/report）
    ("assistant_report", "assistant_report_alert",
     {"ticket_id": 12, "dup": False, "severity": "P2", "reporter": "op1",
      "page": "/workspace", "text": "语音发送按钮点了没反应",
      "rate_key": "assistant:1"}),
    # P0-2：平台会话健康（unified_inbox_account_routes /session-status → 掉线/恢复）
    ("platform_session", "platform_session_alert",
     {"platform": "messenger", "account_id": "10008900", "login_id": "msg_x",
      "status": "needs_login", "detail": "cookies expired", "recovered": False}),
    # 2026-08-14：telegram 手机端退出（编排器拉起撞 SESSION_REVOKED →
    # report_session_transition 上报 logged_out；此前该故障被磨成泛化 unhealthy 静默）
    ("platform_session", "platform_session_alert",
     {"platform": "telegram", "account_id": "8244899900",
      "status": "logged_out", "detail": "[401 SESSION_REVOKED] ...",
      "recovered": False, "rate_key": "telegram:8244899900"}),
    # P0 2026-08-04：入站半死态（health_watchdog._check_inbox_read_stall →
    # 登录在线但侧栏有未读、进线程读取持续全失败＝看得到读不到，E2EE 卡 Loading 实锤）
    ("platform_session", "platform_session_alert",
     {"platform": "messenger", "account_id": "61584070255403",
      "status": "inbox_stalled", "reminder": True, "down_minutes": 45,
      "unread": 5, "detail": "看得到未读(5)却读不到内容：进线程读取连续失败 5/5",
      "rate_key": "messenger:61584070255403:inbox_stall"}),
    # 驾驶舱 P0 2026-08-13：人工接管超时（health_watchdog._check_takeover_overdue →
    # 坐席接管会话后忘交还，接管期该客户 AI 全停＝没人管）
    ("takeover", "takeover_alert",
     {"conversation_id": "telegram:tg1:5433982810", "platform": "telegram",
      "by": "agent01", "elapsed_min": 135,
      "rate_key": "telegram:tg1:5433982810:takeover_remind"}),
    # 一键代理 P2 2026-08-21：托管代理生命周期（health_watchdog._check_proxy_managed
    # → proxy_lifecycle.run_lifecycle_sweep；kind 分 expiring/renew_blocked/
    # renew_unbilled/expired/low_stock 五类，rate_key 按类独立限流窗）
    ("proxy_managed", "proxy_managed_alert",
     {"kind": "renew_blocked", "count": 2, "need_tokens": 600,
      "reasons": {"insufficient": 2},
      "subs": [{"sub_id": "pxs_a", "country": "JP", "kind": "isp",
                "remaining_days": 2}],
      "rate_key": "proxy_managed:renew_blocked"}),
    ("proxy_managed", "proxy_managed_alert",
     {"kind": "expired", "count": 1, "countries": {"JP": 1},
      "subs": [{"sub_id": "pxs_b", "country": "JP", "kind": "isp",
                "remaining_days": 0}],
      "rate_key": "proxy_managed:expired"}),
    ("proxy_managed", "proxy_managed_alert",
     {"kind": "low_stock", "low": {"JP": 0, "US": 1}, "min_stock": 2,
      "rate_key": "proxy_managed:low_stock"}),
    # P4 2026-08-22（老板拍板 JIT 即买即用、零囤货）：上游预付余额=库存水位
    ("proxy_managed", "proxy_managed_alert",
     {"kind": "vendor_balance_low", "balance": 4.5, "min_alert": 10,
      "rate_key": "proxy_managed:vendor_balance"}),
    # P2 2026-08-13：注册表期望在线但 sidecar 无会话（health_watchdog.
    # _check_messenger_not_restored → 重启恢复失败/会话被清，零事件零心跳的盲区对账）
    ("platform_session", "platform_session_alert",
     {"platform": "messenger", "account_id": "10008900",
      "status": "not_restored", "reminder": True, "down_minutes": 35,
      "detail": "注册表期望在线，但 messenger-web 服务器浏览器没有该账号会话",
      "rate_key": "messenger:10008900:not_restored"}),
    # 2026-07-12：主机级关键告警镜像（host_alert.notify_host → EventBus；
    # 云端 Key 失效/云端不可达/余额不足的远程副本，机主不在算力机前也能收到）
    ("host_alert", "host_alert",
     {"title": "云端 Key 异常",
      "message": "deepseek-chat @ api.deepseek.com 的 API Key 不可用或出现问题",
      "rate_key": "keyfail:deepseek-chat @ api.deepseek.com"}),
    # 2026-08-03 案例中心：severity≥2 开案/升级（危机/要人工/怀疑机器人/媒体质疑）
    ("cases", "case_alert",
     {"case_id": "CASE-664288-58396", "source": "human_request", "severity": 2,
      "action": "created", "user_id": "8921664288",
      "last_message": "我要找真人客服！", "conv_ref": "telegram:default:8921664288",
      "rate_key": "case:8921664288"}),
    # P8：带同源历史处置上下文（安抚过又立案——接警开局知道旧招没用）
    ("cases", "case_alert",
     {"case_id": "CASE-664288-58397", "source": "media_complaint", "severity": 2,
      "action": "created", "user_id": "8921664288",
      "last_message": "这照片还是假的吧", "conv_ref": "telegram:default:8921664288",
      "prior_resolutions": {"buckets": {"soothed": 2},
                            "last_closed_ago_hours": 5.0},
      "rate_key": "case:8921664288"}),
    # 案例积压巡检（P4）：立案后无人认领/处理（危机级单独点名）+ 恢复通知
    ("cases", "case_backlog_alert",
     {"urgent_count": 1, "stale_count": 3, "oldest_hours": 6.5,
      "by_source": {"human_request": 2, "crisis": 1, "ai_doubt": 1},
      "min_age_hours": 4, "reminder": False, "rate_key": "case_backlog:remind"}),
    # 媒体档（P5）：单条媒体质疑超龄即报（穿帮风险不等凑数；同事件同别名零新订阅面）
    ("cases", "case_backlog_alert",
     {"urgent_count": 0, "stale_count": 1, "oldest_hours": 5.2,
      "by_source": {"media_complaint": 1}, "min_age_hours": 4,
      "media_stale_count": 1, "media_stale_hours": 4.0,
      "media_oldest_hours": 5.2, "reminder": False,
      "rate_key": "case_backlog:remind"}),
    ("cases", "case_backlog_alert",
     {"recovered": True, "rate_key": "case_backlog:recovered"}),
    # Phase21a：出站媒体承诺频繁未兑现（AI 说发图/语音却撤回 → 信任受损）
    ("media_promise", "media_promise_alert",
     {"net_retracted": 4, "promise_retracted": 6, "promise_fulfilled": 2,
      "voice_fallback": {"7852_unready": 3}, "reminder": False,
      "rate_key": "media_promise:remind"}),
    # 撤销设定复活（2026-08-04：被删的人设设定被批量丰富/直改文件写回档案）
    ("persona_retired", "persona_retired_alert",
     {"conflicts": {"lin_xiaoyu": [{"term": "猫", "path": "context.hobbies[0]"}]},
      "personas": ["lin_xiaoyu"], "total": 1, "reminder": False,
      "rate_key": "persona_retired:remind"}),
    # 原生通话主机持续不可用（MiniCPM-o 176:7860 掉线 → 来电全接不了）
    ("tg_call", "tg_call_alert",
     {"reachable": False, "model_loaded": False, "url": "http://192.168.0.176:7860",
      "error": "connection refused", "down_minutes": 45, "reminder": False,
      "rate_key": "tg_call:remind"}),
    # 试用履约端停摆（厂商机是签发链单点；三种故障各有不同处置，故三个 payload 都过一遍）
    ("trial_fulfiller", "trial_fulfiller_alert",
     {"kind": "stale", "never": False, "heartbeat_min": 47, "pending": 3,
      "backlog_min": 52, "site": "https://bd2026.cc", "down_minutes": 30,
      "reminder": False, "rate_key": "trial_fulfiller:remind"}),
    ("trial_fulfiller", "trial_fulfiller_alert",
     {"kind": "stuck", "never": False, "heartbeat_min": 2, "pending": 5,
      "backlog_min": 41, "site": "https://bd2026.cc", "down_minutes": 20,
      "reminder": True, "rate_key": "trial_fulfiller:remind"}),
    ("trial_fulfiller", "trial_fulfiller_alert",
     {"kind": "unreachable", "never": False, "heartbeat_min": -1, "pending": -1,
      "backlog_min": -1, "site": "https://bd2026.cc", "down_minutes": 18,
      "reminder": False, "rate_key": "trial_fulfiller:remind"}),
    # 待审草稿积压（补 SLA 的 L1 盲区）：告警态 + 恢复态各过一遍
    ("draft_backlog", "draft_backlog_alert",
     {"stale_count": 6, "min_age_hours": 24, "oldest_hours": 214.1,
      "by_level": {"L1": 5, "L3": 1}, "sla_uncovered": 5,
      "reminder": False, "rate_key": "draft_backlog:remind"}),
    ("draft_backlog", "draft_backlog_alert",
     {"recovered": True, "rate_key": "draft_backlog:recovered"}),
    # 系统标签泄漏（2026-09-12「[我方语音消息]」事故）：告警态 + 恢复态
    ("label_leak", "label_leak_alert",
     {"count": 4, "system_label": 4, "bracket_prefix": 0, "conversations": 1,
      "top_conversations": [["whatsapp:639270135480:639273815533", 4]],
      "sample_tags": ["[我方发出的语音]", "[Voice message from our side]", "[我方语音消息]"],
      "lookback_hours": 24, "latest_ts": 1789217241.0,
      "latest_text": "[我方语音消息] 天哪，我这边说英语。", "latest_media": "",
      "reminder": False, "unchanged": False, "rate_key": "label_leak:remind"}),
    ("label_leak", "label_leak_alert",
     {"recovered": True, "lookback_hours": 24, "rate_key": "label_leak:recovered"}),
    # 前端脚本 bug（2026-09-15 `_psnArRender` 人设工坊 3.5 天不可用零告警）：告警态 + 恢复态
    ("frontend_error", "frontend_error_alert",
     {"symbols": 1, "hits": 7, "pages": ["/personas"],
      "top": [["/personas ReferenceError _psnArRender", 7]],
      "last_ts": 1789473436.0, "min_count": 2,
      "reminder": False, "unchanged": False, "rate_key": "frontend_error:remind"}),
    ("frontend_error", "frontend_error_alert",
     {"recovered": True, "quiet_hours": 12, "rate_key": "frontend_error:recovered"}),
    ("accounts_truth", "accounts_truth_alert",
     {"ghost_count": 2, "samples": ["telegram:leak1", "whatsapp:leak2"],
      "reminder": False, "rate_key": "accounts_truth:remind"}),
    ("accounts_truth", "accounts_truth_alert",
     {"recovered": True, "rate_key": "accounts_truth:recovered"}),
    # 回复额度触顶聚合（peer_bot_guard P1）：多会话同日烧穿预算＝系统性状况；
    # 告警态（含 near 提前量 + 用量样本）+ 恢复态各过一遍
    ("reply_budget", "reply_budget_alert",
     {"exhausted_count": 3, "hard_count": 1, "near_count": 2,
      "budget_limit": 40,
      "samples": [{"title": "无界科技 BOUNDELESS", "used": 74},
                  {"title": "阿龙", "used": 41}],
      "reminder": False, "rate_key": "reply_budget:remind"}),
    ("reply_budget", "reply_budget_alert",
     {"recovered": True, "rate_key": "reply_budget:recovered"}),
    # 坐席字符额度水位聚合（2026-08-16 health_watchdog._check_agent_quota）：
    # warn（达用户行 quota_alert_pct 提醒线）/ over（超额）两桶；enforce 硬闸
    # 默认关（软提醒先行），这条告警是软提醒期的唯一主动信号。告警态 + 恢复态各过一遍
    ("agent_quota", "agent_quota_alert",
     {"month": "2026-08", "warn_count": 1, "over_count": 1,
      "warn": [{"username": "zuoxi02", "used": 8400, "quota": 10000, "pct": 84}],
      "over": [{"username": "zuoxi01", "used": 12050, "quota": 10000, "pct": 121}],
      "reminder": False, "rate_key": "agent_quota:remind"}),
    ("agent_quota", "agent_quota_alert",
     {"recovered": True, "rate_key": "agent_quota:recovered"}),
    # AI 对聊提醒（P0-6 2026-08-09 加的发布点：health_watchdog._check_mutual_chat
    # 聚合一条、绝不拦截）。formatter/别名/严重度表当时全齐、唯漏本表——完整性
    # 门禁红了两天成「无主既有红」，2026-08-12 补登记。payload 形状与真实
    # publish 一致（conversations 条目=mutual_chat_monitor 扫描行，formatter 消费
    # conversation_id/n_in/n_out/managed_peer）。
    ("ai_mutual_chat", "ai_mutual_chat_alert",
     {"conversations": [
         {"conversation_id": "telegram:8438080491:7654321098",
          "n_in": 66, "n_out": 61, "managed_peer": True},
         {"conversation_id": "telegram:7654321098:8438080491",
          "n_in": 61, "n_out": 66, "managed_peer": False}],
      "window_hours": 24, "rate_key": "ai_mutual_chat"}),
    # 被埋会话（归档着却有未读＝客户在等而工作台看不见）：告警态 + 恢复态各过一遍
    ("buried_conv", "buried_conv_alert",
     {"buried_count": 2, "total_unread": 7, "oldest_hours": 51.2,
      "auto_archived": 0, "manual_archived": 2,
      "samples": ["telegram:acctA:c1", "telegram:acctA:c2"],
      "reminder": False, "rate_key": "buried_conv:remind"}),
    ("buried_conv", "buried_conv_alert",
     {"recovered": True, "rate_key": "buried_conv:recovered"}),
    # 幽灵未读（#159 / #120 族：账号栏徽标口径与旧全库口径的差额）：告警 + 恢复
    ("phantom_unread", "phantom_unread_alert",
     {"phantom": 7, "badge_total": 3, "store_total": 10,
      "worst_accounts": [{"account": "telegram:acctA", "phantom": 5},
                         {"account": "whatsapp:acctB", "phantom": 2}],
      "reminder": False, "rate_key": "phantom_unread:remind"}),
    ("phantom_unread", "phantom_unread_alert",
     {"recovered": True, "rate_key": "phantom_unread:recovered"}),
    # 账号接入链路停摆（某 platform:mode 发起 N 次成功 0 次）：告警 + 恢复
    ("login_funnel", "login_funnel_alert",
     {"key": "messenger:web", "platform": "messenger", "mode": "web",
      "started": 7, "qr_shown": 7, "failed": 6,
      "reasons": {"checkpoint": 4, "session_timeout": 2},
      "reminder": False, "rate_key": "login_funnel:messenger:web"}),
    ("login_funnel", "login_funnel_alert",
     {"recovered": True, "key": "messenger:web",
      "rate_key": "login_funnel:messenger:web:recovered"}),
    # CSRF 写请求拦截激增（P1 2026-07-31：前端宿主写通道断裂/跨站探测）：告警+恢复
    ("csrf_reject", "csrf_reject_alert",
     {"count": 7, "window_min": 60, "total": 12,
      "by_kind": {"cookie_no_header": 6, "bare": 1},
      "top_paths": {"/api/persona/bind": 5, "/api/unified-inbox/send": 2},
      "reminder": False, "rate_key": "csrf_reject:remind"}),
    ("csrf_reject", "csrf_reject_alert",
     {"recovered": True, "rate_key": "csrf_reject:recovered"}),
    # 2026-07-30：补齐此前漂移出表的 9 个真实发布点（health_watchdog / voice_burst_guard
    # 都真会 publish，却从没在这张 e2e 投递表里）。payload 触发各自「告警态」分支
    # （非 recovered），_build_message 全程 .get 兜底故最小字段即可送达+出标题。
    # 完整性由 test_source_alerts_all_have_e2e_payload 钉死（源码 *_alert == 本表 *_alert）。
    ("draft_quality", "draft_quality_alert",
     {"light": "yellow", "problems": [{"name": "记忆命中率", "detail": "偏低"}]}),
    ("ai_quality", "ai_quality_alert",
     {"light": "yellow", "problems": [{"name": "草稿采纳率", "detail": "偏低"}]}),
    ("realtime_voice", "realtime_voice_alert",
     {"light": "yellow", "problems": [{"name": "接通率", "detail": "偏低"}]}),
    ("human_deliver", "human_deliver_alert",
     {"human_approved": 3, "bad_minutes": 45, "reminder": False,
      "rate_key": "human_deliver:remind"}),
    ("avatar_voice", "avatar_voice_alert",
     {"down_minutes": 45, "reminder": False, "rate_key": "avatar_voice:remind"}),
    # hub 引擎目录离线（2026-08-22 顶包事故）：lenient=换声 / strict=拒发 / 恢复
    ("hub_engine", "hub_engine_alert",
     {"engine": "index_tts", "url": "http://192.168.0.176:9000", "strict": False,
      "available_engines": ["fish_speech", "moss_ttsd"], "down_minutes": 35,
      "reminder": False, "rate_key": "hub_engine:remind"}),
    ("hub_engine", "hub_engine_alert",
     {"engine": "index_tts", "strict": True, "down_minutes": 200,
      "reminder": True, "rate_key": "hub_engine:remind"}),
    ("hub_engine", "hub_engine_alert",
     {"recovered": True, "engine": "index_tts",
      "rate_key": "hub_engine:recovered"}),
    # 出图模型失踪（2026-08-22 模型被清空事故防再犯）：告警态 + 恢复态
    ("image_models", "image_models_alert",
     {"url": "http://192.168.0.176:8188", "ckpts": 0, "unets": 0,
      "reminder": False, "rate_key": "image_models:remind"}),
    ("image_models", "image_models_alert",
     {"recovered": True, "url": "http://192.168.0.176:8188",
      "rate_key": "image_models:recovered"}),
    # LAN GPU 主机整机下线（2026-08-01 176 静默宕机两小时实锤）：告警态 + 恢复态
    ("lan_gpu", "lan_gpu_alert",
     {"host": "192.168.0.176:11434", "url": "http://192.168.0.176:11434",
      "error": "timed out", "down_minutes": 35, "reminder": False,
      "rate_key": "lan_gpu:192.168.0.176:11434"}),
    ("lan_gpu", "lan_gpu_alert",
     {"recovered": True, "host": "192.168.0.176:11434",
      "url": "http://192.168.0.176:11434",
      "rate_key": "lan_gpu:192.168.0.176:11434:recovered"}),
    ("compute_lane", "compute_lane_alert",
     {"lane": "cloud", "label": "DeepSeek 官方", "kind": "quota",
      "kind_zh": "没有费用 / 余额不可用", "detail": "余额为 0",
      "standins": ["173 本地 vLLM"], "down_minutes": 6, "reminder": False,
      "remind_key": "compute_lane:cloud",
      "rate_key": "compute_lane:cloud:0"}),
    ("compute_lane", "compute_lane_alert",
     {"recovered": True, "lane": "cloud", "label": "DeepSeek 官方",
      "rate_key": "compute_lane:cloud:recovered"}),
    ("colloquial_llm", "colloquial_llm_alert",
     {"down_minutes": 45, "fail_streak": 5, "reminder": False,
      "rate_key": "colloquial_llm:remind"}),
    # 本地主链保险（2026-08-15）：local* 档 vLLM 连续探测失败 → 单向热切 cloud
    ("ai_primary_guard", "ai_primary_guard_alert",
     {"from_mode": "local_only", "base_url": "http://192.168.0.173:8001/v1",
      "fail_count": 2, "down_minutes": 5,
      "rate_key": "ai_primary_guard:switched"}),
    # 老板锁（2026-08-22）：配置被越权改动 → 装载点强制回锁值（ai_client 发布）
    ("ai_primary_guard", "ai_primary_guard_alert",
     {"kind": "lock_enforced", "from_mode": "local_only", "lock": "cloud",
      "effective": "cloud", "rate_key": "ai_primary_guard:lock"}),
    # 老板锁（2026-08-22）：治理接口拒绝与锁不符的切换请求（setup 路由发布）
    ("ai_primary_guard", "ai_primary_guard_alert",
     {"kind": "lock_rejected", "requested": "local_only", "lock": "cloud",
      "actor": "user:admin", "rate_key": "ai_primary_guard:lock"}),
    # 装载点切档通知（2026-09-17）：overlay/接口任一途径改档，运维群都要收到
    ("ai_primary_guard", "ai_primary_guard_alert",
     {"kind": "mode_switched", "from_mode": "cloud", "to_mode": "local",
      "lock_from": "cloud", "lock": "local", "effective": "local",
      "primary_text": "本地 vLLM chatx（档位 local，锁 local）",
      "chain_text": "本地 LAN .173 vLLM chatx → 回落 deepseek-official deepseek-chat → canned",
      "mode_label": "本地主链（可回落云端）", "via": "ai_client_init",
      "rate_key": "ai_primary_guard:switched:local:local"}),
    # 入站漏球（P0 2026-08-05：客户最后一句既没被回也没拟稿——draft_backlog 只看
    # 「有稿没人处理」，「压根没稿」此前零信号）：告警态 + 恢复态各过一遍
    ("unanswered_inbound", "unanswered_inbound_alert",
     {"count": 1, "min_age_hours": 2, "oldest_hours": 8.7,
      "samples": [{"conversation_id": "telegram:8244899900:8921664288",
                   "platform": "telegram", "account_id": "8244899900",
                   "age_hours": 8.7}],
      "reminder": False, "rate_key": "unanswered_inbound:remind"}),
    ("unanswered_inbound", "unanswered_inbound_alert",
     {"recovered": True, "rate_key": "unanswered_inbound:recovered"}),
    ("orchestrator_worker", "orchestrator_worker_alert",
     {"light": "yellow", "problems": [{"name": "worker", "detail": "掉线"}]}),
    ("memory_key_drift", "memory_key_drift_alert",
     {"light": "yellow", "problems": [{"name": "key 漂移", "detail": "检测到"}]}),
    ("voice_burst", "voice_burst_alert",
     {"chat_id": "123456", "count": 3, "window_sec": 10}),
    ("audit_danger", "audit_danger_alert",
     {"user_id": "admin", "action": "episodic_bulk_delete", "target": "test",
      "old_val": "sample", "snapshot_id": "",
      "rate_key": "audit_danger:admin:episodic_bulk_delete"}),
    # 语音出站断档（2026-08-02：hub 音色档 404 → 低流量下语音全失败 5 天零告警）：
    # 告警态 + 恢复态各过一遍
    ("voice_outage", "voice_outage_alert",
     {"attempts": 4, "window_hours": 24, "consecutive_fails": 4,
      "top_reasons": {"synth_failed": 3, "edge_rejected": 1},
      "last_ok_hours": 121.5, "by_source": {"aline": {"attempts": 3, "ok": 0}},
      "reminder": False, "rate_key": "voice_outage:remind"}),
    ("voice_outage", "voice_outage_alert",
     {"recovered": True, "rate_key": "voice_outage:recovered"}),
    # 对方机器人守卫（P0 2026-08-03：主动触达问候 @SpamBot → 80 秒 8 轮空转实锤）：
    # 确定级检出降 manual + 灰区熔断降 review 两种形态各过一遍
    ("bot_peer", "bot_peer_alert",
     {"conversation_id": "telegram:8438080491:178220800", "platform": "telegram",
      "display_name": "Spam Info Bot", "username": "SpamBot",
      "reason": "tg_username_bot", "evidence": "@SpamBot",
      "action": "automation_mode→manual",
      "rate_key": "bot_peer:telegram:8438080491:178220800"}),
    ("bot_peer", "bot_peer_alert",
     {"conversation_id": "telegram:8244899900:5433982810", "platform": "telegram",
      "display_name": "AI 智控王", "username": "ai_zkw",
      "reason": "inbound_repeat", "evidence": "连续5条相同: '不行，给我你的照片'",
      "action": "automation_mode→review",
      "rate_key": "bot_peer:telegram:8244899900:5433982810"}),
    # 目标达成（P0 2026-08-09：goals.notify 扫描器——settle/订单回流/人工成交
    # 三条完成路径同一扫描覆盖；好消息逐条即时推）：订单成交 + 信号结算两种形态
    ("goal_complete", "goal_completed_alert",
     {"goal_id": "g1", "conversation_id": "telegram:8244899900:8921664288",
      "platform": "telegram", "account_id": "8244899900",
      "chat_key": "8921664288", "contact_name": "小美",
      "template": "conversion_subscribe", "template_name": "会员订阅",
      "title": "会员订阅", "result": "order:pro:ORD-1", "result_kind": "order",
      "won": True, "amount": 199, "product": "pro", "days_to_done": 3.5,
      "done_at": 1754700000.0, "rate_key": "goal_done:8244899900",
      # P3 2026-08-18：摸底要点（include_profile opt-in）+ 逐目标点名收件人
      # （params.notify_extra 解析产物；telegram 渠道逐个加发副本）
      "slots_brief": "年龄:28岁｜业务痛点:客服人手",
      "extra_chat_ids": ["7770001"]}),
    ("goal_complete", "goal_completed_alert",
     {"goal_id": "g2", "conversation_id": "telegram:8244899900:5433982810",
      "platform": "telegram", "account_id": "8244899900",
      "chat_key": "5433982810", "contact_name": "",
      "template": "engagement_reactivate", "template_name": "沉默唤回",
      "title": "沉默唤回", "result": "replied", "result_kind": "auto",
      "won": False, "amount": None, "product": "", "days_to_done": 1.2,
      "done_at": 1754700000.0, "rate_key": "goal_done:8244899900"}),
    # 目标失守日报（P1 2026-08-09：failed/expired 聚合一条，攒够条数/压龄才出账）
    ("goal_miss", "goal_miss_alert",
     {"count": 4, "failed": 1, "expired": 3,
      "by_template": {"付费解锁": 2, "会员订阅": 2},
      "by_account": {"telegram:8244899900": 3, "telegram:8438080491": 1},
      "triage": {"offered": 2, "engaged": 1, "silent": 1},
      "oldest_hours": 26.5, "window_hours": 72,
      "rate_key": "goal_miss:digest"}),
    # 常备扫描循环停摆（P4 2026-08-09：心跳停走/未挂载 + 恢复通知各过一遍）
    ("scan_stall", "scan_loop_stall_alert",
     {"loop": "goal_scan", "stalled_min": 12.3, "ticks": 42,
      "last_tick_ts": 1786246575.0, "mounted": True, "reminder": False,
      "rate_key": "scan_stall:goal_scan"}),
    ("scan_stall", "scan_loop_stall_alert",
     {"loop": "workflow_autorun", "recovered": True,
      "rate_key": "scan_stall:workflow_autorun:recovered"}),
    # D1b P0-5（2026-09-05）：冲刺推进器 / care 派发循环进同一张停摆表；
    # running=False = asyncio task 崩了（与「慢」区分）
    ("scan_stall", "scan_loop_stall_alert",
     {"loop": "goal_sprint", "stalled_min": 31.0, "ticks": 7,
      "last_tick_ts": 1786246575.0, "mounted": True, "running": False,
      "reminder": False, "rate_key": "scan_stall:goal_sprint"}),
    ("scan_stall", "scan_loop_stall_alert",
     {"loop": "care_dispatch", "stalled_min": 12.0, "ticks": 3,
      "last_tick_ts": 1786246575.0, "mounted": True, "running": True,
      "reminder": False, "rate_key": "scan_stall:care_dispatch"}),
    ("scan_stall", "scan_loop_stall_alert",
     {"loop": "goal_sprint_sends", "active_auto": 3, "sent_24h": 0,
      "oldest_hours": 8.5, "reminder": False,
      "rate_key": "scan_stall:goal_sprint_sends"}),
    ("scan_stall", "scan_loop_stall_alert",
     {"loop": "goal_sprint_sends", "recovered": True,
      "rate_key": "scan_stall:goal_sprint_sends:recovered"}),
    # M-7 D：单目标 stalled 点名——旧文案套成「常备循环停摆：goal_sprint_goal」
    ("scan_stall", "scan_loop_stall_alert",
     {"loop": "goal_sprint_goal", "goal_id": "52d57b432fab410b",
      "conversation_id": "telegram:8244899900:990001088", "title": "",
      "reminder": False, "rate_key": "scan_stall:goal:52d57b432fab410b"}),
    ("scan_stall", "scan_loop_stall_alert",
     {"loop": "goal_sprint_goal", "recovered": True,
      "rate_key": "scan_stall:goal:52d57b432fab410b:recovered"}),
    # 内嵌网页端选择器失配（2026-08-10）：三类症状文案各不相同（元素找不到 vs
    # 找到了取不出内容 vs 取不到消息 id），全过一遍防某一支渲染成空文案。
    ("inject_health", "inject_health_alert",
     {"platform": "telegram", "account_id": "telegram:8244899900",
      "status": "mismatch_text", "field": "bubbleText", "generic": False,
      "bubbles": 24, "missing_selectors": [], "down_minutes": 35,
      "first_reminder": True, "rate_key": "inject:telegram:8244899900"}),
    ("inject_health", "inject_health_alert",
     {"platform": "whatsapp", "account_id": "wa:1", "status": "mismatch_ingest",
      "field": "mid", "generic": True, "bubbles": 12,
      "missing_selectors": [], "down_minutes": 240, "first_reminder": False,
      "rate_key": "inject:whatsapp:wa:1"}),
    ("inject_health", "inject_health_alert",
     {"platform": "telegram", "account_id": "telegram:8438080491",
      "status": "mismatch_composer", "field": "composer", "generic": False,
      "bubbles": 8, "missing_selectors": ["composer", "sendBtn"],
      "down_minutes": 22, "first_reminder": True,
      "rate_key": "inject:telegram:8438080491"}),
    # #142 件三（2026-09-02）：真发总闸关满 pause_auto_resume_hours 自动恢复
    # （autosend_gate_state.sweep_gate_auto_resume 发布）——「闸被自动打开了」群外可见
    ("autosend_gate", "autosend_gate_alert",
     {"action": "auto_resumed",
      "paths": ["inbox.l2_autosend.enabled"],
      "paused_hours": 6.5, "paused_by": "admin", "paused_source": "preset",
      "rate_key": "autosend_gate:auto_resume"}),
]


@pytest.mark.parametrize("alias,etype,payload", _EMITTED_ALERTS)
async def test_each_emitted_alert_delivers(monkeypatch, alias, etype, payload):
    """每一类「代码里真会发」的告警都能经 webhook 投递（整链，非 mock）。"""
    bus = _reset_bus()
    captured = _patch_http(monkeypatch)
    notifier, task = await _run_notifier([alias])
    try:
        bus.publish(etype, payload)
        await _drain_until(captured, 1)
    finally:
        await _stop(notifier, task)
    assert captured, f"{etype} 应经 webhook 投递"
    body = json.loads(captured[0]["body"])
    # 不得落通用兜底「[etype] 事件」——那等于投出去一坨没人看得懂的 JSON。
    assert body["title"] != f"[{etype}] 事件", f"{etype} 落了通用兜底，缺 _build_message 分支"


def test_emitted_alerts_have_readable_messages():
    """系统性守卫：凡真发布的告警，别名映射正确 + _build_message 有专属分支（不落通用兜底）。

    与上面 e2e 互补——本测试纯同步、零网络，CI 快速失败定位「哪类告警没格式化/没接别名」。
    """
    from src.inbox.webhook_notifier import _build_message, _EVENT_ALIASES
    missing = []
    for alias, etype, payload in _EMITTED_ALERTS:
        title, _ = _build_message(etype, payload)
        if title == f"[{etype}] 事件":
            missing.append(etype)
        rule = _EVENT_ALIASES.get(alias)
        assert rule is not None, f"别名 {alias} 不在 _EVENT_ALIASES"
        assert rule["types"] is None or etype in rule["types"], \
            f"别名 {alias} 未映射到 {etype}"
    assert not missing, f"以下告警落通用兜底、缺 _build_message 分支: {missing}"


def test_source_alerts_all_have_e2e_payload():
    """完整性门禁：源码每个 publish("*_alert") 都必须在本表有 e2e 投递用例，反向防死条目。

    背景：``_EMITTED_ALERTS`` 曾是纯手工表，2026-07-30 实测漂移 9 个（真发布却没 e2e
    用例）。本门禁复用 ``test_alert_alias_coverage._emitted_alerts`` 作**单一源码扫描器**
    自动跟源码——从此漏登记立刻红，无需人记得同步。非 ``_alert`` 后缀的告警
    （draft_sla_breach/escalation/human_reply_risk 等）发布点不带该后缀、不在扫描器口径内，
    故双向只对 ``*_alert`` 子集（csat/anomaly/queue 恰以 _alert 结尾，纳入）。
    """
    from tests.test_alert_alias_coverage import _emitted_alerts
    source = _emitted_alerts()
    table = {e for _, e, _ in _EMITTED_ALERTS if e.endswith("_alert")}
    missing = sorted(source - table)
    dead = sorted(table - source)
    assert not missing, (
        "以下告警在源码 publish 但没有 e2e 投递用例（手工表漂移）——补进 _EMITTED_ALERTS:"
        "\n  " + "\n  ".join(missing))
    assert not dead, (
        "以下 _EMITTED_ALERTS 条目在源码已无 *_alert 发布点（死条目）——删掉:"
        "\n  " + "\n  ".join(dead))


async def test_unsubscribed_alert_not_delivered(monkeypatch):
    """只订阅 draft_quality 的渠道，不应收到 memory_key_drift（反向隔离）。"""
    bus = _reset_bus()
    captured = _patch_http(monkeypatch)
    notifier, task = await _run_notifier(["draft_quality"])
    try:
        bus.publish("memory_key_drift_alert", {"light": "red", "problems": []})
        await asyncio.sleep(0.3)  # 给足时间，确认确实没有投递
    finally:
        await _stop(notifier, task)
    assert captured == []


# ── Level B：watchdog 驱动的全栈链路 ─────────────────────────────────────────

def _reset_draft_metrics():
    from src.monitoring import metrics_store as _ms
    _ms.MetricsStore._instance = None
    return _ms.get_metrics_store()


class _CM:
    def __init__(self, config):
        self.config = config


def _fake_app():
    state = types.SimpleNamespace()
    state.inbox_store = types.SimpleNamespace(ping=lambda: True)
    state.draft_service = types.SimpleNamespace(
        list_drafts=lambda status="pending", limit=1000: [{} for _ in range(10)])
    return types.SimpleNamespace(state=state)


async def test_full_stack_watchdog_to_webhook(monkeypatch):
    """watchdog._check_draft_quality 发现低命中率 → 真 bus → notifier → 真 _http_post。"""
    from src.inbox.health_watchdog import HealthWatchdog

    bus = _reset_bus()
    captured = _patch_http(monkeypatch)

    m = _reset_draft_metrics()
    for _ in range(30):
        m.record_inbox_draft_event("generated")
    for _ in range(3):  # 命中率 10% < 阈值 30%
        m.record_inbox_draft_event("memory_hit")

    notifier, task = await _run_notifier(["draft_quality"])
    try:
        cm = _CM({"ai": {"provider": "openai", "api_key": "sk-real-123"},
                  "inbox": {"auto_draft": {"quality_alert": {
                      "enabled": True, "min_samples": 10,
                      "memory_hit_min": 0.30, "p95_ms_max": 8000,
                      "fast_path_ratio_max": 0.98}}}})
        wd = HealthWatchdog(app=_fake_app(), config_manager=cm, interval_sec=60)
        wd._check_draft_quality()  # 同步 publish 到真 bus
        assert wd.total_draft_quality_alerts == 1
        await _drain_until(captured, 1)
    finally:
        await _stop(notifier, task)

    assert captured, "watchdog 告警应一路投递到 webhook（全栈）"
    payload = json.loads(captured[0]["body"])
    assert "草稿质量告警" in payload["title"]
    assert any("memory_hit_low" == p.get("id") for p in payload["data"].get("problems", []))
