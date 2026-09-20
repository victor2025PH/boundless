"""群戏场次落库（SQLite）—— 让一场戏可续演、可回放、可归因。

为什么必须落库
--------------
一场戏是**跨小时的长事务**：20 拍按泊松节奏铺开可能演一两个小时，中间必然遇到
进程重启、实例切换、真人插话让路、Kill-Switch 冻结。内存态一丢，轻则从头重演
（同一批号在同一个群把同一个话题又演一遍＝最刺眼的机器特征），重则演到一半
永远烂尾。落库解决三件事：

1. **断点续演**：``load_session`` 拿回 ``beat_cursor``（演到第几拍）+ ``events``
   （已经说过什么），导演接着往下演而不是重开；
2. **回放复盘**：整条事件流是自然度评测（:mod:`naturalness`）与人工复盘的唯一
   素材，也是将来做「生成质量漂移」回归门禁的语料源；
3. **转化归因**：哪场戏、哪一拍、哪个号带来了后续私聊转化，只能靠场次 id 串起来。

设计取舍（对齐 ``src.fatex.store`` / ``src.utils.isolation_trend_store``）
-----------------------------------------------------------------------
- **单连接 + Lock**，``check_same_thread=False``：本仓是多线程环境（web 线程 /
  worker 线程 / RPA runner 各有 loop），导演循环与看板读取会并发撞同一个库。
- **全方法软失败**：所有异常吞掉记 debug 日志。炒群链路正在真发消息，**绝不能
  因为落库失败把一场正在演的戏掀翻**——宁可丢一行审计，也不能丢半场戏。
  连 ``__init__`` 都不抛（库路径不可写 → ``available=False`` 全方法降级空转）。
- **类型不重复定义**：``ShowState`` / ``ShowEvent`` / ``Casting`` / ``CastMember``
  一律复用 :mod:`~src.companion.group_show.playbook` 的契约，本模块只做
  「对象 ↔ 行」的搬运与 JSON 编解码。
- **``save_session`` 顺带 flush 事件**：``(session_id, seq)`` 是主键、写入走
  upsert，重复 flush 幂等。这样即使 ``append_event`` 在崩溃前漏了一条，下一次
  整体保存也能补齐——续演的正确性优先于那点写放大。
- **剧本本体不入库**（只存 ``playbook_id``）：剧本是 YAML 单一事实源，拷一份进
  库就会出现「库里的旧剧本 vs 文件里的新剧本」双源真相。续演时按 id 重新载入。
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from src.companion.group_show.playbook import (
    SHOW_STATUSES, SPOKEN_KINDS, CastMember, Casting,
)

logger = logging.getLogger(__name__)

#: 默认库路径（与其它 config/*.db 同处）；双实例部署下随各实例 config 目录走，
#: 由启动期 ``configure_group_show_store`` 显式指定以保证实例隔离。
DEFAULT_DB_PATH = "config/group_show.db"

_DDL = """
CREATE TABLE IF NOT EXISTS choreography_sessions (
    session_id  TEXT NOT NULL PRIMARY KEY,
    group_key   TEXT NOT NULL DEFAULT '',
    platform    TEXT NOT NULL DEFAULT 'telegram',
    playbook_id TEXT NOT NULL DEFAULT '',
    cast_json   TEXT NOT NULL DEFAULT '{}',
    beat_cursor INTEGER NOT NULL DEFAULT 0,
    status      TEXT NOT NULL DEFAULT 'pending',
    dry_run     INTEGER NOT NULL DEFAULT 1,
    started_at  REAL NOT NULL DEFAULT 0,
    ended_at    REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS choreography_events (
    session_id      TEXT NOT NULL,
    seq             INTEGER NOT NULL,
    ts              REAL NOT NULL DEFAULT 0,
    speaker_account TEXT NOT NULL DEFAULT '',
    role            TEXT NOT NULL DEFAULT '',
    beat_id         TEXT NOT NULL DEFAULT '',
    text            TEXT NOT NULL DEFAULT '',
    kind            TEXT NOT NULL DEFAULT 'line',
    PRIMARY KEY (session_id, seq)
);
CREATE TABLE IF NOT EXISTS choreography_memberships (
    platform   TEXT NOT NULL DEFAULT 'telegram',
    group_key  TEXT NOT NULL,
    account_id TEXT NOT NULL,
    joined_at  REAL NOT NULL DEFAULT 0,
    source     TEXT NOT NULL DEFAULT 'manual',
    PRIMARY KEY (platform, group_key, account_id)
);
CREATE INDEX IF NOT EXISTS idx_choreo_sessions_group
    ON choreography_sessions (group_key, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_choreo_memberships_acct
    ON choreography_memberships (platform, account_id);
"""

# 既有库升级钩子（当前无迁移；加列时进这里，ALTER 幂等失败即已存在）
_MIGRATIONS: List[str] = []

_SESSION_COLS = (
    "session_id", "group_key", "platform", "playbook_id", "cast_json",
    "beat_cursor", "status", "dry_run", "started_at", "ended_at",
)
_EVENT_COLS = (
    "session_id", "seq", "ts", "speaker_account", "role", "beat_id", "text", "kind",
)


def _s(v: Any) -> str:
    return str(v or "").strip()


def _f(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _i(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


# ── Casting ↔ JSON（复用 playbook 契约，不另立类型） ─────────────────────────


def casting_to_json(casting: Any) -> str:
    """``Casting`` → JSON 串。``unfilled`` 一并存——号不够时的降级演法是复盘要点。"""
    members: List[Dict[str, Any]] = []
    try:
        for m in getattr(casting, "members", ()) or ():
            members.append({
                "slot": _s(getattr(m, "slot", "")),
                "account_id": _s(getattr(m, "account_id", "")),
                "persona_id": _s(getattr(m, "persona_id", "")),
                "display_name": str(getattr(m, "display_name", "") or ""),
                "platform": _s(getattr(m, "platform", "")) or "telegram",
            })
        unfilled = [_s(x) for x in (getattr(casting, "unfilled", ()) or ()) if _s(x)]
        return json.dumps({"members": members, "unfilled": unfilled},
                          ensure_ascii=False)
    except Exception:  # noqa: BLE001 —— 坏 casting 不该阻断整场落库
        logger.debug("[group_show.store] casting 序列化失败（落空表）", exc_info=True)
        return json.dumps({"members": [], "unfilled": []}, ensure_ascii=False)


def _json_items(value: Any, field_name: str) -> List[Any]:
    """JSON 里的数组字段 → 列表；不是数组就当空。

    库里这份 JSON 可能来自旧版本、外部工具或人工编辑，字段类型没有任何保证
    （见过 ``{"members": 7}``）。这里逐字段兜住，让「坏数据 → 空 Casting」
    的契约对内层字段同样成立，而不是只在最外层判一次 dict。
    """
    if isinstance(value, (list, tuple)):
        return list(value)
    if value:
        logger.debug("[group_show.store] casting.%s 不是数组（%r），按空处理",
                     field_name, value)
    return []


def casting_from_json(raw: Any) -> Casting:
    """JSON 串/已解析结构 → ``Casting``（续演时还原演员表）。坏数据 → 空 Casting。

    兼容纯 list 形式（只有 members、无 unfilled）的早期/外部数据。
    """
    data: Any = raw
    if isinstance(raw, (str, bytes)):
        try:
            data = json.loads(raw or "{}")
        except (ValueError, TypeError):
            return Casting()
    if isinstance(data, list):
        data = {"members": data, "unfilled": []}
    if not isinstance(data, dict):
        return Casting()
    members: List[CastMember] = []
    for d in _json_items(data.get("members"), "members"):
        if not isinstance(d, dict):
            continue
        slot = _s(d.get("slot"))
        if not slot:
            continue
        members.append(CastMember(
            slot=slot,
            account_id=_s(d.get("account_id")),
            persona_id=_s(d.get("persona_id")),
            display_name=str(d.get("display_name") or ""),
            platform=_s(d.get("platform")) or "telegram",
        ))
    unfilled = tuple(
        _s(x) for x in _json_items(data.get("unfilled"), "unfilled") if _s(x))
    return Casting(members=tuple(members), unfilled=unfilled)


# ── Store ───────────────────────────────────────────────────────────────────


class GroupShowStore:
    """群戏场次与事件流（线程安全 SQLite；建库失败即降级空转，永不抛）。"""

    def __init__(self, db_path: Any = DEFAULT_DB_PATH) -> None:
        self._db_path = str(db_path)
        self._lock = threading.Lock()
        self._conn: Optional[sqlite3.Connection] = None
        #: False ＝ 建库失败，本实例所有方法空转（调用方无需判空，照常调用）
        self.available = False
        try:
            is_mem = self._db_path == ":memory:"
            if not is_mem:
                Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self._db_path, check_same_thread=False, timeout=10)
            conn.row_factory = sqlite3.Row
            with self._lock:
                if not is_mem:
                    try:
                        conn.execute("PRAGMA journal_mode=WAL")
                    except sqlite3.Error:
                        pass  # 网络盘/只读介质不支持 WAL，回落默认日志模式即可
                conn.execute("PRAGMA busy_timeout=5000")
                conn.executescript(_DDL)
                for mig in _MIGRATIONS:
                    try:
                        conn.execute(mig)
                    except sqlite3.OperationalError:
                        pass  # 列已存在（新建库走 DDL）
                conn.commit()
            self._conn = conn
            self.available = True
        except Exception:  # noqa: BLE001 —— 落库故障绝不能掀翻正在演的戏
            logger.debug("[group_show.store] 建库失败，降级为不落库（%s）",
                         self._db_path, exc_info=True)
            self._conn = None
            self.available = False

    def close(self) -> None:
        """关连接并转入降级空转态（换库/收尾用；重复调用安全）。

        关掉之后本实例的所有方法与「建库失败」走同一条空转路径——调用方拿到旧实例
        也只是不落档，不会拿着一个半死的连接抛异常。
        """
        conn, self._conn, self.available = self._conn, None, False
        if conn is None:
            return
        try:
            with self._lock:
                conn.close()
        except Exception:  # noqa: BLE001
            logger.debug("[group_show.store] 关库失败（忽略）", exc_info=True)

    # ── 场次 ────────────────────────────────────────────────────────────────

    def save_session(self, state: Any) -> None:
        """upsert 一场戏的状态（反复调用只更新同一行）。

        同时 flush ``state.events``（按 ``(session_id, seq)`` upsert，幂等），
        补齐 ``append_event`` 可能漏写的行——续演正确性优先。
        """
        if self._conn is None or state is None:
            return
        sid = _s(getattr(state, "session_id", ""))
        if not sid:
            return
        try:
            playbook_id = _s(getattr(getattr(state, "playbook", None), "id", ""))
            row = (
                sid,
                _s(getattr(state, "group_key", "")),
                _s(getattr(state, "platform", "")) or "telegram",
                playbook_id,
                casting_to_json(getattr(state, "casting", None)),
                max(0, _i(getattr(state, "beat_cursor", 0))),
                _s(getattr(state, "status", "")) or "pending",
                1 if getattr(state, "dry_run", True) else 0,
                _f(getattr(state, "started_at", 0.0)),
                _f(getattr(state, "ended_at", 0.0)),
            )
            updates = ", ".join(
                f"{c} = excluded.{c}" for c in _SESSION_COLS if c != "session_id")
            with self._lock:
                self._conn.execute(
                    "INSERT INTO choreography_sessions ("
                    + ", ".join(_SESSION_COLS)
                    + ") VALUES (" + ", ".join("?" * len(_SESSION_COLS)) + ") "
                    "ON CONFLICT(session_id) DO UPDATE SET " + updates,
                    row,
                )
                for ev in (getattr(state, "events", None) or []):
                    self._write_event_locked(sid, ev)
                self._conn.commit()
        except Exception:  # noqa: BLE001
            logger.debug("[group_show.store] save_session 失败（已忽略）", exc_info=True)

    def load_session(self, session_id: Any) -> Optional[Dict[str, Any]]:
        """按 id 取场次行 → dict；无记录/异常 → None。

        ``cast`` 为解析后的结构，``casting`` 为还原好的 ``Casting`` 对象（续演直接用）。
        """
        if self._conn is None:
            return None
        sid = _s(session_id)
        if not sid:
            return None
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT * FROM choreography_sessions WHERE session_id = ?",
                    (sid,),
                ).fetchone()
        except Exception:  # noqa: BLE001
            logger.debug("[group_show.store] load_session 失败（已忽略）", exc_info=True)
            return None
        return self._session_row_to_dict(row) if row is not None else None

    def recent_sessions(
        self, *, group_key: str = "", limit: int = 20, platform: str = "",
    ) -> List[Dict[str, Any]]:
        """最近场次（按 ``started_at`` 降序）。

        ``group_key`` / ``platform`` 非空则各自过滤、可独立组合（P0 多平台：导播台按
        当前平台上下文只看该平台的场次；两者皆空＝全部，与旧行为一字不差）。
        """
        if self._conn is None:
            return []
        n = max(1, min(_i(limit, 20) or 20, 500))
        gk = _s(group_key)
        plat = _s(platform)
        wheres: List[str] = []
        params: List[Any] = []
        if gk:
            wheres.append("group_key = ?")
            params.append(gk)
        if plat:
            wheres.append("platform = ?")
            params.append(plat)
        where_sql = (" WHERE " + " AND ".join(wheres)) if wheres else ""
        params.append(n)
        try:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT * FROM choreography_sessions" + where_sql +
                    " ORDER BY started_at DESC, session_id DESC LIMIT ?",
                    params,
                ).fetchall()
        except Exception:  # noqa: BLE001
            logger.debug("[group_show.store] recent_sessions 失败（已忽略）", exc_info=True)
            return []
        # 逐行解析、逐行兜底：SQLite 是弱类型，外部工具/旧版本能把 beat_cursor 写成
        # TEXT。整批 try 住等于「一行脏数据让 19 场健康数据一起消失」，而看板上的空白
        # 与「今天没有戏」长得一模一样——损失必须被限制在坏的那一行里。
        out: List[Dict[str, Any]] = []
        for r in rows:
            try:
                out.append(self._session_row_to_dict(r))
            except Exception:  # noqa: BLE001
                logger.debug("[group_show.store] 跳过无法解析的场次行（已忽略）",
                             exc_info=True)
        # 补「真发出去几条」：beat_cursor 含跳拍，坐席盯进度要的是**送达数**。
        # 一条聚合查询批量取，失败＝各行 0（进度是辅助读数，绝不连累列表本身）。
        counts: Dict[str, int] = {}
        sids = [str(r.get("session_id") or "") for r in out]
        sids = [s for s in sids if s]
        if sids:
            try:
                marks = ",".join("?" for _ in sids)
                with self._lock:
                    got = self._conn.execute(
                        "SELECT session_id, COUNT(*) FROM choreography_events "
                        f"WHERE kind IN ('line', 'media') "
                        f"AND session_id IN ({marks}) GROUP BY session_id",
                        sids,
                    ).fetchall()
                counts = {str(g[0]): _i(g[1]) for g in got}
            except Exception:  # noqa: BLE001
                logger.debug("[group_show.store] 场次送达数统计失败（已忽略）",
                             exc_info=True)
        for r in out:
            r["lines_sent"] = counts.get(str(r.get("session_id") or ""), 0)
        return out

    def update_status(
        self, session_id: Any, status: Any, *, ended_at: float = 0.0,
    ) -> None:
        """改场次状态。``ended_at > 0`` 才写入，否则保留原值（避免误清收尾时间）。

        未知 status 仍照写并记 debug——本库是运行时的镜子，不是校验器，
        丢状态比存一个眼生的状态更糟。
        """
        if self._conn is None:
            return
        sid = _s(session_id)
        st = _s(status)
        if not sid or not st:
            return
        if st not in SHOW_STATUSES:
            logger.debug("[group_show.store] 未知 status=%s（仍写入）", st)
        end = _f(ended_at)
        try:
            with self._lock:
                if end > 0:
                    self._conn.execute(
                        "UPDATE choreography_sessions SET status = ?, ended_at = ? "
                        "WHERE session_id = ?", (st, end, sid))
                else:
                    self._conn.execute(
                        "UPDATE choreography_sessions SET status = ? "
                        "WHERE session_id = ?", (st, sid))
                self._conn.commit()
        except Exception:  # noqa: BLE001
            logger.debug("[group_show.store] update_status 失败（已忽略）", exc_info=True)

    # ── 出席台账 ────────────────────────────────────────────────────────────
    #
    # 出席矩阵（:mod:`~src.companion.group_show.attendance`）算出来的是「该加哪些群」，
    # 加群本身是**跨天的手工动作**。没有台账就有两个后果：① 运营第二天打开控制台，
    # 排班器不知道昨天加过什么，又给同一张 Day 1；② 更要命的是**风险低报**——历史群里
    # 早就产生的共同出席，新一批排班完全看不见，每批都显示「安全」，累积起来早已超标。
    #
    # ``source`` 区分 ``manual``（运营点「已加入」的自述）与 ``observed``（将来从平台
    # 侧真实枚举出来的）。留这一列是为了以后接真实同步时能覆盖自述值，而不是再开一张表。

    def record_membership(
        self,
        group_key: Any,
        account_id: Any,
        *,
        platform: str = "telegram",
        source: str = "manual",
        joined_at: float = 0.0,
    ) -> None:
        """登记「这个号已经在这个群里了」。重复登记幂等，且**不覆盖首次时间**
        （第一次进群的时间是事实，重新点一次按钮不该把它刷成今天）。"""
        if self._conn is None:
            return
        gk, acct = _s(group_key), _s(account_id)
        if not gk or not acct:
            return
        try:
            with self._lock:
                self._conn.execute(
                    "INSERT INTO choreography_memberships "
                    "(platform, group_key, account_id, joined_at, source) "
                    "VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(platform, group_key, account_id) "
                    "DO UPDATE SET source = excluded.source",
                    (_s(platform) or "telegram", gk, acct,
                     _f(joined_at) or time.time(), _s(source) or "manual"),
                )
                self._conn.commit()
        except Exception:  # noqa: BLE001
            logger.debug("[group_show.store] record_membership 失败（已忽略）",
                         exc_info=True)

    def forget_membership(
        self, group_key: Any, account_id: Any, *, platform: str = "telegram",
    ) -> None:
        """撤销一条登记（点错了 / 号被踢出群）。

        删除的是**我们的账本**，不是真实群成员关系——所以这里没有任何退群动作。
        """
        if self._conn is None:
            return
        gk, acct = _s(group_key), _s(account_id)
        if not gk or not acct:
            return
        try:
            with self._lock:
                self._conn.execute(
                    "DELETE FROM choreography_memberships WHERE platform = ? "
                    "AND group_key = ? AND account_id = ?",
                    (_s(platform) or "telegram", gk, acct))
                self._conn.commit()
        except Exception:  # noqa: BLE001
            logger.debug("[group_show.store] forget_membership 失败（已忽略）",
                         exc_info=True)

    def memberships(self, *, platform: str = "telegram") -> Dict[str, List[str]]:
        """``{群: [号, ...]}``——直接喂给 ``plan_attendance`` 的 existing/history。"""
        if self._conn is None:
            return {}
        try:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT group_key, account_id FROM choreography_memberships "
                    "WHERE platform = ? ORDER BY group_key, joined_at, account_id",
                    (_s(platform) or "telegram",),
                ).fetchall()
        except Exception:  # noqa: BLE001
            logger.debug("[group_show.store] memberships 失败（已忽略）", exc_info=True)
            return {}
        out: Dict[str, List[str]] = {}
        for r in rows:
            try:
                gk, acct = _s(r["group_key"]), _s(r["account_id"])
            except Exception:  # noqa: BLE001 —— 一行脏数据不该吞掉整本账
                continue
            if gk and acct and acct not in out.setdefault(gk, []):
                out[gk].append(acct)
        return out

    def joins_since(
        self, cutoff_ts: float, *, platform: str = "telegram",
    ) -> Dict[str, int]:
        """``{号: 这个号在 cutoff 之后加了几个群}``。

        排期表里「每个号每天最多加 N 个群」这条限速，只在**一张表内部**成立；运营做完
        今天的任务、点掉「已加入」再重排，新表的「第 1 天」是干净的——同一个号当天就
        又被派了 N 个群。限速形同虚设。喂这个计数给 :func:`schedule_joins` 才能把
        「今天已经加了几个」从当天预算里扣掉。
        """
        if self._conn is None:
            return {}
        try:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT account_id, COUNT(*) AS n FROM choreography_memberships "
                    "WHERE platform = ? AND joined_at >= ? GROUP BY account_id",
                    (_s(platform) or "telegram", _f(cutoff_ts)),
                ).fetchall()
        except Exception:  # noqa: BLE001
            logger.debug("[group_show.store] joins_since 失败（已忽略）", exc_info=True)
            return {}
        out: Dict[str, int] = {}
        for r in rows:
            try:
                acct, n = _s(r["account_id"]), int(r["n"] or 0)
            except Exception:  # noqa: BLE001 —— 一行脏数据不该吞掉整份计数
                continue
            if acct and n > 0:
                out[acct] = n
        return out

    # ── 演出台账 ────────────────────────────────────────────────────────────
    #
    # 出席台账答的是「谁在这个群里」，演出台账答的是「谁在这个群里**真开过口**」。两者
    # 是平台侧两条完全不同的信号：成员关系要被专门拉出来做交集分析才暴露（被动风险），
    # 而每一条消息都在实时喂反垃圾管道（主动风险）。更要紧的是**可控性相反**——群一旦
    # 加了就基本冻结（退群本身可疑），而「这一场让谁开口」每场都能重排。
    #
    # 不新建表：``choreography_events`` 早就记了 ``speaker_account``、
    # ``choreography_sessions`` 早就记了 ``group_key``/``dry_run``，台账是这两张表的
    # **派生视图**。多存一份就会出现「事件流说 A 发过言、台账说没有」的双源真相。

    def performance_ledger(
        self, *, platform: str = "telegram", since: float = 0.0,
    ) -> Dict[str, List[str]]:
        """``{群: [真开过口的号, ...]}``——形状与 :meth:`memberships` 一致，可直接喂给
        ``attendance_metrics`` 算演出共现矩阵。

        两条过滤是这份数据的全部意义所在，写反任何一条整个读数就是假的：

        * ``dry_run = 0``——**排练不算暴露**。排练一条消息都没发出去，把它计进来会让
          「多排练几次」凭空推高风险读数，运营会因此不敢排练，而排练恰恰是零风险的。
        * ``kind IN SPOKEN_KINDS``——``human`` 事件的 ``speaker_account`` 是**真人**
          （群友插话），算进来等于把真实用户当成我们的号做关联分析；``yield``/
          ``terminate`` 是导演的内部动作，平台侧一个字都看不到。

        ``since > 0`` 时按事件时刻裁窗（事件没记时刻的回落到场次开演时刻——早期数据
        ``ts`` 可能为 0，用 ``e.ts`` 硬过滤会把它们整批判成「远古」而消失）。窗口的意义
        是：共现要能**随时间淡出**，否则跑上几个月每一对都会饱和，这个数就失去了指导
        「这场该让谁开口」的能力。
        """
        if self._conn is None:
            return {}
        kinds = tuple(SPOKEN_KINDS) or ("line",)
        placeholders = ", ".join("?" * len(kinds))
        cutoff = _f(since)
        sql = (
            "SELECT DISTINCT s.group_key AS group_key, "
            "e.speaker_account AS speaker_account "
            "FROM choreography_events e "
            "JOIN choreography_sessions s ON s.session_id = e.session_id "
            "WHERE s.platform = ? AND s.dry_run = 0 "
            f"AND e.kind IN ({placeholders}) "
            "AND s.group_key <> '' AND e.speaker_account <> '' "
        )
        params: List[Any] = [_s(platform) or "telegram", *kinds]
        if cutoff > 0:
            sql += "AND COALESCE(NULLIF(e.ts, 0), s.started_at) >= ? "
            params.append(cutoff)
        sql += "ORDER BY s.group_key, e.speaker_account"
        try:
            with self._lock:
                rows = self._conn.execute(sql, params).fetchall()
        except Exception:  # noqa: BLE001
            logger.debug("[group_show.store] performance_ledger 失败（已忽略）",
                         exc_info=True)
            return {}
        out: Dict[str, List[str]] = {}
        for r in rows:
            try:
                gk, acct = _s(r["group_key"]), _s(r["speaker_account"])
            except Exception:  # noqa: BLE001 —— 一行脏数据不该吞掉整本台账
                continue
            if gk and acct and acct not in out.setdefault(gk, []):
                out[gk].append(acct)
        return out

    def role_ledger(
        self, *, platform: str = "telegram", since: float = 0.0,
    ) -> Dict[Tuple[str, str], int]:
        """``{(号, 角色槽): 这个号在多少个**不同的群**演过这个角色}``——角色轴的台账。

        与 :meth:`performance_ledger` 同源同过滤（排练不算、真人插话不算），只是把
        「谁开过口」细分到「以什么身份开的口」。这条轴要单独看，因为**只有推销性角色
        是把柄**：一个号在 50 个群当路人完全正常（真人就这样），在 40 个群都是那个
        「强烈推荐」的人，群主肉眼就能看出来。判定哪些槽算推销见
        :data:`~src.companion.group_show.roles.SELLING_SLOTS`。

        形状与 ``co_performance()`` 的 ``{(号A,号B): 群数}`` 对称，两张卡才能并排读。
        """
        if self._conn is None:
            return {}
        kinds = tuple(SPOKEN_KINDS) or ("line",)
        placeholders = ", ".join("?" * len(kinds))
        cutoff = _f(since)
        sql = (
            "SELECT e.speaker_account AS acct, e.role AS role, "
            "COUNT(DISTINCT s.group_key) AS n "
            "FROM choreography_events e "
            "JOIN choreography_sessions s ON s.session_id = e.session_id "
            "WHERE s.platform = ? AND s.dry_run = 0 "
            f"AND e.kind IN ({placeholders}) "
            "AND s.group_key <> '' AND e.speaker_account <> '' AND e.role <> '' "
        )
        params: List[Any] = [_s(platform) or "telegram", *kinds]
        if cutoff > 0:
            sql += "AND COALESCE(NULLIF(e.ts, 0), s.started_at) >= ? "
            params.append(cutoff)
        sql += "GROUP BY e.speaker_account, e.role"
        try:
            with self._lock:
                rows = self._conn.execute(sql, params).fetchall()
        except Exception:  # noqa: BLE001
            logger.debug("[group_show.store] role_ledger 失败（已忽略）", exc_info=True)
            return {}
        out: Dict[Tuple[str, str], int] = {}
        for r in rows:
            try:
                acct, role, n = _s(r["acct"]), _s(r["role"]), int(r["n"] or 0)
            except Exception:  # noqa: BLE001 —— 一行脏数据不该吞掉整本台账
                continue
            if acct and role and n > 0:
                out[(acct, role)] = n
        return out

    def prior_slots(
        self, *, group_key: str, platform: str = "telegram",
    ) -> Dict[str, str]:
        """``{号: 它在**这个群**最近一次真发时演的角色槽}``——群内角色粘性的台账。

        角色轮转（``role_ledger`` 轴）防「单号在很多群长期当主推」，是**跨群**语义；
        但在同一个群里翻转立场是另一种破绽：上一场泼冷水的号下一场安利同一个产品，
        群里的真人翻两屏聊天记录就能看出来（2026-07-27 双号灰度实录：两场之间
        advocate/skeptic 恰好对调）。这本台账让选角在**群内粘住**上次的角色，
        轮转照常发生在不同群之间——两条轴不打架。

        与 :meth:`role_ledger` 同源同过滤（排练不算暴露、真人插话不算我们的号），
        每个号取**最近**一次发言的角色（事件缺时刻回落场次开演时刻）。
        """
        gk = _s(group_key)
        if self._conn is None or not gk:
            return {}
        kinds = tuple(SPOKEN_KINDS) or ("line",)
        placeholders = ", ".join("?" * len(kinds))
        sql = (
            "SELECT e.speaker_account AS acct, e.role AS role, "
            "MAX(COALESCE(NULLIF(e.ts, 0), s.started_at)) AS at "
            "FROM choreography_events e "
            "JOIN choreography_sessions s ON s.session_id = e.session_id "
            "WHERE s.platform = ? AND s.dry_run = 0 AND s.group_key = ? "
            f"AND e.kind IN ({placeholders}) "
            "AND e.speaker_account <> '' AND e.role <> '' "
            "GROUP BY e.speaker_account, e.role"
        )
        try:
            with self._lock:
                rows = self._conn.execute(
                    sql, [_s(platform) or "telegram", gk, *kinds]).fetchall()
        except Exception:  # noqa: BLE001
            logger.debug("[group_show.store] prior_slots 失败（已忽略）", exc_info=True)
            return {}
        latest: Dict[str, Tuple[float, str]] = {}
        for r in rows:
            try:
                acct, role, at = _s(r["acct"]), _s(r["role"]), _f(r["at"])
            except Exception:  # noqa: BLE001 —— 一行脏数据不该吞掉整本台账
                continue
            if acct and role and at >= latest.get(acct, (0.0, ""))[0]:
                latest[acct] = (at, role)
        return {acct: role for acct, (_, role) in latest.items()}

    def last_spoke_at(
        self, *, platform: str = "telegram", since: float = 0.0,
        exclude_group: str = "",
    ) -> Dict[str, float]:
        """``{号: 最近一次在群里开口的时刻}``——时间轴的台账。

        用来挡「同一个号前后脚在两个群发言」：真人不会隔十秒换个群接着说，而这正是
        跨群编排最容易露出来的节奏特征（静态特征洗干净了，节奏照样出卖人）。取值口径
        与 :meth:`performance_ledger` 一致，只是聚合成每个号的最新时刻。

        事件没记时刻的回落到场次开演时刻——早期数据 ``ts`` 可能为 0，直接取 ``e.ts``
        会把它们判成「1970 年说过话」，于是这个号永远畅通无阻，闸门形同虚设。

        ``exclude_group`` 排除目标群自己：闸门问的是「有没有在*别的*群刚冒过头」，
        同一个群里连着演两拍是这场戏本身，不是跨群痕迹。语义与
        ``InboxStore.group_last_spoke_at`` 的同名参数一字不差（两份台账要能直接取 max）。
        """
        if self._conn is None:
            return {}
        kinds = tuple(SPOKEN_KINDS) or ("line",)
        placeholders = ", ".join("?" * len(kinds))
        cutoff = _f(since)
        sql = (
            "SELECT e.speaker_account AS acct, "
            "MAX(COALESCE(NULLIF(e.ts, 0), s.started_at)) AS last_ts "
            "FROM choreography_events e "
            "JOIN choreography_sessions s ON s.session_id = e.session_id "
            "WHERE s.platform = ? AND s.dry_run = 0 "
            f"AND e.kind IN ({placeholders}) "
            "AND s.group_key <> '' AND e.speaker_account <> '' "
        )
        params: List[Any] = [_s(platform) or "telegram", *kinds]
        if cutoff > 0:
            sql += "AND COALESCE(NULLIF(e.ts, 0), s.started_at) >= ? "
            params.append(cutoff)
        skip = _s(exclude_group)
        if skip:
            sql += "AND s.group_key <> ? "
            params.append(skip)
        sql += "GROUP BY e.speaker_account"
        try:
            with self._lock:
                rows = self._conn.execute(sql, params).fetchall()
        except Exception:  # noqa: BLE001
            logger.debug("[group_show.store] last_spoke_at 失败（已忽略）", exc_info=True)
            return {}
        out: Dict[str, float] = {}
        for r in rows:
            try:
                acct, ts = _s(r["acct"]), _f(r["last_ts"])
            except Exception:  # noqa: BLE001
                continue
            if acct and ts > 0:
                out[acct] = ts
        return out

    # ── 事件流 ──────────────────────────────────────────────────────────────

    def append_event(self, session_id: Any, event: Any) -> None:
        """追加一条 ``ShowEvent``（同 ``(session_id, seq)`` 重放 → 覆盖，幂等）。"""
        if self._conn is None or event is None:
            return
        sid = _s(session_id)
        if not sid:
            return
        try:
            with self._lock:
                self._write_event_locked(sid, event)
                self._conn.commit()
        except Exception:  # noqa: BLE001
            logger.debug("[group_show.store] append_event 失败（已忽略）", exc_info=True)

    def events(self, session_id: Any) -> List[Dict[str, Any]]:
        """整条事件流（按 ``seq`` 升序）——续演/回放/自然度评测的素材。"""
        if self._conn is None:
            return []
        sid = _s(session_id)
        if not sid:
            return []
        try:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT " + ", ".join(_EVENT_COLS) + " FROM choreography_events "
                    "WHERE session_id = ? ORDER BY seq", (sid,),
                ).fetchall()
        except Exception:  # noqa: BLE001
            logger.debug("[group_show.store] events 失败（已忽略）", exc_info=True)
            return []
        # 同 recent_sessions 的理由，且这里代价更直接：整条流抛穿 → 续演拿不到「已经
        # 说过什么」→ 同一批号在同一个群把同一个话题重演一遍，那是最刺眼的机器特征。
        # 少一条脏行的历史，远好过丢掉全部历史。
        out: List[Dict[str, Any]] = []
        for r in rows:
            try:
                out.append(self._event_row_to_dict(r))
            except Exception:  # noqa: BLE001
                logger.debug("[group_show.store] 跳过无法解析的事件行（session=%s）",
                             sid, exc_info=True)
        return out

    # ── 内部 ────────────────────────────────────────────────────────────────

    def _write_event_locked(self, sid: str, ev: Any) -> None:
        """写一条事件（**调用方必须已持锁且负责 commit**）。"""
        if self._conn is None or ev is None:
            return
        seq = _i(getattr(ev, "seq", 0))
        if seq <= 0:
            logger.debug("[group_show.store] 跳过 seq<=0 的事件（session=%s）", sid)
            return
        self._conn.execute(
            "INSERT INTO choreography_events (" + ", ".join(_EVENT_COLS) + ") "
            "VALUES (" + ", ".join("?" * len(_EVENT_COLS)) + ") "
            "ON CONFLICT(session_id, seq) DO UPDATE SET "
            "ts = excluded.ts, speaker_account = excluded.speaker_account, "
            "role = excluded.role, beat_id = excluded.beat_id, "
            "text = excluded.text, kind = excluded.kind",
            (
                sid, seq, _f(getattr(ev, "ts", 0.0)),
                _s(getattr(ev, "speaker_account", "")),
                _s(getattr(ev, "role", "")),
                _s(getattr(ev, "beat_id", "")),
                str(getattr(ev, "text", "") or ""),
                _s(getattr(ev, "kind", "")) or "line",
            ),
        )

    @staticmethod
    def _event_row_to_dict(row: Any) -> Dict[str, Any]:
        return {
            "session_id": str(row["session_id"]),
            "seq": int(row["seq"] or 0),
            "ts": float(row["ts"] or 0.0),
            "speaker_account": str(row["speaker_account"] or ""),
            "role": str(row["role"] or ""),
            "beat_id": str(row["beat_id"] or ""),
            "text": str(row["text"] or ""),
            "kind": str(row["kind"] or "line"),
        }

    @staticmethod
    def _session_row_to_dict(row: Any) -> Dict[str, Any]:
        cast_raw = str(row["cast_json"] or "{}")
        casting = casting_from_json(cast_raw)
        return {
            "session_id": str(row["session_id"]),
            "group_key": str(row["group_key"] or ""),
            "platform": str(row["platform"] or "telegram"),
            "playbook_id": str(row["playbook_id"] or ""),
            "cast": {
                "members": [{
                    "slot": m.slot, "account_id": m.account_id,
                    "persona_id": m.persona_id, "display_name": m.display_name,
                    "platform": m.platform,
                } for m in casting.members],
                "unfilled": list(casting.unfilled),
            },
            "casting": casting,
            "beat_cursor": int(row["beat_cursor"] or 0),
            "status": str(row["status"] or "pending"),
            "dry_run": bool(row["dry_run"]),
            "started_at": float(row["started_at"] or 0.0),
            "ended_at": float(row["ended_at"] or 0.0),
        }


# ── 模块级单例（三件套对齐 fatex_store / persona_media_store） ────────────────

_STORE: Optional[GroupShowStore] = None
_DB_PATH: str = DEFAULT_DB_PATH
_CFG_LOCK = threading.Lock()


def configure_group_show_store(db_path: Any = DEFAULT_DB_PATH) -> GroupShowStore:
    """启动期装配（同路径幂等；**换路径则重建**）。双实例部署下各实例指定自己的库。

    「已装配就无脑返回既有实例」曾是个静默串库陷阱：``_DB_PATH`` 被改成新路径、
    实例却还连着旧库，之后所有人都以为自己在写新库。显式 configure 是**声明意图**，
    路径变了就该真换过去（2026-07-26 踩中：导播台用例先把单例装到仓库根
    ``config/group_show.db``，路由随后带实例 tmp 路径来 configure 拿到的仍是那一个，
    假场次写进了仓库库）。同路径重复调用仍是纯幂等，不重连。
    """
    global _STORE, _DB_PATH
    with _CFG_LOCK:
        path = str(db_path)
        if _STORE is not None and _DB_PATH != path:
            try:
                _STORE.close()
            except Exception:  # noqa: BLE001 —— 换库不该被旧连接的收尾拖住
                logger.debug("[group_show] 旧场次库关闭失败（忽略）", exc_info=True)
            _STORE = None
        _DB_PATH = path
        if _STORE is None:
            _STORE = GroupShowStore(_DB_PATH)
        return _STORE


def get_group_show_store(db_path: Any = None) -> GroupShowStore:
    """取进程级单例；未装配则按 ``db_path``（缺省 :data:`DEFAULT_DB_PATH`）懒建。

    **永不返回 None**：建库失败也返回一个 ``available=False`` 的降级实例，
    调用方不必到处判空——炒群链路不该被落库这种旁路能力打断。
    ``db_path`` 只在**首次**创建时生效（换库请先 ``reset_group_show_store``）。
    """
    global _STORE, _DB_PATH
    if _STORE is None:
        with _CFG_LOCK:
            if _STORE is None:
                if db_path is not None:
                    _DB_PATH = str(db_path)
                _STORE = GroupShowStore(_DB_PATH)
    return _STORE


def reset_group_show_store() -> None:
    """测试钩子：清空单例（生产勿用）。"""
    global _STORE, _DB_PATH
    with _CFG_LOCK:
        _STORE = None
        _DB_PATH = DEFAULT_DB_PATH


__all__ = [
    "DEFAULT_DB_PATH",
    "GroupShowStore",
    "casting_from_json",
    "casting_to_json",
    "configure_group_show_store",
    "get_group_show_store",
    "reset_group_show_store",
]
