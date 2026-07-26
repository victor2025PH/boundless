"""Telegram「目录同步」生产者：好友名单 → 通讯录，云端会话 → 会话占位。

工作台原先只看得见「最近有消息往来」的会话——加了好友却从没开口的人根本不在列表里，
坐席无从主动发起。WhatsApp（Node Baileys 走 HTTP 桥）与 LINE（``LineProtocolWorker``
同进程直调 store）早已有这条生产者，本模块补上缺失的 Telegram 侧。

**为什么独立成模块**：有两类 Telegram 账号要复用同一套逻辑——主账号
``src/client/telegram_client.py::TelegramClient``（周期性 loop）与协议号
``src/integrations/account_orchestrator.py::TelegramProtocolWorker``（登录后一次性）。
因此这里全部是「传入 client + account_id + cfg」的无状态函数，既不持有连接也不读全局
状态，单测可直接喂 duck-typed 的假 pyrogram client。

⚠ **硬红线：目录同步只写名单，绝不产生「消息」**。它只调 store 的
``upsert_protocol_contacts`` / ``upsert_protocol_chats`` 两个方法（通讯录 + 会话占位），
**绝不**调用 ``emit_incoming`` / ``make_message`` / 任何 sink——因此不会触发自动回复、
不进 episodic 记忆、不算新消息、不刷 SLA。它产出的是「通讯录」，不是「收件箱内容」。

全程只读 RPC（``get_contacts`` / ``get_dialogs``）。FloodWait **刻意不单独 catch**：
pyrogram 内建等待已覆盖，真超限时外层调用方（loop / bootstrap）的 ``except Exception``
兜底进下一轮——与 ``TelegramClient._poll_inbound_loop`` 的既有惯例保持一致，
不为一个 6 小时一轮的只读任务引入额外重试语义。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List

from src.inbox.store import is_system_peer
from src.integrations.protocol_bridge import tg_peer_identity

logger = logging.getLogger(__name__)

# 会话分页节流：pyrogram 的 get_dialogs 内部按 100 条一页拉，页与页之间歇一下，
# 与 protocol_bridge.sync_telegram_history 的 pace_sec 同思路（温和拉取防风控）。
DEFAULT_PACE_SECONDS = 0.35
_DIALOG_PAGE = 100


# ── 配置读取 ────────────────────────────────────────────────────────────────

def directory_sync_cfg(config: Any) -> Dict[str, Any]:
    """取 ``platform_login.telegram.sync`` 段（缺省空 dict）。

    ``config`` 既可以是 ConfigManager（主账号）也可以是原始 dict（编排器 worker）——
    两者都支持 ``.get``，故这里只用 duck typing，不引入对任一实现的依赖。
    """
    try:
        tg = ((config.get("platform_login") or {}).get("telegram") or {})
    except Exception:  # noqa: BLE001 — 配置形态异常不该拖垮调用方
        return {}
    cfg = tg.get("sync") if isinstance(tg, dict) else None
    return cfg if isinstance(cfg, dict) else {}


def sync_enabled(cfg: Dict[str, Any]) -> bool:
    """默认 **关**（AGENTS.md「新子系统默认 false」约定）——本机 overlay 显式开。"""
    return bool((cfg or {}).get("enabled", False))


def _pos_int(value: Any, default: int) -> int:
    """配置值可能被写成字符串/空/None，一律归一为非负 int（异常回落默认）。"""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return n if n >= 0 else default


def _epoch(value: Any) -> float:
    """pyrogram 的 ``date`` 是 datetime → 转 epoch 秒；取不到一律 0（会话仍可见）。"""
    if value is None:
        return 0.0
    ts = getattr(value, "timestamp", None)
    if callable(ts):
        try:
            return float(ts())
        except Exception:  # noqa: BLE001
            return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _chat_is_group(chat: Any) -> bool:
    """群/超级群/频道 → 会话占位落 chat_type=group（分流「群组动态」，不刷 SLA）。

    判定写法照抄 ``TelegramClient._poll_inbound_once``：ChatType 枚举取 ``.name``，
    拿不到就 str 化——两处同一风格，pyrogram 换枚举实现时一起改。
    """
    ctype = getattr(chat, "type", None)
    name = (getattr(ctype, "name", None) or str(ctype or "")).upper()
    return "GROUP" in name or "CHANNEL" in name


# ── 拉取（纯读，无落库）─────────────────────────────────────────────────────

async def fetch_contact_rows(client: Any, limit: int) -> List[Dict[str, Any]]:
    """好友名单 → ``upsert_protocol_contacts`` 的 rows。

    pyrogram 2.x 的 ``get_contacts()`` 一次返回全量 User 列表（无 limit 形参），
    故上限只能在客户端截断。显示名复用 ``tg_peer_identity``（first+last → @username），
    全空时回落裸 id——与 ``tg_chat_dict`` 的 ``name or str(id)`` 同口径。
    """
    if client is None or limit <= 0:
        return []
    users = await client.get_contacts()
    rows: List[Dict[str, Any]] = []
    for user in list(users or [])[:limit]:
        uid = getattr(user, "id", None)
        if uid is None:
            continue
        ident = tg_peer_identity(user)
        rows.append({
            "jid": str(uid),
            "name": ident["name"] or str(uid),
            # notify 作备用名：store 在 name 为空时用它兜底显示
            "notify": ident["username"],
        })
    return rows


async def fetch_dialog_rows(
    client: Any, limit: int, *, pace_sec: float = DEFAULT_PACE_SECONDS,
    account_id: str = "",
) -> List[Dict[str, Any]]:
    """云端会话列表 → ``upsert_protocol_chats`` 的 rows（含未读数与末条时间）。

    ``account_id`` 只用于剔除系统 peer（Saved Messages / 官方服务号，见
    ``is_system_peer``）——**源头止血**：读侧虽然也过滤，但不挡住这里，每轮同步
    都会给这两个非人条目建一次会话占位，库里的噪音只增不减，且任何绕过联系人
    面板直接读 ``conversations`` 的消费方都会看见它们。
    留默认空串是为了向后兼容既有调用方（不传即不过滤，行为逐字节不变）。
    """
    rows: List[Dict[str, Any]] = []
    if client is None or limit <= 0:
        return rows
    account_id = str(account_id or "")
    seen = 0
    async for dialog in client.get_dialogs(limit=limit):
        chat = getattr(dialog, "chat", None)
        chat_id = getattr(chat, "id", None)
        if chat_id is None:
            continue
        if account_id and is_system_peer("telegram", account_id, str(chat_id)):
            continue
        ident = tg_peer_identity(chat)
        top = getattr(dialog, "top_message", None)
        rows.append({
            "jid": str(chat_id),
            "name": ident["name"] or str(chat_id),
            "ts": _epoch(getattr(top, "date", None)),
            "unread": _pos_int(getattr(dialog, "unread_messages_count", 0), 0),
            "is_group": _chat_is_group(chat),
        })
        seen += 1
        if pace_sec > 0 and seen % _DIALOG_PAGE == 0:
            await asyncio.sleep(pace_sec)
    return rows


# ── 观测埋点（best-effort）───────────────────────────────────────────────────
#
# 统计口径见 ``src/integrations/directory_sync_stats.py``。这两个 helper 只做一件事：
# 把异常吃干净。本模块是无人值守的后台任务，一个计数器的 ImportError 没理由让整轮
# 名单同步作废——本仓惯例（埋点 try/except 包裹）在此处尤其硬。
# ``platform`` 恒传 "telegram"（本模块 TG 专用），统计侧保留该维度供 WA/LINE 复用。

def _record_sync(account_id: str, stats: Dict[str, int]) -> None:
    try:
        from src.integrations.directory_sync_stats import get_directory_sync_stats
        get_directory_sync_stats().record_sync(
            "telegram", account_id,
            contacts=stats.get("contacts", 0), chats=stats.get("chats", 0))
    except Exception:  # noqa: BLE001
        logger.debug("[目录同步] 埋点失败 account=%s（不影响同步）", account_id,
                     exc_info=True)


def _record_failure(account_id: str, stage: str) -> None:
    try:
        from src.integrations.directory_sync_stats import get_directory_sync_stats
        get_directory_sync_stats().record_failure("telegram", account_id, stage)
    except Exception:  # noqa: BLE001
        logger.debug("[目录同步] 失败埋点失败 account=%s stage=%s（不影响同步）",
                     account_id, stage, exc_info=True)


# ── 一轮同步（落库）─────────────────────────────────────────────────────────

async def sync_directory_once(
    client: Any, account_id: str, cfg: Dict[str, Any],
) -> Dict[str, int]:
    """一轮目录同步：拉名单 + 拉会话 → 经 ``get_inbox_store()`` 直调落库。

    返回 ``{"contacts": n, "chats": n}``（store 实际处理条数；未启用/store 未就绪
    /无数据均为 0）。两段各自 best-effort：一段 RPC 挂掉不该让另一段陪葬，
    否则下次补齐要等满一个 6 小时周期。
    """
    stats = {"contacts": 0, "chats": 0}
    account_id = str(account_id or "")
    cfg = cfg if isinstance(cfg, dict) else {}
    if client is None or not account_id or not sync_enabled(cfg):
        return stats
    # 观测埋点全程 best-effort（见下方三处 _record_*）：统计模块坏掉/未装载绝不能
    # 拖垮一个 6 小时才跑一轮的同步——宁可指标缺一轮，不可名单缺一轮。

    from src.integrations.protocol_bridge import get_inbox_store
    store = get_inbox_store()
    if store is None:
        logger.debug("[目录同步] inbox store 未就绪，跳过本轮 account=%s", account_id)
        return stats

    contacts: List[Dict[str, Any]] = []
    try:
        contacts = await fetch_contact_rows(client, _pos_int(cfg.get("max_contacts"), 3000))
        if contacts:
            stats["contacts"] = store.upsert_protocol_contacts("telegram", account_id, contacts)
            logger.info("[目录同步] 通讯录同步 %d 条 account=%s", stats["contacts"], account_id)
    except Exception:  # noqa: BLE001
        logger.warning("[目录同步] 好友名单拉取失败 account=%s（本轮跳过）", account_id,
                       exc_info=True)
        _record_failure(account_id, "contacts")

    try:
        dialogs = await fetch_dialog_rows(
            client, _pos_int(cfg.get("max_dialogs"), 500),
            pace_sec=float(cfg.get("pace_seconds", DEFAULT_PACE_SECONDS) or 0),
            account_id=account_id,
        )
        rows: List[Dict[str, Any]] = list(dialogs)
        # 好友建会话占位只对小号做：这样工作台立刻能主动发起对话；大号（上千好友）
        # 全建会话会把列表灌成噪音，那种情况只留通讯录（新消息到了自然冒出会话）。
        # —— 与 LineProtocolWorker._sync_bootstrap_blocking 同一护栏、同一配置语义。
        seed_max = _pos_int(cfg.get("seed_chats_max", 200), 200)
        if contacts and seed_max and len(contacts) <= seed_max:
            known = {r["jid"] for r in dialogs}
            rows += [{"jid": r["jid"], "name": r.get("name") or ""}
                     for r in contacts if r["jid"] not in known]
        elif contacts and seed_max:
            logger.info(
                "[目录同步] 好友 %d 个 > seed_chats_max=%d：只同步通讯录，不建会话占位",
                len(contacts), seed_max)
        if rows:
            stats["chats"] = store.upsert_protocol_chats("telegram", account_id, rows)
            logger.info("[目录同步] 会话占位同步 %d 条（其中云端会话 %d）account=%s",
                        stats["chats"], len(dialogs), account_id)
    except Exception:  # noqa: BLE001
        logger.warning("[目录同步] 会话列表拉取失败 account=%s（本轮跳过）", account_id,
                       exc_info=True)
        _record_failure(account_id, "dialogs")

    _record_sync(account_id, stats)
    return stats
