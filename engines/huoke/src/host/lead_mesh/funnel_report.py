# -*- coding: utf-8 -*-
"""Phase 8 准备: lead_journey 漏斗分析 library (2026-04-24).

从 ``lead_journey`` 表汇总 add_friend → greeting 漏斗, 按 persona_key / via /
reason 分组, 计算转化率.

单独成 lib 是因为:
  * 单测友好 (不依赖 CLI)
  * 可被 Dashboard API / scheduled job 复用

核心事件 (2026-04-23 Phase 6.A 后的 schema):
  * friend_requested         data: {source, preset_key, note_len, persona_key}
  * friend_request_risk      data: {source, ...}
  * greeting_sent            data: {via: inline_profile_message | messenger_fallback,
                                    template_id, persona_key}
  * greeting_blocked         data: {reason: no_message_button / peer_already_handed_off /
                                           cap_hit / phase_blocked / template_empty /
                                           messenger_not_installed / search_miss / ...}
  * handoff_created          data: {channel, receiver_account_key}
  * handoff_blocked          data: {reason: peer_cooldown}
"""
from __future__ import annotations

import datetime as _dt
import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from src.host.database import _connect


@dataclass
class FunnelStats:
    """一个时间窗口的 A 端漏斗统计."""
    window_days: int = 0
    since_iso: str = ""

    # 计数
    total_extracted: int = 0
    total_friend_requested: int = 0
    total_friend_request_risk: int = 0
    total_greeting_sent: int = 0
    total_greeting_blocked: int = 0

    # greeting_sent.via 分布
    greeting_via_inline: int = 0
    greeting_via_fallback: int = 0
    greeting_via_unknown: int = 0

    # greeting_blocked.reason 分布 (Top N)
    blocked_reasons: Dict[str, int] = field(default_factory=dict)

    # 按 persona 分组的 friend_requested 数 (帮运营看哪类 persona 贡献多)
    per_persona_friend_requested: Dict[str, int] = field(default_factory=dict)

    # handoff 相关 (Lead Mesh 视角)
    total_handoff_created: int = 0
    total_handoff_blocked: int = 0

    # ── 衍生 rate ─────────────────────────────────────────────────
    @property
    def rate_greet_after_friend(self) -> float:
        """已发好友请求中, 有 greeting_sent 的占比."""
        if self.total_friend_requested == 0:
            return 0.0
        return self.total_greeting_sent / self.total_friend_requested

    @property
    def rate_inline_vs_fallback(self) -> float:
        """greeting 里 inline 路径占比 (剩下的是 fallback / unknown)."""
        total = (self.greeting_via_inline + self.greeting_via_fallback
                    + self.greeting_via_unknown)
        if total == 0:
            return 0.0
        return self.greeting_via_inline / total

    @property
    def top_blocked_reason(self) -> str:
        """被挡最多的原因 (帮运营看瓶颈在哪)."""
        if not self.blocked_reasons:
            return ""
        return max(self.blocked_reasons.items(), key=lambda kv: kv[1])[0]

    def to_dict(self) -> Dict[str, Any]:
        d = {
            "window_days": self.window_days,
            "since_iso": self.since_iso,
            "total_extracted": self.total_extracted,
            "total_friend_requested": self.total_friend_requested,
            "total_friend_request_risk": self.total_friend_request_risk,
            "total_greeting_sent": self.total_greeting_sent,
            "total_greeting_blocked": self.total_greeting_blocked,
            "greeting_via_inline": self.greeting_via_inline,
            "greeting_via_fallback": self.greeting_via_fallback,
            "greeting_via_unknown": self.greeting_via_unknown,
            "blocked_reasons": dict(self.blocked_reasons),
            "per_persona_friend_requested": dict(self.per_persona_friend_requested),
            "total_handoff_created": self.total_handoff_created,
            "total_handoff_blocked": self.total_handoff_blocked,
            "rate_greet_after_friend": round(self.rate_greet_after_friend, 3),
            "rate_inline_vs_fallback": round(self.rate_inline_vs_fallback, 3),
            "top_blocked_reason": self.top_blocked_reason,
        }
        return d


def _iso_since(days: int) -> str:
    dt = _dt.datetime.utcnow() - _dt.timedelta(days=int(days))
    # lead_journey.at 格式 "YYYY-MM-DD HH:MM:SS" (SQLite datetime('now'))
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _validate_date(s: str) -> str:
    """校验 YYYY-MM-DD 格式, 不合法返回空字符串 (caller 降级到 days)."""
    if not s:
        return ""
    try:
        d = _dt.datetime.strptime(s.strip(), "%Y-%m-%d")
        return d.strftime("%Y-%m-%d")
    except Exception:
        return ""


