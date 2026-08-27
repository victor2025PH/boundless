"""companion proactive_topic 派发观测（Phase14；P1 2026-07-29 扩归因）。

P1 新增四组读数（修「配置 50%/25% 实际 6%/0% 无人知晓」「兜底占 100% 上线
两周才被用户投诉发现」两类观测盲区）：
- ``sent_modes``：真发成功的 mode 分布（gentle_checkin 占比一眼可见）；
- ``media_skips``：语音/照片分支逐层跳过原因计数（gate 概率/亲密度/无通道/
  合成失败/投递失败……）；
- ``variety_blocks``：变体守卫拦下的复读文案次数；
- ``optout_mutes``：opt-out 识别静默的会话数。
全部进程级计数，经 /api/companion/proactive/status 出看板。
"""
from __future__ import annotations

import threading
import time
from typing import Any, Dict

_lock = threading.Lock()
_ticks = 0
_planned_sum = 0
_sent_sum = 0
_voice_sent = 0
_voice_foreign_sent = 0
_photo_sent = 0
_variety_blocks = 0
_optout_mutes = 0
_checkin_gate_skips = 0
_fabrication_blocks = 0
_lang_gate_blocks = 0
_wall_skips = 0
_season_blocks = 0
_greeting_time_blocks = 0
_sent_modes: Dict[str, int] = {}
_media_skips: Dict[str, Dict[str, int]] = {}
_last: Dict[str, Any] = {}

# distinct 上限防撑爆（mode/reason 是代码枚举，正常远小于此）
_MAX_KEYS = 32


def record_voice(*, foreign: bool = False) -> None:
    global _voice_sent, _voice_foreign_sent
    with _lock:
        if foreign:
            _voice_foreign_sent += 1
        else:
            _voice_sent += 1


def record_photo() -> None:
    """主动生活照发出计数（Phase16）。"""
    global _photo_sent
    with _lock:
        _photo_sent += 1


def record_sent_mode(mode: str) -> None:
    """真发成功的开场 mode 计数（P1：兜底占比可见化）。"""
    m = str(mode or "unknown")[:32]
    with _lock:
        if m in _sent_modes or len(_sent_modes) < _MAX_KEYS:
            _sent_modes[m] = _sent_modes.get(m, 0) + 1


def record_media_skip(kind: str, reason: str) -> None:
    """语音/照片分支跳过原因计数（P1 透传归因）。"""
    k = str(kind or "unknown")[:16]
    r = str(reason or "unknown")[:32]
    with _lock:
        bucket = _media_skips.setdefault(k, {})
        if r in bucket or len(bucket) < _MAX_KEYS:
            bucket[r] = bucket.get(r, 0) + 1


def record_variety_block() -> None:
    """变体守卫拦截计数（拦下=避免了一次复读连发）。"""
    global _variety_blocks
    with _lock:
        _variety_blocks += 1


def record_optout_mute() -> None:
    """opt-out 静默计数（用户说「别再发了」被听懂的次数）。"""
    global _optout_mutes
    with _lock:
        _optout_mutes += 1


def record_checkin_gate() -> None:
    """checkin 效能门控跳过计数（P5：数据说 checkin 不如不发的那些「没发」）。"""
    global _checkin_gate_skips
    with _lock:
        _checkin_gate_skips += 1


def record_fabrication_block() -> None:
    """反编造守卫拦截计数（P1 2026-08-03：拦下=避免了一次「编造共同回忆」穿帮）。"""
    global _fabrication_blocks
    with _lock:
        _fabrication_blocks += 1


def record_lang_gate_block() -> None:
    """出站语言闸拦截计数（P2-198 2026-08-04：拦下=避免了一次「给外语客户
    发中文开场」穿帮——生产实锤 telegram:8244899… 30 天 6 条中文晨安）。"""
    global _lang_gate_blocks
    with _lock:
        _lang_gate_blocks += 1


def record_wall_skip() -> None:
    """未回消息墙拦截计数（2026-08-18：拦下=避免了一次「往 ≥N 条未回消息上
    再堆一条」的刷屏——对方打开聊天看到连排早安是流失级体验）。"""
    global _wall_skips
    with _lock:
        _wall_skips += 1


def record_season_block() -> None:
    """季节守卫拦截计数（2026-08-18「迎新表演」事故：拦下=避免了一次
    「8 月声称在排迎新」式的反季穿帮）。"""
    global _season_blocks
    with _lock:
        _season_blocks += 1


def record_greeting_time_block() -> None:
    """问候词×时刻守卫拦截计数（2026-08-19「上午晚安」事故：拦下=避免了
    一次「问候词与收件人时刻矛盾」的当场穿帮）。"""
    global _greeting_time_blocks
    with _lock:
        _greeting_time_blocks += 1


def record_tick(*, planned: int, sent: int, dry_run: bool = False) -> None:
    global _ticks, _planned_sum, _sent_sum, _last
    with _lock:
        _ticks += 1
        _planned_sum += max(0, int(planned))
        _sent_sum += max(0, int(sent))
        _last = {
            "planned": int(planned),
            "sent": int(sent),
            "dry_run": bool(dry_run),
            "ts": time.time(),
        }


def metrics_snapshot() -> Dict[str, Any]:
    with _lock:
        return {
            "ticks": _ticks,
            "planned_sum": _planned_sum,
            "sent_sum": _sent_sum,
            "voice_sent": _voice_sent,
            "voice_foreign_sent": _voice_foreign_sent,
            "photo_sent": _photo_sent,
            "variety_blocks": _variety_blocks,
            "optout_mutes": _optout_mutes,
            "checkin_gate_skips": _checkin_gate_skips,
            "fabrication_blocks": _fabrication_blocks,
            "lang_gate_blocks": _lang_gate_blocks,
            "wall_skips": _wall_skips,
            "season_blocks": _season_blocks,
            "greeting_time_blocks": _greeting_time_blocks,
            "sent_modes": dict(_sent_modes),
            "media_skips": {k: dict(v) for k, v in _media_skips.items()},
            "last_tick": dict(_last),
        }


__all__ = [
    "record_tick", "record_voice", "record_photo",
    "record_sent_mode", "record_media_skip",
    "record_variety_block", "record_optout_mute",
    "record_checkin_gate", "record_fabrication_block",
    "record_lang_gate_block", "record_wall_skip", "record_season_block",
    "record_greeting_time_block",
    "metrics_snapshot",
]
