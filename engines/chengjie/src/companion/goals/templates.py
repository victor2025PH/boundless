"""营销目标「模板注册表」（纯数据 + 纯函数，零 IO，可单测）。

设计原则（与 AI SDR「信号驱动 cadence」对齐，但**意图化**而非「第 N 天发什么话术」）：
- 模板只声明**弧线**（4 个里程碑）与**每段的今日意图池**——具体话术由回复生成层
  按当前人设/语言/上下文现场生成，模板绝不出成品文案（防「群发感」）。
- 意图池按 ``crc32(goal_id + day)`` 确定性轮换：同目标同日恒定（缓存/复现友好、
  可单测），跨日自然变化（不像机器人每天说一样的话）。
- ``push_curve`` 声明每个里程碑的默认推进力度：``none``（只字不提营销）/
  ``soft``（顺势自然带到）/ ``direct``（可以直说）。谁消费谁负责再叠情绪护栏。
"""

from __future__ import annotations

import zlib
from typing import Any, Dict, List, Optional

# 目标生命周期状态（store/routes/UI 共用词表）
GOAL_STATUSES = ("active", "paused", "done", "failed", "expired", "cancelled")
# 自治档位：observe=只看不动（注入零推进）；suggest=注入草稿/回复链（默认）；
# auto=另可搭主动触达桥（companion.goals.bridge，仅 auto_ai 会话）。
AUTONOMY_LEVELS = ("observe", "suggest", "auto")
# 今日意图的推进力度
PUSH_LEVELS = ("none", "soft", "direct")

# 退避日（连发未回时）的纯陪伴意图池——所有模板共用
CARE_INTENTS = (
    "今天只关心对方情绪和近况，完全不提任何推进话题",
    "纯陪伴日：聊对方感兴趣的轻松话题，不带任何目的",
)

# 漏斗阶段序（relationship_stage 结算用；与 contacts.Journey 阶段名对齐，全小写比较）
STAGE_ORDER = (
    "initial", "contacted", "engaged", "qualified",
    "handoff_ready", "handed_off", "converted",
)

