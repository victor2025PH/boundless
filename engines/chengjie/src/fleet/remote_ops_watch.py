# -*- coding: utf-8 -*-
"""远程操作开关的到期自动关 + 通知（0.3.8）。

- ``start_sweep``：后台守护线程，每 ``interval_sec`` 调一次 ``FleetStore.expire_remote_ops``；
  多进程部署只让拿到文件锁的那个进程巡检（同 offline_alert 的做法），避免重复通知。
- 通知复用控制器现有通道 ``src.ops.ops_alert.notify``：配了 EVENT_INGEST_KEY 才推 TG，
  没配只落日志 + ops_alert 审计（不另建通知系统）。开 / 关 / 到期自动关各推一条，不防抖。
- 关的同时主控作废该节点排队中的 phone_* 任务（store 里做）。
"""
from __future__ import annotations

import datetime as _dt
import logging
import os
import tempfile
import threading
from typing import Any, Callable, Dict, List, Optional


logger = logging.getLogger(__name__)

SWEEP_INTERVAL_SEC = 30
ENV_SWEEP_LOCK = "CHATX_FLEET_REMOTE_OPS_LOCK"
NOTIFY_KIND = "fleet_remote_ops"
_TZ = _dt.timezone(_dt.timedelta(hours=8))


def _hm(ts: Any) -> str:
    try:
        return _dt.datetime.fromtimestamp(float(ts), _TZ).strftime("%m-%d %H:%M")
    except (TypeError, ValueError, OverflowError, OSError):
        return "?"


def _default_notify(text: str, node_id: str, reason: str) -> None:
    from src.ops.ops_alert import notify
    notify(NOTIFY_KIND, text, account_id=str(node_id), reason=reason, source="fleet-controller", debounce_sec=0)


# 测试 / 其它部署可替换（签名：text, node_id, reason）
notify_hook: Callable[[str, str, str], None] = _default_notify


def _send(text: str, node_id: str, reason: str) -> None:
    try:
        notify_hook(text, node_id, reason)
    except Exception:  # noqa: BLE001 - 通知是旁路，失败不影响开关本身
        logger.warning("fleet remote_ops notify failed node=%s reason=%s", node_id, reason, exc_info=True)


def toggle_text(node: Dict[str, Any], enabled: bool, actor: str) -> str:
    name = str(node.get("label") or node.get("host_name") or node.get("node_id") or "?")
    if enabled:
        mins = (node.get("meta") or {}).get("remote_ops_minutes")
        return (f"【群控】{name} 已开启远程操作（{actor or '?'}），"
                f"{mins or '?'} 分钟后自动关闭（到期 {_hm(node.get('remote_ops_expires_at'))} UTC+8）")
    return f"【群控】{name} 已关闭远程操作（{actor or '?'}），排队中的手机操作已取消"


def notify_toggle(node: Optional[Dict[str, Any]], enabled: bool, actor: str) -> None:
    if not node:
        return
    text = toggle_text(node, enabled, actor)
    logger.warning("fleet remote_ops %s node=%s by=%s", "on" if enabled else "off", node.get("node_id"), actor)
    _send(text, str(node.get("node_id") or ""), "enabled" if enabled else "disabled")


def auto_off_text(hit: Dict[str, Any]) -> str:
    return (f"【群控】{hit.get('label') or hit.get('node_id')} 远程操作已到期自动关闭"
            f"（{hit.get('enabled_by') or '?'} 于 {_hm(hit.get('enabled_at'))} 开启），"
            f"取消排队手机操作 {int(hit.get('cancelled_tasks') or 0)} 个")


def sweep_once(store: Any, *, default_minutes: Any = None, now: Optional[float] = None) -> List[Dict[str, Any]]:
    if store is None:
        return []
    kw: Dict[str, Any] = {"now": now}
    if default_minutes is not None:
        kw["default_minutes"] = default_minutes
    hits = store.expire_remote_ops(**kw)
    for h in hits:
        logger.warning("fleet remote_ops auto_off node=%s enabled_by=%s cancelled=%s",
                       h.get("node_id"), h.get("enabled_by"), h.get("cancelled_tasks"))
        _send(auto_off_text(h), str(h.get("node_id") or ""), "expired")
    return hits


def try_lock(path: str) -> Optional[Any]:
    """非阻塞独占文件锁；拿到返回打开的文件对象（进程活着就一直持有），拿不到返回 None。
    自带实现，不依赖 offline_alert（线上主控的 offline_alert 版本可能更旧、没有这个函数）。"""
    try:
        fh = open(path, "a+b")
    except OSError:
        logger.warning("fleet remote_ops sweep lock unavailable: %s", path, exc_info=True)
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


def _default_lock_path() -> str:
    return os.environ.get(ENV_SWEEP_LOCK) or os.path.join(tempfile.gettempdir(), "chatx-fleet-remote-ops.lock")


def start_sweep(get_store: Callable[[], Any], *, default_minutes: Any = None,
                interval_sec: float = SWEEP_INTERVAL_SEC, lock_path: Optional[str] = None,
                stop: Optional[threading.Event] = None) -> threading.Thread:
    """起巡检守护线程；``stop.set()`` 让它退出（测试用；生产随进程退出）。"""
    path = lock_path or _default_lock_path()
    held: Dict[str, Any] = {}
    stop_ev = stop if stop is not None else threading.Event()

    def _loop() -> None:
        while not stop_ev.is_set():
            try:
                if "fh" not in held:
                    fh = try_lock(path)
                    if fh is not None:
                        held["fh"] = fh
                        logger.info("fleet remote_ops sweep: this process (pid %s) runs auto-off", os.getpid())
                if "fh" in held:
                    sweep_once(get_store(), default_minutes=default_minutes)
            except Exception:  # noqa: BLE001
                logger.warning("fleet remote_ops sweep tick failed", exc_info=True)
            stop_ev.wait(interval_sec)
        fh = held.pop("fh", None)
        if fh is not None:
            try:
                fh.close()
            except OSError:
                logger.debug("fleet remote_ops sweep: lock close failed", exc_info=True)

    t = threading.Thread(target=_loop, name="fleet-remote-ops-sweep", daemon=True)
    t.start()
    logger.info("fleet remote_ops sweep started (every %ss, lock %s)", interval_sec, path)
    return t


__all__ = ["notify_toggle", "sweep_once", "start_sweep", "toggle_text", "auto_off_text", "SWEEP_INTERVAL_SEC"]