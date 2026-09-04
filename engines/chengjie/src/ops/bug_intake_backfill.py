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

**二次观察收口（J-5 A，2026-09-05）**：水位账本只知道「本模块回放到哪」，
不知道实时链已经处理过哪些 mid——实时链每次进程重启后 ``note_group_msg_seq``
的 ``_SEQ`` 归零、backfill 水位又落后于实况，于是 0904 21:0x 花无缺一句
「我这边显示的都好了」实时被限频压下后，4 分钟后被 backfill 当漏网重喂：
#153 落了第二条 rate_capped 正文、verify_yes 又在 #154 上触发一次。
修法＝**「已观察 mid 集」**（``config/bug_intake_seen.json``，与水位账本同目录）：
实时链 ``note_group_msg_seq`` 一到就 ``mark_seen``（早于去重/限频/observe，
只要消息进过实时链就算见过）；回放前 ``is_seen`` 命中即跳过并计
``seen_skipped``；回放成功也 ``mark_seen``。每群保最近 ``_SEEN_MAX_PER_CHAT``
个 id，跨重启持久。旧水位语义原样保留（首见不回放 / cap 不跳档）。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

logger = logging.getLogger("ai_chat_assistant.bug_intake_backfill")

_STATE_PATH_DEFAULT = Path("config") / "bug_intake_backfill.state.json"

# ── 已观察 mid 集（实时链 × 回放链共享；J-5 A）────────────────────────────────
_SEEN_FILE_NAME = "bug_intake_seen.json"
_SEEN_MAX_PER_CHAT = 2000          # 每群保最近 N 个 id（报障群日均几十条，够覆盖数周）
_SEEN_PATH_OVERRIDE: Optional[Path] = None   # 测试隔离用；生产恒 None
_SEEN_LOCK = threading.Lock()
_SEEN_CACHE: Optional[Dict[str, Dict[int, float]]] = None   # chat → {mid: ts}
_SEEN_CACHE_PATH: Optional[Path] = None


def seen_path() -> Path:
    """已观察集落盘路径：与 ``bug_intake.db`` 同 ``config_dir()`` 契约（生产＝实例
    数据根 config/，与水位账本同目录；测试经 AITR_DATA_DIR 落 tmp）。绝不抛。"""
    if _SEEN_PATH_OVERRIDE is not None:
        return Path(_SEEN_PATH_OVERRIDE)
    try:
        from src.licensing.data_paths import config_dir
        return Path(config_dir()) / _SEEN_FILE_NAME
    except Exception:
        return Path("config") / _SEEN_FILE_NAME


def _seen_load_locked() -> Dict[str, Dict[int, float]]:
    """按当前路径装载（路径变化/首次访问才读盘）；调用方持 ``_SEEN_LOCK``。"""
    global _SEEN_CACHE, _SEEN_CACHE_PATH
    p = seen_path()
    if _SEEN_CACHE is not None and _SEEN_CACHE_PATH == p:
        return _SEEN_CACHE
    data: Dict[str, Dict[int, float]] = {}
    try:
        if p.is_file():
            raw = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                for cid, ent in raw.items():
                    mids = ent.get("mids") if isinstance(ent, dict) else None
                    if not isinstance(mids, list):
                        continue
                    ts0 = float(ent.get("ts") or 0.0)
                    bucket: Dict[int, float] = {}
                    for m in mids:
                        try:
                            bucket[int(m)] = ts0
                        except (TypeError, ValueError):
                            continue
                    if bucket:
                        data[str(cid)] = bucket
    except Exception:
        logger.debug("[bug_backfill] 已观察集读取失败（按空）", exc_info=True)
        data = {}
    _SEEN_CACHE, _SEEN_CACHE_PATH = data, p
    return data


def _seen_save_locked(data: Dict[str, Dict[int, float]]) -> None:
    """整表落盘（tmp+replace 原子换；失败只记 debug——账本丢了最坏是多回放一次，
    observe 侧另有近似去重兜底）。调用方持 ``_SEEN_LOCK``。"""
    p = seen_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            cid: {"mids": sorted(bucket.keys()),
                  "ts": max(bucket.values()) if bucket else 0.0}
            for cid, bucket in data.items() if bucket
        }
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, p)
    except Exception:
        logger.debug("[bug_backfill] 已观察集写入失败（忽略）", exc_info=True)


