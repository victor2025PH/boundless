# -*- coding: utf-8 -*-
"""人设补丁提案（P0）：完善度缺口 + 退役冲突 → 人审后 merge 写入。

硬核身份（name/age/gender/identity/boundaries 政策）永不自动写。
``boundaries.retired_facts`` 只允许 append（复用扫描「钉住」语义）。
本模块是纯函数：不碰磁盘、不调 LLM；落库与 HTTP 在 store / routes。
"""
from __future__ import annotations

import time
from typing import Any, Dict, List

FILLABLE_FIELDS = frozenset({
    "background",
    "personality.style",
    "personality.traits",
    "personality.quirks",
    "personality.temperament",
    "context.hobbies",
    "context.specific_memories",
    "context.emotional_triggers",
    "appearance",
    "tastes",
})

_HARD_LOCK_EXACT = frozenset({
    "name", "age", "gender",
    "names.call_peer", "names.peer_calls_you",
})
_LIST_FIELDS = frozenset({
    "personality.traits",
    "context.hobbies",
    "context.specific_memories",
})
_RETIRE_FIELD = "boundaries.retired_facts"


def is_hard_lock(field: str, action: str = "") -> bool:
    """接受闸：硬锁字段禁止写入。退役钉住是唯一例外。"""
    f = str(field or "").strip()
    act = str(action or "").strip()
    if act == "retire_pin" and f == _RETIRE_FIELD:
        return False
    if f in _HARD_LOCK_EXACT:
        return True
    if f == "identity" or f.startswith("identity."):
        return True
    if f == "boundaries" or f.startswith("boundaries."):
        return True
    if f.startswith("names.call_peer") or f.startswith("names.peer_calls_you"):
        return True
    return False


def is_writable_proposal(kind: str, field: str, action: str) -> bool:
    """接受路径只放行白名单：fillable fill，或退役钉住。"""
    if str(action or "") == "retire_pin" and str(field or "") == _RETIRE_FIELD:
        return True
    return str(kind or "") == "fill" and str(field or "") in FILLABLE_FIELDS


def _get_path(persona: dict, field: str) -> Any:
    cur: Any = persona
    for part in str(field or "").split("."):
        if not part:
            continue
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _nest(field: str, value: Any) -> dict:
    out: Any = value
    for part in reversed([p for p in str(field).split(".") if p]):
        out = {part: out}
    return out if isinstance(out, dict) else {}


def build_apply_patch(
    persona: dict, field: str, action: str, proposed: Any,
) -> dict:
    """按当前档案拼 merge 补丁。list 字段整表重写（deep_merge 对 list 是替换）。"""
    if not isinstance(persona, dict):
        persona = {}
    field = str(field or "").strip()
    action = str(action or "").strip()
    if action == "retire_pin" and field == _RETIRE_FIELD:
        return _retire_pin_patch(persona, proposed)
    if field not in FILLABLE_FIELDS:
        raise ValueError("field_not_writable")
    text = proposed if not isinstance(proposed, (dict, list)) else proposed
    if isinstance(text, str):
        text = text.strip()
        if not text:
            raise ValueError("proposed_empty")
    elif text in (None, "", [], {}):
        raise ValueError("proposed_empty")

    if field == "tastes":
        cur = _get_path(persona, "tastes")
        base = dict(cur) if isinstance(cur, dict) else {}
        likes = list(base.get("likes") or [])
        likes.append(str(text))
        base["likes"] = likes
        return {"tastes": base}
    if field == "context.emotional_triggers":
        cur = _get_path(persona, field)
        base = dict(cur) if isinstance(cur, dict) else {}
        pos = list(base.get("positive") or [])
        pos.append(str(text))
        base["positive"] = pos
        return _nest(field, base)
    if field in _LIST_FIELDS:
        cur = _get_path(persona, field)
        items = list(cur) if isinstance(cur, list) else []
        items.append(str(text))
        return _nest(field, items)
    # 标量 / 自由文本：空则填，已有则换行追加（fill 与 append 同形，避免抹掉手写）
    cur = _get_path(persona, field)
    if isinstance(cur, str) and cur.strip():
        return _nest(field, cur.rstrip() + "\n" + str(text))
    return _nest(field, str(text))


