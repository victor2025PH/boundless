# -*- coding: utf-8 -*-
"""情景记忆 memory_key → 人类可读身份（昵称 / 用户名 / 头像）解析。

「AI 记忆」后台此前直接把存储键（``telegram:8244899900:8921664288``）当界面列
展示，运营认不出是谁。本模块把键翻译回「人」：memory_key 的规范形态
``platform:account_id:peer`` 与收件箱 ``conversations`` 表主键
``conversation_id``（``f"{platform}:{account_id}:{chat_key}"``）**同构**，
身份解析本质是主键点查（批量 IN 一次），不是模糊匹配。

四种键形态与解析策略（错认比不认伤害大 → 低置信一律如实 unresolved）：

- **canonical** ``plat:acct:peer``：conversation_id 点查，命中即高置信；
- **canonical 群成员** ``plat:acct:<gid>_<uid>``（memory.scope=chat_user）：
  成员私聊会话（``plat:acct:uid``）优先出昵称/头像，群会话（``plat:acct:gid``）
  出群名作上下文；两者都缺 → unresolved；
- **acct_peer** ``acct:peer``（CPI 未启用时的历史形态）：按已知平台枚举
  ``<plat>:acct:peer`` 候选点查，**恰好一个平台命中**才认（approx 标记）；
- **bare** ``peer``（更早历史遗留，另有 key-health 迁移工具治理存量）：
  ``chat_key`` 精确匹配且**全库唯一**才认（approx），0 或多个命中 → unresolved。

设计约束：
- 只读连接（``mode=ro`` URI，仓内惯例），对活体生产 inbox.db 零写风险；
- 进程级 TTL 缓存（默认 120s、容量上限），列表反复刷新时零重复 IO；
- 全程软失败：任何异常返回已有结果/空映射，绝不阻断记忆列表主功能。
"""
from __future__ import annotations

import logging
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger("ai_chat_assistant.episodic_identity")

# 与 inbox/CPI 一致的平台命名空间（acct_peer 形态枚举候选用）
KNOWN_PLATFORMS: Tuple[str, ...] = (
    "telegram", "whatsapp", "line", "messenger", "instagram", "zalo", "qqbot",
    "qq",
)

# 群成员键 peer 段：``<gid>_<uid>``（telegram 群 id 可为负数；uid 纯数字）。
# LINE/WA 的字母数字 id 不含该形态，正则收紧到纯数字防误拆。
_GROUP_PEER_RE = re.compile(r"^(-?\d+)_(\d+)$")

_CACHE_TTL = 120.0
_CACHE_MAX = 4096
_CACHE: Dict[Tuple[str, str], Tuple[float, Dict[str, Any]]] = {}

# conversations 的 username/phone/avatar_url 是迁移追加列，老库可能缺 →
# 首查探测一次，缺列回落基础列集（进程级记忆，按库路径分桶）。
_FULL_COLS = ("conversation_id", "platform", "account_id", "chat_key",
              "display_name", "chat_type", "username", "phone", "avatar_url")
_BASE_COLS = ("conversation_id", "platform", "account_id", "chat_key",
              "display_name", "chat_type")
_HAS_FULL_COLS: Dict[str, bool] = {}


def _clear_cache() -> None:
    """测试隔离用。"""
    _CACHE.clear()
    _HAS_FULL_COLS.clear()


def parse_memory_key(key: str) -> Dict[str, str]:
    """纯函数：拆 memory_key → ``{form, platform, account_id, peer, group_id, member_id}``。

    form ∈ canonical / acct_peer / bare / empty。group_id/member_id 仅在 peer
    命中群成员形态时非空（任意 form 都可能叠加群形态）。
    """
    k = str(key or "").strip()
    out = {"form": "empty", "platform": "", "account_id": "", "peer": "",
           "group_id": "", "member_id": ""}
    if not k:
        return out
    parts = k.split(":", 2)
    if len(parts) == 3 and parts[0].lower() in KNOWN_PLATFORMS:
        out.update(form="canonical", platform=parts[0].lower(),
                   account_id=parts[1], peer=parts[2])
    elif len(parts) == 2 and parts[0].lower() not in KNOWN_PLATFORMS:
        out.update(form="acct_peer", account_id=parts[0], peer=parts[1])
    else:
        # 单段（bare），或看似平台前缀但段数不足的畸形键 → 按 bare 兜底
        out.update(form="bare", peer=k)
    m = _GROUP_PEER_RE.match(out["peer"])
    if m:
        out["group_id"] = m.group(1)
        out["member_id"] = m.group(2)
    return out


