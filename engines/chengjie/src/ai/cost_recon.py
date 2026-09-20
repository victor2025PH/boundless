"""每日成本对账（2026-09-08 成本对账 P1）：规则引擎 + 定时任务 + 分级告警。

三方对账的三条腿：
  A. 内部计量（``cost_ledger.llm_usage``，厂商返回的 usage × ``ai.pricing``）
  B. ``suspected``（超时/断流没拿到 usage 的估算，单列，解释「A 比账单少」的那部分）
  C. 外部真值（``cost_ledger.bill_truth``：硅基账单 CSV 导入 / 人工填当日金额与余额）
硅基 ``/v1/user/info`` 已下线（2026-08-14），C 没有 API 可拉——所以「真值缺失」本身
也是一条规则：连续 N 天没导账单就提醒管理员，否则对账形同虚设。

规则（都在 ``evaluate_day`` 里，纯函数、离线可单测）：
  mismatch        |A−C|/C > mismatch_pct 且 |A−C| > mismatch_abs_cny
  spike           A > spike_ratio × 前 7 日均值 且 A > spike_min_cny
  budget_daily    A ≥ 80% / 100% 日预算
  budget_monthly  本月累计 ≥ 80% / 100% 月预算
  unknown_purpose 出现 purpose=unknown 的消耗（有人加了新调用点没标用途）
  suspected       超时估算 > 0（提示，不告警）
  runway          余额按 7 日均燃可用天数 < runway_warn_days
  truth_missing   连续 truth_stale_days 天没有真值

告警分级（都走既有通道，不新造）：
  每日摘要 → EventBus ``ai_cost_report``（📊 日报·成本，[tg-ops] + 铃铛），不论好坏都发；
  warn     → EventBus ``billing_alert``（🟠 警告·成本）；
  crit     → ``notify_host``（弹窗 + host_alert 镜像到 TG），冷却 6h。
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional

from src.ai.cost_ledger import CostLedger, day_of

logger = logging.getLogger("ai_chat_assistant.cost_recon")

DEFAULTS: Dict[str, Any] = {
    "enabled": True,
    "provider": "",                 # 空＝账本里出现过的全部厂商（LAN 除外）
    "daily_budget_cny": 0.0,        # 0＝不设
    "monthly_budget_cny": 0.0,
    "mismatch_pct": 10.0,
    "mismatch_abs_cny": 2.0,
    "spike_ratio": 2.0,
    "spike_min_cny": 5.0,
    "runway_warn_days": 7,
    "truth_stale_days": 3,
    "recon_hour": 9,
    "recon_minute": 40,
    "alert_cooldown_sec": 6 * 3600,
}

#: 预算超线时可停的用途（客户回复 / 翻译 / 识图永不在列——那是产品本身）。
NONCRITICAL_PURPOSES = frozenset({"memory_extract", "drill", "eval", "kb", "persona", "tool"})

_FREE_PROVIDERS = frozenset({"lan", "", "unknown"})

PURPOSE_LABELS = {
    "customer_reply": "客户回复", "drill": "夜间演练", "memory_extract": "记忆抽取",
    "translate": "翻译", "assistant": "小智助手", "kb": "知识库辅助", "vision": "识图",
    "probe": "探针", "eval": "评测", "tool": "短判/工具", "colloquial": "口语化改写",
    "persona": "人设精修", "unknown": "未标注",
}


def guard_cfg(config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """``ai.cost_guard`` 合并默认值（缺省一律从紧但不误报：预算 0＝不判）。"""
    out = dict(DEFAULTS)
    try:
        ai = (config or {}).get("ai") or {}
        cg = ai.get("cost_guard") or {}
        if isinstance(cg, dict):
            for k in DEFAULTS:
                if k in cg and cg[k] is not None:
                    out[k] = cg[k]
    except Exception:
        pass
    for k in ("daily_budget_cny", "monthly_budget_cny", "mismatch_pct", "mismatch_abs_cny",
              "spike_ratio", "spike_min_cny"):
        try:
            out[k] = float(out[k] or 0)
        except (TypeError, ValueError):
            out[k] = float(DEFAULTS[k])
    for k in ("runway_warn_days", "truth_stale_days", "recon_hour", "recon_minute",
              "alert_cooldown_sec"):
        try:
            out[k] = int(out[k])
        except (TypeError, ValueError):
            out[k] = int(DEFAULTS[k])
    return out


def _fmt(v: Optional[float]) -> str:
    return "—" if v is None else f"¥{v:.2f}"


def _prev_days(day: str, n: int) -> List[str]:
    try:
        ts = time.mktime(time.strptime(day, "%Y-%m-%d"))
    except Exception:
        ts = time.time()
    return [day_of(ts - i * 86400) for i in range(1, n + 1)]


def _month_prefix(day: str) -> str:
    return str(day)[:7]


def billable_providers(ledger: CostLedger, cfg: Dict[str, Any]) -> List[str]:
    p = str(cfg.get("provider") or "").strip()
    if p:
        return [p]
    return [x for x in ledger.providers() if x not in _FREE_PROVIDERS]


def estimate_balance(ledger: CostLedger, provider: str, upto_day: str) -> Dict[str, Any]:
    """余额估算：优先最近一次人填余额，其后按每日真值（缺则内部计量）往后扣；
    没有人填余额但有充值流水 → 充值总额 − 自首次充值起的累计消耗。"""
    out: Dict[str, Any] = {"balance": None, "basis": "none", "as_of": ""}
    lb = ledger.latest_balance(provider)
    if lb and lb.get("balance") is not None:
        bal = float(lb["balance"])
        start = str(lb["day"])
        out.update({"basis": "manual", "as_of": start})
        # 从填余额那天之后到 upto_day 逐日扣（真值优先）
        d = start
        guard = 0
        while d < upto_day and guard < 400:
            guard += 1
            d = day_of(time.mktime(time.strptime(d, "%Y-%m-%d")) + 86400)
            t = ledger.get_truth(d, provider)
            spent = float(t["amount"]) if t else float(ledger.day_summary(d, provider)["cost"])
            bal -= spent
        out["balance"] = round(bal, 2)
        return out
    rec = ledger.recharges(provider)
    if rec:
        total = sum(float(r.get("amount") or 0) for r in rec)
        first_day = day_of(min(float(r.get("ts") or time.time()) for r in rec))
        spent = 0.0
        d = first_day
        guard = 0
        while d <= upto_day and guard < 400:
            guard += 1
            t = ledger.get_truth(d, provider)
            spent += float(t["amount"]) if t else float(ledger.day_summary(d, provider)["cost"])
            d = day_of(time.mktime(time.strptime(d, "%Y-%m-%d")) + 86400)
        out.update({"balance": round(total - spent, 2), "basis": "recharge", "as_of": first_day})
    return out


def evaluate_day(day: str, ledger: CostLedger, cfg: Dict[str, Any], *,
                 provider: str, now: Optional[float] = None) -> Dict[str, Any]:
    """对某天某厂商跑全部规则 → 结论字典（不落库、不发通知；纯函数便于单测）。"""
    s = ledger.day_summary(day, provider)
    internal = float(s["cost"])
    suspected = float(s["suspected_cost"])
    truth_row = ledger.get_truth(day, provider)
    truth = float(truth_row["amount"]) if truth_row else None
    reasons: List[Dict[str, Any]] = []

    def add(rule: str, level: str, message: str, **extra: Any) -> None:
        reasons.append({"rule": rule, "level": level, "message": message, **extra})

    diff_pct: Optional[float] = None
    verdict = "ok"
    no_internal = int(s["calls"]) == 0 and internal <= 0
    if truth is None:
        verdict = "no_truth"
        stale = 0
        for d in [day] + _prev_days(day, int(cfg["truth_stale_days"]) + 2):
            if ledger.get_truth(d, provider):
                break
            stale += 1
        if stale >= int(cfg["truth_stale_days"]):
            add("truth_missing", "warn",
                f"已连续 {stale} 天没有导入{provider}账单/填写当日金额，对账无法进行；"
                f"请到成本页导入费用明细 CSV 或手填今日金额")
    elif no_internal and truth > 0:
        # 账本当天一笔都没有却有账单——不是「一致」，是「没得比」：账本装载前的消耗、
        # 或计量整体失灵（0909 首次装载实锤：昨日 0 vs 账单 1.25 被判 ✅）。
        verdict = "no_data"
        diff_pct = -100.0
        add("no_internal", "info",
            f"{day} 账本没有任何记录而账单为 {_fmt(truth)}：多半是成本账本当天尚未装载；"
            f"若明天仍如此则是计量整体失灵，请查 llm_cost sink 是否挂上")
    else:
        diff = internal - truth
        if truth > 0:
            diff_pct = round(diff / truth * 100.0, 2)
        big_abs = abs(diff) > float(cfg["mismatch_abs_cny"])
        big_pct = truth <= 0 or abs(diff_pct or 0) > float(cfg["mismatch_pct"])
        if big_abs and big_pct:
            verdict = "mismatch"
            direction = "多" if diff > 0 else "少"
            hint = ("内部估算比账单少：多半是有调用没记账（看「未标注」桶）或价格表过期"
                    if diff < 0 else
                    "内部估算比账单多：多半是价格表单价偏高，或超时估算重复计入")
            add("mismatch", "warn",
                f"{day} 内部估算 {_fmt(internal)} 比账单 {_fmt(truth)} {direction} "
                f"{_fmt(abs(diff))}（{abs(diff_pct or 0):.0f}%）。{hint}",
                internal=internal, truth=truth)

    # 尖峰：对比前 7 天均值（只算有消耗的天）
    hist = [float(ledger.day_summary(d, provider)["cost"]) for d in _prev_days(day, 7)]
    nonzero = [h for h in hist if h > 0]
    baseline = (sum(nonzero) / len(nonzero)) if nonzero else 0.0
    if baseline > 0 and internal > float(cfg["spike_ratio"]) * baseline \
            and internal > float(cfg["spike_min_cny"]):
        top = sorted(s["by_purpose"].items(), key=lambda kv: -kv[1]["cost"])[:3]
        top_txt = "、".join(f"{PURPOSE_LABELS.get(k, k)} {_fmt(v['cost'])}" for k, v in top)
        add("spike", "warn",
            f"{day} 消耗 {_fmt(internal)}，是前 7 日均值 {_fmt(baseline)} 的 "
            f"{internal / baseline:.1f} 倍。主要去向：{top_txt}", baseline=baseline)

    # 预算
    dbud = float(cfg["daily_budget_cny"])
    if dbud > 0:
        if internal >= dbud:
            add("budget_daily", "crit",
                f"{day} 消耗 {_fmt(internal)} 已超日预算 {_fmt(dbud)}；非关键用途"
                f"（记忆抽取/演练/评测/知识库辅助）已自动暂停到次日")
        elif internal >= 0.8 * dbud:
            add("budget_daily", "warn",
                f"{day} 消耗 {_fmt(internal)} 已达日预算 {_fmt(dbud)} 的 {internal / dbud:.0%}")
    mbud = float(cfg["monthly_budget_cny"])
    month_total = 0.0
    if mbud > 0:
        for row in ledger.daily_costs(31, provider=provider, end_day=day):
            if _month_prefix(row["day"]) == _month_prefix(day):
                month_total += float(row["cost"])
        if month_total >= mbud:
            add("budget_monthly", "crit",
                f"本月累计 {_fmt(month_total)} 已超月预算 {_fmt(mbud)}")
        elif month_total >= 0.8 * mbud:
            add("budget_monthly", "warn",
                f"本月累计 {_fmt(month_total)}，已达月预算 {_fmt(mbud)} 的 {month_total / mbud:.0%}")

    # 未标注用途 / 超时估算
    unk = s["by_purpose"].get("unknown")
    if unk and (unk["calls"] > 0):
        add("unknown_purpose", "warn",
            f"有 {unk['calls']} 次调用（{_fmt(unk['cost'])}）没有标注用途——有新调用点绕过了"
            f"计量出口，请给它加 purpose")
    if suspected > 0:
        add("suspected", "info",
            f"{s['suspected_calls']} 次超时/断流按字数估算约 {_fmt(suspected)}，"
            f"服务端可能已计费但本地没拿到 usage")

    # 余额跑道
    bal = estimate_balance(ledger, provider, day)
    avg7 = (sum(hist) + internal) / (len([h for h in hist if h > 0]) + 1)
    runway_days: Optional[float] = None
    if bal.get("balance") is not None and avg7 > 0:
        runway_days = round(float(bal["balance"]) / avg7, 1)
        if runway_days < int(cfg["runway_warn_days"]):
            add("runway", "crit" if runway_days < 3 else "warn",
                f"{provider} 余额约 {_fmt(bal['balance'])}（{ '人工填写' if bal['basis'] == 'manual' else '按充值推算'}），"
                f"按近 7 日均燃 {_fmt(avg7)}/天只够 {runway_days} 天，请安排充值")

    top_purposes = sorted(s["by_purpose"].items(), key=lambda kv: -kv[1]["cost"])
    breakdown = " / ".join(
        f"{PURPOSE_LABELS.get(k, k)} {_fmt(v['cost'])}" for k, v in top_purposes[:4] if v["cost"] > 0
    ) or "无消耗"
    light = {"ok": "✅", "mismatch": "❌", "no_truth": "⚪", "no_data": "⚪"}[verdict]
    if any(r["level"] == "crit" for r in reasons):
        light = "🔴"
    elif any(r["level"] == "warn" for r in reasons) and verdict == "ok":
        light = "🟡"
    if truth is None:
        recon_txt = "对账 ⚪ 无账单真值"
    elif verdict == "no_data":
        recon_txt = f"对账 ⚪ 账本无当日记录（账单 {_fmt(truth)}）"
    else:
        recon_txt = f"对账 {light} 差 {abs(diff_pct or 0):.0f}%"
    runway_txt = (f"余额约 {_fmt(bal['balance'])} 可用 {runway_days} 天"
                  if runway_days is not None else "余额未知（请填一次余额）")
    summary = (f"{day} {provider} AI 花费 {_fmt(internal)}（{breakdown}）· "
               f"{recon_txt} · {runway_txt}")
    return {
        "day": day, "provider": provider, "internal": round(internal, 4),
        "suspected": round(suspected, 4), "truth": truth, "diff_pct": diff_pct,
        "verdict": verdict, "light": light, "reasons": reasons, "summary": summary,
        "by_purpose": s["by_purpose"], "by_model": s["by_model"], "calls": s["calls"],
        "baseline7": round(baseline, 4), "avg7": round(avg7, 4),
        "balance": bal.get("balance"), "balance_basis": bal.get("basis"),
        "runway_days": runway_days, "month_total": round(month_total, 4),
    }


# ── 预算闸 ──────────────────────────────────────────────────────────────────

_CONFIG_PROVIDER: Optional[Any] = None


def configure_cost_guard(config_provider: Any) -> None:
    """启动期注入「现读配置」的回调（``lambda: assistant.config.config``），供预算闸
    在没有 config 上下文的调用点（skill_manager 静态门禁）使用。"""
    global _CONFIG_PROVIDER
    _CONFIG_PROVIDER = config_provider


def allow_purpose(purpose: str, config: Optional[Dict[str, Any]] = None,
                  ledger: Optional[CostLedger] = None, *, now: Optional[float] = None) -> bool:
    """非关键用途在**当日**超日预算后返回 False（客户回复/翻译/识图永远 True）。绝不抛。

    ``config`` / ``ledger`` 缺省分别取 ``configure_cost_guard`` 注入的回调与账本单例；
    两者任一拿不到 → 放行（预算闸绝不能因为自己没装好而拦业务）。
    """
    try:
        if purpose not in NONCRITICAL_PURPOSES:
            return True
        if config is None:
            if _CONFIG_PROVIDER is None:
                return True
            config = _CONFIG_PROVIDER() or {}
        if ledger is None:
            from src.ai.cost_ledger import get_cost_ledger
            ledger = get_cost_ledger()
        if ledger is None:
            return True
        cfg = guard_cfg(config)
        dbud = float(cfg["daily_budget_cny"])
        if dbud <= 0:
            return True
        today = day_of(now)
        total = 0.0
        for p in billable_providers(ledger, cfg):
            total += float(ledger.day_summary(today, p)["cost"])
        return total < dbud
    except Exception:
        return True


# ── 通知 ────────────────────────────────────────────────────────────────────

_LAST_ALERT: Dict[str, float] = {}


def _publish(event_type: str, data: Dict[str, Any]) -> None:
    try:
        from src.integrations.shared.event_bus import get_event_bus
        get_event_bus().publish(event_type, data)
    except Exception:
        logger.debug("[cost_recon] publish %s 跳过", event_type, exc_info=True)


def dispatch_alerts(result: Dict[str, Any], cfg: Dict[str, Any], *,
                    summary: bool = True, now: Optional[float] = None) -> Dict[str, Any]:
    """按级别分发：摘要（每天一条）/ warn → billing_alert / crit → notify_host（带冷却）。"""
    ts = float(now if now is not None else time.time())
    sent: Dict[str, Any] = {"summary": False, "warn": 0, "crit": 0}
    if summary:
        _publish("ai_cost_report", {
            "day": result["day"], "provider": result["provider"],
            "internal": result["internal"], "truth": result["truth"],
            "diff_pct": result["diff_pct"], "verdict": result["verdict"],
            "light": result["light"], "summary": result["summary"],
            "by_purpose": {k: round(v["cost"], 4) for k, v in (result.get("by_purpose") or {}).items()},
            "balance": result.get("balance"), "runway_days": result.get("runway_days"),
            "reasons": [r["message"] for r in result["reasons"] if r["level"] != "info"][:6],
            "rate_key": f"cost_report:{result['provider']}",
        })
        sent["summary"] = True
    warns = [r for r in result["reasons"] if r["level"] == "warn"]
    crits = [r for r in result["reasons"] if r["level"] == "crit"]
    cooldown = float(cfg.get("alert_cooldown_sec") or 21600)
    def _due(key: str) -> bool:
        last = _LAST_ALERT.get(key)
        return last is None or (ts - last) >= cooldown

    if warns:
        key = f"cost_warn:{result['provider']}:{','.join(sorted(r['rule'] for r in warns))}"
        if _due(key):
            _LAST_ALERT[key] = ts
            _publish("billing_alert", {
                "anomalies": [{"message": r["message"], "rule": r["rule"]} for r in warns],
                "provider": result["provider"], "day": result["day"],
                "rate_key": key,
            })
            sent["warn"] = len(warns)
    if crits:
        key = f"cost_crit:{result['provider']}:{','.join(sorted(r['rule'] for r in crits))}"
        if _due(key):
            _LAST_ALERT[key] = ts
            try:
                from src.utils.host_alert import notify_host
                notify_host(
                    f"AI 花费告警（{result['provider']}）",
                    "\n".join(r["message"] for r in crits),
                    key=key, cooldown_sec=cooldown, open_url="/workspace/cost",
                    attribution=f"cost_recon:{result['day']}",
                )
            except Exception:
                logger.debug("[cost_recon] notify_host 跳过", exc_info=True)
            sent["crit"] = len(crits)
    return sent


# ── 任务入口 ────────────────────────────────────────────────────────────────

def refresh_pricing(config: Optional[Dict[str, Any]]) -> None:
    """价格表热应用（AIClient 只在启动时读一次；改了 overlay 不用等重启）。"""
    try:
        ai = (config or {}).get("ai") or {}
        pricing = ai.get("pricing") or {}
        if pricing:
            from src.ai.llm_cost import get_llm_cost
            get_llm_cost().set_pricing(pricing, currency=str(ai.get("pricing_currency") or "CNY"))
    except Exception:
        pass


def run_recon(config: Optional[Dict[str, Any]], ledger: CostLedger, *,
              day: Optional[str] = None, notify: bool = True,
              now: Optional[float] = None) -> List[Dict[str, Any]]:
    """对某天（缺省昨天）全部计费厂商跑对账，落库并（可选）发通知。返回每厂商结论。"""
    cfg = guard_cfg(config)
    ts = float(now if now is not None else time.time())
    target = day or day_of(ts - 86400)
    refresh_pricing(config)
    results: List[Dict[str, Any]] = []
    providers = billable_providers(ledger, cfg)
    if not providers:
        logger.info("[cost_recon] %s 无计费厂商用量，跳过对账", target)
        return results
    for p in providers:
        try:
            r = evaluate_day(target, ledger, cfg, provider=p, now=ts)
        except Exception:
            logger.warning("[cost_recon] evaluate %s/%s 失败", target, p, exc_info=True)
            continue
        ledger.save_recon(target, p, internal=r["internal"], truth=r["truth"],
                          diff_pct=r["diff_pct"], verdict=r["verdict"],
                          reasons=r["reasons"], summary=r["summary"], now=ts)
        if notify:
            r["sent"] = dispatch_alerts(r, cfg, now=ts)
        logger.info("[cost_recon] %s", r["summary"])
        for reason in r["reasons"]:
            logger.info("[cost_recon]   %s %s: %s", reason["level"], reason["rule"], reason["message"])
        results.append(r)
    return results


class CostReconLoop:
    """每日 ``recon_hour:recon_minute`` 跑一次昨日对账（进程内 asyncio 任务）。

    与 daily_verify（09:35）错开 5 分钟；``config_provider`` 每次 tick 现读（热闸）。
    """

    def __init__(self, config_provider, ledger_provider, *, logger_=None) -> None:
        self._cfg = config_provider
        self._ledger = ledger_provider
        self._task: Optional[asyncio.Task] = None
        self._last_run_day = ""
        self._log = logger_ or logger

    def _seconds_until_next(self, now: float) -> float:
        cfg = guard_cfg(self._cfg() or {})
        lt = time.localtime(now)
        target = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, int(cfg["recon_hour"]),
                              int(cfg["recon_minute"]), 0, 0, 0, -1))
        if target <= now or day_of(now) == self._last_run_day:
            target += 86400
        return max(30.0, target - now)

    async def _loop(self) -> None:
        while True:
            try:
                wait = self._seconds_until_next(time.time())
                await asyncio.sleep(min(wait, 3600.0))
                if self._seconds_until_next(time.time()) > 60.0:
                    continue
                cfg_all = self._cfg() or {}
                if not guard_cfg(cfg_all).get("enabled", True):
                    self._last_run_day = day_of()
                    continue
                ledger = self._ledger()
                if ledger is None:
                    continue
                await asyncio.to_thread(run_recon, cfg_all, ledger)
                self._last_run_day = day_of()
            except asyncio.CancelledError:
                raise
            except Exception:
                self._log.warning("[cost_recon] 日对账 tick 异常（下轮继续）", exc_info=True)
                await asyncio.sleep(300.0)

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="cost_recon_loop")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None


__all__ = [
    "DEFAULTS", "NONCRITICAL_PURPOSES", "PURPOSE_LABELS", "guard_cfg", "evaluate_day",
    "estimate_balance", "allow_purpose", "configure_cost_guard", "dispatch_alerts",
    "run_recon", "refresh_pricing", "CostReconLoop", "billable_providers",
]
