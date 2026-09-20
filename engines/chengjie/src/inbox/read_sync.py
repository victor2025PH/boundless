"""工作台「已读」→ 平台侧已读回执（read receipt）的纯决策层（2026-08-23）。

背景：坐席在工作台读消息时，平台侧（手机/Telegram 服务器）的未读数**从不**被
清掉——`orchestrator.mark_read` 此前只有 AI 自动回复链在调（protocol_autoreply /
autosend_helpers / holding_reply），人工阅读永远不发已读回执。后果＝协议号每轮
目录同步都带回一个不为 0 的 `unread` 残值，成为收件箱徽标「已读后回弹」的持续
供体（另一半修复是 effective_unread 的 last_in_ts 闸门，见 store.py）。

本模块只做三件事，全部纯函数/进程内状态，方便直测：
- ``push_enabled(cfg, platform)``：读 ``inbox.read_sync.push_to_platform`` 配置
  （bool=全平台 / dict 按平台 / list 白名单；**默认关**——已读回执是客户可见
  行为（双勾变蓝），开关属产品决策，AI 自动回复链已在发是先例）；
- ``should_push(cid, now)`` + ``record_push(cid, now)``：同会话节流（默认 20s，
  防快速切换会话时对同一线程连环回执）；
- ``note_result(ok)`` / ``stats_snapshot()``：推送成败计数（观测面）。

真正的编排（取会话行、判断残留未读、fire-and-forget 调 orchestrator）留在
mark-read 路由里——那里有 request/store/config_manager，且 orchestrator worker
与路由同在 web loop，`asyncio.create_task` 即可，绝不阻塞已读水位写入的返回。
"""

from __future__ import annotations

import time
from typing import Any, Dict, Mapping, Optional

PUSH_MIN_INTERVAL_SEC = 20.0
_MAX_KEYS = 2000   # 节流表内存上限（超限后最旧条目被挤出——纯保护，不追求精确 LRU）

_last_push: Dict[str, float] = {}
_stats: Dict[str, int] = {
    "pushed": 0,          # 已发起的平台回执次数（fire-and-forget 发起即计）
    "push_ok": 0,         # orchestrator 返回 True
    "push_fail": 0,       # orchestrator 返回 False / 抛异常
    "skipped_throttle": 0,
    "skipped_no_unread": 0,
}


def read_sync_cfg(cfg: Optional[Mapping[str, Any]]) -> Any:
    """取 ``inbox.read_sync.push_to_platform`` 原始配置值（缺省 None=关）。"""
    try:
        inbox = (cfg or {}).get("inbox") or {}
        rs = inbox.get("read_sync") or {}
        return rs.get("push_to_platform")
    except Exception:
        return None


def push_enabled(cfg: Optional[Mapping[str, Any]], platform: str) -> bool:
    """该平台是否开启「工作台已读 → 平台回执」。

    支持三种运营写法（overlay 手改友好）：
    - ``push_to_platform: true``            → 全平台开；
    - ``push_to_platform: {telegram: true}``→ 按平台开；
    - ``push_to_platform: [telegram, whatsapp]`` → 白名单。
    其余/缺省一律 False（默认关；Messenger worker 本就无 mark_read，开了也只是
    orchestrator best-effort False，无害）。
    """
    raw = read_sync_cfg(cfg)
    p = str(platform or "").strip().lower()
    if raw is None or not p:
        return False
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, Mapping):
        return bool(raw.get(p))
    if isinstance(raw, (list, tuple, set)):
        return p in {str(x or "").strip().lower() for x in raw}
    return False


def should_push(conversation_id: str, now: Optional[float] = None) -> bool:
    """同会话节流：距上次发起 < PUSH_MIN_INTERVAL_SEC → False（并计数）。"""
    cid = str(conversation_id or "")
    if not cid:
        return False
    ts = float(now if now is not None else time.time())
    last = _last_push.get(cid, 0.0)
    if ts - last < PUSH_MIN_INTERVAL_SEC:
        _stats["skipped_throttle"] += 1
        return False
    return True


def record_push(conversation_id: str, now: Optional[float] = None) -> None:
    cid = str(conversation_id or "")
    if not cid:
        return
    if len(_last_push) >= _MAX_KEYS:
        try:
            _last_push.pop(next(iter(_last_push)))
        except (StopIteration, KeyError):
            _last_push.clear()
    _last_push[cid] = float(now if now is not None else time.time())
    _stats["pushed"] += 1


def note_no_unread() -> None:
    _stats["skipped_no_unread"] += 1


def note_result(ok: bool) -> None:
    _stats["push_ok" if ok else "push_fail"] += 1


def stats_snapshot() -> Dict[str, int]:
    return dict(_stats)


def _reset_for_tests() -> None:
    _last_push.clear()
    for k in _stats:
        _stats[k] = 0
