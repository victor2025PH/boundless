"""「账号隔离健康」观测纯核心（多协议号数据分桶改造的常驻体检）。

隔离改造（2026-07）后：episodic 记忆键按账号分桶（``platform:acct:peer``）、
每号在注册表绑定独立人设（``meta.persona_id``）、FateX（幻缘）产品数据独立建库。
本模块把「隔离在不在位」压成一份快照，供 ``/api/admin/isolation-health`` +
ops-overview「账号隔离健康」卡消费：

- 记忆键形态分布：**复用**迁移工具 :func:`classify_key` 的口径（scoped /
  legacy_platform / bare / other），legacy/bare 回升 = 有入口漏传 account；
- 账号人设绑定：在线号必须绑人设，未绑 = 多号共用默认人设（隔离名存实亡）；
- FateX 独立库备货：生辰画像行数 + 完整度（时辰+性别齐 = 可排大运）。

纯函数零 IO；数据由调用方注入（episodic ``list_key_stats()`` / registry ``list()`` /
fatex ``stats()``），TTL 缓存由路由层负责。
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Tuple

from src.utils.account_scope_migration import KNOWN_PLATFORMS, classify_key

# 未绑人设在线号最多列名（看板黄字点名用，防超长撑爆卡片）
_UNBOUND_LIST_CAP = 8

# legacy（legacy_platform+bare）键占比越线阈值：超过即总评 warn
_LEGACY_RATIO_WARN = 0.2


def classify_episodic_keys(
    key_stats: Iterable[Tuple[str, Any]],
    known_platforms: Optional[Iterable[str]] = None,
) -> Dict[str, int]:
    """episodic 键形态盘点：``[(key, count), ...]`` → 各形态键数 + 行数合计。

    形态判定复用迁移工具 :func:`classify_key`（单一口径，勿另起炉灶）；
    ``known_platforms`` 收窄时，界外平台的 scoped/legacy 键归 other
    （口径由入参说了算，不静默放宽）。
    """
    plats = {str(p or "").lower() for p in (known_platforms or KNOWN_PLATFORMS)}
    out = {"scoped": 0, "legacy_platform": 0, "bare": 0, "other": 0,
           "scoped_rows": 0, "legacy_rows": 0, "bare_rows": 0}
    for key, cnt in (key_stats or []):
        try:
            rows = max(0, int(cnt))
        except (TypeError, ValueError):
            rows = 0
        c = classify_key(str(key or ""))
        form = c.get("form") or "other"
        if form in ("scoped", "legacy_platform") and c.get("platform") not in plats:
            form = "other"
        if form == "scoped":
            out["scoped"] += 1
            out["scoped_rows"] += rows
        elif form == "legacy_platform":
            out["legacy_platform"] += 1
            out["legacy_rows"] += rows
        elif form == "bare":
            out["bare"] += 1
            out["bare_rows"] += rows
        else:
            out["other"] += 1
    return out


def _bound_persona_id(meta: Any) -> str:
    """registry meta → 绑定的人设 id（``persona_id`` 优先，其次 ``persona_ids[0]``）。"""
    if not isinstance(meta, dict):
        return ""
    pid = str(meta.get("persona_id") or "").strip()
    if pid:
        return pid
    pids = meta.get("persona_ids")
    if isinstance(pids, (list, tuple)) and pids:
        return str(pids[0] or "").strip()
    return ""


def summarize_persona_binding(accounts: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """账号注册表 ``list()`` 行 → 在线号人设绑定摘要（offline/pending 不计）。

    出 ``{"online": n, "bound": n, "unbound": [{platform, account_id, label}, ...]}``
    （unbound 列表封顶 8 条；bound 判定 = meta.persona_id 或 persona_ids[0] 非空）。
    """
    online = 0
    bound = 0
    unbound: List[Dict[str, str]] = []
    for row in (accounts or []):
        if not isinstance(row, dict):
            continue
        if str(row.get("status") or "") != "online":
            continue
        online += 1
        if _bound_persona_id(row.get("meta")):
            bound += 1
        elif len(unbound) < _UNBOUND_LIST_CAP:
            unbound.append({
                "platform": str(row.get("platform") or ""),
                "account_id": str(row.get("account_id") or ""),
                "label": str(row.get("label") or ""),
            })
    return {"online": online, "bound": bound, "unbound": unbound}


def build_isolation_health(
    key_stats: Iterable[Tuple[str, Any]],
    accounts: Iterable[Dict[str, Any]],
    fatex_stats: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """汇总三路信号 + 总评。level=warn 的两个触发条件（任一命中）：

    - 有未绑人设的**在线**号（多号共用默认人设 = 隔离名存实亡）；
    - legacy（legacy_platform+bare）键占比 > 20%（占比分母 = 可迁移形态
      scoped+legacy+bare，组合键 other 不属迁移范畴不入分母）。
    """
    keys = classify_episodic_keys(key_stats)
    personas = summarize_persona_binding(accounts)
    legacy_keys = keys["legacy_platform"] + keys["bare"]
    denom = keys["scoped"] + legacy_keys
    legacy_ratio = round(legacy_keys / denom, 3) if denom else 0.0
    unbound_online = personas["online"] - personas["bound"]
    level = ("warn" if (unbound_online > 0 or legacy_ratio > _LEGACY_RATIO_WARN)
             else "ok")
    return {
        "level": level,
        "keys": keys,
        "legacy_ratio": legacy_ratio,
        "personas": personas,
        "fatex": dict(fatex_stats or {}),
    }


__all__ = [
    "classify_episodic_keys",
    "summarize_persona_binding",
    "build_isolation_health",
]
