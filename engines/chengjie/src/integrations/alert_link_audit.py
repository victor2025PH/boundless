"""告警链路自检（纯函数核心，2026-08-01）。

「告警最后一公里」的可观测缺口：本仓 EventBus 告警（SLA/草稿积压/人工投递断链/
host_alert…）代码层完整，但外发是否**真的接通**只在启动日志一行「WebhookNotifier
已启动（N 个 webhook）」里——运营看不到，也无从自查「配了通道，但那三个最该收的
别名到底有没有人订阅」。本模块把这件事变成可计算的确定答案。

两个判据（都比「数通道」更进一步）：

1. **别名级覆盖**：配了通道 ≠ 收得到告警。一个只订阅 ``report`` 的通道，对
   ``draft_backlog`` / ``human_deliver`` / ``host_alert`` 这些高价值别名毫无覆盖。
   真正的「接通」是**关注别名逐个都有至少一个启用通道在订阅**。

2. **诱饵文件检测**：``notify_webhooks_store`` 用相对路径 ``config/notify_webhooks.json``，
   服务进程 CWD 是**实例数据根** → 它读写的是 ``<数据根>/config/...``。而仓库/引擎根里
   那个同名文件（运营看得见、会去编辑）服务进程**根本读不到**。引擎根有、数据根无 =
   编辑它无效的诱饵坑（本机实测正是此形态）。

纯函数、零 I/O（``detect_orphan_config`` 只 stat 传入路径）、零凭据——可离线跑、可挂巡检、
也是运营真填 token 后的**验收工具**。CLI 见 ``tools/alert_link_selfcheck.py``。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

# 三个最该外发、却最容易掉在缝里的别名（对应记忆里的 L1 盲区 / 人工投递断链 /
# 机主不在算力机前的主机级告警）。business 受众目录已含 draft_backlog，这里补齐另两个。
HIGH_VALUE_ALIASES = ("draft_backlog", "human_deliver", "host_alert")

# 服务进程实际读写的 overlay 相对位置（相对各自的运行根）。
WEBHOOKS_REL = "config/notify_webhooks.json"


def default_focus_aliases() -> List[str]:
    """关注别名集 = 「业务受众」告警目录 ∪ 三个高价值别名（去重、稳定序）。

    业务受众目录来自 ``webhook_notifier.alert_catalog()``（终端运营该收、大白话那批），
    读不到时退化为仅三个高价值别名——绝不抛（自检对缺依赖要软失败）。
    """
    aliases: List[str] = []
    try:
        from src.inbox.webhook_notifier import alert_catalog

        for item in alert_catalog().get("business", []) or []:
            a = str(item.get("alias") or "").strip()
            if a:
                aliases.append(a)
    except Exception:
        pass
    for a in HIGH_VALUE_ALIASES:
        aliases.append(a)
    # 去重保序
    seen: set = set()
    out: List[str] = []
    for a in aliases:
        if a not in seen:
            seen.add(a)
            out.append(a)
    return out


def _enabled_channels(webhooks: Optional[Iterable[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """仅保留启用通道（``enabled is not False``——缺省视为启用，与 store/notifier 同口径）。"""
    return [
        w for w in (webhooks or [])
        if isinstance(w, dict) and w.get("enabled") is not False
    ]


def channel_covers(webhook: Dict[str, Any], alias: str) -> bool:
    """该通道是否订阅了此别名。``all`` 是通配（覆盖一切），与 notifier 匹配语义一致。"""
    events = webhook.get("events") or []
    if not isinstance(events, (list, tuple, set)):
        return False
    return ("all" in events) or (alias in events)


def audit_alert_link(
    webhooks: Optional[Iterable[Dict[str, Any]]],
    focus_aliases: Optional[Iterable[str]] = None,
) -> Dict[str, Any]:
    """对一份有效 webhook 列表做别名级覆盖审计。

    参数
    ----
    webhooks : 已生效的 webhook 列表（overlay 或 config.yaml，每条含 events/format/enabled）。
    focus_aliases : 关注别名；缺省用 :func:`default_focus_aliases`。

    返回一个纯数据快照（无副作用），供 CLI 渲染与门禁断言。``healthy`` = 至少一个启用
    通道且关注别名**零未覆盖**——这才是「最后一公里真的通了」。
    """
    focus = list(focus_aliases) if focus_aliases is not None else default_focus_aliases()
    channels = list(webhooks or [])
    enabled = _enabled_channels(channels)

    per_alias: Dict[str, List[str]] = {}
    for a in focus:
        per_alias[a] = [
            str(w.get("name") or "webhook") for w in enabled if channel_covers(w, a)
        ]
    uncovered = [a for a in focus if not per_alias[a]]

    formats: Dict[str, int] = {}
    for w in enabled:
        f = str(w.get("format") or "json").lower()
        formats[f] = formats.get(f, 0) + 1

    return {
        "channels_total": len(channels),
        "channels_enabled": len(enabled),
        "channels_disabled": len(channels) - len(enabled),
        "has_any_channel": len(enabled) > 0,
        "focus_aliases": focus,
        "per_alias": per_alias,
        "uncovered": uncovered,
        "covered_count": len(focus) - len(uncovered),
        "focus_count": len(focus),
        "formats": formats,
        "healthy": (len(enabled) > 0) and (not uncovered),
    }


def detect_orphan_config(
    engine_root: Any,
    data_root: Any,
    rel: str = WEBHOOKS_REL,
) -> Optional[Dict[str, Any]]:
    """检测「引擎根有 overlay 文件、服务读的数据根却没有」的诱饵坑。

    引擎根 == 数据根（开发机/CI，无迁移）→ 返回 None（不存在诱饵问题）。
    否则：引擎根存在该文件、数据根不存在 → 返回定位信息（编辑引擎根那份对服务无效）。
    """
    er = Path(engine_root)
    dr = Path(data_root)
    try:
        if er.resolve() == dr.resolve():
            return None
    except Exception:
        # resolve 失败（异常路径）不阻断——按「不同根」继续判断
        if str(er) == str(dr):
            return None
    eng_file = er / rel
    data_file = dr / rel
    if eng_file.is_file() and not data_file.is_file():
        try:
            size = eng_file.stat().st_size
        except Exception:
            size = -1
        return {
            "engine_file": str(eng_file),
            "data_file": str(data_file),
            "engine_bytes": size,
        }
    return None
