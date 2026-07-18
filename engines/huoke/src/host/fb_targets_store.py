# -*- coding: utf-8 -*-
"""
fb_targets_store.py — Facebook 日本女性精准获客全局目标管理层

职责:
  - 管理 fb_targets_global / fb_account_health / fb_targets_blocklist / fb_greeting_library
    / fb_outbound_messages 五张表（在 openclaw.db 中追加）
  - 提供 try_claim_target（原子跨设备互斥）、mark_status、release_claim 等核心 API
  - 提供 greeting_library 的读写查询

使用场景:
  - facebook_acquire_from_keyword 任务搜到候选 → try_claim_target → 分类 → mark qualified
  - facebook_jp_female_greet 任务查 friended → 读 insights_json → 写 greeted
  - 多台设备并发，通过 sqlite WAL + busy_timeout 保证原子性
"""

from __future__ import annotations

import json
import logging
import random
import sqlite3
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from src.host.database import get_conn

logger = logging.getLogger(__name__)


def _name_hunter_qualification_evidence(
    *,
    matched: bool,
    score: float,
    insights: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Strict customer gate for point-name acquisition.

    Name seeds and generic L2 matches are not enough for outreach. This helper
    requires explicit persona evidence, especially age 37+. A VLM ``30s`` band
    is kept for manual review because it may include 30-36.
    """
    ins = dict(insights or {})
    reasons: List[str] = []
    gaps: List[str] = []
    profile_score = float(score or ins.get("profile_score") or 0)

    gender = str(ins.get("gender") or "").strip().lower()
    if gender == "female":
        reasons.append("gender=female")
        gender_ok = True
    else:
        gender_ok = False
        gaps.append("gender_not_confirmed_female")

    jp = ins.get("is_japanese")
    jp_conf = float(ins.get("is_japanese_confidence") or 0)
    locale = str(ins.get("locale") or ins.get("language") or "").lower()
    jp_ok = bool(jp is True or jp_conf >= 0.5 or locale.startswith("ja"))
    if jp_ok:
        reasons.append("japanese_context_confirmed")
    else:
        gaps.append("japanese_context_not_confirmed")

    age_band = str(ins.get("age_band") or "").strip().lower()
    explicit_age = None
    for key in ("age", "estimated_age", "age_estimate"):
        try:
            if ins.get(key) is not None:
                explicit_age = int(float(ins.get(key)))
                break
        except Exception:
            continue
    if explicit_age is not None and explicit_age >= 37:
        age_ok = True
        reasons.append(f"explicit_age>={explicit_age}")
    elif age_band in {"40s", "50s", "60s"}:
        age_ok = True
        reasons.append(f"age_band={age_band}")
    else:
        age_ok = False
        if age_band == "30s":
            gaps.append("age_30s_needs_manual_37plus_review")
        else:
            gaps.append("age_37plus_not_confirmed")

    confidence = float(ins.get("overall_confidence") or 0)
    if confidence and confidence < 0.55:
        gaps.append("overall_confidence_low")

    qualified = bool(matched and profile_score >= 70 and gender_ok and jp_ok and age_ok)
    if not matched:
        gaps.append("profile_l2_not_matched")
    if profile_score < 70:
        gaps.append("profile_score_below_70")
    return {
        "qualified": qualified,
        "reasons": reasons,
        "gaps": gaps,
        "age_37plus_confirmed": age_ok,
        "gender_confirmed": gender_ok,
        "japanese_confirmed": jp_ok,
        "profile_score": profile_score,
    }

# ── Schema DDL ─────────────────────────────────────────────────────────────

_FB_TARGETS_SCHEMA = [
    # 全局目标登记表
    """CREATE TABLE IF NOT EXISTS fb_targets_global (
      id            INTEGER PRIMARY KEY AUTOINCREMENT,
      identity_key  TEXT NOT NULL,
      identity_type TEXT NOT NULL DEFAULT 'weak',
      persona_key   TEXT NOT NULL DEFAULT 'jp_female_midlife',
      display_name  TEXT DEFAULT '',
      source_mode   TEXT NOT NULL DEFAULT 'keyword',
      source_ref    TEXT DEFAULT '',
      status        TEXT NOT NULL DEFAULT 'discovered',
      qualified     INTEGER DEFAULT 0,
      insights_json TEXT DEFAULT '{}',
      snapshots_dir TEXT DEFAULT '',
      snapshots_expire_at TEXT DEFAULT '',
      claimed_by    TEXT DEFAULT '',
      claim_expires TEXT DEFAULT '',
      friended_at   TEXT DEFAULT '',
      greeted_at    TEXT DEFAULT '',
      last_touch_by TEXT DEFAULT '',
      last_touch_at TEXT DEFAULT '',
      created_at    TEXT NOT NULL DEFAULT (datetime('now','localtime'))
    )""",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_fb_tg_identity ON fb_targets_global(identity_key, identity_type, persona_key)",
    "CREATE INDEX IF NOT EXISTS idx_fb_tg_status ON fb_targets_global(status)",
    "CREATE INDEX IF NOT EXISTS idx_fb_tg_claim ON fb_targets_global(claimed_by, claim_expires)",
    "CREATE INDEX IF NOT EXISTS idx_fb_tg_persona ON fb_targets_global(persona_key, status)",

    # 账号健康分
    """CREATE TABLE IF NOT EXISTS fb_account_health (
      device_id      TEXT PRIMARY KEY,
      score          INTEGER DEFAULT 100,
      phase          TEXT DEFAULT 'cold_start',
      frozen_until   TEXT DEFAULT '',
      profile_score  INTEGER DEFAULT 0,
      last_event_json TEXT DEFAULT '{}',
      updated_at     TEXT NOT NULL DEFAULT (datetime('now','localtime'))
    )""",

    # 永久屏蔽名单
    """CREATE TABLE IF NOT EXISTS fb_targets_blocklist (
      identity_key  TEXT NOT NULL,
      identity_type TEXT NOT NULL DEFAULT 'weak',
      reason        TEXT DEFAULT 'manual',
      blocked_at    TEXT NOT NULL DEFAULT (datetime('now','localtime')),
      PRIMARY KEY(identity_key, identity_type)
    )""",

    # 打招呼话术库
    """CREATE TABLE IF NOT EXISTS fb_greeting_library (
      id             INTEGER PRIMARY KEY AUTOINCREMENT,
      persona_key    TEXT NOT NULL DEFAULT 'jp_female_midlife',
      text_ja        TEXT NOT NULL,
      reference_layer TEXT DEFAULT 'A',
      style_tag      TEXT DEFAULT 'casual',
      topic_id       TEXT DEFAULT 'general',
      char_count     INTEGER DEFAULT 0,
      sent_count     INTEGER DEFAULT 0,
      replied_count  INTEGER DEFAULT 0,
      reply_rate     REAL DEFAULT 0.0,
      created_at     TEXT NOT NULL DEFAULT (datetime('now','localtime'))
    )""",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_fb_gl_text ON fb_greeting_library(text_ja, persona_key)",
    "CREATE INDEX IF NOT EXISTS idx_fb_gl_persona ON fb_greeting_library(persona_key, reply_rate DESC)",

    # 出站 DM 审计
    """CREATE TABLE IF NOT EXISTS fb_outbound_messages (
      id              INTEGER PRIMARY KEY AUTOINCREMENT,
      target_id       INTEGER DEFAULT 0,
      target_identity TEXT DEFAULT '',
      device_id       TEXT DEFAULT '',
      greeting_id     INTEGER DEFAULT 0,
      prompt_version  TEXT DEFAULT '',
      model           TEXT DEFAULT '',
      generated_text  TEXT DEFAULT '',
      reference_layer TEXT DEFAULT 'A',
      sent_ok         INTEGER DEFAULT 0,
      risk_flags_json TEXT DEFAULT '{}',
      sent_at         TEXT NOT NULL DEFAULT (datetime('now','localtime'))
    )""",
    "CREATE INDEX IF NOT EXISTS idx_fb_om_target ON fb_outbound_messages(target_identity)",
    "CREATE INDEX IF NOT EXISTS idx_fb_om_sent ON fb_outbound_messages(sent_at)",
]


def ensure_schema():
    """确保所有表和索引已创建（幂等）。"""
    with get_conn() as conn:
        for ddl in _FB_TARGETS_SCHEMA:
            try:
                conn.execute(ddl)
            except sqlite3.OperationalError as e:
                # 索引/表已存在等无害错误忽略
                logger.debug("schema DDL 跳过: %s — %s", ddl[:60], e)
        conn.commit()
    logger.debug("fb_targets_store schema 已就绪")


# ── Identity Key 规范化 ─────────────────────────────────────────────────────

def normalize_identity(raw: str) -> Tuple[str, str]:
    """
    将候选标识规范化为 (identity_key, identity_type)。

    优先级:
      1. 纯数字 6-20 位 → fb_user_id
      2. URL 含 id= → fb_user_id（抽取数字）
      3. URL 含 /<username> 路径 → username（slug）
      4. 字母数字/._- 3-50 字符，不含空格，不是纯数字 → username
      5. 其他（含空格/中日文） → weak（display_name hash）
    """
    import hashlib
    import re
    from urllib.parse import urlparse, parse_qs

    s = (raw or "").strip()
    if not s:
        return _weak_key(s), "weak"

    # URL 解析
    if s.startswith(("http://", "https://", "facebook.com/", "m.facebook.com/")):
        if not s.startswith("http"):
            s = "https://" + s
        try:
            u = urlparse(s)
            # profile.php?id=12345
            qs = parse_qs(u.query)
            if "id" in qs:
                uid = qs["id"][0]
                if uid.isdigit():
                    return f"uid:{uid}", "fb_user_id"
            # /<username>
            path = u.path.strip("/")
            if path and "/" not in path and re.fullmatch(r"[A-Za-z0-9._-]{3,50}", path):
                return f"user:{path.lower()}", "username"
            # 规范化 URL 作 url_hash
            clean_url = f"{u.scheme}://{u.netloc}{u.path}".rstrip("/")
            return f"url:{clean_url}", "url_hash"
        except Exception:
            pass

    # 纯数字
    if s.isdigit() and 6 <= len(s) <= 20:
        return f"uid:{s}", "fb_user_id"

    # username slug
    import re as _re
    if _re.fullmatch(r"[A-Za-z0-9._-]{3,50}", s) and not s.isdigit():
        return f"user:{s.lower()}", "username"

    # 弱键（含空格/CJK = display_name）
    return _weak_key(s), "weak"


def _weak_key(name: str) -> str:
    import hashlib
    h = hashlib.md5(name.strip().lower().encode("utf-8")).hexdigest()[:12]
    return f"weak:{h}:{name[:20]}"


# ── 核心操作 ────────────────────────────────────────────────────────────────

def try_claim_target(
    identity_raw: str,
    device_id: str,
    persona_key: str = "jp_female_midlife",
    source_mode: str = "keyword",
    source_ref: str = "",
    display_name: str = "",
    claim_ttl_hours: float = 48.0,
) -> Tuple[bool, int]:
    """
    原子声明一个目标（跨设备互斥）。

    逻辑:
      1. 先检查 blocklist → 如果在黑名单，返回 (False, -1)
      2. INSERT OR IGNORE 创建记录（discovered 状态）
      3. 用一条 UPDATE 竞争 claim:
         claimed_by IS NULL OR claim_expires < now → 设为本机
      4. 立刻查询 claimed_by 是否等于本机

    返回:
      (True, target_id)  → 成功抢占，可以继续操作
      (False, 0)         → 被其他设备抢了（或在黑名单）
      (False, -1)        → 在永久黑名单
    """
    ensure_schema()
    ik, it = normalize_identity(identity_raw if identity_raw else display_name)
    now = datetime.now()
    claim_expires = (now + timedelta(hours=claim_ttl_hours)).strftime("%Y-%m-%d %H:%M:%S")
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")

    with get_conn() as conn:
        # 1. 检查黑名单
        row = conn.execute(
            "SELECT 1 FROM fb_targets_blocklist WHERE identity_key=? AND identity_type=?",
            (ik, it),
        ).fetchone()
        if row:
            logger.info("[try_claim] 黑名单: %s (%s)", ik, device_id)
            return False, -1

        # 2. 尝试插入（IGNORE 如果已存在）
        conn.execute(
            """INSERT OR IGNORE INTO fb_targets_global
               (identity_key, identity_type, persona_key, display_name,
                source_mode, source_ref, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, 'discovered', ?)""",
            (ik, it, persona_key, display_name, source_mode, source_ref, now_str),
        )

        # 3. 原子竞争 claim（只有 claimed_by 为空 OR 已过期才能抢）
        conn.execute(
            """UPDATE fb_targets_global
               SET claimed_by=?, claim_expires=?, last_touch_by=?, last_touch_at=?,
                   status=CASE WHEN status='discovered' THEN 'claimed' ELSE status END
               WHERE identity_key=? AND identity_type=? AND persona_key=?
                 AND (claimed_by='' OR claimed_by IS NULL OR claim_expires < ?)""",
            (device_id, claim_expires, device_id, now_str,
             ik, it, persona_key, now_str),
        )
        conn.commit()

        # 4. 验证是否抢到
        row2 = conn.execute(
            """SELECT id, claimed_by, status FROM fb_targets_global
               WHERE identity_key=? AND identity_type=? AND persona_key=?""",
            (ik, it, persona_key),
        ).fetchone()

    if row2 and row2["claimed_by"] == device_id:
        logger.info("[try_claim] ✅ 抢占成功: %s → device=%s id=%d",
                    ik, device_id, row2["id"])
        return True, row2["id"]
    elif row2:
        logger.info("[try_claim] ❌ 已被抢占: %s → 当前持有者=%s",
                    ik, row2["claimed_by"])
        return False, 0
    return False, 0


def release_claim(target_id: int, device_id: str) -> bool:
    """释放声明（任务失败时回滚）。"""
    ensure_schema()
    with get_conn() as conn:
        conn.execute(
            """UPDATE fb_targets_global
               SET claimed_by='', claim_expires='', status='discovered'
               WHERE id=? AND claimed_by=?""",
            (target_id, device_id),
        )
        conn.commit()
    return True


def _emit_lead_qualified_telemetry(target_id: int, device_id: str) -> None:
    """P4 全域埋点：目标通过 L1+L2 画像分类、qualified 入库
    → zhituo.lead.qualified（fail-silent，只带内部 ID 引用，不带姓名/画像）。"""
    try:
        from src.telemetry import track
        props = {"platform": "facebook",
                 "contact_id": f"fbtgt_{int(target_id)}"}
        if device_id:
            props["account_id"] = device_id
        track("zhituo.lead.qualified", props)
    except Exception:
        pass


def mark_status(
    target_id: int,
    status: str,
    device_id: str = "",
    extra_fields: Optional[Dict[str, Any]] = None,
) -> bool:
    """
    更新目标状态。

    合法状态流:
      discovered → claimed → classifying → qualified / rejected
      → friend_requested → friended / declined / blocked
      → greeted / friended_no_dm
      → replied / opt_out
    """
    ensure_schema()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    sets = ["status=?", "last_touch_at=?"]
    vals: list = [status, now_str]

    if device_id:
        sets.append("last_touch_by=?")
        vals.append(device_id)

    if extra_fields:
        for k, v in extra_fields.items():
            # 白名单字段（防止 SQL 注入）
            if k in {"qualified", "insights_json", "snapshots_dir", "snapshots_expire_at",
                     "friended_at", "greeted_at", "display_name"}:
                sets.append(f"{k}=?")
                vals.append(v if not isinstance(v, dict) else json.dumps(v, ensure_ascii=False))

    vals.append(target_id)
    with get_conn() as conn:
        conn.execute(
            f"UPDATE fb_targets_global SET {', '.join(sets)} WHERE id=?",
            vals,
        )
        conn.commit()
    logger.debug("[mark_status] id=%d → %s", target_id, status)
    if status == "qualified":
        # P4 全域埋点：L2 画像合格入库（获客漏斗"合格线索"节点）
        _emit_lead_qualified_telemetry(target_id, device_id)
    return True


def upsert_name_hunter_candidate(
    *,
    name: str,
    persona_key: str = "jp_female_midlife",
    seed_score: float = 0,
    seed_stage: str = "",
    source_ref: str = "name_hunter",
    status: str = "seeded",
    insights: Optional[Dict[str, Any]] = None,
    device_id: str = "",
) -> int:
    """Create/update a name-hunter candidate in ``fb_targets_global``.

    This is the candidate-pool layer for point-name acquisition. It stores only
    review metadata; actual add/greet still requires the strict L2 gate.
    """
    ensure_schema()
    display_name = (name or "").strip()
    if not display_name:
        return 0
    ik, it = normalize_identity(display_name)
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    meta = dict(insights or {})
    meta.update({
        "seed_score": float(seed_score or 0),
        "seed_stage": seed_stage or "",
        "source": "name_hunter",
        "touch_policy": "high_match_l2_only",
    })
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO fb_targets_global
               (identity_key, identity_type, persona_key, display_name,
                source_mode, source_ref, status, qualified, insights_json,
                last_touch_by, last_touch_at, created_at)
               VALUES (?, ?, ?, ?, 'name_hunter', ?, ?, 0, ?, ?, ?, ?)
               ON CONFLICT(identity_key, identity_type, persona_key) DO UPDATE SET
                 display_name=excluded.display_name,
                 source_mode='name_hunter',
                 source_ref=excluded.source_ref,
                 status=CASE
                   WHEN fb_targets_global.status IN
                        ('friend_requested','friended','greeted','replied','opt_out')
                   THEN fb_targets_global.status
                   ELSE excluded.status END,
                 insights_json=excluded.insights_json,
                 last_touch_by=excluded.last_touch_by,
                 last_touch_at=excluded.last_touch_at""",
            (ik, it, persona_key, display_name, source_ref, status,
             json.dumps(meta, ensure_ascii=False), device_id, now_str, now_str),
        )
        row = conn.execute(
            """SELECT id FROM fb_targets_global
               WHERE identity_key=? AND identity_type=? AND persona_key=?""",
            (ik, it, persona_key),
        ).fetchone()
    return int(row["id"]) if row else 0