def _connect_ro(db_path) -> sqlite3.Connection:
    conn = sqlite3.connect(
        f"file:{Path(db_path).as_posix()}?mode=ro", uri=True, timeout=5,
    )
    conn.row_factory = sqlite3.Row
    return conn


def _select_cols(conn: sqlite3.Connection, db_key: str) -> Tuple[str, ...]:
    has_full = _HAS_FULL_COLS.get(db_key)
    if has_full is None:
        try:
            cols = {str(r[1]) for r in conn.execute(
                "PRAGMA table_info(conversations)").fetchall()}
            has_full = {"username", "phone", "avatar_url"} <= cols
        except Exception:
            has_full = False
        _HAS_FULL_COLS[db_key] = has_full
    return _FULL_COLS if has_full else _BASE_COLS


def _row_to_dict(row: sqlite3.Row, cols: Sequence[str]) -> Dict[str, str]:
    d = {c: str(row[c] or "") for c in cols}
    for c in _FULL_COLS:
        d.setdefault(c, "")
    return d


def _fetch_by_ids(
    conn: sqlite3.Connection, cols: Sequence[str], ids: Sequence[str],
) -> Dict[str, Dict[str, str]]:
    """conversation_id IN (...) 批查（分块避开 SQLite 999 参数上限）。"""
    out: Dict[str, Dict[str, str]] = {}
    ids = [i for i in dict.fromkeys(ids) if i]
    sel = ", ".join(cols)
    for i in range(0, len(ids), 400):
        chunk = ids[i:i + 400]
        rows = conn.execute(
            f"SELECT {sel} FROM conversations WHERE conversation_id IN "
            f"({','.join('?' * len(chunk))})", chunk,
        ).fetchall()
        for r in rows:
            d = _row_to_dict(r, cols)
            out[d["conversation_id"]] = d
    return out


def _fetch_by_chat_keys(
    conn: sqlite3.Connection, cols: Sequence[str], keys: Sequence[str],
) -> Dict[str, List[Dict[str, str]]]:
    """chat_key IN (...) 批查（bare 形态兜底），按 chat_key 分组返回。"""
    out: Dict[str, List[Dict[str, str]]] = {}
    keys = [k for k in dict.fromkeys(keys) if k]
    sel = ", ".join(cols)
    for i in range(0, len(keys), 400):
        chunk = keys[i:i + 400]
        rows = conn.execute(
            f"SELECT {sel} FROM conversations WHERE chat_key IN "
            f"({','.join('?' * len(chunk))})", chunk,
        ).fetchall()
        for r in rows:
            d = _row_to_dict(r, cols)
            out.setdefault(d["chat_key"], []).append(d)
    return out


def _fetch_contact_names(
    conn: sqlite3.Connection, specs: Sequence[Tuple[str, str, str]],
) -> Dict[Tuple[str, str, str], str]:
    """通讯录名兜底（P3）：``protocol_contacts`` 按 (platform, account_id, chat_key)
    精确匹配取名（``name`` 优先、``notify_name`` 兜底，与
    ``InboxStore.get_protocol_contact_name`` 同口径）。

    场景＝「加了好友但从没开口」：联系人存在而会话行不存在，此前只能 unresolved。
    仅 canonical 形态使用（platform/account 已知，精确三元组零错认面）；
    老库无此表 → 静默空映射。
    """
    out: Dict[Tuple[str, str, str], str] = {}
    todo = [s for s in dict.fromkeys(specs) if s[0] and s[1] and s[2]]
    if not todo:
        return out
    want = set(todo)
    keys = sorted({s[2] for s in todo})
    try:
        for i in range(0, len(keys), 400):
            chunk = keys[i:i + 400]
            rows = conn.execute(
                "SELECT platform, account_id, chat_key, name, notify_name "
                "FROM protocol_contacts WHERE chat_key IN "
                f"({','.join('?' * len(chunk))})", chunk,
            ).fetchall()
            for r in rows:
                tup = (str(r[0] or "").lower(), str(r[1] or ""), str(r[2] or ""))
                if tup in want:
                    nm = str(r[3] or "") or str(r[4] or "")
                    if nm:
                        out[tup] = nm
    except sqlite3.Error:
        return {}
    return out