def mark_seen(chat_id: Any, msg_id: Any, *, now: Optional[float] = None,
              persist: bool = True) -> bool:
    """登记 (chat, mid) 已被某条链观察过；返回 True＝本次新登记，False＝早已在集
    /无效入参。超每群上限按 id 剔最旧（mid 单调，最小即最旧）。绝不抛。"""
    try:
        cid = str(chat_id or "").strip()
        mid = int(msg_id or 0)
    except (TypeError, ValueError):
        return False
    if not cid or mid <= 0:
        return False
    ts = float(now if now is not None else time.time())
    try:
        with _SEEN_LOCK:
            data = _seen_load_locked()
            bucket = data.setdefault(cid, {})
            if mid in bucket:
                return False
            bucket[mid] = ts
            if len(bucket) > _SEEN_MAX_PER_CHAT:
                for old in sorted(bucket.keys())[:len(bucket) - _SEEN_MAX_PER_CHAT]:
                    bucket.pop(old, None)
            if persist:
                _seen_save_locked(data)
        return True
    except Exception:
        logger.debug("[bug_backfill] mark_seen 异常（忽略）", exc_info=True)
        return False


def is_seen(chat_id: Any, msg_id: Any) -> bool:
    """(chat, mid) 是否已被观察过。绝不抛；异常按「未见过」（宁多回放不漏单，
    observe 侧近似去重再兜一层）。"""
    try:
        cid = str(chat_id or "").strip()
        mid = int(msg_id or 0)
        if not cid or mid <= 0:
            return False
        with _SEEN_LOCK:
            return mid in _seen_load_locked().get(cid, {})
    except Exception:
        return False


def seen_count(chat_id: Any) -> int:
    """某群已观察集大小（观测/测试用）。"""
    try:
        with _SEEN_LOCK:
            return len(_seen_load_locked().get(str(chat_id or "").strip(), {}))
    except Exception:
        return 0


def reset_seen_for_tests() -> None:
    """测试隔离：清进程缓存（下次访问按当前路径重读）。不动文件。"""
    global _SEEN_CACHE, _SEEN_CACHE_PATH
    with _SEEN_LOCK:
        _SEEN_CACHE, _SEEN_CACHE_PATH = None, None


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


def _row_reporter_name(r: Dict[str, Any]) -> str:
    """回喂名口径对齐实时链：``first_name + " " + last_name``（行里有姓才拼；
    fetch 方只给 ``reporter_name`` 时原样透传）。"""
    first = str(r.get("reporter_name") or "").strip()
    last = str(r.get("reporter_last_name") or "").strip()
    if last and last not in first:
        return f"{first} {last}".strip()
    return first


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
    outgoing, has_media[, reporter_last_name]}]``（新旧不限序）；``observe`` 缺省
    用真 ``bug_intake.observe_group_message``（测试注入假体）。
    返回 ``{groups, replayed, media_skipped, first_seen, seen_skipped}`` 摘要。

    - 行带 ``reporter_last_name`` 时回喂名＝``first last``（与实时链
      ``_peer_display_name`` 同口径；0904 实录 backfill 记「skuio」实时记
      「skuio 花无缺」，值守对账要靶两个名字）；
    - 已在「已观察 mid 集」的行跳过（计 ``seen_skipped``，水位照推）；回喂成功
      的行 ``mark_seen``；``msg_id`` 透传 observe 供其按 mid 精确去重。
    """
    cfg = parse_backfill_cfg(config)
    summary = {"groups": 0, "replayed": 0, "media_skipped": 0, "first_seen": 0,
               "seen_skipped": 0}
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
            rid = int(r.get("id") or 0)
            if is_seen(chat_id, rid):
                # 实时链已经处理过（限频压下/登记过/去重过都算）——回放＝二次观察
                summary["seen_skipped"] += 1
                replayed_top = max(replayed_top, rid)
                continue
            try:
                observe(
                    config, chat_id=chat_id, account_id=account_id,
                    reporter_id=str(r.get("reporter_id") or ""),
                    reporter_name=_row_reporter_name(r),
                    text=str(r.get("text") or ""),
                    msg_id=rid,
                )
                summary["replayed"] += 1
                replayed_top = max(replayed_top, rid)
                mark_seen(chat_id, rid, now=ts)
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
