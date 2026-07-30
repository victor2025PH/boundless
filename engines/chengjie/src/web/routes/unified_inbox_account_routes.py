"""统一收件箱——账号池编排器 / 协议入站 / 自动回复路由域（巨石拆分 slice 10）。

把"账号池编排器（M5：多账号 7×24 在线）+ 协议入站桥 + 账号自动回复管理"这一子域，
从 ``register_unified_inbox_routes`` 巨型闭包中整体外移为
``register_account_routes(app, *, api_auth, config_manager)``，由主 register 在**原位置**
顺序调用，以保持以下时序不变：

- ``_register_protocol_sink()`` / ``_register_protocol_autoreply()``：**register 时立即调用**
  的副作用（向 protocol_bridge 注册入站 sink / 自动回复 hook），随子注册函数在挂载时执行。
- ``@app.on_event("startup")`` ``_orchestrator_autostart``：**startup 钩子**，装饰器在挂载时
  注册到 app，启动时机不变。

端点路径/方法/响应零变化（admin_route_inventory URL 契约守卫 + 编排器/自动回复专项兜底）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Dict, List

from fastapi import HTTPException, Request

from src.inbox.channel_adapters import status_via_adapters
from src.integrations.account_orchestrator import (
    account_key as _acct_key,
    ensure_builtin_workers,
    get_orchestrator,
    orchestrator_enabled,
)
from src.integrations.account_registry import get_account_registry
from src.web.routes.unified_inbox_aggregate import _INBOX_ADAPTERS
from src.web.routes.unified_inbox_auth import _session_agent
from src.web.web_i18n import tr

logger = logging.getLogger(__name__)


def _record_profile_ops_event(
    platform: str, account_id: str, *, kind: str, ok: bool,
    fields: Any = None, persona_id: str = "", note: str = "", run_id: str = "",
) -> None:
    """把「官方资料推送/对齐」落一条 ops 事件（90 天可追溯），best-effort。

    - ``kind``：``profile_push``（单号）/ ``profile_align``（批量对齐）；
    - ``reason``：结果标签（``ok`` / ``failed``）；
    - ``detail``：紧凑串 ``fields=name,avatar;persona=<id>;run=<id>;<note>``（不含昵称
      原文/头像，仅字段名/人设 id/批次 id，避免把 PII 写进审计库）。``run_id`` 把同一次
      批量对齐的多条 per-account 事件归组（前端可折叠成「这批 N 号一次对齐」）。
    审计绝不阻断主流程：任何异常静默降级。
    """
    try:
        from src.ops.ops_events import get_ops_event_store
        store = get_ops_event_store()
        if store is None:
            return
        if isinstance(fields, (list, tuple, set)):
            fstr = ",".join(sorted(str(f) for f in fields if f))
        else:
            fstr = str(fields or "")
        parts = [f"fields={fstr}"] if fstr else []
        if persona_id:
            parts.append(f"persona={persona_id}")
        if run_id:
            parts.append(f"run={run_id}")
        if note:
            parts.append(str(note))
        store.record(
            kind, account_id=str(account_id or ""),
            platform=str(platform or "").lower() or "telegram",
            reason="ok" if ok else "failed",
            detail=";".join(parts),
        )
    except Exception:
        logger.debug("[profile_push] ops 事件审计失败（忽略）", exc_info=True)


def _recent_profile_events(
    platform: str, account_id: str, limit: int = 20,
) -> List[Dict[str, Any]]:
    """取该号近期「资料推送/对齐」审计事件（跨运行，含批量），best-effort。

    只回 ``profile_push`` / ``profile_align`` 两类，供详情页「变更历史」展开；
    缺库或异常 → 空列表（详情页只是少一块，不报错）。
    """
    try:
        from src.ops.ops_events import get_ops_event_store
        store = get_ops_event_store()
        if store is None:
            return []
        rows = store.recent(account_id=str(account_id or ""),
                             limit=max(1, int(limit)) * 3)
        out: List[Dict[str, Any]] = []
        for r in rows:
            if str(r.get("kind") or "") not in ("profile_push", "profile_align"):
                continue
            out.append({
                "ts": float(r.get("ts") or 0),
                "kind": str(r.get("kind") or ""),
                "ok": bool(r.get("reason") == "ok"),
                "detail": str(r.get("detail") or ""),
            })
            if len(out) >= limit:
                break
        return out
    except Exception:
        return []


_last_avatar_sweep_ts: float = 0.0
_AVATAR_SWEEP_INTERVAL_S = 3600.0  # 孤儿头像清扫最短间隔（机会式，随 fleet-health 触发）

# Telegram peer 身份惰性解析的进程级缓存：{(account_id, chat_key): (ts, result|None)}。
# 正缓存(命中真身份)7 天、负缓存(取不到)1 小时——避免反复对同一 peer 打 get_chat。
# client 不在线时**不缓存**（下次上线仍会重试）。
_TG_PEER_IDENTITY_CACHE: Dict[tuple, tuple] = {}
_TG_PEER_IDENTITY_TTL_OK = 7 * 86400.0
_TG_PEER_IDENTITY_TTL_MISS = 3600.0
_TG_PEER_IDENTITY_CACHE_MAX = 5000  # distinct peer 上限，防内存无界增长

# Telegram client「断网冷却」：get_chat/download_media **超时**（≠业务错误——那是立即失败）
# 说明 pyrogram 正断网重连；冷却窗内同账号的 resolve/avatar 直接快速失败，不再发 RPC 排队。
# 修「断网时第一个数字号吃满超时、后续每个可见头像再各吃一次」——现在只有第一个付超时成本。
# 冷却是账号级、不写 per-peer 负缓存（恢复后同一 peer 仍会重试）；成功一次即清除。
_TG_CLIENT_BAD_UNTIL: Dict[str, float] = {}
_TG_CLIENT_COOLDOWN_SEC = 60.0
# get_chat/头像下载的跨 loop 等待超时。原 15s：正常 RPC 数百 ms，15s 只在断网挂满时才吃到
# ——把它降到 8s 缩小「首个牺牲者」的等待，同时仍远大于正常延迟（不误伤慢网）。
_TG_FETCH_TIMEOUT_SEC = 8.0


# resolve-peer 端点并发上限：占用的是 starlette 共享线程池（全站 sync 端点共用 ~40 线程），
# 断网时每个 resolve 挂满 8s，无上限会把池吃空。超限直接快败（best-effort 补名，可重试）。
_RESOLVE_PEER_SEM = threading.Semaphore(4)

# 通讯录「立即同步」按钮的进程内限频：{platform:account_id: 上次触发的 monotonic}。
# 目录同步是重 RPC（Telegram 要遍历 dialogs / WhatsApp 要 resyncAppState 全量回流），
# 坐席狂点会把 Telegram RPC 打爆触发 FloodWait——那会**连累正常收发消息**几分钟到几小时，
# 代价远大于「晚 10 分钟看到新好友」。故限死同号 10 分钟一次。
# 只放进程内 dict：重启后重新计时的代价（最多多一次同步）远小于持久化的复杂度。
_CONTACTS_REFRESH_COOLDOWN_SEC = 600.0
_CONTACTS_REFRESH_LAST: Dict[str, float] = {}


def _contacts_refresh_cooldown_left(platform: str, account_id: str) -> float:
    """距下次允许「立即同步」还剩几秒；0 = 现在可以。"""
    key = f"{platform}:{account_id}"
    last = _CONTACTS_REFRESH_LAST.get(key)
    if last is None:
        return 0.0
    left = _CONTACTS_REFRESH_COOLDOWN_SEC - (time.monotonic() - last)
    if left <= 0:
        _CONTACTS_REFRESH_LAST.pop(key, None)
        return 0.0
    return left


def _contacts_refresh_mark(platform: str, account_id: str) -> None:
    """记一次真实触发（仅在同步真的发出去之后调用，失败不该占用冷却窗）。"""
    _CONTACTS_REFRESH_LAST[f"{platform}:{account_id}"] = time.monotonic()

# Telegram 账号级聊天记录同步的进度状态：{account_id: {state, dialogs_done, dialogs_total,
# messages, error, started_at, finished_at}}。同步协程跑在 pyrogram 自身 loop（独立线程），
# web 线程轮询读——全程经 _TG_HIST_SYNC_LOCK 保护。
_TG_HIST_SYNC: Dict[str, Dict[str, Any]] = {}
_TG_HIST_SYNC_LOCK = threading.Lock()


def tg_history_sync_snapshot(account_id: str) -> Dict[str, Any]:
    """读取某账号历史同步进度快照（无记录 → state=idle）。"""
    with _TG_HIST_SYNC_LOCK:
        st = _TG_HIST_SYNC.get(str(account_id or ""))
        return dict(st) if st else {"state": "idle"}


def _tg_hist_update(account_id: str, **kw: Any) -> None:
    with _TG_HIST_SYNC_LOCK:
        _TG_HIST_SYNC.setdefault(str(account_id or ""), {}).update(kw)


def _tg_hist_try_start(account_id: str) -> bool:
    """原子地把该账号置为 running；已在跑返回 False（防重复并发同步）。"""
    with _TG_HIST_SYNC_LOCK:
        st = _TG_HIST_SYNC.setdefault(str(account_id or ""), {})
        if st.get("state") == "running":
            return False
        st.update(state="running", dialogs_done=0, dialogs_total=0, messages=0,
                  error="", started_at=time.time(), finished_at=0.0)
        return True


def _persist_tg_history_sync(
    account_id: str, *, finished_at: float, dialogs: int = 0, messages: int = 0,
) -> None:
    """把本次成功同步的完成时间写入 registry.meta（跨进程重启仍可提示「上次同步」）。

    read-merge-write：upsert(meta=) 是整块覆盖，必须先读再叠加，否则会抹掉
    session_string / self_* 等既有字段。账号不在注册表（如 A 线 config 默认号）→ 静默跳过，
    仅依赖进程内 ``_TG_HIST_SYNC`` 快照。
    """
    try:
        from src.integrations.account_registry import get_account_registry
        reg = get_account_registry()
        row = reg.get("telegram", str(account_id or ""))
        if row is None:
            return
        meta = dict(row.get("meta") or {})
        meta["last_history_sync_ts"] = float(finished_at or 0)
        meta["last_history_sync_dialogs"] = int(dialogs or 0)
        meta["last_history_sync_messages"] = int(messages or 0)
        reg.upsert("telegram", str(account_id or ""), meta=meta)
    except Exception:
        logger.debug("[protocol] 持久化 tg 历史同步时间失败", exc_info=True)


def read_tg_history_sync_persisted(account_id: str) -> float:
    """从 registry.meta 读持久化的上次历史同步完成时间（无 → 0）。"""
    try:
        from src.integrations.account_registry import get_account_registry
        row = get_account_registry().get("telegram", str(account_id or ""))
        if not row:
            return 0.0
        return float((row.get("meta") or {}).get("last_history_sync_ts") or 0)
    except Exception:
        return 0.0


def _tg_client_cooling(account_id: str) -> bool:
    """该账号的 TG client 是否在断网冷却窗内（True=直接快速失败，不发 RPC）。"""
    until = _TG_CLIENT_BAD_UNTIL.get(str(account_id or ""))
    if until is None:
        return False
    if time.monotonic() >= until:
        _TG_CLIENT_BAD_UNTIL.pop(str(account_id or ""), None)
        return False
    return True


def _mark_tg_client_bad(account_id: str) -> None:
    _TG_CLIENT_BAD_UNTIL[str(account_id or "")] = (
        time.monotonic() + _TG_CLIENT_COOLDOWN_SEC)


def _clear_tg_client_bad(account_id: str) -> None:
    _TG_CLIENT_BAD_UNTIL.pop(str(account_id or ""), None)


def tg_cooldown_snapshot() -> Dict[str, float]:
    """当前处于断网冷却中的 TG 账号 → 剩余秒数（供 ops「平台会话健康」卡可视化）。

    过期条目顺手清掉；无冷却返回空 dict（前端据此不渲染附加行）。
    """
    now = time.monotonic()
    out: Dict[str, float] = {}
    for acct in list(_TG_CLIENT_BAD_UNTIL):
        left = _TG_CLIENT_BAD_UNTIL.get(acct, 0.0) - now
        if left <= 0:
            _TG_CLIENT_BAD_UNTIL.pop(acct, None)
            continue
        out[acct] = round(left, 1)
    return out


def _cache_tg_peer_identity(cache_key: tuple, result, ts: float) -> None:
    """写入 peer 身份解析缓存（``result`` 为 dict=正缓存 / None=负缓存），带上限逐出。

    超上限时按写入时间戳丢弃最旧的 1/5，避免热点误伤且摊薄逐出成本。
    """
    if len(_TG_PEER_IDENTITY_CACHE) >= _TG_PEER_IDENTITY_CACHE_MAX \
            and cache_key not in _TG_PEER_IDENTITY_CACHE:
        for k in sorted(_TG_PEER_IDENTITY_CACHE,
                        key=lambda kk: _TG_PEER_IDENTITY_CACHE[kk][0]
                        )[: max(1, _TG_PEER_IDENTITY_CACHE_MAX // 5)]:
            _TG_PEER_IDENTITY_CACHE.pop(k, None)
    _TG_PEER_IDENTITY_CACHE[cache_key] = (ts, result)


def _record_peer_identity(source: str, outcome: str) -> None:
    """记 peer 身份解析结果到观测单例（best-effort，绝不影响主流程）。"""
    try:
        from src.web.peer_identity_stats import get_peer_identity_stats
        get_peer_identity_stats().record(source, outcome)
    except Exception:
        pass


def _record_peer_route(outcome: str, account_id: str = "") -> None:
    """记一次多账号 client 路由决策到观测单例（best-effort，绝不影响主流程）。"""
    try:
        from src.web.peer_identity_stats import get_peer_identity_stats
        get_peer_identity_stats().record_route(outcome, account_id)
    except Exception:
        pass


def _record_ingest_identity(platform: str, outcome: str) -> None:
    """记一次入站身份分类（named/backfilled/raw）到观测单例（best-effort，绝不影响主流程）。

    同时旁路写入按日趋势库（F1，默认关时恒 no-op）——供 ops 画 raw% 7 天 sparkline。
    """
    try:
        from src.web.peer_identity_stats import get_peer_identity_stats
        get_peer_identity_stats().record_ingest(platform, outcome)
    except Exception:
        pass
    try:
        from src.web.identity_trend_store import record_identity_trend
        record_identity_trend(
            ing_named=(1 if outcome == "named" else 0),
            ing_backfilled=(1 if outcome == "backfilled" else 0),
            ing_raw=(1 if outcome == "raw" else 0))
    except Exception:
        pass


def _record_avatar(platform: str, outcome: str) -> None:
    """记一次头像端点结局（cache_hit/fetched/empty/error/neg_hit）到观测单例（best-effort）。

    同时旁路写入按日趋势库（F1，默认关时恒 no-op）——hit=cache_hit+fetched（拿到图）、empty=空。
    """
    try:
        from src.web.peer_identity_stats import get_peer_identity_stats
        get_peer_identity_stats().record_avatar(platform, outcome)
    except Exception:
        pass
    try:
        from src.web.identity_trend_store import record_identity_trend
        record_identity_trend(
            av_hit=(1 if outcome in ("cache_hit", "fetched") else 0),
            av_empty=(1 if outcome == "empty" else 0),
            av_total=1)
    except Exception:
        pass


def _extract_pyro(obj: Any) -> Any:
    """从「client-ish」对象抽出底层 pyrogram client（有 ``.loop`` + ``.get_chat`` 的那个）。

    统一三种形态：① pyrogram ``Client`` 本身（``TelegramProtocolWorker.client`` = B 线薄连接）；
    ② A 线 ``TelegramClient`` 包装器（进程主 client / ``TelegramCompanionWorker.client``），其
    ``.client`` 才是 pyro。取不到 → None。用鸭子类型判定，单测可传 duck-typed 对象。
    """
    if obj is None:
        return None
    if hasattr(obj, "loop") and hasattr(obj, "get_chat"):
        return obj
    inner = getattr(obj, "client", None)
    if inner is not None and hasattr(inner, "loop") and hasattr(inner, "get_chat"):
        return inner
    return None


def _get_tg_pyro_for_account(app: Any, account_id: str) -> Any:
    """按 ``account_id`` 取该 Telegram 账号的 pyrogram client（多账号头像/补名取数入口）。

    优先**编排器受管 worker**（companion A 线 / protocol B 线，非主账号的 client 藏在此），
    回落**进程主 A 线 client**（单号 / 主账号 / account_id=default）。全程防御式，任一步
    失败即回落，绝不抛——多账号取不到时优雅降级到「主 client 能取哪个取哪个」的旧行为。

    路由决策（worker/fallback/none）计入 ``peer_identity_stats`` 供 ops 观测多账号命中率 +
    暴露「某账号 worker 掉线→静默降级到主 client」的运维盲点。
    """
    pyro = None
    route = "none"
    try:
        from src.integrations.account_orchestrator import get_orchestrator_if_running
        orch = get_orchestrator_if_running()   # 不存在不创建（避免空配置误建单例）
        if orch is not None:
            worker = orch.worker_for("telegram", account_id)
            wp = _extract_pyro(getattr(worker, "client", None))
            if wp is not None:
                pyro, route = wp, "worker"     # 命中该账号受管 worker（多账号真跑起来了）
    except Exception:
        logger.debug("[protocol] 取账号 worker client 失败 acct=%s", account_id, exc_info=True)
    if pyro is None:
        pyro = _extract_pyro(getattr(getattr(app, "state", None), "telegram_client", None))
        route = "fallback" if pyro is not None else "none"
    _record_peer_route(route, account_id)
    return pyro


def _persist_and_cache_tg_identity(store, account_id: str, chat_key: str,
                                   ident: Dict[str, Any], *, now: float = None) -> Dict[str, str]:
    """把已取到的 peer 身份 ``ident`` 落库（no-clobber）+ 写进程缓存；返回规整后的三字段。

    两处复用同一「落库 + 缓存」尾巴：resolve-peer 端点（打开会话解析）与 avatar 端点
    （懒加载头像顺带补名，零额外 API）。无任何可用字段 → 写**负缓存**并返回空三字段；否则
    落库 + 写**正缓存**（顺带暖了 resolve 缓存，头像补名后再打开会话即命中免二次 get_chat）。
    """
    now = time.time() if now is None else now
    cache_key = (str(account_id), str(chat_key))
    name = (ident.get("name") or "").strip()
    username = (ident.get("username") or "").strip()
    phone = (ident.get("phone") or "").strip()
    if not (name or username or phone):
        _cache_tg_peer_identity(cache_key, None, now)
        return {"name": "", "username": "", "phone": ""}
    if store is not None:
        try:
            store.update_conversation_identity(
                f"telegram:{account_id}:{chat_key}",
                display_name=name or None,
                username=username or None,
                phone=phone or None,
            )
        except Exception:
            logger.debug("[protocol] telegram peer 身份回写失败", exc_info=True)
    result = {"name": name, "username": username, "phone": phone}
    _cache_tg_peer_identity(cache_key, result, now)
    return result


def resolve_tg_peer_identity(store, client, account_id: str,
                             chat_key: str, *, now: float = None) -> Dict[str, Any]:
    """惰性解析 Telegram peer 真实身份并回填 store（resolve-peer 端点的纯核心）。

    抽成模块级函数便于独立单测：入参 ``client`` 可为**已归一 pyro** 或 A 线包装器，内部经
    ``_extract_pyro`` 归一到底层 pyrogram client（带 ``.loop`` + async ``get_chat``；生产由
    ``_get_tg_pyro_for_account`` 按账号取到）。web 与 pyrogram 各自 loop → 经
    ``run_coroutine_threadsafe`` 跨 loop 调度。含进程级正/负缓存、昵称优先级回写（no-clobber）。
    返回 ``{"ok":bool, "name"?, "username"?, "phone"?, "reason"?}``；client 不在线不写缓存。
    结果计入 ``peer_identity_stats``（source=tg_open）供 ops 观测。

    ⚠ 本函数**同步阻塞**（``fut.result(timeout=8)``，pyrogram 断网重连时会挂满超时）——
    async 上下文调用必须经线程池（见 resolve-peer 端点的 ``run_in_threadpool``），
    直接调用会把整个 web 事件循环冻结、全站请求排队。
    """
    import asyncio
    chat_key = str(chat_key or "").strip()
    if store is None or not chat_key:
        return {"ok": False}
    cache_key = (str(account_id), chat_key)
    now = time.time() if now is None else now
    cached = _TG_PEER_IDENTITY_CACHE.get(cache_key)
    if cached is not None:
        ttl = _TG_PEER_IDENTITY_TTL_OK if cached[1] else _TG_PEER_IDENTITY_TTL_MISS
        if (now - cached[0]) < ttl:
            _record_peer_identity("tg_open", "cache_hit")
            return {"ok": bool(cached[1]), **(cached[1] or {})}
        _TG_PEER_IDENTITY_CACHE.pop(cache_key, None)  # 过期，重解析
    if _tg_client_cooling(account_id):
        # 断网冷却：不发 RPC、不写 per-peer 负缓存（恢复后同 peer 仍会重试）
        _record_peer_identity("tg_open", "unavailable")
        return {"ok": False, "reason": "client_cooling"}
    pyro = _extract_pyro(client)
    loop = getattr(pyro, "loop", None)
    if pyro is None or loop is None or not loop.is_running():
        _record_peer_identity("tg_open", "unavailable")
        return {"ok": False, "reason": "client_unavailable"}  # 不写缓存，下次上线重试
    try:
        peer: Any = int(chat_key)
    except (TypeError, ValueError):
        peer = chat_key

    async def _resolve():
        return await pyro.get_chat(peer)

    try:
        fut = asyncio.run_coroutine_threadsafe(_resolve(), loop)
        chat = fut.result(timeout=_TG_FETCH_TIMEOUT_SEC)
    except TimeoutError:
        # 超时=pyrogram 断网重连中（业务错误是立即失败，走下面分支）→ 开账号级冷却窗，
        # 后续 60s 内该账号所有 resolve/avatar 秒失败，不再逐个吃满超时。
        # 刻意不写 per-peer 负缓存：网络恢复后该 peer 应可立即重试。
        _mark_tg_client_bad(account_id)
        logger.debug("[protocol] telegram peer 解析超时（断网冷却 %ss）",
                     _TG_CLIENT_COOLDOWN_SEC)
        _record_peer_identity("tg_open", "unavailable")
        return {"ok": False, "reason": "resolve_timeout"}
    except Exception:
        logger.debug("[protocol] telegram peer 身份解析失败", exc_info=True)
        _cache_tg_peer_identity(cache_key, None, now)
        _record_peer_identity("tg_open", "miss")
        return {"ok": False, "reason": "resolve_failed"}
    _clear_tg_client_bad(account_id)   # RPC 成功 → 解除冷却（若有）
    from src.integrations.protocol_bridge import tg_peer_identity
    result = _persist_and_cache_tg_identity(
        store, account_id, chat_key, tg_peer_identity(chat), now=now)
    if not (result["name"] or result["username"] or result["phone"]):
        _record_peer_identity("tg_open", "miss")
        return {"ok": False}
    _record_peer_identity("tg_open", "resolved")
    return {"ok": True, **result}


def _avatar_disk_paths(platform: str, account_id: str, chat_key: str):
    """会话头像的磁盘缓存路径（jpg / .none 负缓存标记 / 前端 302 目标 url_path）。

    抽成模块级供 whatsapp/messenger 头像分支共用 + 单测（含路径穿越字符消毒——文件名只保
    留字母数字，account 另允 ``_-``；Node 侧查询仍用**原始** chat_key，磁盘名仅作稳定缓存键）。
    telegram 走 pyrogram 专路（``_telegram_peer_avatar``），不经此。
    """
    from src.integrations.protocol_bridge import protocol_media_root
    safe_acct = "".join(c for c in str(account_id) if c.isalnum() or c in "_-")
    safe_key = "".join(c for c in str(chat_key) if c.isalnum())
    adir = protocol_media_root() / str(platform) / "avatars"
    adir.mkdir(parents=True, exist_ok=True)
    jpg = adir / f"{safe_acct}_{safe_key}.jpg"
    none_marker = adir / f"{safe_acct}_{safe_key}.none"
    url_path = f"/static/protocol_media/{platform}/avatars/{safe_acct}_{safe_key}.jpg"
    return jpg, none_marker, url_path


async def _download_and_cache_avatar(url: str, jpg, none_marker, url_path: str,
                                     *, neg_cache: bool = True, on_outcome=None):
    """把 Node 返回的 https 直链头像下载落 /static → 302；空 url/下载失败 → 404。

    whatsapp（baileys profilePictureUrl）与 messenger（scontent 直链）两分支共用同一下载/缓存
    尾巴。空 url 时：
    - ``neg_cache=True``（whatsapp）：写 .none 负缓存(≤1 天)——上游是限流的真 API，"无头像"多为
      长期态，避免反复回源触发风控；
    - ``neg_cache=False``（messenger）：**不写**负缓存——空多半只是「本轮轮询还没缓存到该线程」的
      瞬态（Node 侧只是内存 Map.get，无限流成本），写死 1 天会让这些会话白等一天才显头像；不写则
      下次列表重渲染即重试，轮询补齐后自然自愈。
    ``on_outcome``（可选回调）：命 ``empty``/``fetched``/``error`` 之一供观测（依赖注入而非直连
    观测单例，helper 保持纯粹可测；回调自身异常被吞，绝不影响主流程）。
    返回 ``RedirectResponse``（302→静态图）。
    """
    from fastapi.responses import RedirectResponse
    import os

    def _oc(name: str) -> None:
        if on_outcome:
            try:
                on_outcome(name)
            except Exception:
                pass

    if not url:
        _oc("empty")
        if neg_cache:
            try:
                none_marker.write_text("", encoding="utf-8")   # 无头像 → 负缓存(≤1 天)
            except Exception:
                pass
        raise HTTPException(404, "no avatar")
    try:
        import httpx
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(url)
            r.raise_for_status()
            jpg.write_bytes(r.content)
        if none_marker.exists():
            try:
                os.remove(none_marker)
            except Exception:
                pass
    except HTTPException:
        raise
    except Exception:
        _oc("error")
        logger.debug("[protocol] 头像下载失败", exc_info=True)
        raise HTTPException(404, "no avatar")
    _oc("fetched")
    return RedirectResponse(url_path, status_code=302)


def register_account_routes(app, *, api_auth, config_manager=None) -> None:
    """挂载账号池编排器 / 协议入站 / 自动回复相关端点（/api/accounts*、/api/internal/protocol/ingest）。"""

    # ── 账号池编排器（M5：多账号 7×24 在线，默认关） ────────────────────────
    def _orch():
        cfg = (config_manager.config if config_manager is not None else {}) or {}
        ensure_builtin_workers(cfg)
        return get_orchestrator(cfg)

    # M6①：注册 protocol→收件箱 入站 sink（worker 收到消息时落库；store 在 emit 时惰性取）
    def _register_protocol_sink() -> None:
        try:
            from src.integrations.protocol_bridge import (
                ingest_incoming, register_inbox_sink, register_inbox_store_getter,
            )

            def _sink(m: Dict[str, Any]) -> None:
                store = getattr(app.state, "inbox_store", None)
                if store is None:
                    return
                ingest_incoming(store, **m)

            register_inbox_sink(_sink)
            # 供官方 webhook 的 auto_ai 让位护栏只读查 automation_mode（System Z 去重）
            register_inbox_store_getter(
                lambda: getattr(app.state, "inbox_store", None))
        except Exception:
            logger.debug("注册 protocol 收件箱 sink 失败", exc_info=True)

    _register_protocol_sink()

    # Phase 3：注册 protocol 自动回复 hook（hook 内自带双闸门，恒注册、运行时按需生效）
    def _register_protocol_autoreply() -> None:
        try:
            from src.integrations.protocol_autoreply import build_reply_hook
            from src.integrations.protocol_bridge import register_reply_hook
            register_reply_hook(build_reply_hook(app))
        except Exception:
            logger.debug("注册 protocol 自动回复 hook 失败", exc_info=True)

    _register_protocol_autoreply()

    @app.post("/api/internal/protocol/ingest")
    async def api_protocol_ingest(request: Request):
        """内部入站桥：Baileys(Node) 等外部 worker 把收到的消息 push 进统一收件箱。"""
        api_auth(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        store = getattr(request.app.state, "inbox_store", None)
        if store is None:
            raise HTTPException(503, tr(request, "err.svc.inbox_not_ready"))
        from src.integrations.protocol_bridge import (
            ingest_incoming, make_message, maybe_auto_reply,
        )
        _plat = str((body or {}).get("platform") or "")
        _acct = str((body or {}).get("account_id") or "")
        _ck = str((body or {}).get("chat_key") or "")
        if not _ck:
            raise HTTPException(400, tr(request, "err.ws.field_required", field="chat_key"))
        # 空 account_id 回填护栏（2026-07 事故）：Baileys Node 断线后离线队列回放存在
        # accountId 竞态，push 可能带 account_id="" → 原样落库会裂出孤儿会话
        # whatsapp::<peer>（与正常 whatsapp:<acct>:<peer> 同客户两条线程），且
        # protocol_autoreply 因 payload 缺 account_id 直接放弃自动回复。
        # 注册表在册 whatsapp 账号恰好 1 个 → 归属无歧义，直接回填；0 个或多个 →
        # 无法判定归属，保持原行为照收（Node 推送无重试，拒收 4xx = 丢消息），只记
        # error 供人工合并线程。仅对 whatsapp 做——其它平台 account_id 语义不同
        # （telegram 有 default 账号语义），不扩大爆炸半径。
        if _plat == "whatsapp" and not _acct:
            _wa_rows: List[Dict[str, Any]] = []
            try:
                _wa_rows = get_account_registry().list(platform="whatsapp")
            except Exception:
                logger.debug("[protocol] ingest 回填读注册表失败", exc_info=True)
            if len(_wa_rows) == 1:
                _acct = str(_wa_rows[0].get("account_id") or "")
                # 回填必须同步写回 body：后续若有代码直接 body.get("account_id")
                # （而非局部 _acct），拿到的也是回填后的值——防「落库归对了、
                # 自动回复链还是空」的半修状态。
                body["account_id"] = _acct
                logger.warning(
                    "[protocol] whatsapp ingest 空 account_id 已按注册表唯一在册账号"
                    "回填 account=%s chat_key=%s（Node 离线回放 accountId 竞态）",
                    _acct, _ck)
            else:
                logger.error(
                    "[protocol] whatsapp ingest 空 account_id 且在册账号数=%d，"
                    "归属有歧义无法回填 → 照收进孤儿线程 chat_key=%s（需人工合并）",
                    len(_wa_rows), _ck)
        # P0：跨平台入站身份归一（号码/线程 id 补名 + WhatsApp 号码补进资料面板 + 分类观测）。
        # 来显名是真名→用之；否则用已同步通讯录名补齐（好友名单同步后显示联系人名而非号码）；
        # 仍取不到→回落裸 chat_key。WhatsApp 私聊 chat_key 即 E.164 裸号 → 顺带补 phone。
        _raw_name = str((body or {}).get("name") or "")
        direction = str((body or {}).get("direction") or "in")
        _chat_type = str((body or {}).get("chat_type") or "")
        _contact_name = ""
        if not _raw_name or _raw_name == _ck:
            try:
                _contact_name = store.get_protocol_contact_name(_plat, _acct, _ck) or ""
            except Exception:
                logger.debug("[protocol] 号码补名失败", exc_info=True)
        from src.integrations.protocol_bridge import enrich_ingest_identity
        _ident = enrich_ingest_identity(_plat, _ck, _raw_name, _chat_type, _contact_name)
        _name = _ident["display_name"]
        _phone = _ident["phone"]
        # 观测：仅私聊 peer 身份分类（named/backfilled/raw），量化各平台「仍是裸 id」比例
        if direction == "in" and _chat_type != "group":
            _record_ingest_identity(_plat, _ident["outcome"])
        _reply_to = (body or {}).get("reply_to")
        if not isinstance(_reply_to, dict):
            _reply_to = None
        # P4-11D：群提及明细 [{jid,number}]（Node 从 contextInfo.mentionedJid 归一）
        _mentions = (body or {}).get("mentions")
        if not isinstance(_mentions, list):
            _mentions = None
        # P4-11E：群发言人结构化字段（Node 从 participant/pushName 归一）
        _sender_id = str((body or {}).get("sender_id") or "")
        _sender_name = str((body or {}).get("sender_name") or "")
        _avatar_url = str((body or {}).get("avatar_url") or "")
        # 陌生人「消息请求」策略（Messenger 网页 poller 携带 is_request/request_category）：
        # 产品口径——「可能认识(general)」→ 进收件箱且**人设自动 AI 回**（仍受风险闸门约束，
        # 危机/高危自动降级人审）；「垃圾/不确定(非 general)」→ **只进收件箱、不自动回**。
        #
        # 关键：auto-draft(System Z) 在 ingest_incoming 内部即触发，且本部署全局默认档位未设
        # （→ review，不自动发）。故须在**落库前**按分级预置会话档位，抢在回调之前生效：
        #   general → auto_ai（否则只出人审草稿、不真发）；spam → manual（否则可能被自动回）。
        # 仅在坐席未显式设过档位时预置，尊重人工覆盖。
        _is_request = bool((body or {}).get("is_request"))
        _req_cat = str((body or {}).get("request_category") or "").lower()
        # general 才允许自动回；其余（spam/junk/other/未知）一律保守只入收件箱。
        _is_spam_request = _is_request and _req_cat != "general"
        if direction == "in" and _is_request:
            try:
                from src.inbox.normalizer import conv_id as _mk_conv_id
                _pre_cid = _mk_conv_id(_plat, _acct, _ck)
                if store.get_automation_mode_if_set(_pre_cid) is None:
                    store.set_automation_mode(
                        _pre_cid, "manual" if _is_spam_request else "auto_ai")
            except Exception:
                logger.debug("[protocol] 陌生人请求预置档位失败", exc_info=True)
        cid = ingest_incoming(
            store,
            platform=_plat,
            account_id=_acct,
            chat_key=_ck,
            name=_name,
            text=str((body or {}).get("text") or ""),
            ts=float((body or {}).get("ts") or 0),
            msg_id=str((body or {}).get("msg_id") or ""),
            direction=direction,
            media_type=str((body or {}).get("media_type") or ""),
            media_ref=str((body or {}).get("media_ref") or ""),
            chat_type=_chat_type,
            reply_to=_reply_to,
            phone=_phone,
            avatar_url=_avatar_url,
            mentioned=bool((body or {}).get("mentioned")),
            mentions=_mentions,
            sender_id=_sender_id,
            sender_name=_sender_name,
        )
        # 群聊不进自动回复（自动回复面向 1:1）。spam 类陌生人请求只入收件箱、不自动回。
        if direction == "in" and _chat_type != "group" and not _is_spam_request:
            await maybe_auto_reply(make_message(
                platform=_plat,
                account_id=_acct,
                chat_key=_ck,
                name=_name,
                text=str((body or {}).get("text") or ""),
                ts=float((body or {}).get("ts") or 0),
                msg_id=str((body or {}).get("msg_id") or ""),
                # 携带媒体字段：直发线据此识别图片/语音/视频（补全 [图片]/[语音] 占位），
                # 与全自动草稿链同口径。缺这两字段时纯媒体消息会被判 incomplete 早退。
                media_type=str((body or {}).get("media_type") or ""),
                media_ref=str((body or {}).get("media_ref") or ""),
            ))
        return {"ok": bool(cid), "conversation_id": cid or ""}

    @app.post("/api/internal/protocol/session-status")
    async def api_protocol_session_status(request: Request):
        """内部桥（P0-2 会话健康闭环）：外部 worker（messenger-web 等）主动 push 会话
        状态转移（authorized / needs_login / expired / logged_out / failed）。

        落 ``PlatformSessionHealth`` 登记表（→ workspace metrics / Prometheus / ops 卡），
        「进入不健康 / 恢复」两类转移经 EventBus 发 ``platform_session_alert``
        （告警渠道订阅别名 ``platform_session``；notifier 自带每小时限流去重）。
        """
        api_auth(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        plat = str((body or {}).get("platform") or "").lower()
        status = str((body or {}).get("status") or "").lower()
        acct = str((body or {}).get("account_id") or "")
        login_id = str((body or {}).get("login_id") or "")
        detail = str((body or {}).get("detail") or "")
        if not plat or not status:
            return {"ok": False, "reason": "missing_field"}
        # P1 身份化：authorized 推送可携带自身昵称/头像（WA Baileys: pushname/avatar_url、
        # messenger-web: 同名字段）→ 富集 registry meta.self_*。关键在覆盖「服务重启后
        # Node restoreAll 自动重连」的存量账号——它们不经登录轮询，此前永远拿不到真实身份。
        # Node 侧 postStatus 本就是 fire-and-forget（不 await 响应），此处 await 不拖慢对方；
        # flag 关/字段缺失时 enrich 内部 no-op，best-effort 绝不抛。
        if status == "authorized" and acct:
            _sp_name = str((body or {}).get("pushname")
                           or (body or {}).get("name") or "")
            _sp_avatar = str((body or {}).get("avatar_url") or "")
            if _sp_name or _sp_avatar:
                try:
                    from src.integrations.account_self_profile import (
                        enrich_from_fields,
                    )
                    _cfg = (config_manager.config
                            if config_manager is not None else {}) or {}
                    await enrich_from_fields(
                        plat, acct, name=_sp_name,
                        avatar_url=_sp_avatar, config=_cfg)
                except Exception:
                    logger.debug("[protocol] session-status self_profile 富集失败（忽略）",
                                 exc_info=True)
        from src.integrations.platform_session_health import (
            get_platform_session_health,
        )
        trans = get_platform_session_health().record(
            plat, acct or login_id, status, detail=detail, login_id=login_id)
        if trans.get("went_unhealthy") or trans.get("recovered"):
            try:
                from src.integrations.shared.event_bus import get_event_bus
                get_event_bus().publish("platform_session_alert", {
                    "platform": plat,
                    "account_id": acct or login_id,
                    "login_id": login_id,
                    "status": status,
                    "detail": detail,
                    "recovered": bool(trans.get("recovered")),
                    # notifier 限流判别符：多账号同小时先后掉线各自成键，
                    # 互不挤占每小时一条的窗口。
                    "rate_key": f"{plat}:{acct or login_id}",
                })
            except Exception:
                logger.debug("[protocol] 会话健康告警发布失败", exc_info=True)
        return {"ok": True, "changed": bool(trans.get("changed")),
                "went_unhealthy": bool(trans.get("went_unhealthy")),
                "recovered": bool(trans.get("recovered"))}

    @app.post("/api/internal/protocol/contacts")
    async def api_protocol_contacts(request: Request):
        """内部桥：worker 同步平台通讯录（好友名单）到收件箱 store。"""
        api_auth(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        store = getattr(request.app.state, "inbox_store", None)
        if store is None:
            raise HTTPException(503, tr(request, "err.svc.inbox_not_ready"))
        rows = (body or {}).get("contacts") or []
        n = 0
        try:
            n = store.upsert_protocol_contacts(
                str((body or {}).get("platform") or ""),
                str((body or {}).get("account_id") or ""),
                rows if isinstance(rows, list) else [],
            )
        except Exception:
            logger.debug("[protocol] 通讯录同步失败", exc_info=True)
        return {"ok": True, "count": n}

    @app.post("/api/internal/protocol/chats")
    async def api_protocol_chats(request: Request):
        """内部桥：worker 同步平台会话列表 → 建会话占位（全量会话可见）。"""
        api_auth(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        store = getattr(request.app.state, "inbox_store", None)
        if store is None:
            raise HTTPException(503, tr(request, "err.svc.inbox_not_ready"))
        rows = (body or {}).get("chats") or []
        n = 0
        try:
            n = store.upsert_protocol_chats(
                str((body or {}).get("platform") or ""),
                str((body or {}).get("account_id") or ""),
                rows if isinstance(rows, list) else [],
            )
        except Exception:
            logger.debug("[protocol] 会话列表同步失败", exc_info=True)
        return {"ok": True, "count": n}

    @app.post("/api/internal/protocol/reaction")
    async def api_protocol_reaction(request: Request):
        """内部桥（P4-3）：worker 上报表情回应 → 按目标 wamid 挂到 messages。

        body: {platform, account_id, chat_key, target_id, emoji, sender}
        emoji 为空=撤销反应。目标消息未落库则静默忽略（best-effort，不建空消息）。
        """
        api_auth(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        store = getattr(request.app.state, "inbox_store", None)
        if store is None:
            raise HTTPException(503, tr(request, "err.svc.inbox_not_ready"))
        plat = str((body or {}).get("platform") or "").lower()
        acct = str((body or {}).get("account_id") or "")
        ck = str((body or {}).get("chat_key") or "")
        target_id = str((body or {}).get("target_id") or "")
        emoji = str((body or {}).get("emoji") or "")
        sender = str((body or {}).get("sender") or "me")
        if not plat or not ck or not target_id:
            return {"ok": False, "reason": "missing_field"}
        ok = False
        cid = f"{plat}:{acct}:{ck}"
        try:
            ok = store.set_reaction(cid, target_id, sender, emoji)
        except Exception:
            logger.debug("[protocol] 表情回应落库失败", exc_info=True)
        deferred_id = 0
        if ok and emoji:
            try:
                from pathlib import Path
                from src.companion.reaction_followup import schedule_reaction_followup
                cm = getattr(request.app.state, "config_manager", None)
                cfg = (cm.config if cm is not None else {}) or {}
                cfg_dir = Path(getattr(cm, "config_path", "config/config.yaml")).parent
                dispatcher = getattr(
                    request.app.state, "deferred_outbox_dispatcher", None,
                )
                deferred_id = schedule_reaction_followup(
                    store=store,
                    deferred_dispatcher=dispatcher,
                    config=cfg,
                    config_dir=cfg_dir,
                    platform=plat,
                    account_id=acct,
                    chat_key=ck,
                    target_id=target_id,
                    emoji=emoji,
                    sender=sender,
                    chat_type=str((body or {}).get("chat_type") or ""),
                )
            except Exception:
                logger.debug("[protocol] reaction 跟进调度失败", exc_info=True)
        return {"ok": bool(ok), "deferred_id": int(deferred_id or 0)}

    @app.post("/api/internal/protocol/receipt")
    async def api_protocol_receipt(request: Request):
        """内部桥（P4-4）：worker 上报出站消息投递状态 → 单调升级 messages.status。

        body: {platform, account_id, chat_key, target_id, status}（status∈sent/delivered/read）
        目标消息未落库则静默忽略（best-effort）。
        """
        api_auth(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        store = getattr(request.app.state, "inbox_store", None)
        if store is None:
            raise HTTPException(503, tr(request, "err.svc.inbox_not_ready"))
        plat = str((body or {}).get("platform") or "").lower()
        acct = str((body or {}).get("account_id") or "")
        ck = str((body or {}).get("chat_key") or "")
        target_id = str((body or {}).get("target_id") or "")
        status = str((body or {}).get("status") or "")
        if not plat or not ck or not target_id or not status:
            return {"ok": False, "reason": "missing_field"}
        ok = False
        try:
            ok = store.set_message_status(f"{plat}:{acct}:{ck}", target_id, status)
        except Exception:
            logger.debug("[protocol] 回执落库失败", exc_info=True)
        return {"ok": bool(ok)}

    @app.post("/api/internal/protocol/message-op")
    async def api_protocol_message_op(request: Request):
        """内部桥（P4-6A）：worker 上报消息编辑/撤回 → 改写线程内消息状态。

        body: {platform, account_id, chat_key, target_id, op, text?}
        op=revoke → 标撤回（气泡置灰）；op=edit → 正文改写 + 标「已编辑」（text 为新正文）。
        成功后广播 SSE ``message_op`` 让当前会话前端即时重载线程。目标未落库 → 静默忽略。
        """
        api_auth(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        store = getattr(request.app.state, "inbox_store", None)
        if store is None:
            raise HTTPException(503, tr(request, "err.svc.inbox_not_ready"))
        plat = str((body or {}).get("platform") or "").lower()
        acct = str((body or {}).get("account_id") or "")
        ck = str((body or {}).get("chat_key") or "")
        target_id = str((body or {}).get("target_id") or "")
        op = str((body or {}).get("op") or "").lower()
        if not plat or not ck or not target_id or op not in ("revoke", "edit"):
            return {"ok": False, "reason": "bad_request"}
        cid = f"{plat}:{acct}:{ck}"
        ok = False
        try:
            if op == "revoke":
                ok = store.mark_message_revoked(cid, target_id)
            else:
                ok = store.apply_message_edit(
                    cid, target_id, str((body or {}).get("text") or ""))
        except Exception:
            logger.debug("[protocol] 消息编辑/撤回落库失败", exc_info=True)
        if ok:
            try:
                from src.integrations.shared.event_bus import get_event_bus
                get_event_bus().publish("message_op", {
                    "conversation_id": cid, "platform": plat, "account_id": acct,
                    "chat_key": ck, "op": op, "target_id": target_id,
                })
            except Exception:
                logger.debug("[protocol] message_op 事件发布失败", exc_info=True)
        return {"ok": bool(ok)}

    @app.post("/api/internal/protocol/presence")
    async def api_protocol_presence(request: Request):
        """内部桥（P4-5A）：worker 上报对端在线/输入状态 → 经事件总线广播 peer_typing。

        body: {platform, account_id, chat_key, state}（state∈composing/recording/paused/
        available/unavailable）。**纯瞬态**：不落库，只发 SSE 事件，前端按当前会话匹配显示
        「对方正在输入…」。composing/recording=正在输入，其余=停止。
        """
        api_auth(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        plat = str((body or {}).get("platform") or "").lower()
        acct = str((body or {}).get("account_id") or "")
        ck = str((body or {}).get("chat_key") or "")
        state = str((body or {}).get("state") or "").lower()
        if not plat or not ck or not state:
            return {"ok": False, "reason": "missing_field"}
        typing = state in ("composing", "recording")
        try:
            from src.integrations.shared.event_bus import get_event_bus
            get_event_bus().publish("peer_typing", {
                "conversation_id": f"{plat}:{acct}:{ck}",
                "platform": plat, "account_id": acct, "chat_key": ck,
                "typing": typing, "recording": state == "recording",
                "state": state, "ts": time.time(),
            })
        except Exception:
            logger.debug("[protocol] peer_typing 事件发布失败", exc_info=True)
        return {"ok": True}

    @app.post("/api/platforms/{platform}/{account_id}/subscribe-presence")
    async def api_platform_subscribe_presence(
        platform: str, account_id: str, request: Request,
    ):
        """对端在线/输入状态订阅（P4-5A）：打开会话时让 Baileys `presenceSubscribe(jid)`，
        之后对端 typing 才会经 presence.update 回流。仅私聊（群 presence 无意义）。

        仅 WhatsApp（presence 是 Baileys 能力）。best-effort，失败静默。
        """
        api_auth(request)
        platform = str(platform or "").lower()
        if platform != "whatsapp":
            return {"ok": False, "reason": "unsupported_platform"}
        try:
            body = await request.json()
        except Exception:
            body = {}
        chat_key = str((body or {}).get("chat_key") or "").strip()
        if not chat_key:
            return {"ok": False, "reason": "missing_field"}
        if str((body or {}).get("chat_type") or "").lower() == "group":
            return {"ok": False, "reason": "group_unsupported"}
        cfg = (config_manager.config if config_manager is not None else {}) or {}
        from src.integrations.whatsapp_baileys_login import (
            protocol_enabled as wa_enabled, service_base_url, _post_json,
        )
        if not wa_enabled(cfg):
            return {"ok": False, "reason": "protocol_disabled"}
        try:
            res = await _post_json(
                f"{service_base_url(cfg)}/accounts/{account_id}/subscribe-presence",
                {"jid": f"{chat_key}@s.whatsapp.net"})
        except Exception:
            logger.debug("[protocol] presenceSubscribe 触发失败", exc_info=True)
            return {"ok": False, "reason": "service_error"}
        return {"ok": bool((res or {}).get("ok", False))}

    @app.post("/api/platforms/{platform}/{account_id}/react")
    async def api_platform_react(platform: str, account_id: str, request: Request):
        """坐席给某条消息发表情回应（P4-5B，P4-3 接收侧的双向补全）。

        body: {chat_key, target_id, emoji, from_me?, participant?, chat_type?}
        （emoji 空串=撤销）。发到 WhatsApp 后本地即以 sender='me' 落库，气泡即时显 chip。
        仅 WhatsApp 协议号。best-effort。
        """
        api_auth(request)
        platform = str(platform or "").lower()
        if platform != "whatsapp":
            return {"ok": False, "reason": "unsupported_platform"}
        try:
            body = await request.json()
        except Exception:
            body = {}
        chat_key = str((body or {}).get("chat_key") or "").strip()
        target_id = str((body or {}).get("target_id") or "").strip()
        if not chat_key or not target_id:
            return {"ok": False, "reason": "missing_field"}
        emoji = str((body or {}).get("emoji") or "")
        is_group = str((body or {}).get("chat_type") or "").lower() == "group"
        jid_suffix = "@g.us" if is_group else "@s.whatsapp.net"
        cfg = (config_manager.config if config_manager is not None else {}) or {}
        from src.integrations.whatsapp_baileys_login import (
            protocol_enabled as wa_enabled, service_base_url, _post_json,
        )
        if not wa_enabled(cfg):
            return {"ok": False, "reason": "protocol_disabled"}
        try:
            res = await _post_json(
                f"{service_base_url(cfg)}/accounts/{account_id}/react",
                {"jid": f"{chat_key}{jid_suffix}", "target_id": target_id,
                 "emoji": emoji, "from_me": bool((body or {}).get("from_me")),
                 "participant": str((body or {}).get("participant") or "")})
        except Exception:
            logger.debug("[protocol] 发表情回应失败", exc_info=True)
            return {"ok": False, "reason": "service_error"}
        ok = bool((res or {}).get("ok", False))
        if ok:
            store = getattr(request.app.state, "inbox_store", None)
            if store is not None:
                try:
                    store.set_reaction(
                        f"{platform}:{account_id}:{chat_key}", target_id, "me", emoji)
                except Exception:
                    logger.debug("[protocol] 本地表情回应落库失败", exc_info=True)
        return {"ok": ok}

    @app.post("/api/platforms/{platform}/{account_id}/message-op")
    async def api_platform_message_op(platform: str, account_id: str, request: Request):
        """坐席主动编辑/撤回自己发出的消息（P4-6B，P4-6A 接收侧的双向补全）。

        body: {chat_key, target_id, op, text?, chat_type?}（op∈revoke/edit，edit 需 text）。
        发到 WhatsApp 成功后本地即改写线程 + 广播 SSE message_op（多坐席同步）。仅 WhatsApp。
        """
        api_auth(request)
        platform = str(platform or "").lower()
        if platform != "whatsapp":
            return {"ok": False, "reason": "unsupported_platform"}
        try:
            body = await request.json()
        except Exception:
            body = {}
        chat_key = str((body or {}).get("chat_key") or "").strip()
        target_id = str((body or {}).get("target_id") or "").strip()
        op = str((body or {}).get("op") or "").lower()
        text = str((body or {}).get("text") or "")
        if not chat_key or not target_id or op not in ("revoke", "edit"):
            return {"ok": False, "reason": "bad_request"}
        if op == "edit" and not text.strip():
            return {"ok": False, "reason": "empty_text"}
        is_group = str((body or {}).get("chat_type") or "").lower() == "group"
        jid_suffix = "@g.us" if is_group else "@s.whatsapp.net"
        cfg = (config_manager.config if config_manager is not None else {}) or {}
        from src.integrations.whatsapp_baileys_login import (
            protocol_enabled as wa_enabled, service_base_url, _post_json,
        )
        if not wa_enabled(cfg):
            return {"ok": False, "reason": "protocol_disabled"}
        try:
            res = await _post_json(
                f"{service_base_url(cfg)}/accounts/{account_id}/message-op",
                {"jid": f"{chat_key}{jid_suffix}", "target_id": target_id,
                 "op": op, "text": text})
        except Exception:
            logger.debug("[protocol] 主动编辑/撤回失败", exc_info=True)
            return {"ok": False, "reason": "service_error"}
        ok = bool((res or {}).get("ok", False))
        if ok:
            cid = f"{platform}:{account_id}:{chat_key}"
            store = getattr(request.app.state, "inbox_store", None)
            if store is not None:
                try:
                    if op == "revoke":
                        store.mark_message_revoked(cid, target_id)
                    else:
                        store.apply_message_edit(cid, target_id, text)
                except Exception:
                    logger.debug("[protocol] 主动编辑/撤回本地落库失败", exc_info=True)
            try:
                from src.integrations.shared.event_bus import get_event_bus
                get_event_bus().publish("message_op", {
                    "conversation_id": cid, "platform": platform,
                    "account_id": account_id, "chat_key": chat_key,
                    "op": op, "target_id": target_id,
                })
            except Exception:
                logger.debug("[protocol] message_op 事件发布失败", exc_info=True)
        return {"ok": ok}

    @app.get("/api/platforms/{platform}/{account_id}/group-members")
    async def api_platform_group_members(platform: str, account_id: str, request: Request):
        """群成员名单（P4-11，供收件箱 @提及选人面板）。

        query: chat_key=群号(digits)。仅 WhatsApp 协议号；成员名优先用已同步通讯录名，
        否则回落号码。best-effort——取不到（未在线/非群/无权限）返回空 members。
        """
        api_auth(request)
        platform = str(platform or "").lower()
        if platform != "whatsapp":
            return {"ok": False, "reason": "unsupported_platform", "members": []}
        chat_key = str(request.query_params.get("chat_key") or "").strip()
        if not chat_key:
            return {"ok": False, "reason": "missing_chat_key", "members": []}
        cfg = (config_manager.config if config_manager is not None else {}) or {}
        from src.integrations.whatsapp_baileys_login import (
            protocol_enabled as wa_enabled, service_base_url, _get_json,
        )
        if not wa_enabled(cfg):
            return {"ok": False, "reason": "protocol_disabled", "members": []}
        try:
            res = await _get_json(
                f"{service_base_url(cfg)}/accounts/{account_id}/group-members"
                f"?jid={chat_key}")
        except Exception:
            logger.debug("[protocol] 取群成员失败", exc_info=True)
            return {"ok": False, "reason": "service_error", "members": []}
        raw = (res or {}).get("members") or []
        store = getattr(request.app.state, "inbox_store", None)
        members: List[Dict[str, Any]] = []
        for m in raw:
            num = str((m or {}).get("number") or "").strip()
            if not num:
                continue
            name = ""
            if store is not None:
                try:
                    name = store.get_protocol_contact_name(platform, account_id, num)
                except Exception:
                    name = ""
            members.append({"jid": str((m or {}).get("jid") or ""),
                            "number": num, "name": name or num,
                            "admin": str((m or {}).get("admin") or "")})
        return {"ok": bool((res or {}).get("ok", False)),
                "subject": str((res or {}).get("subject") or ""), "members": members}

    @app.get("/api/platforms/{platform}/{account_id}/contacts")
    async def api_platform_contacts(platform: str, account_id: str, request: Request):
        """好友名单：读取某协议账号已同步的通讯录（供前端「通讯录」视图）。

        query:
        - ``limit``：条数上限（store 侧封顶 5000）；
        - ``enriched=1``：改走「联系人 + 会话状态」联表，每行补
          ``has_conversation/last_ts/unread/never_spoke``，并额外返回 ``summary``
          资产盘点段（未开口 / 沉默 / 已有会话）。**默认 0 = 旧形状逐字段不变**
          （现有前端仍在消费旧形状，向后兼容是硬约束）；
        - ``only``：仅 enriched 有效，``""``/``never_spoke``/``silent``/``chat_only``
          档位筛选（``chat_only``＝有往来但没存进通讯录，仅并集口径下有内容）；
        - ``q``：仅 enriched 有效，服务端按 名字/备注名/号码 模糊匹配（不传=全量，
          旧「全量拉下来本地筛」的调用方零改动）；
        - ``include_chats``：仅 enriched 有效，**默认开**。取数口径从「通讯录表」
          升级为「人的并集」＝通讯录 ∪ 会话里的私聊 peer。为什么默认开：Telegram
          的 ``get_contacts()`` 只含「我主动保存过的联系人」，线上实测三个生产 TG
          账号同步全部成功而通讯录=0、同账号却有 62 条真实往来会话——默认关等于
          「联系人」面板对 Telegram 恒空，坐席按名字找昨天聊过的客户永远找不到。
          需要纯通讯录口径的消费方显式传 ``include_chats=0``。
          已知的纯名单口径消费方：ops-overview「客户资产（目录同步）」卡
          （``enriched=1&limit=1&include_chats=0``）——它问的是「已同步的**好友名单**
          里有多少沉睡线索」，并集会把分母扩到全部往来的人（只有会话的人天然算
          「开过口」）使「未开口占比」语义漂移，故显式关。改动该卡时勿丢这个参数。
        """
        api_auth(request)
        store = getattr(request.app.state, "inbox_store", None)
        if store is None:
            raise HTTPException(503, tr(request, "err.svc.inbox_not_ready"))
        try:
            limit = int(request.query_params.get("limit") or 1000)
        except (TypeError, ValueError):
            limit = 1000
        if str(request.query_params.get("enriched") or "") not in ("1", "true", "yes"):
            contacts = store.list_protocol_contacts(platform, account_id, limit=limit)
            return {"ok": True, "platform": platform, "account_id": account_id,
                    "count": len(contacts), "contacts": contacts}
        only = str(request.query_params.get("only") or "")
        q = str(request.query_params.get("q") or "")
        # 缺省=开（本轮要交付的用户价值）；显式传值时与 enriched 同一套真值口径
        _inc_raw = request.query_params.get("include_chats")
        include_chats = (True if _inc_raw is None or _inc_raw == ""
                         else str(_inc_raw) in ("1", "true", "yes"))
        contacts = store.list_protocol_contacts_enriched(
            platform, account_id, limit=limit, only=only, query=q,
            include_chats=include_chats)
        summary = store.protocol_contacts_summary(
            platform, account_id, query=q, include_chats=include_chats)
        return {
            "ok": True, "platform": platform, "account_id": account_id,
            "count": len(contacts),
            # 回显生效口径：前端据此如实标注「通讯录 / 通讯录+会话」，
            # 避免用户把并集基数当成通讯录同步成果去判断同步有没有跑通
            "include_chats": include_chats,
            # 汇总跟随 q 但不跟随 only（档位是视图筛选，盘点数字要给全景基数）；
            # silent_30d 的 30 天口径见 store.PROTOCOL_CONTACT_SILENT_DAYS
            "summary": {
                "total": summary["total"],
                "never_spoke": summary["never_spoke"],
                "silent_30d": summary["silent"],
                "with_conversation": summary["with_conversation"],
                "in_book": summary["in_book"],
                "chat_only": summary["chat_only"],
            },
            "contacts": contacts,
        }

    @app.post("/api/platforms/{platform}/{account_id}/contacts/refresh")
    async def api_platform_contacts_refresh(platform: str, account_id: str,
                                            request: Request):
        """手动触发该账号的「目录重新同步」（好友名单 + 会话占位）。

        供工作台通讯录面板的「立即同步」按钮用。异步触发、立即返回——目录同步走
        平台 RPC 可能耗时数十秒，同步等待会把 web 请求挂死。

        返回 ``{"ok": True, "started": True, ...}``，或 ``ok=False`` + ``reason``：
        ``unsupported_platform`` / ``protocol_disabled`` / ``account_offline`` /
        ``service_error`` / ``cooldown``（附 ``retry_after_sec``）。
        """
        api_auth(request)
        platform = str(platform or "").lower()
        if platform not in ("telegram", "whatsapp"):
            return {"ok": False, "reason": "unsupported_platform"}
        left = _contacts_refresh_cooldown_left(platform, account_id)
        if left > 0:
            return {"ok": False, "reason": "cooldown",
                    "retry_after_sec": int(left) + 1}
        if platform == "telegram":
            # 软依赖：目录同步模块由并行工作线交付，此刻可能还不存在。import 失败
            # 只让本端点降级成 unsupported，绝不能在模块顶层 import 把整个 app 带崩。
            try:
                from src.integrations.telegram_directory_sync import (
                    sync_directory_once,
                )
            except ImportError:
                return {"ok": False, "reason": "unsupported_platform"}
            pyro = _get_tg_pyro_for_account(request.app, account_id)
            loop = getattr(pyro, "loop", None)
            if pyro is None or loop is None or not loop.is_running():
                return {"ok": False, "reason": "account_offline"}
            cfg = (config_manager.config if config_manager is not None else {}) or {}

            async def _run() -> None:
                try:
                    await sync_directory_once(pyro, account_id, cfg)
                except Exception:
                    logger.debug("[protocol] telegram 目录同步失败 acct=%s",
                                 account_id, exc_info=True)

            _contacts_refresh_mark(platform, account_id)
            asyncio.run_coroutine_threadsafe(_run(), loop)
            return {"ok": True, "started": True, "platform": platform,
                    "account_id": account_id}
        # WhatsApp：复用 Baileys 既有 /accounts/:id/resync（resyncAppState →
        # contacts.upsert 回流 → 内部桥 /api/internal/protocol/contacts 落库）
        cfg = (config_manager.config if config_manager is not None else {}) or {}
        from src.integrations.whatsapp_baileys_login import (
            protocol_enabled as wa_enabled, service_base_url, _post_json,
        )
        if not wa_enabled(cfg):
            return {"ok": False, "reason": "protocol_disabled"}
        try:
            res = await _post_json(
                f"{service_base_url(cfg)}/accounts/{account_id}/resync", {})
        except Exception as exc:
            # Node 对「账号未连接」回 404 —— 语义是号离线，不是服务坏了，区分开
            status = getattr(getattr(exc, "response", None), "status_code", 0)
            if status == 404:
                return {"ok": False, "reason": "account_offline"}
            logger.debug("[protocol] whatsapp 通讯录重同步触发失败", exc_info=True)
            return {"ok": False, "reason": "service_error"}
        if not bool((res or {}).get("ok", False)):
            return {"ok": False, "reason": "service_error"}
        _contacts_refresh_mark(platform, account_id)
        return {"ok": True, "started": True, "platform": platform,
                "account_id": account_id}

    @app.post("/api/platforms/{platform}/{account_id}/history")
    async def api_platform_history(platform: str, account_id: str, request: Request):
        """按需拉更早历史（P1）：以 store 里最旧的带平台消息 id 的消息为锚点向上补拉。

        - WhatsApp 协议号：请求 Baileys fetchMessageHistory 向手机补拉，消息异步经
          messaging-history.set 回流落库，前端稍后刷新即见；
        - Telegram（协议号/主账号）：直接经 pyrogram get_chat_history 从云端拉取
          （云历史与手机所见同源），同步落库并返回 inserted 条数。
        best-effort。
        """
        api_auth(request)
        platform = str(platform or "").lower()
        if platform not in ("whatsapp", "telegram"):
            return {"ok": False, "reason": "unsupported_platform"}
        store = getattr(request.app.state, "inbox_store", None)
        if store is None:
            raise HTTPException(503, tr(request, "err.svc.inbox_not_ready"))
        try:
            body = await request.json()
        except Exception:
            body = {}
        chat_key = str((body or {}).get("chat_key") or "").strip()
        if not chat_key:
            raise HTTPException(400, tr(request, "err.ws.field_required", field="chat_key"))
        try:
            count = int((body or {}).get("count") or 50)
        except (TypeError, ValueError):
            count = 50
        count = max(1, min(200, count))
        cid = f"{platform}:{account_id}:{chat_key}"
        anchor = store.get_oldest_message(cid)
        if platform == "telegram":
            # 云端历史直拉：锚点有 MTProto 消息 id 则取更早（offset_id），否则从最新拉
            import asyncio
            from src.inbox.ingest import ingest_thread
            from src.integrations.protocol_bridge import collect_tg_dialog_history
            if _tg_client_cooling(account_id):
                return {"ok": False, "reason": "client_cooling"}
            pyro = _get_tg_pyro_for_account(request.app, account_id)
            loop = getattr(pyro, "loop", None)
            if pyro is None or loop is None or not loop.is_running():
                return {"ok": False, "reason": "client_unavailable"}
            offset_id = 0
            if anchor:
                try:
                    offset_id = int(str(anchor.get("platform_msg_id") or "0"))
                except (TypeError, ValueError):
                    offset_id = 0
            fut = asyncio.run_coroutine_threadsafe(
                collect_tg_dialog_history(
                    pyro, account_id, chat_key, limit=count, offset_id=offset_id),
                loop)
            try:
                res = await asyncio.wait_for(asyncio.wrap_future(fut), timeout=30.0)
            except (TimeoutError, asyncio.TimeoutError):
                _mark_tg_client_bad(account_id)
                return {"ok": False, "reason": "timeout"}
            except Exception:
                logger.debug("[protocol] telegram 历史拉取失败", exc_info=True)
                return {"ok": False, "reason": "service_error"}
            if not res:
                return {"ok": False, "reason": "no_messages", "requested": count}
            chat, msgs = res
            row = store.get_conversation(cid)
            if row is not None:
                # 已有会话：未读数保持现值（拉的是旧消息，不应清/改未读）
                chat["unread"] = int(row.get("unread") or 0)
            inserted = ingest_thread(store, chat, msgs)
            return {"ok": True, "requested": count, "inserted": int(inserted)}
        cfg = (config_manager.config if config_manager is not None else {}) or {}
        from src.integrations.whatsapp_baileys_login import (
            protocol_enabled as wa_enabled, service_base_url, _post_json,
        )
        if not wa_enabled(cfg):
            return {"ok": False, "reason": "protocol_disabled"}
        if not anchor or not str(anchor.get("platform_msg_id") or ""):
            # 无锚点（占位会话：会话列表同步来的、store 里零消息）。此前直接返回
            # no_anchor → 用户永远无法给空会话补拉历史。现在改为发「空 oldest_id」
            # 请求：Node 侧用它缓存的「该会话最新消息 key」（lastMsgKeys）兜底当锚点，
            # 等价于"从最新往前拉 count 条"。Node 明确回 no_cached_anchor（连缓存
            # 也没有：Node 刚重启/从未见过该会话消息）才向前端返回 no_history_available。
            payload = {
                "jid": f"{chat_key}@s.whatsapp.net",
                "count": count,
                "oldest_id": "",  # 空 → Node 回落用缓存的 lastMsgKey
                "oldest_ts": 0,
                "from_me": False,
            }
            try:
                res = await _post_json(
                    f"{service_base_url(cfg)}/accounts/{account_id}/history", payload)
            except Exception:
                # best-effort：兜底路径任何异常都不上抛，前端得到明确的失败原因即可
                logger.debug("[protocol] 占位会话兜底拉历史失败", exc_info=True)
                return {"ok": False, "reason": "service_error"}
            if bool((res or {}).get("ok", False)):
                return {"ok": True, "requested": count}
            if str((res or {}).get("error") or "") == "no_cached_anchor":
                return {"ok": False, "reason": "no_history_available"}
            return {"ok": False, "reason": "service_error"}
        payload = {
            "jid": f"{chat_key}@s.whatsapp.net",
            "count": count,
            "oldest_id": str(anchor.get("platform_msg_id") or ""),
            "oldest_ts": int(float(anchor.get("ts") or 0)),
            "from_me": str(anchor.get("direction") or "in") == "out",
        }
        try:
            res = await _post_json(
                f"{service_base_url(cfg)}/accounts/{account_id}/history", payload)
        except Exception:
            logger.debug("[protocol] fetchMessageHistory 触发失败", exc_info=True)
            return {"ok": False, "reason": "service_error"}
        return {"ok": bool((res or {}).get("ok", False)), "requested": count}

    @app.get("/api/platforms/telegram/{account_id}/sync-history")
    async def api_telegram_sync_history_status(account_id: str, request: Request):
        """Telegram 账号级历史同步的进度快照（前端轮询）。"""
        api_auth(request)
        return {"ok": True, "account_id": account_id,
                **tg_history_sync_snapshot(account_id)}

    @app.post("/api/platforms/telegram/{account_id}/sync-history")
    async def api_telegram_sync_history(account_id: str, request: Request):
        """Telegram 账号级聊天记录同步（对齐手机）。

        Telegram 云聊天历史存在其服务器上——用该账号 session 拉 get_dialogs +
        get_chat_history 得到的就是手机上的同一份数据（端到端加密的密聊除外）。
        本端点触发后台同步：遍历最近 ``dialogs`` 个云端会话、每会话拉近期
        ``per_chat`` 条消息直写 store（不触发 SSE/auto-draft/自动回复；媒体不下载、
        以占位文本入库）。同步跑在 pyrogram 自身 loop，本端点立即返回，进度经
        GET 同路径轮询。同账号并发触发 → already_running。
        """
        api_auth(request)
        store = getattr(request.app.state, "inbox_store", None)
        if store is None:
            raise HTTPException(503, tr(request, "err.svc.inbox_not_ready"))
        try:
            body = await request.json()
        except Exception:
            body = {}
        try:
            dialogs_limit = int((body or {}).get("dialogs") or 100)
        except (TypeError, ValueError):
            dialogs_limit = 100
        dialogs_limit = max(1, min(500, dialogs_limit))
        try:
            per_chat = int((body or {}).get("per_chat") or 30)
        except (TypeError, ValueError):
            per_chat = 30
        per_chat = max(1, min(100, per_chat))
        if _tg_client_cooling(account_id):
            return {"ok": False, "reason": "client_cooling"}
        pyro = _get_tg_pyro_for_account(request.app, account_id)
        loop = getattr(pyro, "loop", None)
        if pyro is None or loop is None or not loop.is_running():
            return {"ok": False, "reason": "client_unavailable"}
        if not _tg_hist_try_start(account_id):
            return {"ok": True, "already_running": True,
                    **tg_history_sync_snapshot(account_id)}
        import asyncio
        from src.inbox.ingest import ingest_thread
        from src.integrations.protocol_bridge import sync_telegram_history

        def _ingest(chat: Dict[str, Any], msgs: List[Dict[str, Any]]) -> int:
            return ingest_thread(store, chat, msgs)

        def _progress(done: int, total: int, messages: int) -> None:
            _tg_hist_update(account_id, dialogs_done=done, dialogs_total=total,
                            messages=messages)

        async def _run() -> None:
            try:
                stats = await sync_telegram_history(
                    pyro, account_id, dialogs_limit=dialogs_limit,
                    per_chat=per_chat, ingest=_ingest, progress=_progress)
                finished = time.time()
                dialogs_n = int(stats.get("dialogs") or 0)
                messages_n = int(stats.get("messages") or 0)
                _tg_hist_update(
                    account_id, state="done", finished_at=finished,
                    dialogs_done=dialogs_n, messages=messages_n)
                _persist_tg_history_sync(
                    account_id, finished_at=finished,
                    dialogs=dialogs_n, messages=messages_n)
            except Exception as exc:
                logger.debug("[protocol] telegram 历史同步失败", exc_info=True)
                _tg_hist_update(account_id, state="error",
                                error=str(exc)[:200], finished_at=time.time())

        asyncio.run_coroutine_threadsafe(_run(), loop)
        return {"ok": True, "started": True,
                **tg_history_sync_snapshot(account_id)}

    @app.post("/api/platforms/{platform}/{account_id}/sync-groups")
    async def api_platform_sync_groups(platform: str, account_id: str, request: Request):
        """老号会话列表回填（P3）：让 Baileys 用 groupFetchAllParticipating 把「所在群」
        补成 群组动态 占位。重连时 Node 已自动做一次；此端点供 UI 手动刷新（新入群后）。

        仅 WhatsApp（群批量拉取是 Baileys 能力）。best-effort。
        """
        api_auth(request)
        platform = str(platform or "").lower()
        if platform != "whatsapp":
            return {"ok": False, "reason": "unsupported_platform"}
        cfg = (config_manager.config if config_manager is not None else {}) or {}
        from src.integrations.whatsapp_baileys_login import (
            protocol_enabled as wa_enabled, service_base_url, _post_json,
        )
        if not wa_enabled(cfg):
            return {"ok": False, "reason": "protocol_disabled"}
        try:
            res = await _post_json(
                f"{service_base_url(cfg)}/accounts/{account_id}/sync-groups", {})
        except Exception:
            logger.debug("[protocol] 群会话回填触发失败", exc_info=True)
            return {"ok": False, "reason": "service_error"}
        return {"ok": bool((res or {}).get("ok", False)),
                "groups": int((res or {}).get("groups") or 0)}

    async def _telegram_peer_avatar(request: Request, account_id: str,
                                    chat_key: str):
        """下载 Telegram 对方头像并缓存到 static（7 天缓存 + .none 负缓存 1 天）。

        web 后台跑在独立线程/事件循环，pyrogram client 跑在主循环——不能跨 loop 直接
        await；故经 pyrogram client 自身的 loop 用 run_coroutine_threadsafe 调度下载。
        无头像/取不到 → 404（前端 <img onerror> 回落首字母渐变头像）。
        """
        from fastapi.responses import RedirectResponse
        import asyncio
        import os
        from src.integrations.protocol_bridge import protocol_media_root
        safe_acct = "".join(c for c in str(account_id) if c.isalnum() or c in "_-")
        safe_key = "".join(c for c in str(chat_key) if c.isalnum() or c in "_-")
        adir = protocol_media_root() / "telegram" / "avatars"
        adir.mkdir(parents=True, exist_ok=True)
        jpg = adir / f"{safe_acct}_{safe_key}.jpg"
        none_marker = adir / f"{safe_acct}_{safe_key}.none"
        url_path = f"/static/protocol_media/telegram/avatars/{safe_acct}_{safe_key}.jpg"
        now = time.time()
        if jpg.exists() and (now - jpg.stat().st_mtime) < 7 * 86400:
            _record_avatar("telegram", "cache_hit")
            return RedirectResponse(url_path, status_code=302)
        if none_marker.exists() and (now - none_marker.stat().st_mtime) < 86400:
            _record_avatar("telegram", "neg_hit")
            raise HTTPException(404, "no avatar")
        if _tg_client_cooling(account_id):
            # 断网冷却：不发 RPC 秒失败（不写 .none 负缓存——网络恢复后重渲染即重试）
            _record_avatar("telegram", "error")
            raise HTTPException(404, "no avatar")
        pyro = _get_tg_pyro_for_account(request.app, account_id)   # 多账号：受管 worker 优先
        loop = getattr(pyro, "loop", None)
        if pyro is None or loop is None or not loop.is_running():
            _record_avatar("telegram", "error")     # TG client 离线/无路由 → 取不到图
            raise HTTPException(404, "no avatar")
        try:
            peer: Any = int(chat_key)
        except (TypeError, ValueError):
            peer = chat_key
        dest = str(jpg.resolve())
        ident_holder: Dict[str, Any] = {}   # _dl 顺带把 get_chat 的身份放这，供机会式补名

        async def _dl() -> str:
            chat = await pyro.get_chat(peer)
            try:
                from src.integrations.protocol_bridge import tg_peer_identity
                ident_holder.update(tg_peer_identity(chat))
            except Exception:
                pass
            photo = getattr(chat, "photo", None)
            fid = None
            if photo is not None:
                fid = (getattr(photo, "big_file_id", None)
                       or getattr(photo, "small_file_id", None))
            if not fid:
                return ""
            saved = await pyro.download_media(fid, file_name=dest)
            return str(saved or "")

        try:
            fut = asyncio.run_coroutine_threadsafe(_dl(), loop)
            # 非阻塞等待：wrap_future + await——此前 fut.result(timeout=15) 会把 web 事件循环
            # 冻结最长 15s（pyrogram 断网重连时 get_chat 挂死 → 全站请求排队 → 前端 /thread
            # 15s 超时红屏）。改 await 后等待期间循环照常服务其他请求。
            saved = await asyncio.wait_for(
                asyncio.wrap_future(fut), timeout=_TG_FETCH_TIMEOUT_SEC)
        except (TimeoutError, asyncio.TimeoutError):
            # 超时=断网重连中 → 开账号级冷却（与 resolve 共用），后续懒加载头像秒失败
            _mark_tg_client_bad(account_id)
            _record_avatar("telegram", "error")
            logger.debug("[protocol] telegram 头像下载超时（断网冷却）")
            raise HTTPException(404, "no avatar")
        except Exception:
            _record_avatar("telegram", "error")
            logger.debug("[protocol] telegram 头像下载失败", exc_info=True)
            raise HTTPException(404, "no avatar")
        finally:
            # 机会式：只要 get_chat 拿到了身份，顺带落库(被动补名)+暖 resolve 缓存——零额外 API。
            # 覆盖「有头像/无头像/下载失败」所有分支：滚动懒加载头像即帮存量数字号补名。
            if ident_holder:
                try:
                    _st = getattr(request.app.state, "inbox_store", None)
                    _res = _persist_and_cache_tg_identity(
                        _st, account_id, chat_key, ident_holder)
                    _record_peer_identity(
                        "tg_avatar",
                        "resolved" if (_res["name"] or _res["username"]
                                       or _res["phone"]) else "miss")
                except Exception:
                    logger.debug("[protocol] avatar 顺手补名失败", exc_info=True)
        _clear_tg_client_bad(account_id)   # RPC 成功 → 解除冷却（若有）
        if not saved or not jpg.exists():
            _record_avatar("telegram", "empty")     # get_chat 成功但无头像
            try:
                none_marker.write_text("", encoding="utf-8")
            except Exception:
                pass
            raise HTTPException(404, "no avatar")
        if none_marker.exists():
            try:
                os.remove(none_marker)
            except Exception:
                pass
        _record_avatar("telegram", "fetched")
        return RedirectResponse(url_path, status_code=302)

    @app.get("/api/platforms/{platform}/{account_id}/avatar")
    async def api_platform_avatar(platform: str, account_id: str, request: Request):
        """头像缓存代理（P2）：向 Baileys(WhatsApp)/Node(Messenger) 取直链，落盘 static 后 302。

        安全要点（防封号）：
        - **单个会话按需取**（前端 <img loading=lazy>，只在滚动进视口才请求，天然节流）；
        - 命中磁盘缓存(≤7 天)直接 302，不再回源；
        - 无头像/隐私号写 `.none` 负缓存(≤1 天)，避免反复回源触发速率限制。
        无头像 → 404（前端 <img onerror> 回落首字母头像）。Messenger 直链取自入站轮询已抓到的
        scontent 缓存（不额外导航），下载落盘避免 scontent token 时效/跨域问题。
        """
        from fastapi.responses import RedirectResponse
        api_auth(request)
        platform = str(platform or "").lower()
        chat_key = str(request.query_params.get("chat_key") or "").strip()
        if not chat_key:
            raise HTTPException(404, "no avatar")
        if platform == "telegram":
            return await _telegram_peer_avatar(request, account_id, chat_key)
        if platform not in ("whatsapp", "messenger"):
            raise HTTPException(404, "no avatar")
        jpg, none_marker, url_path = _avatar_disk_paths(platform, account_id, chat_key)
        now = time.time()
        if jpg.exists() and (now - jpg.stat().st_mtime) < 7 * 86400:
            _record_avatar(platform, "cache_hit")      # 磁盘命中，未回源
            return RedirectResponse(url_path, status_code=302)
        if none_marker.exists() and (now - none_marker.stat().st_mtime) < 86400:
            _record_avatar(platform, "neg_hit")        # 负缓存命中（无头像，未回源）
            raise HTTPException(404, "no avatar")
        cfg = (config_manager.config if config_manager is not None else {}) or {}
        if platform == "whatsapp":
            # 群头像走 @g.us（否则 @s.whatsapp.net）；jid 由 Python 拼好，Node toJid 原样透传
            is_group = str(request.query_params.get("chat_type") or "").lower() == "group"
            jid_suffix = "@g.us" if is_group else "@s.whatsapp.net"
            from src.integrations.whatsapp_baileys_login import (
                protocol_enabled as wa_enabled, service_base_url, _get_json,
            )
            if not wa_enabled(cfg):
                _record_avatar(platform, "error")      # 服务未启用 → 取不到图
                raise HTTPException(404, "no avatar")
            try:
                res = await _get_json(
                    f"{service_base_url(cfg)}/accounts/{account_id}/avatar"
                    f"?jid={chat_key}{jid_suffix}")
            except Exception:
                _record_avatar(platform, "error")
                logger.debug("[protocol] profilePictureUrl 取头像失败", exc_info=True)
                raise HTTPException(404, "no avatar")
        else:  # messenger：取轮询缓存的 scontent 直链（Node 不额外导航）
            from src.integrations.messenger_web_login import (
                web_enabled as msgr_enabled, service_base_url as msgr_base,
                _get_json as msgr_get_json,
            )
            if not msgr_enabled(cfg):
                _record_avatar(platform, "error")
                raise HTTPException(404, "no avatar")
            try:
                res = await msgr_get_json(
                    f"{msgr_base(cfg)}/accounts/{account_id}/avatar?thread={chat_key}")
            except Exception:
                _record_avatar(platform, "error")
                logger.debug("[protocol] messenger 取头像失败", exc_info=True)
                raise HTTPException(404, "no avatar")
        remote_url = str((res or {}).get("url") or "").strip()
        # messenger 空 url 多为「轮询尚未缓存」瞬态 → 不写 1 天负缓存，靠重渲染自愈；
        # empty/fetched/error 结局经 on_outcome 回调落观测（DI，不把 helper 直连观测单例）
        return await _download_and_cache_avatar(
            remote_url, jpg, none_marker, url_path,
            neg_cache=(platform != "messenger"),
            on_outcome=lambda oc: _record_avatar(platform, oc))

    @app.get("/api/platforms/telegram/{account_id}/resolve-peer")
    async def api_telegram_resolve_peer(account_id: str, request: Request):
        """惰性把「一排数字 id」的 Telegram 私聊补成真实昵称 / @username / 电话（自愈式回填）。

        动机：历史会话在「昵称优先级护栏」上线前落库时只存了裸 id；本端点在坐席**打开**该
        会话时按需向 pyrogram 拉一次 peer 资料补齐——覆盖**存量 + 未来**任何仍是数字的 peer，
        由人工浏览速度天然节流（比一次性批量脚本更安全：不会一口气对上千 peer 打 API 触发风控）。
        与头像端点同构：web 后台在独立 loop、pyrogram client 在主 loop，经 client 自身 loop 用
        ``run_coroutine_threadsafe`` 调度，不跨 loop await。进程级正/负缓存防重复打 API。
        取不到 → ``ok:false``，前端静默保持原样（回落裸 id + 首字母渐变头像）。落库走
        ``update_conversation_identity`` 的昵称优先级护栏（不会用空/裸号码冲掉已存真名）。
        纯核心见模块级 ``resolve_tg_peer_identity``（独立单测）；多账号 client 取数见
        ``_get_tg_pyro_for_account``（受管 worker 优先，回落主 client）。
        """
        api_auth(request)
        store = getattr(request.app.state, "inbox_store", None)
        pyro = _get_tg_pyro_for_account(request.app, account_id)
        chat_key = str(request.query_params.get("chat_key") or "").strip()
        # 线程池执行：纯核心内部有 fut.result(timeout=8) 同步等待（跨 loop 取 get_chat 结果），
        # 直接在 async handler 里调用会把 web 事件循环冻结（pyrogram 断网重连时挂满超时）
        # → 全站请求排队 → 前端 /thread 15s 超时红屏。丢线程池后循环照常服务其他请求；
        # 纯核心保持同步签名（单测/复用方零改动）。
        # 并发上限（防线程池耗尽）：starlette 默认线程池 ~40 线程为全站 sync 端点共用，
        # 断网时若大量数字号会话同时打开、每个 resolve 挂满 8s，会把池占空拖垮其他端点。
        # 超上限直接快败（ok:false 不缓存，下次打开重试）——本就是 best-effort 自愈补名。
        if not _RESOLVE_PEER_SEM.acquire(blocking=False):
            _record_peer_identity("tg_open", "unavailable")
            return {"ok": False, "reason": "busy"}
        try:
            from starlette.concurrency import run_in_threadpool
            return await run_in_threadpool(
                resolve_tg_peer_identity, store, pyro, account_id, chat_key)
        finally:
            _RESOLVE_PEER_SEM.release()

    def _collect_config_accounts(cfg: Dict[str, Any]) -> List[tuple]:
        """从 config.yaml 抽取各平台声明的账号 → (platform, account_id, mode, label)。

        覆盖 telegram.accounts[]（+ 扁平单号）与 line/messenger/whatsapp_rpa.accounts[]
        （+ 扁平 enabled 单号）。形态各异，全程防御式读取。
        """
        out: List[tuple] = []
        tg = cfg.get("telegram") or {}
        if isinstance(tg, dict):
            if tg.get("api_id") or tg.get("session_name"):
                out.append(("telegram", str(tg.get("session_name") or "default"),
                            "protocol", str(tg.get("label") or "Telegram")))
            for a in tg.get("accounts") or []:
                if not isinstance(a, dict):
                    continue
                aid = str(a.get("id") or a.get("session_name") or "")
                if aid:
                    out.append(("telegram", aid, "protocol", str(a.get("label") or aid)))
        for plat, key in (("line", "line_rpa"), ("messenger", "messenger_rpa"),
                          ("whatsapp", "whatsapp_rpa")):
            block = cfg.get(key) or {}
            if not isinstance(block, dict):
                continue
            accs = block.get("accounts") or []
            if isinstance(accs, list) and accs:
                for a in accs:
                    if not isinstance(a, dict):
                        continue
                    aid = str(a.get("account_id") or a.get("id") or "")
                    if aid:
                        out.append((plat, aid, "device", str(a.get("label") or aid)))
            elif block.get("enabled"):
                aid = str(block.get("account_id") or f"{plat}_default")
                out.append((plat, aid, "device", str(block.get("label") or aid)))
        return out

    @app.get("/api/accounts")
    async def api_accounts_list(request: Request):
        """统一账号清单（Phase 2）：合并 registry + config.yaml + 运行时健康。

        让桌面端 / web 后台用同一份数据渲染「账号管理」面板，不必再拼 4 个接口。
        返回每个账号的 ``platform/account_id/mode/label/status/running/proxy_id/
        fingerprint_id/sources``（sources 标出来源：registry/config/runtime）。
        """
        api_auth(request)
        cfg = (config_manager.config if config_manager is not None else {}) or {}
        merged: Dict[tuple, Dict[str, Any]] = {}

        def _ensure(platform: str, account_id: str) -> Dict[str, Any]:
            k = (str(platform or "").lower(), str(account_id or ""))
            if k not in merged:
                merged[k] = {
                    "platform": k[0], "account_id": k[1], "mode": "",
                    "label": "", "status": "unknown", "running": False,
                    "proxy_id": "", "fingerprint_id": "", "auto_reply": False,
                    "auto_reply_override": {}, "sources": [],
                }
            return merged[k]

        # 1) 注册表（登录/编排/桌面 ingest 落库的权威账号）
        try:
            for row in get_account_registry().list():
                r = _ensure(row.get("platform"), row.get("account_id"))
                r["mode"] = row.get("mode") or r["mode"]
                r["label"] = row.get("label") or r["label"]
                r["status"] = row.get("status") or r["status"]
                r["proxy_id"] = row.get("proxy_id") or r["proxy_id"]
                r["fingerprint_id"] = row.get("fingerprint_id") or r["fingerprint_id"]
                meta = row.get("meta") or {}
                r["auto_reply"] = bool(meta.get("auto_reply"))
                r["auto_reply_override"] = dict(meta.get("autoreply_override") or {})
                # P1 身份化：透出自身昵称/用户名/头像（缺失即空串，前端回落占位头像）
                for _sk in ("self_name", "self_username", "self_avatar"):
                    _sv = str(meta.get(_sk) or "").strip()
                    if _sv:
                        r[_sk] = _sv
                # 账号卡徽标（轻量，直接来自 meta，不额外查询）：
                # 绑定人设 + 近 7 天成功改资料次数（≥3 前端出黄色热点标）
                _pid = str(meta.get("persona_id") or "").strip()
                if _pid:
                    r["persona_id"] = _pid
                try:
                    from src.integrations.account_profile_push import (
                        profile_churn_count,
                    )
                    _churn = int(profile_churn_count(meta))
                    if _churn > 0:
                        r["profile_churn_7d"] = _churn
                except Exception:
                    pass
                if "registry" not in r["sources"]:
                    r["sources"].append("registry")
        except Exception:
            logger.debug("[accounts] registry 读取失败", exc_info=True)

        # 2) config.yaml 声明的账号（boot 时拉起的 RPA / 协议号）
        for platform, account_id, mode, label in _collect_config_accounts(cfg):
            r = _ensure(platform, account_id)
            if not r["mode"]:
                r["mode"] = mode
            if not r["label"]:
                r["label"] = label
            if "config" not in r["sources"]:
                r["sources"].append("config")

        # 3) 运行时健康（适配器在线状态）
        try:
            status_map = status_via_adapters(request, _INBOX_ADAPTERS)
            for k, v in (status_map or {}).items():
                if not isinstance(v, dict):
                    continue
                platform = v.get("platform")
                account_id = v.get("account_id") or k
                if not platform:
                    continue
                r = _ensure(platform, account_id)
                r["running"] = bool(v.get("running"))
                if v.get("running"):
                    r["status"] = "online"
                if "runtime" not in r["sources"]:
                    r["sources"].append("runtime")
        except Exception:
            logger.debug("[accounts] 运行时状态读取失败", exc_info=True)

        # 3.5) 账号池编排器 managed 状态（N4：protocol/official worker 不经 inbox 适配器，
        #       须单独并入，否则扫码登入并被编排器拉起的协议号会显示为「未在线」）
        try:
            for oa in (_orch().status().get("accounts") or []):
                platform = oa.get("platform")
                account_id = oa.get("account_id")
                if not platform or not account_id:
                    continue
                r = _ensure(platform, account_id)
                if not r["mode"]:
                    r["mode"] = oa.get("mode") or ""
                if oa.get("state") == "running":
                    r["running"] = True
                    r["status"] = "online"
                if "orchestrator" not in r["sources"]:
                    r["sources"].append("orchestrator")
        except Exception:
            logger.debug("[accounts] 编排器状态读取失败", exc_info=True)

        # 3.6) 会话健康（外部 worker 上报的掉线时间戳）——让前端账号卡展示「掉线 X 分钟」+
        #      平台对应的自助恢复 CTA，而不是只有一个笼统的「离线」。全程防御式，任一步失败不影响清单。
        try:
            from src.integrations.platform_session_health import (
                get_platform_session_health,
            )
            for hk, hv in (get_platform_session_health().unhealthy_sessions() or {}).items():
                if not isinstance(hv, dict):
                    continue
                since = float(hv.get("unhealthy_since") or 0.0)
                if not since:
                    continue
                # hk = "platform:account_id"；account_id 可能含冒号，按首个冒号切分
                platform, _, account_id = str(hk or "").partition(":")
                if not platform:
                    continue
                r = _ensure(platform, account_id or "default")
                r["unhealthy_since"] = since
                if hv.get("detail"):
                    r["health_detail"] = str(hv.get("detail"))
        except Exception:
            logger.debug("[accounts] 会话健康读取失败", exc_info=True)

        # 4) 自动回复配额/熔断快照（Phase 5，仅协议号或已开自动回复的号）
        try:
            from src.integrations.protocol_autoreply_limits import (
                get_autoreply_limiter,
            )
            from src.integrations.protocol_autoreply_settings import (
                cfg_with_settings,
            )
            lim = get_autoreply_limiter(cfg_with_settings(cfg))
            for a in merged.values():
                if a.get("auto_reply") or a.get("mode") == "protocol":
                    ov_rate = (a.get("auto_reply_override") or {}).get("rate") or {}
                    a["auto_reply_quota"] = lim.snapshot(
                        f"{a['platform']}:{a['account_id']}",
                        hourly=ov_rate.get("hourly"), daily=ov_rate.get("daily"))
        except Exception:
            logger.debug("[accounts] 配额快照读取失败", exc_info=True)

        accounts = sorted(merged.values(),
                          key=lambda x: (x["platform"], x["account_id"]))
        # P5：脱敏开关下发桌面/前端（与 web 模板 _MASK_ACCT_PHONE 同源，默认脱敏）
        mask_phone = bool(
            ((cfg.get("accounts") or {}).get("self_profile") or {})
            .get("mask_phone", True))
        return {"ok": True, "accounts": accounts, "count": len(accounts),
                "mask_phone": mask_phone}

    @app.get("/api/accounts/orchestrator")
    async def api_orchestrator_status(request: Request):
        api_auth(request)
        return {"ok": True, **_orch().status()}

    @app.get("/api/accounts/fleet-health")
    async def api_accounts_fleet_health(request: Request):
        """N 线 N6：机群反封号健康灯 + 生命周期分布（云端多开运维总览）。

        信号源统一：注册表(天龄/代理/状态) + 自动回复限额计数(今日发送/熔断)，
        经 companion_send_gate.aggregate_fleet(M7) 汇成总体灯色 + 每号建议上限/原因，
        并按 pending/warming/active/restricted/banned/offline 统计生命周期分布。
        """
        api_auth(request)
        cfg = (config_manager.config if config_manager is not None else {}) or {}
        from src.skills.account_signals import fleet_overview
        from src.integrations.protocol_autoreply_limits import get_autoreply_limiter
        from src.integrations.protocol_autoreply_settings import cfg_with_settings
        reg = get_account_registry()
        try:
            lim = get_autoreply_limiter(cfg_with_settings(cfg))
        except Exception:
            lim = None
        accounts = [
            (r.get("platform"), r.get("account_id"), r.get("status", ""))
            for r in reg.list()
        ]
        overview = fleet_overview(
            accounts, registry=reg, limiter=lim, config=cfg
        )
        # P2：身份采集可观测——把 self_profile 富集计数并入机群健康，便于核对真号环境是否生效
        try:
            from src.integrations.account_self_profile import get_self_profile_stats
            overview["self_profile"] = get_self_profile_stats()
        except Exception:
            logger.debug("[accounts] self_profile 计数读取失败", exc_info=True)
        # 官方资料推送漏斗（与 ops 卡同源；零流量时 active=false）
        try:
            from src.integrations.account_profile_push import get_profile_push_stats
            overview["profile_push"] = get_profile_push_stats()
        except Exception:
            logger.debug("[accounts] profile_push 计数读取失败", exc_info=True)
        # P5：机会式孤儿头像清扫（开头像 + 节流），回收「注册表已无但文件还在」的残留
        global _last_avatar_sweep_ts
        try:
            from src.integrations.account_self_profile import (
                self_avatar_enabled, sweep_orphan_avatars,
            )
            now = time.time()
            if self_avatar_enabled(cfg) and \
                    now - _last_avatar_sweep_ts >= _AVATAR_SWEEP_INTERVAL_S:
                _last_avatar_sweep_ts = now
                known = {(r.get("platform"), r.get("account_id")) for r in reg.list()}
                swept = sweep_orphan_avatars(known)
                if swept.get("removed"):
                    logger.info("[accounts] 孤儿头像清扫 %s", swept)
                overview["avatar_sweep"] = swept
        except Exception:
            logger.debug("[accounts] 孤儿头像清扫失败", exc_info=True)
        return {"ok": True, **overview}

    @app.get("/api/accounts/send-health")
    async def api_accounts_send_health(request: Request, hours: float = 24.0):
        """全自动发送安全视图：每号 今日发量/占 cap + 投递成功/失败(含归因) + 健康灯 + 回复率。

        只读，复用 fleet-health 同源信号（注册表 + 持久化 limiter 今日计数）+ draft_audit_log
        自动发审计 + messages 回复探测。回答「真号自动发到底安不安全」的运营总览。
        """
        api_auth(request)
        cfg = (config_manager.config if config_manager is not None else {}) or {}
        from src.inbox.send_health import gather_send_health
        from src.integrations.protocol_autoreply_limits import get_autoreply_limiter
        from src.integrations.protocol_autoreply_settings import cfg_with_settings
        store = getattr(request.app.state, "inbox_store", None)
        if store is None:
            return {"ok": False, "available": False, "message": "inbox store 未就绪"}
        try:
            lim = get_autoreply_limiter(cfg_with_settings(cfg))
        except Exception:
            lim = None
        data = gather_send_health(
            inbox_store=store, registry=get_account_registry(), limiter=lim,
            config=cfg, window_hours=max(1.0, min(float(hours or 24), 168.0)),
        )
        return {"ok": True, "available": True, **data}

    @app.get("/api/accounts/protocol/readiness")
    async def api_protocol_readiness(request: Request):
        """协议栈联调自检：配置/依赖/服务可达性/编排器/入站 sink 的结构化就绪报告。"""
        api_auth(request)
        cfg = (config_manager.config if config_manager is not None else {}) or {}
        from src.integrations.protocol_diagnostics import readiness
        report = await readiness(cfg)
        return {"ok": True, **report}

    @app.post("/api/accounts/orchestrator/sync")
    async def api_orchestrator_sync(request: Request):
        api_auth(request)
        orch = _orch()
        await orch.sync()
        await orch.tick()
        return {"ok": True, **orch.status()}

    @app.post("/api/accounts/{platform}/{account_id}/start")
    async def api_account_start(platform: str, account_id: str, request: Request):
        api_auth(request)
        orch = _orch()
        acc = get_account_registry().get(platform, account_id) or {
            "platform": platform, "account_id": account_id}
        ok = await orch.start_account(acc)
        return {"ok": ok}

    @app.post("/api/accounts/{platform}/{account_id}/stop")
    async def api_account_stop(platform: str, account_id: str, request: Request):
        api_auth(request)
        await _orch().stop_account(_acct_key(platform, account_id))
        return {"ok": True}

    @app.post("/api/accounts/{platform}/{account_id}/restart")
    async def api_account_restart(platform: str, account_id: str, request: Request):
        api_auth(request)
        ok = await _orch().restart_account(_acct_key(platform, account_id))
        return {"ok": ok}

    async def _remote_logout(platform: str, account_id: str,
                             cfg: Dict[str, Any]) -> None:
        """Best-effort 通知平台侧 Node 微服务真正登出（WhatsApp / Messenger）。绝不抛。

        两家微服务端点同形（``POST /accounts/{id}/logout``），但后果不同：
        - WhatsApp：解除设备关联，否则手机端仍留着一个「已登录的网页设备」。
        - Messenger：关浏览器上下文 + 删持久化 profile。**漏掉这一步会无限弹窗**——
          Node 把 context ``close`` 当崩溃自愈（3s 后回灌 ``<login_id>.cookies.json``
          快照重登，且每次登录成功都清零退避计数 → 永远到不了放弃点），只有它自己的
          cancel/logout 会打「主动关闭」免疫标记。于是坐席登出后那个 headed 窗口关一次
          弹一次（2026-07-30 实测三分钟 6 轮），在网页里手动退出登录也会被快照回灌撤销。
        """
        plat = str(platform or "").lower()
        try:
            if plat == "whatsapp":
                from src.integrations.whatsapp_baileys_login import (
                    protocol_enabled, service_base_url, _post_json,
                )
            elif plat == "messenger":
                from src.integrations.messenger_web_login import (
                    web_enabled as protocol_enabled, service_base_url, _post_json,
                )
            else:
                return
            if not protocol_enabled(cfg):
                return
            await _post_json(
                f"{service_base_url(cfg)}/accounts/{account_id}/logout", {})
        except Exception:
            logger.debug("[accounts] 远端登出失败（忽略）", exc_info=True)

    def _clear_session_creds(platform: str, account_id: str,
                             *, status: str = "offline") -> None:
        """清空注册表里可自动重连的会话凭据，使账号需重新扫码登录（保留记录）。"""
        reg = get_account_registry()
        row = reg.get(platform, account_id)
        if row is None:
            return
        meta = dict(row.get("meta") or {})
        for k in ("session_string", "session_name"):
            meta.pop(k, None)
        reg.upsert(platform, account_id, meta=meta, status=status)

    @app.post("/api/accounts/{platform}/{account_id}/logout")
    async def api_account_logout(platform: str, account_id: str, request: Request):
        """登出账号：停 worker + 通知平台解除关联 + 清空会话凭据。

        保留注册表记录（可重新扫码登录），仅使其下线并断开自动重连。
        仅对注册表在册账号有意义；config/适配器来源的条目（如 A 线 telegram
        default、web）没有可清的会话凭据 → 明确 404，而不是静默空操作。
        """
        api_auth(request)
        cfg = (config_manager.config if config_manager is not None else {}) or {}
        if get_account_registry().get(platform, account_id) is None:
            raise HTTPException(
                404, tr(request, "err.ws.account_not_in_registry"))
        try:
            await _orch().stop_account(_acct_key(platform, account_id))
        except Exception:
            logger.debug("[accounts] logout 停止 worker 失败", exc_info=True)
        await _remote_logout(platform, account_id, cfg)
        _clear_session_creds(platform, account_id)
        return {"ok": True, "platform": platform, "account_id": account_id}

    @app.post("/api/accounts/{platform}/{account_id}/remove")
    async def api_account_remove(platform: str, account_id: str, request: Request):
        """删除账号：登出 + 从注册表移除（软删 status=removed，回收自身头像）。

        注册表里没有的账号（config/适配器来源，如 A 线 telegram default、web）
        软删是空操作——旧版仍回 ok:true，前端刷新后账号原样回来，表现为
        「点删除没反应」（2026-07-21 生产实测）→ 现在明确 404 告知不可删。
        """
        api_auth(request)
        cfg = (config_manager.config if config_manager is not None else {}) or {}
        if get_account_registry().get(platform, account_id) is None:
            raise HTTPException(
                404, tr(request, "err.ws.account_not_in_registry"))
        try:
            await _orch().stop_account(_acct_key(platform, account_id))
        except Exception:
            logger.debug("[accounts] remove 停止 worker 失败", exc_info=True)
        await _remote_logout(platform, account_id, cfg)
        try:
            get_account_registry().remove(platform, account_id)
        except Exception:
            logger.debug("[accounts] remove 注册表移除失败", exc_info=True)
        return {"ok": True, "platform": platform, "account_id": account_id}

    @app.post("/api/accounts/{platform}/{account_id}/label")
    async def api_account_set_label(
        platform: str, account_id: str, request: Request,
    ):
        """P2：给账号起人格名（落 registry.label，权威人格名来源）。

        body: {label: str}。空字符串=清除别名（回落显示 account_id）。
        对 config 声明但还没进注册表的号（如 A 线 default）会建一条承载 label 的
        记录；**不传 mode**（upsert 默认 device，telegram device 无 worker factory，
        不会被编排器拉起 → 不会触发重复连接 / database lock）。
        """
        api_auth(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        label = str((body or {}).get("label") or "").strip()[:40]
        reg = get_account_registry()
        row = reg.get(platform, account_id)
        if row is None:
            reg.upsert(platform, account_id, label=label, status="pending")
        else:
            reg.upsert(platform, account_id, label=label)
        return {"ok": True, "platform": platform,
                "account_id": account_id, "label": label}

    @app.get("/api/accounts/{platform}/{account_id}/profile")
    async def api_account_profile_get(
        platform: str, account_id: str, request: Request,
    ):
        """账号官方资料详情（accounts.profile_push 读口）：能力/自身资料/冷却/审计。

        registry 无此账号也能返回（self 空、冷却不激活）——前端对 config 来源账号
        （如 A 线 telegram default）也会点开详情。
        """
        api_auth(request)
        cfg = (config_manager.config if config_manager is not None else {}) or {}
        from src.integrations.account_profile_push import (
            capabilities, cooldown_remaining, fresh_account_days,
            profile_churn_count, profile_push_enabled, push_cooldown_hours,
            resolve_persona_fill,
        )
        from src.integrations.account_self_profile import (
            read_self_profile_from_meta,
        )
        platform = str(platform or "").lower()
        row = get_account_registry().get(platform, account_id) or {}
        meta = row.get("meta") or {}
        persona_id = str(meta.get("persona_id") or "")
        if not persona_id:
            pids = meta.get("persona_ids") or []
            if isinstance(pids, (list, tuple)) and pids:
                persona_id = str(pids[0] or "")
        # 人设一键填充素材预览（use_persona）：绑定人设的名称/锁脸头像是否可用。
        # best-effort——PersonaManager 异常/人设已删 → null，详情页其余信息照常。
        persona = None
        if persona_id:
            try:
                fill = resolve_persona_fill(persona_id, cfg)
            except Exception:
                fill = {}
                logger.debug("[profile_push] persona 填充素材解析失败", exc_info=True)
            if fill:
                face_path = str(fill.get("face_ref_path") or "")
                persona = {
                    "id": str(fill.get("id") or persona_id),
                    "name": str(fill.get("name") or ""),
                    "face_ref": bool(face_path),
                }
                if face_path:
                    try:
                        mt = int(Path(face_path).stat().st_mtime)
                    except Exception:
                        mt = 0
                    persona["face_ref_url"] = (
                        f"/api/personas/{persona_id}/face-ref/image?v={mt}")
        # 新号风控：账号接入天数（registry.created_at）→ 详情页保存前二次确认提示
        try:
            created_at = float(row.get("created_at") or 0)
        except (TypeError, ValueError):
            created_at = 0.0
        account_age_days = (round((time.time() - created_at) / 86400.0, 1)
                            if created_at > 0 else None)
        fdays = fresh_account_days(cfg)
        hours = push_cooldown_hours(cfg)
        remain = cooldown_remaining(meta, time.time(), hours)
        try:
            last_push_ts = float(meta.get("profile_push_last_ts") or 0)
        except (TypeError, ValueError):
            last_push_ts = 0.0
        return {
            "ok": True, "platform": platform, "account_id": account_id,
            "enabled": profile_push_enabled(cfg),
            "capabilities": capabilities(platform),
            "self": read_self_profile_from_meta(meta),
            "label": str(row.get("label") or ""),
            "persona_id": persona_id,
            "persona": persona,
            "account_age_days": account_age_days,
            "fresh": bool(account_age_days is not None
                          and account_age_days < fdays),
            "fresh_days": fdays,
            "proxy": bool(row.get("proxy_id")),
            "fingerprint": bool(row.get("fingerprint_id")),
            "cooldown": {"active": remain > 0,
                         "remaining_sec": int(max(0, remain)),
                         "hours": hours},
            "last_push_ts": last_push_ts,
            "push_log": list(meta.get("profile_push_log") or [])[-5:],
            # 近 7 天成功推送次数（详情页「改名频繁」提示；≥3 黄条）
            "churn_7d": profile_churn_count(meta),
            # 跨运行审计（ops_events，含批量对齐）——供「变更历史」展开；缺库=空
            "profile_events": _recent_profile_events(platform, account_id),
        }

    @app.post("/api/accounts/{platform}/{account_id}/profile")
    async def api_account_profile_push(
        platform: str, account_id: str, request: Request,
    ):
        """把后台编辑的昵称/签名/头像推送到平台官方（accounts.profile_push 写口）。

        body: ``{name?, status_text?, avatar_b64?, use_persona?}``（status_text 对
        Telegram 语义为 bio）。``use_persona=true``＝「把账号对齐到绑定人设」：
        name/头像缺省时取绑定人设的 profile 名 / 锁脸基准照（显式字段永远优先，
        status_text 不受影响）。守卫顺序：flag → 平台能力 → 人设素材解析 →
        至少一字段 → 长度校验（对人设名同样生效）→ 头像解码/加工 → 冷却 →
        分平台执行（Telegram 协议直改 / WhatsApp 经 Baileys）→ meta 审计
        （read-merge-write，保住 session_string 等敏感键）→ 回读刷新 self_*。
        推送漏斗计数（attempts/cooldown_blocked/offline_blocked/failed/success/
        partial/persona_fill）全程 best-effort，绝不影响主流程。
        """
        api_auth(request)
        cfg = (config_manager.config if config_manager is not None else {}) or {}
        from src.integrations import account_profile_push as profile_push

        def _bump(key: str, plat: str = "") -> None:
            try:
                profile_push.bump_push_stat(key, plat or None)
            except Exception:
                logger.debug("[profile_push] 计数失败（忽略）", exc_info=True)

        platform = str(platform or "").lower()
        if not profile_push.profile_push_enabled(cfg):
            raise HTTPException(403, tr(request, "err.acct.profile_disabled"))
        caps = profile_push.capabilities(platform)
        if caps.get("mode") == "manual":
            raise HTTPException(
                400, tr(request, "err.acct.profile_platform_manual"))
        try:
            body = await request.json()
        except Exception:
            body = {}
        name = str((body or {}).get("name") or "").strip()
        status_text = str((body or {}).get("status_text") or "").strip()
        avatar_b64 = str((body or {}).get("avatar_b64") or "").strip()
        use_persona = bool((body or {}).get("use_persona"))
        reg = get_account_registry()
        row = reg.get(platform, account_id)
        # use_persona：把账号对齐到绑定人设——显式 body 字段优先，缺省才取人设素材
        persona_used = {"name": False, "avatar": False}
        persona_avatar_raw = None
        if use_persona:
            meta0 = dict((row or {}).get("meta") or {})
            pid = str(meta0.get("persona_id") or "")
            if not pid:
                pids = meta0.get("persona_ids") or []
                if isinstance(pids, (list, tuple)) and pids:
                    pid = str(pids[0] or "")
            try:
                fill = profile_push.resolve_persona_fill(pid, cfg) if pid else {}
            except Exception:
                fill = {}
                logger.debug("[profile_push] persona 填充素材解析失败", exc_info=True)
            p_name = str((fill or {}).get("name") or "")
            p_face = str((fill or {}).get("face_ref_path") or "")
            if not (p_name or p_face):
                raise HTTPException(
                    400, tr(request, "err.acct.profile_no_persona"))
            if not name and p_name:
                name = p_name
                persona_used["name"] = True
            if not avatar_b64 and p_face:
                try:
                    persona_avatar_raw = Path(p_face).read_bytes()
                except Exception:
                    # 读不到按坏图走同一 400 报错键（与显式坏头像同语义）
                    persona_avatar_raw = b""
                    logger.debug("[profile_push] face_ref 读取失败", exc_info=True)
                persona_used["avatar"] = True
        if not (name or status_text or avatar_b64
                or persona_avatar_raw is not None):
            raise HTTPException(400, tr(request, "err.acct.profile_no_fields"))
        bad = profile_push.validate_fields(platform, name, status_text)
        if bad is not None:
            raise HTTPException(400, tr(request, bad[0], **bad[1]))
        mb = profile_push.max_avatar_mb(cfg)
        mb_disp = int(mb) if mb == int(mb) else mb
        avatar_bytes = None
        if avatar_b64:
            import base64
            if avatar_b64.startswith("data:") and "," in avatar_b64:
                avatar_b64 = avatar_b64.split(",", 1)[1]  # 容忍 dataURL 前缀
            try:
                raw = base64.b64decode(avatar_b64)
            except Exception:
                raise HTTPException(
                    400, tr(request, "err.acct.profile_avatar_invalid"))
            try:
                avatar_bytes = profile_push.prepare_avatar(raw, mb)
            except ValueError as ex:
                key = str(ex.args[0] if ex.args
                          else "err.acct.profile_avatar_invalid")
                raise HTTPException(400, tr(request, key, mb=mb_disp))
        elif persona_avatar_raw is not None:
            try:
                avatar_bytes = profile_push.prepare_avatar(persona_avatar_raw, mb)
            except ValueError as ex:
                key = str(ex.args[0] if ex.args
                          else "err.acct.profile_avatar_invalid")
                raise HTTPException(400, tr(request, key, mb=mb_disp))
        meta = dict((row or {}).get("meta") or {})
        hours = profile_push.push_cooldown_hours(cfg)
        now = time.time()
        _bump("attempts", platform)
        remain = profile_push.cooldown_remaining(meta, now, hours)
        if remain > 0:
            _bump("cooldown_blocked")
            raise HTTPException(429, tr(
                request, "err.acct.profile_cooldown",
                remain=profile_push.format_remaining(remain)))
        requested = [f for f, v in (("name", name), ("status", status_text),
                                    ("avatar", avatar_bytes)) if v]
        tg_client = (_get_tg_pyro_for_account(request.app, account_id)
                     if platform == "telegram" else None)
        exe = await profile_push.execute_profile_push(
            platform, account_id, name=name, status_text=status_text,
            avatar_bytes=avatar_bytes, config=cfg, tg_client=tg_client)
        if exe.get("offline"):
            _bump("offline_blocked")
            raise HTTPException(409, tr(request, "err.acct.profile_offline"))
        applied = dict(exe.get("applied") or {})
        errors_raw = dict(exe.get("errors") or {})
        ok = bool(exe.get("ok"))
        # 推送结果计数（success=至少一字段成功；partial 单独可见；全败=failed）
        try:
            profile_push.record_push_result(platform, ok, bool(errors_raw))
            if ok and (persona_used["name"] or persona_used["avatar"]):
                profile_push.bump_push_stat("persona_fill")
        except Exception:
            logger.debug("[profile_push] 结果计数失败（忽略）", exc_info=True)
        # 成败都落 meta 审计（read-merge-write；registry 无此行时跳过，不建新行）
        if row is not None:
            try:
                fresh = dict((reg.get(platform, account_id) or {})
                             .get("meta") or {})
                merged = profile_push.merge_push_meta(
                    fresh, sorted(applied) if applied else requested, now, ok)
                reg.upsert(platform, account_id, meta=merged)
            except Exception:
                logger.debug("[profile_push] meta 审计写入失败", exc_info=True)
        # 审计事件（成败都记；单号推送人设 id 从本行 meta 取，best-effort）
        _record_profile_ops_event(
            platform, account_id, kind="profile_push", ok=ok,
            fields=(sorted(applied) if applied else requested),
            persona_id=str(meta.get("persona_id") or ""),
            note=("persona_fill" if (persona_used["name"]
                                     or persona_used["avatar"]) else ""))
        if not ok:
            raise HTTPException(
                502, tr(request, "err.acct.profile_push_failed"))
        await profile_push.refresh_self_after_push(
            platform, account_id, config=cfg,
            wa_res=exe.get("wa_res") or {},
            tg_client=exe.get("tg_client"),
            fallback_name=name)
        from src.integrations.account_self_profile import (
            read_self_profile_from_meta,
        )
        fresh_meta = (reg.get(platform, account_id) or {}).get("meta") or {}
        errors_out = {f: tr(request, "err.acct.profile_push_failed")
                      for f in errors_raw}
        return {
            "ok": True, "platform": platform, "account_id": account_id,
            "applied": applied, "errors": errors_out,
            "persona_used": persona_used,
            "self": read_self_profile_from_meta(fresh_meta),
            "cooldown": {"active": True,
                         "remaining_sec": int(hours * 3600), "hours": hours},
        }

    @app.post("/api/accounts/persona-align")
    async def api_accounts_persona_align(request: Request):
        """把某个人设名下已绑定账号的官方昵称/头像批量对齐到人设（P3）。

        body: ``{persona_id, dry_run?: bool, include?: ["plat:aid", ...]}``。
        - dry_run=true（默认）→ 只预览：``would_push`` / ``skipped_*``，零副作用；
        - dry_run=false → 串行真推（头像只 prepare 一次，账号间 sleep 1.5s），
          单号失败不中止整批。新号默认 skip（批量风控面大于单号二次确认）。
        - ``include``（仅真推生效）：账号白名单；未勾选的 would_push →
          ``skipped_excluded``。缺省 = 推全部 would_push。
        - ``fields``（仅真推生效）：字段白名单，``["name"]`` / ``["avatar"]`` /
          两者。缺省 = 推全部可推字段。只推头像时忽略昵称对齐判断。
        上限 ``ALIGN_MAX_ACCOUNTS``；响应 ``results[]`` + ``summary`` +
        ``same_platform_risk``。
        """
        api_auth(request)
        cfg = (config_manager.config if config_manager is not None else {}) or {}
        from src.integrations import account_profile_push as profile_push
        from src.integrations.account_self_profile import (
            read_self_profile_from_meta,
        )

        if not profile_push.profile_push_enabled(cfg):
            raise HTTPException(403, tr(request, "err.acct.profile_disabled"))
        try:
            body = await request.json()
        except Exception:
            body = {}
        persona_id = str((body or {}).get("persona_id") or "").strip()
        dry_run = bool((body or {}).get("dry_run", True))
        include_set = (None if dry_run
                       else profile_push.parse_align_include(
                           (body or {}).get("include")))
        fields_set = (None if dry_run
                      else profile_push.parse_align_fields(
                          (body or {}).get("fields")))
        use_name = fields_set is None or "name" in fields_set
        use_avatar = fields_set is None or "avatar" in fields_set
        # 批次号：把本次真推产生的多条 per-account 审计事件归组（dry_run 不落审计→不需要）
        run_id = "" if dry_run else __import__("uuid").uuid4().hex[:8]
        if not persona_id:
            raise HTTPException(400, tr(request, "err.ws.field_required",
                                        field="persona_id"))
        try:
            fill = profile_push.resolve_persona_fill(persona_id, cfg)
        except Exception:
            fill = {}
            logger.debug("[profile_push] align persona 素材解析失败",
                         exc_info=True)
        p_name = str((fill or {}).get("name") or "")
        p_face = str((fill or {}).get("face_ref_path") or "")
        if not (p_name or p_face):
            raise HTTPException(400, tr(request, "err.acct.profile_no_persona"))

        reg = get_account_registry()
        bound = profile_push.accounts_bound_to_persona(
            reg.list(), persona_id)[:profile_push.ALIGN_MAX_ACCOUNTS]
        hours = profile_push.push_cooldown_hours(cfg)
        fdays = profile_push.fresh_account_days(cfg)
        now = time.time()
        mb = profile_push.max_avatar_mb(cfg)

        # 头像只加工一次——批量 N 账号共享同一 JPEG（省 CPU + 保证字节一致）
        avatar_bytes = None
        if p_face:
            try:
                avatar_bytes = profile_push.prepare_avatar(
                    Path(p_face).read_bytes(), mb)
            except Exception:
                logger.debug("[profile_push] align face_ref 加工失败",
                             exc_info=True)
                avatar_bytes = None
                if not p_name:
                    raise HTTPException(
                        400, tr(request, "err.acct.profile_avatar_invalid"))

        def _online(plat: str, aid: str) -> bool:
            if plat == "telegram":
                return _get_tg_pyro_for_account(request.app, aid) is not None
            if plat == "whatsapp":
                # 轻量：有编排器状态且 running；无状态时 dry-run 不拦（真推由 Node 判）
                try:
                    orch = getattr(request.app.state, "account_orchestrator", None)
                    if orch is None:
                        return True
                    st = (getattr(orch, "status", None) or {})
                    if callable(st):
                        st = st() or {}
                    row = None
                    if isinstance(st, dict):
                        row = (st.get(f"whatsapp:{aid}")
                               or st.get(aid)
                               or (st.get("accounts") or {}).get(aid))
                    if isinstance(row, dict):
                        return bool(row.get("running") or row.get("connected")
                                    or row.get("ok"))
                except Exception:
                    pass
                return True
            return False

        results: List[Dict[str, Any]] = []
        for row in bound:
            plat = str(row.get("platform") or "").lower()
            aid = str(row.get("account_id") or "")
            meta = dict(row.get("meta") or {})
            try:
                created_at = float(row.get("created_at") or 0)
            except (TypeError, ValueError):
                created_at = 0.0
            self_info = read_self_profile_from_meta(meta)
            self_name = str(self_info.get("self_name") or "")
            base = {
                "platform": plat, "account_id": aid,
                "label": str(row.get("label") or ""),
                "self_name": self_name,
            }
            # 字段级对齐：只推所选字段（dry_run 时 fields_set=None → 全量预览）
            eff_name = p_name if use_name else ""
            eff_avatar = avatar_bytes if use_avatar else None
            pred = profile_push.predict_align_status(
                plat, meta, now=now, cooldown_hours=hours,
                fresh_days=fdays, created_at=created_at,
                online=_online(plat, aid),
                target_name=eff_name, has_avatar=bool(eff_avatar),
                current_name=self_name)
            # 运营在确认框取消勾选 → include 白名单；未列入的 would_push 记 excluded
            if (not dry_run and pred == "would_push"
                    and include_set is not None
                    and (plat, aid) not in include_set):
                results.append({**base, "status": "skipped_excluded"})
                continue
            # 选了字段却对本号无可推内容（如只推头像但无头像）→ 视作排除，不空打 API
            if (not dry_run and pred == "would_push"
                    and not eff_name and not eff_avatar):
                results.append({**base, "status": "skipped_excluded"})
                continue
            if dry_run or pred != "would_push":
                results.append({**base, "status": pred})
                continue

            # ── 真推一条（共用 execute_profile_push）──
            name = eff_name
            bad = profile_push.validate_fields(plat, name, "")
            if bad is not None:
                results.append({**base, "status": "failed",
                                "reason": bad[0]})
                continue
            try:
                profile_push.bump_push_stat("attempts", plat)
            except Exception:
                pass
            tg_client = (_get_tg_pyro_for_account(request.app, aid)
                         if plat == "telegram" else None)
            exe = await profile_push.execute_profile_push(
                plat, aid, name=name, avatar_bytes=eff_avatar,
                config=cfg, tg_client=tg_client)
            if exe.get("offline"):
                try:
                    profile_push.bump_push_stat("offline_blocked")
                except Exception:
                    pass
                results.append({**base, "status": "skipped_offline"})
                await asyncio.sleep(1.5)
                continue
            if exe.get("error_key") == "err.acct.profile_platform_manual":
                results.append({**base, "status": "skipped_manual"})
                continue
            applied = dict(exe.get("applied") or {})
            errors_raw = dict(exe.get("errors") or {})
            ok = bool(exe.get("ok"))
            try:
                profile_push.record_push_result(
                    plat, ok, bool(errors_raw) and ok)
                if ok:
                    profile_push.bump_push_stat("persona_fill")
            except Exception:
                pass
            try:
                fresh = dict((reg.get(plat, aid) or {}).get("meta") or {})
                merged = profile_push.merge_push_meta(
                    fresh, sorted(applied) if applied else
                    [f for f, v in (("name", name), ("avatar", eff_avatar))
                     if v],
                    time.time(), ok)
                reg.upsert(plat, aid, meta=merged)
            except Exception:
                logger.debug("[profile_push] align meta 审计失败", exc_info=True)
            if ok:
                await profile_push.refresh_self_after_push(
                    plat, aid, config=cfg, wa_res=exe.get("wa_res") or {},
                    tg_client=exe.get("tg_client"), fallback_name=name)
            # 审计：每个真推账号记一条（可按号回溯「哪次批量把它对齐了」）
            _record_profile_ops_event(
                plat, aid, kind="profile_align", ok=ok,
                fields=(sorted(applied) if applied
                        else [f for f in ("name", "avatar")
                              if (f == "name" and eff_name)
                              or (f == "avatar" and eff_avatar)]),
                persona_id=persona_id, note="align", run_id=run_id)
            results.append({
                **base,
                "status": "pushed" if ok else "failed",
                "applied": applied,
            })
            await asyncio.sleep(1.5)

        if not dry_run:
            try:
                profile_push.bump_push_stat("align_runs")
            except Exception:
                pass
        pushed = sum(1 for r in results if r.get("status") == "pushed")
        failed = sum(1 for r in results if r.get("status") == "failed")
        would = sum(1 for r in results if r.get("status") == "would_push")
        skipped = len(results) - pushed - failed - would
        risk = profile_push.same_platform_align_risk(results)
        # 安全网：一次真推里同平台 ≥2 号被对齐到同一人设 → 关联封号风险，主动告警一次
        # （run 级、按批次 key 去抖；前端已默认只勾 1/平台，触发＝运营主动覆盖了默认）
        if not dry_run and risk:
            try:
                from src.utils.host_alert import notify_host
                worst = max(risk, key=lambda x: int(x.get("count") or 0))
                detail = "、".join(f"{x['platform']}×{x['count']}" for x in risk)
                notify_host(
                    "资料对齐同平台多号风险",
                    (f"人设 {persona_id} 本次对齐：{detail}。"
                     f"同平台多号同名/同头像易被关联风控，建议错峰分批。"),
                    key=f"profile_align_burst:{persona_id}:{worst.get('platform')}",
                    cooldown_sec=1800.0)
            except Exception:
                logger.debug("[profile_push] 同平台风险告警失败（忽略）",
                             exc_info=True)
        return {
            "ok": True,
            "dry_run": dry_run,
            "persona": {"id": persona_id, "name": p_name,
                        "face_ref": bool(avatar_bytes or p_face)},
            "results": results,
            "same_platform_risk": risk,
            "summary": {
                "total": len(results),
                "pushed": pushed if not dry_run else 0,
                "would_push": would if dry_run else 0,
                "failed": failed,
                "skipped": skipped,
            },
        }

    @app.post("/api/accounts/{platform}/{account_id}/auto-reply")
    async def api_account_auto_reply(platform: str, account_id: str, request: Request):
        """切换某协议账号的 7×24 自动回复（账号闸门，写入 registry meta.auto_reply）。

        注意这是「账号闸门」；真正自动发还需全局 ``config.protocol_autoreply.enabled``
        同时打开（双闸门）。body: {enabled: bool}
        """
        api_auth(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        enabled = bool((body or {}).get("enabled"))
        reg = get_account_registry()
        row = reg.get(platform, account_id)
        if row is None:
            raise HTTPException(404, tr(request, "err.ws.account_not_found"))
        meta = dict(row.get("meta") or {})
        was = bool(meta.get("auto_reply"))
        meta["auto_reply"] = enabled
        reg.upsert(platform, account_id, meta=meta)
        if was != enabled:
            try:
                from src.integrations.protocol_autoreply_audit import (
                    get_autoreply_audit,
                )
                actor = _session_agent(request)
                get_autoreply_audit().record_config_change(
                    actor=actor.get("agent_id", ""), scope="toggle",
                    platform=platform, account_id=account_id,
                    changes=[{"key": "auto_reply", "old": was, "new": enabled}])
            except Exception:
                logger.debug("[autoreply] 开关审计失败", exc_info=True)
        cfg = (config_manager.config if config_manager is not None else {}) or {}
        from src.integrations.protocol_autoreply_settings import effective_settings
        global_on = bool(effective_settings(cfg).get("enabled", False))
        return {"ok": True, "platform": platform, "account_id": account_id,
                "auto_reply": enabled, "global_enabled": global_on,
                "effective": enabled and global_on}

    @app.post("/api/accounts/{platform}/{account_id}/auto-reply/override")
    async def api_account_auto_reply_override(
        platform: str, account_id: str, request: Request,
    ):
        """按账号覆盖自动回复参数(配额/营业时段/延迟)，写 registry meta.autoreply_override。

        body: {rate?, hours?, delay?}（白名单深合并到现有覆盖）；
        或 {reset: true} 清空该账号覆盖（回落全局）。
        """
        api_auth(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        reg = get_account_registry()
        row = reg.get(platform, account_id)
        if row is None:
            raise HTTPException(404, tr(request, "err.ws.account_not_found"))
        from src.integrations.protocol_autoreply_settings import (
            deep_merge, diff_settings, sanitize_override,
        )
        meta = dict(row.get("meta") or {})
        before_ov = dict(meta.get("autoreply_override") or {})
        if (body or {}).get("reset"):
            meta.pop("autoreply_override", None)
            override: Dict[str, Any] = {}
        else:
            clean = sanitize_override(body or {})
            override = deep_merge(dict(meta.get("autoreply_override") or {}), clean)
            meta["autoreply_override"] = override
        reg.upsert(platform, account_id, meta=meta)
        try:
            changes = diff_settings(before_ov, override)
            if changes:
                from src.integrations.protocol_autoreply_audit import (
                    get_autoreply_audit,
                )
                actor = _session_agent(request)
                get_autoreply_audit().record_config_change(
                    actor=actor.get("agent_id", ""), scope="account",
                    platform=platform, account_id=account_id, changes=changes)
        except Exception:
            logger.debug("[autoreply] 覆盖审计失败", exc_info=True)
        return {"ok": True, "platform": platform, "account_id": account_id,
                "override": override}

    @app.get("/api/accounts/auto-reply/audit")
    async def api_account_auto_reply_audit(request: Request):
        """自动回复实时流（Phase 4）：最近 N 条决策 + 窗口统计 + 全局闸门状态。

        query: limit(默认50,≤500) / platform / account_id / since(秒,默认24h)
        """
        api_auth(request)
        qp = request.query_params
        try:
            limit = int(qp.get("limit") or 50)
        except Exception:
            limit = 50
        platform = qp.get("platform") or None
        account_id = qp.get("account_id") or None
        try:
            since_sec = float(qp.get("since") or 86400)
        except Exception:
            since_sec = 86400
        from src.integrations.protocol_autoreply_audit import get_autoreply_audit
        audit = get_autoreply_audit()
        items = audit.recent(limit=limit, platform=platform, account_id=account_id)
        stats = audit.stats(since_ts=time.time() - max(0.0, since_sec))
        cfg = (config_manager.config if config_manager is not None else {}) or {}
        from src.integrations.protocol_autoreply_settings import effective_settings
        global_on = bool(effective_settings(cfg).get("enabled", False))
        return {"ok": True, "items": items, "stats": stats,
                "global_enabled": global_on, "count": len(items)}

    @app.get("/api/accounts/auto-reply/config")
    async def api_account_auto_reply_config_get(request: Request):
        """读自动回复全局有效设置（config.yaml 基底 + JSON 覆盖）。"""
        api_auth(request)
        cfg = (config_manager.config if config_manager is not None else {}) or {}
        from src.integrations.protocol_autoreply_settings import effective_settings
        return {"ok": True, "settings": effective_settings(cfg)}

    @app.post("/api/accounts/auto-reply/config")
    async def api_account_auto_reply_config_set(request: Request):
        """改自动回复全局设置（白名单校验落盘 + 热更新限流器，无需重启）。"""
        api_auth(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        from src.integrations.protocol_autoreply_settings import (
            cfg_with_settings, diff_settings, effective_settings, save,
        )
        cfg0 = (config_manager.config if config_manager is not None else {}) or {}
        before = effective_settings(cfg0)
        merged = save(body or {})
        after = effective_settings(cfg0)
        try:
            changes = diff_settings(before, after)
            if changes:
                from src.integrations.protocol_autoreply_audit import (
                    get_autoreply_audit,
                )
                actor = _session_agent(request)
                get_autoreply_audit().record_config_change(
                    actor=actor.get("agent_id", ""), scope="global",
                    changes=changes)
        except Exception:
            logger.debug("[autoreply] 配置变更审计失败", exc_info=True)
        # 热更新限流器阈值
        try:
            from src.integrations.protocol_autoreply_limits import (
                get_autoreply_limiter,
            )
            cfg = (config_manager.config if config_manager is not None else {}) or {}
            lim = get_autoreply_limiter(cfg_with_settings(cfg))
            rate = merged.get("rate") or {}
            brk = merged.get("breaker") or {}
            lim.configure(
                hourly=rate.get("hourly"), daily=rate.get("daily"),
                breaker_threshold=brk.get("threshold"),
                breaker_cooldown=brk.get("cooldown_sec"),
            )
        except Exception:
            logger.debug("[autoreply] 限流器热更新失败", exc_info=True)
        cfg = (config_manager.config if config_manager is not None else {}) or {}
        return {"ok": True, "settings": effective_settings(cfg)}

    @app.get("/api/accounts/auto-reply/health")
    async def api_account_auto_reply_health(request: Request):
        """自动回复一键体检：全局/账号开关、配额余量、熔断、webhook、SkillManager 就绪
        + 最近配置变更，聚合成一张放量前自检表。"""
        api_auth(request)
        cfg = (config_manager.config if config_manager is not None else {}) or {}
        from src.integrations.protocol_autoreply_settings import (
            cfg_with_settings, effective_settings,
        )
        from src.integrations.protocol_autoreply_limits import get_autoreply_limiter
        from src.integrations.protocol_autoreply_audit import get_autoreply_audit
        eff = effective_settings(cfg)
        global_on = bool(eff.get("enabled", False))
        lim = get_autoreply_limiter(cfg_with_settings(cfg))

        accounts_on = 0
        protocol_n = 0
        circuit_open: List[str] = []
        try:
            for row in get_account_registry().list():
                meta = row.get("meta") or {}
                if row.get("mode") == "protocol":
                    protocol_n += 1
                if meta.get("auto_reply"):
                    accounts_on += 1
                ov_rate = (meta.get("autoreply_override") or {}).get("rate") or {}
                snap = lim.snapshot(
                    f"{row.get('platform')}:{row.get('account_id')}",
                    hourly=ov_rate.get("hourly"), daily=ov_rate.get("daily"))
                if snap.get("circuit_open"):
                    circuit_open.append(f"{row.get('platform')}:{row.get('account_id')}")
        except Exception:
            logger.debug("[autoreply-health] registry 读取失败", exc_info=True)

        # webhook 是否订阅了 autoreply_alert（用有效列表：覆盖层优先）
        webhook_on = False
        try:
            from src.integrations.notify_webhooks_store import effective_webhooks
            for wh in effective_webhooks(cfg):
                if wh.get("enabled") is False:
                    continue
                evs = wh.get("events") or []
                if "all" in evs or "autoreply_alert" in evs:
                    webhook_on = True
                    break
        except Exception:
            pass

        sm_ready = getattr(app.state, "skill_manager", None) is not None
        stats = get_autoreply_audit().stats(since_ts=time.time() - 86400)

        warnings: List[str] = []
        if global_on and accounts_on == 0:
            warnings.append("全局已开，但没有任何账号开启自动回复")
        if not global_on and accounts_on > 0:
            warnings.append(f"{accounts_on} 个账号已开自动回复，但全局闸门关闭，不会自动发")
        if global_on and not sm_ready:
            warnings.append("SkillManager 未就绪，无法生成回复")
        if circuit_open:
            warnings.append(f"{len(circuit_open)} 个账号处于熔断中")
        if (global_on or accounts_on) and not webhook_on:
            warnings.append("未配置 autoreply_alert webhook，熔断/配额告警不会外推")

        return {
            "ok": True,
            "healthy": len(warnings) == 0,
            "global_enabled": global_on,
            "skill_manager_ready": sm_ready,
            "webhook_alert_configured": webhook_on,
            "accounts": {"auto_reply_on": accounts_on, "protocol": protocol_n},
            "circuit_open": circuit_open,
            "limits": {
                "hourly": lim.hourly, "daily": lim.daily,
                "breaker_threshold": lim.breaker_threshold,
                "breaker_cooldown": lim.breaker_cooldown,
            },
            "stats_24h": stats,
            "warnings": warnings,
            "recent_changes": get_autoreply_audit().recent_config_changes(limit=10),
        }

    @app.get("/api/accounts/auto-reply/webhooks")
    async def api_account_auto_reply_webhooks_get(request: Request):
        """读有效告警渠道列表（脱敏 token/secret）。"""
        api_auth(request)
        cfg = (config_manager.config if config_manager is not None else {}) or {}
        from src.integrations.notify_webhooks_store import (
            effective_webhooks, mask,
        )
        items = effective_webhooks(cfg)
        return {"ok": True, "webhooks": mask(items), "count": len(items)}

    @app.post("/api/accounts/auto-reply/webhooks")
    async def api_account_auto_reply_webhooks_set(request: Request):
        """整段保存告警渠道列表（白名单校验落盘 + 热更 WebhookNotifier，免重启）。
        token/secret 留空 → 沿用同名旧值（前端展示是脱敏的，避免覆盖真实密钥）。"""
        api_auth(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        incoming = body.get("webhooks") if isinstance(body, dict) else body
        cfg = (config_manager.config if config_manager is not None else {}) or {}
        from src.integrations.notify_webhooks_store import (
            effective_webhooks, mask, sanitize_list, save_list,
        )
        # 按 name 保留旧密钥（前端回传脱敏值或空时不覆盖真实 token/secret）
        old_by_name = {w.get("name"): w for w in effective_webhooks(cfg)}
        cleaned = sanitize_list(incoming)
        for w in cleaned:
            old = old_by_name.get(w.get("name")) or {}
            for k in ("token", "secret"):
                nv = str(w.get(k) or "")
                if (not nv) or nv.endswith("***"):
                    w[k] = str(old.get(k) or "")
        saved = save_list(cleaned)
        # 热更运行中的 notifier
        try:
            notifier = getattr(app.state, "webhook_notifier", None)
            if notifier is not None and hasattr(notifier, "reload"):
                notifier.reload(saved)
        except Exception:
            logger.debug("[autoreply] webhook notifier 热更失败", exc_info=True)
        return {"ok": True, "webhooks": mask(saved), "count": len(saved)}

    @app.post("/api/accounts/auto-reply/webhooks/test")
    async def api_account_auto_reply_webhooks_test(request: Request):
        """对单条渠道即时发一条测试告警（连通性检查）。
        body: {index} 走有效列表第 index 条；或直接传 {webhook:{...}}。"""
        api_auth(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        cfg = (config_manager.config if config_manager is not None else {}) or {}
        from src.integrations.notify_webhooks_store import (
            effective_webhooks, sanitize_webhook,
        )
        items = effective_webhooks(cfg)
        wh: Dict[str, Any] = {}
        if isinstance(body, dict) and body.get("webhook"):
            wh = sanitize_webhook(body.get("webhook"))
            # 测试时若 token 脱敏/空，回退同名已存配置
            old = next((w for w in items if w.get("name") == wh.get("name")), {})
            for k in ("token", "secret"):
                nv = str(wh.get(k) or "")
                if (not nv) or nv.endswith("***"):
                    wh[k] = str(old.get(k) or "")
        else:
            idx = int((body or {}).get("index", -1))
            if 0 <= idx < len(items):
                wh = items[idx]
        if not wh:
            raise HTTPException(400, tr(request, "err.ws.no_valid_webhook"))

        notifier = getattr(app.state, "webhook_notifier", None)
        if notifier is None:
            from src.inbox.webhook_notifier import WebhookNotifier
            notifier = WebhookNotifier(config=[])
        try:
            res = await notifier.send_test(wh)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        return res

    @app.get("/api/accounts/auto-reply/stream")
    async def api_account_auto_reply_stream(request: Request):
        """自动回复实时流 SSE：record() 经进程内事件总线即时推送，零轮询零延迟。
        （先订阅再取最新 id 为游标：订阅后的事件必进队列，游标仅用于对订阅/
        取游标竞态窗口内的事件去重，从而不漏不重。）"""
        api_auth(request)
        from starlette.responses import StreamingResponse
        from src.integrations.protocol_autoreply_audit import (
            get_autoreply_audit, subscribe, unsubscribe,
        )
        audit = get_autoreply_audit()
        # 先订阅再取游标：订阅后产生的事件进队列，游标用于补播订阅前的增量并去重
        queue = subscribe()
        seed = audit.recent(limit=1)
        cursor = int(seed[0]["id"]) if seed else 0

        async def _gen():
            import asyncio as _aio
            last_id = cursor
            try:
                yield ": connected\n\n"
                while True:
                    try:
                        row = await _aio.wait_for(queue.get(), timeout=15.0)
                    except _aio.TimeoutError:
                        if await request.is_disconnected():
                            break
                        yield ": heartbeat\n\n"
                        continue
                    rid = int(row.get("id") or 0)
                    if rid and rid <= last_id:
                        continue  # 已补播/已推过，去重
                    last_id = rid
                    yield f"data: {json.dumps(row, ensure_ascii=False)}\n\n"
            finally:
                unsubscribe(queue)

        return StreamingResponse(_gen(), media_type="text/event-stream", headers={
            "Cache-Control": "no-cache", "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        })

    @app.on_event("startup")
    async def _orchestrator_autostart():
        cfg = (config_manager.config if config_manager is not None else {}) or {}
        if not orchestrator_enabled(cfg):
            return
        try:
            ensure_builtin_workers(cfg)
            await get_orchestrator(cfg).start_loop()
            logger.info("账号池编排器已随启动开启")
        except Exception:
            logger.debug("编排器自启动失败", exc_info=True)
