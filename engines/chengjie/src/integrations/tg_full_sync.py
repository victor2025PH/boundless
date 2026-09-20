# -*- coding: utf-8 -*-
"""Telegram 账号「一键全量深同步」引擎（P2，2026-08-05）。

把「新设备登录官方客户端」的浅铺底（账号级 sync-history：100 会话 × 每会话 ≤100 条）
升级成**战役式全量吸取**：分页枚举云端全部 dialogs → 热会话优先排队 → 逐会话复用
``deep_backfill_tg_history`` 深回填 → 断点续跑。设计要点：

- **热优先**：私聊在前、按最近活跃降序——坐席真正会翻的会话最先就位，
  「接入 10 分钟可用」不必等全量跑完；群组默认不深拉（``include_groups``）。
- **预算化**：``per_chat``（单会话条数）× ``total_messages``（单轮总 RPC 条数，按
  **fetched** 计——预算约束的是风控面/云端拉取量，不是净新增）× ``max_chats``；
  预算耗尽 → ``budget_exhausted=True`` 收工，**再点一次接着跑**（状态记忆已完成会话）。
- **断点续跑**：每完成一个会话即原子落盘状态文件（tmp+replace）；中途崩溃最多
  重拉「正在进行中的那一个会话」，store 主键幂等（INSERT OR IGNORE）不产生重复行。
- **单飞**：同账号同时只跑一份；与账号级 sync-history 互斥由路由层把关
  （见 ``unified_inbox_account_routes``——两个方向都拦）。
- **媒体刻意不下载**：深历史媒体行经 ``history_message_obj`` 结构化落 ``media_type``，
  本体走坐席「拉取原件」按需回填（P1）——全量同步的磁盘/RPC 压力只有文本行。

状态文件：``<实例数据根>/config/tg_full_sync/<account>.json``（服务进程 CWD 契约；
测试/CLI 显式传 ``state_path``，绝不写仓库 config/）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

logger = logging.getLogger(__name__)

# ── 配置 ────────────────────────────────────────────────────────────────────

_DEF_PER_CHAT = 300
_DEF_TOTAL = 20000
_DEF_PACE_SEC = 1.0
_DEF_MAX_CHATS = 0          # 0 = 不限（由 total_messages 兜底）


def parse_full_sync_cfg(tg_cfg: Any) -> Optional[Dict[str, Any]]:
    """解析 ``telegram.full_sync``。返回带夹紧的配置 dict；未开启返回 None。

    宽容 ``full_sync: true``（全默认）与 dict 两种写法；非法类型一律视为关。
    夹紧边界：per_chat 1..2000、total_messages 100..200000、pace_sec 0..30、
    max_chats 0..10000——上限防手滑配出「一轮 20 万条 RPC」级别的风控事故。
    """
    raw = (tg_cfg or {}).get("full_sync") if isinstance(tg_cfg, dict) else None
    if isinstance(raw, bool):
        raw = {"enabled": raw}
    # 与 mirror_outgoing_media 同语义：写成 dict 即视为要开（enabled 可显式置 false）
    if not isinstance(raw, dict) or not bool(raw.get("enabled", True)):
        return None

    def _num(key: str, dflt: float, lo: float, hi: float) -> float:
        try:
            v = float(raw.get(key, dflt))
        except (TypeError, ValueError):
            v = float(dflt)
        return max(lo, min(hi, v))

    return {
        "per_chat": int(_num("per_chat", _DEF_PER_CHAT, 1, 2000)),
        "total_messages": int(_num("total_messages", _DEF_TOTAL, 100, 200000)),
        "pace_sec": _num("pace_sec", _DEF_PACE_SEC, 0.0, 30.0),
        "max_chats": int(_num("max_chats", _DEF_MAX_CHATS, 0, 10000)),
        "include_groups": bool(raw.get("include_groups", False)),
    }


# ── 排队（纯函数） ──────────────────────────────────────────────────────────

def order_dialog_candidates(
    metas: List[Dict[str, Any]], done_keys: Set[str], *,
    include_groups: bool = False,
) -> List[Dict[str, Any]]:
    """从 dialog 元数据里挑出本轮待深拉的会话并排序（纯函数，热优先）。

    meta 契约：``{"chat_key": str, "chat_type": "private"|"group", "last_ts": float}``。
    规则：已完成（状态文件里有记录）跳过；``include_groups=False`` 时群组整类跳过；
    排序＝私聊在前 → 最近活跃降序（坐席最常翻的最先就位）。
    """
    out = []
    for m in metas or []:
        key = str(m.get("chat_key") or "")
        if not key or key in done_keys:
            continue
        ctype = str(m.get("chat_type") or "private")
        if ctype != "private" and not include_groups:
            continue
        out.append(m)
    return sorted(out, key=lambda m: (
        0 if str(m.get("chat_type") or "private") == "private" else 1,
        -float(m.get("last_ts") or 0),
    ))


# ── 断点状态（原子落盘） ────────────────────────────────────────────────────

def default_state_path(account_id: str) -> Path:
    """生产状态文件落点：``config/tg_full_sync/<acct>.json``（相对服务进程 CWD＝
    实例数据根，与 logs/config 同为 C 类数据落点）。文件名消毒防路径穿越。"""
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", str(account_id or "default"))
    safe = safe.lstrip(".") or "default"    # 防隐藏文件/相对引用观感，纯文件名无穿越面
    return Path("config") / "tg_full_sync" / f"{safe}.json"


def load_state(path: Path) -> Dict[str, Any]:
    """读状态文件；缺失/损坏一律回空白状态（战役从头开始，不抛）。"""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and isinstance(data.get("chats"), dict):
            data.setdefault("campaign", {})
            return data
    except FileNotFoundError:
        pass
    except Exception:
        logger.debug("[full_sync] 状态文件损坏，按空白重来 path=%s", path, exc_info=True)
    return {"chats": {}, "campaign": {}}


def save_state(path: Path, state: Dict[str, Any]) -> None:
    """原子写状态（tmp+replace）；父目录按需创建。失败只记日志不抛（丢的是
    断点信息，最坏下轮多拉几个会话，store 幂等不重复落行）。"""
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, p)
    except Exception:
        logger.debug("[full_sync] 状态落盘失败 path=%s", path, exc_info=True)


# ── 进度快照（进程级，路由轮询用） ──────────────────────────────────────────

_RUNS: Dict[str, Dict[str, Any]] = {}
_RUNS_LOCK = threading.Lock()


def full_sync_snapshot(account_id: str) -> Dict[str, Any]:
    with _RUNS_LOCK:
        st = _RUNS.get(str(account_id or ""))
        return dict(st) if st else {"state": "idle"}


def _update(account_id: str, **kw: Any) -> None:
    with _RUNS_LOCK:
        _RUNS.setdefault(str(account_id or ""), {}).update(kw)


def _try_start(account_id: str) -> bool:
    """原子置 running；已在跑返回 False（同账号单飞）。"""
    with _RUNS_LOCK:
        st = _RUNS.setdefault(str(account_id or ""), {})
        if st.get("state") == "running":
            return False
        st.update(state="running", chats_done=0, pending=0, dialogs_total=0,
                  messages=0, fetched=0, budget_exhausted=False,
                  error="", error_kind="", started_at=time.time(),
                  finished_at=0.0)
        return True


# ── 主运行器 ────────────────────────────────────────────────────────────────

def _dialog_meta(dialog: Any) -> Optional[Dict[str, Any]]:
    """pyrogram Dialog → 排队用 meta；无 chat id → None。bot 私聊归 private 类。"""
    chat = getattr(dialog, "chat", None)
    cid = getattr(chat, "id", None)
    if cid is None:
        return None
    ct = getattr(chat, "type", None)
    ctype = str(getattr(ct, "value", "") or getattr(ct, "name", "") or "").lower()
    top = getattr(dialog, "top_message", None)
    mdate = getattr(top, "date", None)
    ts = mdate.timestamp() if (mdate is not None and hasattr(mdate, "timestamp")) else 0.0
    return {
        "chat_key": str(cid),
        "chat_type": "private" if ctype in ("private", "bot") else "group",
        "last_ts": float(ts or 0),
    }


async def run_full_sync(
    client: Any, account_id: str, *,
    ingest: Callable[[Dict[str, Any], List[Dict[str, Any]]], int],
    cfg: Dict[str, Any],
    state: Dict[str, Any],
    save_state_fn: Callable[[Dict[str, Any]], None],
    progress: Optional[Callable[[Dict[str, Any]], None]] = None,
    backfill: Optional[Callable[..., Any]] = None,
) -> Dict[str, Any]:
    """执行一轮全量深同步战役（跑在 pyrogram 自身 loop）。

    - ``backfill`` 可注入（默认 ``deep_backfill_tg_history``，测试传假件）；
    - 单会话失败（peer 失效等）也记进状态（含 error 字段）并跳过——不让一个
      死会话把战役卡成无限重试；要重来用 restart 清状态；
    - 每会话完成即落盘状态 + 回调 progress（路由据此刷新进度快照）。
    """
    if backfill is None:
        from src.integrations.protocol_bridge import deep_backfill_tg_history
        backfill = deep_backfill_tg_history
    stats: Dict[str, Any] = {
        "dialogs_total": 0, "pending": 0, "chats_done": 0,
        "messages": 0, "fetched": 0, "budget_exhausted": False,
    }
    metas: List[Dict[str, Any]] = []
    async for dialog in client.get_dialogs():
        m = _dialog_meta(dialog)
        if m is not None:
            metas.append(m)
    chats_state: Dict[str, Any] = state.setdefault("chats", {})
    cands = order_dialog_candidates(
        metas, set(chats_state), include_groups=bool(cfg.get("include_groups")))
    stats["dialogs_total"] = len(metas)
    stats["pending"] = len(cands)
    if progress is not None:
        try:
            progress(dict(stats))
        except Exception:
            pass
    for meta in cands:
        if cfg.get("max_chats") and stats["chats_done"] >= int(cfg["max_chats"]):
            stats["budget_exhausted"] = True
            break
        remaining = int(cfg["total_messages"]) - stats["fetched"]
        if remaining <= 0:
            stats["budget_exhausted"] = True
            break
        budget = min(int(cfg["per_chat"]), remaining)
        err = ""
        try:
            res = await backfill(
                client, account_id, meta["chat_key"],
                max_messages=budget, ingest=ingest)
        except Exception as exc:
            logger.debug("[full_sync] 单会话深拉失败 chat=%s", meta["chat_key"],
                         exc_info=True)
            res, err = None, str(exc)[:120]
        if not isinstance(res, dict):
            res = {"fetched": 0, "inserted": 0, "exhausted": False}
        entry: Dict[str, Any] = {
            "fetched": int(res.get("fetched") or 0),
            "inserted": int(res.get("inserted") or 0),
            "exhausted": bool(res.get("exhausted")),
            "ts": time.time(),
        }
        if err:
            entry["error"] = err
        chats_state[meta["chat_key"]] = entry
        stats["chats_done"] += 1
        stats["fetched"] += entry["fetched"]
        stats["messages"] += entry["inserted"]
        camp = state.setdefault("campaign", {})
        camp["total_ingested"] = int(camp.get("total_ingested") or 0) + entry["inserted"]
        camp["total_fetched"] = int(camp.get("total_fetched") or 0) + entry["fetched"]
        camp["last_run_ts"] = time.time()
        try:
            save_state_fn(state)
        except Exception:
            logger.debug("[full_sync] save_state 回调失败", exc_info=True)
        if progress is not None:
            try:
                progress(dict(stats))
            except Exception:
                pass
        pace = float(cfg.get("pace_sec") or 0)
        if pace > 0:
            await asyncio.sleep(pace)
    return stats


def start_full_sync(
    pyro: Any, store: Any, account_id: str, *,
    tg_cfg: Any, state_path: Path, restart: bool = False,
) -> Dict[str, Any]:
    """触发一轮全量深同步（后台跑在该账号 pyrogram 自身 loop，立即返回）。

    路由薄包装的唯一入口：配置闸（默认关）→ client 可用性 → 单飞 → 载入/重置
    断点状态 → 后台跑 ``run_full_sync``。与账号级 sync-history 的互斥由调用方
    （路由层）负责——本函数不感知那张登记表，保持模块无环依赖。
    """
    cfg = parse_full_sync_cfg(tg_cfg)
    if cfg is None:
        return {"ok": False, "reason": "disabled"}
    loop = getattr(pyro, "loop", None)
    if pyro is None or store is None or loop is None or not loop.is_running():
        return {"ok": False, "reason": "client_unavailable"}
    if not _try_start(account_id):
        return {"ok": True, "already_running": True,
                **full_sync_snapshot(account_id)}
    state = {"chats": {}, "campaign": {}} if restart else load_state(state_path)
    if restart:
        state["campaign"] = {"restarted_at": time.time()}
    from src.inbox.ingest import ingest_thread

    def _ingest(chat: Dict[str, Any], msgs: List[Dict[str, Any]]) -> int:
        return ingest_thread(store, chat, msgs)

    def _progress(s: Dict[str, Any]) -> None:
        _update(account_id, chats_done=s.get("chats_done", 0),
                pending=s.get("pending", 0),
                dialogs_total=s.get("dialogs_total", 0),
                messages=s.get("messages", 0), fetched=s.get("fetched", 0))

    async def _run() -> None:
        try:
            stats = await run_full_sync(
                pyro, account_id, ingest=_ingest, cfg=cfg, state=state,
                save_state_fn=lambda st: save_state(state_path, st),
                progress=_progress)
            _update(account_id, state="done", finished_at=time.time(),
                    chats_done=stats["chats_done"], pending=stats["pending"],
                    dialogs_total=stats["dialogs_total"],
                    messages=stats["messages"], fetched=stats["fetched"],
                    budget_exhausted=stats["budget_exhausted"])
        except Exception as exc:
            logger.debug("[full_sync] 战役异常终止 acct=%s", account_id, exc_info=True)
            from src.integrations.protocol_bridge import tg_error_kind
            _update(account_id, state="error", error=str(exc)[:200],
                    error_kind=tg_error_kind(exc), finished_at=time.time())

    asyncio.run_coroutine_threadsafe(_run(), loop)
    return {"ok": True, "started": True, **full_sync_snapshot(account_id)}


__all__ = [
    "parse_full_sync_cfg", "order_dialog_candidates",
    "default_state_path", "load_state", "save_state",
    "full_sync_snapshot", "run_full_sync", "start_full_sync",
]