def mark_name_hunter_profile_result(
    *,
    name: str,
    persona_key: str,
    matched: bool,
    score: float = 0,
    stage: str = "L2",
    insights: Optional[Dict[str, Any]] = None,
    device_id: str = "",
) -> int:
    """Persist profile classification result for a name-hunter candidate."""
    ensure_schema()
    ik, it = normalize_identity(name)
    existing_meta: Dict[str, Any] = {}
    with get_conn() as conn:
        row = conn.execute(
            """SELECT source_ref, insights_json FROM fb_targets_global
               WHERE identity_key=? AND identity_type=? AND persona_key=?""",
            (ik, it, persona_key),
        ).fetchone()
        if row:
            source_ref = row["source_ref"] or "name_hunter"
            try:
                existing_meta = json.loads(row["insights_json"] or "{}")
            except Exception:
                existing_meta = {}
        else:
            source_ref = "name_hunter"
    merged = {
        **existing_meta,
        **(insights or {}),
        "profile_stage": stage,
        "profile_match": bool(matched),
        "profile_score": float(score or 0),
        "profile_checked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    evidence = _name_hunter_qualification_evidence(
        matched=matched,
        score=score,
        insights=merged,
    )
    merged["qualification_evidence"] = evidence
    if evidence["qualified"]:
        status = "qualified"
    elif matched:
        status = "review_required"
    else:
        status = "rejected"
    target_id = upsert_name_hunter_candidate(
        name=name,
        persona_key=persona_key,
        seed_score=float(merged.get("seed_score") or 0),
        seed_stage=str(merged.get("seed_stage") or ""),
        source_ref=source_ref,
        status=status,
        insights=merged,
        device_id=device_id,
    )
    if target_id:
        mark_status(
            target_id,
            status,
            device_id=device_id,
            extra_fields={
                "qualified": 1 if evidence["qualified"] else 0,
            },
        )
    return target_id


