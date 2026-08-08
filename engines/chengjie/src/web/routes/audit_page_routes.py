"""审计日志页面路由（Phase E1 续拆，从 admin.py 抽出）。

端点：
  GET /audit
  GET /audit/export

依赖：templates / page_auth / audit_store。

2026-08-05：时间轴改**最新在前**；``family`` 业务视角下推 SQL。
2026-08-05 Phase3：其余筛选（操作人/日期/关键词）同样下推；总数走
``COUNT(*)``（不再「近 500 条窗口内翻页」）；页面/导出过滤口径对齐
（action 精确匹配；表单 ``channel`` → keyword LIKE）。
"""

from __future__ import annotations

import csv
import io as _io
import math
import time

from fastapi import Depends, Request
from fastapi.responses import HTMLResponse, StreamingResponse

from src.utils.audit_store import AuditStore
from src.web.audit_display import family_like_patterns


def _norm_since(date_from: str) -> str:
    d = (date_from or "").strip()
    if not d:
        return ""
    return d if len(d) > 10 else f"{d} 00:00:00"


def _norm_until(date_to: str) -> str:
    d = (date_to or "").strip()
    if not d:
        return ""
    return d if len(d) > 10 else f"{d} 23:59:59"


def _filter_kwargs(action: str, operator: str, channel: str,
                   date_from: str, date_to: str, family: str) -> dict:
    """页面与导出共用的 SQL 过滤参数（单一事实源）。"""
    return {
        "action": (action or "").strip(),
        "user_id": (operator or "").strip(),
        "keyword": (channel or "").strip(),  # 表单字段名历史遗留，语义=关键词
        "since": _norm_since(date_from),
        "until": _norm_until(date_to),
        "action_patterns": family_like_patterns(family) or None,
    }


def register_audit_page_routes(app, ctx) -> None:
    templates = ctx.templates
    _page_auth = ctx.page_auth
    audit_store = ctx.audit_store

    @app.get("/audit", response_class=HTMLResponse)
    async def audit_page(request: Request, _=Depends(_page_auth),
                         action: str = "", keyword: str = "", limit: int = 50,
                         operator: str = "", channel: str = "",
                         date_from: str = "", date_to: str = "",
                         family: str = "", page: int = 1):
        # 兼容旧 ?keyword=；表单用 channel，二者并存时 channel 优先
        chan = (channel or keyword or "").strip()
        flt = _filter_kwargs(action, operator, chan, date_from, date_to, family)
        per = max(1, min(int(limit or 50), 200))
        page = max(1, int(page or 1))
        total = 0
        records: list = []
        all_actions: list = []
        all_operators: list = []
        if audit_store:
            total = audit_store.count(**flt)
            total_pages = max(1, math.ceil(total / per)) if total else 1
            page = min(page, total_pages)
            offset = (page - 1) * per
            records = audit_store.query(
                limit=per, offset=offset, newest_first=True, **flt) if total else []
            # 下拉枚举：在「当前除 action/operator 自身外」的过滤上取 distinct，
            # 避免切了 family 还看到无关动作；action/operator 选中项始终可见。
            enum_base = {k: v for k, v in flt.items() if k not in ("action", "user_id")}
            all_actions = audit_store.distinct_actions(**enum_base)
            all_operators = audit_store.distinct_operators(
                **{k: v for k, v in flt.items() if k != "user_id"})
            if flt["action"] and flt["action"] not in all_actions:
                all_actions = sorted(all_actions + [flt["action"]])
            if flt["user_id"] and flt["user_id"] not in all_operators:
                all_operators = sorted(all_operators + [flt["user_id"]])
        else:
            total_pages = 1

        qs_parts = []
        if action:
            qs_parts.append(f"action={action}")
        if chan:
            qs_parts.append(f"channel={chan}")
        if operator:
            qs_parts.append(f"operator={operator}")
        if date_from:
            qs_parts.append(f"date_from={date_from}")
        if date_to:
            qs_parts.append(f"date_to={date_to}")
        if family:
            qs_parts.append(f"family={family}")
        qs_parts.append(f"limit={per}")
        query_str = "&".join(qs_parts)
        return templates.TemplateResponse(request, "audit.html", {
            "records": records,
            "total": total, "page": page, "total_pages": total_pages,
            "query_str": query_str,
            "filters": {"action": action, "keyword": chan, "operator": operator,
                        "channel": chan, "date_from": date_from, "date_to": date_to,
                        "family": family},
            "all_actions": all_actions, "all_operators": all_operators,
            "retention": {"days": AuditStore.DEFAULT_KEEP_DAYS,
                          "rows": AuditStore.DEFAULT_MAX_ROWS},
        })

    @app.get("/audit/export")
    async def audit_export(request: Request, _=Depends(_page_auth),
                           action: str = "", operator: str = "",
                           channel: str = "", date_from: str = "", date_to: str = "",
                           family: str = ""):
        """导出审计记录为 CSV（UTF-8 BOM，Excel 直接打开不乱码）。

        过滤与页面同源下推——「导出筛选结果」必须与页面所见口径一致。
        行序保持**旧→新**（下游对账历史契约）。
        """
        flt = _filter_kwargs(action, operator, channel, date_from, date_to, family)
        all_entries = (audit_store.query(limit=10000, newest_first=False, **flt)
                       if audit_store else [])

        buf = _io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["# 导出时间", time.strftime("%Y-%m-%d %H:%M:%S")])
        writer.writerow(["# 筛选条件",
                         f"操作={action or '全部'}",
                         f"操作人={operator or '全部'}",
                         f"关键词={channel or '无'}",
                         f"族={family or '全部'}",
                         f"日期={date_from or '不限'}~{date_to or '不限'}"])
        writer.writerow(["# 记录总数", len(all_entries)])
        writer.writerow([])
        writer.writerow(["序号", "时间", "操作类型", "目标", "操作人", "旧值", "新值", "快照ID"])
        for i, e in enumerate(all_entries, 1):
            writer.writerow([
                i,
                e.get("ts", ""),
                e.get("action", ""),
                e.get("target", ""),
                e.get("user_id", ""),
                e.get("old_val", "") or "",
                e.get("new_val", "") or "",
                e.get("snapshot_id", "") or "",
            ])
        content = buf.getvalue().encode("utf-8-sig")
        ts = time.strftime("%Y%m%d_%H%M%S")
        filename = f"audit_{ts}.csv"
        if action or operator or channel or family:
            tag = (action or operator or channel or family or "filtered").replace(" ", "_")[:20]
            filename = f"audit_{tag}_{ts}.csv"
        return StreamingResponse(
            iter([content]),
            media_type="text/csv; charset=utf-8-sig",
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )
