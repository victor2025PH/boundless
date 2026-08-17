# -*- coding: utf-8 -*-
"""坐席「接管即静音」的可见化与自动接回（P0 2026-08-09，.198/.104 事故沉淀）。

背景：Sprint1 起坐席手动出站即把会话档位写成 ``manual``（防 AI 与人抢话），
但这个写入此前 **source 留空**、UI 无任何提示、也没有任何恢复机制——.198/.104
实测两个会话被钉死在 manual **27 小时**，坐席全程不知道是自己那条手动消息
关掉了 AI（现场体感=「全自动坏了」）。本模块补三件事：

1. **打标**：接管写入统一走 :func:`record_agent_takeover`，source 编码为
   ``takeover``（接管前无显式档位）或 ``takeover_from:<prev>``（保留接管前
   档位，供恢复用）。重复接管只刷新 ``updated_at``（= 静默计时器重置），
   **绝不覆盖首次接管保存的 from 档位**。
2. **自动接回**：:func:`sweep_takeover_rearm`（watchdog 每 tick 调）把
   「source=takeover*、mode=manual、距最后一次接管超过 N 分钟」的会话恢复到
   接管前档位（无记录则回全局默认）。**只碰 takeover 来源**——坐席在下拉框
   显式选的 manual（source=human）、守卫降档（guard:*/sweep）一律不动。
3. **纯函数可解释**：:func:`rearm_state` 给 GET /automation 出「几分钟后
   自动接回」的倒计时，横幅说的与 sweep 做的同一套参数。

配置 ``inbox.takeover_rearm``（新子系统默认 **关**，遵守仓库约定；桌面
交付种子按「全自动」承诺显式开）::

    inbox:
      takeover_rearm:
        enabled: true        # 开=坐席静默 after_minutes 后 AI 自动接回
        after_minutes: 30    # 自最后一次人工出站起算（每次出站重置计时）

失败方向：任何一步异常都不得影响发送主链（调用方 best-effort 包裹）；
sweep 单行失败跳过该行继续（一个坏行不能卡死整批恢复）。
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

from src.inbox.store import AUTOMATION_MODES

logger = logging.getLogger(__name__)

# source 词汇（与 conversation_settings.source 的既有词汇并列：
# human/bootstrap/guard:*/sweep/bulk）。
TAKEOVER_SOURCE = "takeover"
_TAKEOVER_FROM_PREFIX = "takeover_from:"
REARM_SOURCE = "rearm"


def is_takeover_source(source: Any) -> bool:
    """该 source 是否为「坐席出站接管」写入（自动接回只认它）。"""
    s = str(source or "")
    return s == TAKEOVER_SOURCE or s.startswith(_TAKEOVER_FROM_PREFIX)


def takeover_prev_mode(source: Any) -> str:
    """从 takeover source 解出接管前档位；无记录/不合法 → 空串。"""
    s = str(source or "")
    if s.startswith(_TAKEOVER_FROM_PREFIX):
        prev = s[len(_TAKEOVER_FROM_PREFIX):].strip().lower()
        if prev in AUTOMATION_MODES:
            return prev
    return ""


def record_agent_takeover(store: Any, conversation_id: str) -> str:
    """坐席手动出站 → 会话切 manual 并打 takeover 标。返回写入的 source。

    - 首次接管：保存接管前档位（``takeover_from:auto_ai``）；接管前无显式
      档位则写 ``takeover``（恢复时回全局默认）。
    - 已处于接管态：原样重写（source 不变 → 保住最初的 from 档位；
      ``updated_at`` 刷新 = 自动接回计时器重置）。
    - 旧 store / 测试假件无 source 形参：退回旧签名（行为=修复前，诚实降级）。
    """
    cid = str(conversation_id or "").strip()
    if not cid or store is None:
        return ""
    source = TAKEOVER_SOURCE
    try:
        meta = None
        if hasattr(store, "get_automation_mode_meta"):
            try:
                meta = store.get_automation_mode_meta(cid)
            except Exception:
                meta = None
        if meta and is_takeover_source(meta.get("source")):
            # 连续接管：保住首次记录的 from 档位，只刷新时间戳。
            source = str(meta.get("source"))
        else:
            prev = str((meta or {}).get("mode") or "").strip().lower()
            if not prev and hasattr(store, "get_automation_mode_if_set"):
                try:
                    prev = str(store.get_automation_mode_if_set(cid) or "")
                except Exception:
                    prev = ""
            if prev in AUTOMATION_MODES and prev != "manual":
                source = f"{_TAKEOVER_FROM_PREFIX}{prev}"
    except Exception:
        source = TAKEOVER_SOURCE
    try:
        store.set_automation_mode(cid, "manual", source=source)
    except TypeError:
        store.set_automation_mode(cid, "manual")
        return ""
    try:
        from src.inbox.automation_mode_stats import record_takeover
        record_takeover(conversation_id=cid)
    except Exception:
        pass
    return source


def takeover_rearm_cfg(config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """解析 ``inbox.takeover_rearm``。返回 {enabled, after_minutes}。"""
    raw = (((config or {}).get("inbox") or {}).get("takeover_rearm") or {})
    if not isinstance(raw, dict):
        raw = {}
    try:
        after_min = float(raw.get("after_minutes", 30) or 30)
    except Exception:
        after_min = 30.0
    return {
        "enabled": bool(raw.get("enabled", False)),
        "after_minutes": max(1.0, after_min),
    }


def rearm_restore_mode(source: Any, config: Optional[Dict[str, Any]]) -> str:
    """接回时恢复到什么档位：接管前档位 > 全局默认。``manual``＝无事可做返空串。"""
    prev = takeover_prev_mode(source)
    if not prev:
        from src.inbox.automation_mode import global_automation_mode_from_config
        prev = global_automation_mode_from_config(config)
    if prev not in AUTOMATION_MODES or prev == "manual":
        return ""
    return prev


def rearm_state(
    meta: Optional[Dict[str, Any]],
    config: Optional[Dict[str, Any]],
    *,
    now: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    """GET /automation 的接回倒计时（横幅说的 = sweep 做的，同参同判定）。

    仅当「当前处于接管态 manual」才返回状态 dict；其余 None（前端不渲染）。
    """
    if not meta or str(meta.get("mode") or "") != "manual":
        return None
    if not is_takeover_source(meta.get("source")):
        return None
    cfg = takeover_rearm_cfg(config)
    ts = float(meta.get("updated_at") or 0.0)
    restore = rearm_restore_mode(meta.get("source"), config)
    out: Dict[str, Any] = {
        "enabled": bool(cfg["enabled"]) and bool(restore),
        "after_minutes": cfg["after_minutes"],
        "taken_at": ts,
        "restore_mode": restore,
    }
    if out["enabled"] and ts > 0:
        out["eta_ts"] = round(ts + cfg["after_minutes"] * 60.0, 1)
        out["eta_in_sec"] = max(
            0.0, round(out["eta_ts"] - float(now or time.time()), 1))
    return out


def sweep_takeover_rearm(
    store: Any,
    config: Optional[Dict[str, Any]],
    *,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """把静默超时的接管会话恢复档位（watchdog 每 tick 调，幂等）。

    返回 ``{enabled, scanned, restored, restored_cids}``。任何一行失败跳过
    继续；store 缺查询口（旧版本）→ scanned=0 静默（观测口径如实为零）。
    """
    cfg = takeover_rearm_cfg(config)
    out: Dict[str, Any] = {
        "enabled": cfg["enabled"], "scanned": 0, "restored": 0,
        "restored_cids": [],
    }
    if not cfg["enabled"] or store is None:
        return out
    if not hasattr(store, "list_takeover_manual"):
        return out
    ts_now = float(now or time.time())
    cutoff = ts_now - cfg["after_minutes"] * 60.0
    try:
        rows = store.list_takeover_manual(before_ts=cutoff) or []
    except Exception:
        logger.debug("[takeover_rearm] 扫描失败（忽略）", exc_info=True)
        return out
    out["scanned"] = len(rows)
    for row in rows:
        cid = str((row or {}).get("conversation_id") or "")
        if not cid:
            continue
        target = rearm_restore_mode((row or {}).get("source"), config)
        if not target:
            continue
        try:
            store.set_automation_mode(cid, target, source=REARM_SOURCE)
        except TypeError:
            store.set_automation_mode(cid, target)
        except Exception:
            logger.debug("[takeover_rearm] 恢复失败 cid=%s（跳过）", cid,
                         exc_info=True)
            continue
        out["restored"] += 1
        out["restored_cids"].append(cid)
        logger.info(
            "[takeover_rearm] 坐席静默超 %.0f 分钟，AI 自动接回 cid=%s → %s",
            cfg["after_minutes"], cid, target)
    if out["restored"]:
        try:
            from src.inbox.automation_mode_stats import record_rearm
            record_rearm(count=int(out["restored"]))
        except Exception:
            pass
    return out


__all__ = [
    "TAKEOVER_SOURCE",
    "REARM_SOURCE",
    "is_takeover_source",
    "takeover_prev_mode",
    "record_agent_takeover",
    "takeover_rearm_cfg",
    "rearm_restore_mode",
    "rearm_state",
    "sweep_takeover_rearm",
]