def mark_name_hunter_touched(
    *,
    name: str,
    persona_key: str,
    status: str,
    device_id: str = "",
) -> int:
    """Move a candidate after an outbound action succeeds or is attempted."""
    ensure_schema()
    ik, it = normalize_identity(name)
    existing_meta: Dict[str, Any] = {}
    with get_conn() as conn:
        row = conn.execute(
            """SELECT source_ref, insights_json FROM fb_targets_global
               WHERE identity_key=? AND identity_type=? AND persona_key=?""",
            (ik, it, persona_key),
        ).fetchone()
        if row:
            source_ref = row["source_ref"] or "name_hunter"
            try:
                existing_meta = json.loads(row["insights_json"] or "{}")
            except Exception:
                existing_meta = {}
        else:
            source_ref = "name_hunter"
    target_id = upsert_name_hunter_candidate(
        name=name,
        persona_key=persona_key,
        seed_score=float(existing_meta.get("seed_score") or 0),
        seed_stage=str(existing_meta.get("seed_stage") or ""),
        source_ref=source_ref,
        status=status,
        insights={**existing_meta, "last_outbound_status": status},
        device_id=device_id,
    )
    if target_id:
        extra: Dict[str, Any] = {}
        if status in ("friend_requested", "friended"):
            extra["friended_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if status == "greeted":
            extra["greeted_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        mark_status(target_id, status, device_id=device_id, extra_fields=extra)
    return target_id


def list_name_hunter_candidates(
    *,
    persona_key: str = "jp_female_midlife",
    status: str = "",
    q: str = "",
    min_seed_score: float = 0,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    """List candidate-pool rows for the Facebook point-name workflow."""
    ensure_schema()
    clauses = ["source_mode='name_hunter'", "persona_key=?"]
    params: list = [persona_key]
    if status:
        clauses.append("status=?")
        params.append(status)
    if q:
        clauses.append("display_name LIKE ?")
        params.append(f"%{q}%")
    sql = (
        "SELECT * FROM fb_targets_global WHERE "
        + " AND ".join(clauses)
        + " ORDER BY last_touch_at DESC, id DESC LIMIT ?"
    )
    params.append(max(1, min(int(limit or 100), 500)))
    out: List[Dict[str, Any]] = []
    with get_conn() as conn:
        for row in conn.execute(sql, params).fetchall():
            d = dict(row)
            try:
                d["insights"] = json.loads(d.get("insights_json") or "{}")
            except Exception:
                d["insights"] = {}
            if min_seed_score and float(d["insights"].get("seed_score") or 0) < min_seed_score:
                continue
            out.append(d)
    return out


def name_hunter_stats(*, persona_key: str = "jp_female_midlife") -> Dict[str, Any]:
    """Aggregate point-name candidate funnel by source/name pack."""
    ensure_schema()
    rows: List[Dict[str, Any]] = []
    with get_conn() as conn:
        for row in conn.execute(
            """SELECT source_ref, status, qualified, insights_json
               FROM fb_targets_global
               WHERE source_mode='name_hunter' AND persona_key=?""",
            (persona_key,),
        ).fetchall():
            d = dict(row)
            try:
                d["insights"] = json.loads(d.get("insights_json") or "{}")
            except Exception:
                d["insights"] = {}
            rows.append(d)

    statuses = [
        "seeded", "review_required", "weak_seed", "qualified", "rejected",
        "friend_requested", "friended", "greeted", "replied", "opt_out",
    ]
    totals = {s: 0 for s in statuses}
    sources: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        st = r.get("status") or ""
        if st in totals:
            totals[st] += 1
        src = r.get("source_ref") or r["insights"].get("source") or "name_hunter"
        bucket = sources.setdefault(src, {"source_ref": src, "total": 0, **{s: 0 for s in statuses}})
        bucket["total"] += 1
        if st in statuses:
            bucket[st] += 1
    for b in sources.values():
        total = max(1, int(b.get("total") or 0))
        b["qualified_rate"] = round(float(b.get("qualified", 0)) / total, 3)
        b["reject_rate"] = round(float(b.get("rejected", 0)) / total, 3)
        b["touch_rate"] = round(
            float((b.get("friend_requested", 0) or 0) + (b.get("greeted", 0) or 0)) / total,
            3,
        )
        if b.get("total", 0) >= 5 and b["qualified_rate"] < 0.2:
            b["source_health"] = "degraded"
            b["recommended_action"] = "lower_priority_or_regenerate"
        elif b.get("qualified", 0) >= 3 and b["qualified_rate"] >= 0.5:
            b["source_health"] = "strong"
            b["recommended_action"] = "scale"
        else:
            b["source_health"] = "learning"
            b["recommended_action"] = "collect_more_prescreen"
    return {
        "total": len(rows),
        "statuses": totals,
        "sources": sorted(sources.values(), key=lambda x: (-x.get("qualified_rate", 0), -x.get("total", 0))),
    }


def name_hunter_source_quality(
    *,
    persona_key: str = "jp_female_midlife",
    source_ref: str = "name_hunter",
) -> Dict[str, Any]:
    """Return quality metadata for a name-pack/source."""
    stats = name_hunter_stats(persona_key=persona_key)
    for src in stats.get("sources") or []:
        if src.get("source_ref") == source_ref:
            return src
    return {
        "source_ref": source_ref,
        "total": 0,
        "qualified": 0,
        "qualified_rate": 0.0,
        "source_health": "new",
        "recommended_action": "collect_more_prescreen",
    }


def name_hunter_touch_targets(
    *,
    persona_key: str = "jp_female_midlife",
    limit: int = 20,
) -> List[Dict[str, Any]]:
    """Return qualified candidates that have not been touched yet."""
    ensure_schema()
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT * FROM fb_targets_global
               WHERE source_mode='name_hunter'
                 AND persona_key=?
                 AND status='qualified'
                 AND qualified=1
               ORDER BY last_touch_at ASC, id ASC
               LIMIT ?""",
            (persona_key, max(1, min(int(limit or 20), 200))),
        ).fetchall()
    out: List[Dict[str, Any]] = []
    for row in rows:
        d = dict(row)
        try:
            d["insights"] = json.loads(d.get("insights_json") or "{}")
        except Exception:
            d["insights"] = {}
        out.append(d)
    return out


def get_target(target_id: int) -> Optional[Dict[str, Any]]:
    """按 ID 查询目标。"""
    ensure_schema()
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM fb_targets_global WHERE id=?", (target_id,)
        ).fetchone()
    if row:
        d = dict(row)
        if d.get("insights_json"):
            try:
                d["insights"] = json.loads(d["insights_json"])
            except Exception:
                d["insights"] = {}
        return d
    return None


def list_greet_queue(
    persona_key: str = "jp_female_midlife",
    min_delay_hours: float = 36.0,
    max_delay_hours: float = 72.0,
    limit: int = 10,
    device_health_min: int = 70,
) -> List[Dict[str, Any]]:
    """
    返回待打招呼队列:
      status=friended AND friended_at < now - uniform(36,72)h AND greeted_at IS NULL/空
    注意: uniform 在 SQL 里用平均值 54h 做截止，业务层再做随机过滤。
    """
    ensure_schema()
    cutoff_h = (min_delay_hours + max_delay_hours) / 2  # SQL 里用均值保守截止
    cutoff_dt = (datetime.now() - timedelta(hours=cutoff_h)).strftime("%Y-%m-%d %H:%M:%S")
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT * FROM fb_targets_global
               WHERE persona_key=?
                 AND status='friended'
                 AND (greeted_at='' OR greeted_at IS NULL)
                 AND friended_at != '' AND friended_at IS NOT NULL
                 AND friended_at < ?
               ORDER BY friended_at ASC
               LIMIT ?""",
            (persona_key, cutoff_dt, limit),
        ).fetchall()
    results = []
    for row in rows:
        d = dict(row)
        if d.get("insights_json"):
            try:
                d["insights"] = json.loads(d["insights_json"])
            except Exception:
                d["insights"] = {}
        results.append(d)
    return results


def add_to_blocklist(identity_raw: str, reason: str = "manual") -> bool:
    """将目标加入永久黑名单。"""
    ensure_schema()
    ik, it = normalize_identity(identity_raw)
    with get_conn() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO fb_targets_blocklist
               (identity_key, identity_type, reason, blocked_at)
               VALUES (?, ?, ?, datetime('now','localtime'))""",
            (ik, it, reason),
        )
        # 同时把 fb_targets_global 里的同一目标标记 opt_out
        conn.execute(
            """UPDATE fb_targets_global SET status='opt_out', last_touch_at=datetime('now','localtime')
               WHERE identity_key=? AND identity_type=?""",
            (ik, it),
        )
        conn.commit()
    return True


# ── Greeting Library ────────────────────────────────────────────────────────

def import_greeting_library(greetings: List[Dict[str, Any]], persona_key: str = "jp_female_midlife") -> int:
    """
    批量导入打招呼话术（从 w0_greeting_library.json 导入）。
    返回实际写入数量。
    """
    ensure_schema()
    count = 0
    with get_conn() as conn:
        for g in greetings:
            text = (g.get("text_ja") or "").strip()
            if not text:
                continue
            try:
                conn.execute(
                    """INSERT OR IGNORE INTO fb_greeting_library
                       (persona_key, text_ja, reference_layer, style_tag, topic_id, char_count)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        persona_key,
                        text,
                        g.get("reference_layer", "A"),
                        g.get("style_tag", "casual"),
                        g.get("topic_id", "general"),
                        len(text),
                    ),
                )
                count += 1
            except sqlite3.IntegrityError:
                pass
        conn.commit()
    logger.info("导入话术 %d 条", count)
    return count


