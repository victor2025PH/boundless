# -*- coding: utf-8 -*-
"""AI 价值周报聚合核心（P1-3 首件，2026-08-06）。

背景：``/api/report/weekly``（F4，早期 Phase 产物）只聚合 KB 命中率+反馈两项；
2026-07/08 长出来的真正价值面（AI 拟稿/自动投递/人工通过、主动触达及回复率、
出站沟通量）散落在十几个垂直周报 CLI（proactive_review / winback_report /
translation_eval_weekly …）里，没有一张「AI 本周替你干了什么」的总账。本模块
把总账做成纯函数聚合，路由与周报推送循环共用同一口径。

口径纪律（改动前先读）：
- **只读持久层**（reply_drafts / outreach_log / messages）——进程内计数器重启
  即清零，周窗口必须落库口径；进程口径的指标（幂等去重命中、raced 拦截等）
  刻意不入周报，避免「重启过就归零」的误导性读数。
- **不猜枚举**：``status`` / ``autopilot_level`` 原样 GROUP BY 输出（口径自解释，
  谁看报表谁看得到状态原名——autosend 落库 real_action=approve 这类映射不在
  这里二次翻译）；派生指标只用稳健列（created_at / decided_at / sent_at 时间窗）。
- 触达回复判定与 ``InboxStore.outreach_response_stats`` 同款（触达后窗口内该会话
  首条入站消息），但不带 batch 过滤——周报是全量总账；同样剔除 bot 会话。

访问模式：与 report_routes 读 kb_store 同惯例，经 store 的 ``_lock``/``_conn``
做只读查询（SELECT-only；任何异常返回空段，绝不让周报拖垮调用方）。
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

_WEEK_S = 7 * 86400.0
_DAY_S = 86400.0
# 触达→回复的判定窗口（天）：与 proactive 侧 response_window_days 默认一致
_RESPONSE_WINDOW_S = 7 * 86400.0


def _draft_window(conn, lo: float, hi: float) -> Dict[str, Any]:
    """reply_drafts 在 [lo, hi) 窗口的聚合（创建口径 + 处置口径 + 真发口径）。"""
    created = conn.execute(
        "SELECT COUNT(*) FROM reply_drafts WHERE created_at >= ? AND created_at < ?",
        (lo, hi)).fetchone()[0]
    by_status = {
        str(r[0] or ""): int(r[1])
        for r in conn.execute(
            "SELECT status, COUNT(*) FROM reply_drafts "
            "WHERE decided_at > 0 AND decided_at >= ? AND decided_at < ? "
            "GROUP BY status ORDER BY COUNT(*) DESC", (lo, hi)).fetchall()
    }
    by_level = {
        str(r[0] or ""): int(r[1])
        for r in conn.execute(
            "SELECT autopilot_level, COUNT(*) FROM reply_drafts "
            "WHERE created_at >= ? AND created_at < ? "
            "GROUP BY autopilot_level ORDER BY COUNT(*) DESC", (lo, hi)).fetchall()
    }
    sent = conn.execute(
        "SELECT COUNT(*) FROM reply_drafts WHERE sent_at > 0 AND sent_at >= ? AND sent_at < ?",
        (lo, hi)).fetchone()[0]
    return {"created": int(created), "resolved_by_status": by_status,
            "created_by_level": by_level, "sent": int(sent)}


def _outreach_window(conn, lo: float, hi: float,
                     not_bot_sql: str = "") -> Dict[str, Any]:
    """outreach_log 在窗口内的触达量 + 回复率（触达后 7 天内该会话首条入站）。"""
    sql = (
        "SELECT o.ts AS sent_ts, "
        "(SELECT MIN(m.ts) FROM messages m "
        " WHERE m.conversation_id = o.conversation_id "
        "   AND m.direction = 'in' AND m.ts > o.ts) AS reply_ts "
        "FROM outreach_log o WHERE o.status = 'sent' AND o.ts >= ? AND o.ts < ?"
        + (not_bot_sql or ""))
    rows = conn.execute(sql, (lo, hi)).fetchall()
    sent = len(rows)
    responded = sum(
        1 for r in rows
        if r["reply_ts"] is not None
        and float(r["reply_ts"]) - float(r["sent_ts"]) <= _RESPONSE_WINDOW_S)
    by_batch: Dict[str, int] = {}
    # 与主计数同口径（含 bot 剔除）——首跑实数曾出现分布合计 159 > 主计数 157
    # 的口径分裂（两条 bot 会话触达只在一边被剔），周报数字必须自洽。
    for row in conn.execute(
            "SELECT o.batch_id FROM outreach_log o "
            "WHERE o.status = 'sent' AND o.ts >= ? AND o.ts < ?" + (not_bot_sql or ""),
            (lo, hi)).fetchall():
        prefix = str(row[0] or "").split(":", 1)[0] or "(none)"
        by_batch[prefix] = by_batch.get(prefix, 0) + 1
    return {"sent": sent, "responded": responded,
            "response_rate": round(responded / sent * 100.0, 1) if sent else 0.0,
            "by_batch_prefix": dict(sorted(by_batch.items(), key=lambda kv: -kv[1]))}


def _outbound_window(conn, lo: float, hi: float) -> Dict[str, Any]:
    out_n = conn.execute(
        "SELECT COUNT(*) FROM messages WHERE direction = 'out' AND ts >= ? AND ts < ?",
        (lo, hi)).fetchone()[0]
    in_n = conn.execute(
        "SELECT COUNT(*) FROM messages WHERE direction = 'in' AND ts >= ? AND ts < ?",
        (lo, hi)).fetchone()[0]
    return {"messages_out": int(out_n), "messages_in": int(in_n)}


def _goals_window(goal_store: Any, lo: float, hi: float) -> Dict[str, Any]:
    """营销目标终态窗口段（P2 2026-08-09）。store 侧自兜异常返回全零。"""
    return dict(goal_store.outcome_counts(lo, hi))


def _cases_window(trend_store: Any, lo: float, hi: float) -> Dict[str, Any]:
    """案例趋势窗口段（P3 2026-08-09）。读跨重启日聚合，演练静音写口已不计入。"""
    try:
        return dict(trend_store.window_totals(lo, hi))
    except Exception:
        return {"opened": 0, "closed": 0, "by_source": {},
                "closed_by_source": {}, "closed_by_resolution": {}}


def build_weekly_value(store: Any, *, goal_store: Any = None,
                       case_trend_store: Any = None,
                       learner: Any = None,
                       now: Optional[float] = None) -> Dict[str, Any]:
    """7 天 AI 价值总账（本周 + 上周环比）。store=InboxStore；异常返回 {}（软失败）。

    ``goal_store``（P2）：营销目标段的数据源——显式传入优先；缺省时**只探测
    进程内既有单例**（``peek_goal_store``，绝不新建：新建会凭空造一个 :memory:
    空库把「零目标」误报成事实）。goals 未启用/单例未初始化 → 报表没有 goals
    段，与旧行为逐字节一致。生产上 P0 的定时结算扫描每分钟都会初始化单例，
    peek 必然温热；测试要密闭就显式传参。

    ``case_trend_store``（P3）：案例立案/结案段——同样 peek 既有单例，绝不新建。

    ``learner``（融合 P2 2026-08-16）：学习队列段（本周入库知识/覆盖提问/待审
    积压）——同 peek 纪律（``peek_daily_learner``）；两周全零且无积压不出段。
    """
    try:
        t = float(now if now is not None else time.time())
        lo_tw, lo_lw = t - _WEEK_S, t - 2 * _WEEK_S
        not_bot = str(getattr(store, "_NOT_BOT_PEER_SQL", "") or "")
        with store._lock:  # noqa: SLF001 —— 与 report_routes 读 kb_store._conn 同惯例
            conn = store._conn
            tw = {
                "drafts": _draft_window(conn, lo_tw, t),
                "outreach": _outreach_window(conn, lo_tw, t, not_bot),
                "traffic": _outbound_window(conn, lo_tw, t),
            }
            lw = {
                "drafts": _draft_window(conn, lo_lw, lo_tw),
                "outreach": _outreach_window(conn, lo_lw, lo_tw, not_bot),
                "traffic": _outbound_window(conn, lo_lw, lo_tw),
            }
        try:
            gs = goal_store
            if gs is None:
                from src.companion.goals.store import peek_goal_store
                gs = peek_goal_store()
            if gs is not None:
                g_tw = _goals_window(gs, lo_tw, t)
                g_lw = _goals_window(gs, lo_lw, lo_tw)
                # 两周皆零＝该部署没在用目标（或本周期没动静）——不塞空段占版面
                if any(g_tw.values()) or any(g_lw.values()):
                    tw["goals"] = g_tw
                    lw["goals"] = g_lw
        except Exception:
            pass                    # 目标段任何失败不拖垮周报主体
        try:
            cts = case_trend_store
            if cts is None:
                from src.utils.case_trend_store import peek_case_trend_store
                cts = peek_case_trend_store()
            if cts is not None:
                c_tw = _cases_window(cts, lo_tw, t)
                c_lw = _cases_window(cts, lo_lw, lo_tw)
                if (c_tw.get("opened") or c_tw.get("closed")
                        or c_lw.get("opened") or c_lw.get("closed")):
                    tw["cases"] = c_tw
                    lw["cases"] = c_lw
        except Exception:
            pass
        # 融合 P2：学习队列段——AI 本周学会了什么（入库知识/覆盖提问/待审积压）
        try:
            ln = learner
            if ln is None:
                from src.utils.daily_learner import peek_daily_learner
                ln = peek_daily_learner()
            if ln is not None:
                l_tw = dict(ln.weekly_window(lo_tw, t))
                l_lw = dict(ln.weekly_window(lo_lw, lo_tw))
                if (l_tw.get("approved") or l_lw.get("approved")
                        or l_tw.get("pending")):
                    tw["learner"] = l_tw
                    lw["learner"] = l_lw
        except Exception:
            pass                    # 学习段任何失败不拖垮周报主体
        return {"this_week": tw, "last_week": lw,
                "text_lines": weekly_value_lines(tw, lw)}
    except Exception:
        return {}


# ── WP-3 日窗口径（2026-08-17）────────────────────────────────────────────────
# 复用周版同一套窗口 SQL（_draft/_outreach/_outbound 本就按 [lo,hi) 参数化），
# 口径注释对齐周版：剔 bot、触达回复判定同源——首跑口径分裂的教训（by_batch
# 159 vs 主计数 157）已写在 _outreach_window 里，日窗自动继承同一 SQL。
# 窗口语义＝滚动 24h（与周版滚动 7d 同哲学，不按日历日切）：好处是
# 「7 个连续日窗平铺 == 周窗」的守恒关系精确成立（tests 用它钉口径一致性）。

def build_daily_value(store: Any, *, now: Optional[float] = None) -> Dict[str, Any]:
    """24h AI 价值日账（近 24h + 前 24h 环比）。store=InboxStore；异常返回 {}。

    刻意只含 drafts/outreach/traffic 三段（老板日报的英雄数字面）；goals/cases/
    learner 属周叙事节奏，日窗噪声大，留在周版。text_lines 复用周版措辞
    （环比文案窗口无关）——但**页面消费方别直接渲染它**（含「拟稿/草稿链」
    工程词，规格禁进 /workspace/boss 页；页面用自己的 bp_* 人话词条拼数字，
    text_lines 只服务推送/导出等运维面）。
    """
    try:
        t = float(now if now is not None else time.time())
        lo_td, lo_yd = t - _DAY_S, t - 2 * _DAY_S
        not_bot = str(getattr(store, "_NOT_BOT_PEER_SQL", "") or "")
        with store._lock:  # noqa: SLF001 —— 与周版同惯例
            conn = store._conn
            td = {
                "drafts": _draft_window(conn, lo_td, t),
                "outreach": _outreach_window(conn, lo_td, t, not_bot),
                "traffic": _outbound_window(conn, lo_td, t),
            }
            yd = {
                "drafts": _draft_window(conn, lo_yd, lo_td),
                "outreach": _outreach_window(conn, lo_yd, lo_td, not_bot),
                "traffic": _outbound_window(conn, lo_yd, lo_td),
            }
        return {"today": td, "yesterday": yd,
                "text_lines": weekly_value_lines(td, yd)}
    except Exception:
        return {}


def daily_series(store: Any, *, days: int = 7,
                 now: Optional[float] = None) -> List[Dict[str, Any]]:
    """近 N 个滚动日窗的关键计数序列（旧→新；/workspace/boss 趋势条数据源）。

    每窗一组 {lo, hi, drafts_created, drafts_sent, outreach_sent,
    messages_out, messages_in}。窗口对齐 ``now``（第 i 窗=[now-(i+1)d, now-i*d)），
    与 build_daily_value/周版同一时钟——sum(7 日窗) == 周窗 恒成立。异常返回 []。
    """
    try:
        n = max(1, min(int(days or 7), 31))
        t = float(now if now is not None else time.time())
        not_bot = str(getattr(store, "_NOT_BOT_PEER_SQL", "") or "")
        out: List[Dict[str, Any]] = []
        with store._lock:  # noqa: SLF001
            conn = store._conn
            for i in range(n - 1, -1, -1):
                hi = t - i * _DAY_S
                lo = hi - _DAY_S
                d = _draft_window(conn, lo, hi)
                o = _outreach_window(conn, lo, hi, not_bot)
                tr = _outbound_window(conn, lo, hi)
                out.append({
                    "lo": lo, "hi": hi,
                    "drafts_created": int(d.get("created") or 0),
                    "drafts_sent": int(d.get("sent") or 0),
                    "outreach_sent": int(o.get("sent") or 0),
                    "outreach_responded": int(o.get("responded") or 0),
                    "messages_out": int(tr.get("messages_out") or 0),
                    "messages_in": int(tr.get("messages_in") or 0),
                })
        return out
    except Exception:
        return []


def resolve_minutes_per_reply(config: Optional[Dict[str, Any]]) -> float:
    """省时换算系数（分钟/条）：``ops.value_report.manual_minutes_per_reply``。

    这是销售话术数字（规格明示：必须让客户按自己团队实情调），默认 3、
    夹 [0.5, 60] 防误配出荒谬账。
    """
    try:
        v = float((((config or {}).get("ops") or {}).get("value_report") or {})
                  .get("manual_minutes_per_reply", 3.0))
    except Exception:
        v = 3.0
    return max(0.5, min(v, 60.0))


def estimate_saved_minutes(sections: Dict[str, Any],
                           minutes_per_reply: float = 3.0) -> float:
    """「省了约 N 小时人工」的分钟数：AI 经草稿链真发条数 × 每条人工均时。

    刻意用 ``drafts.sent`` 而非 ``messages_out``：出站总量含坐席手动消息
    （不该记 AI 的功）。代价＝协议直发链的自动回复未计入（那条链不落草稿行）
    ——宁少报不虚报；出站归因列（区分 AI/人工发出）是后续改进项。
    """
    try:
        sent = int((sections.get("drafts") or {}).get("sent") or 0)
        return round(sent * float(minutes_per_reply), 1)
    except Exception:
        return 0.0


def _pct_delta(cur: float, prev: float) -> str:
    if prev <= 0:
        return "（上周无数据）" if cur else ""
    d = round((cur - prev) / prev * 100.0)
    return f"（环比 {'+' if d >= 0 else ''}{d}%）"


def weekly_value_lines(tw: Dict[str, Any], lw: Dict[str, Any]) -> List[str]:
    """周报可读摘要行（中文，与 F4 text_summary 风格一致；推送循环同源复用）。"""
    lines: List[str] = []
    d, dl = tw.get("drafts", {}), lw.get("drafts", {})
    if d:
        st = d.get("resolved_by_status", {})
        st_txt = " / ".join(f"{k} {v}" for k, v in list(st.items())[:4]) or "无处置"
        lines.append(
            f"AI 拟稿 {d.get('created', 0)} 条{_pct_delta(d.get('created', 0), dl.get('created', 0))}，"
            f"经草稿链发出 {d.get('sent', 0)} 条；处置分布：{st_txt}")
    o, ol = tw.get("outreach", {}), lw.get("outreach", {})
    if o:
        lines.append(
            f"主动触达 {o.get('sent', 0)} 次{_pct_delta(o.get('sent', 0), ol.get('sent', 0))}，"
            f"7 天内获回复 {o.get('responded', 0)} 次（回复率 {o.get('response_rate', 0)}%）")
    tr, trl = tw.get("traffic", {}), lw.get("traffic", {})
    if tr:
        lines.append(
            f"出站消息 {tr.get('messages_out', 0)} 条"
            f"{_pct_delta(tr.get('messages_out', 0), trl.get('messages_out', 0))}，"
            f"入站 {tr.get('messages_in', 0)} 条")
    # P2 2026-08-09：营销目标段（有段才出行——没在用目标的部署一行不多）
    g, gl = tw.get("goals", {}), lw.get("goals", {})
    if g:
        won = int(g.get("won", 0))
        amt = float(g.get("won_amount", 0) or 0)
        miss = int(g.get("failed", 0)) + int(g.get("expired", 0))
        seg = (f"目标达成 {g.get('done', 0)} 个"
               f"{_pct_delta(g.get('done', 0), gl.get('done', 0))}")
        if won:
            seg += f"，赢单 {won} 单" + (f"（${amt}）" if amt else "")
        if miss:
            seg += f"；未达成 {miss} 个"
        lines.append(seg)
    # P3：案例跟进段（持久趋势口径；演练立案已静音不进趋势）
    c, cl = tw.get("cases", {}), lw.get("cases", {})
    if c:
        opened = int(c.get("opened", 0) or 0)
        closed = int(c.get("closed", 0) or 0)
        src = c.get("by_source") or {}
        top_src = " / ".join(
            f"{k} {v}" for k, v in sorted(
                src.items(), key=lambda kv: -int(kv[1] or 0))[:3]
        ) if src else ""
        res = c.get("closed_by_resolution") or {}
        top_res = " / ".join(
            f"{k} {v}" for k, v in sorted(
                res.items(), key=lambda kv: -int(kv[1] or 0))[:3]
        ) if res else ""
        seg = (f"案例立案 {opened} 条"
               f"{_pct_delta(opened, int(cl.get('opened', 0) or 0))}，"
               f"结案 {closed} 条")
        if top_src:
            seg += f"；来源 {top_src}"
        if top_res:
            seg += f"；结案 {top_res}"
        lines.append(seg)
    # 融合 P2：学习队列段（AI 本周学会了什么——ROI 叙事的缺口补齐）
    n, nl = tw.get("learner", {}), lw.get("learner", {})
    if n:
        approved = int(n.get("approved", 0) or 0)
        seg = (f"学习入库 {approved} 条新知识"
               f"{_pct_delta(approved, int(nl.get('approved', 0) or 0))}")
        cov = int(n.get("coverage_hits", 0) or 0)
        if cov:
            seg += f"，覆盖此前被问过 {cov} 次的缺口"
        pend = int(n.get("pending", 0) or 0)
        if pend:
            seg += f"；待审草稿 {pend} 条"
        lines.append(seg)
    return lines
