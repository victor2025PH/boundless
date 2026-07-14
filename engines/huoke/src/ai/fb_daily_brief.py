# -*- coding: utf-8 -*-
"""Facebook 运营日报 — Sprint 2 P1。

功能:
  1. 拉取过去 24h 的 funnel 指标、群组、好友请求、风控事件、TOP 高分线索
  2. 调 LLM 生成中文 markdown 日报
  3. 缓存到 audit_logs 表(category='fb_daily_brief'),前端可取最近 N 份

输出示例(LLM 产出):
    # Facebook 引流日报 (2026-04-19)
    ## 概览
    - 群组:7 个(新增 2)
    - 群成员打招呼准备:142 人(目标群占 80%)
    - 好友请求:25 发出 / 18 通过(72%)
    ## 风险
    - 002 号机 14:32 触发风控 → 已自动降级
    ## 行动建议
    - 重点跟进:Marco Rossi (S 级,意大利男性,多次互动)
    ...
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def _gather_metrics(device_id: Optional[str] = None,
                    hours: int = 24) -> Dict[str, Any]:
    """汇总过去 N 小时的所有数据。"""
    since = (_dt.datetime.utcnow() - _dt.timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")

    from src.host import fb_store
    funnel = fb_store.get_funnel_metrics(device_id=device_id, since_iso=since)
    groups = fb_store.list_groups(device_id=device_id, status="joined", limit=200)
    inbox = fb_store.list_inbox_messages(device_id=device_id, since_iso=since, limit=500)

    risk_history = []
    try:
        from src.host.fb_risk_listener import get_healer
        all_hist = get_healer().get_all_histories()
        if device_id:
            for r in all_hist.get(device_id, []):
                if r.get("ts", "") >= since:
                    risk_history.append({"device_id": device_id, **r})
        else:
            for did, hist in all_hist.items():
                for r in hist:
                    if r.get("ts", "") >= since:
                        risk_history.append({"device_id": did, **r})
    except Exception:
        pass

    top_leads: List[Dict] = []
    try:
        from src.leads.store import get_leads_store
        store = get_leads_store()
        if hasattr(store, "search_leads"):
            raw = store.search_leads(source_platform="facebook",
                                     min_score=60, limit=20) or []
            top_leads = [{"name": l.get("name"), "score": l.get("score", 0),
                          "tags": l.get("tags", [])} for l in raw]
    except Exception:
        pass

    return {
        "since_iso": since,
        "hours": hours,
        "scope_device": device_id or "all",
        "funnel": funnel,
        "groups_count": len(groups),
        "groups_top5": [{"name": g.get("group_name"), "members": g.get("member_count", 0),
                         "extracted": g.get("extracted_member_count", 0)}
                        for g in groups[:5]],
        "inbox_total": len(inbox),
        "inbox_incoming": sum(1 for m in inbox if m.get("direction") == "incoming"),
        "inbox_outgoing": sum(1 for m in inbox if m.get("direction") == "outgoing"),
        "risk_events": risk_history,
        "top_leads": top_leads,
    }


def _build_prompt(metrics: Dict[str, Any]) -> str:
    return f"""你是一个 Facebook 引流运营经理,请基于以下数据写一份精炼的中文日报(Markdown 格式)。

## 原始数据
设备范围: {metrics['scope_device']}
统计窗口: 过去 {metrics['hours']} 小时

漏斗:
{json.dumps(metrics['funnel'], ensure_ascii=False, indent=2)}

群组数: {metrics['groups_count']}
TOP5 群组:
{json.dumps(metrics['groups_top5'], ensure_ascii=False, indent=2)}

收件箱: 总 {metrics['inbox_total']}, 来 {metrics['inbox_incoming']}, 去 {metrics['inbox_outgoing']}

风控事件 ({len(metrics['risk_events'])}):
{json.dumps(metrics['risk_events'][:5], ensure_ascii=False, indent=2)}

TOP 高分线索 ({len(metrics['top_leads'])}):
{json.dumps(metrics['top_leads'][:10], ensure_ascii=False, indent=2)}

