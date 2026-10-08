# -*- coding: utf-8 -*-
"""官方通道 STOP 硬闸（WhatsApp Cloud API / Telegram Bot API 共用，2026-10-08 智聊 DM 接入）。

红线：**客户说 STOP 之后，绝不再真发**（只记录）。主线停联体系（``src/inbox/stop_contact``
冻结 + ``account_blocklist`` 账号级名单）只挂在收件箱草稿链上；官方 webhook 的
「自答」路径（``use_pipeline=False`` 时 SkillManager 直接回发）和出站助手
（``wa_send_text`` / ``tg_bot_send_text``）原本**不过这道闸**——本模块补齐两端：

入站 ``inbound_gate(...)``：
  1. 已冻结（名单未解冻 / 会话 ``frozen_reason`` / 进程内兜底集合）→ ``action=frozen``：
     镜像照常（坐席可见），**不自答、不进主管道**。
  2. 命中 STOP → 冻结（``freeze_conversation``：需人工 + 「客户要求停联」标签 + 档位 manual
     + 名单）→ ``action=stopped``，本条同样不自答。
     命中判定 = 整句关键词（``STOP`` / ``UNSUBSCRIBE`` / ``退订`` / Telegram ``/stop`` …，
     Meta 营销退订按钮 ``Stop promotions``）**或** 主线双语停联词表（``stop_contact_hits``）。
  3. 其余 → ``action=pass``。

出站 ``outbound_gate(...)``：发前查同一份冻结真相，冻结 → ``(True, reason)``，调用方返回
``{ok: False, error: "stop_gate:<reason>", blocked: "stop_contact"}``，不碰网络。主线
「唯一一条告别」默认**也拦**（``allow_farewell`` 配置开了才放行与告别模板逐字相等的那一条）。

与智安统一闸 ``src.compliance.stop_gate``（feat-zhiliao-compliance-gates 在途）的关系：**软委托**——
该模块存在时，判定追加 ``detect()``、冻结真相追加 ``contact_stopped()``（跨账号 / 同手机号视角）、
登记走 ``record_stop()``、出站拦截经 ``outbound_check()`` 记审计；不存在时用本模块回落实现。
合并后两边看到同一份名单，不会出现两套停联真相。

解冻只走人工（``stop_contact.unfreeze_conversation`` / 名单 ``mark_unfrozen``）；本模块不自动解冻。
全部函数绝不抛（闸本身失败时：入站按 pass、出站按「查不到冻结」放行——与主线
``rpa_send_blocked`` 同策略；但命中 STOP 的那条入站一定写进进程内兜底集合，确保本进程内不再发）。
"""
from __future__ import annotations

import logging
import re
import threading
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

#: 整句退订关键词（normalize 后逐字比较；不做子串匹配，防「don't stop」误伤）。
#: 含 Meta 营销模板的退订快捷按钮文案（``Stop promotions``）与 Telegram 命令 ``/stop``。
DEFAULT_STOP_KEYWORDS: Tuple[str, ...] = (
    "stop", "stop all", "stopall", "unsubscribe", "stop promotions", "stop promotion",
    "opt out", "optout", "opt-out",
    "/stop", "/unsubscribe",
    "退订", "取消订阅", "停止", "停止发送", "退订消息", "td",
)

_PUNCT_RE = re.compile(r"[\s\.,!?！？。，、~～…:：;；\"'“”‘’()（）\[\]【】]+")

_lock = threading.Lock()
#: 进程内兜底：store/名单都不可用时也保证「本进程内不再发」。键 (platform, account_id, peer)。
_mem_stopped: Dict[Tuple[str, str, str], float] = {}
#: 可观测计数（per platform）。
_stats: Dict[str, Dict[str, Any]] = {}


def _bump(platform: str, key: str, **last: Any) -> None:
    try:
        with _lock:
            row = _stats.setdefault(str(platform or "?"), {
                "stop_hits": 0, "inbound_frozen_skipped": 0, "outbound_blocked": 0,
                "last_stop_ts": 0.0, "last_block_ts": 0.0,
            })
            row[key] = int(row.get(key) or 0) + 1
            row.update(last)
    except Exception:
        logger.debug("[stop-gate] 计数失败（忽略）", exc_info=True)


def stats_snapshot(platform: str = "") -> Dict[str, Any]:
    """计数快照（无 peer 明细，可直接上健康面板）。"""
    with _lock:
        if platform:
            return dict(_stats.get(platform) or {
                "stop_hits": 0, "inbound_frozen_skipped": 0, "outbound_blocked": 0,
                "last_stop_ts": 0.0, "last_block_ts": 0.0,
            })
        return {k: dict(v) for k, v in _stats.items()}


def reset_for_tests() -> None:
    with _lock:
        _mem_stopped.clear()
        _stats.clear()


