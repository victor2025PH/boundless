"""出生信息（生辰）抽取与落库文本（确定性纯函数）——八字命理技能的取数底座。

与 ``src/utils/birthday.py`` 的分工：birthday 只管 (月,日) 按年庆生；本模块管**排盘所需**的
完整生辰（年/月/日 + 可选时辰 + 公历/农历 + 可选性别）。两者并存互不替代——一条
「用户的出生信息：1995年3月5日…」事实同时可被 ``extract_birthday`` 解出 (3,5) 供生日仪式用。

保守原则（同 birthday.py）：**必须命中出生/命理关键词**才解析——避免把「3月5日开会」
误当生辰。裸日期答复（用户被问后只回「1995年3月5日早上8点」无关键词）靠 AI 回复复述
确认再抽（``birth_info_from_turn`` 双路，与生日 Stage S 同机制，零误报）。

刻意不做（保守，宁漏勿错）：中文数字农历日期（「二月初五」）、闰月标注、公元前。
"""

from __future__ import annotations

import re
from typing import Any, Optional

from src.companion.bazi_engine import BirthInfo

# ── 关键词门控 ────────────────────────────────────────────────────────────────
# 出生系（直接谈生辰）或命理系（算命语境下报日期几乎必为生辰）任一命中才解析。
_BIRTH_KW = re.compile(
    r"出生|生日|生辰|生于|生於|生人|生の|born|birth\s*day|"
    r"[日号號]\s*生|[点點时時辰]\s*生|"  # 「5月2日生的」「辰时生」——「生」紧跟日期/时辰单位
    r"八字|排盘|排盤|算命|命盘|命盤|批命|看命|测命|測命|紫微|斗数|斗數|命理",
    re.IGNORECASE,
)
_LUNAR_KW = re.compile(r"农历|農曆|阴历|陰曆|旧历|舊曆|lunar", re.IGNORECASE)

# ── 日期（须年月日齐全；仅 (月,日) 归 birthday.py 管） ──────────────────────────
# 1) 1995年3月5日/号（「日/号」可省）
_CN_YMD = re.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*[日号號]?")
# 2) 95年3月5日（两位年须带「日/号」增强置信）
_CN_YY_MD = re.compile(r"(?<!\d)(\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*[日号號]")
# 3) 1995-03-05 / 1995.3.5 / 1995/3/5（可跟 08:30）
_YMD = re.compile(
    r"(?<!\d)(\d{4})\s*[-/.]\s*(\d{1,2})\s*[-/.]\s*(\d{1,2})"
    r"(?:\s+(\d{1,2}):(\d{2}))?"
)

# ── 时辰 ─────────────────────────────────────────────────────────────────────
# 传统时辰名 → 代表小时（各时辰跨 2 小时，取区间内任一小时时柱相同；子时取早子 0 点）
_SHICHEN = {
    "子": 0, "丑": 1, "寅": 3, "卯": 5, "辰": 7, "巳": 9,
    "午": 11, "未": 13, "申": 15, "酉": 17, "戌": 19, "亥": 21,
}
_SHICHEN_RE = re.compile(r"([子丑寅卯辰巳午未申酉戌亥])\s*时")
# 「早上8点/下午3点半/晚上10点20分/8点」——段落词决定 12 小时制换算
_HOUR_RE = re.compile(
    r"(凌晨|清晨|早上|早晨|上午|中午|正午|下午|午后|午後|傍晚|晚上|夜里|夜裡|夜晚|深夜)?"
    r"\s*(\d{1,2})\s*[点點时時]\s*(半)?\s*(?:(\d{1,2})\s*分)?"
)
_EVENING = ("下午", "午后", "午後", "傍晚", "晚上", "夜里", "夜裡", "夜晚")
_HOUR_UNKNOWN_RE = re.compile(r"时辰未知|時辰未知")

# ── 第三人称护栏 ──────────────────────────────────────────────────────────────
# 「我男朋友是1993年5月2日出生的」是在给**别人**报生辰——落库成本人生辰是画像污染
# （排出来的盘全错）。命中亲友称谓 → 本模块整条不抽（他人盘/合盘属 Phase 2 多档案）。
_THIRD_PARTY_RE = re.compile(
    r"男朋友|女朋友|男友|女友|老公|老婆|丈夫|妻子|前任|"
    r"我妈|我爸|妈妈|爸爸|母亲|父亲|我哥|我姐|我弟|我妹|哥哥|姐姐|弟弟|妹妹|"
    r"儿子|女儿|孩子|小孩|宝宝|"
    r"我朋友|同事|同学|闺蜜|兄弟|室友|"
    r"boyfriend|girlfriend|husband|wife|my mom|my dad|my friend|my son|my daughter",
    re.IGNORECASE,
)

# ── 性别 ─────────────────────────────────────────────────────────────────────
_GENDER_PATTERNS = (
    re.compile(r"性别\s*[:：]?\s*(男|女)"),
    re.compile(r"性別\s*[:：]?\s*(男|女)"),
    re.compile(r"我是\s*(男|女)"),
    re.compile(r"(男|女)命"),
    # 分隔符包围的独立性别词（「1995年3月5日早上8点，女生」这类报生辰顺带报性别的
    # 真实答复格式）；句中修饰他人的（「见了个男生朋友」）无分隔边界不误触。
    re.compile(r"(?:^|[,，、;；.。\s])(男生|女生|男|女)(?:$|[,，、;；.。!！?？\s])"),
)
_GENDER_BARE = {"男": "male", "女": "female", "男生": "male", "女生": "female",
                "男的": "male", "女的": "female", "男孩": "male", "女孩": "female"}