def compute_funnel(days: int = 7,
                    actor: Optional[str] = None,
                    date: Optional[str] = None) -> FunnelStats:
    """从 lead_journey 表汇总漏斗. actor 过滤 agent_a/agent_b, 空则不限.

    Args:
        days: 近 N 天窗口 (date 未提供时生效)
        actor: actor 过滤
        date: YYYY-MM-DD 单日过滤 (Phase 8g Dashboard 下钻用, 优先于 days).
              非法格式降级到 days 窗口.

    所有 A 端关心的事件 1 次 SQL 全部拿完, 再在 Python 分桶. 原则: 扫表只 1 次,
    其他都是内存聚合. 表规模小 (<10k rows) 时远比多次 SQL 快.
    """
    # date 优先, 降级 days
    valid_date = _validate_date(date) if date else ""
    if valid_date:
        since_iso = valid_date + " 00:00:00"
        until_iso = valid_date + " 23:59:59"
        stats = FunnelStats(window_days=1, since_iso=since_iso)
    else:
        stats = FunnelStats(window_days=int(days),
                              since_iso=_iso_since(days))
        until_iso = ""

    sql = ("SELECT actor, action, data_json FROM lead_journey"
            " WHERE at >= ?")
    params: list = [stats.since_iso]
    if until_iso:
        sql += " AND at <= ?"
        params.append(until_iso)
    if actor:
        sql += " AND actor = ?"
        params.append(actor)

    try:
        with _connect() as conn:
            rows = conn.execute(sql, params).fetchall()
    except Exception:
        return stats

    for row in rows:
        # sqlite3 默认 row 是 tuple: (actor, action, data_json)
        act = row[1] or ""
        data_raw = row[2] or "{}"
        try:
            data = json.loads(data_raw)
        except Exception:
            data = {}

        if act == "extracted":
            stats.total_extracted += 1
        elif act == "friend_requested":
            stats.total_friend_requested += 1
            persona = str(data.get("persona_key") or "(unknown)")
            stats.per_persona_friend_requested[persona] = (
                stats.per_persona_friend_requested.get(persona, 0) + 1)
        elif act == "friend_request_risk":
            stats.total_friend_request_risk += 1
        elif act == "greeting_sent":
            stats.total_greeting_sent += 1
            via = str(data.get("via") or "").lower()
            if via == "inline_profile_message":
                stats.greeting_via_inline += 1
            elif via == "messenger_fallback":
                stats.greeting_via_fallback += 1
            else:
                stats.greeting_via_unknown += 1
        elif act == "greeting_blocked":
            stats.total_greeting_blocked += 1
            reason = str(data.get("reason") or "unknown")
            stats.blocked_reasons[reason] = (
                stats.blocked_reasons.get(reason, 0) + 1)
        elif act == "handoff_created":
            stats.total_handoff_created += 1
        elif act == "handoff_blocked":
            stats.total_handoff_blocked += 1

    return stats


def compute_funnel_timeseries(days: int = 7,
                                actor: Optional[str] = None
                                ) -> List[Dict[str, Any]]:
    """近 N 天按日分桶的漏斗时序 (Phase 8e sparkline 用).

    Returns:
        list 按 date 升序, 每项 {date: YYYY-MM-DD, friend_req, greeting_sent, blocked}.
        **缺失日填 0** (避免 sparkline 断线).

    实现: 1 次 SQL 按 substr(at, 1, 10) + action 分组 COUNT,
          Python 侧填 0 + 按 date 对齐.
    """
    days = max(1, min(90, int(days)))
    today = _dt.datetime.utcnow().date()
    # 近 N 天: 包括今天, 所以起点是 today - (days-1)
    start_date = today - _dt.timedelta(days=days - 1)
    date_list = [(start_date + _dt.timedelta(days=i)).isoformat()
                  for i in range(days)]
    # 预填 0
    by_date: Dict[str, Dict[str, Any]] = {
        d: {"date": d, "friend_req": 0, "greeting_sent": 0, "blocked": 0}
        for d in date_list
    }

    since_iso = start_date.strftime("%Y-%m-%d 00:00:00")
    sql = ("SELECT substr(at, 1, 10) as day, action, COUNT(*) as n"
            " FROM lead_journey"
            " WHERE at >= ?"
            "   AND action IN ('friend_requested', 'greeting_sent',"
            "                  'greeting_blocked')")
    params: list = [since_iso]
    if actor:
        sql += " AND actor = ?"
        params.append(actor)
    sql += " GROUP BY day, action ORDER BY day ASC"

    try:
        with _connect() as conn:
            rows = conn.execute(sql, params).fetchall()
    except Exception:
        rows = []

    for row in rows:
        day = (row[0] or "")[:10]
        action = row[1] or ""
        n = int(row[2] or 0)
        if day not in by_date:
            continue
        if action == "friend_requested":
            by_date[day]["friend_req"] = n
        elif action == "greeting_sent":
            by_date[day]["greeting_sent"] = n
        elif action == "greeting_blocked":
            by_date[day]["blocked"] = n

    return [by_date[d] for d in date_list]


