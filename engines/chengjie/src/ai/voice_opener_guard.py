"""语音开场词会话级去重守卫（活人感 P0-2，2026-08-03「嘿病」修复）。

为什么需要：2026-08-03 生产实测（近 14 天 885 条出站、改写缓存 29 条）——同一会话
连续多条语音都以同一个感叹词开场（「嘿，」「哎呀，」「哈哈」…），29 条改写里 10+ 条
「嘿」开头；开场词同质化是**新的机械感**：单条听着都口语，连听三条就穿帮。根因是
口语化整条链（生成层口语版 / LLM 改写 / 规则档 lead filler / C+ 轻笑）全部是
**句级无状态**——谁都看不见「上一条用了什么开头」，跨消息重复只有专门记忆才能治。

设计（与 proactive_variety 变体守卫同族）：
  - **会话级注册表**：``variety_key``（platform:account:chat，由
    ``persona_voice.resolve_effective_voice_context`` 统一注入 voice_cfg）→ 最近 N 条
    语音的开场词环形记录。进程内、LRU 上限、线程安全；重启即清（可接受：守卫目标是
    「连续几条不同款」，跨重启的首条重复无伤大雅）。
  - **只拆不加**：命中重复只把开场词剥掉（丢弃语义近零的话语标记），绝不替换成
    别的词——替换=又一个「会重复的池子」。
  - **保守剥除**：开场词后必须跟分隔标点/空白才认（「啊？你说什么」的「啊？」是
    真实反应不动）；剥后剩文不足 4 字不剥（「嗯嗯，好」整条几乎就是开场词本身）。
  - **确定性**：给定同样的历史与文本，结果恒定（无随机数）——可测、可复现。
  - 记录发生在**决策之后**：剥掉后本条按「无开场词」入账，让下一条可以自然用词。

接线：``tts_pipeline._try_avatar_clone`` 口语化/C+ 微特征之后、送引擎之前，仅
``colloquial_lead=True``（整条消息 / 分条首条）时生效——分条第 2/3 条本就不加
开场词，也不该重复占历史窗。开关 ``avatar_voice.colloquial.opener_dedupe``
（默认开；它修的是缺陷，与 ``polish_hub_speak_text`` 剥连环假笑同性质）。

可单测纯函数：normalize_opener / leading_opener / strip_opener / should_strip。
"""
from __future__ import annotations

import re
import threading
from collections import OrderedDict, deque
from typing import Deque, Dict, List, Optional, Tuple

# ── 开场词词表 ────────────────────────────────────────────────────────────────
# 多字词优先匹配；全部是「话语标记」性质（剥掉不伤语义）。刻意不收「不过/所以/
# 然后」这类承接连接词——它们携带真实逻辑关系，剥了会伤句意。
_MULTI = (
    "诶嘿", "哎呀", "哎哟", "哎呦", "嘿嘿", "嗯嗯", "好啦", "好嘞",
    "行行行", "话说", "其实", "说起来", "对了", "哇塞", "天哪", "我的天",
)
_SINGLE = "嘿诶哇嗨唉嗯哦喔咦嚯"
# 开场词 + 必须的分隔（逗号/顿号/感叹/波浪/空白）+ 余文。哈/呵/嘻连字算一个词。
_OPENER_RE = re.compile(
    r"^\s*(?P<op>哈{2,}|呵{2,}|嘻{2,}|" +
    "|".join(re.escape(w) for w in _MULTI) +
    r"|[" + _SINGLE + r"])"
    r"(?P<sep>[，,、！!~～呀啊哟\s]+)"
)

# 注册表：variety_key -> 最近 N 条开场词（"" = 该条无开场词）
_REGISTRY: "OrderedDict[str, Deque[str]]" = OrderedDict()
_REGISTRY_CAP = 512
_HISTORY_LEN = 6            # 环形记录长度（>= 最大判定窗即可）
_LOCK = threading.Lock()


def normalize_opener(op: str) -> str:
    """开场词归一：连字笑声折叠（哈哈哈→哈哈），其余原样。纯函数。"""
    s = str(op or "").strip()
    if not s:
        return ""
    if set(s) == {"哈"}:
        return "哈哈"
    if set(s) == {"呵"}:
        return "呵呵"
    if set(s) == {"嘻"}:
        return "嘻嘻"
    return s