def pick_greeting(
    persona_key: str = "jp_female_midlife",
    style_tag: Optional[str] = None,
    topic_id: Optional[str] = None,
    exclude_ids: Optional[List[int]] = None,
) -> Optional[Dict[str, Any]]:
    """
    从话术库随机取一条（回复率高的优先，随机避免重复）。

    使用 weighted random:
      - 回复率 > 0 的按 reply_rate 权重
      - 回复率 = 0 的平等参与（新话术需要曝光）
    """
    ensure_schema()
    where = ["persona_key=?"]
    params: list = [persona_key]

    if style_tag:
        where.append("style_tag=?")
        params.append(style_tag)
    if topic_id:
        where.append("topic_id=?")
        params.append(topic_id)
    if exclude_ids:
        placeholders = ",".join("?" * len(exclude_ids))
        where.append(f"id NOT IN ({placeholders})")
        params.extend(exclude_ids)

    where_sql = " AND ".join(where)
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT * FROM fb_greeting_library WHERE {where_sql} ORDER BY RANDOM() LIMIT 20",
            params,
        ).fetchall()

    if not rows:
        return None

    # Weighted pick（优先曝光过且有回复的）
    dicts = [dict(r) for r in rows]
    weights = [max(0.1, float(d.get("reply_rate") or 0) * 10 + 1) for d in dicts]
    total = sum(weights)
    r = random.uniform(0, total)
    cumul = 0.0
    for d, w in zip(dicts, weights):
        cumul += w
        if r <= cumul:
            return d
    return dicts[-1]


