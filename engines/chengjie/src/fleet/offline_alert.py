"""节点长时间离线告警（P1-7）。

- ``long_offline``：纯函数，挑出离线超过 N 分钟的节点（吊销的不算）。
- ``OfflineAlerter``：每次离线只告警一次；报过离线的节点恢复在线时推一条「已恢复在线」
  （带离线时长，同一节点 10 分钟内只推一次），并清除状态，下次离线再告警。
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
DEFAULT_RECOVER_MIN_INTERVAL_SEC = 600.0     # 同一节点「已恢复」10 分钟内只推一次
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


def _default_recover_notify(text: str, node_id: str, debounce_sec: float) -> None:
    from src.ops.ops_alert import notify
    notify("fleet_node_recovered", text, account_id=str(node_id), reason="recovered",
           source="fleet-controller", debounce_sec=debounce_sec, audit=False)


class OfflineAlerter:
    def __init__(self, after_min: int = DEFAULT_OFFLINE_ALERT_MIN,
                 notify: Optional[Callable[[str, str, float], None]] = None,
                 recover_notify: Optional[Callable[[str, str, float], None]] = None,
                 recover_min_interval_sec: float = DEFAULT_RECOVER_MIN_INTERVAL_SEC) -> None:
        self.after_min = int(after_min)
        self._notify = notify or _default_notify
        # 测试只传 notify 时，恢复消息也走它，便于断言；生产缺省走独立的 kind（防抖互不干扰）
        self._recover_notify = recover_notify or (notify if notify is not None else _default_recover_notify)
        self.recover_min_interval_sec = float(recover_min_interval_sec)
        # node_id → 告警时的快照（offline_since = 最后心跳），恢复时据此算离线时长
        self._alerted: Dict[str, Dict[str, Any]] = {}
        self._last_recover_sent: Dict[str, float] = {}
        self._lock = threading.Lock()

    def check(self, nodes: List[Dict[str, Any]], *, now: Optional[float] = None) -> List[Dict[str, Any]]:
        """返回本轮新告警的节点。after_min<=0 时不推送。永不抛。

        - 离线超过 after_min：每次离线只报一次；
        - 报过离线的节点重新在线：报一次「已恢复在线」，带离线时长；同一节点
          recover_min_interval_sec（缺省 10 分钟）内只报一次恢复，抖动不刷屏（只写日志）。
        """
        if self.after_min <= 0:
            return []
        ts = float(now if now is not None else time.time())
        hits = long_offline(nodes, now=ts, after_min=self.after_min)
        by_id = {n.get("node_id"): n for n in nodes or []}
        recovered: List[Dict[str, Any]] = []
        with self._lock:
            for nid in sorted(self._alerted):
                snap = self._alerted[nid]
                n = by_id.get(nid)
                if n is None or n.get("status") == "revoked":
                    # 被吊销 / 删除：静默清掉
                    self._alerted.pop(nid, None)
                    continue
                since = float(snap.get("last_seen") or 0)
                seen = float(n.get("last_seen") or 0)
                # 当前在线，或两次巡检之间回来过（最后心跳比告警时新）都算恢复
                if n.get("state") == "online" or seen > since:
                    self._alerted.pop(nid, None)
                    back = seen or ts
                    recovered.append({**snap, "offline_min": max(0, int((back - (since or back)) // 60)),
                                      "back_at": back})
            new = [h for h in hits if h["node_id"] not in self._alerted]
            for h in new:
                self._alerted[h["node_id"]] = dict(h)
            send_recover = []
            for r in recovered:
                last = self._last_recover_sent.get(r["node_id"])
                if last is not None and ts - last < self.recover_min_interval_sec:
                    r["suppressed"] = True
                else:
                    self._last_recover_sent[r["node_id"]] = ts
                    send_recover.append(r)
        for r in recovered:
            logger.info("fleet node_offline_recovered node=%s offline_min=%s suppressed=%s",
                        r["node_id"], r["offline_min"], bool(r.get("suppressed")))
        for r in send_recover:
            text = (f"智拓群控：节点「{r['label']}」({r['host_name'] or '-'}"
                    f"{'，' + r['group_name'] if r['group_name'] else ''}) 已恢复在线，离线约 {r['offline_min']} 分钟")
            try:
                self._recover_notify(text, str(r["node_id"]), self.recover_min_interval_sec)
            except Exception:  # noqa: BLE001
                logger.warning("fleet node_recovered notify failed node=%s", r["node_id"], exc_info=True)
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
