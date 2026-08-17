# -*- coding: utf-8 -*-
"""已撤销旧设定（boundaries.retired_facts）的结构化核心（P2 期，2026-08-04）。

P1 期钉子是纯字符串列表；本模块升级为**双形态兼容**的单一事实源：

- 条目形态：``str``（legacy，只有文案）或
  ``{"text": 文案, "added": "YYYY-MM-DD", "terms": [排查词, ...]}``；
  ``terms`` 是这条撤销设定的**机器可读锚词**（如「猫」「猫咖」）——出站守卫、
  写入冲突检测、生效验证都靠它；legacy 纯文案条目没有锚词，只参与 prompt 注入
  （软能力，如实降级）。
- 所有消费方（prompt 组装 full/compact、内容排查、守卫、验证路由）统一经
  本模块取数，杜绝「各处自己解析、口径漂移」。

生命周期刻意**只报龄不自动删**：钉子的成本是几十个 prompt token，误删的代价是
复读复发——安全资产的回收必须过人手（UI 按 added 显示「已 N 天，可考虑清理」）。
"""
from __future__ import annotations

import re
import time
from typing import Any, Dict, List, Optional

_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")


def normalize_retired_entries(persona: Any) -> List[Dict[str, Any]]:
    """persona → 规范化撤销条目列表 ``[{text, added, terms}, …]``（宽进严出）。

    str 条目 → ``{text, added:"", terms:[]}``；dict 条目容忍缺键/错型；
    空文案条目丢弃。绝不抛异常（守卫/prompt 热路消费）。
    """
    try:
        b = (persona or {}).get("boundaries") if isinstance(persona, dict) else None
        raw = (b or {}).get("retired_facts")
    except Exception:
        return []
    if raw is None:
        return []
    if isinstance(raw, (str, dict)):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return []
    out: List[Dict[str, Any]] = []
    for item in raw:
        if isinstance(item, str):
            text = item.strip()
            if text:
                out.append({"text": text, "added": "", "terms": []})
            continue
        if isinstance(item, dict):
            text = str(item.get("text") or item.get("fact") or "").strip()
            if not text:
                continue
            added = str(item.get("added") or "").strip()
            if not _DATE_RE.match(added):
                added = ""
            terms_raw = item.get("terms")
            if isinstance(terms_raw, str):
                terms_raw = [terms_raw]
            terms = []
            if isinstance(terms_raw, (list, tuple)):
                seen = set()
                for t in terms_raw:
                    s = str(t).strip()
                    if s and s.lower() not in seen:
                        seen.add(s.lower())
                        terms.append(s)
            out.append({"text": text, "added": added, "terms": terms})
    return out


def retired_prompt_items(persona: Any) -> List[str]:
    """prompt 注入用的文案列表（full/compact 两个组装器共用此口径）。"""
    return [e["text"] for e in normalize_retired_entries(persona)]


def retired_guard_terms(persona: Any) -> List[str]:
    """出站守卫/冲突检测用的锚词并集（仅结构化条目提供；去重保序）。"""
    seen = set()
    out: List[str] = []
    for e in normalize_retired_entries(persona):
        for t in e.get("terms") or []:
            if t.lower() not in seen:
                seen.add(t.lower())
                out.append(t)
    return out


def entry_age_days(entry: Dict[str, Any], now: Optional[float] = None) -> Optional[int]:
    """条目年龄（天）；无/非法 added → None。用于 UI「已钉 N 天」提示，不用于自动删。"""
    m = _DATE_RE.match(str((entry or {}).get("added") or ""))
    if not m:
        return None
    try:
        import datetime as _dt
        d = _dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        today = _dt.date.fromtimestamp(now if now is not None else time.time())
        return max(0, (today - d).days)
    except Exception:
        return None


def prompt_leaks(prompt_text: str, terms: Any) -> List[Dict[str, str]]:
    """指令文本中各锚词的命中片段（大小写不敏感）；供「验证生效」用。

    调用方约定：传入的 ``prompt_text`` 应当来自**去掉 retired_facts 的人设副本**
    ——钉子块本身合法地含锚词（否定语义），不摘除会假阳；与其做易碎的字符串
    剥离，不如让格式化器直接生成无钉子版本（精确且零维护）。
    """
    text = str(prompt_text or "")
    low = text.lower()
    out: List[Dict[str, str]] = []
    for t in (terms or []):
        term = str(t).strip()
        if not term:
            continue
        i = low.find(term.lower())
        if i < 0:
            continue
        a = max(0, i - 30)
        b = min(len(text), i + len(term) + 30)
        out.append({"term": term,
                    "snippet": ("…" if a > 0 else "") + text[a:b]
                               + ("…" if b < len(text) else "")})
    return out


def retired_conflicts(persona: Any) -> List[Dict[str, Any]]:
    """档案内容与撤销锚词的冲突清单（写入边界的「复活检测」）。

    背景：2026-08-02 批量丰富把运营已删的猫内容再生回档案——删除与再生成两条
    管线互不知情。本函数是通用契约：**任何**写入方（Studio 保存 / 导入 /
    未来的丰富管线）落库前查一次，「新内容与撤销清单打架」即刻可见。
    命中排除 ``boundaries.retired_facts`` 自身（钉子文案含锚词是设计使然）。
    只警不拦：运营若有意恢复设定，正确动作是先删钉子再加内容——响应文案
    引导这个顺序，而不是替运营做决定。
    """
    terms = retired_guard_terms(persona)
    if not terms:
        return []
    try:
        from src.utils.persona_content_scan import scan_profile_fields
    except Exception:
        return []
    out: List[Dict[str, Any]] = []
    for term in terms:
        try:
            hits = scan_profile_fields(persona, term)
        except Exception:
            continue
        for h in hits:
            path = str(h.get("path") or "")
            if path.startswith("boundaries.retired_facts"):
                continue
            out.append({"term": term, "path": path,
                        "text": str(h.get("text") or "")})
    return out


__all__ = [
    "normalize_retired_entries", "retired_prompt_items", "retired_guard_terms",
    "entry_age_days", "prompt_leaks", "retired_conflicts",
]
