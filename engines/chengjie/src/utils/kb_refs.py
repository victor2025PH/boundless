"""KB 引用提取 — 把 ``kb_store.search()`` 结果浓缩成轻量「证据 chips」。

P2 证据整合：草稿引擎（generate_inbox_draft / persona_reply 直连回落）检索到的
KB 条目此前只拼成 prompt 字符串就丢弃——坐席看不到「这条草稿依据了什么」。
本模块把 entries 提取成 ``[{entry_id, title, category, snippet}]`` 随草稿响应
透传（内存链路，零迁移零新表），前端复用知识 chip/浮层展示。

纯函数、防御式：任何形状异常返回 []，绝不抛。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional


def extract_kb_refs(
    result: Optional[Dict[str, Any]], *, limit: int = 3, max_snippet: int = 160,
) -> List[Dict[str, str]]:
    """从 ``kb.search()`` 完整结果提取引用条目（title/snippet 至少有其一才收）。

    snippet 优先级与草稿 prompt 的 answer 口径一致：example_reply_zh →
    example_reply → steps → principles；超长按 ``max_snippet`` 截断加省略号。
    """
    try:
        entries = (result or {}).get("entries") or []
    except Exception:
        return []
    refs: List[Dict[str, str]] = []
    for row in entries[: max(0, int(limit))]:
        if not isinstance(row, dict):
            continue
        snippet = str(
            row.get("example_reply_zh")
            or row.get("example_reply")
            or row.get("steps")
            or row.get("principles")
            or ""
        ).strip()
        if max_snippet > 0 and len(snippet) > max_snippet:
            snippet = snippet[:max_snippet] + "…"
        ref = {
            "entry_id": str(row.get("id") or row.get("entry_id") or ""),
            "title": str(row.get("title") or row.get("scenario") or "").strip()[:80],
            "category": str(row.get("category") or "").strip()[:40],
            "snippet": snippet,
        }
        if ref["title"] or ref["snippet"]:
            refs.append(ref)
    return refs