def register_greeting_text(text: str, persona_key: str = "jp_female_midlife",
                           field: str = "greeting",
                           style_tag: str = "ai_gen") -> Optional[int]:
    """将一条实际使用的文案注册入话术库（INSERT OR IGNORE），返回 id。

    在 fb_device_launch 调用，确保每条真正被发出的话术都有追踪。
    field: 'greeting' | 'verification_note'
    """
    if not text or not text.strip():
        return None
    ensure_schema()
    topic = "verification" if field == "verification_note" else "dm_greeting"
    with get_conn() as conn:
        conn.execute(
            """INSERT OR IGNORE INTO fb_greeting_library
               (persona_key, text_ja, reference_layer, style_tag, topic_id, char_count)
               VALUES (?, ?, 'A', ?, ?, ?)""",
            (persona_key, text.strip(), style_tag, topic, len(text.strip())),
        )
        conn.commit()
        row = conn.execute(
            "SELECT id FROM fb_greeting_library WHERE text_ja=? AND persona_key=?",
            (text.strip(), persona_key),
        ).fetchone()
    return row[0] if row else None


def record_greeting_sent(greeting_id: int, replied: bool = False) -> bool:
    """更新话术的发送/回复计数。"""
    ensure_schema()
    with get_conn() as conn:
        if replied:
            conn.execute(
                """UPDATE fb_greeting_library
                   SET sent_count=sent_count+1, replied_count=replied_count+1,
                       reply_rate=CAST(replied_count+1 AS REAL)/CAST(sent_count+1 AS REAL)
                   WHERE id=?""",
                (greeting_id,),
            )
        else:
            conn.execute(
                """UPDATE fb_greeting_library
                   SET sent_count=sent_count+1,
                       reply_rate=CAST(replied_count AS REAL)/CAST(sent_count+1 AS REAL)
                   WHERE id=?""",
                (greeting_id,),
            )
        conn.commit()
    return True


