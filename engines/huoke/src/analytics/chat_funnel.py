# -*- coding: utf-8 -*-
"""扩展漏斗指标 (P8, B 独占)。

设计原则(深入思考后重构):

  * **零共享区 schema 改动** — 不加列、不加表、不改 fb_store 写入契约。
    对已有 `facebook_inbox_messages` + `fb_contact_events` 做 on-the-fly
    聚合, 需要 intent 信息时**重跑** chat_intent 分类 (rule-first 大部分
    零 LLM, 成本极低)。
  * **消费 A 的 /facebook/greeting-reply-rate** — 不自己重复计算模板 A/B
    回复率, 通过 HTTP 客户端(可选)调 A 的 Phase 5 端点。A 未 merge 时
    graceful skip 不 block。
  * **完全 B 独占** — 新文件 `src/analytics/chat_funnel.py` 不改任何
    共享区。和 fb_store.get_funnel_metrics 共存, caller 二选一或合并调。

用法:

    from src.analytics.chat_funnel import get_funnel_metrics_extended
    m = get_funnel_metrics_extended(device_id="devA", since_iso="2026-04-20T00:00:00Z")
    # m["stage_*"] 是基础漏斗
    # m["reply_rate_by_intent"] / m["gate_block_distribution"] /
    # m["stranger_conversion_rate"] 是 B 扩展字段

每个扩展字段都有独立入口函数, 可单独调用用于 Dashboard 切片。
"""
from __future__ import annotations

import json
import logging
from typing import Any, Callable, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# 辅助: 安全查询 DB
# ─────────────────────────────────────────────────────────────────────────────

def _connect():
    from src.host.database import _connect as _c
    return _c()


def _iso_bound_clause(since_iso: Optional[str], col: str = "seen_at") -> Tuple[str, List[Any]]:
    """生成 ``AND col >= ?`` clause + params。since_iso 空返 ("", [])。"""
    if not since_iso:
        return "", []
    return f" AND {col} >= ?", [since_iso]


def _preset_clause(preset_key: Optional[str]) -> Tuple[str, List[Any]]:
    if not preset_key:
        return "", []
    return " AND preset_key = ?", [preset_key]


# ─────────────────────────────────────────────────────────────────────────────
# 1. reply_rate_by_intent
# ─────────────────────────────────────────────────────────────────────────────

