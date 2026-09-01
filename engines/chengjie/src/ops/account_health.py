# -*- coding: utf-8 -*-
"""账号健康聚合（P1-9 2026-08-29）——「我的号现在安全吗」的单一快照。

散装现状：冻结在顶栏条 / ai-runtime-status、掉线在平台会话卡、风控计数在
价值周报 safety 段——数据都在，但没有一处把「账号资产此刻的险情」拼成一眼
读数。本模块聚合成一份快照进 ``/api/workspace/metrics.account_health``
（ops「🛡️ 账号健康」卡消费）；老板页风险灯（boss_routes._build_now）另取
冻结数。防封护栏（急停/频控/退避）是本产品对客户的核心承诺之一——这份
快照同时是「AI 替你挡了什么」的营销叙事数据源。

peek 纪律（与 value_report 同族）：全部只读既有进程单例
（``kill_switch.status_snapshot`` fail-open / ``get_platform_session_health`` /
``peek_ops_event_store``——绝不新建库把「零数据」误报成事实）；逐段软失败，
缺段＝消费方隐藏对应格子。
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_WEEK_S = 7 * 86400.0


def collect_account_health(
    now: Optional[float] = None,
    *,
    ops_events_store: Any = None,
    session_health: Any = None,
) -> Dict[str, Any]:
    """聚合快照：``frozen``（急停生效面）/ ``sessions``（会话在线面）/
    ``safety_7d``（近 7 天风控审计）。任何一段失败只缺那一段，绝不抛。"""
    t = float(now if now is not None else time.time())
    out: Dict[str, Any] = {"generated_at": round(t, 1)}

    # 冻结：kill_switch 当前生效作用域（global 单独点名——全局急停是最高险情）
    try:
        from src.ops.kill_switch import (
            GLOBAL_SCOPE,
            freeze_source,
            status_snapshot,
        )
        rows = status_snapshot()
        items = []
        for r in rows[:8]:
            kind, cause = freeze_source(r.get("actor"), r.get("reason"))
            exp = float(r.get("expires_at") or 0)
            items.append({
                "scope": str(r.get("scope") or ""),
                "kind": kind,
                "cause": str(cause or "")[:60],
                "left_s": int(exp - t) if exp > t else 0,
            })
        out["frozen"] = {
            "active": len(rows),
            "global_active": any(
                str(r.get("scope")) == GLOBAL_SCOPE for r in rows),
            "items": items,
        }
    except Exception:
        logger.debug("account health: frozen section skipped", exc_info=True)

    # 会话在线面：平台会话健康登记表（进程级，零 IO）
    try:
        sh = session_health
        if sh is None:
            from src.integrations.platform_session_health import (
                get_platform_session_health,
            )
            sh = get_platform_session_health()
        d = sh.dump() or {}
        out["sessions"] = {
            "total": len(d.get("sessions") or {}),
            "down": int(d.get("unhealthy_count") or 0),
            "down_keys": list(d.get("unhealthy") or [])[:8],
            "inbox_stalled": int(d.get("inbox_stalled_count") or 0),
        }
    except Exception:
        logger.debug("account health: sessions section skipped", exc_info=True)

    # 近 7 天风控（ops_events 审计口径，重启不丢）；判据复用 value_report
    # 的 _safety_window——「auto/manual/解除」的分类是单一事实源，绝不再抄一份
    try:
        oe = ops_events_store
        if oe is None:
            from src.ops.ops_events import peek_ops_event_store
            oe = peek_ops_event_store()
        if oe is not None:
            from src.ops.value_report import _safety_window
            out["safety_7d"] = _safety_window(oe, t - _WEEK_S, t)
    except Exception:
        logger.debug("account health: safety section skipped", exc_info=True)
    return out


__all__ = ["collect_account_health"]
