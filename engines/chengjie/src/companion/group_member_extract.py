"""Telegram 群成员提取 —— 纯函数过滤/分片 + 异步提取编排。

分两层，便于测试与复用：

**纯函数**（不碰网络/client，可直接单测）：过滤判定 ``should_keep``、多号分片
``member_in_shard``、当地零点 ``local_midnight_ts``、pyrogram user → 成员行
``user_row``（鸭子类型，测试可传 fake）。

**异步编排** ``run_extraction``：吃一个**已归一的 raw pyrogram client**（由路由经
``_get_tg_pyro_for_account`` 取到），在该 client 的事件循环上跑。职责：
1. resolve 群 → 规范 group_id（chat.id）+ 标题；
2. 取管理员 id 集（剔管理员用）；
3. 按过滤器取候选人——``spoke*`` 走群历史扫描收「发过言的人」，``all`` 走成员枚举；
4. 过滤 + 分片 + **每日配额**（cap - 今日已入库）截断；
5. ``INSERT OR IGNORE`` 批量入库，累加任务计数；
6. FloodWait 退避（限次）、``stop_requested`` 可中断；
7. 任务状态 running → done/stopped/error。

风控哲学：提取是**只读**调用（GetParticipants / GetHistory），本身低危；真正会封号的
是拉完之后私聊陌生人（那是「触达」步骤，不在本模块）。这里只做「别把 GetParticipants
打成 FloodWait」级别的礼貌限速。
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set, Tuple

from src.companion.group_members_store import (
    FILTER_ALL,
    FILTER_SPOKE,
    FILTER_SPOKE_NO_ADMIN,
    JOB_DONE,
    JOB_ERROR,
    JOB_RUNNING,
    JOB_STOPPED,
)

logger = logging.getLogger("ai_chat_assistant.group_member_extract")

# 每轮请求之间的礼貌间隔（秒，带抖动）——GetHistory/GetParticipants 分页之间稍歇。
_MIN_GAP_SEC = 0.6
_MAX_GAP_SEC = 1.4
# FloodWait 单次最多等这么久（超过就放弃本轮，标 error 让运营改天再拉）。
_FLOODWAIT_CAP_SEC = 300
# 单次运行最多容忍几次 FloodWait（防被群反复限速时死磕）。
_FLOODWAIT_MAX_HITS = 3

# 进程内「同一任务同时只跑一次」闸（防重复点击/双窗口重复调度同一 job）。
_RUNNING_JOBS: Set[str] = set()


# ── 纯函数 ────────────────────────────────────────────────────────────────

def local_midnight_ts(now: Optional[float] = None) -> float:
    """当地时区今天零点的 epoch 秒（每日配额窗的起点）。"""
    now = time.time() if now is None else now
    lt = time.localtime(now)
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))


def member_in_shard(user_id: Any, shard_index: int, num_shards: int) -> bool:
    """多号协同分片：``user_id % num_shards == shard_index`` 才归本号拉（「每个拉不同批次」）。

    单号（num_shards<=1）恒 True；不可解析为整数的 id 归第 0 片（不丢人）。
    """
    if num_shards <= 1:
        return True
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return shard_index == 0
    return (uid % int(num_shards)) == int(shard_index)


def should_keep(*, filter_mode: str, spoke: bool, is_admin: bool,
                is_bot: bool, is_self: bool, is_deleted: bool = False) -> bool:
    """成员是否入库的过滤判定（纯函数）。

    - 恒剔除：机器人、自己、已注销账号（对触达无意义）。
    - ``all``：其余全留。
    - ``spoke``：只留发过言的。
    - ``spoke_no_admin``：发过言且非管理员（默认，最贴「可聊的活人」）。
    """
    if is_bot or is_self or is_deleted:
        return False
    if filter_mode == FILTER_ALL:
        return True
    if not spoke:
        return False
    if filter_mode == FILTER_SPOKE_NO_ADMIN and is_admin:
        return False
    return True


def score_member(*, spoke: bool, is_admin: bool, is_bot: bool,
                 username: str = "", first_name: str = "",
                 last_name: str = "") -> int:
    """成员「可聊度/意向分」0-100（确定性静态信号）。

    解决「拉太多聊不完」：同一批人里先聊**最可能回**的。信号权重——
    发过言(活人) +25 > 有用户名(可搜索/更像活跃真人) +15 > 有名/姓(真实档) +10/+5；
    管理员 -20（通常不宜私聊触达）；bot 直接 0。刻意**不含时间衰减**——分数稳定可复现，
    「谁更近发言」交给 ``last_spoke_ts`` 排序兜底（不把易变量塞进存量分数）。
    """
    if is_bot:
        return 0
    s = 40
    if spoke:
        s += 25
    if str(username or "").strip():
        s += 15
    if str(first_name or "").strip():
        s += 10
    if str(last_name or "").strip():
        s += 5
    if is_admin:
        s -= 20
    return max(0, min(100, s))


def user_row(user: Any, *, group_id: str, group_title: str, spoke: bool,
             is_admin: bool, source_account_id: str, job_id: str,
             batch_id: str, now: Optional[float] = None,
             last_msg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """pyrogram ``User``（或鸭子类型对象）→ store 成员行 dict（含可聊度分）。

    ``last_msg``：``{text, ts}`` 他在群里最近那条（collect_speaker_context 给）；有真实
    时间就用它做 ``last_spoke_ts``，没有才退回提取时刻。
    """
    now = time.time() if now is None else now
    lm = last_msg or {}
    lm_text = " ".join(str(lm.get("text") or "").split())[:200]
    try:
        lm_ts = float(lm.get("ts") or 0.0)
    except (TypeError, ValueError):
        lm_ts = 0.0
    _lang = str(getattr(user, "language_code", "") or "")[:16]
    _uname = str(getattr(user, "username", "") or "")
    _fn = str(getattr(user, "first_name", "") or "")
    _ln = str(getattr(user, "last_name", "") or "")
    _bot = bool(getattr(user, "is_bot", False))
    _ah = ""
    try:
        raw_ah = getattr(user, "access_hash", None)
        if raw_ah:
            _ah = str(int(raw_ah))
            if _ah == "0":
                _ah = ""
    except (TypeError, ValueError):
        _ah = ""
    return {
        "group_id": str(group_id),
        "user_id": str(getattr(user, "id", "") or ""),
        "username": _uname,
        "first_name": _fn,
        "last_name": _ln,
        "is_admin": bool(is_admin),
        "is_bot": _bot,
        "spoke": bool(spoke),
        "last_spoke_ts": (lm_ts or now) if spoke else 0.0,
        "last_msg_text": lm_text if spoke else "",
        "last_msg_ts": lm_ts if spoke else 0.0,
        "lang_code": _lang,
        "group_title": str(group_title or ""),
        "source_account_id": str(source_account_id),
        "job_id": str(job_id),
        "batch_id": str(batch_id),
        "outreach_state": "none",
        "extracted_at": now,
        "score": score_member(spoke=spoke, is_admin=is_admin, is_bot=_bot,
                              username=_uname, first_name=_fn, last_name=_ln),
        # access_hash 只属于当时拉到这个人的那个号，别的号不能拿去私聊。
        "access_hash": _ah,
        "hash_account_id": str(source_account_id or "") if _ah else "",
        "outreach_at": 0.0,
        "outreach_error": "",
        "outreach_account_id": "",
    }


async def fill_access_hashes(client: Any, rows: List[Dict[str, Any]], account_id: str) -> int:
    """补齐缺 access_hash 的行，返回补上的条数。

    pyrogram 2.x 的高层 ``User`` 不带 access_hash（user_row 读不到）；但拉成员 / 扫历史时
    pyrogram 已把这些人写进本号 session 的 peers 缓存——这里只读本地缓存，不发网络请求。
    """
    storage = getattr(client, "storage", None)
    getter = getattr(storage, "get_peer_by_id", None)
    if getter is None:
        return 0
    n = 0
    for r in rows:
        if r.get("access_hash"):
            continue
        try:
            peer = await getter(int(r.get("user_id") or 0))
            ah = int(getattr(peer, "access_hash", 0) or 0)
        except Exception:
            continue
        if ah:
            r["access_hash"] = str(ah)
            r["hash_account_id"] = str(account_id or "")
            n += 1
    return n


def _is_floodwait(exc: Exception) -> Optional[int]:
    """是否 pyrogram FloodWait；是则返回需等待秒数（否则 None）。

    用类名 + ``value`` 属性判定，避免对 pyrogram 硬依赖（测试可传 fake FloodWait）。
    """
    if type(exc).__name__ == "FloodWait":
        try:
            return int(getattr(exc, "value", 0) or getattr(exc, "x", 0) or 30)
        except Exception:
            return 30
    return None


# ── 异步 client 交互（薄封装，鸭子类型；测试可传 fake async client）──────────

async def _resolve_chat(client: Any, peer: Any) -> Dict[str, Any]:
    """resolve 群 → {id, title}。失败返回 {}（调用方回落输入 peer）。"""
    try:
        chat = await client.get_chat(peer)
    except Exception:
        logger.debug("[gm_extract] get_chat 失败 peer=%s", peer, exc_info=True)
        return {}
    return {"id": getattr(chat, "id", None), "title": getattr(chat, "title", "") or ""}


async def collect_admin_ids(client: Any, chat_id: Any) -> Set[int]:
    """群管理员 user_id 集（剔管理员用）。任何异常 → 空集（宁可不剔也不崩）。"""
    ids: Set[int] = set()
    flt = None
    try:
        from pyrogram.enums import ChatMembersFilter
        flt = ChatMembersFilter.ADMINISTRATORS
    except Exception:
        flt = None
    try:
        agen = (client.get_chat_members(chat_id, filter=flt) if flt is not None
                else client.get_chat_members(chat_id))
        async for m in agen:
            u = getattr(m, "user", None) or m
            uid = getattr(u, "id", None)
            if uid is not None:
                try:
                    ids.add(int(uid))
                except (TypeError, ValueError):
                    pass
    except Exception:
        logger.debug("[gm_extract] 取管理员失败 chat=%s", chat_id, exc_info=True)
    return ids


def _msg_snippet(msg: Any, limit: int = 200) -> str:
    """消息里能当「他说过什么」用的文字：text / caption；媒体无字就空。"""
    text = getattr(msg, "text", None) or getattr(msg, "caption", None) or ""
    s = " ".join(str(text).split())
    return s[:limit]


def _msg_ts(msg: Any) -> float:
    date = getattr(msg, "date", None)
    try:
        if hasattr(date, "timestamp"):
            return float(date.timestamp())
        if date:
            return float(date)
    except (TypeError, ValueError, OverflowError, OSError):
        pass
    return 0.0


async def collect_speaker_context(client: Any, chat_id: Any, scan_limit: int,
                                  self_id: int = 0) -> Tuple[Dict[int, Any], Dict[int, Dict[str, Any]]]:
    """扫群历史收「发过言的人」→ ({user_id: user}, {user_id: {text, ts}})。

    历史是从新到旧扫的，第一次遇到即他最近那条：把正文摘要和真实时间留下来，AI 开场
    靠这个写出「我看到你说了什么」。``scan_limit``=最多回扫多少条消息。剔自己/无 from_user。
    """
    seen: Dict[int, Any] = {}
    ctx: Dict[int, Dict[str, Any]] = {}
    try:
        async for msg in client.get_chat_history(chat_id, limit=int(scan_limit)):
            u = getattr(msg, "from_user", None)
            if u is None:
                continue
            uid = getattr(u, "id", None)
            if uid is None:
                continue
            try:
                uid = int(uid)
            except (TypeError, ValueError):
                continue
            if self_id and uid == int(self_id):
                continue
            if uid not in seen:
                seen[uid] = u
                ctx[uid] = {"text": _msg_snippet(msg), "ts": _msg_ts(msg)}
            elif not ctx[uid].get("text"):
                # 最近那条是纯媒体 → 往前找他最近一句有字的
                snip = _msg_snippet(msg)
                if snip:
                    ctx[uid]["text"] = snip
    except Exception as exc:
        wait = _is_floodwait(exc)
        if wait is not None:
            raise
        logger.debug("[gm_extract] 扫历史失败 chat=%s", chat_id, exc_info=True)
    return seen, ctx


async def collect_speakers(client: Any, chat_id: Any, scan_limit: int,
                           self_id: int = 0) -> Dict[int, Any]:
    """只要人不要话的旧口径（见 collect_speaker_context）。"""
    seen, _ctx = await collect_speaker_context(client, chat_id, scan_limit, self_id=self_id)
    return seen


async def list_account_groups(client: Any, *, limit: int = 200,
                              scan_cap: int = 800):
    """列该号所在的群/超级群（供管理台「群下拉」替代手输群 id）→ [{id,title,members}]。

    只读（get_dialogs）；扫描上限 scan_cap 防超大对话列表拖死；FloodWait/异常软失败，
    返回已收集的部分（下拉宁可少几个也不阻塞页面）。
    """
    out: List[Dict[str, Any]] = []
    seen: Set[str] = set()
    group_types = None
    try:
        from pyrogram.enums import ChatType
        group_types = {ChatType.GROUP, ChatType.SUPERGROUP}
    except Exception:
        group_types = None
    scanned = 0
    try:
        async for d in client.get_dialogs():
            scanned += 1
            if scanned > int(scan_cap):
                break
            chat = getattr(d, "chat", None)
            if chat is None:
                continue
            cid = getattr(chat, "id", None)
            if cid is None:
                continue
            ctype = getattr(chat, "type", None)
            if group_types is not None:
                is_group = ctype in group_types
            else:
                is_group = "group" in str(ctype).lower()
            if not is_group:
                continue
            key = str(cid)
            if key in seen:
                continue
            seen.add(key)
            out.append({"id": key, "title": str(getattr(chat, "title", "") or ""),
                        "members": getattr(chat, "members_count", None)})
            if len(out) >= int(limit):
                break
    except Exception as exc:
        if _is_floodwait(exc) is None:
            logger.debug("[gm_extract] 列群失败", exc_info=True)
    return out


async def iter_all_members(client: Any, chat_id: Any, self_id: int = 0):
    """枚举全部成员（``all`` 过滤器用）→ 逐个 yield user 对象。FloodWait 上抛由编排处理。"""
    agen = client.get_chat_members(chat_id)
    async for m in agen:
        u = getattr(m, "user", None) or m
        uid = getattr(u, "id", None)
        if uid is None:
            continue
        try:
            if self_id and int(uid) == int(self_id):
                continue
        except (TypeError, ValueError):
            pass
        yield u


# ── 编排 ─────────────────────────────────────────────────────────────────

async def run_extraction(client: Any, store: Any, job_id: str, *,
                         account: Optional[str] = None,
                         shard_index: Optional[int] = None,
                         num_shards: Optional[int] = None,
                         self_id: int = 0,
                         sleep: Optional[Callable[[float], Awaitable[None]]] = None,
                         rng: Optional[random.Random] = None) -> Dict[str, Any]:
    """执行一次提取（单号一次调用；多号协同 = 路由为每个号各调一次、各自 shard）。

    读 ``store.get_job(job_id)`` 为准（group_id/filter/daily_cap/scan_limit）。
    ``account`` 显式指定本次执行的号（缺省取 account_ids[0]）；``shard_index``/``num_shards``
    显式指定分片（缺省按 account 在 account_ids 里的下标 + 总数）——多号并行时按
    ``user_id % num_shards == shard_index`` 天然「每个拉不同批次」，互不重叠。
    并发 guard 按 ``(job_id, account)``：同一号重复调度被挡、不同号可并行。返回汇总 dict。
    """
    _sleep = sleep or asyncio.sleep
    _rng = rng or random
    summary: Dict[str, Any] = {"ok": False, "inserted": 0, "skipped": 0,
                               "admins_excluded": 0, "floodwaits": 0, "reason": ""}
    _runkey = "%s:%s" % (job_id, account or "")
    if _runkey in _RUNNING_JOBS:
        summary["reason"] = "already_running"
        return summary
    _RUNNING_JOBS.add(_runkey)
    try:
        job = store.get_job(job_id) if store is not None else None
        if not job:
            summary["reason"] = "job_not_found"
            return summary
        account_ids = list(job.get("account_ids") or [])
        account_id = str(account) if account else (
            str(account_ids[0]) if account_ids else "")
        if shard_index is None:
            try:
                shard_index = account_ids.index(account_id)
            except ValueError:
                shard_index = 0
        if num_shards is None:
            num_shards = max(1, len(account_ids))
        filter_mode = str(job.get("filter") or FILTER_SPOKE_NO_ADMIN)
        daily_cap = int(job.get("daily_cap_per_account") or 0)
        scan_limit = int(job.get("scan_limit") or 3000)
        peer_in = job.get("group_id") or ""

        store.update_job(job_id, status=JOB_RUNNING, last_error="")

        # 1) resolve 群 → 规范 id/title
        try:
            peer: Any = int(peer_in)
        except (TypeError, ValueError):
            peer = peer_in
        chat = await _resolve_chat(client, peer)
        group_id = str(chat.get("id") or peer_in)
        group_title = str(chat.get("title") or "")
        if chat.get("id") is not None:
            store.update_job(job_id, group_id=group_id, group_title=group_title)
        batch_id = "tg_member_pull:%s" % job_id

        # 2) 每日配额剩余（三层取最小：每号 / 每群 / 全局）。
        #    每群&全局是**跨号共享**额度，多号并行各自会看到「还没满」→ 按分片数均摊
        #    （ceil(剩余/num_shards)）分给每个并行号，合计有界不超总额（分片不相交，
        #    是保守近似，允许少量有界超发）。0 = 该层不限。
        midnight = local_midnight_ts()
        used_today = store.count_extracted_since(account_id, midnight)
        budget = (daily_cap - used_today) if daily_cap > 0 else 10 ** 9
        group_cap = int(job.get("group_daily_cap") or 0)
        if group_cap > 0:
            grp_rem = max(0, group_cap - store.count_extracted_for_group_since(group_id, midnight))
            budget = min(budget, -(-grp_rem // num_shards))   # ceil 均摊
        global_cap = int(job.get("global_daily_cap") or 0)
        if global_cap > 0:
            all_rem = max(0, global_cap - store.count_extracted_all_since(midnight))
            budget = min(budget, -(-all_rem // num_shards))
        if budget <= 0:
            store.update_job(job_id, status=JOB_DONE, finished_at=time.time())
            summary.update(ok=True, reason="daily_cap_reached")
            return summary

        # 3) 管理员集（spoke_no_admin 必需；spoke/all 也顺带标 is_admin）
        admin_ids: Set[int] = set()
        if filter_mode in (FILTER_SPOKE_NO_ADMIN, FILTER_SPOKE, FILTER_ALL):
            admin_ids = await collect_admin_ids(client, peer)

        floodwaits = 0

        async def _cool_floodwait(exc: Exception) -> bool:
            """命中 FloodWait → 退避；返回 True=已处理可继续，False=超限应放弃。"""
            nonlocal floodwaits
            wait = _is_floodwait(exc)
            if wait is None:
                return False
            floodwaits += 1
            store.bump_job_counters(job_id, floodwaits=1)
            if floodwaits > _FLOODWAIT_MAX_HITS or wait > _FLOODWAIT_CAP_SEC:
                return False
            await _sleep(min(wait, _FLOODWAIT_CAP_SEC))
            return True

        # 4) 取候选 → 过滤/分片/配额 → 入库
        pending: List[Dict[str, Any]] = []
        now = time.time()

        speaker_ctx: Dict[int, Dict[str, Any]] = {}

        def _consider(u: Any, spoke: bool) -> bool:
            """判定并暂存一个候选；返回是否已达配额（True=该停）。"""
            uid = getattr(u, "id", None)
            try:
                uid_i = int(uid)
            except (TypeError, ValueError):
                return False
            if not member_in_shard(uid_i, shard_index, num_shards):
                return False
            is_admin = uid_i in admin_ids
            keep = should_keep(
                filter_mode=filter_mode, spoke=spoke, is_admin=is_admin,
                is_bot=bool(getattr(u, "is_bot", False)),
                is_self=bool(self_id and uid_i == int(self_id)),
                is_deleted=bool(getattr(u, "is_deleted", False)),
            )
            if not keep:
                if spoke and is_admin and filter_mode == FILTER_SPOKE_NO_ADMIN:
                    summary["admins_excluded"] += 1
                return False
            pending.append(user_row(
                u, group_id=group_id, group_title=group_title, spoke=spoke,
                is_admin=is_admin, source_account_id=account_id, job_id=job_id,
                batch_id=batch_id, now=now, last_msg=speaker_ctx.get(uid_i)))
            return len(pending) >= budget

        try:
            if filter_mode in (FILTER_SPOKE, FILTER_SPOKE_NO_ADMIN):
                # 扫历史收发言人（可能 FloodWait，重试一次）
                speakers: Dict[int, Any] = {}
                for _attempt in range(_FLOODWAIT_MAX_HITS + 1):
                    if store.is_stop_requested(job_id):
                        break
                    try:
                        speakers, speaker_ctx = await collect_speaker_context(
                            client, peer, scan_limit, self_id=self_id)
                        break
                    except Exception as exc:
                        if not await _cool_floodwait(exc):
                            raise
                for u in speakers.values():
                    if store.is_stop_requested(job_id):
                        break
                    if _consider(u, spoke=True):
                        break
            else:
                async for u in iter_all_members(client, peer, self_id=self_id):
                    if store.is_stop_requested(job_id):
                        break
                    if _consider(u, spoke=False):
                        break
                    if len(pending) % 200 == 0:
                        await _sleep(_rng.uniform(_MIN_GAP_SEC, _MAX_GAP_SEC))
        except Exception as exc:
            if _is_floodwait(exc) is not None:
                store.update_job(job_id, status=JOB_ERROR,
                                 last_error="floodwait_exhausted",
                                 finished_at=time.time())
                summary.update(reason="floodwait_exhausted", floodwaits=floodwaits)
                return summary
            logger.warning("[gm_extract] 提取异常 job=%s", job_id, exc_info=True)
            store.update_job(job_id, status=JOB_ERROR, last_error=str(exc)[:200],
                             finished_at=time.time())
            summary["reason"] = "error"
            return summary

        await fill_access_hashes(client, pending, account_id)
        inserted, skipped = (store.record_members(pending) if pending else (0, 0))
        store.bump_job_counters(job_id, pulled=inserted, dedup=skipped,
                                admins=summary["admins_excluded"])
        stopped = store.is_stop_requested(job_id)
        store.update_job(job_id, status=(JOB_STOPPED if stopped else JOB_DONE),
                         finished_at=time.time())
        summary.update(ok=True, inserted=inserted, skipped=skipped,
                       floodwaits=floodwaits,
                       reason=("stopped" if stopped else "done"))
        return summary
    finally:
        _RUNNING_JOBS.discard(_runkey)


__all__ = [
    "local_midnight_ts", "member_in_shard", "should_keep", "score_member",
    "user_row", "collect_admin_ids", "collect_speakers", "collect_speaker_context",
    "list_account_groups", "fill_access_hashes",
    "iter_all_members", "run_extraction",
]
