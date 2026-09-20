# -*- coding: utf-8 -*-
"""出站行系统标签泄漏巡检——判定单一事实源（2026-09-12「[我方语音消息]」事故沉淀）。

事故：上下文归一把系统标注写进 assistant 内容，LLM 照抄进正文并发给客户（4 例实锤：
「[我方发出的语音]」逐字照抄 → 「[我方语音消息]」「[语音消息来自我们这边]」改写 →
「[Voice message from our side]」翻译）。出稿口守卫（``outbound_text_guard.
strip_system_labels``）负责拦，本模块负责**兜底看见**：按落库的出站行扫「以方括号
开头、且不是合法镜像占位」的行——守卫漏了 / 别的生成链没过守卫 / 老进程未装载新
代码，都在这里现形。

消费方两处、口径一处：``HealthWatchdog._check_label_leak``（告警别名 ``label_leak``）
与 ``tools/strip_label_leaks.py``（清理已落库行）。纯函数 + 只读扫描，绝不抛。
"""
from __future__ import annotations

import re
import time
from typing import Any, Dict, List, Optional

from src.ai.outbound_text_guard import is_system_label, leading_tag

# 出站镜像行的**合法**占位（媒体行正文口径：「[图片] 配文」「[语音]×N 念稿」
# 「[图片内容] 描述」「[唱歌]《…》」）。这些是代码写的镜像文本，不是 LLM 输出。
_LEGIT_PLACEHOLDER_RE = re.compile(
    r"^(?:图片|语音|视频|贴纸|文件|媒体|动图|GIF|图片内容|视频内容|贴纸内容|语音转录|唱歌)"
    r"(?:×\d+)?$",
    re.IGNORECASE,
)

KIND_SYSTEM_LABEL = "system_label"      # 命中系统标注词表（事故家族）——高置信泄漏
KIND_BRACKET_PREFIX = "bracket_prefix"  # AI 出站以其它方括号标签开头——低级别可疑


def classify_outbound_text(text: str, *, media_type: str = "", sent_by: str = "") -> str:
    """一条出站行 → ``""``（干净）/ ``system_label`` / ``bracket_prefix``。

    - 首标签是合法镜像占位（``[图片]``/``[语音]×2``/``[图片内容]``…）→ 干净；
    - 首标签内文命中系统标注词表（我方 / 发出的 / voice message / from our side / N天前…）
      → ``system_label``，不论谁发的（人工不会打这种字）；
    - 其余方括号开头且 ``sent_by='ai'`` → ``bracket_prefix``（巡检宁可多报；人工广播
      「【智聊 ChatX 已发布】」sent_by 为空 → 不报）。
    """
    inner = leading_tag(text)
    if not inner:
        return ""
    if _LEGIT_PLACEHOLDER_RE.match(inner):
        return ""
    if is_system_label(inner):
        return KIND_SYSTEM_LABEL
    if str(sent_by or "").strip().lower() == "ai":
        return KIND_BRACKET_PREFIX
    return ""


def scan_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """store 行列表 → 泄漏条目列表（每项 ``{message_id, conversation_id, ts, kind, tag,
    media_type, text}``），按 ts 升序。坏行跳过，绝不抛。"""
    out: List[Dict[str, Any]] = []
    for r in rows or []:
        try:
            text = str(r.get("text") or "")
            kind = classify_outbound_text(
                text, media_type=str(r.get("media_type") or ""),
                sent_by=str(r.get("sent_by") or ""))
            if not kind:
                continue
            out.append({
                "message_id": str(r.get("message_id") or ""),
                "conversation_id": str(r.get("conversation_id") or ""),
                "ts": float(r.get("ts") or 0.0),
                "kind": kind,
                "tag": leading_tag(text),
                "media_type": str(r.get("media_type") or ""),
                "text": text[:120],
            })
        except Exception:
            continue
    out.sort(key=lambda e: e["ts"])
    return out


def scan_store(store: Any, *, lookback_hours: float = 24.0, limit: int = 500,
               now: Optional[float] = None) -> List[Dict[str, Any]]:
    """从 inbox store 扫最近 ``lookback_hours`` 的出站行。store 缺方法 / 异常 → []。"""
    fn = getattr(store, "list_outbound_bracket_rows", None)
    if not callable(fn):
        return []
    ts = float(now if now is not None else time.time())
    since = ts - max(0.25, float(lookback_hours or 24.0)) * 3600.0
    try:
        rows = fn(since_ts=since, limit=limit) or []
    except Exception:
        return []
    return scan_rows(rows)


def summarize(findings: List[Dict[str, Any]]) -> Dict[str, Any]:
    """告警/看板用摘要：总数、按 kind、涉及会话数、样本标签（去重、最多 5 个）。"""
    by_kind: Dict[str, int] = {}
    convs: Dict[str, int] = {}
    tags: List[str] = []
    for e in findings or []:
        by_kind[e["kind"]] = by_kind.get(e["kind"], 0) + 1
        convs[e["conversation_id"]] = convs.get(e["conversation_id"], 0) + 1
        tag = f"[{e['tag']}]"
        if tag not in tags:
            tags.append(tag)
    return {
        "count": len(findings or []),
        "system_label": by_kind.get(KIND_SYSTEM_LABEL, 0),
        "bracket_prefix": by_kind.get(KIND_BRACKET_PREFIX, 0),
        "conversations": len(convs),
        "top_conversations": sorted(convs.items(), key=lambda kv: -kv[1])[:3],
        "sample_tags": tags[:5],
    }


__all__ = [
    "KIND_BRACKET_PREFIX", "KIND_SYSTEM_LABEL", "classify_outbound_text",
    "scan_rows", "scan_store", "summarize",
]
