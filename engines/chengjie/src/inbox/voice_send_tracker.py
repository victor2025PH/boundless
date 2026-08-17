# -*- coding: utf-8 -*-
"""坐席语音发送结果登记表（P1-1 2026-08-11）——「前端超时 ≠ 发送失败」的对账面。

事故面：send-voice 全链（原文直念后仍有 合成→回验→转码→上传）最坏可迫近前端
60s 等待线；网络抖动/前端超时后坐席只看到「失败」，而服务端往往仍在继续并最终
把语音发给了客户——坐席按直觉重试＝客户收到两条一样的语音。幂等键只拦「同
client_msg_id 的重放」，拦不住人工重试产生的新键，所以需要一个**对账面**：
本表以 (scope, client_msg_id) 登记每次发送的阶段与终局，配套
``GET /api/unified-inbox/send-voice-status`` 让前端在超时后先对账、再决定
「按成功收尾 / 报失败 / 提示人工确认」，从流程上消灭盲目重发。

设计约束：
- 进程级内存表（重启即清）：对账窗口是分钟级的，重启丢失只意味着一次
  「unknown」保守提示，不值得为它引入持久化。
- 容量/寿命双上限（TTL 15min + 400 条）防撑爆；淘汰在写入口顺手做，无后台线程。
- 纯函数式接口 + 可注入时钟（``now``），可直接单测。
- 所有接口对空 scope/client_msg_id no-op / 返回 unknown——老前端不带键也绝不抛。

状态机：in_flight（可带 stage: synth→convert→send）→ sent | failed。
查无此键 → unknown（含：老后端刚重启、条目过期、压根没提交成功）。
"""
from __future__ import annotations

import threading
import time
from typing import Any, Dict, Optional

_TTL_SEC = 15 * 60.0
_CAP = 400
_LOCK = threading.Lock()
_ENTRIES: Dict[str, Dict[str, Any]] = {}

_STAGES = ("synth", "convert", "send")


def _key(scope: str, client_msg_id: str) -> str:
    return f"{scope}\x1f{client_msg_id}"


def _evict_locked(now: float) -> None:
    dead = [k for k, e in _ENTRIES.items() if now - float(e.get("ts", 0.0)) > _TTL_SEC]
    for k in dead:
        _ENTRIES.pop(k, None)
    # >= ：本函数在插入**之前**调用，腾出一个坑位保证插入后仍 ≤ _CAP
    while len(_ENTRIES) >= _CAP:
        oldest = min(_ENTRIES, key=lambda k: float(_ENTRIES[k].get("ts", 0.0)))
        _ENTRIES.pop(oldest, None)


def record_start(scope: str, client_msg_id: str, *, now: Optional[float] = None) -> None:
    """登记一次发送开始（stage=synth）。同键重入＝覆盖（失败后显式重试）。"""
    if not scope or not client_msg_id:
        return
    ts = time.time() if now is None else float(now)
    with _LOCK:
        _evict_locked(ts)
        _ENTRIES[_key(scope, client_msg_id)] = {
            "ts": ts, "state": "in_flight", "stage": "synth",
            "reason": "", "payload": {},
        }


def record_stage(scope: str, client_msg_id: str, stage: str) -> None:
    """推进阶段（仅 in_flight 时生效；未知 stage 忽略）。"""
    if not scope or not client_msg_id or stage not in _STAGES:
        return
    with _LOCK:
        e = _ENTRIES.get(_key(scope, client_msg_id))
        if e is not None and e.get("state") == "in_flight":
            e["stage"] = stage


def record_sent(scope: str, client_msg_id: str,
                payload: Optional[Dict[str, Any]] = None) -> None:
    """终局：已发出（payload 通常带 voice_meta，前端对账成功时可复现成功提示）。"""
    if not scope or not client_msg_id:
        return
    with _LOCK:
        e = _ENTRIES.get(_key(scope, client_msg_id))
        if e is None:
            e = {"ts": time.time()}
            _ENTRIES[_key(scope, client_msg_id)] = e
        e["state"] = "sent"
        e["stage"] = "send"
        e["payload"] = dict(payload or {})


def record_failed(scope: str, client_msg_id: str, reason: str = "") -> None:
    """终局：确定失败（reason=机器可读码，前端可映射人话）。"""
    if not scope or not client_msg_id:
        return
    with _LOCK:
        e = _ENTRIES.get(_key(scope, client_msg_id))
        if e is None:
            e = {"ts": time.time(), "stage": ""}
            _ENTRIES[_key(scope, client_msg_id)] = e
        e["state"] = "failed"
        e["reason"] = str(reason or "")[:120]


def get_status(scope: str, client_msg_id: str,
               *, now: Optional[float] = None) -> Dict[str, Any]:
    """查询对账状态。查无此键/过期 → {"state": "unknown"}。"""
    if not scope or not client_msg_id:
        return {"state": "unknown"}
    ts_now = time.time() if now is None else float(now)
    with _LOCK:
        e = _ENTRIES.get(_key(scope, client_msg_id))
        if e is None or ts_now - float(e.get("ts", 0.0)) > _TTL_SEC:
            return {"state": "unknown"}
        out = {
            "state": str(e.get("state") or "unknown"),
            "stage": str(e.get("stage") or ""),
            "reason": str(e.get("reason") or ""),
            "elapsed_ms": int(max(0.0, ts_now - float(e.get("ts", 0.0))) * 1000),
        }
        payload = e.get("payload")
        if payload:
            out["voice_meta"] = dict(payload.get("voice_meta") or {})
            if "reused_preview" in payload:
                out["reused_preview"] = bool(payload.get("reused_preview"))
        return out


def reset_state() -> None:
    """清空（测试用）。"""
    with _LOCK:
        _ENTRIES.clear()


__all__ = [
    "record_start", "record_stage", "record_sent", "record_failed",
    "get_status", "reset_state",
]
