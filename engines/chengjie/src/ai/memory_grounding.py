"""记忆抽取接地护栏 — 事实必须锚定在用户原话上（防幻觉自我强化）。

真实事故（2026-07-13，telegram 5433982810）：LLM 记忆抽取器把 **AI 自己回复里的
臆测**存成了"用户事实"——AI 问「你明天是不是不用上班啊？」→ 抽出「用户明天不用
上班」；AI 说「你刚才说想去大阪玩」（本身是幻觉）→ 抽出「用户想去大阪玩」。
假记忆下轮注入 prompt → AI 更确信 → 复读幻觉 → 用户："你精神错乱了吗"。

防线（宁可漏记不可错记——漏一条记忆无感，错一条记忆是"精神错乱"事故）：
  - 抽出的每条事实必须与**用户消息**有内容级词汇重叠（CJK bigram / 拉丁词 / 数字）；
  - 只在助手回复里出现、用户从没说过的内容 → 丢弃；
  - 意译到零词汇重叠的极端 legit 案例会被误丢（可接受的保守代价，prompt 侧同时
    硬化要求 LLM 引用用户原词）。

纯函数、零依赖、可单测。
"""
from __future__ import annotations

import re
from typing import List, Tuple

# 事实开头的主语样板（剥掉后再取内容词，防止「用户」二字本身参与匹配）
_FACT_PREFIX_RE = re.compile(r"^(用户|对方|客户|他|她|TA)[:：\s]*")
_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]+")
_LATIN_NUM_RE = re.compile(r"[A-Za-z][A-Za-z']{1,}|\d{2,}")


def _content_tokens(text: str) -> Tuple[set, set]:
    """文本 → (CJK bigram 集合, 拉丁词/数字 token 集合)。

    bigram 只在连续 CJK 段内生成（不跨标点/空格）；拉丁词统一小写、≥2 字符；
    数字 ≥2 位（单字符/单位数噪声大）。
    """
    bigrams: set = set()
    for run in _CJK_RUN_RE.findall(str(text or "")):
        for i in range(len(run) - 1):
            bigrams.add(run[i:i + 2])
    latin = {t.lower() for t in _LATIN_NUM_RE.findall(str(text or ""))}
    return bigrams, latin


def fact_grounded_in_user_msg(fact: str, user_msg: str) -> bool:
    """一条抽取事实是否锚定在用户原话上（任一内容 token 重叠即接地）。

    - fact 剥掉「用户/对方…」主语前缀后取内容 token；
    - 与 user_msg 的 token 集合有交集（CJK bigram 或 拉丁/数字 token）→ True；
    - fact 本身取不出内容 token（超短/纯符号）→ 保守放行 True（无从判断）。
    """
    f = _FACT_PREFIX_RE.sub("", str(fact or "").strip())
    f_bi, f_latin = _content_tokens(f)
    if not f_bi and not f_latin:
        return True
    u_bi, u_latin = _content_tokens(user_msg)
    if f_bi & u_bi:
        return True
    if f_latin & u_latin:
        return True
    return False


def filter_grounded_facts(
    facts: List[str], user_msg: str,
) -> Tuple[List[str], List[str]]:
    """批量过滤：返回 (接地保留的, 被丢弃的)。防御式，绝不抛。"""
    kept: List[str] = []
    dropped: List[str] = []
    for f in facts or []:
        try:
            (kept if fact_grounded_in_user_msg(f, user_msg) else dropped).append(f)
        except Exception:
            kept.append(f)  # 判定器自身异常 → 放行（回到旧行为）
    return kept, dropped


__all__ = ["fact_grounded_in_user_msg", "filter_grounded_facts"]
