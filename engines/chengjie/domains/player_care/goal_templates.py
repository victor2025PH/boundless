"""player_care 域的 companion.goals 模板（B3）。

模板只在本域包内定义，进程启动 / 首次 sync 时 ``register_goal_templates()`` 以
``setdefault`` 挂进 ``src.companion.goals.templates.TEMPLATES``——核心模板表源码不改，
story_matrix 实例不加载本域 → 永远看不到这两个模板。

铁律落到模板上：``push_curve`` 全程 ``none``——朋友唤回 / 充值后的关心都**不带任何
推进**：不催充、不提优惠、不承诺赢、不报数字；游戏只在对方先聊到时接。
"""
from __future__ import annotations

from typing import Any, Dict

TEMPLATE_REENGAGE = "player_reengage"
TEMPLATE_AFTER_DEPOSIT = "player_after_deposit"

PLAYER_GOAL_TEMPLATES: Dict[str, Dict[str, Any]] = {
    TEMPLATE_REENGAGE: {
        "name_zh": "玩家朋友·沉默轻触",
        "name_en": "Player friend: quiet re-touch",
        "kind": "engagement",
        "default_days": 7,
        "params": [
            {"key": "note", "type": "string", "default": "",
             "label_zh": "备注（对方沉默前聊到什么）", "label_en": "Context note"},
            {"key": "stage_before", "type": "string", "default": "",
             "label_zh": "沉默前阶段", "label_en": "Stage before dormant"},
        ],
        "milestones": [
            {"id": "hello", "zh": "随手招呼", "en": "Casual hello"},
            {"id": "recall", "zh": "接上次的话", "en": "Pick up the thread"},
            {"id": "share", "zh": "分享自己的近况", "en": "Share own news"},
            {"id": "leave_door", "zh": "留门", "en": "Leave the door open"},
        ],
        "push_curve": ("none", "none", "none", "none"),
        "intents": {
            0: ("像朋友一样随手打个招呼（问最近忙不忙 / 在干嘛），绝不提「好久没回我」，绝不提游戏或账户",
                "分享自己今天一件很小的事作开场，不要求对方回应，不提游戏"),
            1: ("接上对方上次自己聊过的一件生活小事自然回访，不提游戏、不问账户",
                "用一个只有你们聊过的细节唤起记忆，显得真诚不群发；不带任何推进"),
            2: ("分享自己的近况（吃的 / 天气 / 家里的事），给对方一个回话的由头；游戏只在对方先提时才接",
                "聊一个轻松话题（当地新闻 / 天气 / 节日），不带目的"),
            3: ("最后一次轻触达：表示自己一直在、想聊随时找我，完全不施压、不提任何数字或优惠",
                "轻轻收尾：祝好 + 留门，保持体面"),
        },
    },
    TEMPLATE_AFTER_DEPOSIT: {
        "name_zh": "玩家朋友·玩后关心",
        "name_en": "Player friend: check-in after playing",
        "kind": "engagement",
        "default_days": 3,
        "params": [
            {"key": "note", "type": "string", "default": "",
             "label_zh": "备注", "label_en": "Context note"},
        ],
        "milestones": [
            {"id": "care", "zh": "关心一下", "en": "Check in"},
            {"id": "chat", "zh": "顺着聊", "en": "Keep chatting"},
            {"id": "rest", "zh": "劝休息", "en": "Suggest a break"},
            {"id": "close", "zh": "收尾", "en": "Wrap up"},
        ],
        "push_curve": ("none", "none", "none", "none"),
        "intents": {
            0: ("像朋友一样问问对方今天过得怎样、有没有休息好；不提充值、不提余额、不报任何数字",
                "关心对方近况（吃饭了没 / 累不累），完全不提游戏账户"),
            1: ("顺着对方自己说的话聊；如果对方自己提到在玩，只以「我也在玩」的口吻接一句，不劝加码、不承诺赢",
                "聊对方感兴趣的轻松话题；对方不提游戏就绝不提"),
            2: ("如果对方说玩得久 / 输了 / 累了，像朋友一样劝休息、量力而行，绝不用『下次运气会好』之类的话安慰",
                "提醒对方注意休息和节制，不带任何推进"),
            3: ("轻轻收尾，祝对方好，留门随时聊", "自然结束话题，不提任何数字或优惠"),
        },
    },
}


def register_goal_templates() -> int:
    """把本域模板挂进核心注册表（幂等；核心已有同名模板绝不覆盖）。返回新挂数。"""
    try:
        from src.companion.goals.templates import TEMPLATES
    except Exception:
        return 0
    n = 0
    for tid, tmpl in PLAYER_GOAL_TEMPLATES.items():
        if tid not in TEMPLATES:
            TEMPLATES[tid] = tmpl
            n += 1
    return n


def unregister_goal_templates() -> int:
    """摸掉本域挂进去的模板（只删自己的对象；测试隔离用）。"""
    try:
        from src.companion.goals.templates import TEMPLATES
    except Exception:
        return 0
    n = 0
    for tid, tmpl in PLAYER_GOAL_TEMPLATES.items():
        if TEMPLATES.get(tid) is tmpl:
            del TEMPLATES[tid]
            n += 1
    return n
