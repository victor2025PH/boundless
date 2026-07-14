"""命理话题检测 + prompt 注入块构建（确定性纯函数）——八字技能的对话接线层。

产品定位（对标 AuraMate「灵体对话」）：命理内容**融进人设对话**，不是切换成报告机器。
所以本技能不做「命中意图 → 短路输出排盘文本」，而是把命盘摘要作为**内部参考资料**注入
system prompt，由 LLM 以当前人设口吻自然展开——聊到哪说到哪，千人千面。

三种注入产物（同一 user_context 键 ``_bazi_block``，每轮至多其一）：
  1. 已知生辰 → 命盘参考块（盘面摘要 + 解读守则 + 安全红线）
  2. 未知生辰 → 顺势采集 directive（借「想算」的当口自然要生辰，转化率最高）
  3. 无命理话题 → 不注入（零打扰）

话题粘性：用户问过八字后的追问（「那我明年呢」）往往不含关键词——命中后 N 分钟内
持续注入，粘性窗口由调用方存续在 user_context（进程内会话状态，与剧情引擎同模式）。

安全红线固定注入（比 AuraMate 官网可见更严——本仓危机安全是最重门禁）：
不预言死亡/重病/灾祸时点、不做医疗/投资/法律指令性断言、低落情绪先共情。
"""

from __future__ import annotations

import re
import time
from typing import Any, Dict, Optional, Tuple

# ── 话题检测 ─────────────────────────────────────────────────────────────────
# 保守多语 marker：明确指向命理/运势咨询。单字/过泛词（如「命」「运」）刻意不收。
_TOPIC_MARKERS = (
    "八字", "算命", "命理", "排盘", "排盤", "命盘", "命盤", "批命", "看命",
    "测命", "測命", "命格", "运势", "運勢", "大运", "大運", "流年", "喜用神",
    "五行缺", "看五行", "五行属", "五行屬", "紫微", "斗数", "斗數", "合盘", "合盤",
    "生辰八字", "帮我算", "幫我算", "算一卦", "占卜", "塔罗", "塔羅",
    "星座运", "星座運", "今年运气", "今年運氣", "明年运气", "明年運氣",
    "人生k线", "人生曲线", "人生曲線", "运势曲线", "運勢曲線",
    "bazi", "fortune telling", "fortune-telling", "horoscope", "astrology",
    "read my fortune", "tell my fortune",
)

# 反向护栏：引用他人/否定语境不误触（保守，宁漏勿错——漏了还有粘性窗兜底）。
_NEGATIVE_MARKERS = ("不信这", "不信這", "别给我算", "別給我算", "别算", "別算")


def detect_bazi_topic(text: Any) -> bool:
    """本条消息是否在聊命理/求测（保守多语关键词；超长叙述不判）。"""
    t = str(text or "").strip().lower()
    if not t or len(t) > 300:
        return False
    if any(m in t for m in _NEGATIVE_MARKERS):
        return False
    return any(m in t for m in _TOPIC_MARKERS)


# ── 每日灵签意图 ─────────────────────────────────────────────────────────────
# 只收「明确求运势/求签」的表达；「今天适合…」这类日常安排话术刻意不收
# （「今天适合见面吗」是日程问题，掺命理内容会突兀）。
_DAILY_MARKERS = (
    "今日运势", "今天运势", "今日运气", "今天运气", "今日宜忌", "今天宜忌",
    "抽个签", "抽张签", "抽签", "求个签", "灵签", "靈簽",
    "今日靈籤", "今日签", "每日一签", "daily fortune", "today's fortune",
    "fortune today", "luck today",
)


def detect_daily_card_intent(text: Any) -> bool:
    """是否在求「今天」的运势/签（与整体命理话题分流：签走轻量灵签，不必全盘展开）。"""
    t = str(text or "").strip().lower()
    if not t or len(t) > 300:
        return False
    if any(m in t for m in _NEGATIVE_MARKERS):
        return False
    return any(m in t for m in _DAILY_MARKERS)


# ── 目标年份抽取（「明年怎么样」「2027年运势」→ 该年流年数据进盘面） ──────────────
_YEAR_RE = re.compile(r"(?<!\d)(19[5-9]\d|20[0-9]\d)\s*年?(?!\d)")
_REL_YEAR = {"今年": 0, "明年": 1, "后年": 2, "後年": 2, "大后年": 3, "大後年": 3,
             "next year": 1, "this year": 0}