def _identity_from_row(
    row: Dict[str, str], *, approx: bool = False,
) -> Dict[str, Any]:
    name = row.get("display_name") or ""
    # 「裸号码当名字」不算真名（与收件箱补名逻辑同口径）
    if name == row.get("chat_key"):
        name = ""
    return {
        "resolved": True,
        "approx": bool(approx),
        "kind": "group" if row.get("chat_type") == "group" else "private",
        "name": name or row.get("username") or row.get("phone")
                or row.get("chat_key") or "",
        "username": row.get("username") or "",
        "phone": row.get("phone") or "",
        "avatar_url": row.get("avatar_url") or "",
        "platform": row.get("platform") or "",
        "account_id": row.get("account_id") or "",
        "chat_key": row.get("chat_key") or "",
        "conversation_id": row.get("conversation_id") or "",
        "group_name": "",
        "member_id": "",
    }


def _unresolved(parsed: Dict[str, str]) -> Dict[str, Any]:
    return {
        "resolved": False, "approx": False, "kind": "unknown",
        "name": "", "username": "", "phone": "", "avatar_url": "",
        "platform": parsed.get("platform") or "",
        "account_id": parsed.get("account_id") or "",
        "chat_key": parsed.get("peer") or "",
        "conversation_id": "", "group_name": "",
        "member_id": parsed.get("member_id") or "",
    }


def resolve_identities(
    db_path, keys: Iterable[str], *, now: Optional[float] = None,
) -> Dict[str, Dict[str, Any]]:
    """批量解析 memory_key → 身份块。任何异常软失败（返回已解析部分/空映射）。"""
    ts = now if now is not None else time.time()
    db_key = str(db_path)
    want = [str(k or "").strip() for k in dict.fromkeys(keys)]
    want = [k for k in want if k]
    out: Dict[str, Dict[str, Any]] = {}
    misses: List[str] = []
    for k in want:
        hit = _CACHE.get((db_key, k))
        if hit and ts - hit[0] < _CACHE_TTL:
            out[k] = hit[1]
        else:
            misses.append(k)
    if not misses:
        return out
    try:
        resolved = _resolve_uncached(db_path, db_key, misses)
    except Exception:
        logger.debug("[episodic_identity] resolve 软失败", exc_info=True)
        return out
    if len(_CACHE) + len(resolved) > _CACHE_MAX:
        _CACHE.clear()
    for k, ident in resolved.items():
        _CACHE[(db_key, k)] = (ts, ident)
        out[k] = ident
    return out


