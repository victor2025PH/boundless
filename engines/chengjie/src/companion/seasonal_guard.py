# -*- coding: utf-8 -*-
"""季节/节令日历守卫（2026-08-18 「迎新表演」事故沉淀）。

实锤事故（2026-08-18 20:51，telegram:8244899900:5433982810）：主动开场发出
「诶我今天被拉去社团排迎新表演了，让我跳一小段舞😂」——8 月中旬还在暑假，
迎新季要到 9 月，客户一眼识破「时间明显不符」。根因＝人设 ``life_arc.beats``
是**无日历属性的静态素材池**（迎新排练/期末小组报告与暑假攻略混在一个池里
按天轮播），挑选层与生成层都没有「今天是几月几号」的接地。

三层修法（本模块承担 ①②的判定，纯函数零 IO）：
  ① 素材层：``pick_life_beat`` 挑节拍前按 ``season_conflict`` 过滤——带明确
     节令词且今天不在其时窗内的节拍**今天不选**（到了 9 月迎新节拍自动恢复，
     不必删运营数据）；
  ② 出站层：``present_claim_season_conflict`` 只抓「现在时态 + 节令词 + 出窗」
     的**同子句**组合（「今天被拉去排迎新」拦；「上次圣诞见你」「离圣诞还有
     四个月」不拦——回忆与展望是合法人话，零误伤优先）；
  ③ 生成层：build_proactive_prompt 注入真实日期行（正向接地，防 LLM 自由
     发挥时编出反季活动）——在 proactive_prompt.py，不在本模块。

词表纪律（与 daily_topics 屏蔽表同哲学：宁可漏判不误伤）：
  - 只收**高置信强节令词**（迎新/军训/圣诞/春节/高考…），不收「下雪/游泳」这类
    地域依赖词（海南冬泳、北欧夏雪都合法）；
  - 时窗刻意放宽到「筹备期也算在窗内」（12 月初聊圣诞、8 月底聊迎新都是人话），
    只拦明显出窗；农历节按公历宽窗覆盖（春节 1/10-3/1 盖住所有年份）。
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any, List, Optional, Sequence, Tuple

__all__ = [
    "SEASONAL_WINDOWS",
    "in_window",
    "season_conflict",
    "present_claim_season_conflict",
    "greeting_time_conflict",
    "filter_seasonal_beats",
]

# (节令词组, ((起 MMDD, 止 MMDD), ...))——起 > 止 表示跨年窗（如寒假 1220→0301）。
# 窗口含两端；同组词共享时窗。
SEASONAL_WINDOWS: Tuple[Tuple[Tuple[str, ...], Tuple[Tuple[int, int], ...]], ...] = (
    (("迎新", "新生报到", "军训", "开学典礼"), ((825, 1010),)),
    (("开学",), ((210, 315), (820, 930))),
    (("期末考", "期末周", "备战期末", "期末复习"), ((601, 715), (1210, 120))),
    (("高考",), ((520, 620),)),
    (("圣诞", "平安夜"), ((1120, 1231),)),
    (("跨年", "元旦"), ((1215, 107),)),
    (("春节", "过年", "年夜饭", "拜年", "春晚", "抢红包"), ((110, 301),)),
    (("中秋", "月饼"), ((820, 1020),)),
    (("万圣节",), ((1010, 1105),)),
    (("暑假",), ((615, 910),)),
    (("寒假",), ((1220, 301),)),
    (("樱花", "赏樱"), ((215, 501),)),
    (("双十一", "双11"), ((1020, 1120),)),
)

# 现在时态标记（出站层专用）：只有「此刻/近日正在发生」的断言才可能穿帮；
# 「最近」刻意不收——「最近在想圣诞去哪玩」是合法的展望人话。
_PRESENT_MARKERS: Tuple[str, ...] = (
    "今天", "今晚", "今早", "现在", "正在", "这几天", "昨天", "昨晚",
    "明天", "刚从", "刚去", "刚被", "被拉去", "在忙着",
)

# 子句切分：句读/换行/常见 emoji 边界——现在时标记与节令词必须同子句才算断言
_CLAUSE_SPLIT_RE = re.compile(r"[。！？!?；;\n\r]+|，|,")


def _mmdd(now: Any) -> int:
    """datetime/时间戳 → MMDD 整数；解析失败返回 0（调用方按放行处理）。"""
    try:
        if isinstance(now, datetime):
            return now.month * 100 + now.day
        import time as _t
        lt = _t.localtime(float(now))
        return lt.tm_mon * 100 + lt.tm_mday
    except Exception:
        return 0


def in_window(mmdd: int, window: Tuple[int, int]) -> bool:
    """MMDD 是否落在（含两端的）时窗内；起 > 止 为跨年窗。"""
    start, end = int(window[0]), int(window[1])
    if start <= end:
        return start <= mmdd <= end
    return mmdd >= start or mmdd <= end  # 跨年


def season_conflict(text: str, now: Any) -> str:
    """文本含节令词且今天在其全部时窗之外 → 返回命中的节令词；否则 ""。

    素材层口径（生活节拍是「我正在过的日子」的第一人称陈述，命中即冲突，
    无需现在时标记）。日期解析失败 → ""（放行＝旧行为，守卫不做新故障源）。
    """
    t = str(text or "")
    if not t:
        return ""
    today = _mmdd(now)
    if not today:
        return ""
    for words, windows in SEASONAL_WINDOWS:
        for w in words:
            if w in t and not any(in_window(today, win) for win in windows):
                return w
    return ""


def present_claim_season_conflict(text: str, now: Any) -> str:
    """出站文案层：**同一子句**内「现在时标记 + 出窗节令词」→ 返回节令词；否则 ""。

    刻意窄：回忆（「上次圣诞」）、展望（「离圣诞还有四个月」「圣诞想去北海道」）
    都没有现在时标记，永不误伤；跨子句组合（「今天上班。圣诞的事以后说」）
    也不拦——只有「今天在做出窗节令的事」这种当场穿帮才出手。
    """
    t = str(text or "")
    if not t:
        return ""
    today = _mmdd(now)
    if not today:
        return ""
    for clause in _CLAUSE_SPLIT_RE.split(t):
        if not clause:
            continue
        if not any(m in clause for m in _PRESENT_MARKERS):
            continue
        for words, windows in SEASONAL_WINDOWS:
            for w in words:
                if w in clause and not any(
                        in_window(today, win) for win in windows):
                    return w
    return ""


# ── 问候词 × 时刻守卫（2026-08-19「上午 10:12 晚安」事故第四层）──────────────
# 时钟层已收口（schedule_clock trust=replace-only），这里是**内容层**最后防线：
# 无论上游因为什么产出「问候词与收件人时刻矛盾」的文案（LLM 在下午问候里
# 自由发挥写晚安 / 未来任何排程回归 / 显式时区本身配错），发送前都拦一道。
# 锚定句首/句尾（真实问候行为的位置）——「今晚安排了什么」中缀不误伤。

_GREETING_EDGE_RULES: Tuple[Tuple[str, "re.Pattern", "re.Pattern", frozenset], ...] = (
    (
        "晚安",
        re.compile(r"^[\s]*晚安"),
        re.compile(r"晚安[\s，。！!~～、…\U0001F000-\U0001FAFF\u2600-\u27BF]*$"),
        frozenset(list(range(19, 24)) + list(range(0, 6))),
    ),
    (
        "早安",
        re.compile(r"^[\s]*(早安|早上好)"),
        re.compile(r"(早安|早上好)[\s，。！!~～、…\U0001F000-\U0001FAFF\u2600-\u27BF]*$"),
        frozenset(range(4, 12)),
    ),
    (
        "good night",
        re.compile(r"(?i)^\s*good\s*night\b"),
        re.compile(r"(?i)\bgood\s*night[\s.!~😴🌙💤]*$"),
        frozenset(list(range(19, 24)) + list(range(0, 6))),
    ),
    (
        "good morning",
        re.compile(r"(?i)^\s*good\s*morning\b"),
        re.compile(r"(?i)\bgood\s*morning[\s.!~☀️🌞]*$"),
        frozenset(range(4, 12)),
    ),
)


def greeting_time_conflict(text: str, hour: Any) -> str:
    """句首/句尾的问候词与该小时矛盾 → 返回问候词；否则 ""。

    ``hour``＝**收件人视角**的小时（排程修复后 plan.local_hour = 服务器钟或
    经显式信号核实的用户钟）。窗口刻意宽（晚安 19:00-05:59 / 早安 4:00-11:59
    都算合法）——只拦「上午说晚安」级的当场穿帮，不管风格问题。
    非法 hour → ""（守卫不做新故障源）。
    """
    t = str(text or "")
    if not t:
        return ""
    try:
        h = int(hour)
    except (TypeError, ValueError):
        return ""
    if not (0 <= h <= 23):
        return ""
    for label, head_re, tail_re, allowed in _GREETING_EDGE_RULES:
        if h in allowed:
            continue
        if head_re.search(t) or tail_re.search(t):
            return label
    return ""


def filter_seasonal_beats(
    beats: Sequence[str], now: Any,
) -> List[str]:
    """生活节拍池按当日过滤（出窗节令节拍今天不进池；到窗内自动恢复）。

    全部被滤空时**如实返回空**（上层按「今天没有生活节拍」处理，比硬选一条
    反季节拍诚实）；过滤自身异常 → 原池放行（旧行为）。
    """
    try:
        return [b for b in (beats or []) if not season_conflict(b, now)]
    except Exception:
        return list(beats or [])
