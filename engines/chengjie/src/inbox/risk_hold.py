# -*- coding: utf-8 -*-
"""会话级「风险持有」（Q-3 #264 A，2026-09-10）。

背景（XBGPBN 时间线）：23:49:13 隐私词命中 → 打「需人工」；23:55:42 客户又来一句 → 重新起草
**风险归零**（新稿只看新入站，risk=low → L2 shadow=-）→ 23:56:20 自动发出。此前风险只挂在
**那一条稿**上，会话没有记忆；「需人工」只是**标签**，worker 从不查它。

本模块把风险挂到**会话**上：命中 privacy / commitment / stop_contact / needs_human → ``set``；
之后该会话的每一次判定（``autosend_policy.decide(conversation_id=…)`` / worker 捞稿 / 真发前
二次复检）都先问 ``active(conv)`` → 有 → 强制 L1（人审）并继承 shadow（重新起草不得归零）。
只有**人工动作**（摘标 / 解冻 / 我来回 / 坐席发送）才 ``clear(by=agent)``；系统自动摘标只清
``needs_human`` 这一类泛因（隐私 / 承诺等更具体原因留给人）。TTL 24h 到期自动失效（落一行日志）。

存储：复用 ``InboxStore`` 通用 KV ``app_settings``（键 ``risk_hold:<cid>``，值 JSON
``{reason, hit, set_ts, ttl_h, by, cleared_ts, cleared_by}``），**不动 store.py / 不建表**
（与 Q-14 B ``ai_fail_marker`` 同一先例）。读侧 ``list_app_settings(prefix)`` 一次取全。

接口约定②（Q 批并行）：Q-2 承诺守卫命中 → ``risk_hold.set(conv, "commitment", hit)``；
O-1 A 停联冻结 → ``set(conv, "stop_contact", …)``（只登记，不改其语义）。
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, Iterable, Optional

logger = logging.getLogger(__name__)

KEY_PREFIX = "risk_hold:"
DEFAULT_TTL_H = 24.0
#: 泛因：由「需人工」标签派生，系统自动摘标时可一并清；其余原因只认人工 clear
GENERIC_REASON = "needs_human"
#: 建议的原因码（不强制——Q-2 / 其它线可传自己的码，只要非空）
KNOWN_REASONS = ("privacy", "commitment", "stop_contact", "self_harm", "needs_human",
                 "high_risk", "credential_or_payment_request", "money", "adult")


def _key(cid: str) -> str:
    return KEY_PREFIX + str(cid or "").strip()


def _parse(raw: Any) -> Optional[Dict[str, Any]]:
    try:
        got = json.loads(str(raw or "") or "{}")
        if isinstance(got, dict) and got.get("reason"):
            return got
    except Exception:
        pass
    return None


def _write(store: Any, cid: str, rec: Dict[str, Any]) -> bool:
    try:
        store.set_app_setting(_key(cid), json.dumps(rec, ensure_ascii=False),
                              updated_by="risk_hold")
        return True
    except Exception:
        logger.debug("[risk_hold] 写入失败（忽略）conv=%s", cid, exc_info=True)
        return False


def record(store: Any, cid: str) -> Optional[Dict[str, Any]]:
    """原始记录（含已清 / 已过期的），无 / 脏 → None。绝不抛。"""
    cid = str(cid or "").strip()
    if not cid or store is None or not hasattr(store, "get_app_setting"):
        return None
    try:
        return _parse(store.get_app_setting(_key(cid), ""))
    except Exception:
        return None


def _is_live(rec: Optional[Dict[str, Any]], now: float) -> bool:
    if not rec or float(rec.get("cleared_ts") or 0) > 0:
        return False
    ttl_h = float(rec.get("ttl_h") or DEFAULT_TTL_H)
    return now - float(rec.get("set_ts") or 0) <= ttl_h * 3600.0


def set(store: Any, cid: str, reason: str, hit: Any = "", *,   # noqa: A001（接口约定②命名）
        ttl_h: float = DEFAULT_TTL_H, by: str = "system",
        now: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """登记会话风险持有。返回生效记录；store 缺席 / cid 空 → None。绝不抛。

    幂等纪律：会话上**已有更具体的活跃持有**（reason≠needs_human）而本次只是泛因
    ``needs_human`` → 保留旧记录不覆盖（隐私命中不能被一次打标降成泛因）。其余情况覆盖
    （set_ts 刷新＝TTL 重新计时）。
    """
    cid = str(cid or "").strip()
    r = str(reason or "").strip().lower()
    if not cid or not r or store is None or not hasattr(store, "set_app_setting"):
        return None
    ts = float(now if now is not None else time.time())
    if isinstance(hit, (list, tuple, frozenset)):
        hit_s = "|".join(str(h)[:40] for h in list(hit)[:6] if str(h))
    else:
        hit_s = str(hit or "")[:120]
    prev = record(store, cid)
    prev_live = bool(prev and _is_live(prev, ts))
    if prev_live and r == GENERIC_REASON and str(prev.get("reason") or "") != GENERIC_REASON:
        logger.info("[risk_hold] keep conv=%s reason=%s（新泛因 %s 不覆盖更具体原因）",
                    cid, prev.get("reason"), r)
        return prev
    rec = {
        "reason": r, "hit": hit_s, "set_ts": ts,
        "ttl_h": float(ttl_h or DEFAULT_TTL_H), "by": str(by or "system"),
        "cleared_ts": 0.0, "cleared_by": "",
    }
    if not _write(store, cid, rec):
        return None
    if prev_live and str(prev.get("reason") or "") == r:
        # 同因重复登记（打标链每次入站都会来）：只刷 TTL，不刷屏
        logger.debug("[risk_hold] refresh conv=%s reason=%s", cid, r)
    else:
        logger.info("[risk_hold] set conv=%s reason=%s hit=%s ttl_h=%.0f by=%s",
                    cid, r, hit_s or "-", rec["ttl_h"], rec["by"])
    return rec


def active(store: Any, cid: str, *, now: Optional[float] = None) -> Optional[str]:
    """活跃持有的原因码；无 / 已清 / 已过期 → None。过期时落一行日志并写 cleared_by=ttl。"""
    rec = record(store, cid)
    if not rec:
        return None
    ts = float(now if now is not None else time.time())
    if float(rec.get("cleared_ts") or 0) > 0:
        return None
    if _is_live(rec, ts):
        return str(rec.get("reason") or "") or None
    age_h = (ts - float(rec.get("set_ts") or 0)) / 3600.0
    logger.info("[risk_hold] expired conv=%s reason=%s age_h=%.1f ttl_h=%.0f",
                cid, rec.get("reason"), age_h, float(rec.get("ttl_h") or DEFAULT_TTL_H))
    rec["cleared_ts"] = ts
    rec["cleared_by"] = "ttl"
    _write(store, str(cid or "").strip(), rec)
    return None


def active_record(store: Any, cid: str, *, now: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """活跃持有的完整记录（reason/hit/set_ts…）；非活跃 → None。"""
    if active(store, cid, now=now) is None:
        return None
    return record(store, cid)


def clear(store: Any, cid: str, by: str = "agent", *,
          only_reasons: Optional[Iterable[str]] = None,
          now: Optional[float] = None) -> bool:
    """解除持有。返回「之前确有活跃持有且本次清掉了」。绝不抛。

    ``only_reasons``：仅当活跃原因在此集合内才清（系统自动摘标传 ``("needs_human",)``——
    隐私 / 承诺等更具体原因只认人工）。``by`` 以 ``system`` 开头且未显式给 only_reasons
    → 同样只清泛因（防系统链误清）。
    """
    cid = str(cid or "").strip()
    if not cid or store is None:
        return False
    ts = float(now if now is not None else time.time())
    rec = record(store, cid)
    if not rec or not _is_live(rec, ts):
        return False
    allow = set(str(x) for x in only_reasons) if only_reasons is not None else None
    if allow is None and str(by or "").lower().startswith("system"):
        allow = {GENERIC_REASON}
    if allow is not None and str(rec.get("reason") or "") not in allow:
        logger.debug("[risk_hold] keep conv=%s reason=%s by=%s（不在可清集合）",
                     cid, rec.get("reason"), by)
        return False
    rec["cleared_ts"] = ts
    rec["cleared_by"] = str(by or "agent")
    ok = _write(store, cid, rec)
    if ok:
        logger.info("[risk_hold] clear conv=%s reason=%s by=%s held_min=%.1f",
                    cid, rec.get("reason"), rec["cleared_by"],
                    (ts - float(rec.get("set_ts") or ts)) / 60.0)
    return ok


def all_active(store: Any, *, now: Optional[float] = None) -> Dict[str, Dict[str, Any]]:
    """全部活跃持有 ``{cid: rec}``（会话列表 / 诊断一次取全）。过期的不写回，只不列。"""
    out: Dict[str, Dict[str, Any]] = {}
    if store is None or not hasattr(store, "list_app_settings"):
        return out
    ts = float(now if now is not None else time.time())
    try:
        for row in store.list_app_settings(KEY_PREFIX) or []:
            k = str(row.get("key") or "")
            if not k.startswith(KEY_PREFIX):
                continue
            rec = _parse(row.get("value"))
            if rec and _is_live(rec, ts):
                out[k[len(KEY_PREFIX):]] = rec
    except Exception:
        logger.debug("[risk_hold] 列举失败（忽略）", exc_info=True)
    return out


__all__ = ["KEY_PREFIX", "DEFAULT_TTL_H", "GENERIC_REASON", "KNOWN_REASONS",
           "set", "active", "active_record", "record", "clear", "all_active"]
