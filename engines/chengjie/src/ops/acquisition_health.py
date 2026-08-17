"""个人号获客健康聚合（P8）：把 P0/P6/P7 的治理成果收口成运营可见读数。

链路回顾——
- P0：``ban_signal`` 发送异常 → ``risk_events`` 24h 滚动 flood/error 计数。
- P6：WA/LINE 真机 RPA 屏幕风控（验证墙/限制）→ 同一 ``risk_events`` 计数。
- P7：leadbus 个人号线索归属真实设备账号 + 登记注册表（``personal_rpa`` mode）。
- P8（本模块）：列出所有 ``personal_rpa`` 账号 → ``build_account_signals`` 读回 24h
  风控计数 → ``account_health`` 评分 → 聚合成机群概览。之前这些数只在扣分逻辑里
  流动，运营看不见「哪个获客号在被风控、该不该停」。

纯函数（registry 注入，risk 计数经 build_account_signals 内部读 risk_events），可单测；
零账号 → ``applicable=False``（整卡隐藏，与 companion/bazi/图文一致性卡同款约定）。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from src.integrations.leadbus_account import PERSONAL_RPA_MODE

# 灯色排序（最差排前，运营一眼看到该处置的号）
_LIGHT_ORDER = {"red": 0, "amber": 1, "green": 2}


def collect_acquisition_health(
    registry: Any,
    *,
    now: Optional[float] = None,
    limiter: Any = None,
    risk_source: Any = None,
    **health_kwargs: Any,
) -> Dict[str, Any]:
    """聚合 ``personal_rpa`` 账号的获客健康（纯函数，best-effort）。

    返回 ``{applicable, total, light, counts{green,amber,red}, accounts[...]}``：
    - ``accounts`` 每项带 ``platform/account_id/score/light/recommended_cap/over_cap/
      sends_today/flood_waits_24h/errors_24h/reasons``，按灯色最差排前；
    - ``light``＝取最差账号灯（red>amber>green）；无账号＝``none`` + ``applicable=False``。

    ``registry`` 缺失/异常 → 空结果（不抛，看板缺席即可）。``limiter``/``risk_source``
    透传给 ``build_account_signals``（后者缺省走 risk_events），供单测注入。
    """
    from src.skills.account_health import account_health
    from src.skills.account_signals import build_account_signals

    counts = {"green": 0, "amber": 0, "red": 0}
    items: List[Dict[str, Any]] = []
    try:
        rows = registry.list() if registry is not None else []
    except Exception:
        rows = []

    for row in rows or []:
        if str(row.get("mode") or "") != PERSONAL_RPA_MODE:
            continue
        platform = str(row.get("platform") or "")
        account_id = str(row.get("account_id") or "")
        if not account_id:
            continue
        try:
            sig = build_account_signals(
                platform, account_id, registry=registry,
                limiter=limiter, now=now, risk_source=risk_source)
            h = account_health(sig, **health_kwargs)
        except Exception:
            continue
        light = str(h.get("light") or "green")
        counts[light] = counts.get(light, 0) + 1
        items.append({
            "platform": platform,
            "account_id": account_id,
            "label": str(row.get("label") or account_id),
            "score": int(h.get("score") or 0),
            "light": light,
            "recommended_cap": int(h.get("recommended_cap") or 0),
            "over_cap": bool(h.get("over_cap")),
            "sends_today": int(sig.get("sends_today") or 0),
            "flood_waits_24h": int(sig.get("flood_waits_24h") or 0),
            "errors_24h": int(sig.get("errors_24h") or 0),
            "reasons": list(h.get("reasons") or []),
        })

    items.sort(key=lambda x: (_LIGHT_ORDER.get(x["light"], 3), x["score"]))
    total = len(items)
    light = ("red" if counts["red"]
             else ("amber" if counts["amber"]
                   else ("green" if counts["green"] else "none")))
    return {
        "applicable": total > 0,
        "total": total,
        "light": light,
        "counts": counts,
        "accounts": items,
    }


__all__ = ["collect_acquisition_health"]