def extract_target_year(text: Any, now_year: int) -> Optional[int]:
    """从消息里保守抽「问的是哪一年」；无明确年份 → None（不猜）。

    相对词优先级：大后年 > 后年 > 明年 > 今年（长词先匹配防「大后年」命中「后年」）；
    其次 4 位年份（1950-2099）。
    """
    t = str(text or "").strip().lower()
    if not t or len(t) > 300:
        return None
    for word in ("大后年", "大後年", "后年", "後年", "明年", "今年",
                 "next year", "this year"):
        if word in t:
            return int(now_year) + _REL_YEAR[word]
    m = _YEAR_RE.search(t)
    if m:
        return int(m.group(1))
    return None


# ── 人生 K 线意图（出图请求） ──────────────────────────────────────────────────
_KLINE_MARKERS = (
    "人生k线", "人生曲线", "人生曲線", "运势曲线", "運勢曲線", "运势走势",
    "運勢走勢", "运势图", "運勢圖", "命运曲线", "命運曲線", "画一下我的运势",
    "畫一下我的運勢", "k线图", "fortune chart", "fortune curve", "life curve",
    "life chart",
)


def detect_kline_intent(text: Any) -> bool:
    """是否在求「人生 K 线/运势曲线图」（出图请求，走媒体发送 Stage）。"""
    t = str(text or "").strip().lower()
    if not t or len(t) > 300:
        return False
    return any(m in t for m in _KLINE_MARKERS)


# ── 深度详批意图（付费闸门的判定点） ───────────────────────────────────────────
_DEEP_MARKERS = (
    "详批", "詳批", "详细批", "细批", "細批", "深度解读", "深度解讀", "详细讲讲",
    "詳細講講", "仔细讲讲", "仔細講講", "详细分析", "詳細分析", "展开讲", "展開講",
    "事业运", "事業運", "财运", "財運", "感情运", "感情運", "姻缘", "姻緣",
    "婚姻运", "婚姻運", "桃花运", "桃花運", "健康运", "健康運", "学业运", "學業運",
    "career luck", "wealth luck", "love luck", "marriage luck",
)


def detect_deep_reading_intent(text: Any) -> bool:
    """是否在求「详批/分领域深读」（免费闲聊与付费深度的分界）。"""
    t = str(text or "").strip().lower()
    if not t or len(t) > 300:
        return False
    return any(m in t for m in _DEEP_MARKERS)


def build_deep_reading_directive() -> str:
    """已解锁（或未开变现门控）→ 深度详批展开指令。"""
    return (
        "【详批模式】对方在求深入解读：结合上面的命盘与流年信息，就TA问的领域"
        "（事业/感情/财运/健康等，问哪个讲哪个）先给一句总判，再分 2-3 个具体面展开，"
        "每个面落到「什么阶段、注意什么、怎么做更顺」；有时间维度时结合当前大运与流年讲清"
        "「哪年偏顺、哪年该稳」。仍遵守上面的解读守则与安全红线。"
    )


def build_premium_upsell_directive(pitch_hint: str = "") -> str:
    """未解锁 + 变现门控开 → 免费轻量版 + 软引导（绝不硬拒——陪伴产品不甩脸子）。"""
    pitch = str(pitch_hint or "").strip()
    tail = (f"如果TA感兴趣，自然带一句：{pitch}" if pitch
            else "如果TA感兴趣，自然提一句详批是会员专属内容，语气像分享不像推销。")
    return (
        "【详批引导】对方在求深入解读，但详批是TA还没解锁的会员内容："
        "先就TA问的点给一句真诚的大方向（免费部分，别敷衍），"
        f"然后温柔说明更细的逐项详批要会员才能展开。{tail}"
        "对方不感兴趣就正常聊，绝不反复推销。"
    )


# ── 话题粘性（会话内状态，调用方持有 user_context） ───────────────────────────
_TOPIC_TS_KEY = "_bazi_topic_ts"


def touch_topic(user_context: Dict[str, Any], now: Optional[float] = None) -> None:
    user_context[_TOPIC_TS_KEY] = float(now if now is not None else time.time())


