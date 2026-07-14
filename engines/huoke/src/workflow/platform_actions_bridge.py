# -*- coding: utf-8 -*-
"""
平台动作桥接层 — 将所有平台自动化方法注册为 WorkflowEngine 可调用的 action。

【核心架构决策】
  工作流 Action 函数直接调用自动化层（不经过 WorkerPool/task_store），原因：
  - 工作流已在 WorkerPool 线程中执行，设备锁已持有
  - 避免死锁：不重复申请同设备锁
  - 避免不必要的任务记录开销
  - 支持工作流步骤直接获取返回值用于条件判断

【注册方式】
  在 api.py lifespan 中调用 register_all_platform_actions() 完成注册。
  所有 action 名遵循 "{platform}.{method}" 格式。

【约定】
  - 所有 action 函数都接受 device_id: str 作为第一个关键字参数
  - 函数返回值直接作为步骤结果（dict 或 bool）
  - 失败时抛出异常，由工作流引擎按 on_error 策略处理
"""

from __future__ import annotations

import logging
from typing import Optional

from src.host.device_registry import DEFAULT_DEVICES_YAML

log = logging.getLogger(__name__)

_config_path = DEFAULT_DEVICES_YAML


# ─────────────────────────────────────────────────────────────
# 内部辅助
# ─────────────────────────────────────────────────────────────

def _get_manager():
    from src.device_control.device_manager import get_device_manager
    return get_device_manager(_config_path)


def _fresh_tiktok(device_id: str):
    """创建绑定到指定设备的 TikTok 自动化实例。"""
    from src.app_automation.tiktok import TikTokAutomation
    manager = _get_manager()
    tt = TikTokAutomation(device_manager=manager)
    tt.set_current_device(device_id)
    return tt, manager


def _fresh_telegram(device_id: str):
    from src.app_automation.telegram import TelegramAutomation
    manager = _get_manager()
    tg = TelegramAutomation(manager)
    tg.set_current_device(device_id)
    return tg, manager


def _resolve_account(device_id: str, task_type: str) -> Optional[str]:
    """通过多账号调度器自动选择账号（可选）。"""
    try:
        from src.host.account_scheduler import get_account_scheduler
        sched = get_account_scheduler()
        sched.auto_discover_accounts(device_id, _get_manager())
        return sched.select_account(device_id, task_type)
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────
# ★ TikTok Actions
# ─────────────────────────────────────────────────────────────

def tiktok_warmup_session(
    device_id: str,
    duration_minutes: int = 30,
    phase: str = "auto",
    target_country: str = "italy",
    **kwargs
) -> dict:
    """养号：浏览 For You 页、随机点赞、评论、关注测试。"""
    tt, manager = _fresh_tiktok(device_id)

    # 账号选择
    account = _resolve_account(device_id, "tiktok_warmup")
    if account:
        tt.switch_account(account, device_id)

    # 阶段自动判断
    if phase == "auto":
        try:
            from src.host.device_state import get_device_state_store
            ds = get_device_state_store("tiktok")
            ds.init_device(device_id)
            phase = ds.determine_phase(device_id)
        except Exception:
            phase = "interest_building"

    stats = tt.warmup_session(
        duration_minutes=duration_minutes,
        target_country=target_country,
        phase=phase,
    )

    # 记录到设备状态
    try:
        from src.host.device_state import get_device_state_store
        get_device_state_store("tiktok").record_warmup(device_id, stats)
    except Exception:
        pass

    log.info("[Workflow/TikTok] warmup 完成: device=%s stats=%s", device_id[:8], stats)
    return {"success": True, "warmup_stats": stats, "phase": phase}


def tiktok_smart_follow(
    device_id: str,
    max_follows: int = 15,
    country: str = "italy",
    language: str = "italian",
    gender: str = "",
    min_age: int = 0,
    seed_accounts: Optional[list] = None,
    **kwargs
) -> dict:
    """智能关注：进入种子账号粉丝列表，按条件过滤并关注。"""
    tt, manager = _fresh_tiktok(device_id)

    from src.app_automation.target_filter import TargetProfile
    target = TargetProfile(
        country=country, language=language,
        gender=gender, min_age=min_age,
    )

    from src.leads.follow_tracker import LeadsFollowTracker
    tracker = LeadsFollowTracker()

    # ★ P2-1: 种子账号分层排序（S>A>B，自动截断低质量种子）
    if seed_accounts:
        try:
            from src.host.seed_ranker import rank_seeds
            ranked = rank_seeds(seed_accounts)
            if ranked != seed_accounts:
                log.debug("[Workflow/TikTok] 种子排序: %d→%d个, top=%s",
                          len(seed_accounts), len(ranked), ranked[:2])
            seed_accounts = ranked
        except Exception as _sr_err:
            log.debug("[Workflow/TikTok] 种子排序跳过: %s", _sr_err)

    result = tt.smart_follow(
        target_profile=target,
        max_follows=max_follows,
        seed_accounts=seed_accounts,
        tracker=tracker,
    )

    log.info("[Workflow/TikTok] smart_follow 完成: device=%s result=%s", device_id[:8], result)
    return {"success": True, **result} if isinstance(result, dict) else {"success": bool(result)}


