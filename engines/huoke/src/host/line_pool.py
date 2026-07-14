# -*- coding: utf-8 -*-
"""Phase 11 (2026-04-25): LINE account pool CRUD + 轮循分配.

设计思路:
  - 表 line_accounts 存池子 (line_id 唯一, owner_device_id/persona_key/region 分群).
  - allocate() 用 ``last_used_at ASC`` 轮循 + 24h cap check, 取到后立即
    UPDATE last_used_at + times_used, 同时写 line_dispatch_log 审计.
  - 整个 allocate 在一个 transaction 内, 避免并发撞同一个账号超 cap.
  - CSV/XLSX 批量导入由上层 router 解析后调 add_many().
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from src.host.database import _connect

logger = logging.getLogger(__name__)


VALID_STATUSES = frozenset({"active", "cooldown", "banned", "disabled"})


def _now_iso() -> str:
    import datetime as _dt
    return _dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def _row_to_dict(row) -> Dict[str, Any]:
    return {
        "id": row["id"],
        "line_id": row["line_id"],
        "owner_device_id": row["owner_device_id"] or "",
        "persona_key": row["persona_key"] or "",
        "region": row["region"] or "",
        "status": row["status"] or "active",
        "last_used_at": row["last_used_at"] or "",
        "times_used": int(row["times_used"] or 0),
        "daily_cap": int(row["daily_cap"] or 20),
        "notes": row["notes"] or "",
        "created_at": row["created_at"] or "",
        "updated_at": row["updated_at"] or "",
    }


# ─── 增 ─────────────────────────────────────────────────────────────────

def add(line_id: str, *,
        owner_device_id: str = "",
        persona_key: str = "",
        region: str = "",
        status: str = "active",
        daily_cap: int = 20,
        notes: str = "") -> int:
    """新增一条 LINE 账号. line_id 重复 → 抛 ValueError."""
    line_id = (line_id or "").strip()
    if not line_id:
        raise ValueError("line_id 必填")
    if status not in VALID_STATUSES:
        raise ValueError(f"status 必须在 {sorted(VALID_STATUSES)}")
    try:
        with _connect() as conn:
            cur = conn.execute(
                "INSERT INTO line_accounts (line_id, owner_device_id, persona_key,"
                " region, status, daily_cap, notes) VALUES (?,?,?,?,?,?,?)",
                (line_id, owner_device_id.strip(), persona_key.strip(),
                 region.strip(), status, int(daily_cap), notes.strip()),
            )
            return int(cur.lastrowid)
    except Exception as e:
        msg = str(e).lower()
        if "unique" in msg or "constraint" in msg:
            raise ValueError(f"line_id 已存在: {line_id}") from e
        raise


def add_many(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    """批量导入. 返回 {inserted, duplicate, invalid, errors: [(line_id, reason)]}.

    单条失败不中断, 全部跑完. 空 line_id / status 非法 → invalid.
    """
    inserted = 0
    duplicate = 0
    invalid = 0
    errors: List[Dict[str, str]] = []
    for rec in records or []:
        line_id = (rec.get("line_id") or "").strip()
        if not line_id:
            invalid += 1
            errors.append({"line_id": "", "reason": "empty line_id"})
            continue
        try:
            add(line_id,
                owner_device_id=rec.get("owner_device_id", "") or "",
                persona_key=rec.get("persona_key", "") or "",
                region=rec.get("region", "") or "",
                status=rec.get("status", "active") or "active",
                daily_cap=int(rec.get("daily_cap", 20) or 20),
                notes=rec.get("notes", "") or "")
            inserted += 1
        except ValueError as e:
            if "已存在" in str(e):
                duplicate += 1
            else:
                invalid += 1
            errors.append({"line_id": line_id, "reason": str(e)})
    return {"inserted": inserted, "duplicate": duplicate,
            "invalid": invalid, "total": len(records or []),
            "errors": errors[:50]}  # cap error list 避免 response 过大


# ─── 查 ─────────────────────────────────────────────────────────────────

def list_accounts(*,
                  status: Optional[str] = None,
                  region: Optional[str] = None,
                  persona_key: Optional[str] = None,
                  owner_device_id: Optional[str] = None,
                  limit: int = 200,
                  includes_24h_stats: bool = False
                  ) -> List[Dict[str, Any]]:
    where = []
    args: List[Any] = []
    if status:
        where.append("status = ?")
        args.append(status)
    if region:
        where.append("region = ?")
        args.append(region)
    if persona_key:
        where.append("persona_key = ?")
        args.append(persona_key)
    if owner_device_id:
        where.append("owner_device_id = ?")
        args.append(owner_device_id)
    sql = "SELECT * FROM line_accounts"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(max(1, min(int(limit or 200), 2000)))
    with _connect() as conn:
        out = [_row_to_dict(r) for r in conn.execute(sql, args).fetchall()]
        # Phase 11.1: 可选 24h 使用次数 (供 UI 显示真实 cap 余量)
        if includes_24h_stats and out:
            ids_tuple = tuple(a["id"] for a in out)
            placeholders = ",".join(["?"] * len(ids_tuple))
            counts = {}
            for r in conn.execute(
                f"SELECT line_account_id, COUNT(*) AS n FROM line_dispatch_log"
                f" WHERE line_account_id IN ({placeholders})"
                f" AND status != 'skipped'"
                f" AND created_at >= datetime('now', '-24 hours')"
                f" GROUP BY line_account_id",
                ids_tuple,
            ).fetchall():
                counts[r["line_account_id"]] = int(r["n"])
            for a in out:
                a["used_24h"] = counts.get(a["id"], 0)
        return out


def get_by_id(account_id: int) -> Optional[Dict[str, Any]]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM line_accounts WHERE id=?", (account_id,),
        ).fetchone()
    return _row_to_dict(row) if row else None


# ─── 改 ─────────────────────────────────────────────────────────────────

def update(account_id: int, **fields) -> bool:
    allowed = {"owner_device_id", "persona_key", "region", "status",
                "daily_cap", "notes", "line_id"}
    sets = []
    args: List[Any] = []
    for k, v in fields.items():
        if k not in allowed:
            continue
        if k == "status" and v not in VALID_STATUSES:
            raise ValueError(f"status 非法: {v}")
        sets.append(f"{k} = ?")
        args.append(v)
    if not sets:
        return False
    sets.append("updated_at = datetime('now')")
    args.append(account_id)
    try:
        with _connect() as conn:
            cur = conn.execute(
                f"UPDATE line_accounts SET {', '.join(sets)} WHERE id=?",
                args,
            )
            return cur.rowcount > 0
    except Exception as e:
        msg = str(e).lower()
        if "unique" in msg or "constraint" in msg:
            raise ValueError("line_id 已存在") from e
        raise


def delete(account_id: int) -> bool:
    with _connect() as conn:
        cur = conn.execute("DELETE FROM line_accounts WHERE id=?",
                            (account_id,))
        return cur.rowcount > 0


# ─── 轮循分配 ───────────────────────────────────────────────────────────

def _write_skipped_log(conn, *, reason: str, canonical_id: str = "",
                         peer_name: str = "", source_device_id: str = "",
                         source_event_id: str = "",
                         filters: Optional[Dict[str, Any]] = None) -> None:
    """Phase 12.1: allocate 失败时写一条 skipped log 供 UI 诊断.

    line_account_id=0 表示"无账号被选", line_id='' 清晰标记这是 allocation-miss,
    不是发送失败. note 里带 reason + filter 组合帮运营判断该加账号还是调 cap.
    """
    try:
        note_dict = {"reason": reason}
        if filters:
            note_dict["filters"] = {k: v for k, v in filters.items() if v}
        import json as _json
        note = _json.dumps(note_dict, ensure_ascii=False)[:500]
    except Exception:
        note = reason
    try:
        conn.execute(
            "INSERT INTO line_dispatch_log (line_account_id, line_id,"
            " canonical_id, peer_name, source_device_id, source_event_id,"
            " status, note) VALUES (0, '', ?, ?, ?, ?, 'skipped', ?)",
            (canonical_id, peer_name, source_device_id, source_event_id, note),
        )
    except Exception as e:
        logger.debug("[line_pool] write skipped log 失败: %s", e)


def _count_recent_usage(conn, line_account_id: int, hours: int = 24) -> int:
    """近 N 小时 line_dispatch_log 记录数 (status != skipped)."""
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM line_dispatch_log"
        " WHERE line_account_id = ? AND status != 'skipped'"
        " AND created_at >= datetime('now', ?)",
        (line_account_id, f"-{int(hours)} hours"),
    ).fetchone()
    return int(row["n"] if row else 0)


def allocate(*,
             region: Optional[str] = None,
             persona_key: Optional[str] = None,
             owner_device_id: Optional[str] = None,
             canonical_id: str = "",
             peer_name: str = "",
             source_device_id: str = "",
             source_event_id: str = "") -> Optional[Dict[str, Any]]:
    """按轮循 (last_used_at ASC) + daily_cap 分配一个 LINE 账号.

    过滤顺序:
      1. status = 'active'
      2. region / persona_key / owner_device_id 匹配 (参数给了才过滤)
      3. 最近 24h 使用次数 < daily_cap
      4. 按 last_used_at ASC (空串视为最老) 取第 1 个

    分配成功:
      - UPDATE line_accounts.last_used_at = now, times_used += 1
      - INSERT line_dispatch_log(status='planned') 审计
      - 返回 dict {id, line_id, ...}
    分配失败:
      - 没匹配账号 / 全部超 cap → 返 None (调用方决定 retry / fallback)
    """
    where = ["status = 'active'"]
    args: List[Any] = []
    if region:
        where.append("region = ?")
        args.append(region)
    if persona_key:
        where.append("persona_key = ?")
        args.append(persona_key)
    if owner_device_id:
        # owner 可以为空 (通用池) 或匹配
        where.append("(owner_device_id = ? OR owner_device_id = '')")
        args.append(owner_device_id)
    sql = ("SELECT * FROM line_accounts WHERE " + " AND ".join(where)
           + " ORDER BY (last_used_at = '') DESC, last_used_at ASC, id ASC")
    # Phase 11.1 CAS: UPDATE ... WHERE last_used_at=<prev> 保并发严格原子.
    # 最多 retry 3 次 (覆盖 "被另一进程抢先更新" 的罕见 race).
    MAX_ATTEMPTS = 3
    for attempt in range(MAX_ATTEMPTS):
        with _connect() as conn:
            rows = conn.execute(sql, args).fetchall()
            if not rows:
                # Phase 12.1: 写 skipped log 供 UI 诊断 "为什么没分配"
                _write_skipped_log(
                    conn, reason="no_match",
                    canonical_id=canonical_id, peer_name=peer_name,
                    source_device_id=source_device_id,
                    source_event_id=source_event_id,
                    filters={"region": region, "persona_key": persona_key,
                             "owner_device_id": owner_device_id})
                return None
            picked = None
            prev_last_used = None
            for row in rows:
                used = _count_recent_usage(conn, row["id"], hours=24)
                if used < int(row["daily_cap"] or 20):
                    picked = row
                    prev_last_used = row["last_used_at"] or ""
                    break
            if picked is None:
                logger.info("[line_pool.allocate] %d accounts matched 全超 cap",
                             len(rows))
                _write_skipped_log(
                    conn, reason="all_capped",
                    canonical_id=canonical_id, peer_name=peer_name,
                    source_device_id=source_device_id,
                    source_event_id=source_event_id,
                    filters={"region": region, "persona_key": persona_key,
                             "owner_device_id": owner_device_id,
                             "candidates": len(rows)})
                return None
            # CAS: 如果 last_used_at 被别人改了, UPDATE 影响 0 行, retry.
            new_last = _now_iso()
            cur = conn.execute(
                "UPDATE line_accounts SET last_used_at=?,"
                " times_used=times_used+1, updated_at=datetime('now')"
                " WHERE id=? AND COALESCE(last_used_at,'') = ?",
                (new_last, picked["id"], prev_last_used),
            )
            if cur.rowcount == 0:
                logger.debug("[line_pool.allocate] CAS 冲突 attempt=%d, retry",
                             attempt + 1)
                continue
            # CAS 成功 → 写 dispatch_log + 返
            conn.execute(
                "INSERT INTO line_dispatch_log (line_account_id, line_id,"
                " canonical_id, peer_name, source_device_id, source_event_id,"
                " status) VALUES (?,?,?,?,?,?,'planned')",
                (picked["id"], picked["line_id"], canonical_id, peer_name,
                 source_device_id, source_event_id),
            )
            fresh = conn.execute(
                "SELECT * FROM line_accounts WHERE id=?", (picked["id"],),
            ).fetchone()
            # N2: LINE 分配成功 → 推进 lifecycle 到 qualified
            if canonical_id:
                try:
                    from src.host.lead_mesh.canonical import advance_lifecycle
                    advance_lifecycle(canonical_id, "qualified")
                except Exception:
                    pass
            return _row_to_dict(fresh)
    # 3 次 CAS 都冲突 — 极罕见, 放弃本轮让上游 retry/fallback
    logger.warning("[line_pool.allocate] CAS 连续 %d 次冲突, 放弃", MAX_ATTEMPTS)
    return None


# Phase 12.2: 账号封号自动 cooldown 阈值 (发生 failed 后查最近 N 条)
_AUTO_COOLDOWN_WINDOW = 5      # 看最近 5 条非 skipped 结果
_AUTO_COOLDOWN_FAIL_RATIO = 0.8  # failed / total >= 0.8 触发
_AUTO_COOLDOWN_MIN_SAMPLES = 3  # 至少 3 条才判断, 防止前 2 条就封


def mark_dispatch_outcome(line_account_id: int, *,
                            status: str = "sent",
                            note: str = "") -> bool:
    """更新 dispatch_log 最近一条的 status (B 机/dispatcher 完成发送后回写).

    Phase 12.2 (2026-04-25): status='failed' 触发 auto cooldown 检查 —
    查该账号最近 ``_AUTO_COOLDOWN_WINDOW`` 条非 skipped 记录, 若 ≥
    ``_AUTO_COOLDOWN_MIN_SAMPLES`` 条且 failed 比例 ≥ ``_AUTO_COOLDOWN_FAIL_RATIO``,
    自动把 line_accounts.status 设为 'cooldown' + note='auto:fail_rate_high'.
    运营可在 UI 里手动 toggle 回 active (不做自动恢复避免反复封解).
    """
    if status not in {"sent", "failed", "skipped"}:
        raise ValueError(f"status 非法: {status}")
    with _connect() as conn:
        row = conn.execute(
            "SELECT id FROM line_dispatch_log WHERE line_account_id=?"
            " AND status='planned' ORDER BY id DESC LIMIT 1",
            (line_account_id,),
        ).fetchone()
        if not row:
            return False
        conn.execute(
            "UPDATE line_dispatch_log SET status=?, note=? WHERE id=?",
            (status, note, row["id"]),
        )
        # Phase 12.2 auto cooldown: 刚记了 failed 才值得检查
        if status == "failed":
            _maybe_auto_cooldown(conn, line_account_id)
        return True


def _maybe_auto_cooldown(conn, line_account_id: int) -> bool:
    """内部: 检查最近 N 条 outcome 决定是否把账号转 cooldown. 返 True 表示
    本次调用触发了转换 (状态从 active → cooldown). 已经 cooldown/disabled
    /banned 的不动."""
    try:
        # 只有 active 账号才考虑 cooldown (已停用的无意义)
        acc_row = conn.execute(
            "SELECT status FROM line_accounts WHERE id=?",
            (line_account_id,),
        ).fetchone()
        if not acc_row or acc_row["status"] != "active":
            return False

        recent = conn.execute(
            "SELECT status FROM line_dispatch_log WHERE line_account_id=?"
            " AND status != 'skipped'"
            " ORDER BY id DESC LIMIT ?",
            (line_account_id, _AUTO_COOLDOWN_WINDOW),
        ).fetchall()
        total = len(recent)
        if total < _AUTO_COOLDOWN_MIN_SAMPLES:
            return False
        failed = sum(1 for r in recent if r["status"] == "failed")
        ratio = failed / total if total else 0.0
        if ratio < _AUTO_COOLDOWN_FAIL_RATIO:
            return False
        # 触发 cooldown
        note = (f"auto:fail_rate_high {failed}/{total} "
                f"(>= {_AUTO_COOLDOWN_FAIL_RATIO:.0%})")
        conn.execute(
            "UPDATE line_accounts SET status='cooldown', notes = "
            " CASE WHEN COALESCE(notes,'')='' THEN ? "
            " ELSE notes || char(10) || ? END, "
            " updated_at=datetime('now') WHERE id=?",
            (note, note, line_account_id),
        )
        logger.warning("[line_pool.auto_cooldown] account %d → cooldown (%s)",
                         line_account_id, note)
        return True
    except Exception as e:
        logger.debug("[line_pool.auto_cooldown] 异常(忽略): %s", e)
        return False


def seed_from_config(config_path: Optional[str] = None,
                      *, only_if_empty: bool = True) -> Dict[str, Any]:
    """启动时从 YAML 注入默认 LINE 账号 (Phase 11).

    ``only_if_empty=True`` (默认): 池子非空 → 不做任何事 (避免覆盖运营在 UI 改过
    的状态). 空池才 seed.

    ``only_if_empty=False``: 强制调 add_many (重复 line_id 会被 add_many 计入
    duplicate, 不报错). 运维脚本用.

    返: {skipped: bool, total, inserted, duplicate, invalid, errors}
    """
    import os
    import pathlib
    import yaml

    # 测试/CI 可以设 OPENCLAW_LINE_POOL_SEED_SKIP=1 跳过
    if os.environ.get("OPENCLAW_LINE_POOL_SEED_SKIP"):
        return {"skipped": True, "reason": "env_skip"}

    if config_path is None:
        # 项目根目录/config/line_pool_seed.yaml
        here = pathlib.Path(__file__).resolve().parent.parent.parent
        config_path = str(here / "config" / "line_pool_seed.yaml")

    path = pathlib.Path(config_path)
    if not path.exists():
        return {"skipped": True, "reason": "seed_file_not_found"}

    if only_if_empty:
        with _connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM line_accounts"
            ).fetchone()
            if int(row["n"] if row else 0) > 0:
                return {"skipped": True, "reason": "pool_not_empty"}

    try:
        with path.open(encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except Exception as e:
        return {"skipped": True, "reason": f"yaml_parse_failed: {e}"}

    accounts = data.get("accounts") or []
    if not isinstance(accounts, list):
        return {"skipped": True, "reason": "accounts_not_list"}

    result = add_many(accounts)
    result["skipped"] = False
    logger.info("[line_pool.seed] from %s → %d inserted, %d duplicate, "
                 "%d invalid", path, result.get("inserted", 0),
                 result.get("duplicate", 0), result.get("invalid", 0))
    return result


def _infer_region_from_persona(persona_key: str) -> str:
    """persona_key 前缀 → region 推断 (jp_*→jp, it_*→it, fr_*→fr ...)."""
    if not persona_key:
        return ""
    pk = persona_key.lower()
    for prefix in ("jp", "it", "fr", "de", "es", "us", "kr", "cn", "tw"):
        if pk.startswith(f"{prefix}_"):
            return prefix
    return ""


def _get_lead_region(canonical_id: str) -> str:
    """Phase 19.x.3.4: 三级 region 解析 (返 "" 表示未知).

    顺序:
      1) leads_canonical.metadata.region (直接字段)
      2) metadata.l2_persona_key 前缀推断 (jp_/it_/fr_ ...)
      3) line_dispatch_log JOIN line_accounts.region (该 lead 最近一次分发的账号 region)

    任一级返非空字符串就停, 否则继续下一级. 全 miss 返 "".
    """
    if not canonical_id:
        return ""
    import json as _json
    # Level 1+2: leads_canonical metadata
    try:
        from src.host.lead_mesh.canonical import _connect as _lm_connect
        with _lm_connect() as conn:
            cur = conn.execute(
                "SELECT metadata_json FROM leads_canonical"
                " WHERE canonical_id=? LIMIT 1",
                (canonical_id,))
            row = cur.fetchone()
            if row:
                try:
                    meta = _json.loads(row["metadata_json"] or "{}")
                except Exception:
                    meta = {}
                # L1: 直接字段
                rg = (meta.get("region") or "").strip().lower()
                if rg:
                    return rg
                # L2: persona_key 前缀
                pk = meta.get("l2_persona_key", "") or ""
                inferred = _infer_region_from_persona(pk)
                if inferred:
                    return inferred
    except Exception as e:
        logger.debug("[get_lead_region] L1/L2 失败 cid=%s: %s", canonical_id, e)
    # Level 3: dispatch_log JOIN line_accounts (最近 1 条)
    try:
        with _connect() as conn:
            cur = conn.execute(
                "SELECT la.region FROM line_dispatch_log dl"
                " JOIN line_accounts la ON dl.line_account_id=la.id"
                " WHERE dl.canonical_id=?"
                " ORDER BY dl.created_at DESC LIMIT 1",
                (canonical_id,))
            row = cur.fetchone()
            if row:
                rg = (row["region"] or "").strip().lower()
                if rg:
                    return rg
    except Exception as e:
        logger.debug("[get_lead_region] L3 失败 cid=%s: %s", canonical_id, e)
    return ""


def referral_funnel(*, hours_window: int = 168,
                      region: Optional[str] = None,
                      persona_key: Optional[str] = None) -> Dict[str, Any]:
    """Phase 13 / 19.3: referral 漏斗聚合 4 层 + region/persona 真过滤.

    planned    = line_dispatch_planned contact_events 独立 peer 数
    sent       = wa_referral_sent contact_events 独立 peer 数
    replied    = wa_referral_replied contact_events 独立 peer 数

    Phase 19.3: region/persona_key 通过 leads_canonical metadata JOIN 实现.
      peer_name → resolve_identity (硬匹配) → canonical → metadata.l2_persona_key
      / region (从 line_account 缓存反查比直 metadata 简单).

    简化策略: 用 ``meta_json LIKE '%"l2_persona_key": "<key>"%'`` 字符串匹配,
    避免复杂 JOIN. 性能 OK (canonical 表小, leads_canonical 通常 < 几千).
    """
    from src.host.fb_store import (list_recent_contact_events_by_types,
                                    CONTACT_EVT_LINE_DISPATCH_PLANNED,
                                    CONTACT_EVT_WA_REFERRAL_SENT,
                                    CONTACT_EVT_REFERRAL_STALE)
    CONTACT_EVT_WA_REFERRAL_REPLIED = "wa_referral_replied"

    planned_rows = list_recent_contact_events_by_types(
        [CONTACT_EVT_LINE_DISPATCH_PLANNED],
        hours=hours_window, limit=10000)
    sent_rows = list_recent_contact_events_by_types(
        [CONTACT_EVT_WA_REFERRAL_SENT],
        hours=hours_window, limit=10000)
    replied_rows = list_recent_contact_events_by_types(
        [CONTACT_EVT_WA_REFERRAL_REPLIED],
        hours=hours_window, limit=10000)
    # Phase 20.2 (2026-04-25): SLA 死信回收 — 标过 stale 的 peer 数
    stale_rows = list_recent_contact_events_by_types(
        [CONTACT_EVT_REFERRAL_STALE],
        hours=hours_window, limit=10000)

    # Phase 19.3: region/persona 过滤 — 反查 canonical 看 metadata
    eligible_peers: Optional[set] = None
    if region or persona_key:
        try:
            from src.host.lead_mesh.canonical import _connect as _lm_connect
            import json as _json
            # 收集 candidate peer names
            cand = set()
            for evs in (planned_rows, sent_rows, replied_rows):
                for r in evs:
                    pn = r.get("peer_name")
                    if pn:
                        cand.add(pn)
            if cand:
                # 预查 leads_canonical 这些 peer (主键索引快)
                with _lm_connect() as conn:
                    placeholders = ",".join(["?"] * len(cand))
                    # account_id 格式 fb:peer_name (与 dispatcher resolve_identity 一致)
                    fb_keys = [f"fb:{n}" for n in cand]
                    q = ("SELECT li.account_id, li.canonical_id, lc.metadata_json"
                         " FROM lead_identities li"
                         " JOIN leads_canonical lc"
                         " ON li.canonical_id = lc.canonical_id"
                         f" WHERE li.platform='facebook' AND li.account_id IN ({placeholders})")
                    cur = conn.execute(q, fb_keys)
                    eligible_peers = set()
                    for row in cur.fetchall():
                        try:
                            meta = _json.loads(row["metadata_json"] or "{}")
                        except Exception:
                            meta = {}
                        # persona 检查
                        if persona_key:
                            if meta.get("l2_persona_key") != persona_key:
                                continue
                        # Phase 19.x.3.4: region 用 3-level 解析
                        # L1 metadata.region → L2 persona prefix → L3 dispatch_log JOIN
                        if region:
                            rg_meta = (meta.get("region") or "").strip().lower()
                            pk = meta.get("l2_persona_key", "") or ""
                            rg_persona = _infer_region_from_persona(pk)
                            rg_eff = rg_meta or rg_persona
                            # L3 fallback: 上面都空才查 dispatch_log
                            if not rg_eff:
                                rg_eff = _get_lead_region(row["canonical_id"])
                            if rg_eff != region:
                                continue
                        # 反推 peer_name (account_id = fb:NAME)
                        aid = row["account_id"] or ""
                        if aid.startswith("fb:"):
                            eligible_peers.add(aid[3:])
        except Exception:
            eligible_peers = None  # 异常时不过滤, 退化为 overall

    def _filter(rows):
        if eligible_peers is None:
            return rows
        return [r for r in rows if r.get("peer_name") in eligible_peers]

    planned_rows_f = _filter(planned_rows)
    sent_rows_f = _filter(sent_rows)
    replied_rows_f = _filter(replied_rows)
    stale_rows_f = _filter(stale_rows)

    planned_peers = {r.get("peer_name") for r in planned_rows_f
                      if r.get("peer_name")}
    sent_peers = {r.get("peer_name") for r in sent_rows_f
                    if r.get("peer_name")}
    replied_peers = {r.get("peer_name") for r in replied_rows_f
                       if r.get("peer_name")}
    stale_peers = {r.get("peer_name") for r in stale_rows_f
                     if r.get("peer_name")}

    n_planned = len(planned_peers)
    n_sent = len(sent_peers)
    n_replied = len(replied_peers)
    n_stale = len(stale_peers)
    send_rate = (n_sent / n_planned) if n_planned else 0.0
    conv_rate = (n_replied / n_sent) if n_sent else 0.0
    # Phase 20.2: stale_rate = stale / sent (sent 中失活比例)
    stale_rate = (n_stale / n_sent) if n_sent else 0.0
    return {
        "hours_window": hours_window,
        "planned": n_planned,
        "sent": n_sent,
        "replied": n_replied,
        "stale": n_stale,
        "send_rate": round(send_rate, 4),
        "conversion_rate": round(conv_rate, 4),
        "stale_rate": round(stale_rate, 4),
        "raw_events": {
            "planned_events": len(planned_rows),
            "sent_events": len(sent_rows),
            "replied_events": len(replied_rows),
            "stale_events": len(stale_rows),
            "filtered_planned": len(planned_rows_f),
            "filtered_sent": len(sent_rows_f),
            "filtered_replied": len(replied_rows_f),
            "filtered_stale": len(stale_rows_f),
        },
        "region": region or "",
        "persona_key": persona_key or "",
        "filter_applied": eligible_peers is not None,
    }


def account_ranking(*, hours_window: int = 168,
                      limit: int = 50) -> List[Dict[str, Any]]:
    """Phase 13: per-LINE-account 引流表现排名.

    按 success_rate = sent / (sent+failed) desc, 同 rate 按 total_dispatched desc.
    排除 line_account_id=0 的 skipped-log (allocate 失败占位).
    """
    with _connect() as conn:
        accounts = {
            r["id"]: _row_to_dict(r)
            for r in conn.execute("SELECT * FROM line_accounts").fetchall()
        }
        rows = conn.execute(
            "SELECT line_account_id, status, COUNT(*) AS n"
            " FROM line_dispatch_log"
            " WHERE created_at >= datetime('now', ?)"
            " GROUP BY line_account_id, status",
            (f"-{int(hours_window)} hours",),
        ).fetchall()

    per_acc: Dict[int, Dict[str, int]] = {}
    for r in rows:
        aid = r["line_account_id"]
        if aid not in per_acc:
            per_acc[aid] = {"sent": 0, "failed": 0, "planned": 0,
                             "skipped": 0}
        per_acc[aid][r["status"]] = int(r["n"])

    out: List[Dict[str, Any]] = []
    for aid, stats in per_acc.items():
        if aid == 0:
            continue
        acc = accounts.get(aid)
        if not acc:
            continue
        sent = stats.get("sent", 0)
        failed = stats.get("failed", 0)
        planned = stats.get("planned", 0)
        total = sent + failed
        success_rate = (sent / total) if total else 0.0
        out.append({
            "line_account_id": aid,
            "line_id": acc["line_id"],
            "region": acc["region"],
            "persona_key": acc["persona_key"],
            "status": acc["status"],
            "planned": planned,
            "sent": sent,
            "failed": failed,
            "total_dispatched": total,
            "success_rate": round(success_rate, 4),
            "last_used_at": acc["last_used_at"],
        })
    out.sort(key=lambda x: (-x["success_rate"], -x["total_dispatched"]))
    return out[:max(1, min(int(limit or 50), 500))]


def recent_dispatch_log(limit: int = 100) -> List[Dict[str, Any]]:
    limit = max(1, min(int(limit or 100), 1000))
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM line_dispatch_log ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            {
                "id": r["id"],
                "line_account_id": r["line_account_id"],
                "line_id": r["line_id"],
                "canonical_id": r["canonical_id"] or "",
                "peer_name": r["peer_name"] or "",
                "source_device_id": r["source_device_id"] or "",
                "source_event_id": r["source_event_id"] or "",
                "status": r["status"] or "",
                "note": r["note"] or "",
                "created_at": r["created_at"] or "",
            }
            for r in rows
        ]
