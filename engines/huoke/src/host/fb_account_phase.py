# -*- coding: utf-8 -*-
"""Facebook 账号阶段状态机（2026-04-21 P1-2）。

**问题**: 所有账号用同一套节奏 —— 新号 ≠ 老号 ≠ 被警告过的号。
**解法**: 每台设备（= 一个 FB 账号）挂一个 phase 标签，browse_feed 按 phase
读 playbook，Gate 也能按 phase 决定要不要降级/暂停。

状态：
  cold_start  — 新号/刚重启养号：慢节奏、基本不点赞
  growth      — 3~7 天内稳定账号：接近真人节奏
  mature      — 累计 ≥ 7 天 + ≥ 2000 屏：可稍激进
  cooldown    — 24h 内 ≥ 3 次风控：自动冷却 48h

迁移是 **事件驱动** 的：
  * ``on_scrolls(device_id, n)`` — browse_feed 成功后累加
  * ``on_risk(device_id)``       — _report_risk 触发
  * ``evaluate_transition(device_id)`` — 每次更新后调一次，决定是否换档

查询 API:
  ``get_phase(device_id) -> dict``  供 Gate / browse_feed 读取
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from .database import _connect

logger = logging.getLogger(__name__)


VALID_PHASES = ("cold_start", "growth", "mature", "cooldown")
_DEFAULT_PHASE = "cold_start"


# ── CRUD ──────────────────────────────────────────────────────────────

def _ensure_row(conn, device_id: str) -> Dict[str, Any]:
    conn.row_factory = __import__("sqlite3").Row
    row = conn.execute(
        "SELECT * FROM fb_account_phase WHERE device_id=?", (device_id,)
    ).fetchone()
    if row:
        return dict(row)
    conn.execute(
        "INSERT INTO fb_account_phase (device_id, phase) VALUES (?, ?)",
        (device_id, _DEFAULT_PHASE),
    )
    row = conn.execute(
        "SELECT * FROM fb_account_phase WHERE device_id=?", (device_id,)
    ).fetchone()
    return dict(row) if row else {}


def get_phase(device_id: str) -> Dict[str, Any]:
    """取当前 phase 记录。不存在则自动建一条 cold_start 并返回。"""
    if not device_id:
        return {"phase": _DEFAULT_PHASE, "device_id": "", "_virtual": True}
    try:
        with _connect() as conn:
            r = _ensure_row(conn, device_id)
            return r
    except Exception as e:
        logger.warning("[fb_phase] get_phase 失败 device=%s: %s", device_id[:12], e)
        return {"phase": _DEFAULT_PHASE, "device_id": device_id, "_error": str(e)}


def list_phases(phase: Optional[str] = None) -> List[Dict[str, Any]]:
    """面板用：列所有设备当前 phase。"""
    sql = "SELECT * FROM fb_account_phase"
    params: list = []
    if phase:
        sql += " WHERE phase=?"
        params.append(phase)
    sql += " ORDER BY updated_at DESC"
    try:
        with _connect() as conn:
            conn.row_factory = __import__("sqlite3").Row
            return [dict(r) for r in conn.execute(sql, params).fetchall()]
    except Exception:
        return []


# ── 事件钩子 ──────────────────────────────────────────────────────────

def on_scrolls(device_id: str, scrolls: int, likes: int = 0) -> Dict[str, Any]:
    """browse_feed 结束后调用：累加计数并触发迁移评估。"""
    if not device_id or scrolls <= 0:
        return {}
    try:
        with _connect() as conn:
            _ensure_row(conn, device_id)
            conn.execute(
                "UPDATE fb_account_phase SET "
                "total_scrolls = total_scrolls + ?, "
                "total_likes   = total_likes   + ?, "
                "last_task_at  = datetime('now'), "
                "updated_at    = datetime('now') "
                "WHERE device_id=?",
                (int(scrolls), int(likes), device_id),
            )
        return evaluate_transition(device_id)
    except Exception as e:
        logger.warning("[fb_phase] on_scrolls 失败: %s", e)
        return {}


def on_risk(device_id: str) -> Dict[str, Any]:
    """_report_risk 里调：累加 + 立即触发 cooldown 评估。"""
    if not device_id:
        return {}
    try:
        with _connect() as conn:
            _ensure_row(conn, device_id)
            conn.execute(
                "UPDATE fb_account_phase SET "
                "total_risk_events = total_risk_events + 1, "
                "last_risk_at      = datetime('now'), "
                "updated_at        = datetime('now') "
                "WHERE device_id=?",
                (device_id,),
            )
        return evaluate_transition(device_id)
    except Exception as e:
        logger.warning("[fb_phase] on_risk 失败: %s", e)
        return {}


# ── 迁移核心 ──────────────────────────────────────────────────────────

def _set_phase(conn, device_id: str, new_phase: str, reason: str = ""):
    conn.execute(
        "UPDATE fb_account_phase SET phase=?, since_at=datetime('now'), "
        "updated_at=datetime('now') WHERE device_id=?",
        (new_phase, device_id),
    )
    logger.info("[fb_phase] 设备 %s 迁移: %s  (%s)",
                device_id[:12], new_phase, reason)


def evaluate_transition(device_id: str) -> Dict[str, Any]:
    """按 playbook.phase_transitions 评估一次，必要时写入新 phase。

    规则优先级（符合真实风险模型）：
      1. to_cooldown        — 24h 风控 ≥ 阈值 → cooldown（最优先）
      2. cooldown_to_cold_start — 清静期达标 → cold_start
      3. cold_start_to_growth   — 屏数 + 账号年龄达标
      4. growth_to_mature       — 屏数 + 天数达标
    """
    if not device_id:
        return {}
    from src.host.fb_store import count_risk_events_recent
    try:
        from src.host.fb_playbook import resolve_transitions
        rules = resolve_transitions() or {}
    except Exception:
        rules = {}

    with _connect() as conn:
        row = _ensure_row(conn, device_id)
        cur_phase = row.get("phase") or _DEFAULT_PHASE
        total_scrolls = int(row.get("total_scrolls") or 0)

        # 以 SQLite 的 julianday 计算账号"首次见到"距今的小时/天数，
        # 避开 Python 时区细节。
        ages = conn.execute(
            "SELECT "
            "(julianday('now') - julianday(first_seen_at)) * 24.0 AS age_hours, "
            "(julianday('now') - julianday(COALESCE(last_risk_at, first_seen_at))) * 24.0 AS clean_hours "
            "FROM fb_account_phase WHERE device_id=?",
            (device_id,),
        ).fetchone()
        age_hours = float(ages[0] or 0.0) if ages else 0.0
        clean_hours = float(ages[1] or 0.0) if ages else 0.0
        age_days = age_hours / 24.0

        risk_24h = count_risk_events_recent(device_id, hours=24)

        # 规则 1: 任何 phase → cooldown
        to_cd = rules.get("to_cooldown") or {}
        min_risk = int(to_cd.get("min_risk_count_24h") or 3)
        if cur_phase != "cooldown" and risk_24h >= min_risk:
            _set_phase(conn, device_id, "cooldown",
                       f"risk_24h={risk_24h} >= {min_risk}")
            conn.commit()
            return {"changed": True, "from": cur_phase, "to": "cooldown",
                    "reason": "risk_exceeded", "risk_24h": risk_24h}

        # 规则 2: cooldown → cold_start
        if cur_phase == "cooldown":
            cd_clear = rules.get("cooldown_to_cold_start") or {}
            min_clean = float(cd_clear.get("min_clean_hours") or 48)
            if risk_24h == 0 and clean_hours >= min_clean:
                _set_phase(conn, device_id, "cold_start",
                           f"clean_hours={clean_hours:.1f} >= {min_clean}")
                conn.commit()
                return {"changed": True, "from": "cooldown", "to": "cold_start",
                        "reason": "cooldown_cleared"}
            return {"changed": False, "phase": "cooldown",
                    "clean_hours": round(clean_hours, 1), "risk_24h": risk_24h}

        # 规则 3: cold_start → growth
        if cur_phase == "cold_start":
            r3 = rules.get("cold_start_to_growth") or {}
            min_s = int(r3.get("min_total_scrolls") or 200)
            min_ah = float(r3.get("min_age_hours") or 24)
            if total_scrolls >= min_s and age_hours >= min_ah and risk_24h == 0:
                _set_phase(conn, device_id, "growth",
                           f"scrolls={total_scrolls}/{min_s} age_h={age_hours:.1f}/{min_ah}")
                conn.commit()
                return {"changed": True, "from": "cold_start", "to": "growth",
                        "reason": "warmup_done"}

        # 规则 4: growth → mature
        if cur_phase == "growth":
            r4 = rules.get("growth_to_mature") or {}
            min_s = int(r4.get("min_total_scrolls") or 2000)
            min_d = float(r4.get("min_age_days") or 7)
            if total_scrolls >= min_s and age_days >= min_d and risk_24h == 0:
                _set_phase(conn, device_id, "mature",
                           f"scrolls={total_scrolls}/{min_s} age_d={age_days:.1f}/{min_d}")
                conn.commit()
                return {"changed": True, "from": "growth", "to": "mature",
                        "reason": "tenure_reached"}

        conn.commit()
        return {"changed": False, "phase": cur_phase,
                "total_scrolls": total_scrolls,
                "age_hours": round(age_hours, 1),
                "risk_24h": risk_24h}
