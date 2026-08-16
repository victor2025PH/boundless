"""引擎侧运维告警中继（实施31 追加 · 2026-07-20）。

把「号出事」类关键反封号事件（封号急停 / 暂停 / 熔断 / 健康红灯）从引擎（Python）
直推到集团告警通道 —— VPS ``/api/ops/alert``（Bearer=EVENT_INGEST_KEY → Telegram），
与 PowerShell 侧 watchdog/cron_sentinel 用的**同一条 TG 中继**，老板一个地方收全部告警。

设计约束（与 watchdog 同款「吵醒人但别刷屏」）：
- **非阻塞**：HTTP POST 丢后台 daemon 线程发，绝不阻塞发送热路径 / asyncio 事件循环。
- **防抖**：同 (kind, account_id) 默认 30 分钟最多一条，避免配额耗尽/连环失败时刷屏。
- **无密钥优雅降级**：取不到 EVENT_INGEST_KEY → 只落日志、返回 False，绝不抛（离线/未铺密钥
  的部署零破坏）。
- **纯判定可测**：``should_send`` 防抖判定是纯函数（注入 now）；``notify`` 的 HTTP 发送可注入
  ``poster`` 供单测，不打真实网络。
"""
from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
import urllib.request
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

DEFAULT_DEBOUNCE_SEC = 1800.0
DEFAULT_BASE = "https://bd2026.cc"

# 防抖状态：key=(kind:account_id) → 上次发送时间戳（进程内内存，多进程各自计数）
_seen: Dict[str, float] = {}
_seen_lock = threading.Lock()


def _resolve_base(base: str = "") -> str:
    b = base or os.environ.get("PERSONA_SYNC_BASE") or DEFAULT_BASE
    return str(b).rstrip("/")


def _resolve_key(key: str = "") -> str:
    return str(key or os.environ.get("EVENT_INGEST_KEY") or "").strip()


def should_send(kind: str, account_id: str, *, now: Optional[float] = None,
                debounce_sec: float = DEFAULT_DEBOUNCE_SEC) -> bool:
    """防抖判定（纯函数）：同 (kind, account_id) 在 debounce_sec 内只允许一次。

    首次或超窗 → True 并记录时间；窗内重复 → False。debounce_sec<=0 表示不防抖（恒 True）。
    """
    ts = now if now is not None else time.time()
    if debounce_sec <= 0:
        return True
    k = f"{kind}:{account_id}"
    with _seen_lock:
        last = _seen.get(k)
        if last is not None and (ts - last) < debounce_sec:
            return False
        _seen[k] = ts
        return True


def _http_post(base: str, key: str, text: str, source: str, timeout: float = 15.0) -> None:
    body = json.dumps({"text": text, "source": source}).encode("utf-8")
    req = urllib.request.Request(
        base + "/api/ops/alert", data=body, method="POST",
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + key})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            r.read()
        logger.info("[ops_alert] 告警已推送: %s", text[:60])
    except Exception as ex:  # noqa: BLE001
        logger.warning("[ops_alert] 告警推送失败: %s", ex)


def _audit(kind: str, account_id: str, reason: str, detail: str,
           alerted: bool, now: Optional[float]) -> None:
    """全量落运维事件审计（与 TG 防抖解耦：这里每次都记，best-effort 绝不抛）。"""
    try:
        from src.ops.ops_events import get_ops_event_store
        store = get_ops_event_store()
        if store is not None:
            store.record(kind, account_id=account_id, reason=reason,
                         detail=detail, alerted=alerted, ts=now)
    except Exception:
        logger.debug("[ops_alert] 审计落库失败（忽略）", exc_info=True)


def notify(
    kind: str,
    text: str,
    *,
    account_id: str = "",
    source: str = "",
    reason: str = "",
    debounce_sec: float = DEFAULT_DEBOUNCE_SEC,
    base: str = "",
    key: str = "",
    now: Optional[float] = None,
    poster: Optional[Callable[[str, str, str, str], Any]] = None,
    audit: bool = True,
) -> bool:
    """推一条运维告警到集团 TG 中继（非阻塞、防抖、无密钥降级）+ 全量落审计。

    返回 True=本次被接受并已排程发送（或已交 poster）；False=防抖抑制 / 无密钥跳过。
    **审计与告警解耦**：无论防抖是否抑制、有无密钥，都全量落审计（可回溯「号这周被风控
    几次」）；只有 TG 推送受防抖 + 密钥约束。真实网络发送在 daemon 线程内完成（不阻塞
    调用方）；``poster`` 注入时同步调用（供单测）。``audit=False`` 可关审计（单测/自测用）。
    """
    _pass_debounce = should_send(kind, account_id, now=now, debounce_sec=debounce_sec)
    _key = _resolve_key(key)
    _alerted = bool(_pass_debounce and _key)
    # 先全量落审计（记录本次是否真推了 TG），再决定是否推送
    if audit:
        _audit(kind, account_id, reason, text, _alerted, now)
    if not _pass_debounce:
        return False
    if not _key:
        logger.info("[ops_alert] 无 EVENT_INGEST_KEY，跳过告警发送（仅落日志+审计）：%s", text[:80])
        return False
    _base = _resolve_base(base)
    _src = source or f"chengjie@{socket.gethostname()}"
    if poster is not None:
        try:
            poster(_base, _key, text, _src)
        except Exception:
            logger.debug("[ops_alert] 注入 poster 失败", exc_info=True)
        return True
    # 后台线程发送：绝不阻塞发送热路径 / 事件循环
    threading.Thread(
        target=_http_post, args=(_base, _key, text, _src), daemon=True
    ).start()
    return True


def make_ban_signal_alert(source: str = "") -> Callable[..., Any]:
    """产出适配 ``ban_signal.handle_send_exception(alert=...)`` 签名的回调。

    ban_signal 调用形如 ``alert(kind, {platform, account_id}, detail)``；这里把它转成
    一条中文 TG 告警。kind ∈ account_paused / account_banned。
    """
    def _cb(kind: str, payload: Dict[str, Any], detail: str = "") -> None:
        acct = str((payload or {}).get("account_id") or "")
        plat = str((payload or {}).get("platform") or "telegram")
        label = {"account_banned": "⛔ 账号疑似被封",
                 "account_paused": "⚠️ 账号被风控暂停"}.get(kind, f"账号事件 {kind}")
        text = f"{label}（{plat}:{acct}）：{detail}"
        try:
            notify(kind, text, account_id=acct, reason=detail, source=source)
        except Exception:
            logger.debug("[ops_alert] ban_signal 告警失败", exc_info=True)
    return _cb


__all__ = ["notify", "should_send", "make_ban_signal_alert"]
