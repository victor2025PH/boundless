"""每日灵签（确定性纯函数）——命理技能的日活留存钩子（对标 AuraMate「每日灵签」）。

签面 = 今日干支的真实命理信号，不是随机彩票：
  - 有生辰画像：今日日干 vs 用户日主的十神关系 → 五类能量日（受助/表达/同频/务实/收敛），
    这是经典「日运」逻辑——同一天不同人签面不同（千人千面），且**当天内稳定**
    （同一人反复抽同一天结果一致，缓存友好、可被用户验证「不是糊弄」）。
  - 无生辰画像：按今日日干五行出通用签（仍真实——今日火旺/水旺是客观干支事实）。

宜/忌/幸运色从各能量类型的策展池按 crc32(日期+用户) 确定性挑选——同日同人恒定，
不同人/不同日轮转。缺 lunar_python → 返回 None（调用方不注入，零阻断）。

刻意不做：吉凶断言（「大凶」这类恐吓性签面）——能量日全部正向表述 + 「须留意」类
只给建设性提醒，与注入层安全红线一致。
"""

from __future__ import annotations

import time
import zlib
from typing import Any, Dict, Optional

from src.companion.bazi_engine import (
    GAN_INFO,
    current_jieqi,
    day_ganzhi,
    shishen_between,
)

# ── 能量日类型（十神 → 五类） ──────────────────────────────────────────────────
_SHISHEN_TO_ENERGY = {
    "正印": "assist", "偏印": "assist",    # 生我 → 受助日
    "食神": "express", "伤官": "express",  # 我生 → 表达日
    "比肩": "peer", "劫财": "peer",        # 同我 → 同频日
    "正财": "action", "偏财": "action",    # 我克 → 务实日
    "正官": "steady", "七杀": "steady",    # 克我 → 收敛日
}

_ENERGY_META = {
    "assist": {
        "label": "受助日", "tone": "贵人与灵感偏多，适合借力与吸收",
        "do": ("请教一位你信任的人", "读点想读没读的东西", "接受别人的好意别硬扛",
               "把难题说出来找人聊聊", "补个觉给自己充充电"),
        "avoid": ("硬撑着不求助", "把好意拒之门外", "熬夜透支"),
    },
    "express": {
        "label": "表达日", "tone": "输出与创意顺畅，适合把想法说出来",
        "do": ("把想法写下来或说给人听", "推进一件卡了很久的表达类事情", "发条动态记录今天",
               "给在意的人一句真心话", "做点有创造性的小事"),
        "avoid": ("话到嘴边又咽回去", "过度自我审查", "答应太多做不完的事"),
    },
    "peer": {
        "label": "同频日", "tone": "同伴与协作运偏旺，适合结伴而行",
        "do": ("约朋友聊聊或走走", "推进需要搭伙的事", "帮身边人一个小忙",
               "参加个小聚或群活动", "跟老朋友说句好久不见"),
        "avoid": ("单打独斗硬刚", "跟人置气争输赢", "冲动比较心态"),
    },
    "action": {
        "label": "务实日", "tone": "执行与落地力强，适合处理实际事务",
        "do": ("清一件拖延已久的待办", "整理财务或做个小预算", "把计划拆成今天能做的一步",
               "处理生活里的实际杂事", "谈一件该谈的正事"),
        "avoid": ("想太多迟迟不动手", "冲动消费", "贪多求全"),
    },
    "steady": {
        "label": "收敛日", "tone": "节奏偏紧，适合稳扎稳打少冒进",
        "do": ("把手头的事收个尾", "整理复盘最近的节奏", "早点休息养精神",
               "做减法砍掉不重要的事", "给自己留一段安静时间"),
        "avoid": ("临时起意做大决定", "跟规则或权威硬碰", "把日程排太满"),
    },
    # 无生辰画像 → 按今日日干五行的通用签
    "generic": {
        "label": "平衡日", "tone": "顺着今天的节奏走，稳中有进",
        "do": ("把最重要的一件事先做完", "给自己一点独处时间", "散步或伸展十分钟",
               "跟在意的人说说话", "早点休息"),
        "avoid": ("同时开太多线", "情绪上头时做决定", "熬夜"),
    },
}

# 五行 → 幸运色池（取今日日干五行；确定性挑一）
_WUXING_COLORS = {
    "木": ("绿色", "青色", "薄荷色"),
    "火": ("红色", "橘色", "粉色"),
    "土": ("黄色", "米色", "大地色"),
    "金": ("白色", "金色", "银灰色"),
    "水": ("蓝色", "黑色", "藏青色"),
}