def _retire_pin_patch(persona: dict, proposed: Any) -> dict:
    from src.utils.persona_retired import normalize_retired_entries

    entries = normalize_retired_entries(persona)
    if isinstance(proposed, dict):
        text = str(proposed.get("text") or "").strip()
        terms_raw = proposed.get("terms") or []
        terms = [str(t).strip() for t in terms_raw if str(t).strip()] if isinstance(terms_raw, (list, tuple)) else []
        if not terms and proposed.get("term"):
            terms = [str(proposed.get("term")).strip()]
    else:
        text = str(proposed or "").strip()
        terms = []
    if not text:
        raise ValueError("proposed_empty")
    today = time.strftime("%Y-%m-%d")
    already = {(e.get("text") or "").strip() for e in entries}
    if text not in already:
        entries.append({"text": text, "added": today, "terms": terms})
    return {"boundaries": {"retired_facts": entries}}


def collect_fill_drafts(persona: dict) -> List[Dict[str, Any]]:
    """完善度缺口 → fill 草稿（无证据，proposed 空，需运营手填）。"""
    from src.utils.persona_completeness import persona_completeness

    if not isinstance(persona, dict):
        return []
    missing = list((persona_completeness(persona) or {}).get("missing") or [])
    out: List[Dict[str, Any]] = []
    for field in missing:
        if field not in FILLABLE_FIELDS:
            continue
        if is_hard_lock(field):
            continue
        out.append({
            "kind": "fill",
            "field": field,
            "action": "fill",
            "dedupe_key": f"fill:{field}",
            "current": _get_path(persona, field),
            "proposed": None,
            "needs_operator": True,
            "confidence": 0.4,
            "rationale": f"完善度缺口：{field}",
            "evidence": [{"source": "completeness", "field": field}],
        })
    return out


def collect_retire_drafts(persona: dict) -> List[Dict[str, Any]]:
    """档案与已钉退役锚词打架 → retire 草稿（钉住，不删冲突句）。"""
    if not isinstance(persona, dict):
        return []
    try:
        from src.utils.persona_retired import retired_conflicts
        conflicts = retired_conflicts(persona) or []
    except Exception:
        return []
    seen = set()
    out: List[Dict[str, Any]] = []
    for hit in conflicts:
        if not isinstance(hit, dict):
            continue
        term = str(hit.get("term") or "").strip()
        path = str(hit.get("path") or "").strip()
        text = str(hit.get("text") or "").strip()
        if not term:
            continue
        key = f"retire:{term}"
        if key in seen:
            continue
        seen.add(key)
        pin_text = f"已作废：不再认领「{term}」相关旧设定。"
        out.append({
            "kind": "retire",
            "field": _RETIRE_FIELD,
            "action": "retire_pin",
            "dedupe_key": key,
            "current": {"path": path, "text": text, "term": term},
            "proposed": {"text": pin_text, "terms": [term]},
            "needs_operator": False,
            "confidence": 0.7,
            "rationale": f"档案 {path or '某字段'} 仍含已退役锚词「{term}」",
            "evidence": [{"source": "scan", "path": path, "term": term, "text": text}],
        })
    return out


def collect_drafts(persona: dict) -> List[Dict[str, Any]]:
    return collect_fill_drafts(persona) + collect_retire_drafts(persona)


def draft_to_row(profile_id: str, draft: dict, base_rev: str) -> dict:
    """草稿 → store.insert 行（调用方再补 ts/status）。"""
    return {
        "persona_id": str(profile_id),
        "kind": str(draft.get("kind") or ""),
        "field": str(draft.get("field") or ""),
        "action": str(draft.get("action") or ""),
        "dedupe_key": str(draft.get("dedupe_key") or ""),
        "current": draft.get("current"),
        "proposed": draft.get("proposed"),
        "rationale": str(draft.get("rationale") or ""),
        "confidence": float(draft.get("confidence") or 0),
        "evidence": list(draft.get("evidence") or []),
        "base_rev": str(base_rev or ""),
        "needs_operator": bool(draft.get("needs_operator")),
    }
