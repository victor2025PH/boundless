# -*- coding: utf-8 -*-
"""STOP 之后的「重新订阅」与坐席手动解冻（2026-10-08 智聊 DM 接入 · 对齐智安统一闸）。

两条且仅两条解冻口，都**复用**既有部件、不另起一套停联真相：

* 冻结真相 / 审计：``src.compliance.stop_gate``（``contact_stopped`` / ``audit`` /
  ``FREEZE_REASON``）——本模块只调用其公开函数，不改它。
* 解冻动作：``src.inbox.stop_contact.unfreeze_conversation``（摘「客户要求停联」+ 清
  ``stop_contact_at`` + 摘「需人工」+ risk_hold 解除 + 档位还原 + 名单 ``mark_unfrozen``）。

1. :func:`resubscribe` —— 客户**本人**在同一会话里整句发 ``START`` / ``/start`` /
   ``UNSTOP`` / ``重新订阅`` 等：视为本人重新同意，解冻**该账号**上的这一位客户，审计
   ``action=resubscribed``。只认「之前因 STOP 停联」的会话：危机 / 自伤等其他冻结原因
   绝不因一句 START 解开（``refused``）。别的账号 / 同手机号在别处的停联记录不动——
   客户只在这里重新同意，那些仍按原样拦（结果里 ``still_stopped`` 会如实告知）。
2. :func:`agent_unfreeze` —— 坐席手动解冻：必须带操作人与理由，审计 ``action=unfrozen``
   （``hit`` 存理由前 40 字，不存客户原文）。

两者都幂等、绝不抛；审计只记元数据（平台 / 账号 / 对端 id / 动作 / 理由码），不记原文。
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any, Dict, Iterable, Optional, Tuple

logger = logging.getLogger(__name__)

#: 整句「重新订阅」词（normalize 后逐字比较；不做子串匹配——「don't start」「start again later?」不算）。
#: ``start`` / ``unstop`` 是短信 / WhatsApp 退订体系的通行恢复词；Telegram 用户解除拉黑后点
#: 「开始」客户端会发 ``/start``（含深链参数 ``/start xxx`` 与群内 ``/start@BotName``）。
#: 单独的「订阅」「subscribe」（含 ``/subscribe``）**不算**（智安 2026-10-08，蛋博士拍板）：客户问产品
#: 订阅（「订阅？」「subscribe」）与重新同意接收消息分不开，误解冻 = 停联后继续发（红线①）。
#: 只留整句 START / ``/start`` 与明确表示「恢复接收」的说法。
DEFAULT_RESUBSCRIBE_KEYWORDS: Tuple[str, ...] = (
    "start", "unstop", "resubscribe", "yes start",
    "/start", "/unstop", "/resubscribe",
    "重新订阅", "恢复订阅", "重新开始接收",
)

#: 明确排除的歧义词（防被加回缺省表；测试钉住）。
AMBIGUOUS_NOT_RESUBSCRIBE: Tuple[str, ...] = ("subscribe", "/subscribe", "订阅")

#: 审计 path 前缀与动作名（stop_gate_audit.action 列 ≤20 字）。
ACTION_RESUBSCRIBED = "resubscribed"
ACTION_UNFROZEN = "unfrozen"
ACTION_REFUSED = "resub_refused"

_PUNCT_RE = re.compile(r"[\s\.,!?！？。，、~～…:：;；\"'“”‘’()（）\[\]【】]+")


def _norm(text: Any) -> str:
    s = str(text or "").strip().lower()
    s = _PUNCT_RE.sub(" ", s).strip()
    return re.sub(r"\s+", " ", s)


def resubscribe_hit(text: Any, keywords: Optional[Iterable[str]] = None) -> str:
    """整句重新订阅词命中 → 返回规范化命中词；否则空串。纯函数。"""
    raw = str(text or "").strip().lower()
    if not raw or len(raw) > 64:
        return ""
    kws = {_norm(k) for k in (keywords or DEFAULT_RESUBSCRIBE_KEYWORDS) if str(k or "").strip()}
    if raw.startswith("/"):
        cmd = raw.split()[0].split("@", 1)[0]
        return cmd if cmd in kws else ""
    n = _norm(raw)
    return n if n and n in kws else ""


def _sg() -> Any:
    try:
        from src.compliance import stop_gate
        return stop_gate
    except Exception:
        logger.debug("[resubscribe] stop_gate 不可用", exc_info=True)
        return None


def _conv_id(platform: str, account_id: str, peer: str) -> str:
    try:
        from src.inbox.normalizer import conv_id
        return conv_id(platform, account_id, peer)
    except Exception:
        return f"{platform}:{account_id}:{peer}"


def _split_cid(cid: str) -> Tuple[str, str, str]:
    parts = str(cid or "").split(":", 2)
    return (parts[0], parts[1], parts[2]) if len(parts) == 3 else ("", "", "")


def _frozen_reason(store: Any, cid: str) -> str:
    if store is None or not cid:
        return ""
    try:
        from src.inbox.stop_contact import frozen_reason
        return str(frozen_reason(store, cid) or "")
    except Exception:
        return ""


def _listed_here(store: Any, plat: str, acct: str, peer: str) -> bool:
    try:
        from src.inbox.account_blocklist import get_blocklist
        return bool(get_blocklist(store).is_blocked(plat, acct, peer))
    except Exception:
        return False


def _audit(store: Any, *, path: str, action: str, plat: str, acct: str, peer: str,
           cid: str, reason: str, hit: str, now: float) -> bool:
    sg = _sg()
    if sg is None:
        return False
    try:
        return bool(sg.audit(store, path=path[:40], action=action, platform=plat, account_id=acct,
                             peer=peer, conversation_id=cid, reason=reason[:40], hit=hit[:40], now=now))
    except Exception:
        logger.debug("[resubscribe] 审计写入失败", exc_info=True)
        return False


def _still_stopped(store: Any, plat: str, acct: str, peer: str, cid: str, phone: str = "") -> str:
    sg = _sg()
    if sg is None:
        return ""
    try:
        return str(sg.contact_stopped(store, plat, acct, peer, conversation_id=cid, phone=phone) or "")
    except Exception:
        return ""


def _forget_official_memory(plat: str, acct: str, peer: str) -> None:
    try:
        from src.integrations.shared.official_stop_gate import forget_memory_stop
        forget_memory_stop(plat, acct, peer)
    except Exception:
        logger.debug("[resubscribe] 官方闸进程内集合清理失败（忽略）", exc_info=True)


def resubscribe(store: Any, *, platform: str, account_id: str, peer: str, text: Any,
                source: str = "inbound", keywords: Optional[Iterable[str]] = None,
                phone: str = "", now: Optional[float] = None) -> Dict[str, Any]:
    """客户本人整句发 START 类词 → 解冻该账号上这位客户。绝不抛。

    返回 ``{action, hit, conversation_id, was, still_stopped, detail}``，``action``：
    ``none``（不是重新订阅词）/ ``not_stopped``（本来就没停联，什么也不做）/
    ``refused``（冻结原因不是 STOP，例如危机，必须人工处理）/ ``resubscribed``。
    """
    plat = str(platform or "").strip().lower()
    acct = str(account_id or "").strip() or "_"
    pr = str(peer or "").strip()
    ts = float(now if now is not None else time.time())
    out: Dict[str, Any] = {"action": "none", "hit": "", "conversation_id": "", "was": "",
                           "still_stopped": "", "detail": {}}
    hit = resubscribe_hit(text, keywords)
    if not hit or not plat or not pr:
        return out
    out["hit"] = hit
    cid = _conv_id(plat, acct, pr)
    out["conversation_id"] = cid
    sg = _sg()
    freeze_reason = getattr(sg, "FREEZE_REASON", "stop_contact") if sg is not None else "stop_contact"
    was = _frozen_reason(store, cid)
    listed = _listed_here(store, plat, acct, pr)
    out["was"] = was or ("blocklist" if listed else "")
    try:
        from src.integrations.shared.official_stop_gate import memory_stopped
        mem = bool(memory_stopped(plat, acct, pr))
    except Exception:
        mem = False
    if not was and not listed and not mem:
        out["action"] = "not_stopped"
        return out
    path = f"resubscribe:{str(source or 'inbound')}"
    if was and was != freeze_reason:
        # 危机 / 自伤等冻结：客户一句 START 不能解，留给人工
        out["action"] = "refused"
        _audit(store, path=path, action=ACTION_REFUSED, plat=plat, acct=acct, peer=pr, cid=cid,
               reason=was, hit=hit, now=ts)
        logger.warning("[resubscribe] platform=%s account=%s action=refused was=%s", plat, acct, was)
        return out
    detail: Dict[str, Any] = {}
    try:
        from src.inbox.stop_contact import unfreeze_conversation
        detail = dict(unfreeze_conversation(store, cid, actor="customer_start") or {})
    except Exception:
        logger.debug("[resubscribe] unfreeze_conversation 失败", exc_info=True)
    if not detail.get("unfrozen_listed") and listed:
        # 会话不在本机收件箱（名单先于会话）时 unfreeze_conversation 走不到名单——兜底标记
        try:
            from src.inbox.account_blocklist import get_blocklist
            detail["unfrozen_listed"] = bool(get_blocklist(store).mark_unfrozen(
                plat, acct, pr, by="customer_start", ts=ts))
        except Exception:
            logger.debug("[resubscribe] 名单解冻兜底失败", exc_info=True)
    _forget_official_memory(plat, acct, pr)
    out["detail"] = detail
    out["action"] = "resubscribed"
    out["still_stopped"] = _still_stopped(store, plat, acct, pr, cid, phone)
    _audit(store, path=path, action=ACTION_RESUBSCRIBED, plat=plat, acct=acct, peer=pr, cid=cid,
           reason="customer_start", hit=hit, now=ts)
    logger.warning("[resubscribe] platform=%s account=%s action=resubscribed still_stopped=%s",
                   plat, acct, out["still_stopped"] or "-")
    return out


def agent_unfreeze(store: Any, conversation_id: str, *, actor: str, note: str,
                   config: Optional[Dict[str, Any]] = None,
                   now: Optional[float] = None) -> Dict[str, Any]:
    """坐席手动解冻（必须有操作人与理由）→ ``unfreeze_conversation`` + 审计 ``unfrozen``。绝不抛。

    返回 ``{ok, error, conversation_id, was, still_stopped, detail}``；``error``：
    ``actor_required`` / ``note_required`` / ``bad_conversation`` / ``not_frozen`` / ``unfreeze_failed``。
    """
    cid = str(conversation_id or "").strip()
    who = str(actor or "").strip()
    why = " ".join(str(note or "").split())
    out: Dict[str, Any] = {"ok": False, "error": "", "conversation_id": cid, "was": "",
                           "still_stopped": "", "detail": {}}
    if not who:
        out["error"] = "actor_required"
        return out
    if not why:
        out["error"] = "note_required"
        return out
    plat, acct, pr = _split_cid(cid)
    if not (plat and acct and pr):
        out["error"] = "bad_conversation"
        return out
    ts = float(now if now is not None else time.time())
    was = _frozen_reason(store, cid)
    listed = _listed_here(store, plat, acct, pr)
    out["was"] = was or ("blocklist" if listed else "")
    if not was and not listed:
        out["error"] = "not_frozen"
        return out
    try:
        from src.inbox.stop_contact import unfreeze_conversation
        out["detail"] = dict(unfreeze_conversation(store, cid, actor=who[:60], config=config) or {})
    except Exception:
        logger.debug("[resubscribe] 坐席解冻失败", exc_info=True)
        out["error"] = "unfreeze_failed"
        return out
    _forget_official_memory(plat.lower(), acct, pr)
    out["still_stopped"] = _still_stopped(store, plat.lower(), acct, pr, cid)
    _audit(store, path=f"agent_unfreeze:{who}", action=ACTION_UNFROZEN, plat=plat.lower(), acct=acct,
           peer=pr, cid=cid, reason=(out["was"] or "-"), hit=why, now=ts)
    logger.warning("[resubscribe] conv=%s action=unfrozen by=%s was=%s still_stopped=%s",
                   cid, who[:60], out["was"] or "-", out["still_stopped"] or "-")
    out["ok"] = True
    return out


__all__ = [
    "DEFAULT_RESUBSCRIBE_KEYWORDS", "AMBIGUOUS_NOT_RESUBSCRIBE",
    "ACTION_RESUBSCRIBED", "ACTION_UNFROZEN", "ACTION_REFUSED",
    "resubscribe_hit", "resubscribe", "agent_unfreeze",
]
