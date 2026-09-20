# -*- coding: utf-8 -*-
"""账号栏未读徽标的聚合口径（#159 幽灵未读除根，2026-09-03 证据包 G4BYWH）。

事故：skuio 机 steven 号（tg 7092595256）头像、侧栏、会话入口三处都显示 7 条
待处理，清单里一条都没有。三处数字同源于 ``sum_effective_unread_by_account``
（store.py L2537），而清单走的是另一条路——``_collect_chats_from_store`` 取行
之后，还要过一串**清单专属**的剔除：

  · 会话删除墓碑（坐席删过的会话，占位复活前不该再计数）；
  · 消息全被软删（``messages.deleted_at``：对端撤回/坐席清理后一条不剩，
    清单里这条会话是空的，未读却还挂着）；
  · 协议号手机端未读残值（本地零条消息的纯占位行——「有 7 条未读但一条
    消息都没有」正是 G4BYWH 的形状）；
  · 已归档 / 群会话（这两条 store 侧已经在管，本模块保持同口径）。

徽标少了这几道，于是「三处显示 7、清单空」。本模块按**清单同一口径**重新
聚合 by_account / by_platform，与 read_routes L1042 注释的 effective_unread
语义一致。刻意独立成模块而非改 store.py：口径属于读路径的展示决定（哪些
会话「算数」），store 该只管「库里有什么」；而且并行改动集中在 store.py，
新查询另起一处更安全。

只读：全程 SELECT，绝不写库。任何异常 → 返回空 dict，调用方回落旧聚合/
客户端窗口求和（宁可少一个红点，绝不因为聚合本身出错把收件箱打挂）。
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

#: 与 store._VISIBLE_UNREAD_WHERE 同口径的私聊白名单（群/频道不进主徽标——
#: 群未读有群组动态区自己的徽章）。chat_type 是迁移列，存量群可能仍顶着
#: 'private'，故叠 legacy 群启发式。
_PRIVATE_ONLY = (
    "c.chat_type IN ('private', '') "
    "AND NOT (c.platform = 'telegram' AND c.chat_key GLOB '-[0-9]*') "
    "AND NOT (c.platform = 'line' AND ("
    "LOWER(c.chat_key) LIKE '%:group:%' "
    "OR LOWER(c.chat_key) LIKE '%:room:%'))"
)

#: 群 / 频道桶（#222 第三轮，2026-09-06 FF5PGS）：skuio 机 steven 号角标 5、私聊
#: 列表零未读，同号群组 11 / 频道 2 / 归档 16——坐席看到一个数字却不知道它数的
#: 是哪一类。三桶与 ``_PRIVATE_ONLY`` 互补：legacy 群启发式（chat_type 仍顶着
#: 'private'/'' 的 Telegram 负号 / LINE :group: :room:）归进 group 桶，与前端
#: ``store_row_to_chat → infer_chat_type`` 对同一行的判定一致（否则「角标算群、
#: 列表算私聊」又是两套口径）。AutoDraft ``skip_group_chats=True`` 时群/频道只进
#: 不出天然堆积，所以它们**永不进主徽标**，只在悬浮明细 / 群视图角标里单列。
_GROUP_ONLY = (
    "(c.chat_type = 'group' OR (c.chat_type IN ('private', '') AND ("
    "(c.platform = 'telegram' AND c.chat_key GLOB '-[0-9]*') "
    "OR (c.platform = 'line' AND (LOWER(c.chat_key) LIKE '%:group:%' "
    "OR LOWER(c.chat_key) LIKE '%:room:%')))))"
)
_CHANNEL_ONLY = "c.chat_type = 'channel'"

#: 视图 scope → WHERE。键与前端 ``_convScope``（private | group | channel）同名：
#: 角标数字 = 当前列表 scope 内的未读，浮层 ``/account-unread?scope=`` 同一份 WHERE。
SCOPE_WHERE: Dict[str, str] = {
    "private": _PRIVATE_ONLY,
    "group": _GROUP_ONLY,
    "channel": _CHANNEL_ONLY,
}


def normalize_scope(scope: Any) -> str:
    """未知 / 空 scope 一律回落 private（主徽标口径），绝不抛。"""
    s = str(scope or "").strip().lower()
    return s if s in SCOPE_WHERE else "private"

#: 有效未读的判据（unread>0 且入站闸门 ts 晚于已读水位，闸门=last_in_ts，
#: 0 回落 last_ts）。与 store.effective_unread / normalizer._effective_unread_from_row
#: 同口径，三处必须同改。
_HAS_UNREAD = (
    "c.unread > 0 AND COALESCE(NULLIF(c.last_in_ts, 0), c.last_ts) "
    "> COALESCE(c.last_read_ts, 0)"
)
_EFFECTIVE_UNREAD = f"CASE WHEN {_HAS_UNREAD} THEN c.unread ELSE 0 END"

#: 清单可见性闸（本模块相对 store 聚合多出来的两道，正是 #159 的差额）：
#: ① 未被删除墓碑覆盖；② 本地至少有一条未软删消息。
#: ②同时覆盖了「协议号纯占位行」（零消息）与「消息全被软删」两种形态——
#: 判据都是「点开这条会话，坐席看得见东西吗」，看不见就不该有红点催他去看。
#:
#: 顺序有讲究：两条 EXISTS 都是**相关子查询**（每行一次索引探查），而绝大多数
#: 会话根本没有未读、对结果毫无贡献。把 `_HAS_UNREAD` 摆在最前当短路前置，
#: 让子查询只对「真有未读」的少数行跑（SQLite 的 AND 左到右短路求值，这个
#: 顺序本身就是优化）。30k 会话 × 15 消息实测（未读占比 5%，接近真机）：
#: 166ms → 18ms，比旧的 store 全库聚合（39ms）还快——多两道闸不等于更慢，
#: 因为闸把大头行挡在子查询之外了。未读占比 50% 的极端构造下 64ms，仍可接受。
_LISTABLE = (
    f"({_HAS_UNREAD}) "
    "AND NOT EXISTS (SELECT 1 FROM conversation_tombstones t "
    "WHERE t.conversation_id = c.conversation_id) "
    "AND EXISTS (SELECT 1 FROM messages m "
    "WHERE m.conversation_id = c.conversation_id AND m.deleted_at = 0)"
)


#: 「该会话还有没有可见消息」的覆盖索引：`_LISTABLE` 的 EXISTS 子查询按
#: (conversation_id, deleted_at) 探查，既有 idx_msg_conv_ts 是
#: (conversation_id, ts DESC)——能定位到会话但要回表读 deleted_at。补这条后
#: 子查询走覆盖索引，30k 会话实测再省一截。建在本模块而非 store.py 的迁移表里：
#: 索引是本模块查询形状的附属品，与口径同生共死，放一起才不会「改了查询忘了索引」。
_IDX_DDL = ("CREATE INDEX IF NOT EXISTS idx_msg_conv_live"
            " ON messages(conversation_id, deleted_at)")

_ensured: set = set()
_ensure_lock = threading.Lock()


def _ensure_index(store: Any) -> None:
    """建覆盖索引（每个 store 实例只跑一次）。失败静默——索引只影响快慢
    不影响对错，建不上照样出正确结果。"""
    key = id(store)
    with _ensure_lock:
        if key in _ensured:
            return
        _ensured.add(key)       # 先占位：失败也不重试，别每轮轮询都试一次
    try:
        with store._lock:                    # noqa: SLF001
            store._conn.execute(_IDX_DDL)    # noqa: SLF001
            store._conn.commit()             # noqa: SLF001
    except Exception:
        logger.debug("[unread_aggregate] 覆盖索引创建失败（只影响快慢）",
                     exc_info=True)


def _rows(store: Any, sql: str, params: List[Any]) -> List[Any]:
    """在 store 自己的连接与锁上跑只读查询（绝不另开连接绕过 WAL 纪律）。"""
    _ensure_index(store)
    with store._lock:                        # noqa: SLF001（读路径复用同一把锁）
        return store._conn.execute(sql, params).fetchall()   # noqa: SLF001


def unread_maps(
    store: Any, *, include_archived: bool = False, scope: str = "private",
) -> Optional[Tuple[Dict[str, int], Dict[str, int]]]:
    """按清单口径聚合有效未读 → ``(by_account, by_platform)``；失败返回 None。

    ``by_account`` 键＝``platform:account_id``；``by_platform`` 为各账号合计。
    只返回 >0 的桶（零未读账号省略，调用方按 0 处理）。
    ``include_archived=True``＝``inbox.badge.include_archived`` 显式回退旧口径
    （归档未读也计入主徽标）。``scope``（#222）＝private | group | channel，
    与前端列表 scope 同名；缺省 private＝主徽标口径不变。

    **两个空 dict 与 None 是两件事**：前者＝这台机器此刻确实一条未读都没有
    （#159 的正解形态：旧口径说 7、清单口径说 0，就该显示 0），后者＝聚合本身
    没跑成（store 不可用/缺表/查询异常），调用方才该回落旧口径。把「真的是 0」
    当失败会让幽灵数字原样复活，这条区分是本次修复的关键。绝不抛。
    """
    by_acct: Dict[str, int] = {}
    by_plat: Dict[str, int] = {}
    if store is None:
        return None
    sql = (
        "SELECT c.platform AS p, c.account_id AS a, "
        f"COALESCE(SUM({_EFFECTIVE_UNREAD}), 0) AS n "
        "FROM conversations c "
        "LEFT JOIN conversation_meta m ON m.conversation_id = c.conversation_id "
        f"WHERE {SCOPE_WHERE[normalize_scope(scope)]} AND {_LISTABLE}"
    )
    if not include_archived:
        sql += " AND COALESCE(m.archived, 0) = 0"
    sql += " GROUP BY c.platform, c.account_id HAVING n > 0"
    try:
        rows = _rows(store, sql, [])
    except Exception:
        logger.debug("[unread_aggregate] 清单口径未读聚合失败", exc_info=True)
        return None
    for r in rows:
        n = int(r["n"] or 0)
        if n <= 0:
            continue
        p = str(r["p"] or "web")
        a = str(r["a"] or "default")
        by_acct[f"{p}:{a}"] = n
        by_plat[p] = int(by_plat.get(p) or 0) + n
    return by_acct, by_plat


def unread_conversations(
    store: Any, platform: str, account_id: str, *,
    include_archived: bool = False, limit: int = 200, scope: str = "private",
) -> List[Dict[str, Any]]:
    """该账号「算进徽标」的那 N 条会话（数字可点直达的数据源）。

    #159 的另一半：坐席看到 7 却找不到那 7 条，只能怀疑系统在骗人。徽标既然
    是个数字，就该能点开看见它数的是谁——本函数与 :func:`unread_maps` 用**同一
    份 WHERE**（差别只在 SUM vs 明细），所以「点进去看到的条数」与徽标恒等；
    对不上就是真 bug，而不是两套口径各说各话。``scope`` 与 :func:`unread_maps`
    同义（#222：群视图里点角标看到的就是群未读）。

    返回按未读数降序、其次最近活跃：``[{conversation_id, platform, account_id,
    chat_key, display_name, unread, last_ts, last_text}]``。异常 → 空列表。
    """
    p = str(platform or "").strip().lower()
    a = str(account_id or "").strip()
    if store is None or not p:
        return []
    try:
        cap = max(1, min(500, int(limit or 200)))
    except (TypeError, ValueError):
        cap = 200
    sql = (
        "SELECT c.conversation_id, c.platform, c.account_id, c.chat_key, "
        "c.display_name, c.last_ts, c.last_text, "
        f"{_EFFECTIVE_UNREAD} AS eff "
        "FROM conversations c "
        "LEFT JOIN conversation_meta m ON m.conversation_id = c.conversation_id "
        f"WHERE {SCOPE_WHERE[normalize_scope(scope)]} AND {_LISTABLE} "
        "AND c.platform = ?"
    )
    params: List[Any] = [p]
    if a:
        sql += " AND c.account_id = ?"
        params.append(a)
    if not include_archived:
        sql += " AND COALESCE(m.archived, 0) = 0"
    sql += " AND eff > 0 ORDER BY eff DESC, c.last_ts DESC LIMIT ?"
    params.append(cap)
    try:
        rows = _rows(store, sql, params)
    except Exception:
        logger.debug("[unread_aggregate] 未读会话明细失败 %s:%s", p, a,
                     exc_info=True)
        return []
    return [{
        "conversation_id": str(r["conversation_id"] or ""),
        "platform": str(r["platform"] or ""),
        "account_id": str(r["account_id"] or "default"),
        "chat_key": str(r["chat_key"] or ""),
        "display_name": str(r["display_name"] or ""),
        "unread": int(r["eff"] or 0),
        "last_ts": float(r["last_ts"] or 0.0),
        "last_text": str(r["last_text"] or ""),
    } for r in rows]


#: 明细四桶：private / group / channel 为**未归档**有效未读；archived 为私聊口径的
#: 归档未读（与 ``sum_archived_unread_by_account`` / 被埋横幅同一全集）。
BREAKDOWN_KEYS = ("private", "group", "channel", "archived")


def unread_breakdown(store: Any) -> Optional[Dict[str, Dict[str, int]]]:
    """按账号拆「角标旁悬浮明细」四桶 → ``{"plat:aid": {private, group, channel,
    archived}}``；失败返回 None（调用方不带该字段，前端回落无明细）。

    #222 口径统一的另一半：主徽标只数私聊，但坐席看到「群组 11 / 频道 2 /
    已归档 16」却不知道哪里还有未读——一次 GROUP BY 把四桶都算出来，前端悬浮
    「含群 N / 频道 N / 归档 N」，群视图角标直接读 group 桶。全部走 ``_LISTABLE``
    （墓碑 / 零可见消息剔除），与主徽标同一闸门。只返回至少一桶 >0 的账号。
    """
    if store is None:
        return None
    arch0 = "COALESCE(m.archived, 0) = 0"

    def _bucket(where: str, archived: str, alias: str) -> str:
        return (f"COALESCE(SUM(CASE WHEN {where} AND {archived} "
                f"THEN {_EFFECTIVE_UNREAD} ELSE 0 END), 0) AS {alias}")

    sql = (
        "SELECT c.platform AS p, c.account_id AS a, "
        + ", ".join([
            _bucket(_PRIVATE_ONLY, arch0, "n_private"),
            _bucket(_GROUP_ONLY, arch0, "n_group"),
            _bucket(_CHANNEL_ONLY, arch0, "n_channel"),
            _bucket(_PRIVATE_ONLY, "COALESCE(m.archived, 0) = 1", "n_archived"),
        ])
        + " FROM conversations c "
        "LEFT JOIN conversation_meta m ON m.conversation_id = c.conversation_id "
        f"WHERE {_LISTABLE} GROUP BY c.platform, c.account_id "
        "HAVING n_private > 0 OR n_group > 0 OR n_channel > 0 OR n_archived > 0"
    )
    try:
        rows = _rows(store, sql, [])
    except Exception:
        logger.debug("[unread_aggregate] 未读四桶明细聚合失败", exc_info=True)
        return None
    out: Dict[str, Dict[str, int]] = {}
    for r in rows:
        key = f"{str(r['p'] or 'web')}:{str(r['a'] or 'default')}"
        out[key] = {
            "private": int(r["n_private"] or 0),
            "group": int(r["n_group"] or 0),
            "channel": int(r["n_channel"] or 0),
            "archived": int(r["n_archived"] or 0),
        }
    return out


def buried_conversations(
    store: Any, *, min_unread: int = 1, limit: int = 100,
    platform: str = "", account_id: str = "",
) -> Optional[List[Dict[str, Any]]]:
    """「被埋会话」（归档着却有有效未读）清单——**与横幅 / 主徽标同一口径**。

    #222 第三轮：看门狗此前走 ``store.list_buried_archived``（全表 raw
    ``unread>=1``，含群 / 频道 / 墓碑 / 零可见消息行），而工作台横幅走
    ``sum_archived_unread_by_account``（私聊 + 有效未读）——「被埋 9 个」与横幅
    「N 条」各说各话。本函数把被埋清单收进同一份 WHERE：私聊 + ``_LISTABLE`` +
    archived=1 + 有效未读；群 / 频道 skip_groups 只进不出的堆积不再被当成「客户
    在等」。行形状兼容 ``list_buried_archived``（多带 chat_type / last_in_ts）。
    返回 None＝查询没跑成（调用方回落旧口径）；[]＝确实没有被埋。
    """
    if store is None:
        return None
    try:
        n = max(1, int(min_unread))
    except (TypeError, ValueError):
        n = 1
    try:
        cap = max(1, min(500, int(limit)))
    except (TypeError, ValueError):
        cap = 100
    sql = (
        "SELECT c.conversation_id, c.platform, c.account_id, c.chat_key, "
        "c.display_name, c.chat_type, c.last_ts, c.last_in_ts, c.last_text, "
        f"{_EFFECTIVE_UNREAD} AS eff, m.archived_at, m.auto_archived_at "
        "FROM conversations c "
        "JOIN conversation_meta m ON m.conversation_id = c.conversation_id "
        f"WHERE {_PRIVATE_ONLY} AND {_LISTABLE} AND COALESCE(m.archived, 0) = 1"
    )
    params: List[Any] = []
    p = str(platform or "").strip().lower()
    a = str(account_id or "").strip()
    if p:
        sql += " AND c.platform = ?"
        params.append(p)
    if a:
        sql += " AND c.account_id = ?"
        params.append(a)
    sql += " AND eff >= ? ORDER BY eff DESC, c.last_ts DESC LIMIT ?"
    params += [n, cap]
    try:
        rows = _rows(store, sql, params)
    except Exception:
        logger.debug("[unread_aggregate] 被埋会话清单失败", exc_info=True)
        return None
    out: List[Dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        d["unread"] = int(d.pop("eff", 0) or 0)
        out.append(d)
    return out


def sweep_buried_unread(
    store: Any, *, min_idle_sec: float = 0.0, manual_only: bool = False,
    platform: str = "", account_id: str = "", now: Optional[float] = None,
    limit: int = 500, reason: str = "startup_sweep",
) -> int:
    """把被埋未读标已读（推已读水位，与打开会话同一机制，永不回弹）。

    两个调用方（#222 修法 3）：
    - 看门狗启动一次：``min_idle_sec=72h, manual_only=True``——「人工归档 + 无入站
      > 72h」＝坐席明示收尾、客户此后再没开口的存量，横幅挂着只剩噪音；<72h 的
      仍留给 L-3 B 横幅（客户可能刚开口）。自动归档的不动：那是策略把活跃会话
      判死，该调 ``idle_hours`` 而不是替它清账。
    - 横幅「全部标已读」：``min_idle_sec=0``，按当前平台 / 账号视角清。
    「无入站」按 ``last_in_ts``（0 回落 ``last_ts``）算。返回清掉的会话数；
    任何异常 → 0（卫生路径绝不抛）。日志 ``[buried] <reason> cleared N``。
    """
    if store is None or not hasattr(store, "mark_conversation_read"):
        return 0
    rows = buried_conversations(store, min_unread=1, limit=limit,
                                platform=platform, account_id=account_id)
    if not rows:
        return 0
    ts = float(now if now is not None else time.time())
    idle = max(0.0, float(min_idle_sec or 0))
    cleared = 0
    for r in rows:
        try:
            if manual_only and float(r.get("auto_archived_at") or 0) > 0:
                continue
            gate = float(r.get("last_in_ts") or 0) or float(r.get("last_ts") or 0)
            if idle > 0 and (gate <= 0 or ts - gate < idle):
                continue
            if store.mark_conversation_read(str(r.get("conversation_id") or "")):
                cleared += 1
        except Exception:
            logger.debug("[buried] 标已读失败 %s（忽略）", r.get("conversation_id"),
                         exc_info=True)
    if cleared:
        logger.info("[buried] %s cleared %d（人工归档=%s, 无入站>%.0fh, scope=%s:%s）",
                    reason, cleared, manual_only, idle / 3600.0,
                    platform or "*", account_id or "*")
    return cleared


def phantom_unread_report(store: Any) -> Dict[str, Any]:
    """徽标口径 vs store 全库口径的**差额**（幽灵未读的可观测化）。

    值守侧要能回答「这台机器现在有没有 #159 的病」，而不是等客户截图。差额
    ＝被本模块新闸门剔掉的那部分：墓碑会话 / 一条可见消息都没有的会话。
    返回 ``{"badge": 徽标合计, "store": 旧口径合计, "phantom": 差额,
    "by_account": {key: 该账号差额}}``；异常 → 全 0 空表（诊断口绝不抛）。
    """
    out: Dict[str, Any] = {"badge": 0, "store": 0, "phantom": 0,
                           "by_account": {}}
    if store is None:
        return out
    try:
        maps = unread_maps(store)
        if maps is None:
            return out
        badge = maps[0]
        legacy_raw = store.sum_effective_unread_by_account() or {}
    except Exception:
        logger.debug("[unread_aggregate] 幽灵未读对账失败", exc_info=True)
        return out
    legacy = {f"{str(p or 'web')}:{str(a or 'default')}": int(n or 0)
              for (p, a), n in legacy_raw.items()}
    diff: Dict[str, int] = {}
    for key in set(legacy) | set(badge):
        d = int(legacy.get(key, 0)) - int(badge.get(key, 0))
        if d > 0:
            diff[key] = d
    out["badge"] = sum(badge.values())
    out["store"] = sum(legacy.values())
    out["phantom"] = sum(diff.values())
    out["by_account"] = diff
    return out


def purge_orphan_unread(store: Any) -> int:
    """孤儿未读归零（#170 修法 4，启动时跑一次）：会话**已删除**（有墓碑）却仍带
    ``unread>0``——徽标口径（``_LISTABLE``）早已剔除它们，但旧全库口径 /
    ``phantom_unread_report`` 的差额 / 第三方读数还会把它算进「未读」。

    只动墓碑会话（删除是坐席的明示决定，未读计数对它没有任何行动价值），
    「一条可见消息都没有」的占位会话**不动**——那是历史尚未回流的正常中间态，
    回流后未读应当照常亮起。返回归零的会话数；异常 → 0（卫生路径绝不抛）。
    """
    if store is None:
        return 0
    try:
        with store._lock:                                    # noqa: SLF001
            cur = store._conn.execute(                       # noqa: SLF001
                "UPDATE conversations SET unread = 0 "
                "WHERE unread > 0 AND EXISTS (SELECT 1 FROM conversation_tombstones t "
                "WHERE t.conversation_id = conversations.conversation_id)")
            n = int(cur.rowcount or 0)
            if n:
                store._conn.commit()                         # noqa: SLF001
        if n:
            logger.info("[unread_aggregate] 孤儿未读归零：%d 个已删会话的 unread 清 0", n)
        return n
    except Exception:
        logger.debug("[unread_aggregate] 孤儿未读归零失败（忽略）", exc_info=True)
        return 0


__all__ = ["BREAKDOWN_KEYS", "SCOPE_WHERE", "buried_conversations", "normalize_scope",
           "phantom_unread_report", "purge_orphan_unread", "sweep_buried_unread",
           "unread_breakdown", "unread_conversations", "unread_maps"]
