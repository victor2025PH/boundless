# -*- coding: utf-8 -*-
"""联系人级称呼（#155 人设归属层级，2026-09-03）。

「你称呼对方（爱称）」与「对方称呼你」这两格此前长在**人设**档案里
（personas.html 基本补充区，落 ``names.call_peer`` / ``names.peer_calls_you``），
可它们描述的根本不是人设自己——是**这段客户关系**：同一个人设服务一百个客户，
不可能对每个人都叫 babe；换个客户就得改人设档，改完还影响另外九十九个。

放错层级的直接后果：运营要么不敢填（怕串味），要么填了之后所有客户共用一个
爱称。#155 把它挪到它该在的地方——按联系人存，人设档只留「人设自己是谁」。

存储：inbox 库里的独立小表 ``conversation_contact_names``（键=会话 id，与
``conversation_meta`` 同键同域）。刻意不改 ``store.py``：
``update_conv_meta`` 只认固定列，加字段要动主表迁移；而这两格是读路径的
展示/注入决定，独立表更好回滚，且 ``store.delete_conversation_data`` 按
「含 conversation_id 列的表」结构发现式清理，删会话时本表自动跟着清。

注入优先级（``resolve_address_names``）：联系人级 > 人设级（存量兼容）。
人设级值在迁移后仍读得到——存量部署升级即生效，不必等运营逐个重填。

全部 best-effort：任何异常按「没有配置」处理，绝不成为出话链故障点。
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_DDL = """
CREATE TABLE IF NOT EXISTS conversation_contact_names (
    conversation_id TEXT PRIMARY KEY,
    call_peer       TEXT NOT NULL DEFAULT '',
    peer_calls_you  TEXT NOT NULL DEFAULT '',
    updated_at      REAL NOT NULL DEFAULT 0,
    updated_by      TEXT NOT NULL DEFAULT ''
)
"""

#: 称呼长度上限（爱称就是爱称；过长多半是误粘贴整段文本，截断防注入膨胀）
MAX_LEN = 40

_ensured: set = set()
_ensure_lock = threading.Lock()


def _ensure_table(store: Any) -> bool:
    """建表（每个 store 实例只跑一次）。失败返回 False，调用方按「无此能力」降级。"""
    key = id(store)
    with _ensure_lock:
        if key in _ensured:
            return True
    try:
        with store._lock:                    # noqa: SLF001（复用 store 的连接与锁）
            store._conn.execute(_DDL)        # noqa: SLF001
            store._conn.commit()             # noqa: SLF001
    except Exception:
        logger.debug("[contact_names] 建表失败", exc_info=True)
        return False
    with _ensure_lock:
        _ensured.add(key)
    return True


def _clean(v: Any) -> str:
    return str(v or "").strip()[:MAX_LEN]


def get_contact_names(store: Any, conversation_id: str) -> Dict[str, str]:
    """读该会话的联系人级称呼。无记录/异常 → 两个空串。"""
    out = {"call_peer": "", "peer_calls_you": ""}
    cid = str(conversation_id or "").strip()
    if store is None or not cid or not _ensure_table(store):
        return out
    try:
        with store._lock:                    # noqa: SLF001
            row = store._conn.execute(       # noqa: SLF001
                "SELECT call_peer, peer_calls_you FROM"
                " conversation_contact_names WHERE conversation_id=?",
                (cid,)).fetchone()
    except Exception:
        logger.debug("[contact_names] 读取失败 cid=%s", cid, exc_info=True)
        return out
    if row is None:
        return out
    out["call_peer"] = str(row["call_peer"] or "")
    out["peer_calls_you"] = str(row["peer_calls_you"] or "")
    return out


def set_contact_names(
    store: Any, conversation_id: str, *,
    call_peer: Optional[str] = None,
    peer_calls_you: Optional[str] = None,
    updated_by: str = "",
) -> bool:
    """写该会话的联系人级称呼（只更新显式传入的字段；空串＝清除该字段）。

    ``None``＝不动该字段（部分更新语义，与 store 的 identity 回填同款）；
    传空串是**显式清除**——运营改主意「这个客户不用爱称」必须做得到。
    返回是否写入成功。
    """
    cid = str(conversation_id or "").strip()
    if store is None or not cid or not _ensure_table(store):
        return False
    if call_peer is None and peer_calls_you is None:
        return False
    cur = get_contact_names(store, cid)
    cp = cur["call_peer"] if call_peer is None else _clean(call_peer)
    py = cur["peer_calls_you"] if peer_calls_you is None else _clean(peer_calls_you)
    try:
        import time as _t
        with store._lock:                    # noqa: SLF001
            store._conn.execute(             # noqa: SLF001
                "INSERT INTO conversation_contact_names"
                " (conversation_id, call_peer, peer_calls_you, updated_at,"
                "  updated_by) VALUES (?,?,?,?,?)"
                " ON CONFLICT(conversation_id) DO UPDATE SET"
                "   call_peer      = excluded.call_peer,"
                "   peer_calls_you = excluded.peer_calls_you,"
                "   updated_at     = excluded.updated_at,"
                "   updated_by     = excluded.updated_by",
                (cid, cp, py, _t.time(), str(updated_by or "")[:64]))
            store._conn.commit()             # noqa: SLF001
    except Exception:
        logger.debug("[contact_names] 写入失败 cid=%s", cid, exc_info=True)
        return False
    return True


def resolve_address_names(
    store: Any, conversation_id: str, persona: Optional[Dict[str, Any]] = None,
) -> Dict[str, str]:
    """本次出话该用的称呼：联系人级 > 人设级（存量兼容）。

    #155：这两个值本质是客户关系属性，联系人级才是它们的家；人设级是迁移前
    的存量位置，仍然读——存量部署升级即生效，不必等运营逐个重填。逐字段回落
    （联系人只配了 call_peer 时，peer_calls_you 仍可用人设级的值）。
    异常 → 全空（守卫与注入块自然不出，等于本功能未配置）。
    """
    out = {"call_peer": "", "peer_calls_you": ""}
    try:
        p_names = (persona or {}).get("names") if isinstance(persona, dict) else {}
        if not isinstance(p_names, dict):
            p_names = {}
        contact = get_contact_names(store, conversation_id)
        out["call_peer"] = (contact.get("call_peer")
                            or _clean(p_names.get("call_peer")))
        out["peer_calls_you"] = (contact.get("peer_calls_you")
                                 or _clean(p_names.get("peer_calls_you")))
    except Exception:
        logger.debug("[contact_names] 称呼解析失败 cid=%s", conversation_id,
                     exc_info=True)
        return {"call_peer": "", "peer_calls_you": ""}
    return out


def overlay_sendpoint_names(
    names: Dict[str, Any], platform: str, account_id: str, chat_key: str,
) -> Dict[str, Any]:
    """给出站呼格守卫的名字包叠上联系人级称呼（#155）。

    守卫（``sendpoint_guard.resolve_sendpoint_names``）读的是人设级值；prompt
    注入已按联系人级走。两处不一致就会互相打架——守卫会按人设的 babe 去「纠正」
    本该是联系人 honey 的正确文本。本函数在调用点叠一层，让两侧同源。
    ``self_names`` 等其余键原样保留。store 不可用/无配置 → 原样返回。
    """
    out = dict(names or {})
    try:
        plat = str(platform or "").strip().lower()
        acct = str(account_id or "").strip()
        ck = str(chat_key or "").strip()
        if not (plat and acct and ck):
            return out
        from src.integrations.protocol_bridge import get_inbox_store
        store = get_inbox_store()
        if store is None:
            return out
        contact = get_contact_names(store, f"{plat}:{acct}:{ck}")
        for key in ("call_peer", "peer_calls_you"):
            if contact.get(key):
                out[key] = contact[key]
    except Exception:
        logger.debug("[contact_names] 守卫名字包叠加失败（保留人设级）",
                     exc_info=True)
    return out


def migrate_persona_names_to_contacts(
    store: Any, persona_id: str, names: Dict[str, Any], *,
    conversation_ids: Any = None, updated_by: str = "migration",
) -> int:
    """存量迁移：把人设级称呼写成该人设已绑定会话的**联系人级默认值**。

    #155 的存量承接：人设档里已经填了 babe 的部署，迁移后这些会话照旧叫
    babe（行为零变化），但从此各会话可独立改。只写**尚未设置**的会话——
    联系人级已有值＝运营在新层做过决定，绝不覆盖。返回实际写入的会话数。
    """
    cp = _clean((names or {}).get("call_peer"))
    py = _clean((names or {}).get("peer_calls_you"))
    if not (cp or py) or store is None:
        return 0
    cids = [str(c).strip() for c in (conversation_ids or []) if str(c or "").strip()]
    if not cids:
        return 0
    n = 0
    for cid in cids:
        try:
            cur = get_contact_names(store, cid)
            if cur["call_peer"] or cur["peer_calls_you"]:
                continue          # 新层已有决定 → 不覆盖
            if set_contact_names(store, cid, call_peer=cp, peer_calls_you=py,
                                 updated_by=updated_by):
                n += 1
        except Exception:
            continue
    if n:
        logger.info(
            "[contact_names] #155 存量迁移：人设 %s 的称呼已落为 %d 个会话的"
            "联系人级默认值（行为不变，此后各会话可独立改）", persona_id, n)
    return n


def migrate_all_personas(store: Any, persona_manager: Any = None) -> int:
    """全量存量迁移：每个人设的称呼 → 其已绑定会话的联系人级默认值。

    #155 上线一次性搬运（幂等，可反复跑）：绑定关系取自 PersonaManager 的
    会话绑定表（3 段会话覆写键 ``platform:account:chat_key``）与账号级绑定
    （该账号名下全部会话）。返回写入的会话数；任何异常按 0 处理。

    行为承诺：迁移后所有会话的**生效爱称与迁移前逐字节一致**——搬的是同一个
    值，只是换了个可以按客户改的地方。
    """
    if store is None:
        return 0
    try:
        if persona_manager is None:
            from src.utils.persona_manager import PersonaManager
            persona_manager = PersonaManager.get_instance()
        from src.ai.persona_voice import is_conv_binding_key
    except Exception:
        logger.debug("[contact_names] 迁移前置不可用", exc_info=True)
        return 0
    total = 0
    try:
        bindings = dict(getattr(persona_manager, "_chat_bindings", {}) or {})
    except Exception:
        bindings = {}
    # 会话级绑定：键本身就是 conversation_id
    by_persona: Dict[str, list] = {}
    for key, pid in bindings.items():
        try:
            if not is_conv_binding_key(str(key)):
                continue
            by_persona.setdefault(str(pid), []).append(str(key))
        except Exception:
            continue
    # 账号级绑定：该账号名下全部会话共享账号人设
    try:
        from src.integrations.account_registry import get_account_registry
        for row in (get_account_registry().list(include_removed=True) or []):
            meta = row.get("meta") or {}
            pid = str(meta.get("persona_id") or "").strip()
            plat = str(row.get("platform") or "").strip().lower()
            acct = str(row.get("account_id") or "").strip()
            if not (pid and plat and acct):
                continue
            prefix = f"{plat}:{acct}:"
            with store._lock:                # noqa: SLF001
                rows = store._conn.execute(  # noqa: SLF001
                    "SELECT conversation_id FROM conversations"
                    " WHERE substr(conversation_id, 1, ?) = ?",
                    (len(prefix), prefix)).fetchall()
            for r in rows:
                by_persona.setdefault(pid, []).append(str(r[0]))
    except Exception:
        logger.debug("[contact_names] 账号级绑定枚举失败（只迁会话级）",
                     exc_info=True)
    for pid, cids in by_persona.items():
        try:
            persona = persona_manager.get_persona_by_id(pid)
            names = (persona or {}).get("names") or {}
            if not isinstance(names, dict):
                continue
            total += migrate_persona_names_to_contacts(
                store, pid, names, conversation_ids=sorted(set(cids)))
        except Exception:
            continue
    return total


def _reset_for_tests() -> None:
    """清建表标记（测试用；不同 tmp 库间隔离）。"""
    with _ensure_lock:
        _ensured.clear()


__all__ = [
    "MAX_LEN",
    "get_contact_names",
    "migrate_all_personas",
    "migrate_persona_names_to_contacts",
    "overlay_sendpoint_names",
    "resolve_address_names",
    "set_contact_names",
]