def tiktok_browse_feed(
    device_id: str,
    video_count: int = 10,
    like_probability: float = 0.25,
    target_country: str = "italy",
    **kwargs
) -> dict:
    """浏览 For You 页（轻量版，不含关注）。"""
    tt, _ = _fresh_tiktok(device_id)
    minutes = max(1, int(video_count * 8 / 60) + 1)
    stats = tt.warmup_session(
        duration_minutes=minutes,
        like_probability=like_probability,
        target_country=target_country,
        phase="interest_building",
    )
    return {"success": True, "feed_stats": stats}


def tiktok_check_and_chat_followbacks(
    device_id: str,
    max_chats: int = 10,
    use_ai: bool = True,
    auto_reply: bool = True,
    **kwargs
) -> dict:
    """检查回关用户并发送 AI 私信。"""
    tt, _ = _fresh_tiktok(device_id)

    # ★ A/B 最优变体自动应用
    dm_template = _get_best_ab_variant("dm_template_style")

    result = tt.check_and_chat_followbacks(
        max_chats=max_chats,
        use_ai=use_ai,
        auto_reply=auto_reply,
        dm_template=dm_template,
    )

    log.info("[Workflow/TikTok] chat_followbacks 完成: device=%s result=%s", device_id[:8], result)
    return {"success": True, **(result if isinstance(result, dict) else {"replied": 0})}


def tiktok_search_and_collect_leads(
    device_id: str,
    query: str = "italy",
    max_leads: int = 10,
    **kwargs
) -> dict:
    """搜索关键词并收集潜在用户到 LeadsStore。"""
    tt, _ = _fresh_tiktok(device_id)
    result = tt.search_and_collect_leads(query=query, max_leads=max_leads)
    return {"success": True, "leads": result or [], "count": len(result) if result else 0}


def tiktok_send_dm(
    device_id: str,
    username: str = "",
    message: str = "",
    lead_id: Optional[int] = None,
    **kwargs
) -> dict:
    """向指定用户发送私信。"""
    tt, _ = _fresh_tiktok(device_id)

    # ★ 未指定消息时使用 A/B 最优模板
    if not message:
        template = _get_best_ab_variant("dm_template_style")
        message = _generate_dm_from_template(template, username)

    ok = tt.send_dm(username=username, message=message)
    return {"success": bool(ok), "username": username}


def tiktok_check_inbox(
    device_id: str,
    auto_reply: bool = True,
    use_ai: bool = True,
    max_messages: int = 20,
    **kwargs
) -> dict:
    """检查收件箱并 AI 自动回复。"""
    tt, _ = _fresh_tiktok(device_id)
    result = tt.check_inbox(auto_reply=auto_reply, use_ai=use_ai,
                            max_messages=max_messages)
    return {"success": True, **(result if isinstance(result, dict) else {})}


def tiktok_contact_discovery(
    device_id: str,
    max_friends: int = 15,
    auto_follow: bool = True,
    auto_message: bool = False,
    target_language: str = "italian",
    **kwargs
) -> dict:
    """
    ★ 通讯录好友发现（新增 action）
    进入 TikTok → 添加好友 → 通讯录 tab，关注联系人匹配到的 TikTok 账号。
    发现结果自动同步到 LeadsStore。
    """
    tt, _ = _fresh_tiktok(device_id)

    from src.app_automation.contacts_manager import tiktok_find_contact_friends
    result = tiktok_find_contact_friends(
        tiktok_automation=tt,
        device_id=device_id,
        max_friends=max_friends,
        auto_follow=auto_follow,
        auto_message=auto_message,
        target_language=target_language,
    )

    # ★ 自动同步到 LeadsStore
    if result.get("discovered_names"):
        _sync_contact_discoveries_to_leads(
            device_id=device_id,
            discovered_names=result["discovered_names"],
        )

    log.info("[Workflow/TikTok] contact_discovery 完成: device=%s found=%d followed=%d",
             device_id[:8], result.get("found", 0), result.get("followed", 0))
    return {"success": True, **result}


