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
import os
import tempfile
import threading
import time
from typing import Any, Callable, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_OFFLINE_ALERT_MIN = 10
CHECK_INTERVAL_SEC = 60
ENV_ALERT_LOCK = "CHATX_FLEET_ALERT_LOCK"   # 多进程只让一个进程推送：文件锁路径（缺省系统临时目录）


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


def _default_lock_path() -> str:
    return os.environ.get(ENV_ALERT_LOCK) or os.path.join(tempfile.gettempdir(), "chatx-fleet-offline-watch.lock")


def try_lock(path: str) -> Optional[Any]:
    """非阻塞独占文件锁；拿到返回打开的文件对象（进程活着就一直持有），拿不到返回 None。
    进程退出时操作系统自动释放，其他 worker 下一轮就能接手。"""
    try:
        fh = open(path, "a+b")
    except OSError:
        logger.warning("fleet offline watch lock unavailable: %s", path, exc_info=True)
        return None
    try:
        if os.name == "nt":
            import msvcrt

            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fh
    except OSError:
        fh.close()
        return None


def start_watch(get_nodes: Callable[[], List[Dict[str, Any]]], alerter: OfflineAlerter,
                *, interval_sec: float = CHECK_INTERVAL_SEC,
                lock_path: Optional[str] = None) -> Optional[threading.Thread]:
    """后台守护线程，每 interval_sec 检查一次。after_min<=0 时不启动。

    多 worker / 多进程部署时每个进程都会起线程，但只有拿到 ``lock_path`` 文件锁的那个
    进程真正巡检和推送，避免同一节点离线被重复告警；持锁进程退出后，其他进程下一轮接手。
    """
    if alerter.after_min <= 0:
        logger.info("fleet offline alert disabled (offline_alert_min=0)")
        return None
    path = lock_path or _default_lock_path()
    held: Dict[str, Any] = {}

    def _loop() -> None:
        while True:
            try:
                if "fh" not in held:
                    fh = try_lock(path)
                    if fh is not None:
                        held["fh"] = fh
                        logger.info("fleet offline watch: this process (pid %s) is the alerter", os.getpid())
                if "fh" in held:
                    alerter.check(get_nodes())
            except Exception:  # noqa: BLE001
                logger.warning("fleet offline watch tick failed", exc_info=True)
            time.sleep(interval_sec)

    t = threading.Thread(target=_loop, name="fleet-offline-watch", daemon=True)
    t.start()
    logger.info("fleet offline watch started (after %s min, every %ss, lock %s)", alerter.after_min, interval_sec, path)
    return t
