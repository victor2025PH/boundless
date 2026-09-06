"""官网订单拉取回流（order_pull）——NAT 后引擎的成交闭环通道。

部署形态决定方向：官网（bd2026.cc，公网 VPS）收单；本引擎在 NAT 后无公网
入口，官网 push（order-hook webhook）打不进来 → 引擎**主动拉**官网管理订单
API（与「履约机拉单签发授权」同一通道 ``GET /api/admin/orders``，
``x-setup-key`` 头鉴权），筛出带 ``ref``（会话归因串，下单页 URL 由目录
direct 链接注入）且已到账/已开通的订单，经 ``service.settle_order_ref``
（与 order-hook 路由同一结算入口）把对应目标结算 done。

稳态设计：
- 默认关（新子系统约定）；``site_url``/``admin_key`` 缺失静默不拉；
- 只认 ``status ∈ {paid, activated}``——pending 只是提交了表单还没付款，
  不算成交；paid→activated 的第二次出现由 settle 幂等（同 order_id → dup）；
- 进程内 ``_SEEN`` 集（capped）只为省重复处理/日志，重启即清——settle 本身
  幂等，重复处理无害；
- 网络/解析失败只 debug 日志绝不抛（watchdog tick 外层另有 try/except）。
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("src.companion.goals.order_pull")

# 结算认可的订单状态：钱到账（paid）或已开通（activated）才算成交
SETTLE_STATUSES = frozenset(("paid", "activated"))

DEFAULT_INTERVAL_MIN = 10
DEFAULT_TIMEOUT_SEC = 15

_SEEN: set = set()
_SEEN_CAP = 4096


def reset_seen() -> None:
    """测试用：清进程内已处理订单集。"""
    _SEEN.clear()


def resolve_pull_cfg(cfg_root: Any) -> Dict[str, Any]:
    """``companion.goals.order_pull`` 配置段 → 规范化 dict（缺省=关）。"""
    try:
        from src.companion.goals.service import resolve_goals_cfg
        raw = resolve_goals_cfg(cfg_root).get("order_pull") or {}
        if not isinstance(raw, dict):
            raw = {}
    except Exception:
        raw = {}
    return {
        "enabled": bool(raw.get("enabled", False)),
        "site_url": str(raw.get("site_url") or "").strip().rstrip("/"),
        "admin_key": str(raw.get("admin_key") or "").strip(),
        "interval_min": max(1, int(raw.get("interval_min",
                                           DEFAULT_INTERVAL_MIN) or
                                   DEFAULT_INTERVAL_MIN)),
        "timeout_sec": max(3, int(raw.get("timeout_sec",
                                          DEFAULT_TIMEOUT_SEC) or
                                  DEFAULT_TIMEOUT_SEC)),
    }


def extract_ref_orders(payload: Any) -> List[Dict[str, Any]]:
    """官网 ``/api/admin/orders`` 响应 → 待结算子集（纯函数）。

    过滤：带非空 ``ref`` 且 ``status`` 在 SETTLE_STATUSES；其余（无归因的
    自然流量单 / pending / cancelled）一律忽略。字段裁剪到结算所需
    （id/ref/plan/period/status——period 供留存环定周期天数，年付≠30 天弧线），
    **绝不带出 contact 等 PII**。
    """
    out: List[Dict[str, Any]] = []
    try:
        orders = (payload or {}).get("orders")
        if not isinstance(orders, list):
            return out
        for o in orders:
            if not isinstance(o, dict):
                continue
            ref = str(o.get("ref") or "").strip()
            status = str(o.get("status") or "").strip().lower()
            if not ref or status not in SETTLE_STATUSES:
                continue
            out.append({
                "id": str(o.get("id") or "").strip(),
                "ref": ref,
                "plan": str(o.get("plan") or "").strip(),
                "period": str(o.get("period") or "").strip().lower(),
                "status": status,
            })
    except Exception:
        logger.debug("extract_ref_orders 解析异常", exc_info=True)
    return out


def _default_http_get(url: str, headers: Dict[str, str],
                      timeout: float) -> Any:
    import requests
    r = requests.get(url, headers=headers, timeout=timeout)
    r.raise_for_status()
    return r.json()


def pull_and_settle(
    store: Any,
    cfg_root: Any,
    *,
    http_get: Optional[Callable[..., Any]] = None,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """拉官网订单 → 结算带 ref 的已付款单。返回摘要（观测/日志用）。

    ``http_get(url, headers, timeout) -> dict`` 可注入（测试）；默认 requests。
    摘要：``{pulled, candidates, settled, dup, unmatched, errors, skipped}``。
    """
    summary = {"pulled": 0, "candidates": 0, "settled": 0, "dup": 0,
               "unmatched": 0, "errors": 0, "skipped": 0}
    cfg = resolve_pull_cfg(cfg_root)
    if not (cfg["enabled"] and cfg["site_url"] and cfg["admin_key"]):
        summary["skipped"] = 1
        return summary

    fetch = http_get or _default_http_get
    try:
        payload = fetch(
            f"{cfg['site_url']}/api/admin/orders",
            {"x-setup-key": cfg["admin_key"]},
            cfg["timeout_sec"])
    except Exception:
        logger.debug("拉官网订单失败（网络/鉴权）", exc_info=True)
        summary["errors"] = 1
        return summary

    orders = extract_ref_orders(payload)
    try:
        summary["pulled"] = int((payload or {}).get("count") or 0)
    except Exception:
        pass
    summary["candidates"] = len(orders)

    from src.companion.goals.service import settle_order_ref
    for o in orders:
        oid = o["id"]
        if oid and oid in _SEEN:
            continue
        try:
            res = settle_order_ref(
                store, ref=o["ref"], order_id=oid, plan=o["plan"],
                period=str(o.get("period") or ""), now=now,
                cfg_root=cfg_root)
        except Exception:
            logger.debug("结算订单 %s 异常", oid or "-", exc_info=True)
            summary["errors"] += 1
            continue
        if res.get("dup"):
            summary["dup"] += 1
        elif res.get("updated"):
            summary["settled"] += 1
            logger.info("[goal-order-pull] 订单 %s（%s）→ 目标 %s 结算 done",
                        oid or "-", o["plan"] or "-", res.get("goal_id"))
        elif not res.get("matched"):
            summary["unmatched"] += 1
        if oid:
            if len(_SEEN) >= _SEEN_CAP:
                _SEEN.clear()
            _SEEN.add(oid)
    return summary


__all__ = [
    "SETTLE_STATUSES",
    "extract_ref_orders",
    "pull_and_settle",
    "reset_seen",
    "resolve_pull_cfg",
]