def topic_active(
    user_context: Dict[str, Any],
    *,
    sticky_minutes: float = 10.0,
    now: Optional[float] = None,
) -> bool:
    """命理话题是否仍在粘性窗口内（覆盖无关键词的追问轮）。"""
    ts = user_context.get(_TOPIC_TS_KEY)
    if not ts:
        return False
    try:
        ts = float(ts)
    except (TypeError, ValueError):
        return False
    n = float(now if now is not None else time.time())
    return 0 <= (n - ts) < float(sticky_minutes) * 60.0


# ── prompt 块构建 ─────────────────────────────────────────────────────────────
_SAFETY_RULES = (
    "安全红线（必须遵守）："
    "绝不预言死亡、重病、灾祸的具体时间或断言其必然发生；"
    "涉及健康就医、投资借贷、法律婚姻等重大决策只给参考视角，明确提醒仅供参考、"
    "不构成专业建议；对方情绪低落或有自伤倾向时，放下命理先共情陪伴，必要时按系统安全策略处理。"
)

_TONE_RULES = (
    "解读守则：用你当前人设的口吻像懂命理的朋友一样聊，说人话、轻松自然；"
    "别堆术语，提到术语（十神/喜用神等）就顺口用大白话解释；"
    "一次只聊对方当下问的点，别把整张盘倒给对方；"
    "多给「怎么做会更顺」的建设性视角，少下宿命论断言；"
    "强弱喜用是粗判参考，表述留有余地（「偏向」「倾向」），别说得斩钉截铁。"
)


def build_bazi_prompt_block(chart_summary: str, *, hour_known: bool = True,
                            has_dayun: bool = True) -> str:
    """已知生辰 → 命盘参考注入块。``chart_summary`` 来自 bazi_engine.format_chart_summary。"""
    s = str(chart_summary or "").strip()
    if not s:
        return ""
    caveats = []
    if not hour_known:
        caveats.append("时辰未知：时柱缺失，涉及时柱的部分如实说明「知道出生时间能看得更细」，可顺口问一句几点出生（对方不记得就算了）。")
    if not has_dayun:
        caveats.append("大运未排（缺性别信息）：聊到大运时可自然确认对方性别后再谈（阳男阴女顺逆不同）。")
    caveat_txt = ("\n" + "\n".join("- " + c for c in caveats)) if caveats else ""
    return (
        "【命理参考 · 内部资料，按守则自然融入对话，勿整段照搬】\n"
        "以下是**聊天对象本人**的命盘（TA 若在替亲友问盘：没有对方生辰看不了，"
        "如实说明并把话题带回 TA 自己即可）。\n"
        f"{s}{caveat_txt}\n"
        f"{_TONE_RULES}\n"
        f"{_SAFETY_RULES}"
    )


def build_birth_ask_directive(
    known_birthday: Optional[Tuple[int, int]] = None,
) -> str:
    """未知生辰 → 顺势采集 directive。已知 (月,日) 时体现「记得你生日」的贴心感。"""
    if known_birthday:
        try:
            mo, da = int(known_birthday[0]), int(known_birthday[1])
            remembered = (
                f"你记得对方生日是{mo}月{da}日——先自然提起这一点（体现你记得），"
                "再补问出生年份和大概几点出生、公历还是农历。"
            )
        except (TypeError, ValueError, IndexError):
            remembered = "顺势问对方的出生年月日、大概几点出生、公历还是农历。"
    else:
        remembered = "顺势问对方的出生年月日、大概几点出生、公历还是农历。"
    return (
        "【命理话题 · 需要生辰】对方在聊算命/运势，但你还不知道TA的完整生辰。"
        f"{remembered}"
        "像朋友帮忙看盘那样随口要信息，一次问完别拆成连环追问；"
        "对方不想给就自然带过，聊点轻松的运势话题（不排盘也能聊节气流年的普遍感受）。"
        "拿到生辰后自然复述一遍确认（如「记住啦，1995年3月5日早上8点出生的」）。"
    )


__all__ = [
    "detect_bazi_topic",
    "detect_daily_card_intent",
    "detect_deep_reading_intent",
    "detect_kline_intent",
    "extract_target_year",
    "topic_active",
    "touch_topic",
    "build_bazi_prompt_block",
    "build_birth_ask_directive",
    "build_deep_reading_directive",
    "build_premium_upsell_directive",
]
