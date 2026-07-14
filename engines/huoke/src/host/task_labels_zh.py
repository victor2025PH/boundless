# -*- coding: utf-8 -*-
"""任务类型 → 中文名的单一真源。

设计目标：
    - 后端、前端、AI 快捷指令都读同一份字典，消除中英混杂。
    - 以 routers.platforms.PLATFORMS 里每个 task_type 的 ``label`` 为基础，
      再用本文件下方的 ``_OVERRIDES`` 覆盖 / 补齐 platforms 里漏写的类型。
    - 导出 export_frontend_dict() 给 core.js 启动时拉取。

修改约定：
    - 想把 "FB浏览" 改成 "浏览动态"，优先在 platforms.py 的 label 改；
      本文件只放 platforms 覆盖不到的 task_type（或需要"前缀加平台名"的统一风格）。
    - 不要在 core.js 里再单独添加中文映射，那会立刻又一次分裂。
"""

from __future__ import annotations

import logging
from typing import Dict, Mapping

logger = logging.getLogger(__name__)


# -- platforms.py 未覆盖 / 名字与前端习惯不一致的补充 --------------------------
#   这里也顺手统一了 TikTok 常用却漏写的 tiktok_browse_feed，以及 ai_quick 用的口语名。
_OVERRIDES: Dict[str, str] = {
    # TikTok：补齐 platforms 没列、但代码/executor 里常见的类型
    "tiktok_browse_feed": "刷视频",
    "tiktok_auto": "全流程获客",
    "tiktok_chat": "AI 聊天",
    "tiktok_keyword_search": "关键词搜索",
    "tiktok_live_engage": "直播互动",
    "tiktok_test_follow": "测试关注",
    "tiktok_check_and_chat_followbacks": "回关私信",

    # Facebook：补 platforms 缺口 + 给 campaign/profile_hunt 更友好的展示
    "facebook_warmup": "FB 养号",
    "facebook_profile_hunt": "FB 画像识别",
    "facebook_campaign_run": "FB 剧本任务",
    "facebook_check_inbox": "FB Messenger 收件箱",
    "facebook_check_message_requests": "FB 陌生人收件箱",
    "facebook_check_friend_requests": "FB 好友请求处理",
    "facebook_browse_groups": "FB 浏览我的群组",
    "facebook_group_engage": "FB 群组互动",
    "facebook_extract_members": "FB 群成员候选采集",
    "facebook_group_member_greet": "FB 群成员好友打招呼",
    "facebook_browse_feed": "FB 浏览动态",
    "facebook_browse_feed_by_interest": "FB 兴趣刷帖",
    "facebook_search_leads": "FB 搜索潜客",
    "facebook_join_group": "FB 加入群组",
    "facebook_add_friend": "FB 加好友(安全)",
    "facebook_add_friend_and_greet": "FB 加好友+打招呼",
    "facebook_send_greeting": "FB 搜名字打招呼",
    "facebook_send_message": "FB 发私信",

    # VPN / 系统类
    "vpn_setup": "配置 VPN",
    "vpn_status": "VPN 状态检查",

    # Telegram / WhatsApp / LinkedIn / Instagram / X 等：
    # 绝大多数已经由 platforms.PLATFORMS[*]["task_types"][*]["label"] 覆盖；
    # 这里只对"未出现在 platforms 但 executor 会跑"的类型兜底。
    "telegram_workflow": "Telegram 工作流",
    "telegram_acquisition": "Telegram 全流程获客",
    "whatsapp_acquisition": "WhatsApp 全流程获客",

    # 取消类（AI 快捷指令里用的伪 task_type）
    "_cancel_all": "取消全部任务",
}


def _load_from_platforms() -> Dict[str, str]:
    """从 platforms.PLATFORMS 里提取 task_type -> label。"""
    out: Dict[str, str] = {}
    try:
        from .routers.platforms import PLATFORMS
    except Exception as err:
        logger.debug("[task_labels_zh] 读 platforms 失败: %s", err)
        return out
    for _pid, p in PLATFORMS.items():
        platform_name = p.get("name") or ""
        for item in p.get("task_types", []):
            t = item.get("type")
            label = item.get("label")
            if not t or not label:
                continue
            # 给太短的 label（如"发消息""自动回复"）加上平台前缀，避免一屏里
            # 几个平台同名打架。长度 <= 5 的中文 label 统一加前缀。
            if platform_name and len(label) <= 5 and platform_name not in label:
                out[t] = f"{platform_name} {label}"
            else:
                out[t] = label
    return out


def _build() -> Dict[str, str]:
    merged = _load_from_platforms()
    merged.update(_OVERRIDES)
    return merged


# 首次访问时才构造，避免模块加载顺序问题（platforms import 链较重）
_CACHE: Dict[str, str] | None = None


def get_all_labels() -> Mapping[str, str]:
    global _CACHE
    if _CACHE is None:
        _CACHE = _build()
    return _CACHE


def task_label_zh(task_type: str | None) -> str:
    """拿到中文名；未命中返回原 task_type 字符串本身。"""
    if not task_type:
        return ""
    return get_all_labels().get(task_type, task_type)


def export_frontend_dict() -> Dict[str, str]:
    """返回可直接 JSON 化给前端的字典（副本，避免外部改到缓存）。"""
    return dict(get_all_labels())


def refresh_cache() -> None:
    """测试 / 热更新用；正常运行不必调用。"""
    global _CACHE
    _CACHE = None