# ── Account Health ──────────────────────────────────────────────────────────

def update_account_health(
    device_id: str,
    event: str,  # friend_request_rejected|dm_no_reply_7d|captcha|fb_limit|report
    delta: int = 0,
) -> Dict[str, Any]:
    """
    更新账号健康分。
    event → 自动扣分规则（见开发文档）。
    """
    ensure_schema()
    EVENT_DELTAS = {
        "friend_request_rejected": -2,
        "friend_request_cancelled": -3,
        "dm_no_reply_7d": -1,
        "captcha": -10,
        "fb_limit": -40,
        "report_suspected": -20,
        "daily_recover": +5,  # 每天恢复
    }
    auto_delta = EVENT_DELTAS.get(event, delta)
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with get_conn() as conn:
        # 读取旧阶段（用于变更检测）
        old_row = conn.execute(
            "SELECT phase FROM fb_account_health WHERE device_id=?", (device_id,)
        ).fetchone()
        old_phase = dict(old_row)["phase"] if old_row else "cold_start"

        conn.execute(
            """INSERT INTO fb_account_health (device_id, score, phase, updated_at)
               VALUES (?, 100, 'cold_start', ?)
               ON CONFLICT(device_id) DO UPDATE SET
                 score = MAX(0, MIN(100, score + ?)),
                 last_event_json = ?,
                 updated_at = ?""",
            (device_id, now_str, auto_delta,
             json.dumps({"event": event, "delta": auto_delta, "at": now_str}),
             now_str),
        )
        # 阶段自动更新
        conn.execute(
            """UPDATE fb_account_health SET phase=
               CASE WHEN score >= 80 THEN 'mature'
                    WHEN score >= 60 THEN 'growth'
                    WHEN score >= 40 THEN 'cold_start'
                    ELSE 'cooldown' END,
               frozen_until = CASE WHEN score < 40 THEN datetime('now','+7 days','localtime') ELSE '' END
               WHERE device_id=?""",
            (device_id,),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM fb_account_health WHERE device_id=?", (device_id,)
        ).fetchone()

    result = dict(row) if row else {}
    new_phase = result.get("phase", "cold_start")
    # 阶段变更事件推送
    if new_phase != old_phase:
        result["phase_changed"] = True
        result["old_phase"] = old_phase
        try:
            from src.host.event_stream import push_event
            push_event("fb.phase_changed", {
                "device_id": device_id,
                "old_phase": old_phase,
                "new_phase": new_phase,
                "score": result.get("score", 0),
                "event": event,
            }, device_id=device_id)
        except Exception:
            pass
    return result