## 写作要求
1. 第 1 段:开头 1 句话总结(达成/未达成/异常)
2. 第 2 段:数字看板(漏斗每步带百分比变化)
3. 第 3 段:**风险与建议**(若有 risk_events 必须放显眼位置)
4. 第 4 段:**明日行动建议**(基于 top_leads + 转化率瓶颈)
5. 控制在 400 字以内,绝不超过 600 字
6. 用 emoji 标记重要数字(✅ ⚠️ 📈 📉)
"""


def generate_brief(device_id: Optional[str] = None,
                   hours: int = 24,
                   persist: bool = True) -> Dict[str, Any]:
    """生成 1 份日报。"""
    metrics = _gather_metrics(device_id=device_id, hours=hours)
    prompt = _build_prompt(metrics)

    md_text = ""
    llm_ok = False
    try:
        from src.ai.llm_client import LLMClient
        client = LLMClient()
        md_text = client.chat_with_system(
            "你是一个数据驱动的社媒运营经理,文笔简洁有力。",
            prompt,
            temperature=0.4,
            max_tokens=800,
            use_cache=False,
        )
        llm_ok = bool(md_text and len(md_text.strip()) > 30)
    except Exception as e:
        logger.warning("[fb_daily_brief] LLM 失败,用 fallback 模板: %s", e)

    if not llm_ok:
        md_text = _fallback_brief(metrics)

    brief = {
        "generated_at": _dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "scope_device": metrics["scope_device"],
        "window_hours": hours,
        "markdown": md_text.strip(),
        "metrics": metrics,
        "llm_generated": llm_ok,
    }

    if persist:
        try:
            from src.host.database import _connect
            with _connect() as conn:
                ts = brief["generated_at"]
                conn.execute(
                    "INSERT INTO audit_logs (timestamp, action, target, detail, source)"
                    " VALUES (?, ?, ?, ?, 'fb_daily_brief')",
                    (ts, "fb_daily_brief",
                     device_id or "all",
                     json.dumps(brief, ensure_ascii=False)),
                )
        except Exception as e:
            logger.debug("[fb_daily_brief] persist 失败: %s", e)
    return brief


def _fallback_brief(m: Dict[str, Any]) -> str:
    f = m["funnel"]
    risks = m["risk_events"]
    risk_line = (f"⚠️ 过去 {m['hours']}h 触发 {len(risks)} 次风控,"
                 "已自动降级 warmup。") if risks else "✅ 无风控事件。"
    top_lead = m["top_leads"][0]["name"] if m["top_leads"] else "暂无"
    return f"""# Facebook 引流日报 (设备: {m['scope_device']})

## 概览
过去 {m['hours']}h 已加入 {m['groups_count']} 个群,整理出 {f['stage_extracted_members']} 个群成员招呼对象,
发出 {f['stage_friend_request_sent']} 个好友请求,通过 {f['stage_friend_accepted']}(通过率 {f['rate_accept']*100:.0f}%)。

## 漏斗
- 群成员招呼对象: {f['stage_extracted_members']}
- 好友请求发出: {f['stage_friend_request_sent']}
- 通过: {f['stage_friend_accepted']} ({f['rate_accept']*100:.0f}%)
- DM 收到: {f['stage_inbox_incoming']}
- WA 引流: {f['stage_wa_referrals']}

## 风险
{risk_line}

## 明日建议
重点跟进 TOP 线索:**{top_lead}**(评分 ≥ 60)
"""


def get_latest_brief(device_id: Optional[str] = None,
                     limit: int = 1) -> List[Dict]:
    try:
        from src.host.database import _connect
        with _connect() as conn:
            sql = ("SELECT timestamp, detail FROM audit_logs "
                   "WHERE action='fb_daily_brief'")
            params: list = []
            if device_id:
                sql += " AND target=?"
                params.append(device_id)
            sql += " ORDER BY id DESC LIMIT ?"
            params.append(limit)
            rows = conn.execute(sql, params).fetchall()
        out = []
        for ts, detail in rows:
            try:
                data = json.loads(detail)
            except Exception:
                continue
            data["timestamp"] = ts
            out.append(data)
        return out
    except Exception:
        return []