def reply_rate_by_intent(device_id: Optional[str] = None, *,
                         since_iso: Optional[str] = None,
                         preset_key: Optional[str] = None) -> Dict[str, Any]:
    """对 incoming 消息重跑 intent 分类, 聚合各 intent 的回复情况。

    输出:
        {
          "total_incomings": int,
          "classifiable": int,           # 能重跑 classify 成功的数 (chat_intent 可用)
          "by_intent": {
              "<intent>": {"incomings": N, "replied": M, "reply_rate": M/N}
          },
          "errors": int,                 # classify 抛异常数 (不应为非 0)
        }

    "replied" 定义: 同 peer 该 incoming 行之后 (id 更大) 有 ``direction='outgoing'``
    且 ``ai_decision IN ('reply','wa_referral')`` 的行。不限 window, 自然
    ``since_iso`` 已经限定了 incoming 采样。

    chat_intent 模块不可用(P4 未 merge)时返回 empty shell。
    """
    try:
        from src.ai.chat_intent import classify_intent
    except Exception:
        return {"total_incomings": 0, "classifiable": 0,
                "by_intent": {}, "errors": 0,
                "note": "P4 chat_intent not available"}

    where_since, params_since = _iso_bound_clause(since_iso, "seen_at")
    where_device, params_dev = ("", [])
    if device_id:
        where_device = " AND device_id = ?"
        params_dev = [device_id]
    where_preset, params_pre = _preset_clause(preset_key)

    sql = ("SELECT id, device_id, peer_name, message_text, seen_at, peer_type"
           " FROM facebook_inbox_messages"
           " WHERE direction='incoming'"
           + where_device + where_since + where_preset +
           " ORDER BY id ASC")
    params = params_dev + params_since + params_pre

    by_intent: Dict[str, Dict[str, int]] = {}
    errors = 0
    classifiable = 0
    total = 0

    # 先拉 incomings
    with _connect() as conn:
        incoming_rows = conn.execute(sql, params).fetchall()

    # 为每个 incoming 查是否有后续 reply (单 SQL 批量)
    replied_ids: set = set()
    if incoming_rows:
        inc_ids = [r[0] for r in incoming_rows]
        # 简单做法: 每个 incoming 一个 EXISTS 查询;但 SQLite 下数据量百-千级
        # 可接受。更高效可一次 JOIN, 这里优先可读性。
        with _connect() as conn:
            for row in incoming_rows:
                inc_id, did, peer, msg_text, seen_at, peer_type = row
                q = conn.execute(
                    "SELECT 1 FROM facebook_inbox_messages"
                    " WHERE device_id=? AND peer_name=?"
                    " AND direction='outgoing'"
                    " AND ai_decision IN ('reply','wa_referral')"
                    " AND id > ? LIMIT 1",
                    (did, peer, inc_id),
                ).fetchone()
                if q:
                    replied_ids.add(inc_id)

    for row in incoming_rows:
        inc_id, did, peer, msg_text, seen_at, peer_type = row
        total += 1
        try:
            # 无历史仅看当前 text, 对 rule 层够用 (intent 分类主要看当前消息)
            result = classify_intent(
                msg_text or "",
                history=[{"direction": "outgoing", "message_text": "(prev)"},
                         {"direction": "incoming", "message_text": "(prev)"}],
                use_llm_fallback=False,  # 离线分析关 LLM 降本
            )
            intent = result.intent
            classifiable += 1
        except Exception as e:
            log.debug("[chat_funnel] classify 失败 id=%s: %s", inc_id, e)
            errors += 1
            continue
        bucket = by_intent.setdefault(intent,
                                       {"incomings": 0, "replied": 0})
        bucket["incomings"] += 1
        if inc_id in replied_ids:
            bucket["replied"] += 1

    # 填 reply_rate
    out_by_intent: Dict[str, Dict[str, Any]] = {}
    for intent, stats in by_intent.items():
        inc = stats["incomings"]
        rep = stats["replied"]
        out_by_intent[intent] = {
            "incomings": inc,
            "replied": rep,
            "reply_rate": round(rep / inc, 3) if inc else 0.0,
        }

    return {
        "total_incomings": total,
        "classifiable": classifiable,
        "by_intent": out_by_intent,
        "errors": errors,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 2. gate_block_distribution (from wa_referral_sent meta)
# ─────────────────────────────────────────────────────────────────────────────

def gate_block_distribution(device_id: Optional[str] = None, *,
                            since_iso: Optional[str] = None) -> Dict[str, Any]:
    """gate 决策分布 — 从 `fb_contact_events.wa_referral_sent.meta` 聚合。

    当前 B 的 wa_referral_sent meta 含 ``channel`` / ``peer_type`` / ``intent``,
    未含 ``gate_level`` / ``gate_reasons`` (避免在 _ai_reply_and_send 内过度
    记录)。这里返回可从 meta 推断的分布:
      * by_channel: line/whatsapp/telegram/wechat/... 各渠道引流数
      * by_peer_type: friend/stranger 引流数
      * by_intent_at_referral: 触发引流时的意图分布

    Phase 5 未 merge (list_contact_events_by_peer 不存在) 时返 empty shell。
    """
    try:
        from src.host.fb_store import _connect as _c
    except Exception:
        return {"by_channel": {}, "by_peer_type": {},
                "by_intent_at_referral": {},
                "note": "fb_store not importable"}

    # 试探 fb_contact_events 是否存在
    try:
        with _connect() as conn:
            conn.execute("SELECT 1 FROM fb_contact_events LIMIT 1")
    except Exception:
        return {"by_channel": {}, "by_peer_type": {},
                "by_intent_at_referral": {},
                "note": "Phase 5 fb_contact_events table not present"}

    where_device, params_dev = ("", [])
    if device_id:
        where_device = " AND device_id = ?"
        params_dev = [device_id]
    where_since, params_since = _iso_bound_clause(since_iso, "detected_at")

    sql = ("SELECT meta_json FROM fb_contact_events"
           " WHERE event_type='wa_referral_sent'"
           + where_device + where_since)
    params = params_dev + params_since

    by_channel: Dict[str, int] = {}
    by_peer_type: Dict[str, int] = {}
    by_intent: Dict[str, int] = {}

    try:
        with _connect() as conn:
            rows = conn.execute(sql, params).fetchall()
    except Exception as e:
        log.debug("[chat_funnel.gate_block] DB 查询失败: %s", e)
        return {"by_channel": {}, "by_peer_type": {},
                "by_intent_at_referral": {},
                "note": f"query failed: {e}"}

    for row in rows:
        meta_json = row[0] or ""
        if not meta_json:
            continue
        try:
            meta = json.loads(meta_json)
        except Exception:
            continue
        ch = meta.get("channel") or "unknown"
        pt = meta.get("peer_type") or "unknown"
        it = meta.get("intent") or "unknown"
        by_channel[ch] = by_channel.get(ch, 0) + 1
        by_peer_type[pt] = by_peer_type.get(pt, 0) + 1
        by_intent[it] = by_intent.get(it, 0) + 1

    return {
        "by_channel": by_channel,
        "by_peer_type": by_peer_type,
        "by_intent_at_referral": by_intent,
        "total_referrals": sum(by_channel.values()),
    }


# ─────────────────────────────────────────────────────────────────────────────
# 3. stranger_conversion_rate
# ─────────────────────────────────────────────────────────────────────────────

def stranger_conversion_rate(device_id: Optional[str] = None, *,
                             since_iso: Optional[str] = None,
                             preset_key: Optional[str] = None) -> Dict[str, Any]:
    """Message Requests (陌生人) 场景的引流转化率。

    分子: peer_type='stranger' 的 incoming 之后有 wa_referral 的 peer 数
    分母: peer_type='stranger' 的 unique incoming peer 数

    输出:
        {
          "stranger_peers": int,       # 陌生人 unique peer 数
          "stranger_replied": int,     # B 回复过的陌生人 peer 数
          "stranger_wa_referred": int, # 走到 wa_referral 的陌生人 peer 数
          "reply_rate": float,
          "referral_rate": float,
        }
    """
    where_device, params_dev = ("", [])
    if device_id:
        where_device = " AND device_id = ?"
        params_dev = [device_id]
    where_since, params_since = _iso_bound_clause(since_iso, "seen_at")
    where_preset, params_pre = _preset_clause(preset_key)

    with _connect() as conn:
        # unique stranger peers (有 incoming 的)
        row = conn.execute(
            "SELECT COUNT(DISTINCT peer_name) FROM facebook_inbox_messages"
            " WHERE peer_type='stranger' AND direction='incoming'"
            + where_device + where_since + where_preset,
            params_dev + params_since + params_pre,
        ).fetchone()
        stranger_peers = int(row[0]) if row else 0

        # B 回过的
        row = conn.execute(
            "SELECT COUNT(DISTINCT peer_name) FROM facebook_inbox_messages"
            " WHERE peer_type='stranger' AND direction='outgoing'"
            " AND ai_decision IN ('reply','wa_referral')"
            + where_device + where_since + where_preset,
            params_dev + params_since + params_pre,
        ).fetchone()
        replied = int(row[0]) if row else 0

        # 引流过的
        row = conn.execute(
            "SELECT COUNT(DISTINCT peer_name) FROM facebook_inbox_messages"
            " WHERE peer_type='stranger' AND direction='outgoing'"
            " AND ai_decision='wa_referral'"
            + where_device + where_since + where_preset,
            params_dev + params_since + params_pre,
        ).fetchone()
        referred = int(row[0]) if row else 0

    return {
        "stranger_peers": stranger_peers,
        "stranger_replied": replied,
        "stranger_wa_referred": referred,
        "reply_rate": round(replied / stranger_peers, 3) if stranger_peers else 0.0,
        "referral_rate": round(referred / stranger_peers, 3) if stranger_peers else 0.0,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 4. intent_source_coverage (P9 预留, 本 PR 给最小可用版本)
# ─────────────────────────────────────────────────────────────────────────────

def intent_source_coverage(device_id: Optional[str] = None, *,
                           since_iso: Optional[str] = None,
                           sample_limit: int = 500) -> Dict[str, Any]:
    """跑一批 incoming 看 chat_intent.classify_intent 的 source 分布
    (rule / llm / fallback)。

    采样模式(不全量): 取最近 ``sample_limit`` 条 incoming 离线分类,
    计 rule vs llm vs fallback 占比。运营可据此决定:
    * rule > 70% 健康
    * 40-70% 建议扩正则多语言规则
    * < 40% chat_intent 模块退化, 需审计

    ``use_llm_fallback`` 关闭, 因为离线分析不应烧 token (成本高且
    不可复现)。
    """
    try:
        from src.ai.chat_intent import classify_intent
    except Exception:
        return {"total_sampled": 0, "by_source": {},
                "note": "P4 chat_intent not available"}

    where_device, params_dev = ("", [])
    if device_id:
        where_device = " AND device_id = ?"
        params_dev = [device_id]
    where_since, params_since = _iso_bound_clause(since_iso, "seen_at")

    with _connect() as conn:
        rows = conn.execute(
            "SELECT message_text FROM facebook_inbox_messages"
            " WHERE direction='incoming'"
            + where_device + where_since +
            " ORDER BY id DESC LIMIT ?",
            params_dev + params_since + [int(sample_limit)],
        ).fetchall()

    # 传 stub 非空 history 防止每条都被判 opening (离线分析不重建真实 history)
    stub_history = [
        {"direction": "outgoing", "message_text": "(prev greeting)"},
        {"direction": "incoming", "message_text": "(prev incoming)"},
    ]
    by_source: Dict[str, int] = {}
    for row in rows:
        msg = row[0] or ""
        if not msg.strip():
            continue
        try:
            r = classify_intent(msg, history=stub_history,
                                use_llm_fallback=False)
            by_source[r.source] = by_source.get(r.source, 0) + 1
        except Exception:
            continue

    total = sum(by_source.values())
    rates = {k: round(v / total, 3) for k, v in by_source.items()} if total else {}
    return {
        "total_sampled": total,
        "by_source": by_source,
        "rates": rates,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 4b. intent_health_report (P9 — 基于 intent_source_coverage 做阈值告警)
# ─────────────────────────────────────────────────────────────────────────────

def intent_health_report(device_id: Optional[str] = None, *,
                         since_iso: Optional[str] = None,
                         sample_limit: int = 500,
                         rule_threshold_healthy: float = 0.60,
                         rule_threshold_degraded: float = 0.40
                         ) -> Dict[str, Any]:
    """P9 轻量健康报告 — 根据 rule_source 比例分档给出建议。

    档位:
      * ``healthy``      rule >= rule_threshold_healthy (默认 60%)
      * ``needs_rules``  rule_threshold_degraded <= rule < rule_threshold_healthy
      * ``degraded``     rule < rule_threshold_degraded (默认 40%)

    设计思路: **不做 LLM 硬限流**(太粗暴), 而是用 rule coverage 作为反馈
    信号——coverage 低说明 chat_intent 规则漏覆盖, 应该扩 `_REFERRAL_RE` /
    `_BUYING_RE` 多语言规则, 而不是限制 LLM 调用。

    输出:
        {
          "health": "healthy" | "needs_rules" | "degraded" | "no_data",
          "rule_coverage": float (0-1),
          "llm_coverage": float,
          "fallback_coverage": float,
          "recommendation": str (人类可读的行动建议),
          "sample": {...}  # 同 intent_source_coverage 输出, 供深入调试
        }
    """
    sample = intent_source_coverage(
        device_id=device_id, since_iso=since_iso, sample_limit=sample_limit)
    total = sample.get("total_sampled", 0)
    rates = sample.get("rates", {})
    rule_cov = float(rates.get("rule", 0.0))
    llm_cov = float(rates.get("llm", 0.0))
    fallback_cov = float(rates.get("fallback", 0.0))

    if total == 0:
        health = "no_data"
        rec = "样本不足,需累积 incoming 消息后再评估"
    elif rule_cov >= rule_threshold_healthy:
        health = "healthy"
        rec = (f"rule 命中率 {rule_cov:.0%} ≥ {rule_threshold_healthy:.0%},"
               f" chat_intent 规则覆盖健康")
    elif rule_cov >= rule_threshold_degraded:
        health = "needs_rules"
        # 判断哪类意图最多 fallback → 推 LLM 的方向反推
        low_intents = [k for k, v in sample.get("by_source", {}).items()
                       if k in ("fallback", "llm")]
        rec = (f"rule 命中率 {rule_cov:.0%} 偏低, 建议扩 chat_intent 的"
               f" 多语言正则规则 (ja/zh/en/it). fallback+llm={llm_cov + fallback_cov:.0%}"
               f" 里大概率有可 rule 化的模式")
    else:
        health = "degraded"
        rec = (f"rule 命中率 {rule_cov:.0%} < {rule_threshold_degraded:.0%},"
               f" chat_intent 可能退化 — 审计 _REFERRAL_RE / _BUYING_RE /"
               f" _CLOSING_RE / _COLD_TOKENS 覆盖范围; 或者最近对话文化变迁"
               f" (新表情/新术语) 需要人工扩词表")

    return {
        "health": health,
        "rule_coverage": rule_cov,
        "llm_coverage": llm_cov,
        "fallback_coverage": fallback_cov,
        "recommendation": rec,
        "sample": sample,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 5. A 的 greeting_reply_rate 消费 (可选)
# ─────────────────────────────────────────────────────────────────────────────

def a_greeting_reply_rate(device_id: Optional[str] = None, *,
                          since_iso: Optional[str] = None) -> Dict[str, Any]:
    """消费 A 的 Phase 5 API /facebook/greeting-reply-rate (如果 merge 了)。

    直接调 A 的 ``fb_store.get_greeting_reply_rate_by_template`` 函数
    (同库 Python 调用, 不走 HTTP)。A 未 merge 时 graceful 返 empty shell。

    这个函数**不重复实现模板 A/B 回复率**, 让 A 维护权威口径, B 作为
    消费方。
    """
    try:
        from src.host.fb_store import get_greeting_reply_rate_by_template as fn
    except Exception:
        return {"templates": {}, "note": "A Phase 5 not merged"}
    try:
        return fn(device_id=device_id, since_iso=since_iso)
    except TypeError:
        # 签名可能不同, 尝试位置参数
        try:
            return fn(device_id, since_iso)
        except Exception as e:
            return {"templates": {}, "note": f"call failed: {e}"}
    except Exception as e:
        return {"templates": {}, "note": f"call failed: {e}"}


# ─────────────────────────────────────────────────────────────────────────────
# 一站式入口
# ─────────────────────────────────────────────────────────────────────────────

def get_funnel_metrics_extended(device_id: Optional[str] = None, *,
                                since_iso: Optional[str] = None,
                                preset_key: Optional[str] = None,
                                include_intent_coverage: bool = True,
                                include_greeting_template: bool = True
                                ) -> Dict[str, Any]:
    """一站式扩展漏斗 = fb_store.get_funnel_metrics (基础) + B 侧扩展字段。

    返回 dict 合并基础 stage_* + 以下扩展 keys:
      * reply_rate_by_intent
      * stranger_conversion_rate
      * gate_block_distribution (Phase 5 merge 后才有数据)
      * intent_source_coverage  (P9 预览)
      * greeting_reply_rate     (A Phase 5 的权威数据, 可选)

    Args:
        device_id: 过滤设备
        since_iso: 过滤时间下限 (ISO UTC)
        preset_key: 过滤预设
        include_intent_coverage: 是否跑 intent 分类采样 (有成本但很轻量)
        include_greeting_template: 是否调 A 的模板回复率 API
    """
    out: Dict[str, Any] = {}

    # 1. 基础漏斗 (共享区, 两边都认的权威源)
    try:
        from src.host.fb_store import get_funnel_metrics
        base = get_funnel_metrics(device_id=device_id,
                                   since_iso=since_iso,
                                   preset_key=preset_key)
        out.update({k: v for k, v in base.items()
                    if str(k).startswith("stage_")})
        out["base_metrics"] = base  # 完整 copy 便于 debug
    except Exception as e:
        log.debug("[chat_funnel] 基础漏斗获取失败: %s", e)
        out["base_metrics_error"] = str(e)

    # 2. B 侧扩展
    out["reply_rate_by_intent"] = reply_rate_by_intent(
        device_id=device_id, since_iso=since_iso, preset_key=preset_key)
    out["stranger_conversion_rate"] = stranger_conversion_rate(
        device_id=device_id, since_iso=since_iso, preset_key=preset_key)
    out["gate_block_distribution"] = gate_block_distribution(
        device_id=device_id, since_iso=since_iso)

    if include_intent_coverage:
        out["intent_source_coverage"] = intent_source_coverage(
            device_id=device_id, since_iso=since_iso)
        out["intent_health"] = intent_health_report(
            device_id=device_id, since_iso=since_iso)

    if include_greeting_template:
        out["greeting_reply_rate"] = a_greeting_reply_rate(
            device_id=device_id, since_iso=since_iso)

    return out
