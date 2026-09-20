"""风控隔离三盾覆盖率（凭据 / 出口 / 指纹）。

与本仓已有的 `src/utils/isolation_health.py` **同名不同物**，别混：
  - `isolation_health` 说的是**数据隔离**——记忆键分桶、人设绑定、FateX 独立库，
    防的是「多个号的数据串味」；
  - 本模块说的是**风控隔离**——独立凭据 / 独立出口 IP / 独立设备指纹，
    防的是「Telegram 把一批号识别成同一套软件跑出来的，然后成簇封」。

为什么用「账号覆盖率」而不是「登录事件比率」：运营真正要回答的问题是
「我这些号里，有几个是真被隔离的」。登录事件比率会把历史流量混进来，
一个早就下线的号也会拉高分子；而覆盖率直接对着当下在册的号说话，
低了就知道该去补哪一件套。

纯函数 + 一个读注册表的薄封装，零外部依赖。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Iterable, List

logger = logging.getLogger(__name__)

#: 只统计协议号——官方 API / RPA 号不走凭据池，算进来会稀释分母、误导判断
COUNTED_MODE = "protocol"
#: 已移除的号不该进分母
SKIP_STATUS = {"removed"}


def _meta_of(account: Dict[str, Any]) -> Dict[str, Any]:
    meta = account.get("meta")
    if isinstance(meta, dict):
        return meta
    raw = account.get("meta_json")
    if isinstance(raw, str) and raw.strip():
        try:
            got = json.loads(raw)
            return got if isinstance(got, dict) else {}
        except Exception:  # noqa: BLE001
            return {}
    return {}


def _shield(covered: int, total: int) -> Dict[str, Any]:
    return {
        "covered": covered,
        "total": total,
        "pct": round(covered / total, 3) if total else 0.0,
    }


def build_shields(accounts: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """由账号列表算三盾覆盖率。

    返回 ``{credential, egress, fingerprint, total, legacy, pooled,
    full_isolated, weakest}``。``weakest`` 指出覆盖率最低的那一件套——运营照它
    补货即可，不用自己比大小。

    为什么要单列 ``legacy``（2026-07-27 实测发现）：当时 8 个在册协议号里 6 个是
    凭据池上线**之前**登录的，它们的 session 绑在配置自带的那组 api_id 上，按
    ``credpool_bridge.resolve_for_account`` 的规矩**刻意不迁**（换 api_id 会让
    Telegram 看到「session 与 api_id 不匹配」，是封号级信号）。也就是说这 6 个号
    的覆盖率靠运维怎么补货都不会变，只有**重新登录**才会升级。不把它们单列，
    卡片就永远停在 25% —— 一个到不了 100% 的指标，运营两周后就学会无视它，
    而那时真正的缺口（新号没拿到隔离）也一起被无视了。
    """
    rows: List[Dict[str, Any]] = []
    for a in accounts or []:
        if str(a.get("mode") or "") != COUNTED_MODE:
            continue
        if str(a.get("status") or "") in SKIP_STATUS:
            continue
        rows.append(a)

    total = len(rows)
    cred = egress = fp = full = 0
    for a in rows:
        meta = _meta_of(a)
        has_cred = bool(meta.get("credpool_key"))
        has_fp = bool(meta.get("device_fp"))
        # 出口：账号显式绑定的代理，或池按付费档下发过的出口（后者也落在 proxy_id）
        has_eg = bool(str(a.get("proxy_id") or "").strip())
        cred += 1 if has_cred else 0
        egress += 1 if has_eg else 0
        fp += 1 if has_fp else 0
        full += 1 if (has_cred and has_eg and has_fp) else 0

    shields = {
        "credential": _shield(cred, total),
        "egress": _shield(egress, total),
        "fingerprint": _shield(fp, total),
    }
    weakest = min(shields, key=lambda k: shields[k]["pct"]) if total else ""
    return {
        **shields,
        "total": total,
        # 池内号＝有粘定键的号；存量号只能靠重新登录升级，不是运维补货能救的
        "pooled": cred,
        "legacy": total - cred,
        "full_isolated": full,
        "weakest": weakest,
    }


def collect_shields() -> Dict[str, Any]:
    """读账号注册表算三盾；任何异常返回空壳（观测永远不该把主流程带崩）。"""
    try:
        from src.integrations.account_registry import get_account_registry

        accounts = get_account_registry().list("telegram")
        return build_shields(accounts)
    except Exception:  # noqa: BLE001
        logger.debug("[isolation_shields] 读取账号注册表失败（忽略）", exc_info=True)
        return build_shields([])
