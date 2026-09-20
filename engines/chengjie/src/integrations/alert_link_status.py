"""告警链路自检聚合器（路由 / 健康灯共用的单一事实源，2026-08-02）。

把「告警最后一公里通没通」聚合成一份可判定快照：
  - **文件真相**：``notify_webhooks_store.effective_webhooks``（mtime 失效缓存，
    与磁盘一致）经 ``alert_link_audit.audit_alert_link`` 做别名级覆盖审计；
  - **进程真相**：运行中 ``WebhookNotifier.status_snapshot()``（running / 装载数 /
    已外发与失败计数 / 非密钥配置指纹）；
  - **分歧检测**：两侧 ``config_fingerprint`` 一比——手改文件绕过面板 → notifier
    不会热更，指纹不一致即现形（快照无指纹时退化比条数）；
  - **诱饵检测**：引擎根遗留同名 overlay 文件（服务读不到那份，运营却会去编辑）。

verdict 分档（按可行动性排序，前者优先）：
  ``no_channel``（0 启用通道，告警报进虚空）→ ``not_running``（notifier 存在但
  事件循环没在消费）→ ``divergent``（文件 vs 进程不是同一份）→ ``uncovered``
  （有通道但关注别名有洞）→ ``healthy``。

消费方：``GET /api/admin/alert-link-status``（薄包装）、``health_watchdog.
probe_alert_link``（健康灯黄灯组件）。响应零密钥字段（通道名/计数/路径/指纹）。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


def collect_alert_link_status(
    cfg: Optional[Dict[str, Any]],
    notifier: Any = None,
    engine_root: Optional[Path] = None,
) -> Dict[str, Any]:
    """聚合快照（只读，软失败——任何一环异常都不抛，缺什么少报什么）。"""
    from src.integrations.alert_link_audit import (
        audit_alert_link,
        config_fingerprint,
        default_focus_aliases,
        detect_orphan_config,
    )
    from src.integrations.notify_webhooks_store import (
        effective_webhooks,
        load as load_overlay,
        store_path,
    )

    cfg = cfg or {}
    items = effective_webhooks(cfg)
    audit = audit_alert_link(items, default_focus_aliases())
    file_fp = config_fingerprint(items)

    proc: Dict[str, Any] = {"present": notifier is not None}
    if notifier is not None:
        try:
            proc.update(notifier.status_snapshot())
        except Exception:
            logger.debug("[alert-link] notifier 快照失败", exc_info=True)

    # 分歧＝进程装载的配置与磁盘不是同一份（指纹优先，快照无指纹时退化比条数）
    divergent = False
    if notifier is not None:
        if proc.get("config_fp"):
            divergent = proc["config_fp"] != file_fp
        elif "webhooks" in proc:
            divergent = int(proc.get("webhooks") or 0) != int(audit["channels_total"])

    overlay = load_overlay()
    sp = ""
    try:
        sp = str(store_path())
    except Exception:
        pass
    store = {
        "path": sp,
        "overlay_exists": overlay is not None,
        "source": "overlay" if overlay is not None else (
            "config_yaml" if ((cfg.get("notify") or {}).get("webhooks")) else "none"),
    }

    orphan = None
    try:
        er = engine_root or Path(__file__).resolve().parents[2]
        data_root = Path(sp).parent.parent if sp else er
        orphan = detect_orphan_config(er, data_root)
    except Exception:
        orphan = None

    if not audit["has_any_channel"]:
        verdict = "no_channel"
    elif notifier is not None and not proc.get("running", False):
        verdict = "not_running"
    elif divergent:
        verdict = "divergent"
    elif audit["uncovered"]:
        verdict = "uncovered"
    else:
        verdict = "healthy"
    return {
        "ok": True,
        "verdict": verdict,
        "audit": audit,
        "file_fp": file_fp,
        "process": proc,
        "divergence": divergent,
        "store": store,
        "orphan": orphan,
    }