TEMPLATES: Dict[str, Dict[str, Any]] = {
    "conversion_unlock": {
        "name_zh": "付费解锁转化",
        "name_en": "Paid unlock",
        "kind": "conversion",
        "default_days": 14,
        "params": [
            {"key": "item_id", "type": "string", "default": "bazi_reading",
             "label_zh": "解锁项 ID", "label_en": "Unlock item id"},
            {"key": "item_label", "type": "string", "default": "八字详批",
             "label_zh": "解锁项说法（对话里怎么称呼它）",
             "label_en": "How to call it in chat"},
        ],
        "milestones": [
            {"id": "connect", "zh": "破冰回暖", "en": "Reconnect"},
            {"id": "value", "zh": "价值铺垫", "en": "Seed value"},
            {"id": "offer", "zh": "顺势开价", "en": "Soft offer"},
            {"id": "close", "zh": "跟进收口", "en": "Follow up"},
        ],
        "push_curve": ("none", "soft", "direct", "soft"),
        "intents": {
            0: ("顺着对方最近聊过的事自然回暖，把互动节奏找回来",
                "从今天的日常切入，关心一下对方近况，先让对话热起来"),
            1: ("聊到相关话题时自然展示你在「{item}」上的见解，让对方觉得有收获，不提价格",
                "用一个贴合对方处境的小例子，带出「{item}」能帮到TA什么"),
            2: ("对方兴致好时自然提到「{item}」可以看得更深入，顺势说明解锁方式",
                "如果对方主动追问，就大方介绍「{item}」的内容和价格，态度轻松不推销"),
            3: ("对方还在犹豫就先退回日常话题，轻描淡写补一句「{item}」随时可以看，给足台阶",
                "对方已表现兴趣的话，帮TA下决心：说清拿到后马上能看到什么"),
        },
    },
    "conversion_subscribe": {
        "name_zh": "会员订阅转化",
        "name_en": "Subscription",
        "kind": "conversion",
        "default_days": 21,
        "params": [
            {"key": "tier", "type": "string", "default": "vip",
             "label_zh": "目标会员档", "label_en": "Target tier"},
            {"key": "item_label", "type": "string", "default": "会员",
             "label_zh": "会员说法（对话里怎么称呼）",
             "label_en": "How to call it in chat"},
        ],
        "milestones": [
            {"id": "connect", "zh": "破冰回暖", "en": "Reconnect"},
            {"id": "value", "zh": "价值铺垫", "en": "Seed value"},
            {"id": "offer", "zh": "顺势开价", "en": "Soft offer"},
            {"id": "close", "zh": "跟进收口", "en": "Follow up"},
        ],
        "push_curve": ("none", "soft", "direct", "soft"),
        "intents": {
            0: ("保持轻松日常互动，让对方觉得跟你聊天是件放松的事",
                "顺着对方的话题多聊几轮，先把在场感做足"),
            1: ("在对方用到相关功能/内容时，自然带一句「{item}」还能怎么样，不提价",
                "让对方感受到你们互动里已经有的价值，为「{item}」做心理铺垫"),
            2: ("对方兴致好时顺势介绍「{item}」的好处和开通方式，语气像分享不像推销",
                "对方问到时大方说明「{item}」价格与权益，给对方自己决定的空间"),
            3: ("不催不逼，隔天自然补一句；对方犹豫就先放下，聊回日常",
                "对方已心动的话，给一个现在开通的小理由（新内容/陪伴感），帮TA收口"),
        },
    },
    "relationship_stage": {
        "name_zh": "关系阶段推进",
        "name_en": "Stage advance",
        "kind": "relationship",
        "default_days": 21,
        "params": [
            {"key": "target_stage", "type": "string", "default": "qualified",
             "label_zh": "目标漏斗阶段", "label_en": "Target funnel stage"},
        ],
        "milestones": [
            {"id": "engage", "zh": "拉起互动", "en": "Engage"},
            {"id": "warm", "zh": "持续升温", "en": "Warm up"},
            {"id": "trust", "zh": "建立信任", "en": "Build trust"},
            {"id": "ready", "zh": "就绪收口", "en": "Ready"},
        ],
        "push_curve": ("none", "none", "soft", "soft"),
        "intents": {
            0: ("多用开放式问题让对方多说，找到TA真正愿意聊的话题",
                "对对方说的每件事都接得住，让TA觉得跟你聊天不费劲"),
            1: ("回应里自然带上对方之前说过的细节，让TA感到被记住",
                "在对方的兴趣点上深入聊几轮，制造「聊得来」的感觉"),
            2: ("适度自我暴露一点日常或小心事，换取对方的信任和分享",
                "对方提到烦恼时认真接住，给情绪价值不给说教"),
            3: ("确认对方的核心诉求并自然总结，为下一步做好铺垫",
                "稳定日常互动节奏，让关系保持在热络状态"),
        },
    },
    "relationship_intimacy": {
        "name_zh": "亲密度目标",
        "name_en": "Intimacy target",
        "kind": "relationship",
        "default_days": 30,
        "params": [
            {"key": "target_score", "type": "number", "default": 55,
             "label_zh": "目标亲密度（0-100）", "label_en": "Target intimacy"},
        ],
        "milestones": [
            {"id": "daily", "zh": "日常热络", "en": "Daily rapport"},
            {"id": "deepen", "zh": "话题深入", "en": "Deepen"},
            {"id": "exclusive", "zh": "专属感", "en": "Exclusive"},
            {"id": "steady", "zh": "稳固陪伴", "en": "Steady"},
        ],
        "push_curve": ("none", "none", "none", "none"),
        "intents": {
            0: ("保持轻松日常互动，让对话别断，节奏以对方舒服为准",
                "找一个今天的小事作话头，把互动自然续上"),
            1: ("挑一个对方感兴趣的话题深入聊几轮，别浅尝辄止",
                "认真回应对方说过的事，追问一个走心的细节"),
            2: ("制造一点专属感：记得TA的偏好、只跟TA说的小事",
                "用「上次你说…」自然回访，让对方感到被特别对待"),
            3: ("自然表达在乎和陪伴，让对方习惯有你在",
                "稳定出现在对方的日常里，不黏不冷"),
        },
    },
    "engagement_reactivate": {
        "name_zh": "沉默唤回",
        "name_en": "Reactivate",
        "kind": "engagement",
        "default_days": 10,
        "params": [
            {"key": "note", "type": "string", "default": "",
             "label_zh": "备注（对方为何沉默/背景）", "label_en": "Context note"},
        ],
        "milestones": [
            {"id": "probe", "zh": "轻触探温", "en": "Probe"},
            {"id": "recall", "zh": "记忆回访", "en": "Recall"},
            {"id": "value", "zh": "给个来由", "en": "Give a reason"},
            {"id": "last_call", "zh": "收尾一问", "en": "Last call"},
        ],
        "push_curve": ("none", "none", "soft", "soft"),
        "intents": {
            0: ("用轻量无压力的方式打个招呼，绝不提「好久没回我」",
                "分享一件自己今天的小事作开场，不要求对方必须回应"),
            1: ("带上对方之前聊过的一件事自然回访（『上次你说的那事后来怎么样』）",
                "用一个只有你们聊过的细节唤起对方记忆，显得真诚不群发"),
            2: ("分享一个对方可能感兴趣的小内容/小更新，给TA一个回来的理由",
                "提供一点新鲜价值（趣事/进展/内容），别空转寒暄"),
            3: ("最后一次轻触达：表示自己一直在、随时可以聊，完全不施压",
                "轻轻收尾：祝好 + 留门（想聊随时找我），保持体面"),
        },
    },
    "custom": {
        "name_zh": "自定义目标",
        "name_en": "Custom",
        "kind": "custom",
        "default_days": 14,
        "params": [
            {"key": "note", "type": "string", "default": "",
             "label_zh": "目标描述（给 AI 看的推进方向）",
             "label_en": "Goal description"},
        ],
        "milestones": [
            {"id": "s1", "zh": "起步", "en": "Start"},
            {"id": "s2", "zh": "推进", "en": "Advance"},
            {"id": "s3", "zh": "深化", "en": "Deepen"},
            {"id": "s4", "zh": "收口", "en": "Wrap up"},
        ],
        "push_curve": ("none", "soft", "soft", "soft"),
        "intents": {
            0: ("围绕目标「{note}」找一个自然的切入点起步，节奏以对方舒适为先",),
            1: ("顺着已有话题把「{note}」自然推进一小步，不生硬",),
            2: ("在对方兴致好的时候，围绕「{note}」聊得更具体一些",),
            3: ("围绕「{note}」自然收口：确认对方的态度，给足台阶",),
        },
    },
}