def _valid_ymd(y: int, m: int, d: int) -> bool:
    return 1900 <= y <= 2100 and 1 <= m <= 12 and 1 <= d <= 31


def _expand_yy(yy: int) -> int:
    """两位年：50-99 → 19xx；00-30 → 20xx（生辰场景不会有 2031+ 的成年人）。"""
    return 1900 + yy if yy >= 50 else 2000 + yy


def _extract_hour(t: str) -> tuple:
    """(hour, minute)；解析不出 → (-1, 0)。"""
    m = _SHICHEN_RE.search(t)
    if m:
        return (_SHICHEN[m.group(1)], 0)
    m = _HOUR_RE.search(t)
    if not m:
        return (-1, 0)
    period = m.group(1) or ""
    try:
        h = int(m.group(2))
    except (TypeError, ValueError):
        return (-1, 0)
    minute = 30 if m.group(3) else 0
    if m.group(4):
        try:
            minute = int(m.group(4))
        except (TypeError, ValueError):
            pass
    if h == 12 and period in _EVENING:
        return (-1, 0)  # 「晚上12点」歧义（23:59 vs 00:00 跨日柱）→ 宁缺勿错
    if period in _EVENING and 1 <= h <= 11:
        h += 12
    elif period == "深夜" and 7 <= h <= 11:
        h += 12  # 深夜8点=20；深夜1点=1（凌晨习称）
    if not (0 <= h <= 23) or not (0 <= minute <= 59):
        return (-1, 0)
    return (h, minute)


def extract_gender(text: Any) -> str:
    """从文本保守抽性别："male"/"female"/""。裸答（「女」）仅限超短消息。"""
    t = str(text or "").strip()
    if not t:
        return ""
    bare = _GENDER_BARE.get(t)
    if bare:
        return bare
    for pat in _GENDER_PATTERNS:
        m = pat.search(t)
        if m:
            return "male" if m.group(1).startswith("男") else "female"
    return ""


def extract_birth_info(text: Any) -> Optional[BirthInfo]:
    """从一条文本保守抽完整生辰；无关键词或年月日不齐 → None。

    时辰/性别可缺（缺则 BirthInfo.hour=-1 / gender=""）；「农历」字样 → is_lunar。
    """
    t = str(text or "").strip()
    if len(t) < 4 or len(t) > 400 or not _BIRTH_KW.search(t):
        return None
    if _THIRD_PARTY_RE.search(t):
        return None  # 给亲友报生辰 → 不当本人画像（他人盘/合盘属后续多档案能力）

    y = mo = da = None
    hh, mi = -1, 0

    m = _CN_YMD.search(t)
    if m:
        y, mo, da = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if y is None:
        m = _CN_YY_MD.search(t)
        if m:
            y, mo, da = _expand_yy(int(m.group(1))), int(m.group(2)), int(m.group(3))
    if y is None:
        m = _YMD.search(t)
        if m:
            y, mo, da = int(m.group(1)), int(m.group(2)), int(m.group(3))
            if m.group(4) is not None:
                try:
                    _h, _m = int(m.group(4)), int(m.group(5))
                    if 0 <= _h <= 23 and 0 <= _m <= 59:
                        hh, mi = _h, _m
                except (TypeError, ValueError):
                    pass
    if y is None or not _valid_ymd(y, mo, da):
        return None

    if hh < 0 and not _HOUR_UNKNOWN_RE.search(t):
        hh, mi = _extract_hour(t)

    return BirthInfo(
        year=y, month=mo, day=da, hour=hh, minute=mi,
        is_lunar=bool(_LUNAR_KW.search(t)),
        gender=extract_gender(t),
    )


def birth_info_from_turn(user_msg: Any, reply: Any) -> Optional[BirthInfo]:
    """从一轮对话抽生辰（Stage S 同机制）：用户原话优先，其次 AI 复述确认。

    用户裸报日期（无关键词）不命中路 1；AI 按 directive 复述「记住啦，你是
    1995年3月5日早上8点出生」→ 路 2 命中。性别若只在用户消息里（「女生」），
    以 AI 路解出的生辰为骨架、用户消息的性别补全。
    """
    info = extract_birth_info(user_msg)
    if info is not None:
        return info
    info = extract_birth_info(reply)
    if info is not None and not info.gender:
        g = extract_gender(user_msg)
        if g:
            info.gender = g
    return info


def birth_info_fact_text(info: BirthInfo) -> str:
    """规范化生辰记忆文案（含「出生」关键词，可被 extract_birth_info 复解析）。"""
    cal = "农历" if info.is_lunar else "公历"
    if info.hour_known():
        when = f"{int(info.hour)}时{int(info.minute)}分"
    else:
        when = "时辰未知"
    g = {"male": " 性别男", "female": " 性别女"}.get(str(info.gender or ""), "")
    return (f"用户的出生信息：{cal}{int(info.year)}年{int(info.month)}月"
            f"{int(info.day)}日 {when}出生{g}")


__all__ = [
    "extract_birth_info",
    "extract_gender",
    "birth_info_from_turn",
    "birth_info_fact_text",
]