def get_account_health(device_id: str) -> Dict[str, Any]:
    """查询账号健康状态。"""
    ensure_schema()
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM fb_account_health WHERE device_id=?", (device_id,)
        ).fetchone()
    return dict(row) if row else {"device_id": device_id, "score": 100, "phase": "cold_start"}


# ── 统计查询 ────────────────────────────────────────────────────────────────

def get_funnel_stats(persona_key: str = "jp_female_midlife") -> Dict[str, int]:
    """返回漏斗各阶段计数（用于前端看板）。"""
    ensure_schema()
    statuses = [
        "discovered", "claimed", "classifying", "qualified", "rejected",
        "friend_requested", "friended", "friended_no_dm",
        "greeted", "replied", "declined", "blocked", "opt_out",
    ]
    stats = {}
    with get_conn() as conn:
        for s in statuses:
            row = conn.execute(
                "SELECT COUNT(*) FROM fb_targets_global WHERE persona_key=? AND status=?",
                (persona_key, s),
            ).fetchone()
            stats[s] = row[0] if row else 0
    return stats


def get_greeting_stats(persona_key: str = "jp_female_midlife") -> Dict[str, Any]:
    """话术库统计（总数、平均回复率、最佳话术）。"""
    ensure_schema()
    with get_conn() as conn:
        total = conn.execute(
            "SELECT COUNT(*) FROM fb_greeting_library WHERE persona_key=?", (persona_key,)
        ).fetchone()[0]
        avg_rate = conn.execute(
            "SELECT AVG(reply_rate) FROM fb_greeting_library WHERE persona_key=? AND sent_count > 0",
            (persona_key,),
        ).fetchone()[0]
        best = conn.execute(
            """SELECT id, text_ja, reply_rate, sent_count
               FROM fb_greeting_library
               WHERE persona_key=? AND sent_count >= 3
               ORDER BY reply_rate DESC LIMIT 5""",
            (persona_key,),
        ).fetchall()
    return {
        "total": total,
        "avg_reply_rate": round(float(avg_rate or 0), 3),
        "best_greetings": [dict(r) for r in best],
    }
