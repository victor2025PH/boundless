# -*- coding: utf-8 -*-
"""真发总闸（``inbox.l2_autosend.enabled/deliver``）的翻动留痕与应急态语义（#142，归 #63 族）。

背景（#140/#142，钧 0902 实锤）：总闸被关后，「会话档=全自动」的会话静默不发
（AI 拟稿在写、不外发），界面只有顶栏一个小标签——用户体感「自动回复坏了」，
且事后没人说得清「谁、什么时候、从哪里把总闸关的」。本模块把三件事收成单一
事实源：

1. **翻动留痕**：每次总闸开/关落 ``autosend_gate_state.json``（config 目录），
   字段风格与 ``conversation_settings.source`` 一致（谁 actor / 从哪 source /
   何时 ts）。写入点=能力路由 ``_audit_toggle``（预设/值守三档/拆开控制/回滚/
   横幅一键恢复全部经它）。
2. **暂停元信息**：:func:`pause_meta` 给横幅/设置页出「谁关的+何时+从哪关的
   +几点自动恢复」，判定单点仍是 ``automation_mode.deliver_paused_reason``
   ——本模块只补人话，不另起一套开/关判定。
3. **可选自动过期**（默认关，向后兼容）：``inbox.l2_autosend.
   pause_auto_resume_hours`` > 0 时，:func:`sweep_gate_auto_resume`（watchdog
   每 tick 接线）把关满 N 小时的总闸自动恢复 + 发 ``autosend_gate_alert``
   事件（WebhookNotifier 外发群通知）。**只恢复有留痕记录的翻动**——yaml
   手改/部署形态本就关闭的闸没有留痕，绝不替人做主打开。

刻意不做的：不动总闸本身的语义（是否砍掉属产品决策）；不碰各会话档位与
按账号接管方式（#63 已修部分）；全部读写 best-effort，绝不成为回复链故障点。
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# 总闸的两个 config 路径（与 capability 注册表 flag_path 一致）。
# enabled=worker 存在与否，deliver=AI 可否自动真发；任一关闭即「全局已暂停真发」
# （deliver_paused_reason 同口径）。
GATE_PATHS = ("inbox.l2_autosend.enabled", "inbox.l2_autosend.deliver")

_STATE_FILENAME = "autosend_gate_state.json"
_HISTORY_CAP = 100


def config_dir_from_manager(config_manager: Any) -> Optional[Path]:
    """config_manager → 实例 config 目录（留痕文件所在）。拿不到返回 None。"""
    try:
        base = getattr(config_manager, "config_path", None)
        if not base:
            return None
        return Path(base).parent
    except Exception:
        return None


def gate_state_path(config_dir: Path) -> Path:
    return Path(config_dir) / _STATE_FILENAME


def _load_state(config_dir: Path) -> Dict[str, Any]:
    """读状态文件；缺失/损坏一律回空结构（留痕是辅助事实，绝不抛）。"""
    try:
        p = gate_state_path(config_dir)
        if not p.is_file():
            return {"paths": {}, "history": []}
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"paths": {}, "history": []}
        paths = data.get("paths")
        history = data.get("history")
        return {
            "paths": paths if isinstance(paths, dict) else {},
            "history": history if isinstance(history, list) else [],
        }
    except Exception:
        logger.debug("[autosend_gate] 状态文件读取失败（按空处理）", exc_info=True)
        return {"paths": {}, "history": []}


def record_gate_flip(
    config_dir: Path,
    *,
    path: str,
    value: bool,
    actor: str = "",
    source: str = "",
    ts: Optional[float] = None,
) -> None:
    """落一条总闸翻动记录（谁/从哪/何时/翻成什么）。best-effort，绝不抛。

    ``source``＝翻动入口，与 ``conversation_settings.source`` 同风格
    （standby:off / standby-split:deliver / preset:* / rollback /
    inbox_banner:resume / auto_expire …）。
    """
    if path not in GATE_PATHS:
        return
    try:
        state = _load_state(config_dir)
        rec = {
            "path": str(path),
            "value": bool(value),
            "actor": str(actor or "")[:80],
            "source": str(source or "")[:80],
            "ts": round(float(ts if ts is not None else time.time()), 3),
        }
        state["paths"][str(path)] = rec
        state["history"].append(rec)
        state["history"] = state["history"][-_HISTORY_CAP:]
        gate_state_path(config_dir).write_text(
            json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        logger.debug("[autosend_gate] 翻动留痕写入失败（忽略）", exc_info=True)


def gate_flip_snapshot(config_dir: Path, *, limit: int = 10) -> Dict[str, Any]:
    """读侧快照：``{"paths": {path: 最近记录}, "history": [新→旧]}``（设置页用）。"""
    state = _load_state(config_dir)
    hist = list(reversed(state["history"]))[: max(1, min(int(limit or 10), _HISTORY_CAP))]
    return {"paths": dict(state["paths"]), "history": hist}


def auto_resume_hours(config: Optional[Dict[str, Any]]) -> float:
    """``inbox.l2_autosend.pause_auto_resume_hours``（小时；0=关，默认关）。"""
    try:
        raw = (((config or {}).get("inbox") or {}).get("l2_autosend") or {}).get(
            "pause_auto_resume_hours")
        v = float(raw or 0.0)
        return v if v > 0 else 0.0
    except Exception:
        return 0.0


def _blocking_paths(config: Optional[Dict[str, Any]]) -> List[str]:
    """当前把真发挡住的 config 路径（与 deliver_paused_reason 同判定顺序）。"""
    try:
        l2 = (((config or {}).get("inbox") or {}).get("l2_autosend"))
        if not isinstance(l2, dict) or not l2:
            return []
        out: List[str] = []
        if not bool(l2.get("enabled")):
            out.append("inbox.l2_autosend.enabled")
        if not bool(l2.get("deliver")):
            out.append("inbox.l2_autosend.deliver")
        return out
    except Exception:
        return []


def pause_meta(
    config: Optional[Dict[str, Any]],
    config_dir: Optional[Path],
    *,
    now: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    """「全局已暂停真发」的人话元信息；未暂停返回 None。

    返回（JSON 安全）::

        {
          "reason": "l2_autosend.deliver=false",   # 机器可读原因（判定单点回显）
          "since": 1788306906.4,   # 最近一次关闸翻动时刻；0=无留痕（yaml 手改等）
          "actor": "钧",           # 谁关的（留痕缺席=空串）
          "source": "standby:off", # 从哪关的（同上）
          "auto_resume_at": 0.0,   # >0=预计自动恢复时刻（配置开且有留痕才有）
          "auto_resume_hours": 0.0,
        }
    """
    from src.inbox.automation_mode import deliver_paused_reason

    reason = deliver_paused_reason(config)
    if not reason:
        return None
    out: Dict[str, Any] = {
        "reason": reason, "since": 0.0, "actor": "", "source": "",
        "auto_resume_at": 0.0, "auto_resume_hours": auto_resume_hours(config),
    }
    if config_dir is None:
        return out
    try:
        paths_snap = _load_state(config_dir)["paths"]
        # 责任记录＝当前实际挡闸的路径里、留痕为「关」的最近一条（再翻一次即重置计时）
        recs = [
            r for p in _blocking_paths(config)
            for r in [paths_snap.get(p)]
            if isinstance(r, dict) and r.get("value") is False
        ]
        if recs:
            latest = max(recs, key=lambda r: float(r.get("ts") or 0.0))
            out["since"] = float(latest.get("ts") or 0.0)
            out["actor"] = str(latest.get("actor") or "")
            out["source"] = str(latest.get("source") or "")
            hours = out["auto_resume_hours"]
            if hours > 0 and out["since"] > 0:
                out["auto_resume_at"] = out["since"] + hours * 3600.0
    except Exception:
        logger.debug("[autosend_gate] pause_meta 读取失败（回落无留痕）", exc_info=True)
    return out


def sweep_gate_auto_resume(
    config_manager: Any,
    *,
    rewire: Any = None,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """可选自动过期（默认关）：总闸关满 N 小时 → 自动恢复 + 群外通知。

    安全边界：
    - 配置 ``pause_auto_resume_hours`` ≤0 → 空转（向后兼容，零行为变更）；
    - 挡闸路径**没有留痕**（yaml 手改/部署形态本就关）→ 不动——自动恢复只
      撤销「被顺手带到」的翻动，绝不替部署决策做主；
    - 恢复动作＝把留痕为关的挡闸路径写回 true（overlay 直写）+ 热接线 worker
      + 落留痕（actor=system / source=auto_expire）+ 发 ``autosend_gate_alert``。
    """
    out: Dict[str, Any] = {"resumed": False, "paths": []}
    try:
        config = getattr(config_manager, "config", None)
        if not isinstance(config, dict) or not hasattr(config_manager, "set_overlay_flag"):
            return out
        hours = auto_resume_hours(config)
        if hours <= 0:
            return out
        config_dir = config_dir_from_manager(config_manager)
        if config_dir is None:
            return out
        meta = pause_meta(config, config_dir, now=now)
        if not meta or float(meta.get("since") or 0.0) <= 0:
            return out
        ts = float(now if now is not None else time.time())
        if ts < float(meta["since"]) + hours * 3600.0:
            return out
        paths_snap = _load_state(config_dir)["paths"]
        blocking = _blocking_paths(config)
        # 全部挡闸路径都必须有「关」留痕才动手（部分留痕=状态混杂，宁可不动）
        for p in blocking:
            rec = paths_snap.get(p)
            if not (isinstance(rec, dict) and rec.get("value") is False):
                return out
        flipped: List[str] = []
        for p in blocking:
            try:
                ok, _msg = config_manager.set_overlay_flag(p, True)
            except Exception:
                ok = False
            if ok:
                record_gate_flip(config_dir, path=p, value=True,
                                 actor="system", source="auto_expire", ts=ts)
                flipped.append(p)
        if not flipped:
            return out
        out["resumed"] = True
        out["paths"] = flipped
        out["paused_hours"] = round((ts - float(meta["since"])) / 3600.0, 1)
        if callable(rewire):
            try:
                out["rewire"] = dict(rewire() or {})
            except Exception:
                logger.debug("[autosend_gate] 自动恢复热接线失败（重启后生效）",
                             exc_info=True)
        try:
            from src.integrations.shared.event_bus import get_event_bus
            get_event_bus().publish("autosend_gate_alert", {
                "action": "auto_resumed",
                "paths": flipped,
                "paused_hours": out.get("paused_hours"),
                "paused_by": str(meta.get("actor") or ""),
                "paused_source": str(meta.get("source") or ""),
                "rate_key": "autosend_gate:auto_resume",
            })
        except Exception:
            logger.debug("[autosend_gate] 自动恢复事件发布失败（忽略）", exc_info=True)
        logger.warning(
            "[autosend_gate] 真发总闸关满 %.1fh（由 %s 经 %s 关闭），已按配置自动恢复：%s",
            out["paused_hours"], meta.get("actor") or "?",
            meta.get("source") or "?", ", ".join(flipped))
    except Exception:
        logger.debug("[autosend_gate] 自动过期 sweep 异常（忽略）", exc_info=True)
    return out


__all__ = [
    "GATE_PATHS",
    "config_dir_from_manager",
    "gate_state_path",
    "record_gate_flip",
    "gate_flip_snapshot",
    "auto_resume_hours",
    "pause_meta",
    "sweep_gate_auto_resume",
]
