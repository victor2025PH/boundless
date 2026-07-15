"""消息级去重 + 会话级串行锁（2026-07-15「三连发语音」事故修复）。

事故根因：私聊实时 handler 从不登记 message_id 去重，而语音回复链路
（转录→LLM→TTS→拟人节奏发送）耗时 >12s 轮询间隔，轮询兜底在回复落地前把
同一条消息当新消息再处理一遍 → 同一句话两条流水线并行生成两个矛盾回复
（"还没吃" vs "刚吃了面包"）、连发 3 条语音。

设计：
- ``MessageDedup``：``(chat_id, message_id)`` 复合键的 claim 语义 LRU/TTL 表。
  复合键根治 supergroup per-channel 消息 id 跨群相撞（裸 mid 去重的隐藏 bug：
  两个活跃群 10 分钟内出现相同 id 会被误判重复而静默丢消息）。
  claim=「检查+登记」一步完成，中间无 await → asyncio 单线程内原子；三条入站
  路径（私聊实时 / 群实时 / 轮询兜底）汇聚点统一 claim 即天然互斥。
- ``PerChatLocks``：per-chat ``asyncio.Lock`` 注册表——同一会话的回复生成串行
  化（防同会话两条消息并行生成、上下文互相不可见 → 前后事实矛盾），不同会话
  互不影响；带容量剪枝（只清当前未被持有的锁）。
"""
from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from typing import Any, Callable, Optional


class MessageDedup:
    """``(chat_id, message_id)`` 去重表：claim 语义 + TTL/容量双剪枝。纯内存。

    ``clock`` 可注入（测试用），默认 ``time.time``。
    """

    def __init__(self, *, max_size: int = 2000, ttl_sec: float = 600.0,
                 clock: Optional[Callable[[], float]] = None):
        self.max_size = int(max_size)
        self.ttl_sec = float(ttl_sec)
        self._clock = clock or time.time
        self._seen: "OrderedDict[str, float]" = OrderedDict()  # key -> 登记时刻

    @staticmethod
    def _key(chat_id: Any, message_id: Any) -> str:
        return f"{chat_id}:{message_id}"

    def seen(self, chat_id: Any, message_id: Any) -> bool:
        """只读检查（不登记）：轮询扫描过滤用。TTL 过期视为未见。"""
        if not message_id:
            return False
        ts = self._seen.get(self._key(chat_id, message_id))
        if ts is None:
            return False
        if self.ttl_sec > 0 and (self._clock() - ts) > self.ttl_sec:
            return False
        return True

    def claim(self, chat_id: Any, message_id: Any) -> bool:
        """检查+登记一步完成。True=首见（已登记，调用方继续处理）；False=重复。

        无 message_id（0/None）→ True 放行（与旧行为一致：无法判定就不拦）。
        检查与登记之间无 await → asyncio 单线程内原子，天然防并发双处理。
        """
        if not message_id:
            return True
        key = self._key(chat_id, message_id)
        now = self._clock()
        ts = self._seen.get(key)
        if ts is not None and (self.ttl_sec <= 0 or (now - ts) <= self.ttl_sec):
            return False
        self._seen[key] = now
        self._seen.move_to_end(key)
        self._prune(now)
        return True

    def _prune(self, now: float) -> None:
        while self._seen:
            _k, _ts = next(iter(self._seen.items()))
            if (self.ttl_sec > 0 and now - _ts > self.ttl_sec) or \
                    len(self._seen) > self.max_size:
                self._seen.popitem(last=False)
            else:
                break

    def __len__(self) -> int:
        return len(self._seen)


class PerChatLocks:
    """per-chat ``asyncio.Lock`` 注册表：同会话串行、跨会话并行。

    用法：``async with locks.lock(chat_id): ...``。锁应在全局并发信号量**之外**
    获取——同一会话排队等待时不占并发槽，其他会话不被饿死。
    """

    def __init__(self, *, max_size: int = 512):
        self.max_size = int(max_size)
        self._locks: "OrderedDict[str, asyncio.Lock]" = OrderedDict()

    def lock(self, chat_id: Any) -> asyncio.Lock:
        key = str(chat_id if chat_id is not None else "")
        lk = self._locks.get(key)
        if lk is None:
            lk = asyncio.Lock()
            self._locks[key] = lk
        self._locks.move_to_end(key)
        if len(self._locks) > self.max_size:
            self._prune()
        return lk

    def _prune(self) -> None:
        """只清「当前未被持有」的最旧条目——持有中的锁绝不移除（移除会破坏互斥）。"""
        for key in list(self._locks.keys()):
            if len(self._locks) <= self.max_size:
                break
            lk = self._locks[key]
            if not lk.locked():
                del self._locks[key]

    def __len__(self) -> int:
        return len(self._locks)
