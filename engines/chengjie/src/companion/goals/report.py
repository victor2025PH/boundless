"""目标达成报表聚合（P0 2026-08-09：账号×客户完成情况）。

两张读数面（纯读，绝不抛，坏库返回空骨架）：

- ``matrix_report``  —— 按（平台×账号）聚合：活跃/完成/失败/完成率/赢单，
  回答市场经理「哪个号在出成绩」；
- ``contacts_report`` —— 账号×客户明细行（分页 + 筛选），回答「哪些客户
  刚完成目标、跟进了没有」——这是销售线索列表，不是统计报表。

跨库富化（客户展示名 / 是否已跟进）走 InboxStore 批量接口，任何富化失败
只降级显示、不拖垮报表本体。「已跟进」＝**完成之后该会话有过出站消息**
（客观事实推导，零打点零新写路径；报表页「发消息」成功后自然变真，
跨坐席天然一致——两个运营不会对同一个完成客户重复发祝贺）。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

from src.companion.goals.notify import result_kind, triage_missed
from src.companion.goals.store import GoalStore
from src.companion.goals.templates import get_template

logger = logging.getLogger("GoalReport")

_DAY = 86400.0

# 完成后推荐启动的后续链（与前端 sidebar-chrome.js GOAL_CHAIN_REC 同源；
# 后端权威表在 workflow_starter.GOAL_CHAIN_REC，报表行按完成模板取推荐）
def _rec_chain_for(template_id: str) -> str:
    try:
        from src.inbox.workflow_starter import GOAL_CHAIN_REC
        return str(GOAL_CHAIN_REC.get(str(template_id or "")) or "")
    except Exception:
        return ""


def _template_name(template_id: str, lang: str = "zh") -> str:
    t = get_template(str(template_id or "")) or {}
    key = "name_en" if str(lang).lower().startswith("en") else "name_zh"
    return str(t.get(key) or template_id or "")


def matrix_report(
    store: GoalStore, *, days: int = 30, now: Optional[float] = None,
    lang: str = "zh",
) -> Dict[str, Any]:
    """按账号聚合 + 顶层合计 + 趋势/热力（P2）。``days`` 夹 1..365。

    - ``trend``：近 min(days,30) 天逐日 done/won（sparkline 数据源，补零行）；
    - ``heat``：窗口内（账号 × 模板）done 交叉 + 模板列名（矩阵头），
      零数据时两者都为空容器，前端据此隐藏区块。"""
    n = float(now if now is not None else time.time())
    d = max(1, min(int(days or 30), 365))
    out: Dict[str, Any] = {
        "window_days": d,
        "totals": {"active": 0, "done": 0, "failed": 0, "expired": 0,
                   "cancelled": 0, "won": 0, "won_amount": 0.0,
                   "done_rate": 0.0, "avg_days_to_done": None},
        "accounts": [],
        "trend": [],
        "heat": {"accounts": [], "templates": [], "cells": {}},
    }
    try:
        accounts = store.account_matrix(n - d * _DAY, now=n)
        out["accounts"] = accounts
        t = out["totals"]
        weighted_days = 0.0
        for b in accounts:
            for k in ("active", "done", "failed", "expired", "cancelled",
                      "won"):
                t[k] += int(b.get(k) or 0)
            t["won_amount"] = round(
                t["won_amount"] + float(b.get("won_amount") or 0.0), 2)
            if b.get("avg_days_to_done") is not None and b.get("done"):
                weighted_days += float(b["avg_days_to_done"]) * int(b["done"])
        organic = t["done"] + t["failed"] + t["expired"]
        if organic:
            t["done_rate"] = round(t["done"] / organic, 3)
        if t["done"] and weighted_days:
            t["avg_days_to_done"] = round(weighted_days / t["done"], 1)
        out["trend"] = store.daily_outcomes(days=min(d, 30), now=n)
        cells = store.account_template_matrix(n - d * _DAY)
        if cells:
            tmpl_ids: List[str] = sorted(
                {tid for per in cells.values() for tid in per},
                key=lambda tid: -sum(per.get(tid, 0) for per in cells.values()))
            out["heat"] = {
                "accounts": sorted(
                    cells, key=lambda a: -sum(cells[a].values())),
                "templates": [
                    {"id": tid, "name": _template_name(tid, lang)}
                    for tid in tmpl_ids],
                "cells": cells,
            }
    except Exception:
        logger.debug("matrix_report failed", exc_info=True)
    return out


def _followed_up_map(
    inbox_store: Any, rows: List[Dict[str, Any]],
) -> Dict[str, bool]:
    """{goal_id: 完成后是否有出站}——批量最后出站时刻 vs done_at。

    只对 done 行有语义；InboxStore 缺该方法（旧版本/测试假 store）或任何
    异常 → 返回空 map，前端按「未知」处理（不显示徽章，不误标已跟进）。"""
    try:
        if inbox_store is None:
            return {}
        fn = getattr(inbox_store, "last_outbound_ts_map", None)
        if not callable(fn):
            return {}
        done_rows = [r for r in rows if str(r.get("status")) == "done"]
        ids = [str(r.get("conversation_id") or "") for r in done_rows]
        ts_map = fn([c for c in ids if c]) or {}
        out: Dict[str, bool] = {}
        for r in done_rows:
            cid = str(r.get("conversation_id") or "")
            last_out = float(ts_map.get(cid) or 0.0)
            out[str(r.get("goal_id") or "")] = (
                last_out > float(r.get("done_at") or 0.0) > 0)
        return out
    except Exception:
        logger.debug("followed_up enrichment failed", exc_info=True)
        return {}


def _names_map(
    inbox_store: Any, rows: List[Dict[str, Any]],
) -> Dict[str, str]:
    try:
        if inbox_store is None:
            return {}
        ids = [c for c in (str(r.get("conversation_id") or "")
                           for r in rows) if c]
        if not ids:
            return {}
        out: Dict[str, str] = {}
        for cid, row in (inbox_store.get_conversations_for_ids(ids)
                         or {}).items():
            row = row or {}
            name = str(row.get("display_name") or row.get("name") or "").strip()
            if name:
                out[str(cid)] = name
        return out
    except Exception:
        logger.debug("contacts_report names failed", exc_info=True)
        return {}


def contacts_report(
    store: GoalStore,
    inbox_store: Any = None,
    *,
    days: int = 30,
    status: str = "done",
    platform: str = "",
    account_id: str = "",
    template: str = "",
    page: int = 1,
    page_size: int = 25,
    lang: str = "zh",
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """账号×客户明细（分页）。行字段见下方组装——报表页表格的行契约。"""
    n = float(now if now is not None else time.time())
    d = max(1, min(int(days or 30), 365))
    p = max(1, int(page or 1))
    ps = max(1, min(int(page_size or 25), 50))
    res = store.contact_outcomes(
        n - d * _DAY, status=status, platform=platform,
        account_id=account_id, template=template,
        limit=ps, offset=(p - 1) * ps)
    rows = res.get("rows") or []
    names = _names_map(inbox_store, rows)
    followed = _followed_up_map(inbox_store, rows)
    amounts = store.won_amounts_for_goals(
        [str(r.get("goal_id") or "") for r in rows])
    # P5 失守三分法（只判 failed/expired 行；里程碑有时间兑底判不了真实进展）：
    # offered=开价拍真发出过（最该复核站外漏标）/ engaged=有来有回 / silent=没聊起来
    missed_rows = [r for r in rows
                   if str(r.get("status")) in ("failed", "expired")]
    miss_kinds = triage_missed(store, inbox_store, missed_rows) \
        if missed_rows else {}
    items: List[Dict[str, Any]] = []
    for g in rows:
        gid = str(g.get("goal_id") or "")
        cid = str(g.get("conversation_id") or "")
        st = str(g.get("status") or "")
        done_at = float(g.get("done_at") or 0.0)
        start = float(g.get("start_ts") or 0.0)
        meta = amounts.get(gid) or {}
        item: Dict[str, Any] = {
            "goal_id": gid,
            "conversation_id": cid,
            "platform": str(g.get("platform") or ""),
            "account_id": str(g.get("account_id") or ""),
            "chat_key": str(g.get("chat_key") or ""),
            "contact_name": names.get(cid, ""),
            "template": str(g.get("template") or ""),
            "template_name": _template_name(g.get("template"), lang),
            "title": str(g.get("title") or ""),
            "status": st,
            "progress": float(g.get("progress") or 0.0),
            "milestone_idx": int(g.get("milestone_idx") or 0),
            "created_by": str(g.get("created_by") or ""),
            "result_kind": result_kind(g.get("result")),
            "amount": meta.get("amount"),
            "product": str(meta.get("product") or ""),
            "created_at": float(g.get("created_at") or 0.0),
            "done_at": done_at,
            "days_to_done": (round((done_at - start) / _DAY, 1)
                             if done_at > 0 and start > 0 else None),
        }
        if st == "done":
            # None=推导不了（inbox store 不可用），前端按未知处理
            item["followed_up"] = followed.get(gid)
            item["rec_chain"] = _rec_chain_for(item["template"])
        elif st in ("failed", "expired"):
            item["miss_kind"] = miss_kinds.get(gid, "")
        items.append(item)
    return {
        "window_days": d,
        "status": str(status or ""),
        "page": p,
        "page_size": ps,
        "total": int(res.get("total") or 0),
        "rows": items,
    }
