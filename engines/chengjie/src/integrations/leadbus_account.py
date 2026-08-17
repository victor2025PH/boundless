"""个人号 RPA 线索的账号归属（P7，桥接 huoke 等外部获客产品 → chengjie 账号治理）。

背景：``leadbus`` 线索总线（huoke/zhituo 等获客产品 → chengjie 承接）此前把线索的
``account_id`` 用 ``source.product`` 顶替（"信封没有账号概念"）——于是个人号获客线索
**不归属具体设备账号**，P0/P6 建的按 ``account_id`` 的治理（account_health 评分 /
recommended_cap / risk_events 风控计数）对获客来源完全接不上：无法回答「哪个号在获客、
哪个号被风控了」。

本模块＝**纯函数归属层**：信封可选携带 ``source.account_id``（huoke 侧纯增量，不带＝
旧行为逐字节保留），有则线索归属真实设备账号 + 幂等登记进注册表，接上既有治理链。

关键安全不变量：登记用 ``personal_rpa`` mode——它**不在** ``ORCHESTRATED_MODES``
（protocol/official/web），故本机 ``AccountOrchestrator`` 绝不会去拉起一个由 huoke
外部进程驱动、本地根本没有设备句柄的账号（那会导致 no-serial 反复重启 + 告警刷屏，
与「假账号写进注册表」同类事故）。chengjie 对这类账号只做**归因 / 治理 / 收件箱可见**，
不接管其收发进程。
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

# 外部个人号 RPA 驱动的账号 mode：可见 + 可治理，但本机编排器不接管（见模块 docstring）。
# 必须保持不在 account_orchestrator.ORCHESTRATED_MODES 内——由门禁钉死。
PERSONAL_RPA_MODE = "personal_rpa"


def resolve_lead_account(source: Optional[Dict[str, Any]]) -> Tuple[str, bool]:
    """从 leadbus 信封 ``source`` 段解析线索该归属的账号。

    返回 ``(account_id, is_real_account)``：
    - ``source.account_id`` 非空 → (它, True)：归属真实设备账号（个人号 RPA 新增能力）；
    - 否则 → (``source.product``, False)：**回落旧语义**（product 顶替，向后兼容，
      现有 leadbus 门禁 ``account_id == "zhituo"`` 继续成立）。
    """
    src = source if isinstance(source, dict) else {}
    acc = str(src.get("account_id") or "").strip()
    if acc:
        return acc, True
    return str(src.get("product") or "").strip(), False


def register_lead_account(
    registry: Any,
    platform: str,
    account_id: str,
    *,
    label: str = "",
    business_line: str = "",
) -> bool:
    """幂等登记个人号 RPA 账号进注册表（``personal_rpa`` mode）。

    让获客设备账号在 ops / 收件箱账号列表可见，并接上按 ``account_id`` 的健康/风控
    治理。**best-effort**：registry 缺失或写失败一律吞掉返回 False——线索落库绝不能
    被账号登记拖垮（与 leadbus ingest 的软降级风格一致）。``label`` 首次用 account_id
    兜底（upsert 对 None/后续调用保留原值，不覆盖运营改过的名）。
    """
    if registry is None:
        return False
    pid = str(platform or "").strip()
    aid = str(account_id or "").strip()
    if not pid or not aid:
        return False
    try:
        registry.upsert(
            pid, aid,
            mode=PERSONAL_RPA_MODE,
            label=(label or aid),
            business_line=(business_line or None),
        )
        return True
    except Exception:
        return False


__all__ = ["PERSONAL_RPA_MODE", "resolve_lead_account", "register_lead_account"]
