"""买家信号检测（P1 2026-08-29）——「客户在问价，坐席却还没设目标」的补位。

业界 AI SDR 共识（2026-08 调研）：buying signal 出现后 24–48h 内行动的约见率
8–15%，过窗即凉。本模块只做**提示**不做动作：无活跃目标的会话近窗出现问价/
购买意向 → 目标卡出一行「趁热开『今天收口』」，点开直达限时表单——与既有
限时节奏（pace=today）衔接，闭环仍由坐席拍板。

刻意边界：
- **只扫入站**（客户说的才是信号；我方报价不算）；
- 词表保守偏购买动作（问价/怎么买/要链接/谈折扣），闲聊提到「贵」不算；
- 无活跃目标才提示（有目标的会话已有推进节奏，重复提示=噪音）；
- 纯函数 + 路由薄接线，不进 prompt、不发消息、不写库。
"""

from __future__ import annotations

import re
import time
from typing import Any, Dict, List, Optional

# 近窗：信号超过这个小时数就不再提示（过窗即凉，提示只会显得迟钝）
SIGNAL_WINDOW_HOURS = 48.0

_ZH = (
    r"多少钱|怎么收费|收费(?:标准|方式)|什么价|价格(?:是|多少)|报价|"
    r"怎么买|怎么购买|怎么开通|怎么订阅|怎么付款|怎么支付|付款方式|支付方式|"
    r"购买链接|下单链接|链接发(?:我|一下|个)|发(?:个|我|一下)链接|"
    r"在哪(?:里|儿)?买|怎么下单|有没有优惠|有优惠|打折|折扣|便宜(?:点|一点)|"
    r"试用(?:链接|怎么|多久)|怎么充值"
)
_EN = (
    r"how much(?: is it| does it cost)?|what'?s the price|pricing|"
    r"how (?:do|can) i (?:buy|pay|order|subscribe|sign up)|"
    r"payment (?:method|link|options?)|send (?:me )?(?:the )?link|"
    r"where (?:to|can i) buy|any discount|coupon|cheaper|free trial"
)
_BUY_RE = re.compile(f"(?:{_ZH})|(?:{_EN})", re.IGNORECASE)


def detect_buying_signal(text: Any) -> str:
    """单条文本 → 命中的信号片段；无 → ""。纯函数，宁漏勿误。"""
    s = str(text or "")
    if not s.strip():
        return ""
    m = _BUY_RE.search(s)
    return m.group(0).strip() if m else ""


def scan_recent_inbound(
    msgs: Optional[List[Dict[str, Any]]],
    *,
    now: Optional[float] = None,
    window_hours: float = SIGNAL_WINDOW_HOURS,
) -> Optional[Dict[str, Any]]:
    """近窗入站消息里找最新的买家信号 → ``{kind, v, ts}``；无 → None。

    入参形状=inbox ``list_recent_messages`` 行（direction/content/ts），
    新→旧顺序不限（内部按 ts 取最新命中）。绝不抛。
    """
    n = float(now if now is not None else time.time())
    floor = n - max(1.0, float(window_hours)) * 3600.0
    best: Optional[Dict[str, Any]] = None
    for m in (msgs or []):
        try:
            if str(m.get("direction") or "") != "in":
                continue
            ts = float(m.get("ts") or 0)
            if ts < floor or ts > n + 60:
                continue
            hit = detect_buying_signal(m.get("content"))
            if not hit:
                continue
            if best is None or ts > float(best["ts"]):
                best = {"kind": "buying", "v": hit[:40], "ts": round(ts, 1)}
        except Exception:
            continue
    return best


__all__ = [
    "SIGNAL_WINDOW_HOURS",
    "detect_buying_signal",
    "scan_recent_inbound",
]
