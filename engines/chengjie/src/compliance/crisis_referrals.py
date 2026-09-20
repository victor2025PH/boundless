"""危机转介持久计数（WP-4：SB 243 §2027-07 年报要求的核心数字）。

SB 243 要求运营方年报「向危机服务转介的次数」。现有危机闭环
（``_apply_crisis_safety_net`` 识别 → 安全回复覆盖 → ``crisis_resource_assurance``
热线补挂）只有日志，没有可导出的持久数字——本模块补上：

- ``record_crisis_referral(kind)``：处置路径打点（kind 见 ``REFERRAL_KINDS``）；
- 持久化＝实例数据根 JSON 日桶（``compliance_crisis_referrals.json``），重启不清零、
  按日可回溯（年报要的是区间合计，日桶是最小充分粒度）；写入频率＝危机事件级
  （稀疏），JSON + 进程锁 + 原子替换足够，刻意不建 sqlite（对齐「计数器风格
  对齐 frontend_error_stats（进程单例）+ 持久化」的规格要求，持久层用日桶文件
  替代审计行——审计库 ops_events 正被他线活跃编辑，零碰撞）；
- ``referral_snapshot()``：只读导出（``GET /api/admin/crisis-referrals`` 消费）。

打点语义（三类分开数，年报口径由运营按需取用）：
- ``severe_detected``     识别为 severe 危机（进入处置分支）；
- ``safe_reply_override`` 回复触红线被安全回复整段覆盖；
- ``resource_appended``   热线/危机资源真的补进了回复（＝法条意义上的「转介」，
  年报主数字）。

全部绝不抛；文件坏＝从零重建（历史丢失会在日志留 WARNING——年报数字建议
配合实例备份策略保存）。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

STATE_FILENAME = "compliance_crisis_referrals.json"

REFERRAL_KINDS = ("severe_detected", "safe_reply_override", "resource_appended")

_MAX_DAYS = 800          # 两个年报周期 + 余量；超限剔最旧日桶（total 不受影响）

_LOCK = threading.Lock()
_CACHE: Optional[Dict[str, Any]] = None


def _state_path() -> Path:
    from src.licensing.data_paths import config_dir

    return config_dir() / STATE_FILENAME


def _default_state() -> Dict[str, Any]:
    return {"days": {}, "total": {}, "updated_at": 0}


def _load() -> Dict[str, Any]:
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    st = _default_state()
    try:
        p = _state_path()
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                days = data.get("days")
                total = data.get("total")
                st["days"] = dict(days) if isinstance(days, dict) else {}
                st["total"] = {str(k): int(v or 0) for k, v in total.items()} \
                    if isinstance(total, dict) else {}
                st["updated_at"] = int(data.get("updated_at") or 0)
    except Exception:
        logger.warning("[compliance] 危机转介计数文件损坏（从零重建；年报历史请查备份）",
                       exc_info=True)
        st = _default_state()
    _CACHE = st
    return _CACHE


def _save() -> None:
    try:
        st = _CACHE or _default_state()
        days = st.get("days") or {}
        if len(days) > _MAX_DAYS:
            keep = sorted(days.keys())[-_MAX_DAYS:]
            st["days"] = {d: days[d] for d in keep}
        p = _state_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, p)
    except Exception:
        logger.warning("[compliance] 危机转介计数落盘失败", exc_info=True)


def record_crisis_referral(kind: str, *, now: Optional[float] = None) -> None:
    """危机处置路径打点（未知 kind 归 ``other``；绝不抛、绝不阻塞处置链）。"""
    try:
        k = str(kind or "").strip() or "other"
        if k not in REFERRAL_KINDS:
            k = "other"
        ts = float(now if now is not None else time.time())
        day = time.strftime("%Y-%m-%d", time.localtime(ts))
        with _LOCK:
            st = _load()
            bucket = st["days"].setdefault(day, {})
            bucket[k] = int(bucket.get(k) or 0) + 1
            st["total"][k] = int(st["total"].get(k) or 0) + 1
            st["updated_at"] = int(ts)
            _save()
    except Exception:
        logger.debug("[compliance] record_crisis_referral 失败（已忽略）",
                     exc_info=True)


def referral_snapshot(days: int = 90) -> Dict[str, Any]:
    """只读导出：总计 + 近 N 日日桶（升序）。绝不抛。"""
    try:
        n = max(1, min(int(days or 90), _MAX_DAYS))
    except Exception:
        n = 90
    with _LOCK:
        st = _load()
        day_keys = sorted((st.get("days") or {}).keys())[-n:]
        return {
            "kinds": list(REFERRAL_KINDS),
            "total": dict(st.get("total") or {}),
            "days": {d: dict(st["days"][d]) for d in day_keys},
            "updated_at": int(st.get("updated_at") or 0),
        }


def _reset_for_tests() -> None:
    global _CACHE
    with _LOCK:
        _CACHE = None


__all__ = [
    "STATE_FILENAME",
    "REFERRAL_KINDS",
    "record_crisis_referral",
    "referral_snapshot",
]