# ─────────────────────────────────────────────────────────────
# ★ Telegram Actions
# ─────────────────────────────────────────────────────────────

def telegram_check_new_messages(
    device_id: str,
    max_chats: int = 10,
    **kwargs
) -> dict:
    """检查 Telegram 新消息。"""
    tg, _ = _fresh_telegram(device_id)
    result = tg.check_new_messages(max_chats=max_chats)
    new_messages = result if isinstance(result, int) else (result or {}).get("new_messages", 0)
    return {"success": True, "new_messages": new_messages}


def telegram_auto_reply_pending(
    device_id: str,
    max_replies: int = 5,
    **kwargs
) -> dict:
    """自动回复 Telegram 待回复消息。"""
    tg, _ = _fresh_telegram(device_id)
    result = tg.auto_reply_pending(max_replies=max_replies)
    return {"success": True, **(result if isinstance(result, dict) else {"replied": 0})}


def telegram_send_text_message(
    device_id: str,
    username: str = "",
    message: str = "",
    **kwargs
) -> dict:
    """向 Telegram 用户发送文本消息。"""
    tg, _ = _fresh_telegram(device_id)
    ok = tg.send_text_message(username=username, message=message)
    return {"success": bool(ok), "username": username}


# ─────────────────────────────────────────────────────────────
# ★ 通讯录 Actions
# ─────────────────────────────────────────────────────────────

def contacts_inject(
    device_id: str,
    contacts: Optional[list] = None,
    country_code: str = "IT",
    **kwargs
) -> dict:
    """批量注入联系人到手机通讯录。"""
    from src.app_automation.contacts_manager import ContactsManager
    mgr = ContactsManager(adb_serial=device_id)
    result = mgr.inject_contacts(contacts or [], country_code=country_code)
    return {"success": True, **(result if isinstance(result, dict) else {})}


def contacts_list(device_id: str, **kwargs) -> dict:
    """读取设备通讯录列表。"""
    from src.app_automation.contacts_manager import ContactsManager
    mgr = ContactsManager(adb_serial=device_id)
    items = mgr.list_contacts()
    return {"success": True, "contacts": items, "count": len(items)}


def contacts_sync_to_leads(device_id: str, **kwargs) -> dict:
    """同步通讯录好友发现结果到 LeadsStore。"""
    from src.app_automation.contacts_manager import get_discovery_results
    results = get_discovery_results(device_id=device_id)
    synced = _sync_contact_discoveries_to_leads(
        device_id=device_id,
        discovered_names=[r["tiktok_username"] for r in results if r.get("tiktok_username")],
        source="contacts_sync",
    )
    return {"success": True, "synced": synced}


# ─────────────────────────────────────────────────────────────
# ★ 分析 Actions
# ─────────────────────────────────────────────────────────────

def analytics_daily_summary(days: int = 1, **kwargs) -> dict:
    """获取每日分析摘要。"""
    try:
        from src.host.routers.analytics import analytics_summary
        result = analytics_summary(days=days)
        return {"success": True, "summary": result}
    except Exception as e:
        return {"success": False, "error": str(e)}


def analytics_ab_best_variant(
    experiment_name: str = "dm_template_style",
    metric: str = "reply_received",
    min_samples: int = 5,
    **kwargs
) -> dict:
    """获取 A/B 测试最优变体。"""
    from src.host.ab_testing import get_ab_store
    best = get_ab_store().best_variant(experiment_name, metric=metric, min_samples=min_samples)
    return {"success": True, "best_variant": best, "experiment": experiment_name}


# ─────────────────────────────────────────────────────────────
# 内部辅助函数
# ─────────────────────────────────────────────────────────────

def _get_best_ab_variant(experiment_name: str, metric: str = "reply_received") -> str:
    """获取 A/B 测试最优变体，失败时返回空字符串（使用默认）。"""
    try:
        from src.host.ab_testing import get_ab_store
        best = get_ab_store().best_variant(experiment_name, metric=metric, min_samples=10)
        if best and best != "control":
            log.debug("[A/B] 自动应用最优变体: %s=%s", experiment_name, best)
        return best
    except Exception:
        return ""