def _resolve_uncached(
    db_path, db_key: str, keys: List[str],
) -> Dict[str, Dict[str, Any]]:
    parsed = {k: parse_memory_key(k) for k in keys}
    # 收集候选 conversation_id（点查集）与 bare 兜底 chat_key 集
    cand_ids: List[str] = []
    bare_peers: List[str] = []
    for p in parsed.values():
        if p["form"] == "canonical":
            plat, acct, peer = p["platform"], p["account_id"], p["peer"]
            if p["group_id"]:
                cand_ids.append(f"{plat}:{acct}:{p['member_id']}")
                cand_ids.append(f"{plat}:{acct}:{p['group_id']}")
            else:
                cand_ids.append(f"{plat}:{acct}:{peer}")
        elif p["form"] == "acct_peer":
            for plat in KNOWN_PLATFORMS:
                cand_ids.append(f"{plat}:{p['account_id']}:{p['peer']}")
        elif p["form"] == "bare":
            bare_peers.append(p["peer"])

    conn = _connect_ro(db_path)
    try:
        cols = _select_cols(conn, db_key)
        by_id = _fetch_by_ids(conn, cols, cand_ids) if cand_ids else {}
        by_ck = (_fetch_by_chat_keys(conn, cols, bare_peers)
                 if bare_peers else {})
        # 通讯录名兜底（P3）：canonical 键在会话表未命中的部分再查 protocol_contacts
        # （「加了好友没开口」场景：有联系人、无会话行）
        contact_specs: List[Tuple[str, str, str]] = []
        for p in parsed.values():
            if p["form"] != "canonical":
                continue
            plat, acct = p["platform"], p["account_id"]
            if p["group_id"]:
                if f"{plat}:{acct}:{p['member_id']}" not in by_id:
                    contact_specs.append((plat, acct, p["member_id"]))
            elif f"{plat}:{acct}:{p['peer']}" not in by_id:
                contact_specs.append((plat, acct, p["peer"]))
        contacts = (_fetch_contact_names(conn, contact_specs)
                    if contact_specs else {})
    finally:
        conn.close()

    out: Dict[str, Dict[str, Any]] = {}
    for k in keys:
        p = parsed[k]
        ident: Dict[str, Any]
        if p["form"] == "canonical" and not p["group_id"]:
            row = by_id.get(f"{p['platform']}:{p['account_id']}:{p['peer']}")
            cnm = contacts.get(
                (p["platform"], p["account_id"], p["peer"]), "")
            if row:
                ident = _identity_from_row(row)
            elif cnm:
                # 通讯录命中（精确三元组）：出名字；无会话行 → 不给跳转/头像
                ident = _unresolved(p)
                ident["resolved"] = True
                ident["kind"] = "private"
                ident["name"] = cnm
            else:
                ident = _unresolved(p)
        elif p["form"] == "canonical" and p["group_id"]:
            member = by_id.get(
                f"{p['platform']}:{p['account_id']}:{p['member_id']}")
            group = by_id.get(
                f"{p['platform']}:{p['account_id']}:{p['group_id']}")
            cnm = contacts.get(
                (p["platform"], p["account_id"], p["member_id"]), "")
            if member or group:
                base = member or group
                ident = _identity_from_row(base)
                ident["kind"] = "group"
                ident["member_id"] = p["member_id"]
                ident["group_name"] = (
                    (group or {}).get("display_name")
                    or (group or {}).get("chat_key")
                    or p["group_id"])
                if not member:
                    # 只有群会话可考：昵称/头像不冒充群的；成员名走通讯录兜底
                    ident["name"] = cnm
                    ident["username"] = ""
                    ident["phone"] = ""
                    ident["avatar_url"] = ""
                    ident["chat_key"] = p["member_id"]
            elif cnm:
                ident = _unresolved(p)
                ident["resolved"] = True
                ident["kind"] = "group"
                ident["name"] = cnm
                ident["group_name"] = p["group_id"]
                ident["chat_key"] = p["member_id"]
            else:
                ident = _unresolved(p)
        elif p["form"] == "acct_peer":
            hits = [by_id[f"{plat}:{p['account_id']}:{p['peer']}"]
                    for plat in KNOWN_PLATFORMS
                    if f"{plat}:{p['account_id']}:{p['peer']}" in by_id]
            ident = (_identity_from_row(hits[0], approx=True)
                     if len(hits) == 1 else _unresolved(p))
        elif p["form"] == "bare":
            rows = by_ck.get(p["peer"], [])
            ident = (_identity_from_row(rows[0], approx=True)
                     if len(rows) == 1 else _unresolved(p))
        else:
            ident = _unresolved(p)
        out[k] = ident
    return out


def find_conversation_keys(db_path, q: str, limit: int = 200) -> List[str]:
    """按肉眼可见条件（昵称/用户名/手机号）搜会话 → conversation_id 列表。

    供「按人搜记忆」把人名翻译成记忆键集合（memory_key == conversation_id）。
    最近活跃优先；老库缺 username/phone 列时自动回落只搜昵称；异常返回空。
    """
    qq = str(q or "").strip()
    if not qq:
        return []
    lim = max(1, min(int(limit or 200), 500))
    like = f"%{qq}%"
    try:
        conn = _connect_ro(db_path)
    except Exception:
        return []
    try:
        try:
            rows = conn.execute(
                "SELECT conversation_id FROM conversations WHERE "
                "display_name LIKE ? OR username LIKE ? OR phone LIKE ? "
                "ORDER BY last_ts DESC LIMIT ?", (like, like, like, lim),
            ).fetchall()
        except sqlite3.OperationalError:
            rows = conn.execute(
                "SELECT conversation_id FROM conversations WHERE "
                "display_name LIKE ? ORDER BY last_ts DESC LIMIT ?",
                (like, lim),
            ).fetchall()
        return [str(r[0]) for r in rows if r[0]]
    except Exception:
        logger.debug("[episodic_identity] find_conversation_keys 软失败",
                     exc_info=True)
        return []
    finally:
        conn.close()
