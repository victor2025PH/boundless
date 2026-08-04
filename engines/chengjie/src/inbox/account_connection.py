"""每 (platform, account_id) 的「接入本系统时刻」登记（冷启动预热窗的事实源）。

为什么需要它
============
主动触达的所有判据都是「会话沉默了多久」，唯独缺「**账号**接入多久」。一个刚登录的
Telegram 新号，目录同步会把它手机上的历史会话灌进来，个个「沉默几个月」→ 全员立刻
满足沉默阈值 → 登录第一个 tick 就群发（2026-08-04 智拓机事故）。补上 ``connected_at``
维度，配合 ``outbound_gate.account_warming`` 压住新号的冷启动期外呼。

connected_at 怎么来（相对最初方案的优化）
----------------------------------------
最初设想「在 telegram_client 登录处插桩记时刻」——但那是并发热区，且要覆盖多平台。
改为**自举**：首次见到某账号时，用「该账号所有会话里最早的 ``created_at``」当接入时刻。
目录同步把占位会话的 ``created_at`` 落为**同步时刻**（≈ 账号首次接入本系统的时刻），
于是：
- 真·新号：所有会话 created_at ≈ now → connected_at ≈ now → 判为预热中（正确）；
- 老账号：存在很早的 created_at → connected_at 很早 → 非预热（正确）。
无需改任何登录代码，也天然跨平台。

observe-once（冻结首见值）
-------------------------
``connected_at`` 一旦记下就**不再覆盖**：因为 ``list_conversations`` 只扫最近 N 条，
一个会话很多的账号，后续某轮扫到的最早 created_at 可能偏晚 → 若每轮重算会把老账号
误判成「刚接入」。首见冻结避免这种漂移。首见时若恰好扫不到早期会话（部署时账号已很
老且会话 >N），会一次性保守预热该账号一个窗口——主动外呼漏一窗零代价，可接受。

持久化
------
落 data 区 JSON（``account_connection.json``，与 ``companion_optout_mute.json`` 同目录
同风格）。**I/O 全程 best-effort**：文件读写失败一律降级为进程内内存字典，绝不把安全
闸变成新的崩溃点。
"""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional, Union

logger = logging.getLogger("ai_chat_assistant.account_connection")


def _key(platform: str, account_id: str) -> str:
    return f"{str(platform or 'telegram')}:{str(account_id or 'default')}"


class AccountConnectionLog:
    """(platform, account_id) → 接入时刻 的极小登记表（线程安全，best-effort 持久化）。

    ``path=None`` → 纯内存（测试 / 无可写目录时）。任何持久化异常都吞掉并继续用内存表。
    """

    def __init__(self, path: Optional[Union[str, Path]] = None) -> None:
        self._path = Path(path) if path else None
        self._lock = threading.Lock()
        self._data: Dict[str, Dict[str, float]] = {}
        self._load()

    def _load(self) -> None:
        if not self._path or not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text("utf-8")) or {}
            if isinstance(raw, dict):
                for k, v in raw.items():
                    if isinstance(v, dict) and v.get("connected_at"):
                        try:
                            self._data[str(k)] = {
                                "connected_at": float(v["connected_at"]),
                                "recorded_at": float(v.get("recorded_at") or 0.0),
                            }
                        except (TypeError, ValueError):
                            continue
        except Exception:
            logger.debug("account_connection 装载失败（降级内存表）", exc_info=True)

    def _persist_locked(self) -> None:
        if not self._path:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps(self._data, ensure_ascii=False), "utf-8")
        except Exception:
            logger.debug("account_connection 落盘失败（保留内存表）", exc_info=True)

    def get_connected_at(self, platform: str, account_id: str) -> Optional[float]:
        """已登记的接入时刻（epoch 秒）；未登记 → None。"""
        with self._lock:
            rec = self._data.get(_key(platform, account_id))
            return float(rec["connected_at"]) if rec else None

    def observe(
        self,
        platform: str,
        account_id: str,
        earliest_activity_ts: float,
        *,
        now: Optional[float] = None,
    ) -> float:
        """登记（若首见）并返回该账号的接入时刻。

        ``earliest_activity_ts``＝该账号所有会话里最早的 ``created_at``（epoch 秒）。
        - 首见：connected_at = 合理的 earliest（>0 且不晚于 now）否则 now；**写入即冻结**。
        - 已见：直接返回既有值（observe-once，不覆盖）。
        绝不抛：持久化失败也返回正确的内存值。
        """
        now = time.time() if now is None else float(now)
        k = _key(platform, account_id)
        with self._lock:
            rec = self._data.get(k)
            if rec and rec.get("connected_at"):
                return float(rec["connected_at"])
            try:
                seed = float(earliest_activity_ts or 0.0)
            except (TypeError, ValueError):
                seed = 0.0
            # 未知或时钟漂移到未来 → 用 now（保守：新号按 now 起算预热窗）。
            connected_at = seed if (0.0 < seed <= now) else now
            self._data[k] = {"connected_at": connected_at, "recorded_at": now}
            self._persist_locked()
            return connected_at


__all__ = ["AccountConnectionLog"]