def normalize_keyword(text: Any) -> str:
    s = str(text or "").strip().lower()
    s = _PUNCT_RE.sub(" ", s).strip()
    return re.sub(r"\s+", " ", s)


def keyword_hit(text: Any, keywords: Optional[Iterable[str]] = None) -> str:
    """整句退订关键词命中 → 返回命中词；否则空串。"""
    norm = normalize_keyword(text)
    if not norm or len(norm) > 40:
        return ""
    raw = str(text or "").strip().lower()
    kws = [normalize_keyword(k) for k in (keywords or DEFAULT_STOP_KEYWORDS) if str(k or "").strip()]
    # 「/stop」这类命令：normalize 不动斜杠；同时允许 Telegram 的「/stop@BotName」
    if raw.startswith("/"):
        cmd = raw.split()[0].split("@", 1)[0]
        if cmd in kws:
            return cmd
    return norm if norm in kws else ""


def _compliance() -> Any:
    """智安统一 STOP 闸（``src.compliance.stop_gate``）；未合入 → None。"""
    try:
        import importlib
        return importlib.import_module("src.compliance.stop_gate")
    except ImportError:
        return None
    except Exception:
        logger.debug("[stop-gate] 统一闸导入异常（按未合入）", exc_info=True)
        return None


def _digits(chat_key: str) -> str:
    tail = str(chat_key or "").rsplit(":", 1)[-1]
    d = "".join(ch for ch in tail if ch.isdigit())
    return d if len(d) >= 7 else ""


def detect_stop(text: Any, *, keywords: Optional[Iterable[str]] = None,
                use_phrase_lexicon: bool = True) -> List[str]:
    """STOP 判定：整句关键词 或 统一闸 ``detect`` 或 主线双语停联词表。返回命中词（空 = 未命中）。绝不抛。"""
    try:
        kw = keyword_hit(text, keywords)
        if kw:
            return [kw]
        if not use_phrase_lexicon:
            return []
        cg = _compliance()
        if cg is not None and callable(getattr(cg, "detect", None)):
            hit = str(cg.detect(text) or "")
            if hit:
                return [hit[:40]]
        if use_phrase_lexicon:
            from src.inbox.stop_contact import stop_contact_hits
            return list(stop_contact_hits(str(text or "")))
    except Exception:
        logger.debug("[stop-gate] detect_stop 异常（按未命中）", exc_info=True)
    return []


def _store() -> Any:
    try:
        from src.integrations.protocol_bridge import get_inbox_store
        return get_inbox_store()
    except Exception:
        logger.debug("[stop-gate] 取 inbox store 失败", exc_info=True)
        return None


def is_stopped(platform: str, account_id: str, chat_key: str, *,
               store: Any = None, use_bridge_store: bool = True) -> str:
    """冻结真相（只读）：返回原因（``stop_contact`` / ``self_harm`` / ``blocklist`` / ``memory``）或空串。"""
    plat = str(platform or "").lower()
    acct = str(account_id or "default")
    ck = str(chat_key or "")
    if not plat or not ck:
        return ""
    with _lock:
        if (plat, acct, ck) in _mem_stopped:
            return "memory"
    st = store if store is not None else (_store() if use_bridge_store else None)
    cg = _compliance()
    if cg is not None and callable(getattr(cg, "contact_stopped", None)):
        try:
            r = str(cg.contact_stopped(st, plat, acct, ck, phone=_digits(ck)) or "")
            if r:
                return r
        except Exception:
            logger.debug("[stop-gate] compliance.contact_stopped 异常", exc_info=True)
    try:
        from src.inbox.account_blocklist import get_blocklist
        if get_blocklist(st).is_blocked(plat, acct, ck):
            return "blocklist"
    except Exception:
        logger.debug("[stop-gate] 名单查询异常", exc_info=True)
    if st is not None:
        try:
            from src.inbox.normalizer import conv_id
            from src.inbox.stop_contact import frozen_reason
            r = frozen_reason(st, conv_id(plat, acct, ck))
            if r:
                return r
        except Exception:
            logger.debug("[stop-gate] frozen_reason 异常", exc_info=True)
    return ""


