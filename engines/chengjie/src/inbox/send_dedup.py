"""出站发送幂等去重（P0 多开/双击防双发，2026-07-29）。

问题：``/api/unified-inbox/send*`` 无幂等键——同一坐席开两个窗口（或双击/网络层
重放）对同一会话各提交一次，客户就收到两条一样的消息。陪伴场景里同句复读是
「人设崩塌」级事故；跨窗口场景前端锁管不住（第二台电脑/另一浏览器），必须服务端兜底。

方案：前端每次提交生成 ``client_msg_id``（uuid），服务端按
``(scope_key, client_msg_id)`` 在进程内 TTL 窗口去重：

- ``reserve()`` 返回 ``True``＝首见（放行发送并占位）；``False``＝窗口内重复（拒发）。
- 发送**失败**时调用 ``release()`` 释放占位——客户端携同一 id 的显式重试仍可通过
  （幂等键语义：同 id 至多**成功**一次，而非至多尝试一次）。
- 老客户端不带 id → 完全走旧行为（零破坏）。

刻意进程内不落库：双实例（智聊/通译）各管各的会话集，无跨进程会话；进程重启丢
占位窗口的代价是「重启瞬间的重复窗口」，可接受且远优于给发送热路径加一张表。

**进程内为何是正确选择（不是将就）**：本引擎架构上就是单进程——
``bootstrap/web_app`` 用 ``uvicorn.Config(app, host, port)`` + ``uvicorn.Server``
在**独立线程**里跑（**无 workers 参数**），而同一进程还持有 Telegram pyrogram 客户端、
账号编排器、AutosendWorker 等进程级单例。多进程 web worker 会让两个进程抢同一个
Telegram 会话——那正是本仓反复设防的「幽灵实例」事故（见 ``classify_web_serve_outcome``
的取证注释）。因此「同一会话的两次发送落在不同进程」在当前架构下不可能发生。

⚠️ 唯一会让本模块**静默失效**的场景：将来若真把 web 层拆成多进程/多副本（需先解决
TG 会话独占问题）。届时必须把占位表换成共享存储（Redis/DB 唯一索引），否则去重
形同不存在**且没有任何告警**。改部署形态前请先回来读这段。
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Tuple

__all__ = ["SendDedup", "get_send_dedup"]

# 去重窗口：覆盖「双窗口先后点发送」「前端超时重放」的现实时距；
# 超过窗口的同 id 重放按新消息放行（客户端 uuid 按次生成，正常不会撞）。
_DEFAULT_WINDOW_SEC = 600.0
# 容量上限：防异常客户端刷 id 撑爆内存（LRU 淘汰最旧占位）。
_DEFAULT_MAX_ENTRIES = 4096


class SendDedup:
    """进程内 (scope_key, client_msg_id) → 占位时间 的 TTL/LRU 去重表（线程安全）。"""

    def __init__(
        self,
        *,
        window_sec: float = _DEFAULT_WINDOW_SEC,
        max_entries: int = _DEFAULT_MAX_ENTRIES,
    ) -> None:
        self._window = float(window_sec)
        self._cap = max(16, int(max_entries))
        self._seen: "OrderedDict[Tuple[str, str], float]" = OrderedDict()
        self._lock = threading.Lock()
        # 观测计数（autosend-status / 将来 ops 卡可读）
        self.total_reserved = 0
        self.total_duplicates = 0
        self.total_released = 0

    def _evict(self, now: float) -> None:
        """锁内调用：清过期 + 超容淘汰最旧。"""
        while self._seen:
            key, ts = next(iter(self._seen.items()))
            if now - ts > self._window or len(self._seen) > self._cap:
                self._seen.popitem(last=False)
                continue
            break
        while len(self._seen) > self._cap:
            self._seen.popitem(last=False)

    def reserve(self, scope_key: str, client_msg_id: str) -> bool:
        """占位：首见返回 True（调用方继续发送），窗口内重复返回 False（拒发）。

        空 ``client_msg_id`` 恒返回 True（老客户端旧行为）。
        """
        cid = str(client_msg_id or "").strip()
        if not cid:
            return True
        key = (str(scope_key or ""), cid)
        now = time.time()
        with self._lock:
            self._evict(now)
            ts = self._seen.get(key)
            if ts is not None and now - ts <= self._window:
                self.total_duplicates += 1
                return False
            self._seen[key] = now
            self._seen.move_to_end(key)
            self._evict(now)   # 插入后再收口一次，保证 entries 恒 ≤ cap
            self.total_reserved += 1
            return True

    def release(self, scope_key: str, client_msg_id: str) -> None:
        """发送失败时释放占位：同 id 显式重试可再次通过（成功才终身占位到窗口尾）。"""
        cid = str(client_msg_id or "").strip()
        if not cid:
            return
        key = (str(scope_key or ""), cid)
        with self._lock:
            if self._seen.pop(key, None) is not None:
                self.total_released += 1

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "entries": len(self._seen),
                "total_reserved": self.total_reserved,
                "total_duplicates": self.total_duplicates,
                "total_released": self.total_released,
            }


_GLOBAL: SendDedup | None = None
_GLOBAL_LOCK = threading.Lock()


def get_send_dedup() -> SendDedup:
    """进程级单例（发送路由共用；风格对齐 outbound_translation_stats）。"""
    global _GLOBAL
    if _GLOBAL is None:
        with _GLOBAL_LOCK:
            if _GLOBAL is None:
                _GLOBAL = SendDedup()
    return _GLOBAL
