"""A 线语音欠账台账（voice IOU——「下一回合语音升级」，2026-08-02）。

产品语义：诚实回落（``voice_honest_fallback``）对客户说了「语音这会儿发不出去…
回头补给你」之后，这个承诺必须真兑现。刻意**不做后台主动补发**（免去静音时段/
跨线程投递/打扰判定整套复杂度），改为「下一回合语音升级」：该会话下一次生成
回复时，若语音链已恢复，则视同客户点名要语音强制走语音——从行为上兑现欠账。

记账口径（防「永不过期」）：``record_iou`` **只**在诚实回落改写确实发生且客户
点名要过语音（``wants_media(peer_text)=='voice'``）时调用；兑现失败不重复记账
（重复覆盖时间戳会让 TTL 永远走不完），TTL 内下次回合再试，超 TTL 自然过期。

存储：进程级注册表 + JSON 落盘 ``config/voice_iou.json``（进程 CWD=实例数据根，
本仓 C 类相对路径惯例——服务进程落实例数据根恰是想要的）。写失败静默、读坏
文件当空表；每 chat 只保留最新一条（覆盖）。所有公开函数防御式永不抛，
线程安全（模块级锁）。测试经 ``store_path`` 参数指向 tmp，绝不写仓库 config/。

开关 ``telegram.voice_reply.voice_iou.enabled``（**默认关**，新子系统铁律；
主线在 overlay 灰度开）。
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

DEFAULT_TTL_HOURS = 24.0
# C 类相对路径：进程 CWD=实例数据根（与 logs/、config/*.db 家族同惯例）
DEFAULT_STORE_PATH = Path("config") / "voice_iou.json"
_MAX_ENTRIES = 500          # 防长期运行撑爆（超限剪最老）

_LOCK = threading.RLock()
_STATE: Dict[str, Dict[str, Any]] = {}   # chat_key -> {"ts": float, "persona_id": str}
_LOADED_FOR: Optional[str] = None        # 已加载的落盘路径（换路径即重载，测试友好）


def parse_iou_cfg(config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """解析 ``telegram.voice_reply.voice_iou``（缺失/坏形一律回默认：关）。"""
    try:
        blk = ((((config or {}).get("telegram") or {}).get("voice_reply")
                or {}).get("voice_iou"))
        blk = dict(blk) if isinstance(blk, dict) else {}
    except Exception:
        blk = {}
    out: Dict[str, Any] = {"enabled": bool(blk.get("enabled", False))}
    try:
        out["ttl_hours"] = max(0.0, float(blk.get("ttl_hours", DEFAULT_TTL_HOURS)))
    except Exception:
        out["ttl_hours"] = DEFAULT_TTL_HOURS
    return out


def _resolve_path(store_path: Optional[Any]) -> Path:
    return Path(store_path) if store_path is not None else DEFAULT_STORE_PATH


def _ensure_loaded(path: Path) -> None:
    """按需从落盘文件装载（调用方须持锁）。坏文件/缺文件＝空表。"""
    global _LOADED_FOR
    key = str(path)
    if _LOADED_FOR == key:
        return
    _STATE.clear()
    _LOADED_FOR = key
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return
    if not isinstance(raw, dict):
        return
    for k, v in raw.items():
        try:
            if isinstance(v, dict):
                ts = float(v.get("ts", 0.0) or 0.0)
                pid = str(v.get("persona_id", "") or "")
            else:                      # 容忍扁平 {chat: ts} 旧形
                ts = float(v or 0.0)
                pid = ""
            if ts > 0:
                _STATE[str(k)] = {"ts": ts, "persona_id": pid}
        except Exception:
            continue


def _persist(path: Path) -> None:
    """best-effort 落盘（调用方须持锁）；失败静默——台账丢了最多少补一条语音。"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(_STATE, ensure_ascii=False, indent=1),
            encoding="utf-8")
    except Exception:
        pass


def record_iou(chat_key: str, persona_id: str = "",
               now: Optional[float] = None, *,
               store_path: Optional[Any] = None) -> None:
    """登记一条语音欠账（每 chat 只保留最新一条，覆盖）。"""
    try:
        key = str(chat_key or "").strip()
        if not key:
            return
        ts = float(now if now is not None else time.time())
        path = _resolve_path(store_path)
        with _LOCK:
            _ensure_loaded(path)
            _STATE[key] = {"ts": ts, "persona_id": str(persona_id or "")}
            if len(_STATE) > _MAX_ENTRIES:
                overflow = len(_STATE) - _MAX_ENTRIES
                for old in sorted(
                        _STATE, key=lambda k: _STATE[k].get("ts", 0.0)
                )[:overflow]:
                    _STATE.pop(old, None)
            _persist(path)
    except Exception:
        pass


def pending_iou(chat_key: str, *, ttl_hours: float = DEFAULT_TTL_HOURS,
                now: Optional[float] = None,
                store_path: Optional[Any] = None) -> bool:
    """该会话是否有未兑现欠账。超 TTL 视为过期（顺手清理落盘）。"""
    try:
        key = str(chat_key or "").strip()
        if not key:
            return False
        ts_now = float(now if now is not None else time.time())
        path = _resolve_path(store_path)
        with _LOCK:
            _ensure_loaded(path)
            row = _STATE.get(key)
            if not isinstance(row, dict):
                return False
            age_sec = ts_now - float(row.get("ts", 0.0) or 0.0)
            if age_sec > max(0.0, float(ttl_hours)) * 3600.0:
                _STATE.pop(key, None)
                _persist(path)
                return False
            return True
    except Exception:
        return False


def clear_iou(chat_key: str, now: Optional[float] = None, *,
              store_path: Optional[Any] = None) -> None:
    """欠账已补（语音真发成功）→ 清除条目。``now`` 仅为签名对称保留。"""
    try:
        key = str(chat_key or "").strip()
        if not key:
            return
        path = _resolve_path(store_path)
        with _LOCK:
            _ensure_loaded(path)
            if _STATE.pop(key, None) is not None:
                _persist(path)
    except Exception:
        pass


def iou_snapshot(now: Optional[float] = None, *,
                 store_path: Optional[Any] = None) -> Dict[str, Any]:
    """观测快照：pending 数 + 最老欠账时长（秒）。永不抛。"""
    try:
        ts_now = float(now if now is not None else time.time())
        path = _resolve_path(store_path)
        with _LOCK:
            _ensure_loaded(path)
            oldest = 0.0
            for row in _STATE.values():
                try:
                    age = ts_now - float(row.get("ts", 0.0) or 0.0)
                except Exception:
                    continue
                if age > oldest:
                    oldest = age
            return {"pending": len(_STATE),
                    "oldest_age_sec": round(max(0.0, oldest), 1)}
    except Exception:
        return {"pending": 0, "oldest_age_sec": 0.0}


def _reset_state_for_tests() -> None:
    """仅测试用：清空进程注册表并强制下次访问重新装载。"""
    global _LOADED_FOR
    with _LOCK:
        _STATE.clear()
        _LOADED_FOR = None


__all__ = [
    "DEFAULT_STORE_PATH", "DEFAULT_TTL_HOURS", "clear_iou", "iou_snapshot",
    "parse_iou_cfg", "pending_iou", "record_iou",
]