def apply_stop(platform: str, account_id: str, chat_key: str, *, hits: Iterable[str] = (),
               name: str = "", store: Any = None) -> Dict[str, Any]:
    """冻结一位客户（幂等、绝不抛）：进程内集合 + 名单 + 会话冻结（需人工/标签/档位 manual）。"""
    plat = str(platform or "").lower()
    acct = str(account_id or "default")
    ck = str(chat_key or "")
    hits_l = [str(h)[:40] for h in (hits or []) if str(h)][:6]
    now = time.time()
    out: Dict[str, Any] = {"platform": plat, "memory": False, "blocklisted": False,
                           "frozen": False, "handoff": False}
    if not plat or not ck:
        return out
    with _lock:
        _mem_stopped[(plat, acct, ck)] = now
    out["memory"] = True
    st = store if store is not None else _store()
    cg = _compliance()
    if cg is not None and callable(getattr(cg, "record_stop", None)):
        try:
            r = cg.record_stop(st, platform=plat, account_id=acct, peer=ck,
                               hit="|".join(hits_l), source="official_stop_gate",
                               chat_name=name, now=now) or {}
            out["compliance"] = True
            out["frozen"] = bool(r.get("frozen") or r.get("already"))
            out["blocklisted"] = bool(r.get("listed") or r.get("already"))
        except Exception:
            logger.debug("[stop-gate] compliance.record_stop 异常（回落本地）", exc_info=True)
    try:
        from src.inbox.account_blocklist import get_blocklist
        get_blocklist(st).add(plat, acct, ck, reason="stop_contact",
                              hit_text="|".join(hits_l), ts=now, source="official_stop_gate")
        out["blocklisted"] = True
    except Exception:
        logger.debug("[stop-gate] 名单写入失败", exc_info=True)
    if st is not None and not out.get("compliance"):
        try:
            from src.inbox.stop_contact import freeze_conversation
            r = freeze_conversation(st, platform=plat, account_id=acct, chat_key=ck,
                                    reason="stop_contact", hits=hits_l, chat_name=name, now=now)
            out["frozen"] = bool(r.get("mode_set") or r.get("labelled") or r.get("already_frozen"))
            out["handoff"] = bool(r.get("tagged"))
        except Exception:
            logger.debug("[stop-gate] freeze_conversation 失败", exc_info=True)
    _bump(plat, "stop_hits", last_stop_ts=now)
    logger.warning("[stop-gate] platform=%s account=%s action=stopped hits=%s frozen=%s",
                   plat, acct, "|".join(hits_l) or "-", out["frozen"])
    return out


def inbound_gate(platform: str, account_id: str, chat_key: str, text: Any, *,
                 name: str = "", keywords: Optional[Iterable[str]] = None,
                 store: Any = None) -> Dict[str, Any]:
    """入站闸：返回 ``{action: pass|stopped|frozen, hits, reason}``。非 pass → 调用方不得自答/进管道。"""
    try:
        hits = detect_stop(text, keywords=keywords)
        if hits:
            res = apply_stop(platform, account_id, chat_key, hits=hits, name=name, store=store)
            return {"action": "stopped", "hits": hits, "reason": "stop_contact", "detail": res}
        r = is_stopped(platform, account_id, chat_key, store=store)
        if r:
            _bump(str(platform or "").lower(), "inbound_frozen_skipped")
            return {"action": "frozen", "hits": [], "reason": r}
    except Exception:
        logger.debug("[stop-gate] inbound_gate 异常（按 pass）", exc_info=True)
    return {"action": "pass", "hits": [], "reason": ""}


def _is_farewell_text(text: Any) -> bool:
    try:
        from src.inbox import stop_contact as _sc
        t = str(text or "").strip()
        return bool(t) and t in set(getattr(_sc, "_FAREWELL", {}).values())
    except Exception:
        return False


def outbound_gate(platform: str, account_id: str, chat_key: str, *, text: Any = "",
                  allow_farewell: bool = False, store: Any = None) -> Tuple[bool, str]:
    """出站闸：``(blocked, reason)``。冻结 → 拦（主线告别稿默认也拦，``allow_farewell`` 才放行逐字告别）。"""
    try:
        r = is_stopped(platform, account_id, chat_key, store=store)
        if not r:
            return False, ""
        if allow_farewell and _is_farewell_text(text):
            return False, ""
        _bump(str(platform or "").lower(), "outbound_blocked", last_block_ts=time.time())
        cg = _compliance()
        if cg is not None and callable(getattr(cg, "audit", None)):
            try:
                cg.audit(store if store is not None else _store(), action="blocked",
                         path=f"official:{platform}", platform=str(platform or "").lower(),
                         account_id=str(account_id or ""), peer=str(chat_key or ""), reason=r)
            except Exception:
                logger.debug("[stop-gate] compliance.audit 异常（忽略）", exc_info=True)
        logger.warning("[stop-gate] platform=%s account=%s action=outbound_blocked reason=%s",
                       platform, account_id, r)
        return True, r
    except Exception:
        logger.debug("[stop-gate] outbound_gate 异常（放行）", exc_info=True)
        return False, ""


__all__ = [
    "DEFAULT_STOP_KEYWORDS", "normalize_keyword", "keyword_hit", "detect_stop",
    "is_stopped", "apply_stop", "inbound_gate", "outbound_gate", "stats_snapshot",
    "reset_for_tests",
]
