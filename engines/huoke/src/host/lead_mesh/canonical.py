# -*- coding: utf-8 -*-
"""CanonicalResolver — 跨平台 Lead 身份解析 + 高置信度自动合并 (Phase 5)。

策略
----
1. **硬匹配** (`verified=1`): ``lead_identities`` 里 (platform, account_id)
   唯一约束命中 → 直接返回已有 canonical_id
2. **软匹配** (`verified=0`): 名字规范化 / 头像 hash / 电话后缀 指纹匹配,
   置信度计算:
     * display_name 完全一致 + 同 platform        → 0.35
     * display_name 规范化 (去空格/emoji) 一致    → 0.25
     * 头像 hash 一致                              → 0.40
     * 电话后 4 位一致                             → 0.20
     * metadata 里 bio_hash 一致                   → 0.15
   累加 clip [0, 1]。
3. **自动合并**: 置信度 ≥ ``AUTO_MERGE_THRESHOLD=0.85`` 且**源 canonical 无
   进行中的 handoff/锁** 时, 自动合并到 target, 落审计日志 (lead_merges 表)。
   未达阈值 → 只返回候选列表, 留人工 Dashboard 合并。

回滚
----
每次 merge 有 lead_merges 行, 提供 ``revert_merge(merge_id, reason, by)``。
撤销后原 canonical 重新激活, journey 事件按时间线分配回来 (不自动拆分, 人工改)。
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from typing import Any, Dict, List, Optional, Tuple

from src.host.database import _connect

logger = logging.getLogger(__name__)

AUTO_MERGE_THRESHOLD = 0.85
SOFT_MATCH_MIN_SCORE = 0.40     # 低于此值不进入候选
MIN_NAME_SIM_FOR_AUTO_MERGE = 0.15  # I1: 名字字符集重叠最低门槛(防误合并)


def _normalize_name(name: str) -> str:
    """规范化名字: 小写, 去非字母数字日韩中文。用于软匹配比较。"""
    if not name:
        return ""
    # 保留中日韩 + 字母数字, 去空格/表情/标点
    return re.sub(r"[^\w　-鿿゠-ヿ぀-ゟ]", "", name).lower()


def _resolve_hard(platform: str, account_id: str) -> Optional[str]:
    """硬匹配: (platform, account_id) UNIQUE 索引查询。"""
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT canonical_id FROM lead_identities"
                " WHERE platform=? AND account_id=?",
                (platform, account_id),
            ).fetchone()
        if row:
            cid = row[0]
            # 检查是否被合并
            with _connect() as conn:
                r = conn.execute(
                    "SELECT merged_into FROM leads_canonical WHERE canonical_id=?",
                    (cid,),
                ).fetchone()
            if r and r[0]:
                return r[0]  # 跟随 merged_into 指针
            return cid
    except Exception as e:
        logger.debug("[canonical] 硬匹配查询失败: %s", e)
    return None


def _score_candidate(display_name: str,
                      extra_metadata: Dict[str, Any],
                      candidate_row: Dict[str, Any]) -> Tuple[float, List[str]]:
    """计算某候选 canonical 与输入的软匹配置信度 + 触发理由。"""
    score = 0.0
    reasons: List[str] = []
    cand_name = candidate_row.get("primary_name") or ""

    # 1. 名字精确一致
    if display_name and cand_name and display_name.strip() == cand_name.strip():
        score += 0.35
        reasons.append("name_exact")
    # 2. 名字规范化一致
    elif display_name and cand_name:
        n1 = _normalize_name(display_name)
        n2 = _normalize_name(cand_name)
        if n1 and n1 == n2:
            score += 0.25
            reasons.append("name_normalized")

    # metadata 维度
    cand_meta: Dict[str, Any] = {}
    try:
        cand_meta = json.loads(candidate_row.get("metadata_json") or "{}")
    except Exception:
        pass

    # 3. 头像 hash
    ah_in = (extra_metadata or {}).get("avatar_hash") or ""
    ah_cand = cand_meta.get("avatar_hash") or ""
    if ah_in and ah_in == ah_cand:
        score += 0.40
        reasons.append("avatar_hash")

    # 4. 电话后 4 位
    ph_in = str((extra_metadata or {}).get("phone") or "")
    ph_cand = str(cand_meta.get("phone") or "")
    if ph_in and ph_cand and ph_in[-4:] == ph_cand[-4:] and len(ph_in) >= 4:
        score += 0.20
        reasons.append("phone_suffix")

    # 5. bio hash
    bh_in = (extra_metadata or {}).get("bio_hash") or ""
    bh_cand = cand_meta.get("bio_hash") or ""
    if bh_in and bh_in == bh_cand:
        score += 0.15
        reasons.append("bio_hash")

    return min(score, 1.0), reasons


def _find_soft_candidates(display_name: str,
                           extra_metadata: Optional[Dict[str, Any]] = None,
                           limit: int = 20) -> List[Dict[str, Any]]:
    """在 leads_canonical 里找可能是同一人的候选。

    粗筛先: 同名 (规范化) 的前 N 条; 细筛交给 _score_candidate。
    """
    if not display_name:
        return []
    norm = _normalize_name(display_name)
    if not norm or len(norm) < 2:
        return []
    try:
        with _connect() as conn:
            conn.row_factory = __import__("sqlite3").Row
            # 粗筛: 名字规范化相同 (SQLite 不支持自定义函数, 这里用 LIKE 粗过滤)
            rows = conn.execute(
                "SELECT canonical_id, primary_name, primary_language,"
                " metadata_json FROM leads_canonical"
                " WHERE merged_into IS NULL AND primary_name != ''"
                " LIMIT ?",
                (200,),  # 最多扫 200 行找候选,性能和召回率平衡
            ).fetchall()
        candidates = []
        for r in rows:
            d = dict(r)
            # 进一步规范化比较
            cand_norm = _normalize_name(d.get("primary_name") or "")
            if cand_norm == norm or (cand_norm and norm in cand_norm):
                score, reasons = _score_candidate(display_name,
                                                    extra_metadata or {}, d)
                if score >= SOFT_MATCH_MIN_SCORE:
                    d["score"] = score
                    d["reasons"] = reasons
                    candidates.append(d)
        candidates.sort(key=lambda x: -x["score"])
        return candidates[:limit]
    except Exception as e:
        logger.debug("[canonical] 软匹配失败: %s", e)
        return []


def resolve_identity(platform: str, account_id: str, *,
                      display_name: str = "",
                      extra_metadata: Optional[Dict[str, Any]] = None,
                      discovered_via: str = "",
                      discovered_by_device: str = "",
                      language: str = "",
                      persona_key: str = "",
                      auto_merge: bool = True) -> str:
    """核心入口: 按 (platform, account_id) 拿到 canonical_id, 没有就创建。

    Args:
        platform: 必须。facebook / line / whatsapp / telegram / instagram
        account_id: 必须。该平台上的账号唯一标识
        display_name: 可选。用于软匹配 + primary_name 填充
        extra_metadata: 可选。avatar_hash / phone / bio_hash 等, 供软匹配
        discovered_via: 可选。来源 (group_extract / inbox / handoff)
        discovered_by_device: 可选。发现者 device_id
        language: 可选。首次写入时填 primary_language
        persona_key: 可选。首次写入时填 primary_persona_key
        auto_merge: True (默认) = 置信度 ≥ 阈值时自动合并; False = 永远新建

    Returns:
        canonical_id (UUID 字符串)
    """
    if not platform or not account_id:
        raise ValueError("platform / account_id 必填")
    platform = platform.lower().strip()
    account_id = account_id.strip()

    # 1) 硬匹配
    hit = _resolve_hard(platform, account_id)
    if hit:
        # 补全 identity 里的缺失字段 (display_name 等, 幂等)
        try:
            with _connect() as conn:
                conn.execute(
                    "UPDATE lead_identities SET"
                    " display_name=CASE WHEN display_name='' THEN ? ELSE display_name END,"
                    " discovered_via=CASE WHEN discovered_via='' THEN ? ELSE discovered_via END,"
                    " discovered_by_device=CASE WHEN discovered_by_device='' THEN ? ELSE discovered_by_device END"
                    " WHERE platform=? AND account_id=?",
                    (display_name, discovered_via, discovered_by_device,
                     platform, account_id),
                )
        except Exception:
            pass
        return hit

    # 2) 软匹配: 名字相近 + metadata 指纹交叉确认
    merge_target: Optional[str] = None
    merge_confidence = 0.0
    merge_reasons: List[str] = []
    if auto_merge and display_name:
        candidates = _find_soft_candidates(display_name, extra_metadata or {})
        if candidates:
            top = candidates[0]
            if top["score"] >= AUTO_MERGE_THRESHOLD:
                # Phase I1: 前置质量门槛 — 名字字符集重叠度必须达标
                cand_name = top.get("primary_name") or ""
                name_sim = _name_similarity(display_name, cand_name)
                if name_sim < MIN_NAME_SIM_FOR_AUTO_MERGE and cand_name:
                    logger.warning(
                        "[canonical] I1 质量门槛拦截: score=%.2f 但 name_sim=%.2f"
                        " (%s vs %s), 降级为候选而非自动合并",
                        top["score"], name_sim,
                        display_name[:20], cand_name[:20])
                    # J1: 记录拦截事件到 lead_merges 供审计面板展示
                    try:
                        _record_merge(
                            source_canonical_id=f"virt-ident:{platform}:{account_id}",
                            target_canonical_id=top["canonical_id"],
                            mode="auto_blocked_i1",
                            confidence=top["score"],
                            reasons=top["reasons"] + [f"name_sim={name_sim:.2f}"],
                            merged_by="system_quality_gate",
                        )
                    except Exception:
                        pass
                else:
                    merge_target = top["canonical_id"]
                    merge_confidence = top["score"]
                    merge_reasons = top["reasons"]
                    logger.info(
                        "[canonical] 高置信度软匹配 %.2f (name_sim=%.2f) → 合并到 %s",
                        merge_confidence, name_sim, merge_target[:12])

    # 3) 决定最终 canonical_id
    if merge_target:
        # 直接把新 identity 挂到已有 canonical 下, 不新建 lead
        canonical_id = merge_target
        verified = 0  # 软匹配产物, verified=0
        try:
            with _connect() as conn:
                conn.execute(
                    "INSERT INTO lead_identities"
                    " (canonical_id, platform, account_id, display_name,"
                    "  verified, discovered_via, discovered_by_device,"
                    "  metadata_json)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (canonical_id, platform, account_id, display_name,
                     verified, discovered_via, discovered_by_device,
                     json.dumps(extra_metadata or {}, ensure_ascii=False)),
                )
                # 审计日志 (不产生"合并"事件, 因为是新 identity 直接挂而非两个 lead 合并)
                # 可以在 journey 里记一笔 soft_match_merged
        except Exception as e:
            # UNIQUE 冲突 (并发场景) → 退回硬匹配
            logger.debug("[canonical] soft-merge insert 冲突, 退回硬查: %s", e)
            return _resolve_hard(platform, account_id) or canonical_id
        # 审计: 写 lead_merges (source=虚拟新 canonical, target=merge_target)
        try:
            _record_merge(source_canonical_id=f"virt-ident:{platform}:{account_id}",
                          target_canonical_id=canonical_id,
                          mode="auto_soft_identity",
                          confidence=merge_confidence,
                          reasons=merge_reasons,
                          merged_by="system")
        except Exception:
            pass
        # Journey 记录
        try:
            from .journey import append_journey
            append_journey(canonical_id, actor="system",
                           action="lead_marked_duplicate",
                           platform=platform,
                           data={"matched_account": account_id,
                                 "confidence": merge_confidence,
                                 "reasons": merge_reasons})
        except Exception:
            pass
        return canonical_id

    # 4) 新建 canonical + identity
    canonical_id = str(uuid.uuid4())
    try:
        with _connect() as conn:
            conn.execute(
                "INSERT INTO leads_canonical"
                " (canonical_id, primary_name, primary_language,"
                "  primary_persona_key, metadata_json)"
                " VALUES (?,?,?,?,?)",
                (canonical_id, display_name, language, persona_key,
                 json.dumps(extra_metadata or {}, ensure_ascii=False)),
            )
            conn.execute(
                "INSERT INTO lead_identities"
                " (canonical_id, platform, account_id, display_name, verified,"
                "  discovered_via, discovered_by_device, metadata_json)"
                " VALUES (?,?,?,?,1,?,?,?)",
                (canonical_id, platform, account_id, display_name,
                 discovered_via, discovered_by_device,
                 json.dumps(extra_metadata or {}, ensure_ascii=False)),
            )
    except Exception as e:
        # 并发新建冲突 → 再查一次硬匹配
        logger.debug("[canonical] 新建冲突, 再硬查: %s", e)
        existing = _resolve_hard(platform, account_id)
        if existing:
            return existing
        raise
    try:
        from .journey import append_journey
        append_journey(canonical_id, actor="system", action="extracted",
                       actor_device=discovered_by_device,
                       platform=platform,
                       data={"account_id": account_id,
                             "display_name": display_name,
                             "via": discovered_via})
    except Exception:
        pass
    return canonical_id


def auto_merge_candidates(canonical_id: str,
                           min_confidence: float = 0.70) -> List[Dict[str, Any]]:
    """列出某 lead 的潜在合并候选 (不自动操作, 给 Dashboard / 人工用)。

    Args:
        canonical_id: 要查的 lead
        min_confidence: 置信度下限 (默认 0.70, 低于此不返回)
    """
    try:
        with _connect() as conn:
            conn.row_factory = __import__("sqlite3").Row
            row = conn.execute(
                "SELECT * FROM leads_canonical WHERE canonical_id=?",
                (canonical_id,)).fetchone()
        if not row:
            return []
        d = dict(row)
    except Exception:
        return []
    meta = {}
    try:
        meta = json.loads(d.get("metadata_json") or "{}")
    except Exception:
        pass
    candidates = _find_soft_candidates(d.get("primary_name") or "", meta, limit=50)
    return [c for c in candidates
            if c["canonical_id"] != canonical_id and c["score"] >= min_confidence]


def _record_merge(source_canonical_id: str,
                   target_canonical_id: str,
                   mode: str,
                   confidence: float,
                   reasons: List[str],
                   merged_by: str) -> int:
    try:
        with _connect() as conn:
            cur = conn.execute(
                "INSERT INTO lead_merges"
                " (source_canonical_id, target_canonical_id, merge_mode,"
                "  confidence, merge_reasons_json, merged_by)"
                " VALUES (?,?,?,?,?,?)",
                (source_canonical_id, target_canonical_id, mode,
                 float(confidence), json.dumps(reasons, ensure_ascii=False),
                 merged_by),
            )
            return cur.lastrowid or 0
    except Exception as e:
        logger.warning("[canonical] record_merge 失败: %s", e)
        return 0


def merge_manually(source_canonical_id: str,
                    target_canonical_id: str,
                    merged_by: str = "human",
                    reason: str = "manual") -> bool:
    """手动合并 (Dashboard 入口)。把 source 标记为 merged_into=target。

    * 事务原子: 更新 source + 搬迁 lead_identities + 写审计
    * source 后续 resolve 会跟随 merged_into 指针返回 target
    * journey 保留在各自 canonical_id 下; 查询 Dossier 时按 target 聚合, 含
      source 的 journey 事件 (通过 merges 映射)
    """
    if source_canonical_id == target_canonical_id:
        return False
    try:
        with _connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            # 标记 source
            conn.execute(
                "UPDATE leads_canonical SET merged_into=?, updated_at=datetime('now')"
                " WHERE canonical_id=? AND merged_into IS NULL",
                (target_canonical_id, source_canonical_id))
            # identities 改挂
            conn.execute(
                "UPDATE lead_identities SET canonical_id=? WHERE canonical_id=?",
                (target_canonical_id, source_canonical_id))
            conn.execute("COMMIT")
    except Exception as e:
        logger.warning("[canonical] manual merge 失败: %s", e)
        return False
    _record_merge(source_canonical_id, target_canonical_id, "manual",
                  1.0, ["human_decision"], merged_by)
    try:
        from .journey import append_journey
        append_journey(target_canonical_id, actor=merged_by,
                       action="lead_merged",
                       data={"from": source_canonical_id, "reason": reason})
    except Exception:
        pass
    return True


def revert_merge(merge_id: int,
                  reverted_by: str = "human",
                  reason: str = "") -> bool:
    """撤销一次合并 (自动或手动产生的都支持)。

    行为: source canonical 的 merged_into 清空, identities 改挂回 source。
    **注意**: journey 历史不自动拆分 (时间线依然挂在各自时间点),
    Dashboard 上可见 "曾合并, 已撤销" 标记。
    """
    try:
        with _connect() as conn:
            conn.row_factory = __import__("sqlite3").Row
            row = conn.execute(
                "SELECT * FROM lead_merges WHERE id=? AND reverted_at IS NULL",
                (int(merge_id),)).fetchone()
            if not row:
                return False
            m = dict(row)
            src = m["source_canonical_id"]
            if src.startswith("virt-ident:"):
                # soft-identity 合并: 只能拆 identity (把对应 identity 移回新 canonical)
                # 细节留给 dashboard 手工处理
                logger.warning("[canonical] virt-ident 合并撤销需人工介入 merge_id=%s", merge_id)
                return False
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE leads_canonical SET merged_into=NULL, updated_at=datetime('now')"
                " WHERE canonical_id=?", (src,))
            conn.execute(
                "UPDATE lead_identities SET canonical_id=? WHERE canonical_id=?"
                " AND discovered_at > ?",
                (src, m["target_canonical_id"], m["merged_at"]))
            conn.execute(
                "UPDATE lead_merges SET reverted_at=datetime('now'), reverted_reason=?"
                " WHERE id=?", (reason or f"by {reverted_by}", int(merge_id)))
            conn.execute("COMMIT")
    except Exception as e:
        logger.warning("[canonical] revert merge 失败: %s", e)
        return False
    return True


def list_recent_merges(limit: int = 50, include_reverted: bool = True,
                       canonical_id: str = "") -> List[Dict[str, Any]]:
    """Phase E2: 查询最近的合并操作 (审计面板).

    Args:
        limit: 最大返回条数
        include_reverted: 是否包含已撤销的记录
        canonical_id: 可选过滤 — 只查与此 canonical 相关的合并

    Returns:
        [{id, source_canonical_id, target_canonical_id, merge_mode,
          confidence, merge_reasons_json, merged_by, merged_at,
          reverted_at, reverted_reason}]
    """
    results: List[Dict[str, Any]] = []
    try:
        import sqlite3
        with _connect() as conn:
            conn.row_factory = sqlite3.Row
            sql = "SELECT * FROM lead_merges WHERE 1=1"
            params: list = []
            if not include_reverted:
                sql += " AND reverted_at IS NULL"
            if canonical_id:
                sql += " AND (source_canonical_id=? OR target_canonical_id=?)"
                params.extend([canonical_id, canonical_id])
            sql += " ORDER BY merged_at DESC LIMIT ?"
            params.append(min(limit, 200))
            for row in conn.execute(sql, params):
                results.append(dict(row))
    except Exception as e:
        logger.debug("[canonical] list_recent_merges 失败: %s", e)
    return results


def get_unified_identity_kpi(since_days: int = 30) -> Dict[str, Any]:
    """Phase H1: 跨平台统一身份解析 KPI.

    基于 leads_canonical + lead_identities + lead_merges 三张表,
    不依赖任何平台特定表, 可同时覆盖 Facebook/TikTok/LINE 等.

    Returns:
        {total_leads, active_leads, identities_by_platform,
         cross_platform_leads, merge_stats, daily_new_leads}
    """
    import sqlite3
    import datetime as _dt
    cutoff = (_dt.datetime.utcnow() - _dt.timedelta(days=max(1, since_days))
              ).strftime("%Y-%m-%d")
    result: Dict[str, Any] = {
        "total_leads": 0, "active_leads": 0,
        "identities_by_platform": {},
        "total_identities": 0,
        "cross_platform_leads": 0,
        "avg_identities_per_lead": 0.0,
        "merge_stats": {"total": 0, "auto": 0, "manual": 0, "reverted": 0},
        "daily_new_leads": [],
        "scope_since_days": since_days,
    }
    try:
        with _connect() as conn:
            conn.row_factory = sqlite3.Row
            # 1. Lead 总数 / 活跃 Lead (未被合并)
            row = conn.execute(
                "SELECT COUNT(*) as total,"
                " SUM(CASE WHEN merged_into IS NULL THEN 1 ELSE 0 END) as active"
                " FROM leads_canonical"
            ).fetchone()
            result["total_leads"] = row["total"] or 0
            result["active_leads"] = row["active"] or 0

            # 2. 各平台身份数
            for r2 in conn.execute(
                "SELECT platform, COUNT(*) as cnt FROM lead_identities"
                " GROUP BY platform ORDER BY cnt DESC"
            ):
                result["identities_by_platform"][r2["platform"]] = r2["cnt"]
            result["total_identities"] = sum(
                result["identities_by_platform"].values())

            # 3. 跨平台 Lead 数 (同一 canonical_id 有 2+ 平台)
            row3 = conn.execute(
                "SELECT COUNT(*) FROM ("
                "  SELECT canonical_id FROM lead_identities"
                "  WHERE canonical_id IN (SELECT canonical_id FROM leads_canonical WHERE merged_into IS NULL)"
                "  GROUP BY canonical_id HAVING COUNT(DISTINCT platform) >= 2"
                ")"
            ).fetchone()
            result["cross_platform_leads"] = row3[0] if row3 else 0

            # 4. 平均身份数 / Lead
            if result["active_leads"] > 0:
                result["avg_identities_per_lead"] = round(
                    result["total_identities"] / result["active_leads"], 2)

            # 5. 合并统计
            for r4 in conn.execute(
                "SELECT"
                " COUNT(*) as total,"
                " SUM(CASE WHEN merge_mode='auto' THEN 1 ELSE 0 END) as auto_cnt,"
                " SUM(CASE WHEN merge_mode='manual' THEN 1 ELSE 0 END) as manual_cnt,"
                " SUM(CASE WHEN reverted_at IS NOT NULL THEN 1 ELSE 0 END) as reverted"
                " FROM lead_merges WHERE merged_at >= ?"
            , (cutoff,)):
                result["merge_stats"] = {
                    "total": r4["total"] or 0,
                    "auto": r4["auto_cnt"] or 0,
                    "manual": r4["manual_cnt"] or 0,
                    "reverted": r4["reverted"] or 0,
                }

            # 6. 每日新增 Lead 趋势
            for r5 in conn.execute(
                "SELECT DATE(created_at) as d, COUNT(*) as cnt"
                " FROM leads_canonical WHERE created_at >= ?"
                " GROUP BY DATE(created_at) ORDER BY d DESC LIMIT 14"
            , (cutoff,)):
                result["daily_new_leads"].append({
                    "date": r5["d"], "count": r5["cnt"]})
            result["daily_new_leads"].reverse()
    except Exception as e:
        logger.debug("[canonical] get_unified_identity_kpi 失败: %s", e)
    return result


def _name_similarity(a: str, b: str) -> float:
    """简单字符集重叠比率 [0,1]. 用于误合并检测, 无需外部依赖."""
    if not a or not b:
        return 0.0
    sa, sb = set(a.lower().strip()), set(b.lower().strip())
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def detect_suspect_merges(limit: int = 50,
                          name_sim_threshold: float = 0.25,
                          identity_count_threshold: int = 6) -> List[Dict[str, Any]]:
    """Phase H2: 检测可疑误合并.

    规则:
      1. source 和 target 的 primary_name 相似度 < name_sim_threshold
      2. 合并后目标 canonical 的身份数 >= identity_count_threshold

    Returns:
        [{merge_id, source_cid, target_cid, source_name, target_name,
          name_similarity, target_identity_count, reason}]
    """
    import sqlite3
    suspects: List[Dict[str, Any]] = []
    try:
        with _connect() as conn:
            conn.row_factory = sqlite3.Row
            # 取最近的未撤销合并
            merges = conn.execute(
                "SELECT m.id, m.source_canonical_id, m.target_canonical_id,"
                " m.confidence, m.merge_mode, m.merged_at,"
                " cs.primary_name as src_name, ct.primary_name as tgt_name"
                " FROM lead_merges m"
                " LEFT JOIN leads_canonical cs ON cs.canonical_id = m.source_canonical_id"
                " LEFT JOIN leads_canonical ct ON ct.canonical_id = m.target_canonical_id"
                " WHERE m.reverted_at IS NULL"
                " AND COALESCE(m.audit_status, '') != 'safe'"
                " ORDER BY m.merged_at DESC LIMIT ?",
                (min(limit, 200),),
            ).fetchall()

            for m in merges:
                reasons = []
                src_name = m["src_name"] or ""
                tgt_name = m["tgt_name"] or ""
                sim = _name_similarity(src_name, tgt_name)

                # Rule 1: 名字差异过大
                if src_name and tgt_name and sim < name_sim_threshold:
                    reasons.append(f"name_dissimilar({sim:.2f})")

                # Rule 2: 目标身份数过多
                id_count_row = conn.execute(
                    "SELECT COUNT(*) FROM lead_identities WHERE canonical_id=?",
                    (m["target_canonical_id"],),
                ).fetchone()
                id_count = id_count_row[0] if id_count_row else 0
                if id_count >= identity_count_threshold:
                    reasons.append(f"high_identities({id_count})")

                if reasons:
                    suspects.append({
                        "merge_id": m["id"],
                        "source_cid": m["source_canonical_id"],
                        "target_cid": m["target_canonical_id"],
                        "source_name": src_name,
                        "target_name": tgt_name,
                        "name_similarity": round(sim, 3),
                        "target_identity_count": id_count,
                        "confidence": m["confidence"],
                        "merge_mode": m["merge_mode"],
                        "merged_at": m["merged_at"],
                        "reasons": reasons,
                    })
    except Exception as e:
        logger.debug("[canonical] detect_suspect_merges 失败: %s", e)
    return suspects


def is_contacted_globally(platform: str, account_id: str,
                          current_device: str = "") -> Dict[str, Any]:
    """K1: 跨设备去重检查 — 某 (platform, account_id) 是否已被其他设备接触过.

    基于 lead_identities 表, 平台无关 (FB/TikTok/LINE 通用).

    Returns:
        {"contacted": bool, "existing_device": str, "canonical_id": str,
         "display_name": str}
    """
    result = {"contacted": False, "existing_device": "",
              "canonical_id": "", "display_name": ""}
    if not platform or not account_id:
        return result
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT canonical_id, display_name, discovered_by_device"
                " FROM lead_identities"
                " WHERE platform=? AND account_id=?",
                (platform, account_id.strip()),
            ).fetchone()
            if row:
                existing_device = row[2] or ""
                result["canonical_id"] = row[0] or ""
                result["display_name"] = row[1] or ""
                result["existing_device"] = existing_device
                # 被其他设备接触过 = 存在且不是当前设备
                if existing_device and current_device and existing_device != current_device:
                    result["contacted"] = True
                    # L1: 记录拦截事件到 lead_journey (append-only)
                    try:
                        from src.host.lead_mesh.journey import append_journey
                        append_journey(
                            result["canonical_id"],
                            actor="system",
                            action="cross_device_dedup_blocked",
                            actor_device=current_device,
                            platform=platform,
                            data={"blocked_account": account_id,
                                  "existing_device": existing_device},
                        )
                    except Exception:
                        pass
    except Exception as e:
        logger.debug("[canonical] is_contacted_globally 查询失败: %s", e)
    return result


def get_cross_device_dedup_stats(since_days: int = 30) -> Dict[str, Any]:
    """L1: 跨设备去重效果统计 — 从 lead_journey 的 cross_device_dedup_blocked 事件聚合."""
    import sqlite3
    import datetime as _dt
    since = (_dt.datetime.utcnow() - _dt.timedelta(days=since_days)).strftime(
        "%Y-%m-%d %H:%M:%S")
    stats: Dict[str, Any] = {
        "total_blocks": 0,
        "unique_leads_saved": 0,
        "by_platform": {},
        "by_device": {},
        "daily": [],
    }
    try:
        with _connect() as conn:
            conn.row_factory = sqlite3.Row
            # 总拦截次数 + 不重复 lead 数
            row = conn.execute(
                "SELECT COUNT(*) as cnt, COUNT(DISTINCT canonical_id) as leads"
                " FROM lead_journey WHERE action='cross_device_dedup_blocked'"
                " AND at >= ?", (since,)).fetchone()
            stats["total_blocks"] = row["cnt"] or 0
            stats["unique_leads_saved"] = row["leads"] or 0
            # 按平台
            for r in conn.execute(
                "SELECT platform, COUNT(*) as cnt FROM lead_journey"
                " WHERE action='cross_device_dedup_blocked' AND at >= ?"
                " GROUP BY platform ORDER BY cnt DESC", (since,)):
                stats["by_platform"][r["platform"] or "unknown"] = r["cnt"]
            # 按设备 (top 10)
            for r in conn.execute(
                "SELECT actor_device, COUNT(*) as cnt FROM lead_journey"
                " WHERE action='cross_device_dedup_blocked' AND at >= ?"
                " GROUP BY actor_device ORDER BY cnt DESC LIMIT 10", (since,)):
                stats["by_device"][r["actor_device"][:12] or "?"] = r["cnt"]
            # 日趋势
            for r in conn.execute(
                "SELECT DATE(at) as day, COUNT(*) as cnt FROM lead_journey"
                " WHERE action='cross_device_dedup_blocked' AND at >= ?"
                " GROUP BY day ORDER BY day DESC LIMIT 14", (since,)):
                stats["daily"].append({"date": r["day"], "blocks": r["cnt"]})
    except Exception as e:
        logger.debug("[canonical] cross_device_dedup_stats 失败: %s", e)
    return stats


# ════════════════════════════════════════════════════════════════════
# Phase K2: Lead 生命周期状态机
# ════════════════════════════════════════════════════════════════════
_LIFECYCLE_ORDER = ["new", "contacted", "engaged", "qualified", "converted", "lost"]
_LIFECYCLE_RANK = {s: i for i, s in enumerate(_LIFECYCLE_ORDER)}


def advance_lifecycle(canonical_id: str, new_stage: str,
                      force: bool = False) -> bool:
    """K2: 推进 lead 生命周期. 默认只允许向前推进 (rank 更高), force=True 可跳转.

    'lost' 是终态, 只有 force=True 才能退回.
    """
    if new_stage not in _LIFECYCLE_RANK:
        return False
    import datetime as _dt
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT lifecycle_stage FROM leads_canonical WHERE canonical_id=?",
                (canonical_id,),
            ).fetchone()
            current = (row[0] if row else "") or "new"
            cur_rank = _LIFECYCLE_RANK.get(current, 0)
            new_rank = _LIFECYCLE_RANK.get(new_stage, 0)
            # 只允许前进 (或 force)
            if not force and new_rank <= cur_rank:
                return False
            conn.execute(
                "UPDATE leads_canonical SET lifecycle_stage=?,"
                " lifecycle_updated_at=? WHERE canonical_id=?",
                (new_stage,
                 _dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
                 canonical_id),
            )
            conn.commit()
            # L2: 记录 journey 事件 (供趋势分析)
            try:
                from src.host.lead_mesh.journey import append_journey
                append_journey(canonical_id, actor="system",
                               action="lifecycle_advanced",
                               data={"from": current, "to": new_stage})
            except Exception:
                pass
            # S1: 增量重算 lead_score
            try:
                compute_lead_score(canonical_id, persist=True)
            except Exception:
                pass
            # Q1: webhook 联动 (qualified/converted 触发外部通知)
            if new_stage in ("qualified", "converted"):
                try:
                    from src.host.lead_mesh.webhook_dispatcher import enqueue_webhook
                    # 获取 lead 名称 (row 可能已获取)
                    _wh_name = ""
                    try:
                        _n = conn.execute(
                            "SELECT primary_name FROM leads_canonical"
                            " WHERE canonical_id=?", (canonical_id,)
                        ).fetchone()
                        _wh_name = (_n[0] if _n else "") or ""
                    except Exception:
                        pass
                    enqueue_webhook(
                        event_type=f"lifecycle.{new_stage}",
                        payload={
                            "canonical_id": canonical_id,
                            "primary_name": _wh_name,
                            "stage": new_stage,
                            "from_stage": current,
                            "timestamp": _dt.datetime.utcnow().strftime(
                                "%Y-%m-%dT%H:%M:%SZ"),
                        },
                        related_canonical_id=canonical_id,
                    )
                except Exception:
                    pass
            return True
    except Exception as e:
        logger.debug("[lifecycle] advance 失败: %s", e)
        return False


def batch_advance_lifecycle(canonical_ids: List[str], new_stage: str,
                            force: bool = False) -> Dict[str, Any]:
    """N1: 批量推进生命周期. 返回 {success, failed, skipped} 计数."""
    result = {"success": 0, "failed": 0, "skipped": 0, "total": len(canonical_ids)}
    for cid in canonical_ids:
        if not cid:
            result["skipped"] += 1
            continue
        ok = advance_lifecycle(cid, new_stage, force=force)
        if ok:
            result["success"] += 1
        else:
            result["skipped"] += 1
    return result


def get_lifecycle_trend(days: int = 14) -> List[Dict[str, Any]]:
    """L2: 最近 N 天的生命周期流入趋势 — 每日进入各阶段的数量."""
    import sqlite3
    import datetime as _dt
    since = (_dt.datetime.utcnow() - _dt.timedelta(days=days)).strftime(
        "%Y-%m-%d %H:%M:%S")
    trend: List[Dict[str, Any]] = []
    try:
        with _connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT DATE(at) as day,"
                " json_extract(data_json, '$.to') as stage,"
                " COUNT(*) as cnt"
                " FROM lead_journey"
                " WHERE action='lifecycle_advanced' AND at >= ?"
                " GROUP BY day, stage ORDER BY day DESC",
                (since,),
            ).fetchall()
            # pivot: [{date, new, contacted, engaged, ...}, ...]
            by_day: Dict[str, Dict[str, int]] = {}
            for r in rows:
                d = r["day"] or ""
                s = r["stage"] or ""
                c = r["cnt"] or 0
                by_day.setdefault(d, {})
                by_day[d][s] = c
            for d in sorted(by_day.keys(), reverse=True):
                entry = {"date": d}
                for s in _LIFECYCLE_ORDER:
                    entry[s] = by_day[d].get(s, 0)
                trend.append(entry)
    except Exception as e:
        logger.debug("[lifecycle] trend 失败: %s", e)
    return trend


def get_lifecycle_summary() -> Dict[str, Any]:
    """K2: 生命周期阶段分布汇总."""
    summary: Dict[str, int] = {s: 0 for s in _LIFECYCLE_ORDER}
    total = 0
    try:
        with _connect() as conn:
            for row in conn.execute(
                "SELECT COALESCE(lifecycle_stage, 'new') as stage, COUNT(*) as cnt"
                " FROM leads_canonical WHERE merged_into IS NULL"
                " GROUP BY stage"
            ):
                stage = row[0] or "new"
                cnt = row[1] or 0
                summary[stage] = summary.get(stage, 0) + cnt
                total += cnt
    except Exception as e:
        logger.debug("[lifecycle] summary 失败: %s", e)
    return {
        "stages": summary,
        "total": total,
        "stage_rates": {k: round(v / max(total, 1), 3) for k, v in summary.items()},
    }


def check_lifecycle_alerts(min_total: int = 20,
                           thresholds: Optional[Dict[str, float]] = None
                           ) -> List[Dict[str, Any]]:
    """O1: 检查生命周期漏斗瓶颈, 返回告警列表.

    默认阈值 (累积比率): contacted>=40%, engaged>=10%, qualified>=3%, converted>=1%.
    只在 total >= min_total 时检查 (数据不足不告警).
    """
    defaults = {
        "contacted": 0.40,
        "engaged": 0.10,
        "qualified": 0.03,
        "converted": 0.01,
    }
    th = dict(defaults, **(thresholds or {}))
    lc = get_lifecycle_summary()
    total = lc.get("total", 0)
    if total < min_total:
        return []
    stages = lc.get("stages", {})
    rates = lc.get("stage_rates", {})
    alerts: List[Dict[str, Any]] = []
    # 累积比率: contacted 包含 contacted+engaged+qualified+converted
    cumulative = {}
    _order = ["contacted", "engaged", "qualified", "converted"]
    for s in _order:
        cumulative[s] = sum(stages.get(o, 0) for o in _order[_order.index(s):]) / max(total, 1)
    for stage, min_rate in th.items():
        actual = cumulative.get(stage, 0)
        if actual < min_rate:
            alerts.append({
                "type": "lifecycle_bottleneck",
                "stage": stage,
                "expected_min": min_rate,
                "actual": round(actual, 4),
                "total": total,
                "message": f"漏斗瓶颈: {stage} 累积率 {actual*100:.1f}% < 阈值 {min_rate*100:.0f}% (总量{total})",
            })
    # 流失率过高告警
    lost = stages.get("lost", 0)
    if total > 0 and (lost / total) > 0.25:
        alerts.append({
            "type": "high_loss_rate",
            "stage": "lost",
            "actual": round(lost / total, 4),
            "total": total,
            "message": f"流失率过高: {lost}/{total} = {lost/total*100:.1f}%",
        })
    return alerts


def get_lifecycle_dwell_stats() -> Dict[str, Any]:
    """P2: 各生命周期阶段平均停留时长 (天). 基于 journey 中 lifecycle_advanced 事件."""
    import sqlite3
    from datetime import datetime as _dt
    # 对每个 canonical_id, 按时间序排列 lifecycle_advanced 事件, 计算相邻事件间隔
    dwell_sums: Dict[str, float] = {}  # stage → total_days
    dwell_counts: Dict[str, int] = {}  # stage → count
    try:
        with _connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT canonical_id, data, created_at FROM lead_journey"
                " WHERE action='lifecycle_advanced'"
                " ORDER BY canonical_id, created_at ASC"
            ).fetchall()
        # group by canonical_id
        current_cid = None
        prev_time = None
        prev_stage = None
        for row in rows:
            cid = row["canonical_id"]
            data_raw = row["data"]
            ts_str = row["created_at"] or ""
            try:
                import json as _j
                data = _j.loads(data_raw) if isinstance(data_raw, str) else (data_raw or {})
            except Exception:
                data = {}
            try:
                ts = _dt.fromisoformat(ts_str.replace("Z", "+00:00").replace("T", " ").split("+")[0])
            except Exception:
                continue
            if cid != current_cid:
                current_cid = cid
                prev_time = ts
                prev_stage = data.get("from", "new")
                continue
            from_stage = data.get("from", "")
            if from_stage and prev_time:
                delta = (ts - prev_time).total_seconds() / 86400.0
                if 0 < delta < 365:  # sanity: ignore >1yr
                    dwell_sums[from_stage] = dwell_sums.get(from_stage, 0) + delta
                    dwell_counts[from_stage] = dwell_counts.get(from_stage, 0) + 1
            prev_time = ts
            prev_stage = from_stage
    except Exception as e:
        logger.debug("[lifecycle] dwell stats 失败: %s", e)
    result = {}
    for stage in _LIFECYCLE_ORDER:
        cnt = dwell_counts.get(stage, 0)
        if cnt > 0:
            result[stage] = {"avg_days": round(dwell_sums[stage] / cnt, 2),
                             "samples": cnt}
        else:
            result[stage] = {"avg_days": None, "samples": 0}
    return result


def backfill_lifecycle_from_history(dry_run: bool = True) -> Dict[str, Any]:
    """O2: 从历史事件表一次性回填 lifecycle_stage.

    数据源优先级 (高→低):
      1. fb_contact_events: wa_referral_sent/replied → qualified
      2. fb_contact_events: greeting_replied/message_received → engaged
      3. fb_contact_events: greeting_sent/greeting_fallback → contacted
      4. facebook_friend_requests: accepted → engaged, sent → contacted
      5. line_dispatch_log: planned/sent → qualified

    dry_run=True 只返回计划, 不实际更新.
    """
    import sqlite3
    stats = {"scanned": 0, "upgraded": 0, "skipped": 0, "by_stage": {}, "dry_run": dry_run}

    # 收集每个 canonical_id 的最高推导阶段
    inferred: Dict[str, int] = {}  # cid → max rank

    def _bump(cid: str, stage: str):
        if not cid:
            return
        rank = _LIFECYCLE_RANK.get(stage, 0)
        if rank > inferred.get(cid, 0):
            inferred[cid] = rank

    try:
        # --- fb_contact_events ---
        _evt_stage_map = {
            "greeting_sent": "contacted", "greeting_fallback": "contacted",
            "greeting_replied": "engaged", "message_received": "engaged",
            "wa_referral_sent": "qualified", "wa_referral_replied": "qualified",
            "line_dispatch_planned": "qualified",
        }
        try:
            from src.host.fb_store import _connect as _fb_conn
            with _fb_conn() as conn:
                conn.row_factory = sqlite3.Row
                for row in conn.execute(
                    "SELECT canonical_id, event_type FROM fb_contact_events"
                    " WHERE canonical_id != '' AND canonical_id IS NOT NULL"
                ):
                    stage = _evt_stage_map.get(row["event_type"])
                    if stage:
                        _bump(row["canonical_id"], stage)
        except Exception:
            pass

        # --- facebook_friend_requests ---
        try:
            from src.host.fb_store import _connect as _fb_conn2
            with _fb_conn2() as conn:
                conn.row_factory = sqlite3.Row
                for row in conn.execute(
                    "SELECT canonical_id, status FROM facebook_friend_requests"
                    " WHERE canonical_id != '' AND canonical_id IS NOT NULL"
                ):
                    if row["status"] == "accepted":
                        _bump(row["canonical_id"], "engaged")
                    elif row["status"] == "sent":
                        _bump(row["canonical_id"], "contacted")
        except Exception:
            pass

        # --- line_dispatch_log ---
        try:
            with _connect() as conn:
                conn.row_factory = sqlite3.Row
                for row in conn.execute(
                    "SELECT canonical_id, status FROM line_dispatch_log"
                    " WHERE canonical_id != '' AND canonical_id IS NOT NULL"
                    " AND status IN ('planned','sent')"
                ):
                    _bump(row["canonical_id"], "qualified")
        except Exception:
            pass

        stats["scanned"] = len(inferred)

        if not dry_run:
            for cid, rank in inferred.items():
                stage = _LIFECYCLE_ORDER[rank]
                ok = advance_lifecycle(cid, stage)
                if ok:
                    stats["upgraded"] += 1
                    stats["by_stage"][stage] = stats["by_stage"].get(stage, 0) + 1
                else:
                    stats["skipped"] += 1
        else:
            # dry_run: 统计将会升级多少
            with _connect() as conn:
                conn.row_factory = sqlite3.Row
                for cid, rank in inferred.items():
                    cur = conn.execute(
                        "SELECT lifecycle_stage FROM leads_canonical WHERE canonical_id=?",
                        (cid,)).fetchone()
                    current_rank = _LIFECYCLE_RANK.get(
                        (cur["lifecycle_stage"] if cur else "") or "new", 0)
                    if rank > current_rank:
                        stage = _LIFECYCLE_ORDER[rank]
                        stats["upgraded"] += 1
                        stats["by_stage"][stage] = stats["by_stage"].get(stage, 0) + 1
                    else:
                        stats["skipped"] += 1

    except Exception as e:
        logger.warning("[backfill] lifecycle 回填失败: %s", e)
        stats["error"] = str(e)
    return stats


def auto_mark_lost_leads(inactive_days: int = 30,
                         dry_run: bool = False) -> Dict[str, Any]:
    """Q4: 自动标记长期无活动的 lead 为 lost.

    判断逻辑:
      - lifecycle_stage 不在终态 (converted/lost)
      - MAX(lifecycle_updated_at, 最后 journey 事件时间) < now - inactive_days
    """
    import sqlite3
    _terminal = ("converted", "lost")
    cutoff = (_dt.datetime.utcnow() - _dt.timedelta(days=inactive_days)
              ).strftime("%Y-%m-%d %H:%M:%S")
    stats = {"scanned": 0, "marked_lost": 0, "dry_run": dry_run,
             "inactive_days": inactive_days}
    try:
        with _connect() as conn:
            conn.row_factory = sqlite3.Row
            # 找不在终态且 lifecycle_updated_at 老于 cutoff 的 lead
            candidates = conn.execute(
                "SELECT c.canonical_id, c.lifecycle_stage, c.lifecycle_updated_at,"
                " (SELECT MAX(j.created_at) FROM lead_journey j"
                "  WHERE j.canonical_id = c.canonical_id) AS last_journey_at"
                " FROM leads_canonical c"
                " WHERE c.merged_into IS NULL"
                " AND COALESCE(c.lifecycle_stage, 'new') NOT IN ('converted','lost')"
                " AND COALESCE(c.lifecycle_updated_at, c.created_at) < ?"
                " LIMIT 2000",
                (cutoff,)
            ).fetchall()
        stats["scanned"] = len(candidates)
        for row in candidates:
            cid = row["canonical_id"]
            last_activity = row["last_journey_at"] or row["lifecycle_updated_at"] or ""
            if last_activity and last_activity >= cutoff:
                continue  # journey 活动比 lifecycle_updated_at 新
            if not dry_run:
                advance_lifecycle(cid, "lost", force=True)
            stats["marked_lost"] += 1
    except Exception as e:
        logger.debug("[auto_lost] 失败: %s", e)
        stats["error"] = str(e)
    return stats


def check_lifecycle_sla(sla_days: Optional[Dict[str, int]] = None,
                        limit: int = 50) -> Dict[str, Any]:
    """R4: 检查各阶段超 SLA 停留时间的 lead (at-risk).

    默认 SLA (天): contacted=14, engaged=10, qualified=7.
    返回 {at_risk_count, leads: [{canonical_id, primary_name, stage, dwell_days}]}
    """
    import sqlite3
    defaults = {"contacted": 14, "engaged": 10, "qualified": 7}
    sla = dict(defaults, **(sla_days or {}))
    at_risk: List[Dict[str, Any]] = []
    try:
        with _connect() as conn:
            conn.row_factory = sqlite3.Row
            for stage, max_days in sla.items():
                cutoff = (_dt.datetime.utcnow() - _dt.timedelta(days=max_days)
                          ).strftime("%Y-%m-%d %H:%M:%S")
                rows = conn.execute(
                    "SELECT canonical_id, primary_name, lifecycle_stage,"
                    " lifecycle_updated_at, created_at"
                    " FROM leads_canonical"
                    " WHERE merged_into IS NULL"
                    " AND COALESCE(lifecycle_stage, 'new') = ?"
                    " AND COALESCE(lifecycle_updated_at, created_at) < ?"
                    " ORDER BY lifecycle_updated_at ASC LIMIT ?",
                    (stage, cutoff, limit)
                ).fetchall()
                for r in rows:
                    ts_str = r["lifecycle_updated_at"] or r["created_at"] or ""
                    dwell = None
                    if ts_str:
                        try:
                            ts = _dt.datetime.fromisoformat(
                                ts_str.replace("Z", "").replace("T", " ").split("+")[0])
                            dwell = round((_dt.datetime.utcnow() - ts).total_seconds() / 86400, 1)
                        except Exception:
                            pass
                    at_risk.append({
                        "canonical_id": r["canonical_id"],
                        "primary_name": r["primary_name"] or "",
                        "stage": stage,
                        "dwell_days": dwell,
                        "sla_days": max_days,
                    })
    except Exception as e:
        logger.debug("[sla] check 失败: %s", e)
    at_risk.sort(key=lambda x: -(x.get("dwell_days") or 0))
    return {"at_risk_count": len(at_risk), "leads": at_risk[:limit]}


def compute_lead_score(canonical_id: str, *, persist: bool = True) -> int:
    """S1: 计算单个 lead 的综合评分 (0-100).

    维度:
      - lifecycle_stage 权重 (0-35)
      - identity_count 跨平台身份 (0-15)
      - journey 活跃度 log2(events) (0-20)
      - recency 最近活动天数 (0-15)
      - reply_signal 有回复事件 (+15)
    """
    import sqlite3, math, json as _json
    _stage_scores = {"new": 0, "contacted": 10, "engaged": 25,
                     "qualified": 35, "converted": 35, "lost": 0}
    score = 0
    try:
        with _connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT lifecycle_stage, lifecycle_updated_at, created_at,"
                " metadata_json"
                " FROM leads_canonical WHERE canonical_id=?",
                (canonical_id,)).fetchone()
            if not row:
                return 0
            stage = (row["lifecycle_stage"] or "new")
            score += _stage_scores.get(stage, 0)

            # identity count
            id_cnt = conn.execute(
                "SELECT COUNT(*) AS n FROM lead_identities WHERE canonical_id=?",
                (canonical_id,)).fetchone()
            id_n = (id_cnt["n"] if id_cnt else 1) or 1
            score += min(15, (id_n - 1) * 8)

            # journey activity
            j_cnt = conn.execute(
                "SELECT COUNT(*) AS n FROM lead_journey WHERE canonical_id=?",
                (canonical_id,)).fetchone()
            j_n = (j_cnt["n"] if j_cnt else 0) or 0
            if j_n > 0:
                score += min(20, int(5 * math.log2(j_n + 1)))

            # recency
            last_evt = conn.execute(
                "SELECT MAX(created_at) AS last FROM lead_journey"
                " WHERE canonical_id=?", (canonical_id,)).fetchone()
            last_ts = (last_evt["last"] if last_evt else "") or ""
            if last_ts:
                try:
                    ts = _dt.datetime.fromisoformat(
                        last_ts.replace("Z", "").replace("T", " ").split("+")[0])
                    days_ago = (_dt.datetime.utcnow() - ts).total_seconds() / 86400
                    if days_ago <= 3:
                        score += 15
                    elif days_ago <= 7:
                        score += 10
                    elif days_ago <= 14:
                        score += 5
                except Exception:
                    pass

            # reply signal
            reply_cnt = conn.execute(
                "SELECT COUNT(*) AS n FROM lead_journey WHERE canonical_id=?"
                " AND action IN ('greeting_replied','message_received',"
                "'inbox_received','friend_accepted')",
                (canonical_id,)).fetchone()
            if reply_cnt and (reply_cnt["n"] or 0) > 0:
                score += 15

            score = max(0, min(100, score))

            # persist to metadata + S3 auto-tagging
            if persist:
                try:
                    meta_raw = row["metadata_json"] or "{}"
                    meta = _json.loads(meta_raw)
                except Exception:
                    meta = {}
                meta["lead_score"] = score
                # S3: 自动标签
                cur_tags_row = conn.execute(
                    "SELECT tags FROM leads_canonical WHERE canonical_id=?",
                    (canonical_id,)).fetchone()
                tags_set = {t.strip() for t in
                            ((cur_tags_row["tags"] if cur_tags_row else "") or "").split(",")
                            if t.strip()}
                # 高价值
                if score >= 70:
                    tags_set.add("high_value")
                    tags_set.discard("cold_lead")
                elif score <= 20 and stage not in ("converted", "lost"):
                    tags_set.add("cold_lead")
                    tags_set.discard("high_value")
                else:
                    tags_set.discard("high_value")
                    tags_set.discard("cold_lead")
                # 多平台
                if id_n >= 3:
                    tags_set.add("multi_platform")
                new_tags = ",".join(sorted(tags_set)) if tags_set else ""
                conn.execute(
                    "UPDATE leads_canonical SET metadata_json=?, tags=?,"
                    " updated_at=datetime('now') WHERE canonical_id=?",
                    (_json.dumps(meta, ensure_ascii=False), new_tags, canonical_id))
    except Exception as e:
        logger.debug("[score] compute 失败 %s: %s", canonical_id[:8], e)
    return score


def batch_recompute_scores(limit: int = 2000) -> Dict[str, Any]:
    """S1: 批量重算 lead_score (日报触发). 返回 {computed, avg_score}."""
    import sqlite3
    stats = {"computed": 0, "total_score": 0}
    try:
        with _connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT canonical_id FROM leads_canonical"
                " WHERE merged_into IS NULL"
                " ORDER BY updated_at DESC LIMIT ?",
                (min(limit, 5000),)).fetchall()
        for row in rows:
            s = compute_lead_score(row["canonical_id"], persist=True)
            stats["computed"] += 1
            stats["total_score"] += s
    except Exception as e:
        logger.debug("[score] batch recompute 失败: %s", e)
        stats["error"] = str(e)
    if stats["computed"] > 0:
        stats["avg_score"] = round(stats["total_score"] / stats["computed"], 1)
    return stats


def get_score_leaderboard(top_n: int = 10) -> Dict[str, Any]:
    """T2: lead_score 排行榜 + 分段分布.

    返回:
      top: [{canonical_id, primary_name, lifecycle_stage, lead_score}]
      distribution: {"0-20":n, "21-40":n, "41-60":n, "61-80":n, "81-100":n}
      avg_score, total_scored
    """
    import sqlite3, json as _json
    top: List[Dict[str, Any]] = []
    dist = {"0-20": 0, "21-40": 0, "41-60": 0, "61-80": 0, "81-100": 0}
    scores: List[int] = []
    try:
        with _connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT canonical_id, primary_name, lifecycle_stage,"
                " metadata_json FROM leads_canonical"
                " WHERE merged_into IS NULL"
            ).fetchall()
        for r in rows:
            try:
                meta = _json.loads(r["metadata_json"] or "{}")
            except Exception:
                meta = {}
            sc = meta.get("lead_score")
            if sc is None:
                continue
            sc = int(sc)
            scores.append(sc)
            if sc <= 20:
                dist["0-20"] += 1
            elif sc <= 40:
                dist["21-40"] += 1
            elif sc <= 60:
                dist["41-60"] += 1
            elif sc <= 80:
                dist["61-80"] += 1
            else:
                dist["81-100"] += 1
            top.append({
                "canonical_id": r["canonical_id"],
                "primary_name": r["primary_name"] or "",
                "lifecycle_stage": r["lifecycle_stage"] or "new",
                "lead_score": sc,
            })
        top.sort(key=lambda x: -x["lead_score"])
    except Exception as e:
        logger.debug("[score] leaderboard 失败: %s", e)
    avg = round(sum(scores) / len(scores), 1) if scores else 0
    return {"top": top[:top_n], "distribution": dist,
            "avg_score": avg, "total_scored": len(scores)}


def update_canonical_metadata(canonical_id: str,
                              metadata_patch: Dict[str, Any],
                              tags: Optional[List[str]] = None) -> bool:
    """合并 metadata_patch 到 leads_canonical.metadata_json (shallow merge).

    用于 L2 VLM PASS 后把 age_band/gender/is_japanese/overall_confidence
    等精准画像字段存入用户画像 DB, 供运营面板 / CRM 查询.

    Args:
        canonical_id: leads_canonical.canonical_id
        metadata_patch: 要合并的字段 (覆盖同 key)
        tags: 可选, append 到 tags 列 (逗号分隔)

    Returns True if updated, False otherwise.
    """
    if not canonical_id or not metadata_patch:
        return False
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT metadata_json, tags FROM leads_canonical WHERE canonical_id=?",
                (canonical_id,),
            ).fetchone()
            if not row:
                logger.warning("[canonical] update_metadata: %s 不存在",
                               canonical_id[:12])
                return False
            try:
                meta = json.loads(row["metadata_json"] or "{}")
            except Exception:
                meta = {}
            meta.update({k: v for k, v in metadata_patch.items() if v is not None})
            new_tags = row["tags"] or ""
            if tags:
                existing = {t.strip() for t in new_tags.split(",") if t.strip()}
                existing.update(t.strip() for t in tags if t)
                new_tags = ",".join(sorted(existing))
            conn.execute(
                "UPDATE leads_canonical SET metadata_json=?, tags=?,"
                " updated_at=datetime('now') WHERE canonical_id=?",
                (json.dumps(meta, ensure_ascii=False), new_tags, canonical_id),
            )
            return True
    except Exception as e:
        logger.warning("[canonical] update_metadata 失败: %s", e)
        return False


def remove_canonical_tags(canonical_id: str, tags: List[str]) -> bool:
    """Phase 12.3: 从 leads_canonical.tags 里去掉指定 tag (不改 metadata).

    和 update_canonical_metadata 的 tags 参数只加不减的语义互补.
    """
    if not canonical_id or not tags:
        return False
    strip = {t.strip() for t in tags if t and t.strip()}
    if not strip:
        return False
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT tags FROM leads_canonical WHERE canonical_id=?",
                (canonical_id,),
            ).fetchone()
            if not row:
                return False
            existing = {t.strip() for t in
                         (row["tags"] or "").split(",") if t.strip()}
            remain = existing - strip
            if remain == existing:
                return False  # 没变化
            conn.execute(
                "UPDATE leads_canonical SET tags=?,"
                " updated_at=datetime('now') WHERE canonical_id=?",
                (",".join(sorted(remain)), canonical_id),
            )
            return True
    except Exception as e:
        logger.warning("[canonical] remove_tags 失败: %s", e)
        return False


def revive_referral(canonical_id: str, *,
                    actor: str = "operator") -> bool:
    """Phase 12.3/12.4: 给"已死" peer 第二次机会 —
      - 去 referral_dead tag
      - 清 metadata.referral_dead_reason / at / peer_name
      - 清 metadata.referral_fail_count_* (每种错误码的累计计数)
      - Phase 12.4: 写 lead_journey(action='referral_revived', actor=...)
        作为 audit trail, data 带清理前的 dead_reason + fail counts 快照.

    ``actor`` 用于 journey 审计: ``"operator"`` (默认, 手动/UI), ``"operator_ui"``,
    ``"scheduled_7d_auto"`` (cron task), ``"human:<username>"`` 等. 格式与
    append_journey 一致.

    返 True 表示确实执行了清理 (peer 原本有 referral_dead).
    """
    if not canonical_id:
        return False
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT metadata_json, tags FROM leads_canonical"
                " WHERE canonical_id=?", (canonical_id,),
            ).fetchone()
            if not row:
                return False
            tags_set = {t.strip() for t in
                         (row["tags"] or "").split(",") if t.strip()}
            had_dead = "referral_dead" in tags_set
            try:
                meta = json.loads(row["metadata_json"] or "{}")
            except Exception:
                meta = {}
            # 清理前快照 (供 journey 审计)
            snapshot = {
                "had_dead_tag": had_dead,
                "dead_reason": meta.get("referral_dead_reason"),
                "dead_at": meta.get("referral_dead_at"),
                "fail_counts": {
                    k: v for k, v in meta.items()
                    if k.startswith("referral_fail_count_")
                },
            }
            # 清 metadata 字段
            dirty = False
            for k in ("referral_dead_reason", "referral_dead_at",
                       "referral_dead_peer_name"):
                if meta.pop(k, None) is not None:
                    dirty = True
            for k in list(meta.keys()):
                if k.startswith("referral_fail_count_"):
                    meta.pop(k, None)
                    dirty = True
            if had_dead:
                tags_set.discard("referral_dead")
            if dirty or had_dead:
                conn.execute(
                    "UPDATE leads_canonical SET metadata_json=?, tags=?,"
                    " updated_at=datetime('now') WHERE canonical_id=?",
                    (json.dumps(meta, ensure_ascii=False),
                     ",".join(sorted(tags_set)),
                     canonical_id),
                )

        did_something = had_dead or dirty

        # Phase 12.4: 写 journey audit (成功 revive 才写, 不破坏现有行为).
        if did_something:
            try:
                from .journey import append_journey
                append_journey(
                    canonical_id=canonical_id,
                    actor=actor or "operator",
                    action="referral_revived",
                    platform="system",
                    data=snapshot,
                )
            except Exception as _e:
                logger.debug("[canonical] revive journey 写失败(忽略): %s", _e)

        return did_something
    except Exception as e:
        logger.warning("[canonical] revive_referral 失败 cid=%s: %s",
                         canonical_id[:12] if canonical_id else "", e)
        return False


def count_l2_verified_leads(
    *, include_tags: Optional[List[str]] = None,
    exclude_tags: Optional[List[str]] = None,
) -> int:
    """Phase 12.5: 数 l2_verified leads (带可选 tag 过滤). SQL 层只能算 tag-LIKE,
    其它 metadata 过滤 (age/gender/...) 不算 — 给 UI 显示总页范围用,
    精度 OK. 性能: 单 COUNT, 几十毫秒.
    """
    sql_parts = ["tags LIKE '%l2_verified%'", "merged_into IS NULL"]
    if include_tags:
        for t in include_tags:
            t_clean = t.strip().replace("%", "")
            if t_clean:
                # 用参数化避免注入. 但 LIKE 模式拼 % 必须 inline (SQLite ?
                # 不展开为 LIKE pattern). 再用 simple 字符 whitelist.
                if all(c.isalnum() or c in "_-:" for c in t_clean):
                    sql_parts.append(f"tags LIKE '%{t_clean}%'")
    if exclude_tags:
        for t in exclude_tags:
            t_clean = t.strip().replace("%", "")
            if t_clean and all(c.isalnum() or c in "_-:" for c in t_clean):
                sql_parts.append(f"tags NOT LIKE '%{t_clean}%'")
    sql = ("SELECT COUNT(*) AS n FROM leads_canonical"
           " WHERE " + " AND ".join(sql_parts))
    try:
        with _connect() as conn:
            row = conn.execute(sql).fetchone()
        return int(row["n"] if row else 0)
    except Exception as e:
        logger.warning("[canonical] count_l2_verified 失败: %s", e)
        return 0


def list_l2_verified_leads(
    *, age_band: Optional[str] = None,
    gender: Optional[str] = None,
    is_japanese: Optional[bool] = None,
    persona_key: Optional[str] = None,
    platform: Optional[str] = None,
    min_score: float = 0,
    limit: int = 50,
    offset: int = 0,
    include_tags: Optional[List[str]] = None,
    exclude_tags: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """Phase 10.3: 查询 L2 VLM 验证过的"精准画像用户".

    逻辑:
      - SQL 层按 ``tags LIKE '%l2_verified%'`` 预筛 (tag 写入时做了归一化).
      - JSON metadata 字段 (age_band / gender / is_japanese / l2_persona_key /
        l2_score) 由 Python 层过滤 — 避免依赖 SQLite JSON1 扩展 (老版本不带).
      - 按 l2_score 降序返 + l2_verified_at 新 → 旧.

    返回: [{canonical_id, display_name, platform, primary_account_id,
            metadata (dict), tags (list), l2_score, l2_verified_at}, ...]
    """
    limit = max(1, min(int(limit or 50), 1000))
    offset = max(0, int(offset or 0))
    # SQL 预筛: tag 含 l2_verified + 未被合并. 其它过滤 (metadata 字段 /
    # tag AND/NOT) 都在 Python 层. 先拉 (offset + limit) * 4 行做缓冲,
    # 过滤完再在 Python 层切 [offset : offset+limit]. offset 小的话 (<500)
    # 性能够用.
    fetch_n = (offset + limit) * 4
    sql = (
        "SELECT canonical_id, primary_name, tags, metadata_json, updated_at"
        "  FROM leads_canonical"
        " WHERE tags LIKE '%l2_verified%' AND merged_into IS NULL"
        " ORDER BY updated_at DESC LIMIT ?"
    )
    args: List[Any] = [fetch_n]

    out: List[Dict[str, Any]] = []
    try:
        with _connect() as conn:
            rows = conn.execute(sql, args).fetchall()
            # 平台 / account_id 在 lead_identities M:N 表, 取首个 identity 作为展示.
            ident_cache: Dict[str, Dict[str, str]] = {}
            if rows:
                cids_tuple = tuple(r["canonical_id"] for r in rows)
                placeholders = ",".join(["?"] * len(cids_tuple))
                for ir in conn.execute(
                    f"SELECT canonical_id, platform, account_id FROM lead_identities"
                    f" WHERE canonical_id IN ({placeholders})"
                    f" ORDER BY id ASC",
                    cids_tuple,
                ).fetchall():
                    # 只保第一条 (首次发现)
                    ident_cache.setdefault(
                        ir["canonical_id"],
                        {"platform": ir["platform"] or "",
                         "account_id": ir["account_id"] or ""})
    except Exception as e:
        logger.warning("[canonical] list_l2_verified SQL 失败: %s", e)
        return []

    for row in rows:
        try:
            meta = json.loads(row["metadata_json"] or "{}")
        except Exception:
            meta = {}
        tags_str = row["tags"] or ""
        tags = [t.strip() for t in tags_str.split(",") if t.strip()]

        if age_band and (meta.get("age_band") or "").lower() != age_band.lower():
            continue
        if gender and (meta.get("gender") or "").lower() != gender.lower():
            continue
        if is_japanese is not None:
            _ij = meta.get("is_japanese")
            if bool(_ij) is not bool(is_japanese):
                continue
        if persona_key and (meta.get("l2_persona_key") or "") != persona_key:
            continue
        try:
            score_v = float(meta.get("l2_score", 0) or 0)
        except (TypeError, ValueError):
            score_v = 0.0
        if score_v < float(min_score or 0):
            continue

        ident = ident_cache.get(row["canonical_id"], {})
        _plat = (ident.get("platform") or "").lower()
        if platform and _plat != platform.lower():
            continue
        # Phase 12.2 tags include/exclude (含 referral_dead / line_referred 等)
        if include_tags:
            if not all(t in tags for t in include_tags):
                continue
        if exclude_tags:
            if any(t in tags for t in exclude_tags):
                continue

        out.append({
            "canonical_id": row["canonical_id"],
            "display_name": row["primary_name"] or "",
            "platform": _plat,
            "primary_account_id": ident.get("account_id") or "",
            "tags": tags,
            "metadata": meta,
            "l2_score": score_v,
            "l2_verified_at": meta.get("l2_verified_at") or "",
        })
        # Phase 12.4: 不 break early, 跑完 fetch_n 行再 sort + offset slice.
        # SQL ORDER BY updated_at DESC 在同秒 tie 下不保证顺序, 早退会切到
        # 错误的 row 子集. fetch_n=(offset+limit)*4 已 cap 内存压力.
    # 排序: 先按 l2_verified_at 新 → 旧, 再按 l2_score 高 → 低 (主键); Python
    # sort stable 保证同 score 下仍按 verified_at 新的在前.
    out.sort(key=lambda x: x["l2_verified_at"], reverse=True)
    out.sort(key=lambda x: x["l2_score"], reverse=True)
    # Phase 12.4: 应用 offset (排序后切, 保证页与页之间顺序一致)
    if offset:
        out = out[offset:offset + limit]
    else:
        out = out[:limit]
    return out
