"""发前确认（L1）原因（M-2 E，#235 / D-M10，2026-09-06）。

8TV32T：草稿审批台「发前确认 3」只有「短句接话 / 简短」标签，日志 ``level=L1`` 无 reason
字段，坐席只能猜「为什么这条要我确认」。本模块给 L1 一个**可读原因码**，并把它送到三处：
``drafts.py`` 的 L1 日志行（D-M10 只准加的那一行读 ``peek``）、草稿审计行
（``record_draft_audit action=l1_reason``，重启后仍可查）、``/api/drafts`` 列表字段
``l1_reason``（卡片 / 会话内草稿条显示）。

原因码（按 8TV32T 要求的优先级排：刚登录/重登冷静期 > 无人设 > 语言未定 > 客户首条 >
证据不足；其后是其它封顶层 / 用户自己选的人审档）：

- ``cooldown``            登录/重登冷静期（login_cooldown 封顶）
- ``no_persona``          账号未选人设（persona_unselected 封顶 / #156 #167）
- ``lang_unknown``        客户语言判不出（消息无文字系统证据且会话语言未知）
- ``first_contact``       客户首条来信（会话内入站 ≤1 条）
- ``weak_evidence``       消息太短 / 只有占位符（拼写错误的「Hllo」也归此）
- ``channel_disconnected`` / ``channel_degraded`` / ``deliver_paused`` / ``warmup`` /
  ``platform_cap`` / ``business_line`` / ``identity_pending`` / ``reconnect_backlog`` /
  ``own_fleet_peer``  其它封顶层（层名即码）
- ``manual_review``       会话显式选了「AI 草稿·我审」/ 多选
- ``account_default``     账号层默认半自动（登录后未按账号开全自动 / onboarding 未确认）
- ``global_default``      全局默认半自动
- ``risk_hold``           风险层扣稿（enforce 档 L3/L4，非本模块判定，仅透传）

纯函数 + 进程级小注册表（{key: (reason, ts)}，上限 2000、TTL 6h），任何异常返回 ``""``。
"""
from __future__ import annotations

import threading
import time
from typing import Any, Dict, List, Optional, Tuple

_lock = threading.Lock()
_REG: Dict[str, Tuple[str, float]] = {}
_MAX = 2000
_TTL = 6 * 3600.0

REASON_PRIORITY = (
    "cooldown", "no_persona", "lang_unknown", "first_contact", "weak_evidence",
)
_CAP_LAYER_REASON = {
    "login_cooldown": "cooldown",
    "persona_unselected": "no_persona",
    "channel_disconnected": "channel_disconnected",
    "channel_degraded": "channel_degraded",
    "deliver_paused": "deliver_paused",
    "warmup": "warmup",
    "platform": "platform_cap",
    "business_line": "business_line",
    "identity_pending": "identity_pending",
    "reconnect_backlog": "reconnect_backlog",
    "own_fleet_peer": "own_fleet_peer",
}
#: 「证据不足」：去掉空白 / 标点后不足这么多字符
_WEAK_MIN_CHARS = 3


def note(key: str, reason: str) -> None:
    """登记（key＝conversation_id 或 draft_id）。"""
    k = str(key or "")
    r = str(reason or "")
    if not k or not r:
        return
    now = time.time()
    with _lock:
        _REG[k] = (r, now)
        if len(_REG) > _MAX:
            cutoff = now - _TTL
            for kk in [x for x, (_, ts) in _REG.items() if ts < cutoff]:
                _REG.pop(kk, None)
            if len(_REG) > _MAX:
                for kk in sorted(_REG, key=lambda x: _REG[x][1])[: len(_REG) - _MAX]:
                    _REG.pop(kk, None)


def peek(key: str) -> str:
    """读原因码（无 → ``""``，绝不抛）。"""
    try:
        rec = _REG.get(str(key or ""))
        return rec[0] if rec else ""
    except Exception:
        return ""


