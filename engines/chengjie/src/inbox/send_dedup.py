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

__all__ = ["SendDedup", "get_send_dedup", "resend_verdict",
           "RESEND_ALLOW", "RESEND_SUPPRESS_SENT", "RESEND_SUPPRESS_IN_FLIGHT",
           "RESEND_SUPPRESS_RESERVED"]

# ── 显式重发的幂等裁决（A1，#148 文本件，2026-09-03）────────────────────────────
# 事故（族4 第 6 层，钧机 64PY7D）：断连/慢发送窗里前端超时误标「发送失败」→ 坐席点
# 「重发」→ 旧前端**每次重发都现造新 client_msg_id**（"显式重试永不被去重拦截" 的旧
# 口径）→ 服务端眼里是全新一件，幂等键压根不认识它 → 客户收到两条一样的话。
# 媒体件已在 E2（v1.0.71）把幂等键随件固定；文本件走的是乐观气泡 _retryBody，
# 修法是让重发**带上原件的 client_msg_id**（``resend_of_cmid``），服务端按原 id 查
# 三态登记表 + 占位表，判「原件到底有没有发出去」再决定放行还是压制。
RESEND_ALLOW = "allow"
RESEND_SUPPRESS_SENT = "suppress_sent"
RESEND_SUPPRESS_IN_FLIGHT = "suppress_in_flight"
RESEND_SUPPRESS_RESERVED = "suppress_reserved"


def resend_verdict(prior_state: str, prior_reserved: bool) -> str:
    """原件状态 → 本次重发的裁决（纯函数，无 IO，可直接单测）。

    ``prior_state``＝``voice_send_tracker.get_status`` 的 state
    （``sent``/``failed``/``in_flight``/``unknown``）；``prior_reserved``＝
    ``SendDedup.holds`` 对原 id 的只读判定。

    - ``sent``      → 压制（原件已送达，再发就是双发——本 bug 的正主）；
    - ``in_flight`` → 压制（还在途；前端继续走 /send-status 对账收尾）；
    - ``failed``    → 放行（服务端明确认败，重发正是该做的事）；
    - ``unknown``   → 占位还在＝已发或在途（``release`` 只在失败路径调用）→ 压制；
                      占位也没了＝失败过、或后端重启把两张表都清了 → 放行。
                      **放行是这里的正确保守方向**：后端重启后无从得知，而漏发
                      （静默丢消息）比重发一条更贵，且坐席是显式点了重发。
    """
    st = str(prior_state or "unknown").strip().lower()
    if st == "sent":
        return RESEND_SUPPRESS_SENT
    if st == "in_flight":
        return RESEND_SUPPRESS_IN_FLIGHT
    if st == "failed":
        return RESEND_ALLOW
    return RESEND_SUPPRESS_RESERVED if prior_reserved else RESEND_ALLOW

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

    def holds(self, scope_key: str, client_msg_id: str) -> bool:
        """该 id 的占位是否仍在窗口内（**只读**，不占位、不计数）。

        A1（#148 文本件，2026-09-03）：``release`` 只在失败路径调用，所以
        「占位还在」＝这一笔**要么已成功、要么仍在途**，两种都不该再发一遍。
        供 ``resend_verdict`` 在 tracker 查无此键（条目过期）时兜底判定。
        """
        cid = str(client_msg_id or "").strip()
        if not cid:
            return False
        key = (str(scope_key or ""), cid)
        now = time.time()
        with self._lock:
            ts = self._seen.get(key)
            return ts is not None and now - ts <= self._window

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
