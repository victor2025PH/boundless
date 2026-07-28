# -*- coding: utf-8 -*-
"""身份影子人工处置：dismiss 持久化 + 证据包 + 多账号安全关联。

影子扫描只发现；本模块是「人点确认/否定」的落地，**绝不在扫描路径自动调用**。

关联口径（2026-07-27）：CPI ``platform_uid`` 实际是记忆键片段
（``make_context_key(chat_key, account_id)``，可能为 ``acct:peer``），
而影子配对只有 ``(platform, chat_key)``。确认关联时必须把该 peer 在 inbox
里出现过的**所有账号分桶键 + 裸 chat_key**一并链到同一 canonical，
否则 ``annotate_already_linked`` 与生产读写会对对齐。
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from src.utils.context_store import make_context_key

logger = logging.getLogger("identity_shadow_actions")

DISMISS_FILENAME = "identity_shadow_dismissed.json"
TOTALS_FILENAME = "identity_shadow_totals.json"
MAX_DISMISSED = 2000
EVIDENCE_MSG_LIMIT = 5


def _norm_platform(p: str) -> str:
    """平台名归一：AI Studio 遗留下拉曾用 ``whatsapp_rpa`` 等 `_rpa` 后缀名，
    与 inbox/影子的 ``whatsapp`` 指同一逻辑平台——读侧容错，写侧一律 inbox 名。"""
    s = str(p or "").strip().lower()
    return s[:-4] if s.endswith("_rpa") else s


def pair_key(platform_a: str, chat_a: str, platform_b: str, chat_b: str) -> str:
    """稳定配对键（平台+chat_key 字典序），与账号无关。"""
    a = (str(platform_a or "").strip().lower(), str(chat_a or "").strip())
    b = (str(platform_b or "").strip().lower(), str(chat_b or "").strip())
    if not a[0] or not a[1] or not b[0] or not b[1]:
        return ""
    if a > b:
        a, b = b, a
    return f"{a[0]}:{a[1]}|{b[0]}:{b[1]}"


def dismiss_path(cfg_dir: Path) -> Path:
    return Path(cfg_dir) / DISMISS_FILENAME


def load_dismissed(path: Path) -> Dict[str, Any]:
    """``{keys: {pair_key: {ts, reason}}, updated_at}``；坏文件 → 空。"""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return {"keys": {}, "updated_at": 0.0}
        keys = raw.get("keys") or {}
        if not isinstance(keys, dict):
            keys = {}
        return {"keys": dict(keys), "updated_at": float(raw.get("updated_at") or 0)}
    except FileNotFoundError:
        return {"keys": {}, "updated_at": 0.0}
    except Exception:
        logger.warning("read dismiss file failed: %s", path, exc_info=True)
        return {"keys": {}, "updated_at": 0.0}


def save_dismissed(path: Path, state: Mapping[str, Any]) -> bool:
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        keys = dict(state.get("keys") or {})
        if len(keys) > MAX_DISMISSED:
            # 按 ts 升序裁最旧
            ordered = sorted(
                keys.items(),
                key=lambda kv: float((kv[1] or {}).get("ts") or 0),
            )
            keys = dict(ordered[-MAX_DISMISSED:])
        payload = {
            "keys": keys,
            "updated_at": float(state.get("updated_at") or time.time()),
        }
        p.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                     encoding="utf-8")
        return True
    except Exception:
        logger.warning("write dismiss file failed: %s", path, exc_info=True)
        return False


def dismiss_pair(
    cfg_dir: Path,
    platform_a: str,
    chat_a: str,
    platform_b: str,
    chat_b: str,
    *,
    reason: str = "not_same_person",
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """标记「不是同一人」；幂等。返回 ``{ok, pair_key, dismissed_total}``。"""
    pk = pair_key(platform_a, chat_a, platform_b, chat_b)
    if not pk:
        return {"ok": False, "error": "bad_pair"}
    path = dismiss_path(cfg_dir)
    st = load_dismissed(path)
    ts = float(now if now is not None else time.time())
    st["keys"][pk] = {"ts": ts, "reason": str(reason or "not_same_person")[:80]}
    st["updated_at"] = ts
    ok = save_dismissed(path, st)
    return {"ok": ok, "pair_key": pk, "dismissed_total": len(st["keys"])}


def is_dismissed(
    dismissed: Mapping[str, Any],
    platform_a: str,
    chat_a: str,
    platform_b: str,
    chat_b: str,
) -> bool:
    pk = pair_key(platform_a, chat_a, platform_b, chat_b)
    keys = (dismissed or {}).get("keys") or {}
    return bool(pk and pk in keys)


def filter_dismissed_pairs(
    pairs: Sequence[Mapping[str, Any]],
    dismissed: Mapping[str, Any],
) -> List[Mapping[str, Any]]:
    """从报告 pairs / sample 里去掉已否定的对（保留 already_linked）。"""
    out: List[Mapping[str, Any]] = []
    for p in pairs or []:
        a, b = p.get("a") or {}, p.get("b") or {}
        if p.get("already_linked"):
            out.append(p)
            continue
        if is_dismissed(
            dismissed,
            str(a.get("platform") or ""), str(a.get("chat_key") or ""),
            str(b.get("platform") or ""), str(b.get("chat_key") or ""),
        ):
            continue
        out.append(p)
    return out


def memory_uids_for_peer(
    account_ids: Iterable[str], chat_key: str,
) -> List[str]:
    """某 peer 在 CPI 里应登记的全部 platform_uid（裸键 + 各账号分桶）。"""
    ck = str(chat_key or "").strip()
    if not ck:
        return []
    uids: Set[str] = {ck}
    for acct in account_ids or []:
        uids.add(make_context_key(ck, str(acct or "")))
    return sorted(uids)


def collect_account_ids(
    inbox_db: Path, platform: str, chat_key: str,
) -> List[str]:
    """inbox 里该 (platform, chat_key) 出现过的 account_id 列表（只读）。"""
    plat = str(platform or "").strip().lower()
    ck = str(chat_key or "").strip()
    if not plat or not ck:
        return []
    try:
        conn = sqlite3.connect(
            f"file:{Path(inbox_db).as_posix()}?mode=ro", uri=True,
            check_same_thread=False,
        )
    except Exception:
        return []
    try:
        rows = conn.execute(
            "SELECT DISTINCT account_id FROM conversations "
            "WHERE platform=? AND chat_key=?",
            (plat, ck),
        ).fetchall()
        return [str(r[0] or "") for r in rows if r and str(r[0] or "").strip()]
    except Exception:
        return []
    finally:
        conn.close()


def confirm_link_pair(
    cpi: Any,
    inbox_db: Path,
    platform_a: str,
    chat_a: str,
    platform_b: str,
    chat_b: str,
    *,
    episodic_store: Any = None,
) -> Dict[str, Any]:
    """人工确认同一人：链两侧所有账号分桶键 + 裸 chat_key → 同一 canonical。

    传入 ``episodic_store`` 时顺带**记忆合流**：关联前逐 uid 捕获旧 canonical
    （resolve 幂等注册默认映射），关联后把旧 canonical 下的历史事实
    ``merge_key`` 进共享 canonical（按 content_hash 去重）——否则关联只对
    未来写入生效、旧记忆永远孤儿（07-27 探明的 CPI 缺口）。

    返回 ``{ok, canonical_id, linked, pair_key, merged_rows, merged_from}``。
    """
    pa = _norm_platform(platform_a)
    pb = _norm_platform(platform_b)
    ca = str(chat_a or "").strip()
    cb = str(chat_b or "").strip()
    pk = pair_key(pa, ca, pb, cb)
    if not pk or not cpi:
        return {"ok": False, "error": "bad_pair_or_cpi"}
    accts_a = collect_account_ids(inbox_db, pa, ca)
    accts_b = collect_account_ids(inbox_db, pb, cb)
    uids_a = memory_uids_for_peer(accts_a, ca)
    uids_b = memory_uids_for_peer(accts_b, cb)
    if not uids_a or not uids_b:
        return {"ok": False, "error": "no_uids"}
    primary = uids_a[0]
    # 关联前捕获每个 uid 的旧 canonical（link 会覆写，之后就取不到了）
    pre: Dict[Tuple[str, str], str] = {}
    for uid in uids_a:
        pre[(pa, uid)] = str(cpi.resolve(pa, uid) or "")
    for uid in uids_b:
        pre[(pb, uid)] = str(cpi.resolve(pb, uid) or "")
    canon = pre[(pa, primary)]
    # 幂等信号：两侧全部 uid 关联前就已指向同一 canonical → 重复确认
    # （验证 ping / 双击），调用方据此跳过合流观测计数，防 totals 虚胀。
    was_already_linked = all(v == canon for v in pre.values())
    linked: List[Dict[str, str]] = [{"platform": pa, "uid": primary}]
    for uid in uids_a[1:]:
        cpi.link(pa, primary, pa, uid)
        linked.append({"platform": pa, "uid": uid})
    for uid in uids_b:
        cpi.link(pa, primary, pb, uid)
        linked.append({"platform": pb, "uid": uid})
    # cluster 传递性：某 uid 先前若已与第三平台成簇（同 canonical），上面的
    # link 只改了本对 uid 的指向，簇友仍指旧 canonical——历史即将被搬走，
    # 不改挂它们未来写入会重新孤儿化。整簇跟着改挂共享 canonical。
    old_canons = sorted({c for c in pre.values() if c and c != canon})
    cluster_relinked: List[Dict[str, str]] = []
    for old in old_canons:
        try:
            members = list(cpi.get_by_canonical(old) or [])
        except Exception:
            members = []
        for mp, mu in members:
            try:
                cpi.link(pa, primary, mp, mu)
                cluster_relinked.append({"platform": mp, "uid": mu})
            except Exception:
                logger.debug(
                    "cluster relink %s:%s failed", mp, mu, exc_info=True)
    # 记忆合流：旧 canonical 的历史事实并入共享 canonical（幂等、绝不抛）
    merged_rows = 0
    merged_from: List[Dict[str, Any]] = []
    if episodic_store is not None:
        for old in old_canons:
            try:
                moved = int(episodic_store.merge_key(old, canon) or 0)
            except Exception:
                logger.debug("merge_key %s failed", old, exc_info=True)
                moved = 0
            if moved:
                merged_rows += moved
                merged_from.append({"from": old, "rows": moved})
    return {
        "ok": True,
        "canonical_id": canon,
        "linked": linked,
        "pair_key": pk,
        "accounts_a": accts_a,
        "accounts_b": accts_b,
        "merged_rows": merged_rows,
        "merged_from": merged_from,
        "cluster_relinked": cluster_relinked,
        "already_linked": was_already_linked,
    }


def _connect_ro(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(
        f"file:{Path(db_path).as_posix()}?mode=ro", uri=True,
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    return conn


def fetch_message_snippets(
    inbox_db: Path,
    platform: str,
    chat_key: str,
    *,
    limit: int = EVIDENCE_MSG_LIMIT,
) -> List[Dict[str, Any]]:
    """取该会话最近几条可读文本（跨 account 合并，按 ts 降序）。"""
    plat = str(platform or "").strip().lower()
    ck = str(chat_key or "").strip()
    lim = max(1, min(int(limit or EVIDENCE_MSG_LIMIT), 20))
    if not plat or not ck:
        return []
    try:
        conn = _connect_ro(inbox_db)
    except Exception:
        return []
    try:
        rows = conn.execute(
            """
            SELECT m.direction, m.text, m.ts, m.sender_name, c.account_id,
                   c.display_name
            FROM messages m
            JOIN conversations c ON c.conversation_id = m.conversation_id
            WHERE c.platform = ? AND c.chat_key = ?
              AND COALESCE(m.revoked, 0) = 0
              AND TRIM(COALESCE(m.text, '')) != ''
            ORDER BY m.ts DESC
            LIMIT ?
            """,
            (plat, ck, lim),
        ).fetchall()
        out: List[Dict[str, Any]] = []
        for r in rows:
            text = str(r["text"] or "").strip().replace("\n", " ")
            if len(text) > 160:
                text = text[:157] + "…"
            out.append({
                "direction": str(r["direction"] or ""),
                "text": text,
                "ts": float(r["ts"] or 0),
                "sender_name": str(r["sender_name"] or ""),
                "account_id": str(r["account_id"] or ""),
                "display_name": str(r["display_name"] or ""),
            })
        return out
    except Exception:
        logger.debug("fetch_message_snippets failed", exc_info=True)
        return []
    finally:
        conn.close()


def build_pair_evidence(
    inbox_db: Path,
    pair: Mapping[str, Any],
    *,
    msg_limit: int = EVIDENCE_MSG_LIMIT,
) -> Dict[str, Any]:
    """影子 pair dict → 人工核对证据包。"""
    a = pair.get("a") or {}
    b = pair.get("b") or {}
    pa, ca = str(a.get("platform") or ""), str(a.get("chat_key") or "")
    pb, cb = str(b.get("platform") or ""), str(b.get("chat_key") or "")
    return {
        "pair_key": pair_key(pa, ca, pb, cb),
        "tier": pair.get("tier") or "",
        "evidence": list(pair.get("evidence") or []),
        "already_linked": bool(pair.get("already_linked")),
        "a": {
            "platform": pa, "chat_key": ca,
            "display_name": str(a.get("display_name") or ""),
            "phone": str(a.get("phone") or ""),
            "username": str(a.get("username") or ""),
            "account_ids": collect_account_ids(inbox_db, pa, ca),
            "messages": fetch_message_snippets(
                inbox_db, pa, ca, limit=msg_limit),
        },
        "b": {
            "platform": pb, "chat_key": cb,
            "display_name": str(b.get("display_name") or ""),
            "phone": str(b.get("phone") or ""),
            "username": str(b.get("username") or ""),
            "account_ids": collect_account_ids(inbox_db, pb, cb),
            "messages": fetch_message_snippets(
                inbox_db, pb, cb, limit=msg_limit),
        },
    }


def totals_path(cfg_dir: Path) -> Path:
    return Path(cfg_dir) / TOTALS_FILENAME


def load_totals(path: Path) -> Dict[str, Any]:
    """合流观测累计（缺失/损坏 → 全零）。独立于扫描 state 文件——
    state 每轮扫描整体重写，累计数放这儿才不会被冲掉。"""
    base = {
        "confirm_pairs": 0,
        "manual_links": 0,
        "merged_rows": 0,
        "cluster_relinked": 0,
        "last": {},
        "updated_at": 0.0,
    }
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return base
        for k in ("confirm_pairs", "manual_links", "merged_rows",
                  "cluster_relinked"):
            base[k] = max(0, int(raw.get(k) or 0))
        if isinstance(raw.get("last"), dict):
            base["last"] = dict(raw["last"])
        base["updated_at"] = float(raw.get("updated_at") or 0)
    except FileNotFoundError:
        pass
    except Exception:
        logger.warning("read totals file failed: %s", path, exc_info=True)
    return base


def record_merge_event(
    cfg_dir: Path,
    kind: str,
    *,
    merged_rows: int = 0,
    cluster_relinked: int = 0,
    canonical: str = "",
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """记一次人工关联动作的合流结果（confirm / manual_link）。

    best-effort：读改写同一 JSON，失败只记日志绝不抛——观测缺一笔
    不能影响关联动作本身。返回更新后的累计。
    """
    k = str(kind or "").strip()
    path = totals_path(cfg_dir)
    tot = load_totals(path)
    if k == "confirm":
        tot["confirm_pairs"] += 1
    elif k == "manual_link":
        tot["manual_links"] += 1
    tot["merged_rows"] += max(0, int(merged_rows or 0))
    tot["cluster_relinked"] += max(0, int(cluster_relinked or 0))
    ts = float(now if now is not None else time.time())
    tot["last"] = {
        "kind": k,
        "canonical": str(canonical or "")[:120],
        "merged_rows": max(0, int(merged_rows or 0)),
        "ts": ts,
        "at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)),
    }
    tot["updated_at"] = ts
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(tot, ensure_ascii=False, indent=2),
                     encoding="utf-8")
    except Exception:
        logger.warning("write totals file failed: %s", path, exc_info=True)
    return tot


def peer_canonicals(
    links: Mapping[Tuple[str, str], str],
    platform: str,
    chat_key: str,
) -> Set[str]:
    """links 里与该 peer 相关的全部 canonical（裸键或 *:chat_key 后缀）。

    平台名经 :func:`_norm_platform` 容错（``whatsapp_rpa`` ≡ ``whatsapp``），
    防 AI Studio 遗留写法导致 already_linked 漏标。
    """
    plat = _norm_platform(platform)
    ck = str(chat_key or "").strip()
    if not plat or not ck:
        return set()
    suffix = ":" + ck
    out: Set[str] = set()
    for (p, uid), canon in (links or {}).items():
        if _norm_platform(p) != plat:
            continue
        u = str(uid or "")
        if u == ck or u.endswith(suffix):
            if canon:
                out.add(str(canon))
    return out


__all__ = [
    "DISMISS_FILENAME", "TOTALS_FILENAME",
    "pair_key", "dismiss_path", "load_dismissed", "save_dismissed",
    "dismiss_pair", "is_dismissed", "filter_dismissed_pairs",
    "memory_uids_for_peer", "collect_account_ids", "confirm_link_pair",
    "fetch_message_snippets", "build_pair_evidence", "peer_canonicals",
    "totals_path", "load_totals", "record_merge_event",
]
