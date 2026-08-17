"""演出效果归因 —— 「这场戏到底换来了什么」的**唯一**口径（纯函数，零 I/O）。

本包此前所有读数都在回答「会不会被封号」；运营总监与市场侧真正要问的那个问题
——**值不值得继续投**——在系统里一个字都没有。这个模块补的就是那一半。

为什么口径要单独成模块（而不是在路由里现算）
--------------------------------------------
效果数字一旦有两处实现就一定会漂，而漂掉的效果读数比没有更坏：它会被写进周报、
变成「这批号该不该继续养」的依据。与五条风险轴同理，**只允许有一处口径**。

三条不可让步的诚实原则
----------------------
1. **排练不算数**（``dry_run`` 场次一律剔除）。排练物理上没有出口，一条消息都没发过，
   给它算「效果」是纯粹的自欺。这是本模块最容易被写错、也最致命的一条。
2. **窗口没走完不下判词**。一场 40 分钟前刚演完的戏，24 小时窗口里当然没什么数字；
   把它标成「无人问津」会让运营去改一个根本没问题的剧本。故未到点的场次判
   :data:`VERDICT_PENDING`，**不参与**汇总里的冷场率。
3. **只认能证明的，不猜**。潜水的人看完戏私聊我们，系统无从知晓——这类转化
   **不可归因**，本模块也不假装能算。所以这里的转化数是**下限**（floor），
   不是全部战果，UI 必须如实这么说。

两级证据（强弱分明，不混在一个数里）
------------------------------------
- **群内反响**（直接可数）：演出窗口内该群有多少条真人进向消息、多少个不同的人开口、
  第一个人多久后开口。来源 ``InboxStore.group_inbound_since``，零推断。
- **私聊转化**（强证据链）：在窗口内于该群**发过言**的人，其与我们的**首次私聊**
  发生在演出开始之后 ⇒ 判定为这场戏带来的转化。靠的是平台侧同一个 ``sender_id``，
  不是「时间上差不多」的猜测。已在私聊里的老客户不会被算进来（首次私聊早于演出）。

演员自己的发言不算反响：出站镜像偶发会被标成 ``in``（见 ``group_inbound_since``
的 ``exclude_senders``），不排掉的话「自己人鼓掌」会被算成群众反响。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

# ── 口径常量（改这里等于改所有消费方；别在路由/前端另算一份） ──────────────────

#: 归因窗口默认长度（小时）。24h 的依据：群聊是异步场景，人看到消息的时间高度分散，
#: 窗口太短会把「晚上才刷到群」的真实反响算成冷场。
DEFAULT_WINDOW_HOURS = 24.0
MIN_WINDOW_HOURS = 1.0
#: 上限 7 天：再长就不该叫「这场戏带来的」了——群里每天都在发生别的事，归因会失真。
MAX_WINDOW_HOURS = 168.0

#: 单场最多取多少条群内进向消息（与 ``group_inbound_since`` 的硬上限对齐）。
#: 取满即视为被截断，需如实标注：截断下的「反响」是下限。
INBOUND_CAP = 200

VERDICT_PENDING = "pending"   # 窗口还没走完，不下判词
VERDICT_COLD = "cold"         # 窗口内没有任何真人开口
VERDICT_WARM = "warm"         # 有人接话，但没人私聊
VERDICT_HOT = "hot"           # 有人接话且有人私聊过来


def clamp_window_hours(raw: Any) -> float:
    """把外部传入的窗口长度夹进合理区间（脏输入 → 默认值，绝不抛）。"""
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_WINDOW_HOURS
    if v != v or v <= 0:            # NaN / 非正
        return DEFAULT_WINDOW_HOURS
    return max(MIN_WINDOW_HOURS, min(v, MAX_WINDOW_HOURS))


def is_real_show(session: Any) -> bool:
    """真发过的场次才有「效果」可言——排练一条消息都没发出去过。

    缺 ``dry_run`` 字段的旧行按**排练**处理（保守）：把来历不明的场次算成真发，
    会凭空造出一批「零效果」的戏，把冷场率做难看，进而让人去改没问题的剧本。
    """
    if not isinstance(session, dict):
        return False
    if bool(session.get("dry_run", True)):
        return False
    return bool(str(session.get("group_key") or "").strip())


def window_bounds(session: Any, window_hours: float,
                  now: float) -> Tuple[float, float, bool]:
    """``(窗口起, 窗口止, 是否已走完)``。

    起点取 ``ended_at``（戏演完才开始数反响）；没有 ended_at（还在演/异常中断）
    则回落 ``started_at``——宁可把演出期间的插话也算进来，也好过整场没有读数。
    """
    started = float(session.get("started_at") or 0.0)
    ended = float(session.get("ended_at") or 0.0)
    base = ended if ended > 0 else started
    span = float(window_hours) * 3600.0
    return base, base + span, (now >= base + span)


def summarize_inbound(rows: Sequence[Dict[str, Any]], *,
                      start: float, end: float) -> Dict[str, Any]:
    """群内反响：窗口内的真人进向消息 → 人数 / 条数 / 首次响应延迟。

    ``group_inbound_since`` 只能给下界（``ts > since``），上界必须在这里裁——
    否则「24 小时窗口」会把之后几天的消息全算进来，越老的戏读数越好看。
    """
    senders: List[str] = []
    seen = set()
    replies = 0
    first_ts = 0.0
    for r in rows or ():
        try:
            ts = float((r or {}).get("ts") or 0.0)
        except (TypeError, ValueError):
            continue
        if ts < start or ts > end:
            continue
        replies += 1
        if first_ts <= 0 or ts < first_ts:
            first_ts = ts
        sid = str((r or {}).get("sender_id") or "").strip()
        if sid and sid not in seen:
            seen.add(sid)
            senders.append(sid)
    return {
        "responders": len(senders),
        "replies": replies,
        "sender_ids": tuple(senders),
        # 没人开口时是 -1 而不是 0：0 会被读成「秒回」，恰好是相反的意思。
        "first_reply_sec": (first_ts - start) if (first_ts > 0 and start > 0) else -1.0,
    }


def count_conversions(sender_ids: Sequence[str],
                      first_dm_ts: Optional[Dict[str, float]],
                      *, after: float) -> Tuple[int, Tuple[str, ...]]:
    """转化：这些群内发言者里，**首次**私聊发生在演出之后的有几个。

    ``first_dm_ts`` 读不到（老库/查询失败）→ 返回 0 并由调用方标 degraded：
    「没查到」和「真的没人私聊」必须能区分，否则零转化会被当成剧本不行。
    """
    if not first_dm_ts:
        return 0, ()
    hit: List[str] = []
    for sid in sender_ids or ():
        ts = first_dm_ts.get(str(sid))
        if ts is None:
            continue
        try:
            if float(ts) > float(after):
                hit.append(str(sid))
        except (TypeError, ValueError):
            continue
    return len(hit), tuple(hit)


def outcome_verdict(*, window_complete: bool, responders: int,
                    conversions: int) -> str:
    """判词。窗口未走完一律 pending——没到点就下结论是最容易误导人的一步。"""
    if not window_complete:
        return VERDICT_PENDING
    if conversions > 0:
        return VERDICT_HOT
    if responders > 0:
        return VERDICT_WARM
    return VERDICT_COLD


@dataclass(frozen=True)
class ShowOutcome:
    """一场真发演出的效果读数（可直接 JSON 化）。"""

    session_id: str
    playbook_id: str
    group_key: str
    platform: str
    started_at: float
    ended_at: float
    window_hours: float
    window_complete: bool
    responders: int
    replies: int
    first_reply_sec: float
    conversions: int
    verdict: str
    truncated: bool = False
    conversion_ids: Tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "playbook_id": self.playbook_id,
            "group_key": self.group_key,
            "platform": self.platform,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "window_hours": self.window_hours,
            "window_complete": self.window_complete,
            "responders": self.responders,
            "replies": self.replies,
            "first_reply_sec": self.first_reply_sec,
            "conversions": self.conversions,
            "verdict": self.verdict,
            "truncated": self.truncated,
        }


def build_outcome(session: Dict[str, Any], rows: Sequence[Dict[str, Any]],
                  first_dm_ts: Optional[Dict[str, float]], *,
                  window_hours: float, now: float) -> ShowOutcome:
    """把一场戏 + 它的群内进向消息 + 首次私聊表 拼成一条效果读数（纯函数）。"""
    start, end, complete = window_bounds(session, window_hours, now)
    stat = summarize_inbound(rows, start=start, end=end)
    conv, conv_ids = count_conversions(
        stat["sender_ids"], first_dm_ts,
        after=float(session.get("started_at") or start))
    return ShowOutcome(
        session_id=str(session.get("session_id") or ""),
        playbook_id=str(session.get("playbook_id") or ""),
        group_key=str(session.get("group_key") or ""),
        platform=str(session.get("platform") or "telegram"),
        started_at=float(session.get("started_at") or 0.0),
        ended_at=float(session.get("ended_at") or 0.0),
        window_hours=float(window_hours),
        window_complete=complete,
        responders=int(stat["responders"]),
        replies=int(stat["replies"]),
        first_reply_sec=float(stat["first_reply_sec"]),
        conversions=int(conv),
        verdict=outcome_verdict(window_complete=complete,
                                responders=int(stat["responders"]),
                                conversions=int(conv)),
        truncated=bool(len(rows or ()) >= INBOUND_CAP),
        conversion_ids=conv_ids,
    )


#: 一本剧本要攒够几场**已判定**演出，它的效果对比才值得看。
#: 依据不是统计学精确值，而是决策后果：低于这个数时「A 本 100% / B 本 0%」很可能只是
#: 两场戏的运气差异，而运营看到就会去砍 B 本——**砍掉一本其实没问题的剧本**，
#: 比没有这张对比表坏得多。所以样本不足时如实显示「样本不足 n/5」，不给百分比。
MIN_SHOWS_FOR_PLAYBOOK = 5


def by_playbook(outcomes: Sequence[ShowOutcome]) -> List[Dict[str, Any]]:
    """按剧本聚合效果，并**显式标注样本够不够**。

    ``pending`` 场次同样不进分母（与 :func:`rollup` 一致）：刚演完的戏拉低哪本剧本
    纯属排期先后，与剧本好坏无关。
    """
    acc: Dict[str, Dict[str, Any]] = {}
    for o in outcomes or ():
        pid = str(o.playbook_id or "") or "—"
        row = acc.setdefault(pid, {
            "playbook_id": pid, "shows": 0, "judged": 0,
            "responded": 0, "conversions": 0,
        })
        row["shows"] += 1
        if o.verdict == VERDICT_PENDING:
            continue
        row["judged"] += 1
        row["conversions"] += int(o.conversions)
        if o.responders > 0:
            row["responded"] += 1
    out: List[Dict[str, Any]] = []
    for row in acc.values():
        judged = int(row["judged"])
        enough = judged >= MIN_SHOWS_FOR_PLAYBOOK
        out.append({
            **row,
            "enough_sample": enough,
            "min_shows": MIN_SHOWS_FOR_PLAYBOOK,
            # 样本不足就**不给**百分比：给了它就会被读成结论，哪怕旁边写着「仅供参考」。
            "response_rate": (row["responded"] / judged) if (enough and judged) else -1.0,
        })
    out.sort(key=lambda r: (-r["response_rate"], -r["shows"], r["playbook_id"]))
    return out


def rollup(outcomes: Sequence[ShowOutcome]) -> Dict[str, Any]:
    """汇总成一句话能读懂的几个数。

    ``pending`` 的场次**只计入 total、不计入分母**：拿还没到点的戏摊薄回应率，
    等于让「最近演得越勤，效果看起来越差」，那是纯粹的读数陷阱。
    """
    total = len(outcomes or ())
    judged = [o for o in (outcomes or ()) if o.verdict != VERDICT_PENDING]
    pending = total - len(judged)
    responded = sum(1 for o in judged if o.responders > 0)
    conversions = sum(o.conversions for o in judged)
    replies = sum(o.replies for o in judged)
    lat = [o.first_reply_sec for o in judged if o.first_reply_sec >= 0]
    return {
        "total": total,
        "judged": len(judged),
        "pending": pending,
        "responded": responded,
        "response_rate": (responded / len(judged)) if judged else 0.0,
        "replies": replies,
        "conversions": conversions,
        "avg_first_reply_sec": (sum(lat) / len(lat)) if lat else -1.0,
        "truncated": any(o.truncated for o in (outcomes or ())),
    }
