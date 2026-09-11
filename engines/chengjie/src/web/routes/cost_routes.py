# -*- coding: utf-8 -*-
"""AI 花费与对账 /workspace/cost（2026-09-08 成本对账 P1/P2）。

给管理员/老板一屏：今天/本月花了多少、花在哪、和厂商账单对得上吗、余额还能撑几天；
以及两件人工动作的入口——**导入硅基费用明细 CSV**（真值）与**手填当日金额/余额**。
硅基没有账单 API（/v1/user/info 2026-08-14 下线），真值只能这样进来。

端点：
  GET  /workspace/cost                页面
  GET  /api/cost/summary?days=14      汇总（今日/本月/趋势/去向/对账记录/余额/预算/价格表状态）
  POST /api/cost/import-bill          账单 CSV（multipart file 或 JSON {text}）→ 按天写真值 + 对账
  POST /api/cost/truth                手填 {day, provider, amount, balance?, note?}
  POST /api/cost/recharge             充值流水 {provider, amount, note?}
  POST /api/cost/recon/run            立即对账 {day?, notify?}
数据全在 ``cost_ledger.db``；金额单位与 ``ai.pricing_currency`` 一致（生产 CNY）。
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

from fastapi import Depends, HTTPException, Request
from fastapi.responses import HTMLResponse

from src.ai.cost_ledger import day_of, get_cost_ledger, parse_bill_csv
from src.ai.cost_recon import (
    PURPOSE_LABELS,
    billable_providers,
    estimate_balance,
    guard_cfg,
    run_recon,
)
from src.web.web_i18n import tr

logger = logging.getLogger(__name__)


# DeepSeek 官方峰时差价（¥/百万 token）：未命中 2.0 vs 缓存命中 0.04。
# 只用来给「大约省了多少」一个量级，不是账单；硅基等其它厂商没有这档差价时
# hit_ratio 仍有观测价值，saved_cny 标 estimated=true。
_DS_MISS_CNY_PER_M = 2.0
_DS_HIT_CNY_PER_M = 0.04


def _prompt_cache_snapshot() -> Dict[str, Any]:
    """进程内前缀缓存滚动统计（重启清零）。绝不为了这一栏去打厂商 API。"""
    empty = {
        "calls": 0, "prompt_tokens": 0, "cache_hit_tokens": 0, "cache_miss_tokens": 0,
        "hit_ratio": 0.0, "saved_cny": 0.0, "since_ts": 0,
    }
    try:
        from src.ai import prompt_trace
        s = prompt_trace.cache_stats()
        hit = int(s.get("cache_hit_tokens") or 0)
        saved = round(hit * (_DS_MISS_CNY_PER_M - _DS_HIT_CNY_PER_M) / 1_000_000, 4)
        return {
            "calls": int(s.get("calls") or 0),
            "prompt_tokens": int(s.get("prompt_tokens") or 0),
            "cache_hit_tokens": hit,
            "cache_miss_tokens": int(s.get("cache_miss_tokens") or 0),
            "hit_ratio": float(s.get("hit_ratio") or 0.0),
            "saved_cny": saved,
            "since_ts": float(s.get("since_ts") or 0),
        }
    except Exception:
        return empty


def _default_provider(ledger: Any, cfg: Dict[str, Any], config: Dict[str, Any]) -> str:
    """页面默认厂商：``ai.cost_guard.provider`` > 主链 ``ai.base_url`` 的厂商 > 账本里
    第一个计费厂商。0909 首次装载实锤：按字母序会选到备用池的 deepseek，而老板要看的是
    主链硅基。"""
    if str(cfg.get("provider") or "").strip():
        return str(cfg["provider"]).strip()
    primary = ""
    try:
        from src.ai.llm_cost import provider_from_base_url
        primary = provider_from_base_url(((config.get("ai") or {}).get("base_url")))
    except Exception:
        primary = ""
    if primary and primary not in ("lan", "other"):
        return primary
    provs = billable_providers(ledger, cfg) if ledger is not None else []
    return provs[0] if provs else (primary or "siliconflow")


def build_summary(ledger: Any, config: Dict[str, Any], *, days: int = 14,
                  provider: str = "", now: Optional[float] = None) -> Dict[str, Any]:
    """页面/老板日报共用的汇总（纯装配，绝不抛；账本缺席 → available=False）。"""
    t = float(now if now is not None else time.time())
    if ledger is None:
        return {"ok": False, "available": False, "prompt_cache": _prompt_cache_snapshot()}
    cfg = guard_cfg(config)
    prov = provider or _default_provider(ledger, cfg, config)
    today = day_of(t)
    yesterday = day_of(t - 86400)
    today_s = ledger.day_summary(today, prov)
    yest_s = ledger.day_summary(yesterday, prov)
    series = ledger.daily_costs(max(7, min(int(days), 60)), provider=prov, end_day=today)
    month_total = sum(float(r["cost"]) for r in
                      ledger.daily_costs(int(today[8:10]), provider=prov, end_day=today))
    recon = ledger.recon_rows(prov, days=14)
    truth_today = ledger.get_truth(today, prov)
    truth_yest = ledger.get_truth(yesterday, prov)
    bal = estimate_balance(ledger, prov, today)
    nonzero = [float(r["cost"]) for r in series[-8:-1] if float(r["cost"]) > 0]
    avg7 = (sum(nonzero) / len(nonzero)) if nonzero else 0.0
    runway = round(float(bal["balance"]) / avg7, 1) if (bal.get("balance") is not None and avg7 > 0) else None
    try:
        from src.ai.llm_cost import get_llm_cost
        pricing_ok = get_llm_cost().has_pricing() or bool((config.get("ai") or {}).get("pricing"))
        currency = get_llm_cost().currency
    except Exception:
        pricing_ok, currency = False, "CNY"

    def _purposes(s: Dict[str, Any]) -> List[Dict[str, Any]]:
        rows = []
        for k, v in sorted((s.get("by_purpose") or {}).items(), key=lambda kv: -kv[1]["cost"]):
            rows.append({"purpose": k, "label": PURPOSE_LABELS.get(k, k),
                         "cost": round(float(v["cost"]), 4), "calls": int(v["calls"]),
                         "tokens": int(v["tokens"])})
        return rows

    last_recon = recon[0] if recon else None
    return {
        "ok": True, "available": True, "provider": prov,
        "prompt_cache": _prompt_cache_snapshot(),
        "providers": billable_providers(ledger, cfg), "currency": currency,
        "pricing_configured": pricing_ok,
        "today": {"day": today, "cost": today_s["cost"], "calls": today_s["calls"],
                  "suspected_cost": today_s["suspected_cost"], "purposes": _purposes(today_s),
                  "models": {k: round(v["cost"], 4) for k, v in today_s["by_model"].items()},
                  "truth": truth_today["amount"] if truth_today else None},
        "yesterday": {"day": yesterday, "cost": yest_s["cost"], "calls": yest_s["calls"],
                      "purposes": _purposes(yest_s),
                      "truth": truth_yest["amount"] if truth_yest else None},
        "month_total": round(month_total, 4),
        "avg7": round(avg7, 4),
        "series": series,
        "balance": bal.get("balance"), "balance_basis": bal.get("basis"),
        "balance_as_of": bal.get("as_of"), "runway_days": runway,
        "budget": {"daily": cfg["daily_budget_cny"], "monthly": cfg["monthly_budget_cny"],
                   "mismatch_pct": cfg["mismatch_pct"], "mismatch_abs": cfg["mismatch_abs_cny"],
                   "recon_at": f"{int(cfg['recon_hour']):02d}:{int(cfg['recon_minute']):02d}"},
        "last_recon": last_recon,
        "recon": recon,
        "truth_rows": ledger.truth_rows(prov, days=14),
        "recharges": ledger.recharges(prov)[:10],
    }


def register_cost_routes(app, *, page_auth, api_auth, templates,
                         config_manager=None) -> None:
    """挂载成本页与数据/动作端点。"""

    def _cfg() -> Dict[str, Any]:
        return getattr(config_manager, "config", None) or {}

    def _ctx(request: Request) -> dict:
        try:
            sess = request.session
        except (AttributeError, AssertionError):
            sess = {}
        ctx: dict = {
            "user_name": sess.get("username") or "",
            "user_display_name": sess.get("display_name") or sess.get("username") or "",
        }
        try:
            _wa = (_cfg().get("web_admin") or {})
            if _wa.get("site_name"):
                ctx["site_name"] = _wa["site_name"]
        except Exception:
            pass
        return ctx

    async def _json(request: Request) -> Dict[str, Any]:
        try:
            data = await request.json()
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    def _ledger_or_503(request: Request):
        ledger = get_cost_ledger()
        if ledger is None:
            raise HTTPException(503, tr(request, "err.cost.ledger_unavailable"))
        return ledger

    def _provider(data: Dict[str, Any], ledger) -> str:
        p = str(data.get("provider") or "").strip()
        return p or _default_provider(ledger, guard_cfg(_cfg()), _cfg())

    @app.get("/workspace/cost", response_class=HTMLResponse)
    async def workspace_cost_page(request: Request, _=Depends(page_auth)):
        return templates.TemplateResponse(request, "cost.html", _ctx(request))

    @app.get("/api/cost/summary")
    async def api_cost_summary(request: Request, days: int = 14, provider: str = "",
                               _=Depends(api_auth)):
        return build_summary(get_cost_ledger(), _cfg(), days=days, provider=provider)

    @app.post("/api/cost/import-bill")
    async def api_cost_import_bill(request: Request, _=Depends(api_auth)):
        """硅基费用明细 CSV → 按天真值。multipart 字段 ``file``，或 JSON ``{text, provider}``。"""
        ledger = _ledger_or_503(request)
        text = ""
        provider = ""
        ctype = str(request.headers.get("content-type") or "")
        if "multipart/form-data" in ctype:
            form = await request.form()
            up = form.get("file")
            if up is not None and hasattr(up, "read"):
                raw = await up.read()
                for enc in ("utf-8-sig", "utf-8", "gb18030"):
                    try:
                        text = raw.decode(enc)
                        break
                    except UnicodeDecodeError:
                        continue
            provider = str(form.get("provider") or "")
        else:
            data = await _json(request)
            text = str(data.get("text") or "")
            provider = str(data.get("provider") or "")
        provider = provider.strip() or _default_provider(ledger, guard_cfg(_cfg()), _cfg())
        parsed = parse_bill_csv(text, provider=provider)
        if not parsed["ok"]:
            raise HTTPException(400, tr(request, "err.cost.bill_parse_failed",
                                        err=parsed["error"] or "?"))
        today = day_of()
        written: List[str] = []
        for day, amount in sorted(parsed["days"].items()):
            # 当天的账单还没走完，不当真值（否则必然「内部比账单多」误报）
            if day >= today:
                continue
            ledger.set_truth(day, provider, float(amount), source="csv",
                             note=f"csv rows={parsed['rows']}")
            written.append(day)
        results = []
        for day in written[-14:]:
            results.extend(run_recon(_cfg(), ledger, day=day, notify=False))
        try:
            audit = getattr(request.app.state, "audit_store", None)
            if audit is not None:
                audit.log(request.session.get("username", "web_admin"), "cost_import_bill",
                          provider, "", f"days={len(written)} rows={parsed['rows']}")
        except Exception:
            pass
        return {"ok": True, "provider": provider, "rows": parsed["rows"],
                "skipped": parsed["skipped"], "days_written": written,
                "skipped_today": [d for d in parsed["days"] if d >= today],
                "recon": [{"day": r["day"], "verdict": r["verdict"], "internal": r["internal"],
                           "truth": r["truth"], "diff_pct": r["diff_pct"], "light": r["light"]}
                          for r in results]}

    @app.post("/api/cost/truth")
    async def api_cost_truth(request: Request, _=Depends(api_auth)):
        ledger = _ledger_or_503(request)
        data = await _json(request)
        day = str(data.get("day") or day_of(time.time() - 86400)).strip()
        provider = _provider(data, ledger)
        amount = data.get("amount")
        balance = data.get("balance")
        if amount in (None, "") and balance in (None, ""):
            raise HTTPException(400, tr(request, "err.cost.need_amount_or_balance"))
        try:
            amt = float(amount) if amount not in (None, "") else None
            bal = float(balance) if balance not in (None, "") else None
        except (TypeError, ValueError):
            raise HTTPException(400, tr(request, "err.cost.not_number"))
        if amt is None:
            existing = ledger.get_truth(day, provider)
            amt = float(existing["amount"]) if existing else float(ledger.day_summary(day, provider)["cost"])
            note = "manual: balance only"
        else:
            note = str(data.get("note") or "manual")
        ledger.set_truth(day, provider, amt, balance=bal, source="manual", note=note)
        results = run_recon(_cfg(), ledger, day=day, notify=False) if day < day_of() else []
        return {"ok": True, "day": day, "provider": provider, "amount": amt, "balance": bal,
                "recon": [{"day": r["day"], "verdict": r["verdict"], "light": r["light"],
                           "summary": r["summary"]} for r in results]}

    @app.post("/api/cost/recharge")
    async def api_cost_recharge(request: Request, _=Depends(api_auth)):
        ledger = _ledger_or_503(request)
        data = await _json(request)
        provider = _provider(data, ledger)
        try:
            amount = float(data.get("amount"))
        except (TypeError, ValueError):
            raise HTTPException(400, tr(request, "err.cost.recharge_not_number"))
        if amount <= 0:
            raise HTTPException(400, tr(request, "err.cost.recharge_positive"))
        ts = None
        if data.get("day"):
            try:
                ts = time.mktime(time.strptime(str(data["day"]), "%Y-%m-%d")) + 12 * 3600
            except ValueError:
                raise HTTPException(400, tr(request, "err.cost.bad_date"))
        ledger.add_recharge(provider, amount, note=str(data.get("note") or ""), ts=ts)
        return {"ok": True, "provider": provider, "amount": amount,
                "balance": estimate_balance(ledger, provider, day_of())}

    @app.post("/api/cost/recon/run")
    async def api_cost_recon_run(request: Request, _=Depends(api_auth)):
        ledger = _ledger_or_503(request)
        data = await _json(request)
        day = str(data.get("day") or "").strip() or None
        notify = bool(data.get("notify", False))
        results = run_recon(_cfg(), ledger, day=day, notify=notify)
        return {"ok": True, "results": results}

    logger.info("成本页已注册（/workspace/cost）")