def _text_weak(text: str) -> bool:
    t = "".join(ch for ch in str(text or "") if ch.isalnum())
    return len(t) < _WEAK_MIN_CHARS


def _lang_unknown(text: str, conv: Optional[Dict[str, Any]], store: Any = None) -> bool:
    # Q-21 B（#302 / Y82GWM）：先问会话语言计划（outbound_translate.build_conv_lang_plan，
    # 起草链在本判定前已登记）——计划说「对方语言有证据」（英文「Hi」按文字系统判 en）就不是
    # lang_unknown；计划说「按人设/账号默认回」才是。无计划 → 旧口径（conv.language + 证据）。
    try:
        from src.inbox.outbound_translate import peek_conv_lang_plan
        plan = peek_conv_lang_plan(str((conv or {}).get("conversation_id") or ""), store=store)
        if plan:
            return not bool(plan.get("peer_known"))
    except Exception:
        pass
    lang = str((conv or {}).get("language") or "").strip().lower()
    if lang and lang != "unknown":
        return False
    try:
        from src.ai.lang_policy import evidence_lang
        ev = evidence_lang(str(text or ""))
    except Exception:
        ev = None
    return not ev or str(ev).lower() == "unknown"


def _first_contact(store: Any, conversation_id: str) -> bool:
    if store is None or not conversation_id:
        return False
    try:
        rows = store.list_recent_messages(conversation_id, limit=6) or []
    except Exception:
        return False
    inbound = [r for r in rows if str((r or {}).get("direction") or "in") == "in"]
    return len(inbound) <= 1


def derive_l1_reason(
    *, mode: str, caps_applied: Optional[List[Any]] = None,
    conv: Optional[Dict[str, Any]] = None, store: Any = None,
    peer_text: str = "", explicit_mode: Optional[str] = None,
    account_layer: Optional[str] = None,
) -> str:
    """推导本稿为何是 L1（``mode`` ≠ auto_ai 时才有意义；auto_ai → ``""``）。

    ``caps_applied``：effective_automation.apply_mode_caps 返回的生效封顶（ModeCap 或
    dict）；``explicit_mode``：会话显式档位（None＝无显式行）；``account_layer``：账号层
    档位（None＝不表态）。全部 fail-safe。
    """
    try:
        m = str(mode or "").strip().lower()
        if m == "auto_ai" or not m:
            return ""
        layers: List[str] = []
        for c in (caps_applied or []):
            layer = getattr(c, "layer", None)
            if layer is None and isinstance(c, dict):
                layer = c.get("layer")
            if layer:
                layers.append(str(layer))
        cid = str((conv or {}).get("conversation_id") or "")
        # ① 冷静期 / ② 无人设（封顶层直接给出，最优先）
        if "login_cooldown" in layers:
            return "cooldown"
        if "persona_unselected" in layers:
            return "no_persona"
        # ③ 会话显式选的人审/多选/手动：原因就是「你设的」，不必再猜证据
        if str(explicit_mode or "").lower() in ("review", "multi_choice", "manual"):
            return "manual_review"
        # ④ 其它封顶层（通道未连接 / 降级 / 总闸 / 预热 / 平台封顶 …）
        for layer in layers:
            if layer in _CAP_LAYER_REASON:
                return _CAP_LAYER_REASON[layer]
        # ⑤ 账号/全局默认半自动：给坐席最可操作的证据码（语言未定 > 首条 > 证据不足）
        if _lang_unknown(peer_text, conv, store):
            return "lang_unknown"
        if _first_contact(store, cid):
            return "first_contact"
        if _text_weak(peer_text):
            return "weak_evidence"
        if account_layer:
            return "account_default"
        return "global_default"
    except Exception:
        return ""


def _reset_for_tests() -> None:
    with _lock:
        _REG.clear()


__all__ = ["note", "peek", "derive_l1_reason", "REASON_PRIORITY"]
