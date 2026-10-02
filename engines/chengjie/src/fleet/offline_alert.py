"""节点长时间离线告警（P1-7）。

- ``long_offline``：纯函数，挑出离线超过 N 分钟的节点（吊销的不算）。
- ``OfflineAlerter``：每次离线只告警一次；节点恢复在线后清除状态，下次离线再告警。
  通道：控制器现有的 ``src.ops.ops_alert.notify``（配了 EVENT_INGEST_KEY 才会推 TG，
  没配时只写日志）；另外始终 ``logger.warning`` 一行，方便 journalctl 检索：
  ``fleet node_offline_alert node=... offline_min=...``。
- 控制台另外通过 ``/api/fleet/overview`` 的 ``offline_alert`` 字段做醒目标记。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_OFFLINE_ALERT_MIN = 10
CHECK_INTERVAL_SEC = 60


def resolve_alert_min(value: Any) -> int:
    """配置值 → 分钟数。缺省/非法 → 10；显式 0 或负数 → 0（关闭推送，控制台仍按 10 分钟标记）。"""
    if value is None or value == "":
        return DEFAULT_OFFLINE_ALERT_MIN
    try:
        v = int(float(value))
    except (TypeError, ValueError):
        return DEFAULT_OFFLINE_ALERT_MIN
    return max(0, v)


def long_offline(nodes: Iterable[Dict[str, Any]], *, now: float, after_min: int) -> List[Dict[str, Any]]:
    after = max(1, int(after_min or DEFAULT_OFFLINE_ALERT_MIN)) * 60
    out: List[Dict[str, Any]] = []
    for n in nodes or []:
        if n.get("state") != "offline" or n.get("status") == "revoked":
            continue
        last = n.get("last_seen") or n.get("enrolled_at")
        if not last:
            continue
        gap = float(now) - float(last)
        if gap < after:
            continue
        out.append({
            "node_id": n.get("node_id"),
            "label": n.get("label") or n.get("host_name") or n.get("node_id"),
            "host_name": n.get("host_name") or "",
            "group_name": n.get("group_name") or "",
            "last_seen": last,
            "offline_min": int(gap // 60),
        })
    out.sort(key=lambda x: -x["offline_min"])
    return out


def _default_notify(text: str, node_id: str, debounce_sec: float) -> None:
    from src.ops.ops_alert import notify
    notify("fleet_node_offline", text, account_id=str(node_id), reason="offline",
           source="fleet-controller", debounce_sec=debounce_sec, audit=False)


class OfflineAlerter:
    def __init__(self, after_min: int = DEFAULT_OFFLINE_ALERT_MIN,
                 notify: Optional[Callable[[str, str, float], None]] = None) -> None:
        self.after_min = int(after_min)
        self._notify = notify or _default_notify
        self._alerted: set = set()
        self._lock = threading.Lock()

    def check(self, nodes: List[Dict[str, Any]], *, now: Optional[float] = None) -> List[Dict[str, Any]]:
        """返回本轮新告警的节点。after_min<=0 时不推送。永不抛。"""
        if self.after_min <= 0:
            return []
        ts = float(now if now is not None else time.time())
        hits = long_offline(nodes, now=ts, after_min=self.after_min)
        hit_ids = {h["node_id"] for h in hits}
        online_ids = {n.get("node_id") for n in nodes or [] if n.get("state") == "online"}
        with self._lock:
            for nid in sorted(self._alerted & online_ids):
                logger.info("fleet node_offline_recovered node=%s", nid)
            # 恢复在线或被吊销/删除的节点清除状态，下次离线重新告警
            self._alerted &= hit_ids
            new = [h for h in hits if h["node_id"] not in self._alerted]
            self._alerted |= {h["node_id"] for h in new}
        for h in new:
            last = time.strftime("%Y-%m-%d %H:%M", time.localtime(float(h["last_seen"])))
            text = (f"智拓群控：节点「{h['label']}」({h['host_name'] or '-'}"
                    f"{'，' + h['group_name'] if h['group_name'] else ''}) 已离线 {h['offline_min']} 分钟，"
                    f"最后心跳 {last}")
            logger.warning("fleet node_offline_alert node=%s offline_min=%s label=%s",
                           h["node_id"], h["offline_min"], h["label"])
            try:
                self._notify(text, str(h["node_id"]), float(self.after_min * 60))
            except Exception:  # noqa: BLE001 — 告警失败不影响控制器
                logger.warning("fleet node_offline_alert notify failed node=%s", h["node_id"], exc_info=True)
        return new


def start_watch(get_nodes: Callable[[], List[Dict[str, Any]]], alerter: OfflineAlerter,
                *, interval_sec: float = CHECK_INTERVAL_SEC) -> Optional[threading.Thread]:
    """后台守护线程，每 interval_sec 检查一次。after_min<=0 时不启动。"""
    if alerter.after_min <= 0:
        logger.info("fleet offline alert disabled (offline_alert_min=0)")
        return None

    def _loop() -> None:
        while True:
            try:
                alerter.check(get_nodes())
            except Exception:  # noqa: BLE001
                logger.warning("fleet offline watch tick failed", exc_info=True)
            time.sleep(interval_sec)

    t = threading.Thread(target=_loop, name="fleet-offline-watch", daemon=True)
    t.start()
    logger.info("fleet offline watch started (after %s min, every %ss)", alerter.after_min, interval_sec)
    return t