_WUXING_FLAVOR = {
    "木": "生长与舒展", "火": "热络与行动", "土": "沉稳与承载",
    "金": "清晰与决断", "水": "流动与思考",
}


def _pick(pool, seed: int, salt: int = 0) -> str:
    if not pool:
        return ""
    return pool[(seed + salt) % len(pool)]


def daily_card(
    *,
    day_master_gan: str = "",
    seed_key: str = "",
    now_ts: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    """生成某人今日灵签；缺 lunar_python → None（软失败）。

    ``day_master_gan``：用户日主天干（有生辰画像时传入 → 个性化能量日）；
    ``seed_key``：用户记忆键（宜忌轮转的确定性种子，同日同人恒定）。
    """
    gz = day_ganzhi(now_ts)
    if len(gz) != 2:
        return None
    lt = time.localtime(now_ts if now_ts is not None else time.time())
    ymd = f"{lt.tm_year:04d}{lt.tm_mon:02d}{lt.tm_mday:02d}"
    seed = zlib.crc32(f"{ymd}:{seed_key}".encode("utf-8"))

    day_gan = gz[0]
    day_wx = (GAN_INFO.get(day_gan) or ("",))[0]
    shishen = ""
    if day_master_gan in GAN_INFO:
        shishen = shishen_between(day_master_gan, day_gan)
        energy = _SHISHEN_TO_ENERGY.get(shishen, "generic")
    else:
        energy = "generic"
    meta = _ENERGY_META[energy]

    return {
        "date": ymd,
        "day_ganzhi": gz,
        "day_wuxing": day_wx,
        "wuxing_flavor": _WUXING_FLAVOR.get(day_wx, ""),
        "jieqi": current_jieqi(now_ts),
        "personalized": bool(shishen),
        "shishen": shishen,
        "energy": energy,
        "energy_label": meta["label"],
        "tone": meta["tone"],
        "do": _pick(meta["do"], seed),
        "avoid": _pick(meta["avoid"], seed, salt=1),
        "lucky_color": _pick(_WUXING_COLORS.get(day_wx, ()), seed, salt=2),
        "lucky_number": (seed % 9) + 1,
    }


def format_daily_card(card: Dict[str, Any]) -> str:
    """灵签 → 紧凑事实行（供 prompt 注入）。"""
    if not isinstance(card, dict) or not card.get("day_ganzhi"):
        return ""
    parts = [f"今日{card['day_ganzhi']}日"]
    if card.get("day_wuxing"):
        parts.append(f"{card['day_wuxing']}气当值（{card.get('wuxing_flavor', '')}）")
    if card.get("jieqi"):
        parts.append(f"节气·{card['jieqi']}")
    head = "，".join(parts)
    lines = [head]
    if card.get("personalized"):
        lines.append(
            f"对TA是「{card.get('energy_label')}」（今日日干对TA日主为{card.get('shishen')}）"
            f"：{card.get('tone')}")
    else:
        lines.append(f"通用签「{card.get('energy_label')}」：{card.get('tone')}")
    lines.append(
        f"宜：{card.get('do')}；不宜：{card.get('avoid')}；"
        f"幸运色：{card.get('lucky_color')}；幸运数字：{card.get('lucky_number')}")
    return "\n".join(lines)


def build_daily_card_block(card: Dict[str, Any]) -> str:
    """聊天注入块：签面事实 + 展开守则（安全红线由外层命理块统一注入，这里不重复）。"""
    s = format_daily_card(card)
    if not s:
        return ""
    return (
        "【今日灵签 · 内部参考，用人设口吻像翻开一张签那样自然聊】\n"
        f"{s}\n"
        "展开守则：签面是「今天的能量倾向」不是命令——轻松聊、给具体可做的小建议；"
        "对方今天心情不好时先顺着情绪、签面点到为止；不要报「大凶大吉」式断言。"
    )


def ritual_card_line(card: Dict[str, Any]) -> str:
    """早安 ritual 附加行：把灵签压成一句「顺手翻签」的开场素材（克制，一句带过）。"""
    if not isinstance(card, dict) or not card.get("day_ganzhi"):
        return ""
    if card.get("personalized"):
        core = f"今天对TA是「{card.get('energy_label')}」，宜{card.get('do')}"
    else:
        core = f"今天{card.get('day_wuxing')}气当值，宜{card.get('do')}"
    return (
        f"可以像顺手替TA翻了张今日签那样自然带一句：{core}"
        "（一句带过、别展开成运势播报、别加吉凶断言）。"
    )


__all__ = [
    "daily_card",
    "format_daily_card",
    "build_daily_card_block",
    "ritual_card_line",
]
