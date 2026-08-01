"""出站近重复守卫（2026-07-31，198 实锤三连发的投递侧防线）。

事故：坐席在回复工坊「生成草稿 → 填入并发送」连点三次，同一条客户消息在 43 秒内
收到三条同义改写（『Haha, that's true. …』×3）；一分钟后又把客户**已经回答过**的
问题原样再发一遍。生成层的防复读（anti-repeat 窗口比对 + 换角度重生）管不住这类
「每次重生都被发出」的人为/流程重复——需要**投递前**对「最近刚发过什么」做最后一道
核对。

设计（纯函数 + 进程级计数，风格对齐 ``reply_split``）：
  - ``near_duplicate_of_recent(text, recent_rows)``：与窗口内最近**出站**消息比对，
    返回命中详情或 None。两档判定（阈值按 198 实录金标校准）：
      * ``dup``     —— 归一化后相等 / SequenceMatcher ≥ 0.90 / 旧消息整体被包含
                       （对应「同一句原样再发」）；
      * ``similar`` —— 共同前缀 ≥ 12（归一化字符，实录三连发共同开头
                       『hahathatstrue』=13）**或** 相似度 ≥ 0.78
                       （对应「换角度重生的同义改写」——尾部各异，靠开头/高重合抓）。
  - **只做建议，不做决定**：路由把命中转成 409 + 前端确认框（坐席仍可强发），
    绝不静默吞掉人的明确意图；过短文本（归一化 < 12 字符）永不判——「好的」「嗯嗯」
    重复发送是正常聊天。
  - 判定基于**发出去的文本**（翻译后），与库里最近出站行同一口径。
"""

from __future__ import annotations

import re
import threading
import time
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional

# 归一化：去空白 + 常见标点/emoji 噪声，小写化——「换个标点/emoji」不算新内容
_STRIP_RE = re.compile(
    r"[\s,.!?;:~·、，。！？；：…\"'“”‘’()（）\[\]【】\-—_*]+"
)
_EMOJI_RE = re.compile(r"[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F]")

DEFAULT_WINDOW_SEC = 180.0
DEFAULT_MIN_LEN = 12          # 归一化后短于此永不判（正常聊天口头禅）
DEFAULT_DUP_RATIO = 0.90
DEFAULT_SIMILAR_RATIO = 0.78
DEFAULT_SIMILAR_PREFIX = 12   # 共同前缀（归一化字符）达到即认「同义开头」
DEFAULT_CONTAIN_MIN = 24      # 旧消息整体被新文本包含时，旧消息至少要这么长


def normalize_for_dup(text: str) -> str:
    """比对用归一化：去空白/标点/emoji + 小写。"""
    s = _EMOJI_RE.sub("", str(text or ""))
    s = _STRIP_RE.sub("", s)
    return s.lower()


def _common_prefix_len(a: str, b: str) -> int:
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return i


def near_duplicate_of_recent(
    text: str,
    recent_rows: Optional[List[Dict[str, Any]]],
    *,
    now: Optional[float] = None,
    window_sec: float = DEFAULT_WINDOW_SEC,
    min_len: int = DEFAULT_MIN_LEN,
    dup_ratio: float = DEFAULT_DUP_RATIO,
    similar_ratio: float = DEFAULT_SIMILAR_RATIO,
    similar_prefix: int = DEFAULT_SIMILAR_PREFIX,
) -> Optional[Dict[str, Any]]:
    """待发文本是否与窗口内最近出站消息近重复。

    recent_rows：store.list_recent_messages 产物（含 direction/text/ts）。
    命中返回 ``{level, similarity, age_sec, matched_text}``，否则 None。
    防御式：任何字段缺失/异常按「不命中」处理（守卫绝不阻断正常发送）。
    """
    cand = normalize_for_dup(text)
    if len(cand) < int(min_len):
        return None
    now = float(now if now is not None else time.time())
    best: Optional[Dict[str, Any]] = None
    for row in reversed(list(recent_rows or [])):
        try:
            if not isinstance(row, dict):
                continue
            if str(row.get("direction") or "") != "out":
                continue
            ts = float(row.get("ts") or 0.0)
            if ts <= 0 or now - ts > float(window_sec):
                continue
            prev_raw = str(row.get("text") or "")
            prev = normalize_for_dup(prev_raw)
            if len(prev) < int(min_len):
                continue
            ratio = SequenceMatcher(None, cand, prev).ratio()
            level = ""
            if cand == prev or ratio >= float(dup_ratio):
                level = "dup"
            elif len(prev) >= DEFAULT_CONTAIN_MIN and prev in cand:
                # 旧消息整体被新文本包含：把刚问过的问题拼在新话里原样再问
                level = "dup"
            elif (_common_prefix_len(cand, prev) >= int(similar_prefix)
                    or ratio >= float(similar_ratio)):
                level = "similar"
            if not level:
                continue
            hit = {
                "level": level,
                "similarity": round(ratio, 3),
                "age_sec": max(0.0, round(now - ts, 1)),
                "matched_text": prev_raw[:120],
            }
            # dup 强于 similar；同级取相似度更高者
            if (best is None
                    or (hit["level"] == "dup" and best["level"] != "dup")
                    or (hit["level"] == best["level"]
                        and hit["similarity"] > best["similarity"])):
                best = hit
        except Exception:
            continue
    return best


# ── 观测（进程级计数，autosend-status / metrics 可挂）────────────────────────
_STATS_LOCK = threading.Lock()
_STATS: Dict[str, int] = {"checked": 0, "hit_dup": 0, "hit_similar": 0, "forced": 0}


def record_dup_check(level: str = "", *, forced: bool = False) -> None:
    """记一次守卫判定：level ∈ ''(未命中)|dup|similar；forced=坐席确认后强发。"""
    with _STATS_LOCK:
        _STATS["checked"] += 1
        if level == "dup":
            _STATS["hit_dup"] += 1
        elif level == "similar":
            _STATS["hit_similar"] += 1
        if forced:
            _STATS["forced"] += 1


def dup_guard_metrics_snapshot() -> Dict[str, int]:
    with _STATS_LOCK:
        return dict(_STATS)


__all__ = [
    "near_duplicate_of_recent",
    "normalize_for_dup",
    "record_dup_check",
    "dup_guard_metrics_snapshot",
]
