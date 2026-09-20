# -*- coding: utf-8 -*-
"""AI 质疑检测 + 应对提示（活人感闭环，2026-08-03，纯函数 + 进程级计数）。

背景：AI 味周报首跑实锤——14 天窗入站 5 条直接质疑（上一窗 2 条，翻倍），
客户原话包括「自然一点，一听就知道你是Ai」；而运行时对这类消息此前**零特殊
处理**，模型常见翻车＝机械否认三连 + 长篇自证 + 突然热情，反而坐实怀疑
（07-28 实录：「哈哈，看来我演技不行啊 / 我真不是什么AI啦」）。

本模块：
  - ``detect_ai_suspicion``：保守词表判定（direct=直接质疑身份 / flavor=机器味
    吐槽 / ""=无）。**宁可漏报不误报**——「AI绘画」「用AI做图」「机器学习」这类
    工具/话题提及绝不命中（负样本门禁钉住）。本文件是**单一事实源**：
    eval 侧（``ai_flavor_eval``）与周报 CLI 从这里 re-export，线上线下同口径。
  - ``build_suspicion_hint``：命中后注入 prompt 的应对要点。消费口＝
    ``inbound_enrich.apply_inbound_enrichments`` → ``_topic_switch_hint``
    （A 线 process_message / B 线 generate_inbox_draft 同一入口）。
    要点全部**反着常见翻车写**：别否认三连、别自证、别解释技术、别突然热情。
  - ``record_suspicion`` / ``suspicion_snapshot``：进程级计数 + 最近样本
    （为 ops 卡预留读口）。周报的权威口径仍是离线扫库，两者互不依赖——
    进程计数重启即清，只作「今天有没有人在质疑」的快速信号。
"""
from __future__ import annotations

import re
import threading
import time
from collections import deque
from typing import Any, Deque, Dict, Tuple

# ── 保守词表（与 2026-08-03 AI 味周报同源；改这里两侧同步生效）─────────────────
# direct＝直接质疑身份；flavor＝味道投诉（机器味/像机器人/念稿感）。
# 刻意不收：无第二人称锚点、无味道词的 AI 提及（聊工具/新闻不是质疑身份）。
_SUSPICION_DIRECT: Tuple[re.Pattern, ...] = (
    re.compile(r"你\s*(?:是不是|是|就是|真是)\s*一?个?\s*"
               r"(?:ai|人工智能|机器人|机器|bot|智能助手|假人)"),
    re.compile(r"(?:是不是|^是)\s*(?:ai|机器人|bot)\s*(?:吧|吗|\?|？|$)"),
    re.compile(r"真人\s*(?:吗|嘛|么|\?|？)"),
    re.compile(r"\bare\s+you\s+(?:a\s+|an\s+)?(?:bot|robot|ai)\b"),
    re.compile(r"\byou\s*'?\s*re\s+(?:a\s+|an\s+)?(?:bot|robot|ai)\b"),
)
_SUSPICION_FLAVOR: Tuple[re.Pattern, ...] = (
    re.compile(r"机器味|机械感|ai\s*味|像\s*个?\s*机器人|像\s*个?\s*ai\b|念稿"),
    re.compile(r"\bsounds?\s+like\s+(?:a\s+)?(?:bot|robot)\b"),
)


def detect_ai_suspicion(text: str) -> str:
    """入站文本是否在质疑「你是 AI/机器人」。返回 "direct" | "flavor" | ""。纯函数。"""
    s = str(text or "").strip().lower()
    if not s:
        return ""
    for pat in _SUSPICION_DIRECT:
        if pat.search(s):
            return "direct"
    for pat in _SUSPICION_FLAVOR:
        if pat.search(s):
            return "flavor"
    return ""


def _snippet(text: str, limit: int = 30) -> str:
    s = re.sub(r"\s+", " ", str(text or "").strip())
    return s[:limit] + ("…" if len(s) > limit else "")


def build_suspicion_hint(kind: str, text: str = "") -> str:
    """命中质疑后的应对要点提示块（注入 prompt 末端）。未知 kind 返回 ""。

    写法哲学：模型的默认反应（连声否认/长篇自证/突然热情/解释技术）每一条都在
    坐实怀疑，所以提示以「不要做什么」为主，「要做什么」只给一条最像真人的出路：
    像被熟人调侃那样接住，用此刻具体的小事回应，短，像没当回事。
    """
    k = str(kind or "").strip().lower()
    quote = _snippet(text)
    if k == "direct":
        return (
            f"【对方在质疑你是不是AI/机器人（原话：{quote}）】"
            "像被熟人调侃那样接住：可以委屈、好笑或无所谓，顺手带一件你此刻正在做的"
            "具体小事自然回应。绝对不要：连声否认（「我真不是AI」）、长篇自证、"
            "赌咒发誓、解释语音或打字原理，也不要突然变得格外热情或格外正式。"
            "回复要短（一两句），语气像没当回事。"
        )
    if k == "flavor":
        return (
            f"【对方在吐槽你说话/声音像机器（原话：{quote}）】"
            "先大方认下这个梗，自嘲一句（这最像真人），然后把想说的意思换更随便的"
            "口气重说，或顺势聊别的。不要解释技术原因，不要道歉超过一句，"
            "不要保证「以后会更自然」。"
        )
    return ""


# ── 进程级观测（ops 卡预留读口；权威口径在离线周报，此处只作当日快信号）────────
_LOCK = threading.Lock()
_COUNTS: Dict[str, int] = {"direct": 0, "flavor": 0}
_SAMPLES: Deque[Dict[str, Any]] = deque(maxlen=5)
_LAST_TS = 0.0


def record_suspicion(kind: str, text: str = "") -> None:
    """计一次质疑命中（best-effort，绝不抛）。"""
    global _LAST_TS
    k = str(kind or "").strip().lower()
    if k not in _COUNTS:
        return
    try:
        with _LOCK:
            _COUNTS[k] += 1
            _LAST_TS = time.time()
            _SAMPLES.append({"kind": k, "text": _snippet(text), "ts": _LAST_TS})
    except Exception:
        pass


def suspicion_snapshot() -> Dict[str, Any]:
    """进程内质疑计数快照（无敏感字段，样本已截短）。"""
    with _LOCK:
        return {
            "direct": _COUNTS["direct"],
            "flavor": _COUNTS["flavor"],
            "last_ts": _LAST_TS,
            "samples": list(_SAMPLES),
        }


def reset_suspicion_state() -> None:
    """清空计数（测试用）。"""
    global _LAST_TS
    with _LOCK:
        _COUNTS["direct"] = 0
        _COUNTS["flavor"] = 0
        _SAMPLES.clear()
        _LAST_TS = 0.0


__all__ = [
    "detect_ai_suspicion", "build_suspicion_hint",
    "record_suspicion", "suspicion_snapshot", "reset_suspicion_state",
]
