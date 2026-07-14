# -*- coding: utf-8 -*-
"""接收方账号管理 (Phase 6.B · 2026-04-23)。

每个 receiver 是一个接收引流的账号 (通常是一个 LINE / WhatsApp 号),
handoff 生成时根据 channel + persona 选 receiver + 轮转 backup。

配置源: config/referral_receivers.yaml (热加载, 改完保存即生效)
配置模板: config/referral_receivers.yaml.example

核心 API
--------
    load_receivers()                   # 读配置
    get_receiver(key)                  # 单个
    list_receivers(channel=, enabled_only=)
    count_today_handoffs(receiver_key) # 今日负载
    pick_receiver(channel, persona=)   # 智能选: cap 满 → backup → 轮转
    receiver_load(key)                 # {current, cap, percent_used, remaining}
    save_receivers(data)               # 管理员侧写回(Dashboard UI 用)

与 handoff 的联动
-----------------
* 调用 ``create_handoff(receiver_account_key="")`` 时, handoff 模块不再
  强求上游传 receiver, 而是调 ``pick_receiver(channel, persona)`` 自动路由
* handoff 状态 pending → acknowledged → completed 过程中, 占的是 receiver
  的 daily_cap 配额; expired / rejected 不计
"""
from __future__ import annotations

import datetime as _dt
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from src.host._yaml_cache import YamlCache
from src.host.device_registry import config_file

logger = logging.getLogger(__name__)

_cfg_path = config_file("referral_receivers.yaml")


_FALLBACK: Dict[str, Any] = {
    "version": 1,
    "defaults": {
        "daily_cap": 15,
        "cap_reset_tz": "+00:00",
        "rotation_mode": "least_loaded",
    },
    "receivers": {},
}


def _post_process(raw: Any) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        return dict(_FALLBACK)
    data = dict(_FALLBACK)
    data.update({k: v for k, v in raw.items()
                  if k in ("version", "defaults", "receivers")})
    if not isinstance(data.get("defaults"), dict):
        data["defaults"] = dict(_FALLBACK["defaults"])
    if not isinstance(data.get("receivers"), dict):
        data["receivers"] = {}
    # 规范化每 receiver 的字段
    for key, r in (data["receivers"] or {}).items():
        if not isinstance(r, dict):
            continue
        r.setdefault("channel", "")
        r.setdefault("account_id", "")
        r.setdefault("display_name", key)
        r.setdefault("daily_cap", data["defaults"].get("daily_cap", 15))
        r.setdefault("backup_key", None)
        r.setdefault("enabled", True)
        r.setdefault("persona_filter", [])
        r.setdefault("tags", [])
        r.setdefault("webhook_url", "")
    return data


_CACHE = YamlCache(
    path=_cfg_path,
    defaults=_FALLBACK,
    post_process=_post_process,
    log_label="referral_receivers.yaml",
    logger=logger,
)


def load_receivers(force_reload: bool = False) -> Dict[str, Any]:
    """加载配置(mtime 热加载)。"""
    return _CACHE.get(force_reload=force_reload)


def reload_receivers() -> Dict[str, Any]:
    return _CACHE.reload()


def get_receiver(key: str) -> Optional[Dict[str, Any]]:
    """取单个 receiver; 不存在返回 None。"""
    if not key:
        return None
    data = load_receivers()
    r = (data.get("receivers") or {}).get(key)
    if not r:
        return None
    # 深拷贝一份 + 注入 key 字段, 方便调用方直接使用
    out = dict(r)
    out["key"] = key
    return out


def list_receivers(channel: Optional[str] = None,
                    enabled_only: bool = False,
                    persona_key: Optional[str] = None) -> List[Dict[str, Any]]:
    """按条件列 receiver 清单, 每条附 key 字段。"""
    data = load_receivers()
    out: List[Dict[str, Any]] = []
    for key, r in (data.get("receivers") or {}).items():
        if not isinstance(r, dict):
            continue
        if channel and r.get("channel") != channel:
            continue
        if enabled_only and not r.get("enabled", True):
            continue
        if persona_key:
            pf = r.get("persona_filter") or []
            if pf and persona_key not in pf:
                continue
        item = dict(r)
        item["key"] = key
        out.append(item)
    return out


def _today_start_iso(tz_offset: str = "+00:00") -> str:
    """返回日界限 (指定时区的 00:00 对应的 UTC ISO)。"""
    try:
        sign = 1 if tz_offset.startswith("+") else -1
        hh, mm = map(int, tz_offset.strip("+-").split(":"))
        offset = _dt.timedelta(hours=hh * sign, minutes=mm * sign)
    except Exception:
        offset = _dt.timedelta(0)
    now_utc = _dt.datetime.utcnow()
    now_local = now_utc + offset
    local_midnight = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    utc_midnight = local_midnight - offset
    return utc_midnight.strftime("%Y-%m-%dT%H:%M:%SZ")


