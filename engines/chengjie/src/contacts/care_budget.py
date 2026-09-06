"""P3 2026-08-01：每联系人主动预算（care 真发前的最后一道防打扰闸）。

背景：各主动子系统各有频控，但互不知情——proactive_topic / daily_ritual /
milestone 都会给 **pending** care 让路（``has_pending_care``），可 care 自己
不看「这个联系人今天已经被主动打过几次」。真发开闸后，同一客户可能一天内
被「主动开场 + 早晚安 + 关怀」三连击。业界共识（Reverie / OTTO）：宁可不发。

设计（深想后的选择：**不新建预算库，把既有 ``outreach_log`` 升格为共享账本**）：
- 读侧：``outreach_log`` 按 conversation_id 建有索引（=care 的 contact_key），
  proactive_topic 真发本就落账（``record_outreach(batch_id="proactive_topic:*")``）；
  care 据此判「今天已被摸过几次 / 距上次多久」。
- 写侧：care 真发成功后也 ``record_outreach(batch_id="care:*")``——对周报 CLI
  （proactive_review 读同一张表）与将来任何预算消费方自动可见。
- 本模块只放**纯函数**（解析配置 + 判定），零 I/O 可单测；取数闭包在
  background_tasks 注入（与 dispatcher 其它护栏同范式）。

判定语义（两条独立规则，任一违反即拦）：
- ``min_gap_hours``：距该联系人上一次任何主动触达不足 N 小时 → 拦（防连击）。
- ``max_daily_touches``：本地自然日内已触达次数 + 本次 ≥ 上限 → 拦（防轰炸）。
失败开放（fail-open）：取数异常/无账本 → 放行——预算是体验优化不是安全红线，
漏判一次打扰可接受、因账本抖动漏发到点的关怀不可接受。危机关怀在派发器层豁免。
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Optional

_DEFAULT_MIN_GAP_HOURS = 4.0
_DEFAULT_MAX_DAILY = 2


@dataclass
class ContactBudgetCfg:
    enabled: bool = True
    min_gap_hours: float = _DEFAULT_MIN_GAP_HOURS
    max_daily_touches: int = _DEFAULT_MAX_DAILY


def parse_contact_budget_cfg(care_cfg: Optional[Dict[str, Any]]) -> ContactBudgetCfg:
    """从 ``companion.proactive_care.contact_budget`` 解析（缺省=默认开，只减不增）。"""
    raw = ((care_cfg or {}).get("contact_budget") or {})
    if not isinstance(raw, dict):
        raw = {}
    try:
        gap = max(0.0, float(raw.get("min_gap_hours", _DEFAULT_MIN_GAP_HOURS)))
    except Exception:
        gap = _DEFAULT_MIN_GAP_HOURS
    try:
        max_daily = max(0, int(raw.get("max_daily_touches", _DEFAULT_MAX_DAILY)))
    except Exception:
        max_daily = _DEFAULT_MAX_DAILY
    return ContactBudgetCfg(
        enabled=bool(raw.get("enabled", True)),
        min_gap_hours=gap,
        max_daily_touches=max_daily,
    )


def local_midnight_ts(now: Optional[float] = None) -> float:
    """本地自然日零点（max_daily_touches 的分桶边界，与 dashboard 日桶口径一致）。"""
    n = float(now if now is not None else time.time())
    d = datetime.fromtimestamp(n)
    return d.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def budget_allows(
    *,
    cfg: ContactBudgetCfg,
    last_touch_ts: float,
    touches_today: int,
    now: Optional[float] = None,
) -> bool:
    """纯判定：这条 care 现在发出去是否超预算。True=放行。

    - ``last_touch_ts``：该联系人最近一次主动触达（0=从未）。
    - ``touches_today``：本地今日已触达次数（**不含**本次）。
    规则任一违反 → False；cfg.enabled=False / 阈值为 0 → 对应规则不启用。
    ``outbound.unlimited_mode``（实时读 provider）→ 恒放行（联系人预算属业务频控）。
    """
    if not cfg.enabled:
        return True
    try:
        from src.ops.outbound_policy import is_unlimited
        if is_unlimited():
            return True
    except Exception:
        pass
    n = float(now if now is not None else time.time())
    if cfg.min_gap_hours > 0 and last_touch_ts > 0:
        if (n - float(last_touch_ts)) < cfg.min_gap_hours * 3600.0:
            return False
    if cfg.max_daily_touches > 0:
        if int(touches_today) + 1 > cfg.max_daily_touches:
            return False
    return True


__all__ = [
    "ContactBudgetCfg", "parse_contact_budget_cfg", "local_midnight_ts",
    "budget_allows",
]