def _generate_dm_from_template(template: str, username: str) -> str:
    """根据模板生成 DM 文案。"""
    try:
        from src.ai.tiktok_chat_ai import TikTokChatAI
        ai = TikTokChatAI()
        return ai.generate_natural_message(context=template or "greeting", language="italian")
    except Exception:
        return "Ciao! Grazie per il follow! 😊"


def _sync_contact_discoveries_to_leads(
    device_id: str,
    discovered_names: list,
    source: str = "contact_discovery",
) -> int:
    """
    ★ 核心闭环函数：将通讯录好友发现结果同步到 LeadsStore。
    对已存在的 lead 仅更新互动记录，不重复创建。
    返回新增的 lead 数量。
    """
    if not discovered_names:
        return 0

    try:
        from src.leads.store import get_leads_store
        store = get_leads_store()
        new_count = 0

        for username in discovered_names:
            if not username:
                continue

            # 查找或创建 lead
            lead_id = store.find_by_platform_username("tiktok", username)
            if not lead_id:
                lead_id = store.add_lead(
                    name=username,
                    source_platform="tiktok",
                    tags=[source, "contact_friend"],
                )
                if lead_id:
                    store.add_platform_profile(lead_id, "tiktok", username)
                    new_count += 1
                    log.info("[通讯录同步] 新 Lead: %s (id=%s)", username, lead_id)

            # 记录"通讯录发现"互动（高价值标记）
            if lead_id:
                store.add_interaction(
                    lead_id, "tiktok", "contact_discovered",
                    direction="outbound",
                    device_id=device_id,
                    metadata={"source": source, "device": device_id[:8]},
                )
                # 通讯录好友优先度更高，重新计算评分（互动记录已更新）
                store.update_score(lead_id)

        log.info("[通讯录同步] 完成: 新增 %d 个 Lead (总发现 %d 个)",
                 new_count, len(discovered_names))
        return new_count

    except Exception as e:
        log.error("[通讯录同步] 失败: %s", e)
        return 0


# ─────────────────────────────────────────────────────────────
# 注册入口
# ─────────────────────────────────────────────────────────────

def register_all_platform_actions():
    """
    在系统启动时调用，将所有平台 action 注册到 ActionRegistry。
    幂等安全（重复注册会覆盖同名 action）。
    """
    from src.workflow.actions import get_action_registry
    registry = get_action_registry()

    # ── TikTok ──
    tiktok_actions = {
        "tiktok.warmup_session":           tiktok_warmup_session,
        "tiktok.smart_follow":             tiktok_smart_follow,
        "tiktok.browse_feed":              tiktok_browse_feed,
        "tiktok.check_and_chat_followbacks": tiktok_check_and_chat_followbacks,
        "tiktok.search_and_collect_leads": tiktok_search_and_collect_leads,
        "tiktok.send_dm":                  tiktok_send_dm,
        "tiktok.check_inbox":              tiktok_check_inbox,
        "tiktok.contact_discovery":        tiktok_contact_discovery,  # ★ 新增
    }
    for name, fn in tiktok_actions.items():
        registry.register(name, fn)

    # ── Telegram ──
    telegram_actions = {
        "telegram.check_new_messages":  telegram_check_new_messages,
        "telegram.auto_reply_pending":  telegram_auto_reply_pending,
        "telegram.send_text_message":   telegram_send_text_message,
    }
    for name, fn in telegram_actions.items():
        registry.register(name, fn)

    # ── 通讯录 ──
    contacts_actions = {
        "contacts.inject":          contacts_inject,
        "contacts.list":            contacts_list,
        "contacts.sync_to_leads":   contacts_sync_to_leads,
        "contacts.tiktok_discovery": tiktok_contact_discovery,  # 双入口
    }
    for name, fn in contacts_actions.items():
        registry.register(name, fn)

    # ── 分析 ──
    analytics_actions = {
        "analytics.daily_summary":  analytics_daily_summary,
        "analytics.ab_best_variant": analytics_ab_best_variant,
    }
    for name, fn in analytics_actions.items():
        registry.register(name, fn)

    total = registry.count
    log.info("[Platform Actions] 注册完成: 共 %d 个 action (平台: tiktok=%d telegram=%d contacts=%d analytics=%d util=4)",
             total, len(tiktok_actions), len(telegram_actions),
             len(contacts_actions), len(analytics_actions))
    return total