def list_blocked_peers(reason: str,
                         days: int = 7,
                         limit: int = 50,
                         date: Optional[str] = None) -> List[Dict[str, Any]]:
    """返回被某 reason 挡住的 peer 列表 (点击 Dashboard top_blocked_reason
    子 modal 用). 按最近 blocked 时间倒序, 同 peer 只出 1 行 (含总次数).

    Args:
        reason: 要过滤的 reason (如 "no_message_button")
        days: 时间窗口 (date 未提供时生效)
        limit: 最多返回 N 条 (默认 50)
        date: YYYY-MM-DD 单日过滤 (Phase 8g 下钻用, 优先于 days)

    Returns:
        [{"canonical_id", "last_blocked_at", "n_blocked", "persona_key"}]
    """
    if not reason:
        return []
    valid_date = _validate_date(date) if date else ""
    if valid_date:
        since = valid_date + " 00:00:00"
        until = valid_date + " 23:59:59"
    else:
        since = _iso_since(days)
        until = ""
    # SQLite JSON 解析 Python 侧做, 避免 json_extract 版本要求
    sql = ("SELECT canonical_id, data_json, at FROM lead_journey"
            " WHERE action = 'greeting_blocked' AND at >= ?")
    params: list = [since]
    if until:
        sql += " AND at <= ?"
        params.append(until)
    sql += " ORDER BY at DESC LIMIT ?"
    params.append(int(limit) * 10)
    try:
        with _connect() as conn:
            rows = conn.execute(sql, params).fetchall()
    except Exception:
        return []

    agg: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        cid = row[0] or ""
        try:
            data = json.loads(row[1] or "{}")
        except Exception:
            data = {}
        if (data.get("reason") or "") != reason:
            continue
        at = row[2] or ""
        if cid not in agg:
            agg[cid] = {
                "canonical_id": cid,
                "last_blocked_at": at,  # DESC ordering 下第一次命中就是最近
                "n_blocked": 1,
                "persona_key": str(data.get("persona_key") or ""),
            }
        else:
            agg[cid]["n_blocked"] += 1
        if len(agg) >= int(limit):
            break
    return list(agg.values())


def format_text_report(stats: FunnelStats) -> str:
    """人类友好的控制台输出. 多行 markdown-ish 格式."""
    lines = [
        f"# A 端漏斗报告 (近 {stats.window_days} 天)",
        f"- since: {stats.since_iso}",
        f"- total extracted: {stats.total_extracted}",
        f"- friend_requested: {stats.total_friend_requested}"
        f"  (+{stats.total_friend_request_risk} risk)",
        f"- greeting_sent: {stats.total_greeting_sent}"
        f" (inline={stats.greeting_via_inline}"
        f" fallback={stats.greeting_via_fallback}"
        f" unknown={stats.greeting_via_unknown})",
        f"- greeting_blocked: {stats.total_greeting_blocked}",
        "",
        f"## Conversion",
        f"- rate_greet_after_friend: {stats.rate_greet_after_friend:.1%}",
        f"- rate_inline_vs_fallback: {stats.rate_inline_vs_fallback:.1%}",
        "",
        f"## greeting_blocked 分布 (top reasons)",
    ]
    if not stats.blocked_reasons:
        lines.append("  (无)")
    else:
        ranked = sorted(stats.blocked_reasons.items(),
                          key=lambda kv: -kv[1])
        for r, n in ranked[:8]:
            lines.append(f"  - {r}: {n}")

    lines += ["",
              f"## per-persona friend_requested"]
    if not stats.per_persona_friend_requested:
        lines.append("  (无)")
    else:
        ranked = sorted(stats.per_persona_friend_requested.items(),
                          key=lambda kv: -kv[1])
        for p, n in ranked[:8]:
            lines.append(f"  - {p}: {n}")

    lines += ["",
              f"## Handoff (Lead Mesh)",
              f"- handoff_created: {stats.total_handoff_created}",
              f"- handoff_blocked: {stats.total_handoff_blocked}"]

    return "\n".join(lines)