def template_ids() -> List[str]:
    return list(TEMPLATES.keys())


def get_template(template_id: str) -> Optional[Dict[str, Any]]:
    return TEMPLATES.get(str(template_id or "").strip())


def list_templates() -> List[Dict[str, Any]]:
    """API/UI 消费的公开形状（含 id；不含 intents 内部池）。"""
    out: List[Dict[str, Any]] = []
    for tid, t in TEMPLATES.items():
        out.append({
            "id": tid,
            "name_zh": t["name_zh"],
            "name_en": t["name_en"],
            "kind": t["kind"],
            "default_days": t["default_days"],
            "params": [dict(p) for p in t["params"]],
            "milestones": [dict(m) for m in t["milestones"]],
        })
    return out


def milestone_label(template: Dict[str, Any], idx: int, lang: str = "zh") -> str:
    ms = template.get("milestones") or []
    i = max(0, min(int(idx), len(ms) - 1)) if ms else 0
    if not ms:
        return ""
    key = "en" if str(lang).lower().startswith("en") else "zh"
    return str(ms[i].get(key) or ms[i].get("zh") or "")


def push_for_milestone(template: Dict[str, Any], idx: int) -> str:
    curve = template.get("push_curve") or ()
    if not curve:
        return "soft"
    i = max(0, min(int(idx), len(curve) - 1))
    lvl = str(curve[i])
    return lvl if lvl in PUSH_LEVELS else "soft"


def _format_intent(raw: str, params: Dict[str, Any]) -> str:
    """把模板参数代入意图串（只认 {item}/{note}；缺参优雅留白不抛）。"""
    item = str((params or {}).get("item_label")
               or (params or {}).get("item_id") or "").strip()
    note = str((params or {}).get("note") or "").strip()
    try:
        return raw.replace("{item}", item or "它").replace("{note}", note or "这个目标")
    except Exception:
        return raw


def pick_intent(
    template: Dict[str, Any], milestone_idx: int, goal_id: str, day: str,
    params: Optional[Dict[str, Any]] = None,
) -> str:
    """确定性选今日意图：``crc32(goal_id+day)`` 定池内下标——同目标同日恒定、跨日轮换。"""
    pools = template.get("intents") or {}
    keys = sorted(pools.keys())
    if not keys:
        return ""
    mi = max(0, min(int(milestone_idx), max(keys)))
    pool = pools.get(mi) or pools.get(keys[-1]) or ()
    if not pool:
        return ""
    h = zlib.crc32(f"{goal_id}:{day}".encode("utf-8", "ignore"))
    return _format_intent(str(pool[h % len(pool)]), params or {})


def pick_care_intent(goal_id: str, day: str) -> str:
    """退避日纯陪伴意图（同样确定性轮换）。"""
    h = zlib.crc32(f"care:{goal_id}:{day}".encode("utf-8", "ignore"))
    return CARE_INTENTS[h % len(CARE_INTENTS)]


__all__ = [
    "AUTONOMY_LEVELS",
    "CARE_INTENTS",
    "GOAL_STATUSES",
    "PUSH_LEVELS",
    "STAGE_ORDER",
    "TEMPLATES",
    "get_template",
    "list_templates",
    "milestone_label",
    "pick_care_intent",
    "pick_intent",
    "push_for_milestone",
    "template_ids",
]
