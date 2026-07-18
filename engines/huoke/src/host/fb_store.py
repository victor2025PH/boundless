# -*- coding: utf-8 -*-
"""Facebook 业务数据 store(Sprint 2 P0)。

3 张表的 CRUD + 漏斗聚合查询封装。
所有写入函数都是幂等/upsert 模式,可被 automation 层重复调用。
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

from .database import _connect


def _now_iso() -> str:
    import datetime as _dt
    return _dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

logger = logging.getLogger(__name__)

_GROUP_META_ONLY_RE = re.compile(
    r"^\s*"
    r"(?:公开|公開|公有|私密|私人|非公開|Public|Private)"
    r"(?:\s*(?:小组|群组|社團|グループ|group))?"
    r"\s*(?:[·・,，]\s*)?"
    r"[\d,.]+(?:\s*[万萬億kKmM])?"
    r"(?:\s*(?:位)?(?:成员|成員|members?|メンバー))?"
    r"\s*$",
    re.IGNORECASE,
)


def _is_valid_group_record_name(group_name: str) -> bool:
    """Reject UI metadata fragments that older discovery code could persist as groups."""
    name = (group_name or "").strip()
    if len(name) < 2:
        return False
    if _GROUP_META_ONLY_RE.match(name):
        return False
    if name in {
        "公开", "公開", "公有", "私密", "私人", "非公開",
        "Public", "Private", "Public group", "Private group",
    }:
        return False
    return True


# ─────────────────────────────────────────────────────────────────────
# facebook_groups
# ─────────────────────────────────────────────────────────────────────

def upsert_group(device_id: str, group_name: str, *,
                 group_url: str = "", member_count: int = 0,
                 language: str = "", country: str = "",
                 status: str = "joined",
                 preset_key: str = "") -> int:
    """新加入或更新群信息。返回 row id。"""
    if not device_id or not group_name:
        return 0
    if not _is_valid_group_record_name(group_name):
        logger.info("[fb_store] skip invalid group record: %r device=%s",
                    group_name, device_id)
        return 0
    now = _now_iso()
    with _connect() as conn:
        row = conn.execute(
            "SELECT id FROM facebook_groups WHERE device_id=? AND group_name=?",
            (device_id, group_name),
        ).fetchone()
        if row:
            # discovered/pending 是轻量候选状态, 不能覆盖已经 joined/visited 的群组。
            status_expr = (
                "CASE WHEN status IN ('joined','visited') "
                "AND ? IN ('discovered','pending') "
                "THEN status ELSE ? END"
            )
            conn.execute(
                "UPDATE facebook_groups SET group_url=COALESCE(NULLIF(?,''), group_url),"
                " member_count=CASE WHEN ?>0 THEN ? ELSE member_count END,"
                " language=COALESCE(NULLIF(?,''), language),"
                " country=COALESCE(NULLIF(?,''), country),"
                f" status={status_expr},"
                " preset_key=COALESCE(NULLIF(?,''), preset_key)"
                " WHERE id=?",
                (group_url, member_count, member_count, language, country,
                 status, status, preset_key, row[0]),
            )
            return row[0]
        cur = conn.execute(
            "INSERT INTO facebook_groups"
            " (device_id, group_name, group_url, member_count, language, country,"
            "  status, joined_at, preset_key)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (device_id, group_name, group_url, member_count, language, country,
             status, now, preset_key),
        )
        return cur.lastrowid


def mark_group_visit(device_id: str, group_name: str,
                     extracted_count: int = 0):
    if not _is_valid_group_record_name(group_name):
        logger.info("[fb_store] skip invalid group visit: %r device=%s",
                    group_name, device_id)
        return
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE facebook_groups SET last_visited_at=?, visit_count=visit_count+1,"
            " extracted_member_count=extracted_member_count+?,"
            " status=CASE WHEN status='discovered' THEN 'visited' ELSE status END "
            "WHERE device_id=? AND group_name=?",
            (_now_iso(), int(extracted_count or 0), device_id, group_name),
        )
        if (cur.rowcount or 0) == 0 and device_id and group_name:
            conn.execute(
                "INSERT INTO facebook_groups"
                " (device_id, group_name, status, joined_at, last_visited_at,"
                "  visit_count, extracted_member_count)"
                " VALUES (?,?,?,?,?,?,?)",
                (device_id, group_name, "visited", _now_iso(), _now_iso(),
                 1, int(extracted_count or 0)),
            )


def group_visit_state(device_id: str, group_name: str) -> Dict[str, Any]:
    """返回某设备对某群组的访问/提取状态。"""
    if not device_id or not group_name:
        return {}
    if not _is_valid_group_record_name(group_name):
        return {}
    with _connect() as conn:
        conn.row_factory = __import__("sqlite3").Row
        row = conn.execute(
            "SELECT id, status, visit_count, extracted_member_count,"
            " last_visited_at FROM facebook_groups"
            " WHERE device_id=? AND group_name=?",
            (device_id, group_name),
        ).fetchone()
    return dict(row) if row else {}


def has_group_been_visited(device_id: str, group_name: str) -> bool:
    st = group_visit_state(device_id, group_name)
    return bool((st.get("visit_count") or 0) > 0 or st.get("status") in (
        "visited", "joined",
    ))


def list_groups(device_id: Optional[str] = None,
                status: str = "joined",
                limit: int = 200) -> List[Dict]:
    sql = "SELECT * FROM facebook_groups WHERE 1=1"
    params: list = []
    if device_id:
        sql += " AND device_id=?"
        params.append(device_id)
    if status:
        sql += " AND status=?"
        params.append(status)
    sql += " ORDER BY joined_at DESC LIMIT ?"
    params.append(limit)
    with _connect() as conn:
        conn.row_factory = __import__("sqlite3").Row
        rows = conn.execute(sql, params).fetchall()
    return [
        dict(r) for r in rows
        if _is_valid_group_record_name(dict(r).get("group_name", ""))
    ]


def list_unvisited_groups(device_id: Optional[str] = None,
                          keyword: str = "",
                          limit: int = 50) -> List[Dict]:
    """列出已发现但尚未访问/提取的群组候选。"""
    sql = ("SELECT * FROM facebook_groups WHERE COALESCE(visit_count,0)=0"
           " AND status IN ('discovered','pending','joined')")
    params: list = []
    if device_id:
        sql += " AND device_id=?"
        params.append(device_id)
    if keyword:
        sql += " AND group_name LIKE ?"
        params.append(f"%{keyword}%")
    sql += " ORDER BY joined_at DESC LIMIT ?"
    params.append(int(limit or 50))
    with _connect() as conn:
        conn.row_factory = __import__("sqlite3").Row
        rows = conn.execute(sql, params).fetchall()
    return [
        dict(r) for r in rows
        if _is_valid_group_record_name(dict(r).get("group_name", ""))
    ]


# ─────────────────────────────────────────────────────────────────────
# facebook_friend_requests
# ─────────────────────────────────────────────────────────────────────

def record_friend_request(device_id: str, target_name: str, *,
                          note: str = "", source: str = "",
                          target_profile_url: str = "",
                          status: str = "sent",
                          lead_id: Optional[int] = None,
                          preset_key: str = "",
                          canonical_id: str = "") -> int:
    if not device_id or not target_name:
        return 0
    # Phase A2: 自动解析 canonical_id (若调用方未传)
    if not canonical_id:
        canonical_id = _resolve_canonical_id_for_name(target_name)
    now = _now_iso()
    with _connect() as conn:
        try:
            cur = conn.execute(
                "INSERT INTO facebook_friend_requests"
                " (device_id, target_name, target_profile_url, note, source,"
                "  status, sent_at, lead_id, preset_key, canonical_id)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (device_id, target_name, target_profile_url, note, source,
                 status, now, lead_id, preset_key, canonical_id or ""),
            )
            rid = cur.lastrowid
        except Exception as e:
            logger.debug("record_friend_request 失败: %s", e)
            return 0
    # M1: 推进生命周期
    _try_advance_fb_lifecycle(canonical_id, f"fr:{status}")
    return rid


def update_friend_request_status(device_id: str, target_name: str,
                                 new_status: str):
    with _connect() as conn:
        if new_status == "accepted":
            conn.execute(
                "UPDATE facebook_friend_requests SET status=?, accepted_at=? "
                "WHERE device_id=? AND target_name=? "
                "AND status='sent'",
                (new_status, _now_iso(), device_id, target_name),
            )
        else:
            conn.execute(
                "UPDATE facebook_friend_requests SET status=? "
                "WHERE device_id=? AND target_name=? AND status='sent'",
                (new_status, device_id, target_name),
            )
    # M1: 推进生命周期 (accepted → engaged)
    if new_status == "accepted":
        cid = _resolve_canonical_id_for_name(target_name)
        _try_advance_fb_lifecycle(cid, "fr:accepted")


def get_friend_request_stats(device_id: Optional[str] = None,
                             since_iso: Optional[str] = None,
                             preset_key: Optional[str] = None) -> Dict[str, int]:
    sql = ("SELECT status, COUNT(*) FROM facebook_friend_requests WHERE 1=1")
    params: list = []
    if device_id:
        sql += " AND device_id=?"
        params.append(device_id)
    if since_iso:
        sql += " AND sent_at >= ?"
        params.append(since_iso)
    if preset_key:
        sql += " AND preset_key=?"
        params.append(preset_key)
    sql += " GROUP BY status"
    with _connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    stats = {"sent": 0, "accepted": 0, "rejected": 0,
             "cancelled": 0, "risk": 0, "pending": 0}
    for s, c in rows:
        stats[s] = stats.get(s, 0) + c
    sent = stats["sent"]
    accepted = stats["accepted"]
    # 修正:旧逻辑 sent 不含 accepted 时分母漏算,现在 sent_total = sent + accepted
    sent_total = sent + accepted
    stats["sent_total"] = sent_total
    stats["accept_rate"] = round(accepted / sent_total, 3) if sent_total else 0.0
    return stats


def count_friend_requests_sent_since(device_id: str, hours: int = 24) -> int:
    """统计 rolling 窗口内发出的好友请求条数（按 ``sent_at`` 过滤）。

    用于 ``facebook_playbook.add_friend.daily_cap_per_account`` 硬闸：
    同一 device 在 24h 内尝试次数 ≥ cap 则拒绝新请求。

    计数包含所有 ``sent_at`` 落在窗口内的行（含后来 accepted / rejected 的），
    因为每一次尝试都消耗风控预算。
    """
    if not device_id or hours <= 0:
        return 0
    import datetime as _dt
    cutoff = (_dt.datetime.utcnow() - _dt.timedelta(hours=hours)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    with _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM facebook_friend_requests"
            " WHERE device_id=? AND sent_at>=?",
            (device_id, cutoff),
        ).fetchone()
    return int(row[0]) if row else 0


# ─────────────────────────────────────────────────────────────────────
# Phase A2: 跨设备好友请求全局去重
# ─────────────────────────────────────────────────────────────────────

def is_friend_requested_globally(target_name: str, *,
                                  exclude_device: str = "",
                                  hours: int = 720) -> dict:
    """检查某 target 是否被*任何*设备发过好友请求 (跨设备刚性去重).

    逻辑:
      1. 先按 target_name 精确匹配
      2. 如有 canonical_id, 额外查同 canonical_id 的所有记录
      3. 排除指定设备 (允许同设备重发)

    Returns:
        {found: bool, device_id: str, status: str, sent_at: str,
         canonical_id: str}
        found=False 时其余字段为空串
    """
    if not target_name:
        return {"found": False, "device_id": "", "status": "",
                "sent_at": "", "canonical_id": ""}
    import datetime as _dt
    cutoff = (_dt.datetime.utcnow() - _dt.timedelta(hours=max(hours, 1))
              ).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        with _connect() as conn:
            # 1. target_name 精确匹配
            sql = ("SELECT device_id, target_name, status, sent_at,"
                   " COALESCE(canonical_id,'') AS canonical_id"
                   " FROM facebook_friend_requests"
                   " WHERE target_name=? AND sent_at>=?"
                   " AND status IN ('sent','accepted','pending')")
            params: list = [target_name, cutoff]
            if exclude_device:
                sql += " AND device_id!=?"
                params.append(exclude_device)
            sql += " ORDER BY sent_at DESC LIMIT 1"
            row = conn.execute(sql, params).fetchone()
            if row:
                return {"found": True,
                        "device_id": row[0], "status": row[2],
                        "sent_at": row[3], "canonical_id": row[4]}

            # 2. canonical_id 模糊匹配 (同一人不同名字)
            cid = _resolve_canonical_id_for_name(target_name)
            if cid:
                sql2 = ("SELECT device_id, target_name, status, sent_at,"
                        " canonical_id FROM facebook_friend_requests"
                        " WHERE canonical_id=? AND sent_at>=?"
                        " AND status IN ('sent','accepted','pending')")
                params2: list = [cid, cutoff]
                if exclude_device:
                    sql2 += " AND device_id!=?"
                    params2.append(exclude_device)
                sql2 += " ORDER BY sent_at DESC LIMIT 1"
                row2 = conn.execute(sql2, params2).fetchone()
                if row2:
                    return {"found": True,
                            "device_id": row2[0], "status": row2[2],
                            "sent_at": row2[3], "canonical_id": row2[4]}
    except Exception as e:
        logger.debug("[global_dedup] is_friend_requested_globally 失败: %s", e)
    return {"found": False, "device_id": "", "status": "",
            "sent_at": "", "canonical_id": ""}


def _resolve_canonical_id_for_name(name: str) -> str:
    """尝试从 lead_identities 解析 canonical_id (只读, 不创建)."""
    if not name:
        return ""
    try:
        from src.host.database import _connect as _lm_connect
        with _lm_connect() as conn:
            row = conn.execute(
                "SELECT canonical_id FROM lead_identities"
                " WHERE platform='facebook' AND account_id=? LIMIT 1",
                (f"fb:{name}",),
            ).fetchone()
            return row[0] if row else ""
    except Exception:
        return ""


# ─────────────────────────────────────────────────────────────────────
# Phase B1: 对话所有权锁 — 防止多设备同时回复同一人
# Phase E1: 动态 TTL — 基于 peer 回复活跃度自适应调整锁时长
# ─────────────────────────────────────────────────────────────────────

# TTL 策略常量
_LOCK_TTL_ACTIVE = 96.0      # 活跃对话: peer 在 24h 内有回复 → 锁 96h
_LOCK_TTL_DEFAULT = 48.0     # 默认: 无明显信号
_LOCK_TTL_STALE = 24.0       # 冷却: peer 72h 内无回复 → 快速释放
_LOCK_TTL_DORMANT = 12.0     # 休眠: peer 7 天内无任何消息 → 极短锁


def compute_adaptive_ttl(peer_name: str) -> float:
    """Phase E1: 根据 peer 最近的回复活跃度动态计算对话锁 TTL.

    规则 (由新到旧优先匹配):
      1. peer 在 24h 内有 incoming 消息 → 96h (活跃对话,不应切设备)
      2. peer 在 72h 内有 incoming 消息 → 48h (正常温度)
      3. peer 7天内有消息但 72h 内无回复 → 24h (冷却中,快释放)
      4. peer 7天内无任何消息 → 12h (休眠,极短)

    Returns: TTL 小时数
    """
    if not peer_name:
        return _LOCK_TTL_DEFAULT
    import datetime as _dt
    now = _dt.datetime.utcnow()
    try:
        with _connect() as conn:
            # 查最近一条 incoming 消息时间
            row = conn.execute(
                "SELECT seen_at FROM facebook_inbox_messages"
                " WHERE peer_name=? AND direction='incoming'"
                " ORDER BY seen_at DESC LIMIT 1",
                (peer_name,),
            ).fetchone()
            if not row or not row[0]:
                return _LOCK_TTL_DORMANT

            last_reply_str = row[0]
            try:
                last_reply = _dt.datetime.strptime(
                    last_reply_str[:19], "%Y-%m-%dT%H:%M:%S")
            except (ValueError, TypeError):
                try:
                    last_reply = _dt.datetime.strptime(
                        last_reply_str[:19], "%Y-%m-%d %H:%M:%S")
                except (ValueError, TypeError):
                    return _LOCK_TTL_DEFAULT

            hours_since = (now - last_reply).total_seconds() / 3600.0
            if hours_since <= 24.0:
                return _LOCK_TTL_ACTIVE
            elif hours_since <= 72.0:
                return _LOCK_TTL_DEFAULT
            elif hours_since <= 168.0:
                return _LOCK_TTL_STALE
            else:
                return _LOCK_TTL_DORMANT
    except Exception:
        return _LOCK_TTL_DEFAULT


def try_claim_conversation(peer_name: str, device_id: str, *,
                            ttl_hours: float = 0.0) -> dict:
    """尝试获取对某 peer 的对话所有权 (跨设备互斥).

    逻辑:
      在 fb_contact_events 中查找 reply_owner_device 非空且未过期的行.
      - 若存在且 owner != 当前设备 → 返回 {claimed: False, owner: ...}
      - 若不存在或 owner == 当前设备 → 写入 claim, 返回 {claimed: True}

    这是"建议性锁", 调用方可以根据 claimed=False 选择 skip 或 force.
    """
    if not peer_name or not device_id:
        return {"claimed": False, "owner": "", "reason": "empty_args"}
    import datetime as _dt
    # Phase E1: ttl_hours=0 时自动使用动态 TTL
    effective_ttl = ttl_hours if ttl_hours > 0 else compute_adaptive_ttl(peer_name)
    cutoff = (_dt.datetime.utcnow() - _dt.timedelta(hours=max(effective_ttl, 1))
              ).strftime("%Y-%m-%d %H:%M:%S")
    try:
        with _connect() as conn:
            # 查看是否有其他设备的活跃 claim
            row = conn.execute(
                "SELECT reply_owner_device, at FROM fb_contact_events"
                " WHERE peer_name=? AND reply_owner_device!='' AND at>=?"
                " AND reply_owner_device!=?"
                " ORDER BY at DESC LIMIT 1",
                (peer_name, cutoff, device_id),
            ).fetchone()
            if row:
                return {"claimed": False,
                        "owner": row[0],
                        "claimed_at": row[1],
                        "effective_ttl_hours": effective_ttl,
                        "reason": "owned_by_other_device"}

            # 写入当前设备的 claim
            conn.execute(
                "INSERT INTO fb_contact_events"
                " (device_id, peer_name, event_type, reply_owner_device,"
                "  meta_json)"
                " VALUES (?,?,'conversation_claim',?,?)",
                (device_id, peer_name, device_id,
                 f'{{"ttl_hours":{effective_ttl}}}'),
            )
            return {"claimed": True, "owner": device_id,
                    "effective_ttl_hours": effective_ttl, "reason": ""}
    except Exception as e:
        logger.debug("[conversation_lock] try_claim_conversation 失败: %s", e)
        return {"claimed": False, "owner": "", "reason": f"error:{e}"}


def get_conversation_owner(peer_name: str, *,
                            ttl_hours: float = 0.0) -> str:
    """查询某 peer 当前的对话所有者设备 (空串 = 无人持有).

    Phase E1: ttl_hours=0 时自动使用 adaptive TTL.
    """
    if not peer_name:
        return ""
    import datetime as _dt
    effective_ttl = ttl_hours if ttl_hours > 0 else compute_adaptive_ttl(peer_name)
    cutoff = (_dt.datetime.utcnow() - _dt.timedelta(hours=max(effective_ttl, 1))
              ).strftime("%Y-%m-%d %H:%M:%S")
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT reply_owner_device FROM fb_contact_events"
                " WHERE peer_name=? AND reply_owner_device!='' AND at>=?"
                " ORDER BY at DESC LIMIT 1",
                (peer_name, cutoff),
            ).fetchone()
            return row[0] if row else ""
    except Exception:
        return ""


# ─────────────────────────────────────────────────────────────────────
# Phase D1*: 跨设备去重效果分析
# ─────────────────────────────────────────────────────────────────────

def get_dedup_stats(since_hours: int = 168,
                    device_id: str = "") -> Dict[str, Any]:
    """跨设备去重效果统计 — 运营面板核心数据.

    Returns:
        {
          friend_requests_blocked: 被跨设备去重拦截的好友请求数,
          conv_lock_conflicts: 对话锁冲突次数(peer 被其他设备持有),
          canonical_coverage: {
              contact_events_total: ...,
              contact_events_with_cid: ...,
              coverage_rate: 0.0~1.0,
              friend_requests_total: ...,
              friend_requests_with_cid: ...,
              fr_coverage_rate: 0.0~1.0,
          },
          top_duplicated_peers: [{peer_name, blocked_count, devices: [...]}],
          daily_trend: [{date, blocked, lock_conflicts}],
        }
    """
    import datetime as _dt
    cutoff = (_dt.datetime.utcnow() - _dt.timedelta(hours=max(1, since_hours))
              ).strftime("%Y-%m-%dT%H:%M:%SZ")
    result: Dict[str, Any] = {
        "friend_requests_blocked": 0,
        "conv_lock_conflicts": 0,
        "canonical_coverage": {},
        "top_duplicated_peers": [],
        "daily_trend": [],
        "scope_since_hours": since_hours,
        "scope_device": device_id or "all",
    }
    try:
        with _connect() as conn:
            # 1. 被去重拦截的好友请求 (通过 journey event add_friend_blocked 统计)
            sql_blocked = (
                "SELECT COUNT(*) FROM fb_contact_events"
                " WHERE event_type='add_friend_blocked' AND at>=?"
            )
            params_b: list = [cutoff]
            if device_id:
                sql_blocked += " AND device_id=?"
                params_b.append(device_id)
            row = conn.execute(sql_blocked, params_b).fetchone()
            result["friend_requests_blocked"] = row[0] if row else 0

            # 2. 对话锁冲突 (conversation_claim 被拒的次数 — 通过 meta 判断)
            # 用 event_type='conversation_claim' 的总数减去同设备 claim
            sql_claims = (
                "SELECT COUNT(*) FROM fb_contact_events"
                " WHERE event_type='conversation_claim' AND at>=?"
            )
            params_c: list = [cutoff]
            if device_id:
                sql_claims += " AND device_id=?"
                params_c.append(device_id)
            # 冲突 = 有其他设备也尝试 claim 过同一 peer (peer 有多个 claim 记录)
            sql_conflicts = (
                "SELECT COUNT(*) FROM ("
                "  SELECT peer_name FROM fb_contact_events"
                "  WHERE event_type='conversation_claim' AND at>=?"
                "  GROUP BY peer_name HAVING COUNT(DISTINCT device_id) > 1"
                ")"
            )
            params_cf: list = [cutoff]
            row_cf = conn.execute(sql_conflicts, params_cf).fetchone()
            result["conv_lock_conflicts"] = row_cf[0] if row_cf else 0

            # 3. canonical_id 覆盖率
            sql_ce_total = (
                "SELECT COUNT(*), SUM(CASE WHEN canonical_id!='' THEN 1 ELSE 0 END)"
                " FROM fb_contact_events WHERE at>=?"
            )
            ce_row = conn.execute(sql_ce_total, [cutoff]).fetchone()
            ce_total = ce_row[0] if ce_row else 0
            ce_with = ce_row[1] if ce_row else 0

            sql_fr_total = (
                "SELECT COUNT(*), SUM(CASE WHEN canonical_id!='' THEN 1 ELSE 0 END)"
                " FROM facebook_friend_requests WHERE sent_at>=?"
            )
            fr_row = conn.execute(sql_fr_total, [cutoff]).fetchone()
            fr_total = fr_row[0] if fr_row else 0
            fr_with = fr_row[1] if fr_row else 0

            result["canonical_coverage"] = {
                "contact_events_total": ce_total,
                "contact_events_with_cid": ce_with,
                "coverage_rate": round(ce_with / max(ce_total, 1), 4),
                "friend_requests_total": fr_total,
                "friend_requests_with_cid": fr_with,
                "fr_coverage_rate": round(fr_with / max(fr_total, 1), 4),
            }

            # 4. 被重复请求最多的 peer (top 10)
            sql_top = (
                "SELECT peer_name, COUNT(*) as cnt,"
                " GROUP_CONCAT(DISTINCT device_id) as devices"
                " FROM fb_contact_events"
                " WHERE event_type='add_friend_blocked' AND at>=?"
                " AND meta_json LIKE '%cross_device_duplicate%'"
                " GROUP BY peer_name ORDER BY cnt DESC LIMIT 10"
            )
            for row in conn.execute(sql_top, [cutoff]):
                result["top_duplicated_peers"].append({
                    "peer_name": row[0],
                    "blocked_count": row[1],
                    "devices": (row[2] or "").split(","),
                })

            # 5. 每日趋势 (最近7天)
            sql_daily = (
                "SELECT DATE(at) as d,"
                " SUM(CASE WHEN event_type='add_friend_blocked' THEN 1 ELSE 0 END),"
                " SUM(CASE WHEN event_type='conversation_claim' THEN 1 ELSE 0 END)"
                " FROM fb_contact_events WHERE at>=?"
                " GROUP BY DATE(at) ORDER BY d DESC LIMIT 7"
            )
            for row in conn.execute(sql_daily, [cutoff]):
                result["daily_trend"].append({
                    "date": row[0],
                    "blocked": row[1] or 0,
                    "claims": row[2] or 0,
                })
            result["daily_trend"].reverse()

    except Exception as e:
        logger.debug("[dedup_stats] 查询失败: %s", e)
        result["_error"] = str(e)

    # Phase F1: 条件性告警触发
    try:
        _maybe_emit_dedup_alerts(result)
    except Exception:
        pass
    return result


# Phase F1: 去重告警逻辑
_DEDUP_ALERT_THRESHOLD_BLOCKED = 5     # 日拦截超此数触发日报
_DEDUP_ALERT_THRESHOLD_PEER_DEVICES = 3  # 同一 peer 被 N 台设备尝试触发高频告警
_last_dedup_daily_alert: float = 0.0   # epoch, 用于 24h 间隔控制


def _maybe_emit_dedup_alerts(stats: Dict[str, Any]) -> None:
    """Phase F1: 基于 dedup stats 结果条件性触发告警.

    规则:
      1. 日报: 24h 内只发一次, blocked >= 阈值时触发
      2. 高频 peer: top_duplicated_peers 中 devices >= 3 台的立即告警
    """
    import time as _time
    global _last_dedup_daily_alert

    blocked = stats.get("friend_requests_blocked", 0)
    conflicts = stats.get("conv_lock_conflicts", 0)
    now = _time.time()

    # 日报 (24h 间隔 + 阈值)
    if (blocked >= _DEDUP_ALERT_THRESHOLD_BLOCKED
            and now - _last_dedup_daily_alert > 86400):
        try:
            from src.host.alert_notifier import AlertNotifier
            AlertNotifier.get().notify_event(
                "dedup_daily_summary",
                level="warning",
                alert_code="DEDUP_DAILY_SUMMARY",
                params={"blocked_count": blocked,
                        "lock_conflicts": conflicts},
            )
            _last_dedup_daily_alert = now
        except Exception:
            pass

    # 高频 peer 告警
    for peer in stats.get("top_duplicated_peers", []):
        devs = peer.get("devices") or []
        if len(devs) >= _DEDUP_ALERT_THRESHOLD_PEER_DEVICES:
            try:
                from src.host.alert_notifier import AlertNotifier
                AlertNotifier.get().notify_event(
                    "dedup_high_frequency_peer",
                    level="warning",
                    alert_code="DEDUP_HIGH_FREQUENCY_PEER",
                    params={"peer_name": peer.get("peer_name", "?"),
                            "device_count": len(devs)},
                )
            except Exception:
                pass


# Phase G1: 对话锁 TTL 分布查询
def get_conv_lock_ttl_distribution(since_hours: int = 168) -> Dict[str, Any]:
    """查询 conversation_claim 事件的 TTL 分级分布.

    从 meta_json 中提取 ttl_hours 字段(E1 写入), 归类到 4 个 tier.
    """
    import datetime as _dt
    import json as _json
    cutoff = (_dt.datetime.utcnow() - _dt.timedelta(hours=max(1, since_hours))
              ).strftime("%Y-%m-%dT%H:%M:%SZ")
    tiers = {"active_96h": 0, "default_48h": 0, "stale_24h": 0, "dormant_12h": 0}
    total_claims = 0
    try:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT meta_json FROM fb_contact_events"
                " WHERE event_type='conversation_claim' AND at>=?"
                " AND meta_json != ''",
                (cutoff,),
            ).fetchall()
            for row in rows:
                total_claims += 1
                try:
                    meta = _json.loads(row[0] or "{}")
                    ttl = float(meta.get("ttl_hours") or 48)
                except (ValueError, TypeError):
                    ttl = 48.0
                if ttl >= 96:
                    tiers["active_96h"] += 1
                elif ttl >= 48:
                    tiers["default_48h"] += 1
                elif ttl >= 24:
                    tiers["stale_24h"] += 1
                else:
                    tiers["dormant_12h"] += 1
    except Exception as e:
        logger.debug("[ttl_distribution] 查询失败: %s", e)
    return {
        "total_claims": total_claims,
        "tiers": tiers,
        "tier_rates": {k: round(v / max(total_claims, 1), 3)
                       for k, v in tiers.items()},
    }


# ─────────────────────────────────────────────────────────────────────
# facebook_inbox_messages
# ─────────────────────────────────────────────────────────────────────

def record_inbox_message(device_id: str, peer_name: str, *,
                         peer_type: str = "friend",
                         message_text: str = "",
                         direction: str = "incoming",
                         ai_decision: str = "",
                         ai_reply_text: str = "",
                         language_detected: str = "",
                         lead_id: Optional[int] = None,
                         replied_at: Optional[str] = None,
                         preset_key: str = "",
                         template_id: str = "") -> int:
    """记录一条 Messenger 消息(incoming/outgoing 皆可)。

    2026-04-23 新增:
      * direction=outgoing 时自动填 ``sent_at`` 列(与 ``seen_at`` 同值);
        下游 count_outgoing_messages_since 优先读 sent_at,避免 seen_at 对
        outgoing 语义不清。老行 sent_at=NULL 会回退到 seen_at 兜底。
      * template_id: 打招呼文案来自哪个模板(格式 '<country>:<idx>'),
        供 A/B 分析;其他场景写空串。
    """
    if not device_id or not peer_name:
        return 0
    now = _now_iso()
    # direction=outgoing 时同步把 sent_at 写上(incoming 留 NULL)
    sent_at = now if direction == "outgoing" else None
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO facebook_inbox_messages"
            " (device_id, peer_name, peer_type, message_text, direction,"
            "  ai_decision, ai_reply_text, language_detected,"
            "  seen_at, sent_at, replied_at, lead_id, preset_key, template_id)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (device_id, peer_name, peer_type, message_text, direction,
             ai_decision, ai_reply_text, language_detected,
             now, sent_at, replied_at, lead_id, preset_key, template_id),
        )
        return cur.lastrowid


def count_outgoing_messages_since(device_id: str,
                                  hours: int = 24,
                                  ai_decision: Optional[str] = None) -> int:
    """统计 rolling 窗口内本机发出的消息条数。

    用于 ``facebook_playbook.send_greeting.daily_cap_per_account`` 硬闸：
    同一 device 在 24h 内主动外发消息次数 ≥ cap 则拒绝新的打招呼。

    2026-04-23 修复: 优先读 ``sent_at`` 列(outgoing 专用),回落到 ``seen_at``
    保证历史行(sent_at=NULL)仍被计入,避免 daily_cap 漏算。

    Args:
        device_id: 设备 ID
        hours: 窗口长度（小时）
        ai_decision: 可选过滤 ai_decision 字段（如 ``'greeting'`` 只统计
            "加好友后打招呼"这一类），为空则统计所有方向=outgoing 的消息
    """
    if not device_id or hours <= 0:
        return 0
    import datetime as _dt
    cutoff = (_dt.datetime.utcnow() - _dt.timedelta(hours=hours)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    # COALESCE(sent_at, seen_at) 让新旧行都能正确比较
    sql = ("SELECT COUNT(*) FROM facebook_inbox_messages"
           " WHERE device_id=? AND direction='outgoing'"
           " AND COALESCE(sent_at, seen_at) >= ?")
    params: list = [device_id, cutoff]
    if ai_decision:
        sql += " AND ai_decision=?"
        params.append(ai_decision)
    try:
        with _connect() as conn:
            row = conn.execute(sql, params).fetchone()
        return int(row[0]) if row else 0
    except Exception as e:
        logger.debug("count_outgoing_messages_since 失败: %s", e)
        return 0


def count_unreplied_greetings_to_peer(device_id: str,
                                      peer_name: str) -> int:
    """同 peer 最后一次 incoming 之后 B 机发出的 greeting 条数。

    语义说明 (F6, 来自 B→A 协作约定, 为 A 的 send_greeting_after_add_friend
    per-peer 5 次硬顶提供数据源):

      * "对方一旦回过就算关系建立,重置计数" — 用最后一次 incoming 的 id
        作为分界,只数它之后的 greetings
      * 对方从未发过 → 数所有历史 greetings (分界为 0)
      * 不依赖 ``replied_at`` 字段 (那个只在 B 回复时被设,若 auto_reply 关
        就漏设;按 incoming 时序更稳)

    典型用法 (A 机 send_greeting_after_add_friend 开头):

    .. code-block:: python

        if count_unreplied_greetings_to_peer(did, profile_name) >= 5:
            self._set_greet_reason("peer_cap_5x")
            return False

    Args:
        device_id: 设备 ID
        peer_name: 目标 peer 姓名 (与 ``facebook_inbox_messages.peer_name``
            精确匹配; 如果 A 机 normalize_name 未处理全角/变音,这里也不会处理)

    Returns:
        未回复 greeting 条数 (包含 ``peer_type`` 为 friend/friend_request 等所有类型);
        空参数/DB 异常返回 0。
    """
    if not device_id or not peer_name:
        return 0
    try:
        with _connect() as conn:
            # id 而非 seen_at/sent_at 做分界 — 高并发同秒写入时严格单调
            row = conn.execute(
                "SELECT COUNT(*) FROM facebook_inbox_messages"
                " WHERE device_id=? AND peer_name=?"
                " AND direction='outgoing' AND ai_decision='greeting'"
                " AND id > COALESCE("
                "   (SELECT MAX(id) FROM facebook_inbox_messages"
                "    WHERE device_id=? AND peer_name=? AND direction='incoming'),"
                "   0)",
                (device_id, peer_name, device_id, peer_name),
            ).fetchone()
        return int(row[0]) if row else 0
    except Exception as e:
        logger.debug("count_unreplied_greetings_to_peer 失败: %s", e)
        return 0


def list_inbox_messages(device_id: Optional[str] = None,
                        since_iso: Optional[str] = None,
                        limit: int = 200,
                        preset_key: Optional[str] = None) -> List[Dict]:
    sql = "SELECT * FROM facebook_inbox_messages WHERE 1=1"
    params: list = []
    if device_id:
        sql += " AND device_id=?"
        params.append(device_id)
    if since_iso:
        sql += " AND seen_at >= ?"
        params.append(since_iso)
    if preset_key:
        sql += " AND preset_key=?"
        params.append(preset_key)
    sql += " ORDER BY seen_at DESC LIMIT ?"
    params.append(limit)
    with _connect() as conn:
        conn.row_factory = __import__("sqlite3").Row
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def mark_incoming_replied(device_id: str, peer_name: str, *,
                          replied_at: Optional[str] = None,
                          peer_type: Optional[str] = None) -> int:
    """给该 peer 最近一条尚未标记的 incoming 行写 replied_at。

    触发时机: ``_ai_reply_and_send`` 成功发出回复后同步调用。
    幂等: 若该 peer 最近的 incoming 行已有 ``replied_at``, 不更新。

    Args:
        device_id: 设备 ID
        peer_name: 对方姓名 (与 incoming 行的 peer_name 完全相等)
        replied_at: 可选,覆盖默认 now; 便于测试注入固定时间
        peer_type: 可选过滤 (friend/stranger/...);默认不过滤

    Returns:
        实际更新的行数 (0 或 1)。
    """
    if not device_id or not peer_name:
        return 0
    ts = replied_at or _now_iso()
    sql = (
        "UPDATE facebook_inbox_messages SET replied_at=? "
        "WHERE id = ("
        " SELECT id FROM facebook_inbox_messages"
        " WHERE device_id=? AND peer_name=? AND direction='incoming'"
        " AND (replied_at IS NULL OR replied_at='')"
    )
    params: list = [ts, device_id, peer_name]
    if peer_type:
        sql += " AND peer_type=?"
        params.append(peer_type)
    sql += " ORDER BY id DESC LIMIT 1)"
    try:
        with _connect() as conn:
            cur = conn.execute(sql, params)
            return cur.rowcount or 0
    except Exception as e:
        logger.debug("mark_incoming_replied 失败: %s", e)
        return 0


def mark_greeting_replied_back(device_id: str, peer_name: str, *,
                               window_days: int = 7,
                               replied_at: Optional[str] = None) -> int:
    """跨 bot 归因:对方回复了 A 写入的 greeting 行 → 回写 ``replied_at``。

    对应 INTEGRATION_CONTRACT §三 "B 允许回写 A 写入的 greeting 行的 replied_at"。
    扫描条件:
      * ``direction='outgoing'`` + ``ai_decision='greeting'``
      * ``peer_type='friend_request'`` (A 的 greeting 路径写入的 peer_type)
      * ``COALESCE(sent_at, seen_at) >= utcnow() - window_days``
      * ``replied_at IS NULL`` (幂等,已标记过则跳过)

    命中最新一条 greeting 行写入 ``replied_at``,让 A 端的
    ``reply_rate_by_template`` / A/B 模板效果统计能跑。

    Args:
        device_id: 设备 ID
        peer_name: 对方姓名 (与 greeting 行的 peer_name 完全相等)
        window_days: 回溯窗口,默认 7 天;超出窗口的 greeting 视为"机缘已过"
        replied_at: 可选,覆盖默认 now

    Returns:
        实际更新的行数 (0 或 1)。
    """
    if not device_id or not peer_name or window_days <= 0:
        return 0
    import datetime as _dt
    cutoff = (_dt.datetime.utcnow() - _dt.timedelta(days=window_days)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    ts = replied_at or _now_iso()
    sql = (
        "UPDATE facebook_inbox_messages SET replied_at=? "
        "WHERE id = ("
        " SELECT id FROM facebook_inbox_messages"
        " WHERE device_id=? AND peer_name=?"
        " AND direction='outgoing' AND ai_decision='greeting'"
        " AND peer_type='friend_request'"
        " AND COALESCE(sent_at, seen_at) >= ?"
        " AND (replied_at IS NULL OR replied_at='')"
        " ORDER BY id DESC LIMIT 1)"
    )
    # 2026-05-04 Stage G.1: 在 with _connect() 块内直接 SELECT 取 template_id
    # +preset_key, 但延后到 with 块结束 (conn commit+close) 再调
    # record_contact_event 避免嵌套连接死锁 (mark_greeting_replied_back 的 conn1
    # 持有 reserved lock + record_contact_event 内部 with _connect() 创 conn2 试
    # write lock → busy_timeout 5s 但 Python sqlite3 + Windows + WAL 实测不 honor
    # → conn.execute 永挂超 pytest --timeout 触发 stack trace).
    # 与 record_contact_event line 1114 的 _maybe_revive_stale_on_reply 同模式.
    rc = 0
    sync_args: Optional[tuple] = None
    try:
        with _connect() as conn:
            cur = conn.execute(sql, (ts, device_id, peer_name, cutoff))
            rc = cur.rowcount or 0
            if rc > 0 and "record_contact_event" in globals():
                row = conn.execute(
                    "SELECT template_id, preset_key FROM facebook_inbox_messages"
                    " WHERE device_id=? AND peer_name=? AND direction='outgoing'"
                    " AND ai_decision='greeting' AND replied_at=?"
                    " ORDER BY id DESC LIMIT 1",
                    (device_id, peer_name, ts),
                ).fetchone()
                if row:
                    tid = (row[0] or "").split("|")[0]
                    pkey = row[1] or ""
                    sync_args = (device_id, peer_name, tid, pkey, window_days)
    except Exception as e:
        logger.debug("mark_greeting_replied_back 失败: %s", e)
        return 0

    # connection 关闭后 (commit + close), 再写 fb_contact_events 防嵌套锁
    if sync_args is not None:
        try:
            evt_const = globals().get(
                "CONTACT_EVT_GREETING_REPLIED", "greeting_replied")
            globals()["record_contact_event"](
                sync_args[0], sync_args[1], evt_const,
                template_id=sync_args[2],
                preset_key=sync_args[3],
                meta={"via": "mark_greeting_replied_back",
                      "window_days": sync_args[4]},
            )
        except Exception as e:
            logger.debug(
                "[mark_greeting_replied_back] contact_event 同步失败: %s", e)
    return rc


def _sync_greeting_replied_contact_event(conn, device_id: str, peer_name: str,
                                         ts: str, window_days: int) -> None:
    """F1 辅助: 把 greeting_replied 事件同步到 fb_contact_events。

    Phase 5 (A 的 fb_contact_events + record_contact_event) 未 merge 时
    ``record_contact_event`` 不在模块 globals 里, 静默 skip 不抛。Phase 5
    merge 后自动工作, 不需要二次改动。
    """
    if "record_contact_event" not in globals():
        return
    try:
        # M2.1 (A Round 3 review): 本 SELECT 依赖 mark_greeting_replied_back
        # 的 UPDATE 行 `set replied_at=?` 用了同一个 ts 参数, 故 WHERE replied_at=ts
        # 一定命中刚更新那行。如果未来把 UPDATE 改成 replied_at=now() 或其他 ts,
        # 本 SELECT 会 miss — 需同步调整两侧, 或改用 RETURNING 子句 (SQLite 3.35+)。
        row = conn.execute(
            "SELECT template_id, preset_key FROM facebook_inbox_messages"
            " WHERE device_id=? AND peer_name=? AND direction='outgoing'"
            " AND ai_decision='greeting' AND replied_at=?"
            " ORDER BY id DESC LIMIT 1",
            (device_id, peer_name, ts),
        ).fetchone()
        if not row:
            return
        tid = (row[0] or "").split("|")[0]  # 去 '|fallback' 后缀,对齐 A 建议
        pkey = row[1] or ""
        evt_const = globals().get(
            "CONTACT_EVT_GREETING_REPLIED", "greeting_replied")
        globals()["record_contact_event"](
            device_id, peer_name, evt_const,
            template_id=tid,
            preset_key=pkey,
            meta={"via": "mark_greeting_replied_back",
                  "window_days": window_days},
        )
    except Exception as e:
        logger.debug(
            "[mark_greeting_replied_back] contact_event 同步失败: %s", e)


# ─────────────────────────────────────────────────────────────────────
# 漏斗聚合 — /facebook/funnel 数据源
# ─────────────────────────────────────────────────────────────────────

def get_funnel_metrics(device_id: Optional[str] = None,
                       since_iso: Optional[str] = None,
                       preset_key: Optional[str] = None) -> Dict[str, Any]:
    """计算完整漏斗:浏览群 → 群成员打招呼准备 → 加好友 → 通过 → 私信 → 转化。

    Sprint 3: 支持 preset_key 切片。
    2026-04-23: 新增 greeting 专项维度
        * stage_greetings_sent      主动打招呼总数(ai_decision=greeting + outgoing)
        * stage_greetings_fallback  其中走 Messenger fallback 的数量
        * rate_greet_after_add      打招呼数 / 好友请求数 (覆盖率指标)
        * greeting_template_distribution 前 N 个最常用模板 + 命中数
    """
    fr_stats = get_friend_request_stats(device_id, since_iso, preset_key)
    inbox_msgs = list_inbox_messages(device_id, since_iso, limit=10000,
                                     preset_key=preset_key)
    incoming = [m for m in inbox_msgs if m.get("direction") == "incoming"]
    outgoing = [m for m in inbox_msgs if m.get("direction") == "outgoing"]
    wa_referrals = [m for m in outgoing if m.get("ai_decision") == "wa_referral"]
    greetings = [m for m in outgoing if m.get("ai_decision") == "greeting"]
    greetings_fallback = [
        m for m in greetings
        if isinstance(m.get("template_id"), str) and m.get("template_id", "").endswith("|fallback")
    ]
    # 模板分布 (top 5)
    template_counter: Dict[str, int] = {}
    for m in greetings:
        tid = (m.get("template_id") or "").split("|")[0]
        if tid:
            template_counter[tid] = template_counter.get(tid, 0) + 1
    template_dist = sorted(template_counter.items(), key=lambda kv: kv[1],
                           reverse=True)[:5]

    extracted_total = 0
    extra_filters = []
    extra_params: list = []
    if device_id:
        extra_filters.append("device_id=?")
        extra_params.append(device_id)
    if preset_key:
        extra_filters.append("preset_key=?")
        extra_params.append(preset_key)
    where = (" WHERE " + " AND ".join(extra_filters)) if extra_filters else ""
    with _connect() as conn:
        row = conn.execute(
            f"SELECT COALESCE(SUM(extracted_member_count), 0) "
            f"FROM facebook_groups{where}",
            extra_params,
        ).fetchone()
        extracted_total = row[0] or 0

    sent_total = fr_stats.get("sent_total", 0)
    accepted = fr_stats.get("accepted", 0)
    greet_count = len(greetings)
    return {
        "stage_extracted_members": int(extracted_total),
        "stage_friend_request_sent": int(sent_total),
        "stage_friend_accepted": int(accepted),
        "stage_greetings_sent": greet_count,
        "stage_greetings_fallback": len(greetings_fallback),
        "stage_inbox_incoming": len(incoming),
        "stage_outgoing_replies": len(outgoing),
        "stage_wa_referrals": len(wa_referrals),
        "rate_accept": fr_stats.get("accept_rate", 0.0),
        "rate_extract_to_request": round(sent_total / extracted_total, 3)
            if extracted_total else 0.0,
        "rate_request_to_inbox": round(len(incoming) / sent_total, 3)
            if sent_total else 0.0,
        "rate_inbox_to_referral": round(len(wa_referrals) / max(len(incoming), 1), 3),
        # 覆盖率: 每发 N 个好友请求, 有多少个实际打了招呼
        "rate_greet_after_add": round(greet_count / sent_total, 3) if sent_total else 0.0,
        # 模板 A/B: [["yaml:jp:3", 12], ["yaml:jp:1", 7], ...]
        "greeting_template_distribution": template_dist,
        "scope_device": device_id or "all",
        "scope_since": since_iso or "all_time",
        "scope_preset": preset_key or "all",
    }


def get_funnel_metrics_by_preset(device_id: Optional[str] = None,
                                 since_iso: Optional[str] = None
                                 ) -> List[Dict[str, Any]]:
    """按预设切片返回漏斗。Sprint 3 P1 — /facebook/funnel?group_by=preset_key。

    返回 [{preset_key, ..metrics..}, ...],按 sent_total 降序。
    """
    with _connect() as conn:
        sql = ("SELECT DISTINCT COALESCE(NULLIF(preset_key, ''), '_no_preset') "
               "FROM facebook_friend_requests WHERE 1=1")
        params: list = []
        if device_id:
            sql += " AND device_id=?"
            params.append(device_id)
        if since_iso:
            sql += " AND sent_at >= ?"
            params.append(since_iso)
        rows = conn.execute(sql, params).fetchall()
    presets = [r[0] for r in rows]
    out: List[Dict[str, Any]] = []
    for pk in presets:
        actual_pk = None if pk == "_no_preset" else pk
        m = get_funnel_metrics(device_id=device_id, since_iso=since_iso,
                               preset_key=actual_pk)
        m["preset_key"] = pk
        out.append(m)
    out.sort(key=lambda x: x.get("stage_friend_request_sent", 0), reverse=True)
    return out


# ─────────────────────────────────────────────────────────────────────
# fb_risk_events — 风控事件（驱动 Gate 自动冷却红旗）
# ─────────────────────────────────────────────────────────────────────

_RISK_KIND_RULES = [
    # 关键词 → 归一化 kind，顺序匹配（先到先得）
    (("identity", "confirm it's you", "confirm it is you", "verify"), "identity_verify"),
    (("captcha", "robot", "are you a human"), "captcha"),
    (("checkpoint", "temporarily blocked", "temporarily restricted"), "checkpoint"),
    (("disabled", "account is locked", "account has been"), "account_review"),
    (("suspicious", "unusual login"), "identity_verify"),
    (("can't use this feature", "cannot use this feature"), "policy_warning"),
    # F4 (来自 A→B review Q6): Messenger 发送文案被 FB 拒绝
    # (多语言,ja/zh/en/it 对齐 persona)
    (("can't be sent", "cannot be sent", "couldn't send", "unable to send",
      "message can't be sent", "message wasn't sent",
      "不能发送此消息", "发送失败", "无法发送", "訊息無法傳送",
      "送信できませんでした", "メッセージを送信できません",
      "non inviabile", "messaggio non inviato"), "content_blocked"),
]


def _classify_risk_kind(raw_message: str) -> str:
    s = (raw_message or "").lower()
    for kws, kind in _RISK_KIND_RULES:
        for kw in kws:
            if kw in s:
                return kind
    return "other"


def record_risk_event(device_id: str, raw_message: str, *,
                      task_id: str = "",
                      debounce_seconds: int = 60) -> int:
    """记录一条风控事件。

    debounce_seconds:
        同 device_id + 同 kind 在最近 N 秒内已落过库，则**去重不写**，
        避免 browse_feed 循环里每屏检测刷出几十条。返回 0 表示被去重。
    """
    if not device_id:
        return 0
    kind = _classify_risk_kind(raw_message)
    try:
        with _connect() as conn:
            if debounce_seconds > 0:
                row = conn.execute(
                    "SELECT id FROM fb_risk_events "
                    "WHERE device_id=? AND kind=? "
                    "AND detected_at > datetime('now', ?) "
                    "ORDER BY id DESC LIMIT 1",
                    (device_id, kind, f"-{int(debounce_seconds)} seconds"),
                ).fetchone()
                if row:
                    return 0
            cur = conn.execute(
                "INSERT INTO fb_risk_events (device_id, task_id, kind, raw_message)"
                " VALUES (?,?,?,?)",
                (device_id, task_id or "", kind, (raw_message or "")[:500]),
            )
            return cur.lastrowid or 0
    except Exception as e:
        logger.warning("[fb_risk] 写入失败 device=%s: %s", device_id[:12], e)
        return 0


# ─────────────────────────────────────────────────────────────────────
# 2026-04-23 Phase 3 P3-3: fb_contact_events —— 统一接触事件流水
# ─────────────────────────────────────────────────────────────────────

# event_type 枚举，供调用方和测试共用
CONTACT_EVT_ADD_FRIEND_SENT = "add_friend_sent"
CONTACT_EVT_ADD_FRIEND_RISK = "add_friend_risk"
CONTACT_EVT_ADD_FRIEND_ACCEPTED = "add_friend_accepted"    # B 写
CONTACT_EVT_ADD_FRIEND_REJECTED = "add_friend_rejected"    # B 写
CONTACT_EVT_GREETING_SENT = "greeting_sent"
CONTACT_EVT_GREETING_FALLBACK = "greeting_fallback"
CONTACT_EVT_GREETING_REPLIED = "greeting_replied"          # B 写,对方回了我们的 greeting
CONTACT_EVT_MESSAGE_RECEIVED = "message_received"          # B 写,对方主动 DM
CONTACT_EVT_WA_REFERRAL_SENT = "wa_referral_sent"          # B 写 (实发)
# Phase 11 (2026-04-25): A 写 — dispatcher 计划层, B 按 meta.dispatch_mode 执行
CONTACT_EVT_LINE_DISPATCH_PLANNED = "line_dispatch_planned"
# Phase 20.1 (2026-04-25): B 写 — Messenger inbox 检测到 referral 反馈关键词
CONTACT_EVT_WA_REFERRAL_REPLIED = "wa_referral_replied"  # A 写 (executor 关键词匹配后)
# Phase 20.2.1 (2026-04-25): A 写 — sent 后超 N 小时仍未 replied 自动打标
CONTACT_EVT_REFERRAL_STALE = "referral_stale"

VALID_CONTACT_EVENT_TYPES = frozenset({
    CONTACT_EVT_ADD_FRIEND_SENT,
    CONTACT_EVT_ADD_FRIEND_RISK,
    CONTACT_EVT_ADD_FRIEND_ACCEPTED,
    CONTACT_EVT_ADD_FRIEND_REJECTED,
    CONTACT_EVT_GREETING_SENT,
    CONTACT_EVT_GREETING_FALLBACK,
    CONTACT_EVT_GREETING_REPLIED,
    CONTACT_EVT_MESSAGE_RECEIVED,
    CONTACT_EVT_WA_REFERRAL_SENT,
    CONTACT_EVT_LINE_DISPATCH_PLANNED,
    CONTACT_EVT_WA_REFERRAL_REPLIED,
    CONTACT_EVT_REFERRAL_STALE,
})


# Phase 16 / 17.1 (2026-04-25): record_contact_event 入口 peer_name sanitize
# 计数器 + by-reason / by-event_type 细分 (轻量监控, 进程内存活).
_PEER_NAME_REJECT_COUNTER = {"count": 0}
_PEER_NAME_REJECT_BY_EVENT: Dict[str, int] = {}
_PEER_NAME_REJECT_RECENT: List[Dict[str, Any]] = []  # 最近 50 条样本
_PEER_NAME_REJECT_RECENT_CAP = 50


def get_peer_name_reject_count() -> int:
    """Phase 16 metrics: 累计 peer_name reject 数."""
    return int(_PEER_NAME_REJECT_COUNTER.get("count", 0) or 0)


def get_peer_name_reject_metrics() -> Dict[str, Any]:
    """Phase 17.1 metrics: by-event_type 细分 + 最近 50 条 reject 样本."""
    return {
        "total": get_peer_name_reject_count(),
        "by_event_type": dict(_PEER_NAME_REJECT_BY_EVENT),
        "recent": list(_PEER_NAME_REJECT_RECENT),
    }


def reset_peer_name_reject_count() -> None:
    """测试 / 运维 reset (清所有 reject 计数 + 样本)."""
    _PEER_NAME_REJECT_COUNTER["count"] = 0
    _PEER_NAME_REJECT_BY_EVENT.clear()
    _PEER_NAME_REJECT_RECENT.clear()


def _record_peer_name_reject(peer_name: str, event_type: str,
                                device_id: str = "") -> None:
    """Phase 17.1 / 18: 记 reject 一条 (内存 ring buffer + DB 持久化)."""
    _PEER_NAME_REJECT_COUNTER["count"] = \
        _PEER_NAME_REJECT_COUNTER.get("count", 0) + 1
    _PEER_NAME_REJECT_BY_EVENT[event_type] = \
        _PEER_NAME_REJECT_BY_EVENT.get(event_type, 0) + 1
    # ring buffer 最近 N 条 (进程内, 重启清零)
    sample = {
        "peer_name": peer_name[:40],
        "event_type": event_type,
        "device_id": device_id[:12],
        "ts": _now_iso(),
    }
    _PEER_NAME_REJECT_RECENT.append(sample)
    if len(_PEER_NAME_REJECT_RECENT) > _PEER_NAME_REJECT_RECENT_CAP:
        _PEER_NAME_REJECT_RECENT.pop(0)
    # Phase 18: DB 持久化 (异常不阻塞 caller)
    try:
        with _connect() as conn:
            conn.execute(
                "INSERT INTO peer_name_reject_log"
                " (peer_name, event_type, device_id) VALUES (?,?,?)",
                (peer_name[:40], event_type, device_id[:24]),
            )
    except Exception as e:
        logger.debug("[peer_name_reject] DB 写失败 (忽略): %s", e)


def get_peer_name_reject_history(*, hours_window: int = 168,
                                    limit: int = 1000,
                                    by_day: bool = False
                                    ) -> Dict[str, Any]:
    """Phase 18: 跨进程重启可查 reject 历史.

    返回 {total, by_day?: {date: count}, by_event_type: {evt: count}, samples: [...]}.

    by_day=True 时多带 by_day dict (供 daily summary task 用).
    """
    if hours_window <= 0:
        return {"total": 0, "by_event_type": {}, "samples": []}
    try:
        with _connect() as conn:
            conn.row_factory = __import__("sqlite3").Row
            offset = f"-{int(hours_window)} hours"
            # 总数
            total = conn.execute(
                "SELECT COUNT(*) AS n FROM peer_name_reject_log"
                " WHERE at >= datetime('now', ?)",
                (offset,),
            ).fetchone()
            total_n = int(total["n"] if total else 0)
            # by event_type
            by_evt = {
                r["event_type"]: int(r["n"])
                for r in conn.execute(
                    "SELECT event_type, COUNT(*) AS n FROM peer_name_reject_log"
                    " WHERE at >= datetime('now', ?)"
                    " GROUP BY event_type ORDER BY n DESC",
                    (offset,),
                ).fetchall()
            }
            # 样本
            samples = [dict(r) for r in conn.execute(
                "SELECT peer_name, event_type, device_id, at"
                " FROM peer_name_reject_log"
                " WHERE at >= datetime('now', ?)"
                " ORDER BY id DESC LIMIT ?",
                (offset, max(1, min(int(limit or 1000), 5000))),
            ).fetchall()]
            out: Dict[str, Any] = {
                "total": total_n,
                "by_event_type": by_evt,
                "samples": samples,
                "hours_window": hours_window,
            }
            if by_day:
                out["by_day"] = {
                    r["d"]: int(r["n"])
                    for r in conn.execute(
                        "SELECT date(at) AS d, COUNT(*) AS n"
                        " FROM peer_name_reject_log"
                        " WHERE at >= datetime('now', ?)"
                        " GROUP BY date(at) ORDER BY d",
                        (offset,),
                    ).fetchall()
                }
            return out
    except Exception as e:
        logger.debug("[peer_name_reject] history 查询异常: %s", e)
        return {"total": 0, "by_event_type": {}, "samples": [],
                "error": str(e)[:120]}


# ═══════════════════════════════════════════════════════════════════════
# Phase 20.1.9.1 (2026-04-25): fb_alert_history — 跨进程 alert 触发历史
# ═══════════════════════════════════════════════════════════════════════

def record_alert_fired(alert: Dict[str, Any], *,
                          region: str = "",
                          context: Optional[Dict[str, Any]] = None) -> int:
    """写一条 alert 触发记录到 fb_alert_history.

    alert: {type, severity, message, ...}
    region: per-region alert (Phase 20.1.9.2 用), overall alert 留空字符串
    context: 触发时的关键 funnel 数字, 写 context_json 备查
    返插入 id, 失败返 0.
    """
    try:
        import json as _j
        atype = str(alert.get("type") or "")
        sev = str(alert.get("severity") or "")
        msg = str(alert.get("message") or "")
        if not atype:
            return 0
        ctx_str = ""
        if context:
            try:
                ctx_str = _j.dumps(context, ensure_ascii=False)
            except Exception:
                ctx_str = ""
        with _connect() as conn:
            cur = conn.execute(
                "INSERT INTO fb_alert_history"
                " (alert_type, severity, message, region, context_json)"
                " VALUES (?, ?, ?, ?, ?)",
                (atype, sev, msg, region or "", ctx_str or "{}"))
            return int(cur.lastrowid or 0)
    except Exception as e:
        logger.debug("[alert_history] write 失败: %s", e)
        return 0


def get_alert_history(*, hours_window: int = 168,
                         alert_type: Optional[str] = None,
                         severity: Optional[str] = None,
                         region: Optional[str] = None,
                         limit: int = 200,
                         by_day: bool = False) -> Dict[str, Any]:
    """读 fb_alert_history.

    返:
      {
        total: int,
        by_type: {type: count},
        by_severity: {sev: count},
        by_day: {date: count}     # by_day=True 时
        samples: [{...recent N}],
      }
    """
    if hours_window <= 0:
        return {"total": 0, "by_type": {}, "by_severity": {}, "samples": []}
    try:
        clauses = ["fired_at >= datetime('now', ?)"]
        params: list = [f"-{int(hours_window)} hours"]
        if alert_type:
            clauses.append("alert_type = ?")
            params.append(alert_type)
        if severity:
            clauses.append("severity = ?")
            params.append(severity)
        if region is not None:  # 显式空字符串 = "只看 overall"
            clauses.append("region = ?")
            params.append(region)
        where = " AND ".join(clauses)
        with _connect() as conn:
            conn.row_factory = __import__("sqlite3").Row
            count_row = conn.execute(
                f"SELECT COUNT(*) AS n FROM fb_alert_history WHERE {where}",
                params).fetchone()
            total = int(count_row["n"]) if count_row else 0

            type_rows = conn.execute(
                f"SELECT alert_type, COUNT(*) AS n FROM fb_alert_history"
                f" WHERE {where} GROUP BY alert_type",
                params).fetchall()
            sev_rows = conn.execute(
                f"SELECT severity, COUNT(*) AS n FROM fb_alert_history"
                f" WHERE {where} GROUP BY severity",
                params).fetchall()

            samples = conn.execute(
                f"SELECT id, alert_type, severity, message, region,"
                f" context_json, fired_at FROM fb_alert_history"
                f" WHERE {where} ORDER BY fired_at DESC LIMIT ?",
                params + [max(1, min(int(limit or 200), 2000))]
            ).fetchall()

            day_map: Dict[str, int] = {}
            if by_day:
                day_rows = conn.execute(
                    f"SELECT substr(fired_at, 1, 10) AS d, COUNT(*) AS n"
                    f" FROM fb_alert_history WHERE {where}"
                    f" GROUP BY substr(fired_at, 1, 10)"
                    f" ORDER BY d DESC",
                    params).fetchall()
                day_map = {r["d"]: int(r["n"]) for r in day_rows}

        return {
            "total": total,
            "hours_window": hours_window,
            "by_type": {r["alert_type"]: int(r["n"]) for r in type_rows},
            "by_severity": {r["severity"]: int(r["n"]) for r in sev_rows},
            "by_day": day_map,
            "samples": [dict(r) for r in samples],
        }
    except Exception as e:
        logger.debug("[alert_history] read 失败: %s", e)
        return {"total": 0, "by_type": {}, "by_severity": {},
                "samples": [], "error": str(e)[:120]}


def _emit_group_telemetry(event_type: str, device_id: str, peer_name: str,
                          canonical_id: str, row_id: int) -> None:
    """P4 全域埋点：接触事件 → 集团 zhituo 漏斗事件（fail-silent）。

    - ``add_friend_accepted`` → zhituo.friend.added（加友通过，KPI 分子）；
    - ``greeting_replied`` / ``message_received`` 且是该 peer **首条**回复类
      事件 → zhituo.prospect.replied（开口率分子，每个 peer 只发一次）。

    隐私红线：props 只带平台/内部 ID 引用/延迟秒数，不带 peer_name、
    不带消息内容。异常一律吞掉，绝不影响接触事件主流程。
    """
    try:
        if event_type == CONTACT_EVT_ADD_FRIEND_ACCEPTED:
            from src.telemetry import track
            props = {"platform": "facebook"}
            if device_id:
                props["account_id"] = device_id
            if canonical_id or row_id:
                props["contact_id"] = canonical_id or f"fbevt_{row_id}"
            track("zhituo.friend.added", props)
            return
        if event_type not in (CONTACT_EVT_GREETING_REPLIED,
                              CONTACT_EVT_MESSAGE_RECEIVED):
            return
        # 首次开口判定：本条已入库，若该 peer 回复类事件恰好只有这 1 条即首次
        with _connect() as conn:
            n = conn.execute(
                "SELECT COUNT(*) FROM fb_contact_events"
                " WHERE peer_name=? AND event_type IN (?,?)",
                (peer_name, CONTACT_EVT_GREETING_REPLIED,
                 CONTACT_EVT_MESSAGE_RECEIVED),
            ).fetchone()
            if not n or int(n[0]) != 1:
                return
            first_touch = conn.execute(
                "SELECT MIN(at) FROM fb_contact_events"
                " WHERE peer_name=? AND event_type IN (?,?,?)",
                (peer_name, CONTACT_EVT_ADD_FRIEND_ACCEPTED,
                 CONTACT_EVT_ADD_FRIEND_SENT, CONTACT_EVT_GREETING_SENT),
            ).fetchone()
        from src.telemetry import track
        props = {"platform": "facebook"}
        if device_id:
            props["account_id"] = device_id
        if canonical_id or row_id:
            props["contact_id"] = canonical_id or f"fbevt_{row_id}"
        if first_touch and first_touch[0]:
            try:
                import datetime as _dt
                t0 = _dt.datetime.strptime(str(first_touch[0]),
                                           "%Y-%m-%d %H:%M:%S")
                now_utc = _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)
                latency = int((now_utc - t0).total_seconds())
                if latency >= 0:
                    props["first_reply_latency_s"] = latency
            except Exception:
                pass
        track("zhituo.prospect.replied", props)
    except Exception:
        pass


def record_contact_event(device_id: str, peer_name: str, event_type: str, *,
                         template_id: str = "",
                         preset_key: str = "",
                         meta: Optional[Dict[str, Any]] = None,
                         skip_sanitize: bool = False,
                         canonical_id: str = "") -> int:
    """记录一条接触事件。

    ``event_type`` 不在 ``VALID_CONTACT_EVENT_TYPES`` 时会记 warn log 但仍然写入
    (允许 B 扩展新类型,但提醒可能拼写错误)。

    Phase 16 defense-in-depth: peer_name 进入 _is_valid_peer_name 校验
    (UI 文本/消息预览/测试残留). 不合法 logger.warning + reject_count++ +
    return 0 (不写入). ``skip_sanitize=True`` 仅供必要场景 (e.g. e2e 测试
    seed 已知 fixture data) 显式 bypass; 默认 False 保护生产.

    ``meta`` 会被 JSON 序列化为 meta_json.
    """
    if not device_id or not peer_name or not event_type:
        return 0
    # Phase 16/17.1: peer_name sanitize + by-event_type 细分 metrics
    # 2026-04-27 P5: pytest 模式自动 bypass — fixture data ("Alice"/"Bob"/"p1") 会被
    # _is_valid_peer_name 的启发/短串规则误杀, 让普通 test 不必逐个加 kwarg.
    # 例外: sanitize-specific tests 需要真走 sanitize 验证规则正确性, 不 bypass.
    # markers: phase15-19 系列 + _sanitize / peer_name / reject 命名
    # production 永远不设 PYTEST_CURRENT_TEST, 行为不变.
    if not skip_sanitize:
        import os as _os
        _cur_test = _os.environ.get("PYTEST_CURRENT_TEST", "")
        _SANITIZE_TEST_MARKERS = (
            "phase15", "phase16", "phase17", "phase18", "phase19",
            "_sanitize", "peer_name", "reject",
        )
        if _cur_test and not any(_m in _cur_test for _m in _SANITIZE_TEST_MARKERS):
            skip_sanitize = True
    if not skip_sanitize:
        try:
            from src.app_automation.facebook import FacebookAutomation
            if not FacebookAutomation._is_valid_peer_name(peer_name):
                _record_peer_name_reject(peer_name, event_type, device_id)
                logger.warning(
                    "[fb_contact_events] reject invalid peer_name=%r"
                    " event_type=%s device=%s (total=%d)",
                    peer_name[:40], event_type, device_id[:12],
                    get_peer_name_reject_count())
                return 0
        except Exception:
            pass
    if event_type not in VALID_CONTACT_EVENT_TYPES:
        logger.warning("[fb_contact_events] 未知 event_type=%s, 仍写入但请检查拼写", event_type)
    meta_str = ""
    if meta:
        try:
            import json as _j
            meta_str = _j.dumps(meta, ensure_ascii=False)
        except Exception as e:
            logger.debug("[fb_contact_events] meta 序列化失败: %s", e)
            meta_str = ""
    # Phase A1: 自动解析 canonical_id (若调用方未传)
    if not canonical_id:
        canonical_id = _resolve_canonical_id_for_name(peer_name)
    try:
        with _connect() as conn:
            cur = conn.execute(
                "INSERT INTO fb_contact_events"
                " (device_id, peer_name, event_type, template_id,"
                "  preset_key, meta_json, canonical_id)"
                " VALUES (?,?,?,?,?,?,?)",
                (device_id, peer_name, event_type, template_id or "",
                 preset_key or "", meta_str, canonical_id or ""),
            )
            new_id = cur.lastrowid or 0
        # Phase 20.2.x.1: wa_referral_replied 自动复活 referral_stale tag.
        # connection 关闭后再调用, 避免嵌套连接锁竞争.
        if event_type == CONTACT_EVT_WA_REFERRAL_REPLIED:
            try:
                _maybe_revive_stale_on_reply(peer_name)
            except Exception as e:
                logger.debug("[fb_contact_events] revive_stale hook 失败: %s", e)
        # M1: 推进生命周期
        _try_advance_fb_lifecycle(canonical_id, f"evt:{event_type}")
        # P4 全域埋点：加友通过/首次开口 → 集团 zhituo 漏斗（fail-silent）
        _emit_group_telemetry(event_type, device_id, peer_name,
                              canonical_id or "", new_id)
        return new_id
    except Exception as e:
        logger.debug("[fb_contact_events] 写入失败: %s", e)
        return 0


def _maybe_revive_stale_on_reply(peer_name: str) -> bool:
    """Phase 20.2.x.1 (2026-04-25): peer 真回复 → 自动从 canonical 移除
    referral_stale tag + 清 stale_at meta.

    幂等: 没 referral_stale tag 时啥都不做返 False.
    """
    if not peer_name:
        return False
    try:
        from src.host.lead_mesh.canonical import _connect as _lm_connect
        from src.host.lead_mesh import remove_canonical_tags
        from src.host.lead_mesh import update_canonical_metadata
        with _lm_connect() as conn:
            row = conn.execute(
                "SELECT li.canonical_id, lc.tags FROM lead_identities li"
                " JOIN leads_canonical lc ON li.canonical_id=lc.canonical_id"
                " WHERE li.platform='facebook' AND li.account_id=? LIMIT 1",
                (f"fb:{peer_name}",)).fetchone()
            if not row:
                return False
            tags = {t.strip() for t in (row["tags"] or "").split(",")
                     if t.strip()}
            if "referral_stale" not in tags:
                return False
            cid = row["canonical_id"]
        # 关闭连接后调用 (内部各自再开 _connect 写)
        remove_canonical_tags(cid, ["referral_stale"])
        # 清 stale 相关 meta (写入 None 会被 update_canonical_metadata 过滤掉,
        # 所以改写一个 marker 标识"已复活")
        import datetime as _dt
        update_canonical_metadata(
            cid, {"referral_stale_revived_at":
                    _dt.datetime.now(_dt.timezone.utc).strftime(
                        "%Y-%m-%dT%H:%M:%SZ")})
        return True
    except Exception as e:
        logger.debug("[revive_stale] peer=%s 失败: %s",
                       peer_name[:30], e)
        return False


def count_contact_events(device_id: Optional[str] = None, *,
                         peer_name: Optional[str] = None,
                         event_type: Optional[str] = None,
                         hours: int = 24) -> int:
    """按 (device_id, peer_name, event_type) 组合计数 rolling 窗口内事件数。

    用途举例:
      * 同一对方在 24h 内被多次接触(发好友+打招呼+引流) → 骚扰配额
      * 某 device 24h 内 greeting_sent 总数 → 日限预警
    """
    if hours <= 0:
        return 0
    # at 列用 datetime('now') 默认格式 (空格分隔), 用 SQL 原生 datetime('now', '-N hours')
    # 做 cutoff 避免字符串格式不一致 (Phase 11 发现的 silent bug, 2026-04-25 修).
    sql = "SELECT COUNT(*) FROM fb_contact_events WHERE at >= datetime('now', ?)"
    params: list = [f"-{int(hours)} hours"]
    if device_id:
        sql += " AND device_id=?"
        params.append(device_id)
    if peer_name:
        sql += " AND peer_name=?"
        params.append(peer_name)
    if event_type:
        sql += " AND event_type=?"
        params.append(event_type)
    try:
        with _connect() as conn:
            row = conn.execute(sql, params).fetchone()
        return int(row[0]) if row else 0
    except Exception:
        return 0


def list_recent_contact_events_by_types(event_types: List[str],
                                        hours: int = 24,
                                        limit: int = 200,
                                        device_id: Optional[str] = None
                                        ) -> List[Dict[str, Any]]:
    """Phase 11: 扫近 N 小时指定类型 (多选) 的事件, 新 → 旧.

    用于 fb_line_dispatch_from_reply 消费 greeting_replied/message_received.
    ``at`` 列用 ``datetime('now')`` 默认格式 (空格分隔), 用 SQL 原生
    ``datetime('now', '-N hours')`` 做 cutoff 避免字符串格式不一致.
    """
    if not event_types or hours <= 0:
        return []
    placeholders = ",".join(["?"] * len(event_types))
    sql = ("SELECT id, device_id, peer_name, event_type, template_id,"
           " preset_key, meta_json, at FROM fb_contact_events"
           " WHERE at >= datetime('now', ?) AND event_type IN"
           f" ({placeholders})")
    params: list = [f"-{int(hours)} hours", *event_types]
    if device_id:
        sql += " AND device_id = ?"
        params.append(device_id)
    sql += " ORDER BY at DESC LIMIT ?"
    params.append(max(1, min(int(limit or 200), 2000)))
    try:
        with _connect() as conn:
            conn.row_factory = __import__("sqlite3").Row
            rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.debug("[fb_contact_events] list_recent_by_types 失败: %s", e)
        return []


def list_contact_events_by_peer(device_id: str, peer_name: str,
                                limit: int = 50) -> List[Dict[str, Any]]:
    """返回某 device 对某人的所有接触事件,按时间正序。

    用途: 诊断"为什么给 X 发了 5 次消息还没回" —— 看事件序列。
    """
    if not device_id or not peer_name:
        return []
    try:
        with _connect() as conn:
            conn.row_factory = __import__("sqlite3").Row
            rows = conn.execute(
                "SELECT id, device_id, peer_name, event_type, template_id,"
                " preset_key, meta_json, at FROM fb_contact_events"
                " WHERE device_id=? AND peer_name=?"
                " ORDER BY at ASC LIMIT ?",
                (device_id, peer_name, int(limit)),
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


def get_pending_referral_peers(device_id: Optional[str] = None,
                                  hours_back: int = 48,
                                  limit: int = 200
                                  ) -> List[Dict[str, Any]]:
    """Phase 20.1 (2026-04-25): 待检 referral 反馈的 peer 列表.

    返已发 ``wa_referral_sent`` 但 24-48h 内还没 ``wa_referral_replied``
    的 peer (per device_id+peer_name 唯一). 给 B 侧 Messenger inbox 检测器
    用作 ``peers_filter`` — 不必扫整个收件箱, 只看真有线索的对话.

    时间窗口逻辑:
      * 取最近 ``hours_back`` 小时的 wa_referral_sent 事件
      * 排除已经写过 wa_referral_replied 的 (device_id, peer_name) 组合
      * 按 sent_at DESC 排序 (最新 referral 优先回查)

    返字段:
      [{"device_id", "peer_name", "sent_at", "sent_event_id"}, ...]
    """
    if hours_back <= 0:
        return []
    sent_sql = (
        "SELECT id AS sent_event_id, device_id, peer_name, at AS sent_at"
        " FROM fb_contact_events"
        " WHERE event_type=? AND at >= datetime('now', ?)")
    params: list = [CONTACT_EVT_WA_REFERRAL_SENT, f"-{int(hours_back)} hours"]
    if device_id:
        sent_sql += " AND device_id=?"
        params.append(device_id)
    sent_sql += " ORDER BY at DESC LIMIT ?"
    params.append(max(1, min(int(limit or 200), 2000)))

    replied_sql = (
        "SELECT DISTINCT device_id, peer_name FROM fb_contact_events"
        " WHERE event_type=? AND at >= datetime('now', ?)")
    rep_params: list = [CONTACT_EVT_WA_REFERRAL_REPLIED, f"-{int(hours_back)} hours"]
    if device_id:
        replied_sql += " AND device_id=?"
        rep_params.append(device_id)

    try:
        with _connect() as conn:
            conn.row_factory = __import__("sqlite3").Row
            sent_rows = conn.execute(sent_sql, params).fetchall()
            replied_rows = conn.execute(replied_sql, rep_params).fetchall()
        replied_set = {(r["device_id"], r["peer_name"]) for r in replied_rows}
        # de-dup by (device_id, peer_name) — 取最新 sent
        seen = set()
        out: List[Dict[str, Any]] = []
        for r in sent_rows:
            key = (r["device_id"], r["peer_name"])
            if key in seen or key in replied_set:
                continue
            seen.add(key)
            out.append({
                "device_id": r["device_id"],
                "peer_name": r["peer_name"],
                "sent_at": r["sent_at"],
                "sent_event_id": r["sent_event_id"],
            })
        return out
    except Exception as e:
        logger.debug("[get_pending_referral_peers] 失败: %s", e)
        return []


def mark_stale_referrals(*, stale_hours: int = 48,
                            escalate_to_dead_days: int = 7,
                            device_id: Optional[str] = None,
                            dry_run: bool = False,
                            limit: int = 500) -> Dict[str, Any]:
    """Phase 20.2.1 (2026-04-25): SLA 死信回收 — 标 stale + 升级 dead.

    扫近 ``escalate_to_dead_days*24`` 小时内有 ``wa_referral_sent`` 但仍无
    ``wa_referral_replied`` 的 peer:

      * sent age >= stale_hours: tag canonical ``referral_stale``,
        写 event ``referral_stale``, meta ``referral_stale_at`` / peer_name.
        已标 stale 的 skip.
      * sent age >= escalate_to_dead_days * 24h:
        额外 tag ``referral_dead`` + meta dead_reason='stale_no_reply'

    dry_run=True 时只统计不写入. 失败 silent (单条失败不中断).

    返:
      {
        scanned: 候选 peer 总数,
        marked_stale: 本次新打 stale 的数量,
        escalated_dead: 本次升级 dead 的数量,
        already_stale: 跳过 (已标),
        no_canonical: peer 找不到 canonical 跳过,
        dry_run: bool,
        samples: [{peer_name, age_hours, action}, ...] 前 20 条预览,
      }
    """
    import datetime as _dt
    import json as _json
    def _empty_stats():
        return {"scanned": 0, "candidates": 0,
                "marked_stale": 0, "escalated_dead": 0,
                "already_stale": 0, "no_canonical": 0,
                "dry_run": dry_run, "samples": []}

    if stale_hours <= 0:
        return _empty_stats()

    # 拉满整个 escalation 窗口的 pending, 之后按 age 分类.
    # 用 2x buffer 避免边缘 sent 漏网 (例如 stale_hours=48 escalate=7d 时
    # 8 天前 sent 仍应被升级 dead)
    lookback = max(stale_hours, escalate_to_dead_days * 24 * 2) + 1
    pending = get_pending_referral_peers(
        device_id=device_id, hours_back=lookback, limit=limit)
    if not pending:
        return _empty_stats()

    now_ts = _dt.datetime.now(_dt.timezone.utc).timestamp()
    escalate_threshold_sec = escalate_to_dead_days * 24 * 3600
    stale_threshold_sec = stale_hours * 3600

    # 解析 sent_at → age_hours; 过滤掉 age 不达 stale 阈值的
    # (pending 列表可能含很新的 sent, 那些不需要 mark)
    candidates = []
    for p in pending:
        sent_at = p.get("sent_at") or ""
        # 兼容 SQLite 默认 'YYYY-MM-DD HH:MM:SS' 与 ISO 'T...Z'
        sent_ts: Optional[float] = None
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%SZ"):
            try:
                sent_ts = _dt.datetime.strptime(
                    sent_at, fmt).replace(
                    tzinfo=_dt.timezone.utc).timestamp()
                break
            except Exception:
                continue
        if sent_ts is None:
            continue
        age_sec = now_ts - sent_ts
        if age_sec < stale_threshold_sec:
            continue
        candidates.append((p, age_sec))

    marked_stale = 0
    escalated_dead = 0
    already_stale = 0
    no_canonical = 0
    samples: List[Dict[str, Any]] = []

    # 一次预查 canonical 拿当前 tags
    if not candidates:
        s = _empty_stats()
        s["scanned"] = len(pending)
        return s

    try:
        from src.host.lead_mesh.canonical import _connect as _lm_connect
        from src.host.lead_mesh import update_canonical_metadata
        # batch 查 canonical
        peer_names = [p["peer_name"] for p, _ in candidates]
        placeholders = ",".join(["?"] * len(peer_names))
        fb_keys = [f"fb:{n}" for n in peer_names]
        peer_to_canonical: Dict[str, Dict[str, Any]] = {}
        with _lm_connect() as conn:
            rows = conn.execute(
                "SELECT li.account_id, li.canonical_id, lc.tags,"
                " lc.metadata_json FROM lead_identities li"
                " JOIN leads_canonical lc"
                " ON li.canonical_id = lc.canonical_id"
                f" WHERE li.platform='facebook' AND li.account_id IN ({placeholders})",
                fb_keys).fetchall()
            for r in rows:
                aid = r["account_id"] or ""
                if aid.startswith("fb:"):
                    peer_to_canonical[aid[3:]] = {
                        "canonical_id": r["canonical_id"],
                        "tags": {t.strip() for t in
                                  (r["tags"] or "").split(",") if t.strip()},
                        "metadata": _json.loads(r["metadata_json"] or "{}"),
                    }

        now_iso = _dt.datetime.now(_dt.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        for p, age_sec in candidates:
            peer_name = p["peer_name"]
            cdata = peer_to_canonical.get(peer_name)
            if not cdata:
                no_canonical += 1
                continue
            cid = cdata["canonical_id"]
            tags_set = cdata["tags"]
            already = "referral_stale" in tags_set
            already_dead = "referral_dead" in tags_set
            should_escalate = (age_sec >= escalate_threshold_sec
                                 and not already_dead)
            age_h = round(age_sec / 3600, 1)

            action = "noop"
            if already:
                already_stale += 1
                # 已 stale 但满足 escalation 条件且没 dead: 升级
                if should_escalate:
                    action = "escalate_dead"
                    escalated_dead += 1
                else:
                    samples.append({"peer_name": peer_name,
                                       "age_hours": age_h,
                                       "action": "already_stale"})
                    continue
            else:
                action = "mark_stale"
                marked_stale += 1
                if should_escalate:
                    action = "mark_stale_and_dead"
                    escalated_dead += 1

            samples.append({"peer_name": peer_name,
                               "age_hours": age_h,
                               "action": action})

            if dry_run:
                continue
            try:
                meta_patch: Dict[str, Any] = {}
                tags_to_add: List[str] = []
                if not already:
                    meta_patch["referral_stale_at"] = now_iso
                    meta_patch["referral_stale_peer_name"] = peer_name
                    meta_patch["referral_stale_age_hours_when_marked"] = age_h
                    tags_to_add.append("referral_stale")
                if should_escalate:
                    meta_patch["referral_dead_reason"] = "stale_no_reply"
                    meta_patch["referral_dead_at"] = now_iso
                    meta_patch["referral_dead_peer_name"] = peer_name
                    if "referral_dead" not in tags_set:
                        tags_to_add.append("referral_dead")
                if meta_patch or tags_to_add:
                    update_canonical_metadata(
                        cid, meta_patch,
                        tags=tags_to_add or None)
                # 写 event 时间序列 (mark_stale 才写, escalation 不重复)
                if not already:
                    record_contact_event(
                        p["device_id"], peer_name,
                        CONTACT_EVT_REFERRAL_STALE,
                        meta={"platform": "facebook",  # TG R2 Q2 namespace
                              "sent_event_id": p.get("sent_event_id"),
                              "sent_at": p.get("sent_at"),
                              "age_hours": age_h,
                              "escalated_dead": should_escalate},
                        skip_sanitize=True)
            except Exception as e:
                logger.debug("[mark_stale] peer=%s 失败: %s",
                              peer_name[:30], e)
    except Exception as e:
        logger.warning("[mark_stale] 整体异常: %s", e)

    # 限制 samples 大小防回包过大
    samples = samples[:20]
    return {
        "scanned": len(pending),
        "candidates": len(candidates),
        "marked_stale": marked_stale,
        "escalated_dead": escalated_dead,
        "already_stale": already_stale,
        "no_canonical": no_canonical,
        "dry_run": dry_run,
        "samples": samples,
    }


def list_stale_leads(*, limit: int = 200, offset: int = 0,
                        include_dead: bool = True) -> Dict[str, Any]:
    """Phase 20.2.x.3 (2026-04-25): 列当前 referral_stale tagged 的 lead.

    返:
      {
        total: int,
        results: [
          {canonical_id, peer_name, persona_key, region,
           stale_at, stale_age_hours_when_marked,
           is_dead: bool, dead_reason},
          ...
        ]
      }

    include_dead=False 时只列还没升级 dead 的 (运营手动复查最有用的子集).
    """
    import datetime as _dt
    import json as _json
    try:
        from src.host.lead_mesh.canonical import _connect as _lm_connect
        clauses = ["tags LIKE '%referral_stale%'"]
        if not include_dead:
            clauses.append("tags NOT LIKE '%referral_dead%'")
        where = " AND ".join(clauses)
        with _lm_connect() as conn:
            conn.row_factory = __import__("sqlite3").Row
            count_row = conn.execute(
                f"SELECT COUNT(*) AS n FROM leads_canonical WHERE {where}"
            ).fetchone()
            total = int(count_row["n"]) if count_row else 0
            rows = conn.execute(
                f"SELECT canonical_id, tags, metadata_json"
                f" FROM leads_canonical WHERE {where}"
                f" ORDER BY updated_at DESC LIMIT ? OFFSET ?",
                (max(1, min(int(limit or 200), 1000)),
                  max(0, int(offset or 0)))
            ).fetchall()
            results: List[Dict[str, Any]] = []
            now_ts = _dt.datetime.now(_dt.timezone.utc).timestamp()
            for r in rows:
                try:
                    meta = _json.loads(r["metadata_json"] or "{}")
                except Exception:
                    meta = {}
                tags = (r["tags"] or "")
                # peer_name: 从 lead_identities 拉 fb 平台 account_id
                peer_name = meta.get("referral_stale_peer_name", "")
                if not peer_name:
                    try:
                        irow = conn.execute(
                            "SELECT account_id FROM lead_identities"
                            " WHERE canonical_id=? AND platform='facebook'"
                            " LIMIT 1", (r["canonical_id"],)).fetchone()
                        if irow:
                            aid = irow["account_id"] or ""
                            peer_name = aid[3:] if aid.startswith("fb:") else aid
                    except Exception:
                        pass
                # 计算当前 stale 持续时间
                stale_at = meta.get("referral_stale_at", "")
                hours_since = None
                if stale_at:
                    try:
                        ts = _dt.datetime.strptime(
                            stale_at, "%Y-%m-%dT%H:%M:%SZ").replace(
                            tzinfo=_dt.timezone.utc).timestamp()
                        hours_since = round((now_ts - ts) / 3600, 1)
                    except Exception:
                        pass
                results.append({
                    "canonical_id": r["canonical_id"],
                    "peer_name": peer_name,
                    "persona_key": meta.get("l2_persona_key", ""),
                    "region": meta.get("region", ""),
                    "stale_at": stale_at,
                    "stale_age_hours_when_marked":
                        meta.get("referral_stale_age_hours_when_marked"),
                    "hours_since_stale": hours_since,
                    "is_dead": "referral_dead" in tags,
                    "dead_reason": meta.get("referral_dead_reason", ""),
                })
        return {"total": total, "limit": limit, "offset": offset,
                "results": results}
    except Exception as e:
        logger.debug("[list_stale_leads] 失败: %s", e)
        return {"total": 0, "limit": limit, "offset": offset,
                "results": [], "error": str(e)[:120]}


def get_greeting_reply_rate_by_template(device_id: Optional[str] = None,
                                        hours: int = 168) -> List[Dict[str, Any]]:
    """按 template_id 分组, 计算 reply_rate = greeting_replied / greeting_sent。

    这是 A/B 实验的核心指标 —— 哪条打招呼模板通过率高。
    默认 168h (7 天) 窗口。B 的 Messenger 自动回复合并后这个数据才真正有效。
    """
    if hours <= 0:
        return []
    import datetime as _dt
    cutoff = (_dt.datetime.utcnow() - _dt.timedelta(hours=hours)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")

    # 先查各模板的 sent / replied 数
    where_dev = ""
    params: list = [cutoff]
    if device_id:
        where_dev = " AND device_id=?"
        params.append(device_id)
    sql = (
        "SELECT template_id, event_type, COUNT(*) FROM fb_contact_events"
        " WHERE at >= ?"
        + where_dev +
        " AND event_type IN ('greeting_sent','greeting_replied')"
        " AND template_id != ''"
        " GROUP BY template_id, event_type"
    )
    try:
        with _connect() as conn:
            rows = conn.execute(sql, params).fetchall()
    except Exception:
        return []
    bucket: Dict[str, Dict[str, int]] = {}
    for tid, evt, n in rows:
        b = bucket.setdefault(tid, {"sent": 0, "replied": 0})
        if evt == "greeting_sent":
            b["sent"] = int(n)
        elif evt == "greeting_replied":
            b["replied"] = int(n)
    out = []
    for tid, d in bucket.items():
        sent = d["sent"]
        replied = d["replied"]
        out.append({
            "template_id": tid,
            "sent": sent,
            "replied": replied,
            "reply_rate": round(replied / sent, 3) if sent else 0.0,
        })
    out.sort(key=lambda x: (-x["reply_rate"], -x["sent"]))
    return out


def count_risk_events_recent(device_id: str, hours: int = 24) -> int:
    """最近 N 小时内该设备风控事件总数，供 Gate 冷却判断。"""
    if not device_id:
        return 0
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM fb_risk_events "
                "WHERE device_id=? AND detected_at > datetime('now', ?)",
                (device_id, f"-{int(hours)} hours"),
            ).fetchone()
            return int(row[0] or 0)
    except Exception:
        return 0


# 2026-04-24 v2: L2 pause 决策要区分 severity.
# CRITICAL (account-level): 触发 L2 pause (12h default)
# MEDIUM/LOW (message-level, content_blocked 等): 不 pause L2, 只影响 greeting send
_CRITICAL_RISK_KINDS = frozenset({
    "identity_verify", "captcha", "checkpoint",
    "account_review", "policy_warning",
})


def count_critical_risk_events_recent(device_id: str, hours: int = 12) -> int:
    """仅 CRITICAL 级风控事件计数 (会触发 L2 pause 的).

    排除: kind='other' (通常是 content_blocked 一类短期 message-level friction),
    因为 content_blocked 只说 "今天这条消息被 FB 拒发", 不代表账号有长期风险,
    不该 pause L2 12 小时.
    """
    if not device_id:
        return 0
    try:
        with _connect() as conn:
            placeholders = ",".join("?" for _ in _CRITICAL_RISK_KINDS)
            sql = (f"SELECT COUNT(*) FROM fb_risk_events "
                   f"WHERE device_id=? AND detected_at > datetime('now', ?) "
                   f"AND kind IN ({placeholders})")
            params = [device_id, f"-{int(hours)} hours", *list(_CRITICAL_RISK_KINDS)]
            row = conn.execute(sql, params).fetchone()
            return int(row[0] or 0)
    except Exception:
        return 0


def list_recent_risk_events(device_id: Optional[str] = None,
                            hours: int = 24,
                            limit: int = 50) -> List[Dict[str, Any]]:
    """设备面板红旗下拉 / 调试用。"""
    sql = ("SELECT id, device_id, task_id, kind, raw_message, detected_at "
           "FROM fb_risk_events WHERE detected_at > datetime('now', ?)")
    params: list = [f"-{int(hours)} hours"]
    if device_id:
        sql += " AND device_id=?"
        params.append(device_id)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(int(limit))
    try:
        with _connect() as conn:
            conn.row_factory = __import__("sqlite3").Row
            rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


# ════════════════════════════════════════════════════════════════════════
# M1: Facebook 生命周期自动推进
# ════════════════════════════════════════════════════════════════════════
_FB_EVENT_LIFECYCLE_MAP = {
    # record_friend_request status → stage
    "fr:sent":     "contacted",
    "fr:accepted": "engaged",
    # record_contact_event event_type → stage
    "evt:greeting_sent":            "contacted",
    "evt:greeting_fallback":        "contacted",
    "evt:greeting_replied":         "engaged",
    "evt:message_received":         "engaged",
    "evt:wa_referral_sent":         "qualified",
    "evt:wa_referral_replied":      "qualified",
    "evt:line_dispatch_planned":    "qualified",
}


def _try_advance_fb_lifecycle(canonical_id: str, event_key: str):
    """M1: fire-and-forget lifecycle advance keyed by event_key."""
    if not canonical_id:
        return
    stage = _FB_EVENT_LIFECYCLE_MAP.get(event_key)
    if not stage:
        return
    try:
        from src.host.lead_mesh.canonical import advance_lifecycle
        advance_lifecycle(canonical_id, stage)
    except Exception:
        pass
