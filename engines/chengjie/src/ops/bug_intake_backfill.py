# -*- coding: utf-8 -*-
"""报障群断线补拉（实施74 B123，值守工具链自察项）。

事故（0827 凌晨实锤）：117 离线期（03:18~04:16）报障群两条消息（03:32/03:36）
无归档无登记——``bug_intake`` 只吃实时 update，无 catch-up，靠值守手工 sync
才发现漏单。

机制：telegram_client 后台循环周期性拉报障群近端历史，与**本模块自己的
seen 账本**（``config/bug_intake_backfill.state.json``，C 类数据落实例数据根）
做 msg_id 差集，漏网消息按时间升序回喂 ``bug_intake.observe_group_message``
（与实时链同一入口＝分类/工单/去重照常生效），随后推进账本水位。

护栏：

- **首见群不回放**（账本无水位 → 只标记当前最新 id）：防首次部署把整页
  历史当新报障回放；
- 每群每轮回放 ≤ ``cap``（默认 30），文本消息为主——媒体消息回喂占位文本
  （截图归档链属实时管线，补拉轮如实记 ``media_skipped`` 计数不装完整）;
- 自家账号/支持账号发言不回放（与实时链 observe 只登记客户报障的语义对齐，
  由 observe 内部判定兜底，这里先剔自家 outgoing 省调用）；
- 任何异常整轮吞掉只记日志，绝不影响主消息循环。

配置 ``bug_intake.backfill.{enabled, interval_sec, cap}``——随 ``bug_intake``
主开关走（bug_intake.enabled=false 或 groups 空 → 循环整体不跑）；
backfill.enabled 默认 **true**（本功能就是监控自察，装上即该工作）。
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

logger = logging.getLogger("ai_chat_assistant.bug_intake_backfill")

_STATE_PATH_DEFAULT = Path("config") / "bug_intake_backfill.state.json"


# ── 配置 ─────────────────────────────────────────────────────────────────────

def parse_backfill_cfg(config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """解析补拉配置；``enabled`` 已并入 bug_intake 主开关与 groups 非空判定。"""
    from src.ops.bug_intake import parse_cfg
    base = parse_cfg(config)
    raw: Dict[str, Any] = {}
    try:
        raw = ((config or {}).get("bug_intake") or {}).get("backfill") or {}
        if not isinstance(raw, dict):
            raw = {}
    except Exception:
        raw = {}
    interval = 300
    cap = 30
    try:
        interval = max(60, int(raw.get("interval_sec", 300)))
    except (TypeError, ValueError):
        interval = 300
    try:
        cap = max(1, min(100, int(raw.get("cap", 30))))
    except (TypeError, ValueError):
        cap = 30
    enabled = bool(base["enabled"]) and bool(base["groups"]) \
        and bool(raw.get("enabled", True))
    return {"enabled": enabled, "groups": set(base["groups"]),
            "interval_sec": interval, "cap": cap}


# ── seen 账本 ────────────────────────────────────────────────────────────────

def load_state(path: Optional[Path] = None) -> Dict[str, Dict[str, Any]]:
    p = Path(path) if path else _STATE_PATH_DEFAULT
    try:
        if p.is_file():
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {str(k): dict(v) for k, v in data.items()
                        if isinstance(v, dict)}
    except Exception:
        logger.debug("[bug_backfill] 账本读取失败（按空）", exc_info=True)
    return {}


def save_state(state: Dict[str, Dict[str, Any]],
               path: Optional[Path] = None) -> None:
    p = Path(path) if path else _STATE_PATH_DEFAULT
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(state, ensure_ascii=False, indent=1),
                     encoding="utf-8")
    except Exception:
        logger.debug("[bug_backfill] 账本写入失败（忽略）", exc_info=True)


# ── 纯函数：回放计划 ─────────────────────────────────────────────────────────

def plan_replay(
    rows: List[Dict[str, Any]], last_msg_id: Optional[int], *, cap: int = 30,
) -> List[Dict[str, Any]]:
    """从近端历史行挑「账本水位之后」的漏网行，按 msg_id 升序返回。

    - ``last_msg_id is None``（首见群）→ 空列表（只标记不回放）；
    - 只回放带非空 ``text`` 且非自家 outgoing 的行；
    - 超 cap 取**最旧**的 cap 条（漏得最久的优先，剩余下一轮继续——水位只推
      到已回放的最大 id，不跳档）。
    """
    if last_msg_id is None:
        return []
    missed = [
        r for r in (rows or [])
        if isinstance(r, dict)
        and int(r.get("id") or 0) > int(last_msg_id)
        and str(r.get("text") or "").strip()
        and not bool(r.get("outgoing"))
    ]
    missed.sort(key=lambda r: int(r.get("id") or 0))
    return missed[:cap]


def max_row_id(rows: List[Dict[str, Any]]) -> int:
    best = 0
    for r in rows or []:
        try:
            best = max(best, int(r.get("id") or 0))
        except (TypeError, ValueError):
            continue
    return best


# ── 执行一轮 ─────────────────────────────────────────────────────────────────

async def run_backfill_once(
    config: Optional[Dict[str, Any]],
    fetch_history: Callable[[str, int], Awaitable[List[Dict[str, Any]]]],
    *,
    observe: Optional[Callable[..., Dict[str, Any]]] = None,
    account_id: str = "",
    state_path: Optional[Path] = None,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """对全部报障群跑一轮补拉。

    ``fetch_history(chat_id, cap) -> [{id, text, reporter_id, reporter_name,
    outgoing, has_media}]``（新旧不限序）；``observe`` 缺省用真
    ``bug_intake.observe_group_message``（测试注入假体）。
    返回 ``{groups, replayed, media_skipped, first_seen}`` 摘要。
    """
    cfg = parse_backfill_cfg(config)
    summary = {"groups": 0, "replayed": 0, "media_skipped": 0, "first_seen": 0}
    if not cfg["enabled"]:
        return summary
    if observe is None:
        from src.ops.bug_intake import observe_group_message as observe  # type: ignore
    state = load_state(state_path)
    ts = float(now if now is not None else time.time())
    dirty = False
    for chat_id in sorted(cfg["groups"]):
        summary["groups"] += 1
        try:
            rows = await fetch_history(chat_id, cfg["cap"] * 2)
        except Exception:
            logger.debug("[bug_backfill] 拉取 %s 历史失败（本轮跳过）",
                         chat_id, exc_info=True)
            continue
        rows = [r for r in (rows or []) if isinstance(r, dict)]
        top = max_row_id(rows)
        if top <= 0:
            continue
        entry = state.get(str(chat_id))
        last_id = None
        if isinstance(entry, dict) and entry.get("last_msg_id") is not None:
            try:
                last_id = int(entry.get("last_msg_id"))
            except (TypeError, ValueError):
                last_id = None
        if last_id is None:
            # 首见群：只立水位不回放（防把历史当新报障）
            state[str(chat_id)] = {"last_msg_id": top, "ts": ts}
            summary["first_seen"] += 1
            dirty = True
            continue
        missed = plan_replay(rows, last_id, cap=cfg["cap"])
        media_only = [
            r for r in rows
            if int(r.get("id") or 0) > last_id and bool(r.get("has_media"))
            and not str(r.get("text") or "").strip()
            and not bool(r.get("outgoing"))
        ]
        summary["media_skipped"] += len(media_only)
        replayed_top = last_id
        for r in missed:
            try:
                observe(
                    config, chat_id=chat_id, account_id=account_id,
                    reporter_id=str(r.get("reporter_id") or ""),
                    reporter_name=str(r.get("reporter_name") or ""),
                    text=str(r.get("text") or ""),
                )
                summary["replayed"] += 1
                replayed_top = max(replayed_top, int(r.get("id") or 0))
            except Exception:
                logger.warning("[bug_backfill] 回喂 %s#%s 失败（跳过该条）",
                               chat_id, r.get("id"), exc_info=True)
        # 水位推进：无漏网 → 直接推到 top；有漏网 → 推到已回放最大 id
        #（cap 截断的剩余下一轮继续，绝不跳档丢单）
        new_mark = top if len(missed) < cfg["cap"] else replayed_top
        if new_mark != last_id:
            state[str(chat_id)] = {"last_msg_id": int(new_mark), "ts": ts}
            dirty = True
        if summary["replayed"]:
            logger.info(
                "[bug_backfill] %s 补登记 %s 条（水位 %s→%s）",
                chat_id, summary["replayed"], last_id, new_mark)
    if dirty:
        save_state(state, state_path)
    return summary