def leading_opener(text: str) -> Tuple[str, str]:
    """识别句首开场词。返回 ``(归一开场词, 剥除后的余文)``；未命中 → ``("", 原文)``。

    只认「开场词 + 分隔符」形态：「嘿，这回听清了」命中；「啊？你说什么」不命中
    （？不是分隔符——那是真实反应不是话语标记）。纯函数。
    """
    t = str(text or "")
    m = _OPENER_RE.match(t)
    if not m:
        return "", t
    rest = t[m.end():].lstrip("，,、！!~～ \t")
    return normalize_opener(m.group("op")), rest


def strip_opener(text: str, *, min_rest_chars: int = 4) -> Tuple[str, str]:
    """剥掉句首开场词。返回 ``(新文本, 被剥的归一开场词)``；不可剥 → ``(原文, "")``。

    剥后余文 < ``min_rest_chars`` 不剥（整条几乎就是开场词本身，剥了会变空/怪）。
    """
    op, rest = leading_opener(text)
    if not op:
        return str(text or ""), ""
    if len(rest.strip()) < max(1, int(min_rest_chars)):
        return str(text or ""), ""
    return rest, op


def should_strip(
    opener: str,
    history: List[str],
    *,
    window: int = 2,
    fatigue_streak: int = 3,
) -> bool:
    """是否剥除本条开场词。纯函数（历史显式传入）。

    - 重复规则：最近 ``window`` 条里出现过同一开场词 → 剥（连续同款=机械感主源）。
    - 疲劳规则：最近 ``fatigue_streak`` 条**全部**带开场词（不论哪个）→ 剥
      （条条有语气词开场，分布上同样假；剥一条让节奏喘口气）。
    """
    if not opener:
        return False
    h = [str(x or "") for x in (history or [])]
    if window > 0 and opener in h[-window:]:
        return True
    if fatigue_streak > 0 and len(h) >= fatigue_streak:
        tail = h[-fatigue_streak:]
        if all(tail):
            return True
    return False


def _history_for(key: str) -> Deque[str]:
    dq = _REGISTRY.get(key)
    if dq is None:
        dq = deque(maxlen=_HISTORY_LEN)
        _REGISTRY[key] = dq
    _REGISTRY.move_to_end(key)
    while len(_REGISTRY) > _REGISTRY_CAP:
        _REGISTRY.popitem(last=False)
    return dq


def guard_opener(
    key: str,
    text: str,
    *,
    window: int = 2,
    fatigue_streak: int = 3,
    min_rest_chars: int = 4,
) -> str:
    """主入口：按会话历史决定是否剥除本条开场词，并记录本条最终开场词。

    ``key`` 空 → 原样返回不记录（预渲染/试听等无会话上下文的合成不受影响）。
    防御式：任何异常返回原文，绝不阻塞合成。
    """
    t = str(text or "")
    k = str(key or "").strip()
    if not k or not t.strip():
        return t
    try:
        op, _rest = leading_opener(t)
        with _LOCK:
            dq = _history_for(k)
            hist = list(dq)
            out = t
            final_op = op
            if op and should_strip(
                    op, hist, window=window, fatigue_streak=fatigue_streak):
                stripped, removed = strip_opener(
                    t, min_rest_chars=min_rest_chars)
                if removed:
                    out = stripped
                    # 剥后余文可能还有第二层开场词（「嘿，哎呀，…」罕见但存在）：
                    # 按剥后形态重新识别，历史记录以真实发声的开头为准。
                    final_op, _ = leading_opener(out)
            dq.append(final_op)
        return out
    except Exception:
        return t


def peek_history(key: str) -> List[str]:
    """观测/测试用：某会话当前的开场词历史快照。"""
    with _LOCK:
        dq = _REGISTRY.get(str(key or "").strip())
        return list(dq) if dq else []


def reset_opener_state() -> None:
    """清空注册表（测试用）。"""
    with _LOCK:
        _REGISTRY.clear()


__all__ = [
    "normalize_opener", "leading_opener", "strip_opener", "should_strip",
    "guard_opener", "peek_history", "reset_opener_state",
]
