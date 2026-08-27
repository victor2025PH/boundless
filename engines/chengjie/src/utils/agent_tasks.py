"""坐席新手任务流·后端半件（WP-7 2026-08-18）。

「新坐席第一天敢点通过」的首日 5 任务清单：看一条会话 → 通过一条草稿 →
编辑后发送一条 → 发一条语音 → 建一个跟进目标。本模块＝**每坐席**进度的
服务端镜像（换机不丢；前端 localStorage 只是加速缓存）+ 老坐席豁免判据。

分工契约（与前端卡片解耦，卡片属 sidebar 线的施工区）：
- **完成上报走客户端**（卡片在 5 个动作的既有 handler 里调
  ``POST /api/agent-tasks/complete``）——新手清单不是计费口径，客户端上报
  即可；刻意**不**在 drafts/send-voice/goal 5 个服务端点埋 hook（那几个路由
  文件是多条线的活跃热区，为一张引导卡加 5 个跨文件 hook 不成比例）。
- 服务端负责：持久镜像（``agent_tasks.json``，键=username）、幂等完成、
  ``dismiss`` 逃生门、**老坐席豁免**（账号龄 > ``VETERAN_ACCOUNT_AGE_DAYS``
  在首次读取时钉死 veteran=True，永不弹卡——「有历史操作的老坐席不弹」的
  可判定近似：老账号必然老坐席；判定失败按新人处理=宁多显示一次，有 dismiss 兜底）。

落点与纪律与 ``onboarding_state`` 同族：实例数据根 config 目录、原子写、
读写绝不抛（引导路径零阻塞）。flag ``onboarding.agent_tasks`` 基线关。
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

STATE_FILENAME = "agent_tasks.json"

#: 首日 5 任务 id（前端卡片/埋点前缀 agt_ 与此一致，勿单独改一边）
TASK_IDS = ("view_conversation", "approve_draft", "edit_send",
            "send_voice", "create_goal")

#: 账号创建超过该天数＝老坐席（首读钉死，永不弹卡）
VETERAN_ACCOUNT_AGE_DAYS = 7

#: 状态文件里最多保留的坐席条目（防离职账号累积撑爆；LRU 按 updated_at 驱逐）
_MAX_USERS = 200


def agent_tasks_enabled(config: Optional[Dict[str, Any]]) -> bool:
    """``onboarding.agent_tasks``（基线 false）。"""
    try:
        return bool(((config or {}).get("onboarding") or {}).get("agent_tasks"))
    except Exception:
        return False


def _state_path() -> Path:
    from src.licensing.data_paths import config_dir

    return config_dir() / STATE_FILENAME


def _load_all() -> Dict[str, Any]:
    try:
        p = _state_path()
        if not p.exists():
            return {}
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        logger.debug("[agent_tasks] 状态读取失败（按空处理）", exc_info=True)
        return {}


def _save_all(data: Dict[str, Any]) -> bool:
    try:
        if len(data) > _MAX_USERS:
            keep = sorted(data.items(),
                          key=lambda kv: (kv[1] or {}).get("updated_at") or 0,
                          reverse=True)[:_MAX_USERS]
            data = dict(keep)
        p = _state_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, p)
        return True
    except Exception:
        logger.warning("[agent_tasks] 状态落盘失败", exc_info=True)
        return False


def parse_created_at(raw: Any) -> float:
    """web_users.created_at（TEXT，历史上多种格式）→ epoch；解析不了 → 0。"""
    s = str(raw or "").strip()
    if not s:
        return 0.0
    try:
        if re.fullmatch(r"\d{10,13}(\.\d+)?", s):
            v = float(s)
            return v / 1000.0 if v > 1e12 else v
        from datetime import datetime
        norm = s.replace("T", " ").split("+")[0].split(".")[0].strip()
        return datetime.strptime(norm, "%Y-%m-%d %H:%M:%S").timestamp()
    except Exception:
        try:
            from datetime import datetime
            return datetime.strptime(s[:10], "%Y-%m-%d").timestamp()
        except Exception:
            return 0.0


def is_veteran_account(created_at_raw: Any, *, now: Optional[float] = None,
                       days: int = VETERAN_ACCOUNT_AGE_DAYS) -> bool:
    """账号龄 > days ＝ 老坐席。解析失败 → False（按新人显示，dismiss 兜底）。"""
    ts = parse_created_at(created_at_raw)
    if ts <= 0:
        return False
    return (float(now if now is not None else time.time()) - ts) > days * 86400.0


def _default_entry() -> Dict[str, Any]:
    return {"tasks": {}, "dismissed": False, "veteran": False,
            "first_seen": 0, "updated_at": 0}


def user_status(username: str, *, account_created_at: Any = "",
                now: Optional[float] = None) -> Dict[str, Any]:
    """当前坐席状态（首读钉 veteran + first_seen；绝不抛）。

    返回 ``{tasks: [{id, done_ts}], done_count, all_done, dismissed, veteran,
    show}``；``show`` 是前端唯一消费判据＝非老坐席、未 dismiss、未全✓。
    """
    uname = str(username or "").strip() or "?"
    ts_now = float(now if now is not None else time.time())
    data = _load_all()
    entry = data.get(uname)
    if not isinstance(entry, dict):
        entry = _default_entry()
    changed = False
    if not entry.get("first_seen"):
        entry["first_seen"] = int(ts_now)
        entry["veteran"] = is_veteran_account(account_created_at, now=ts_now)
        changed = True
    tasks = entry.get("tasks") if isinstance(entry.get("tasks"), dict) else {}
    out_tasks = [{"id": t, "done_ts": int(tasks.get(t) or 0)} for t in TASK_IDS]
    done = sum(1 for t in out_tasks if t["done_ts"])
    all_done = done >= len(TASK_IDS)
    if changed:
        entry["updated_at"] = int(ts_now)
        data[uname] = entry
        _save_all(data)
    return {
        "username": uname,
        "tasks": out_tasks,
        "done_count": done,
        "all_done": all_done,
        "dismissed": bool(entry.get("dismissed")),
        "veteran": bool(entry.get("veteran")),
        "show": not (bool(entry.get("veteran")) or bool(entry.get("dismissed"))
                     or all_done),
    }


def complete_task(username: str, task_id: str,
                  *, now: Optional[float] = None) -> Dict[str, Any]:
    """标记一项完成（幂等：已完成不改首次时间戳）。未知任务抛 ValueError。"""
    if task_id not in TASK_IDS:
        raise ValueError(f"unknown agent task: {task_id!r}")
    uname = str(username or "").strip() or "?"
    ts_now = int(now if now is not None else time.time())
    data = _load_all()
    entry = data.get(uname)
    if not isinstance(entry, dict):
        entry = _default_entry()
        entry["first_seen"] = ts_now
    tasks = entry.get("tasks") if isinstance(entry.get("tasks"), dict) else {}
    if not tasks.get(task_id):
        tasks[task_id] = ts_now
    entry["tasks"] = tasks
    entry["updated_at"] = ts_now
    data[uname] = entry
    _save_all(data)
    return user_status(uname, now=ts_now)


def dismiss(username: str, *, now: Optional[float] = None) -> Dict[str, Any]:
    """坐席手动收起（逃生门；老坐席误判/不想看都走这）。"""
    uname = str(username or "").strip() or "?"
    ts_now = int(now if now is not None else time.time())
    data = _load_all()
    entry = data.get(uname)
    if not isinstance(entry, dict):
        entry = _default_entry()
        entry["first_seen"] = ts_now
    entry["dismissed"] = True
    entry["updated_at"] = ts_now
    data[uname] = entry
    _save_all(data)
    return user_status(uname, now=ts_now)


__all__ = [
    "STATE_FILENAME",
    "TASK_IDS",
    "VETERAN_ACCOUNT_AGE_DAYS",
    "agent_tasks_enabled",
    "parse_created_at",
    "is_veteran_account",
    "user_status",
    "complete_task",
    "dismiss",
]