def count_today_handoffs(receiver_key: str,
                          include_states: Tuple[str, ...] = (
                              "pending", "acknowledged", "completed"
                          )) -> int:
    """查今日分配到该 receiver 且在给定状态集合内的 handoff 数。

    默认不计 rejected / expired / duplicate_blocked (这些不消耗配额)。
    """
    if not receiver_key:
        return 0
    data = load_receivers()
    tz = data.get("defaults", {}).get("cap_reset_tz", "+00:00")
    cutoff = _today_start_iso(tz)
    try:
        from src.host.database import _connect
        placeholders = ",".join(["?"] * len(include_states))
        sql = (f"SELECT COUNT(*) FROM lead_handoffs"
               f" WHERE receiver_account_key=? AND created_at >= ?"
               f" AND state IN ({placeholders})")
        with _connect() as conn:
            row = conn.execute(sql, (receiver_key, cutoff,
                                       *include_states)).fetchone()
        return int(row[0]) if row else 0
    except Exception as e:
        logger.debug("[receivers] count_today 失败: %s", e)
        return 0


def receiver_load(key: str) -> Dict[str, Any]:
    """返回单接收方负载 {key, current, cap, percent_used, remaining, enabled}."""
    r = get_receiver(key)
    if not r:
        return {"key": key, "exists": False}
    cap = int(r.get("daily_cap") or 0)
    current = count_today_handoffs(key)
    remaining = max(0, cap - current)
    percent = int(current * 100 / cap) if cap > 0 else 0
    return {
        "key": key, "exists": True,
        "display_name": r.get("display_name") or key,
        "channel": r.get("channel"),
        "account_id_masked": _mask(r.get("account_id") or ""),
        "enabled": r.get("enabled", True),
        "current": current,
        "cap": cap,
        "remaining": remaining,
        "percent_used": percent,
        "at_cap": current >= cap,
        "backup_key": r.get("backup_key"),
    }


def _mask(account_id: str) -> str:
    """简易脱敏; 留首末 2-3 字符。"""
    v = (account_id or "").strip()
    if len(v) <= 4:
        return "*" * len(v) if v else ""
    return v[:2] + "*" * (len(v) - 4) + v[-2:]


def pick_receiver(channel: str, *,
                   persona_key: Optional[str] = None,
                   preferred_key: Optional[str] = None
                   ) -> Optional[Dict[str, Any]]:
    """按 channel + persona 选一个 receiver, 含 backup 链路。

    算法:
      1. 如果 preferred_key 指定 且 enabled 且 not at_cap → 用它
      2. 否则找所有匹配 (channel + persona + enabled) 的 receiver, 按
         剩余配额从多到少排序 (least_loaded); 取第一个 not at_cap 的
      3. 若全部 at_cap, 但首选的 backup_key 有空闲 → 跟链到 backup (最多 3 跳)
      4. 全 at_cap/无候选 → 返回 None, 调用方可以记日志 + 放弃 handoff

    Returns:
        receiver dict (含 key, load info) 或 None
    """
    channel = (channel or "").strip().lower()
    if not channel:
        return None

    # 1. preferred_key
    if preferred_key:
        load = receiver_load(preferred_key)
        if load.get("exists") and load.get("enabled") and not load.get("at_cap"):
            return load

    # 2. 匹配 channel + persona
    candidates = list_receivers(channel=channel, enabled_only=True,
                                  persona_key=persona_key)
    if not candidates:
        return None
    loads = [receiver_load(c["key"]) for c in candidates]
    # 按剩余 remaining 降序
    loads.sort(key=lambda x: -x.get("remaining", 0))
    for ld in loads:
        if not ld.get("at_cap"):
            return ld

    # 3. 追 backup 链 (最多 3 跳)
    for ld in loads:
        bk = ld.get("backup_key")
        hops = 0
        while bk and hops < 3:
            bk_load = receiver_load(bk)
            if bk_load.get("exists") and bk_load.get("enabled") \
               and not bk_load.get("at_cap"):
                return bk_load
            bk = bk_load.get("backup_key") if bk_load.get("exists") else None
            hops += 1

    # 4. 全满
    return None


def upsert_receiver(key: str, spec: Dict[str, Any]) -> Dict[str, Any]:
    """新增或更新一个 receiver (管理员 API 用)。

    直接写回 YAML 文件。返回规范化后的条目。
    """
    if not key or not isinstance(spec, dict):
        raise ValueError("key + spec 必填")
    data = load_receivers(force_reload=True)
    receivers = data.get("receivers") or {}
    # 合并: 已有就 merge, 没有就新建
    existing = dict(receivers.get(key) or {})
    existing.update({k: v for k, v in spec.items() if v is not None or k in existing})
    receivers[key] = existing
    data["receivers"] = receivers
    _save_receivers(data)
    # force reload
    reload_receivers()
    return get_receiver(key) or {}


def delete_receiver(key: str) -> bool:
    """删除一个 receiver。"""
    if not key:
        return False
    data = load_receivers(force_reload=True)
    receivers = data.get("receivers") or {}
    if key not in receivers:
        return False
    receivers.pop(key)
    data["receivers"] = receivers
    _save_receivers(data)
    reload_receivers()
    return True


def _save_receivers(data: Dict[str, Any]) -> None:
    """原子写回 YAML 文件 (用 temp file 防半写)。"""
    import yaml
    import tempfile
    import os

    path = Path(_cfg_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    tmp_fd, tmp_path = tempfile.mkstemp(prefix="receivers_", suffix=".yaml.tmp",
                                          dir=str(path.parent))
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)
        # Windows 上 os.replace 是原子的
        os.replace(tmp_path, str(path))
    except Exception:
        if os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
        raise


def all_loads() -> List[Dict[str, Any]]:
    """Dashboard 看板用: 返回所有 receiver 的负载信息。"""
    data = load_receivers()
    return [receiver_load(key) for key in (data.get("receivers") or {})]
