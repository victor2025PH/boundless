"""演出矩阵 —— 「谁在这个群里**开过口**」，成员矩阵之外第二条、也是唯一还能动的暴露面。

为什么要把它和出席矩阵分开
--------------------------
:mod:`~src.companion.group_show.attendance` 管的是**成员矩阵**：谁在哪些群里。那一层
决定了风险的上界，但它有个致命性质——**一旦加了群就冻结了**。退群本身是可疑动作
（正常人不会集体退 30 个群），所以成员矩阵一旦排坏，几乎没有回头路。

演出矩阵管的是**谁这一场开口**，性质正相反：**每一场都能重排**。同一批号在同一个群里，
这次让 A、B 说，下次让 C、D 说，「两个号在同一个群都发过言」的实际次数可以远低于
「两个号在同一个群里」的次数。

两条信号在平台侧的地位也不同：

* **成员关系是被动风险**——要把各群成员名单拉出来做交集分析才暴露，成本高、通常只在
  已经被盯上之后才做；
* **消息是主动风险**——每一条都实时流过反垃圾管道，共现是顺手就能统计出来的。

所以「成员面已经超标」不等于「已经完蛋」：演出面才是平台最先看到的东西，而它完全可控。
本模块的存在意义就是把这条**还能动的自由度**变成可读数、可决策、可执行的东西。

一个反直觉但决定性的结论：**每场开口人数是比号池更强的杠杆**
----------------------------------------------------------
共现的对次是 ``群数 × C(每场开口人数, 2)``，对开口人数是**二次**的：

===============  ==================  ==================
每场开口人数      10 个号能安全覆盖    15 个号能安全覆盖
===============  ==================  ==================
4                15 个群             35 个群
3                30 个群             70 个群
2                **90 个群**         **210 个群**
1                无上限（零号对）     无上限（零号对）
===============  ==================  ==================

也就是说「10 个号想铺 100 个群」——按每场 3 个号开口是**超标 3 倍**，按每场 2 个号开口
却**绰绰有余**。运营的直觉往往是「那我去多养几个号」，但少让一个号开口的效果，
比多养五个号还大，且立刻生效、零成本。这张表是本模块最该被看见的产出。

``seats=1`` 的特判（容易写错，单独说明）
----------------------------------------
:func:`~src.companion.group_show.attendance.max_safe_groups` 在 ``seats <= 1`` 时返回 0，
那是**成员面**的正确守卫——一个人的「群戏」不成立。但在**演出面**，每场只有一个号开口
恰恰意味着**一个号对都不产生**，是最安全的演法（单号铺群正是这么玩的）。直接套用会把
最安全的配置报成最危险，:func:`speaker_budget` 因此对 ``k <= 1`` 单独放行。

设计取舍
--------
* **不重新实现矩阵**：演出台账的形状（``{群: [号, ...]}``）与成员台账完全一致，共现统计
  直接复用 :func:`~src.companion.group_show.attendance.attendance_metrics`。两套矩阵代码
  必然漂，而漂的后果是看板上两个数不可比——那正是这张对照表唯一的价值所在。
* **纯函数、无 I/O**：台账由调用方派生后传进来，本模块不认识任何一个库。
* **不抛异常**：与本子系统一贯风格一致，坏数据一律降级成「形状完整的空报告」。

台账要取**两个来源的并集**（调用方的责任，别只喂一个）
------------------------------------------------------
* :meth:`~src.companion.group_show.store.GroupShowStore.performance_ledger`
  —— 编排出来的戏；
* :meth:`~src.inbox.store.InboxStore.group_speech_ledger`
  —— 收件箱里**所有**群内出向消息（日常自动回复 / 坐席手发 / 主动触达）。

只喂前者会得到「一场戏都没演过 ⇒ 安全」的结论，可这些号很可能早就靠日常回复在几十个
群里互相同框了——平台数的是消息，不区分是不是编排的。风险读数最不该犯的错就是这种
**假安全**，所以宁可把口径取全——合并那一步由 ``ledgers.read_speech()`` 统一做掉，
调用方（影子 CLI / 导播台 / 真发链路）一律经 ``ledgers.read_show_context()`` 拿数，
**不要**各自再拼一遍（口径漂移比读不到账更难发现）。
"""
from __future__ import annotations

import logging
import zlib
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from src.companion.group_show.attendance import (
    NORMAL_PAIR_CO,
    VERDICT_DANGER,
    VERDICT_SAFE,
    VERDICT_WARN,
    attendance_metrics,
    co_attendance,
    max_safe_groups,
    shared_groups,
)

logger = logging.getLogger(__name__)

#: 演出共现的默认回看窗（天）。共现必须**随时间淡出**：跑上几个月，每一对都会在某个群
#: 同台过，这个数会饱和到失去分辨力，既没法给看板报警、也没法指导「这场该让谁开口」。
#: 平台侧的反垃圾窗口本身也是滚动的，陈年旧账不在它的工作集里。
DEFAULT_WINDOW_DAYS = 30

#: :func:`speaker_budget` 枚举到几人为止。超过 6 个号同场开口，共现之外「六个人围着夸
#: 同一个产品」本身就假得离谱，不值得再算。
MAX_SPEAKERS_CONSIDERED = 6

#: 每场开口人数降到这个数就不产生任何号对——演出面的绝对安全区。
SOLO_SPEAKERS = 1


def _s(value: Any) -> str:
    if value is None:
        return ""
    try:
        return str(value).strip()
    except Exception:  # noqa: BLE001 —— 外部脏数据的 __str__ 都可能炸
        return ""


def _i(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _pair(a: str, b: str) -> Tuple[str, str]:
    """无序号对的规范键（与 attendance 同款，两边必须一致才能对照）。"""
    return (a, b) if a <= b else (b, a)


def _tie_break(*parts: str) -> int:
    """确定性打破平局；不用内置 ``hash()``（对 str 每进程加盐，换进程换一套结果）。"""
    return zlib.crc32("\x1f".join(parts).encode("utf-8", "replace"))


# ── 演出矩阵 ────────────────────────────────────────────────────────────────


def performance_metrics(
    ledger: Any,
    *,
    pool: Optional[Iterable[Any]] = None,
) -> Dict[str, Any]:
    """演出共现度量。输入 ``{群: [在该群开过口的号, ...]}``。

    刻意只是 :func:`~src.companion.group_show.attendance.attendance_metrics` 的转调用：
    两个矩阵**必须用同一套算法**，否则看板上「成员共现 8 / 演出共现 2」这组对照数就不可比，
    而可比正是它唯一的用处。这个函数存在的价值只是给调用点一个说得清的名字。
    """
    return attendance_metrics(ledger, pool=pool)


def co_performance(ledger: Any) -> Dict[Tuple[str, str], int]:
    """``{(号A, 号B): 两个号共同开过口的群数}``——喂给选角器做「优先挑没同台过的」。"""
    return co_attendance(ledger)


# ── 开口人数预算（本模块最该被看见的产出） ──────────────────────────────────


def speaker_budget(*, pool: int, groups: int) -> Dict[str, Any]:
    """现有号池要覆盖这些群，**每场最多让几个号开口**才不超标。

    Args:
        pool: 号池大小（能上台的号总数）。
        groups: 要覆盖的群数。

    Returns:
        ``max_speakers`` 建议的每场开口人数上限（``0`` ＝号池太小，连两人同台都撑不住，
        只能单号演）／``solo_only`` 是否只剩单号演这一条路／``options`` 逐档明细
        （每档 ``speakers`` / ``max_groups`` 能覆盖多少群 / ``fits`` 够不够 /
        ``unlimited`` 是否无上限）／``pool``／``groups``。

    ``speakers <= 1`` 恒为 ``unlimited``：一个号开口产生零个号对，铺多少群都不增加共现。
    这条不能靠 :func:`~src.companion.group_show.attendance.max_safe_groups` 推导——它在
    ``seats <= 1`` 时返回 0（那是成员面「一个人不成群戏」的守卫，见模块 docstring）。
    """
    result: Dict[str, Any] = {
        "pool": 0, "groups": 0, "max_speakers": 0,
        "solo_only": True, "options": [],
    }
    try:
        p, g = max(0, _i(pool)), max(0, _i(groups))
        result["pool"], result["groups"] = p, g
        options: List[Dict[str, Any]] = []
        best = 0
        for k in range(1, MAX_SPEAKERS_CONSIDERED + 1):
            if k <= SOLO_SPEAKERS:
                # 零号对：铺多少群都不产生共现
                options.append({"speakers": k, "max_groups": None,
                                "unlimited": True, "fits": True})
                best = max(best, k)
                continue
            cap = max_safe_groups(pool=p, seats=k)
            fits = g <= cap
            options.append({"speakers": k, "max_groups": cap,
                            "unlimited": False, "fits": fits})
            if fits:
                best = max(best, k)
        result["options"] = options
        result["max_speakers"] = best
        # best 只剩 1 ＝ 连「每场 2 个号」都撑不住，唯一安全演法是单号出场
        result["solo_only"] = best <= SOLO_SPEAKERS
        return result
    except Exception:  # noqa: BLE001 —— 预算炸了也要给形状完整的空报告
        logger.debug("[group_show.performance] speaker_budget 失败（已忽略）",
                     exc_info=True)
        return result


# ── 号对余量（执行层护栏：预算管「一场几张嘴」，这里管「哪两张嘴不能再同台」） ──


def pair_headroom(
    a: Any,
    b: Any,
    counts: Optional[Mapping[Tuple[str, str], int]],
    *,
    limit: int = NORMAL_PAIR_CO,
) -> int:
    """这一对号**还能再同台几个群**才到线（``<=0`` ＝已经到线，不该再一起上）。

    平台真正在数的是号对，不是人头。所以按号对判是**直接**约束，按人数判只是代理量——
    而且这个代理在两个方向上都判反：第三个号如果跟前两个从没同台过，让他上只花掉三个
    全新的号对（很便宜）却会被人数上限一刀拦掉；反过来两个号如果已经在五个群里同框，
    人数上限（2 人）却照样放行，而这一对恰恰是最扎眼的那个。
    """
    try:
        x, y = _s(a), _s(b)
        if not x or not y or x == y or not isinstance(counts, Mapping):
            return max(0, _i(limit))
        # 夹到 0：余量是给人读的数，「还差 -96 个群」没有意义，越线就是越线
        return max(0, max(0, _i(limit)) - _i(counts.get(_pair(x, y))))
    except Exception:  # noqa: BLE001 —— 算不出来一律放行，护栏不该反过来卡死正常演出
        return max(0, _i(limit))


def admits_speaker(
    candidate: Any,
    chosen: Optional[Iterable[Any]],
    counts: Optional[Mapping[Tuple[str, str], int]],
    *,
    limit: int = NORMAL_PAIR_CO,
) -> bool:
    """再让这个号开口，会不会把**某一对**推过线。

    ``limit <= 0`` ＝护栏关闭恒放行；没台账也恒放行（无数据不该变成无演出）。
    第一个人永远进得来——护栏最坏把一场戏压成独白，但绝不能压成空场：空场会让运营
    直接把护栏关掉，那就一点保护都不剩了。
    """
    try:
        cap = _i(limit)
        if cap <= 0 or not isinstance(counts, Mapping):
            return True
        me = _s(candidate)
        if not me:
            return True
        for other in (chosen or ()):
            if pair_headroom(me, other, counts, limit=cap) <= 0:
                return False
        return True
    except Exception:  # noqa: BLE001
        return True


def resolve_speaker_cap(
    *,
    requested: Any = 0,
    budget: Any = 0,
    allow_over: bool = False,
) -> Dict[str, Any]:
    """把「预算建议」落成「本场实际执行的开口人数上限」+ 一句能审计的理由。

    与 :func:`admits_speaker` 的分工——两条都要，因为它们**一个预防一个兜底**：

    * 预算是**预测性**的：还没演之前就知道「10 个号铺 100 个群，每场只能 2 人开口」。
      按 3 人演下去，等号对余量真的见底时，一批号对已经全部顶在线上了；
    * 号对余量是**反应性**的：它只在伤害累积到线上才拦，但它认的是真账，能兜住
      「预算是拿陈旧/残缺台账算出来的」这种情况。

    Returns:
        ``effective`` 本场生效的上限（``0`` ＝不限）／``requested`` 运营要的／
        ``budget`` 预算给的／``capped`` 是否被压／``source`` 来源
        （``auto``＝没指定按预算走｜``explicit``＝指定且在预算内｜``capped``＝指定超标被压回｜
        ``override``＝指定超标且显式放行）／``reason`` 给人看的一句话。
    """
    out: Dict[str, Any] = {"effective": 0, "requested": 0, "budget": 0,
                           "capped": False, "source": "none", "reason": ""}
    try:
        req, bud = max(0, _i(requested)), max(0, _i(budget))
        out["requested"], out["budget"] = req, bud
        if req <= 0:
            out["effective"], out["source"] = bud, "auto" if bud else "none"
            if bud:
                out["reason"] = f"按预算自动限到每场 {bud} 个号开口"
            return out
        if bud and req > bud:
            if allow_over:
                out.update(effective=req, source="override",
                           reason=f"已显式放行超预算：本场 {req} 个号开口，"
                                  f"预算只有 {bud}")
            else:
                out.update(effective=bud, capped=True, source="capped",
                           reason=f"本场要 {req} 个号开口，超出预算 {bud}，已压回预算内"
                                  f"（确要突破需显式放行）")
            return out
        out.update(effective=req, source="explicit")
        return out
    except Exception:  # noqa: BLE001 —— 解析炸了按「不限」走，与护栏上线前行为一致
        logger.debug("[group_show.performance] resolve_speaker_cap 失败（已忽略）",
                     exc_info=True)
        return out


# ── 下钻：把「最坏一对共享 8 个群」翻译成可执行的动作 ────────────────────────

#: 下钻列几对。运营一次能记住的就那么几对，列满 8 对等于没重点。
MAX_HOT_PAIRS = 5

#: 每对列几个群。举证到能认出「哦是那批群」就够了，超出只报数量。
MAX_HOT_GROUPS = 6


def hot_pairs(
    ledger: Any,
    *,
    counts: Optional[Mapping[Tuple[str, str], int]] = None,
    limit: int = NORMAL_PAIR_CO,
    max_pairs: int = MAX_HOT_PAIRS,
    max_groups: int = MAX_HOT_GROUPS,
) -> List[Dict[str, Any]]:
    """最扎眼的几对号 + 它们缠在哪几个群 + 闸门还剩多少余量。

    Args:
        ledger: ``{群: [号, ...]}``（成员台账或演出台账都行，两条轴共用这一份实现）。
        counts: 已算好的共现表；省得调用方为了下钻再扫一遍台账。
        limit: 号对上线阈值，用于算 ``headroom``（与 :func:`admits_speaker` 同一个）。
        max_pairs / max_groups: 截断，理由见 :data:`MAX_HOT_PAIRS` / :data:`MAX_HOT_GROUPS`。

    Returns:
        每项 ``pair``／``count`` 共享几个群／``groups`` 是哪几个（截断后）／``more``
        还有几个没列／``headroom`` 还能再同框几个群／``blocked`` 是否已到线。

    为什么要带 ``headroom``/``blocked`` 而不只是列群
    ------------------------------------------------
    光给证据只完成了一半。运营看到「这一对已经 8 个群」的下一个问题必然是「那我现在
    还能不能让它俩一起上」，而这件事**系统已经替他决定了**（``admits_speaker`` 到线即
    拒）。把闸门的当前判定与证据放在同一行，读数与系统行为才对得上；否则会出现最伤
    信任的那种错位——卡片红着，运营手动躲着这两个号，其实闸门早就在拦了，他白躲；
    或者反过来卡片红着他以为被拦了，实际台账是空的、闸门根本没生效。
    """
    out: List[Dict[str, Any]] = []
    try:
        table = counts if isinstance(counts, Mapping) else co_attendance(ledger)
        cap = max(0, _i(limit))
        # 次数降序；同次数按号对字典序，保证同一份台账每次下钻顺序一致（不然运营
        # 每刷新一次看到的「最坏一对」都在换，会以为矩阵在抖）
        ranked = sorted(table.items(), key=lambda kv: (-_i(kv[1]), kv[0]))
        for (a, b), n in ranked[:max(0, _i(max_pairs))]:
            count = _i(n)
            groups = shared_groups(ledger, a, b, limit=max(0, _i(max_groups)))
            out.append({
                "pair": [a, b],
                "count": count,
                "groups": groups,
                "more": max(count - len(groups), 0),
                "headroom": max(cap - count, 0),
                "blocked": bool(cap) and count >= cap,
            })
        return out
    except Exception:  # noqa: BLE001 —— 下钻挂了只该少一块，主读数必须还在
        logger.debug("[group_show.performance] hot_pairs 失败（已忽略）",
                     exc_info=True)
        return out


# ── 双读数暴露报告 ──────────────────────────────────────────────────────────


def _ratio(perf: int, member: int) -> float:
    """演出共现相对成员共现压到了几成（越小越好）。成员面为 0 时无意义 → 0.0。"""
    return (perf / member) if member > 0 else 0.0


def exposure_report(
    memberships: Any,
    performances: Any,
    *,
    pool: Optional[Iterable[Any]] = None,
) -> Dict[str, Any]:
    """成员面 vs 演出面双读数 + 可执行建议。控制台那张卡与 CLI 共用这一份。

    Args:
        memberships: ``{群: [号, ...]}`` 成员台账（谁在群里）。
        performances: ``{群: [号, ...]}`` 演出台账（谁开过口）。
        pool: 完整号池；号池里一个群都没排到的号也要进分母（理由同
            :func:`~src.companion.group_show.attendance.attendance_metrics`）。

    Returns:
        ``membership``/``performance`` 两份度量／``verdict`` 取两者更差的那个／
        ``suppression`` 轮换把暴露压到了几成／``budget`` 开口人数预算／
        ``advice`` 可执行建议（已按紧要程度排序）。

    判词取**两者更差**而不是只看演出面：成员面超标虽然是被动风险，但它是演出面的上界，
    且它已经冻结、只会随时间累积。只报演出面等于告诉运营「你安全」，然后他继续加群。
    """
    result: Dict[str, Any] = {
        "membership": {}, "performance": {}, "verdict": VERDICT_SAFE,
        "suppression": 0.0, "budget": {}, "advice": [],
    }
    try:
        member_m = attendance_metrics(memberships, pool=pool)
        perf_m = performance_metrics(performances, pool=pool)
        # 下钻挂在各自那条轴上（而不是并成一张表）：两条轴的同一对号含义完全不同——
        # 成员面是「风险还只是潜在的」，演出面是「已经真的一起说过话了」。混在一起看，
        # 运营会把还没兑现的风险当成已兑现的，然后去动那些其实还安全的群。
        member_m["hot_pairs"] = hot_pairs(memberships)
        perf_m["hot_pairs"] = hot_pairs(performances)
        result["membership"] = member_m
        result["performance"] = perf_m

        member_max = _i(member_m.get("max_pair_co"))
        perf_max = _i(perf_m.get("max_pair_co"))
        result["suppression"] = _ratio(perf_max, member_max)

        rank = {VERDICT_SAFE: 0, VERDICT_WARN: 1, VERDICT_DANGER: 2}
        verdicts = [_s(member_m.get("verdict")) or VERDICT_SAFE,
                    _s(perf_m.get("verdict")) or VERDICT_SAFE]
        result["verdict"] = max(verdicts, key=lambda v: rank.get(v, 0))

        # 号池口径取两边的并集：只在成员台账里、还没开过口的号同样是可调度的演员
        accounts = max(_i(member_m.get("accounts")), _i(perf_m.get("accounts")))
        group_count = max(_i(member_m.get("groups")), _i(perf_m.get("groups")))
        budget = speaker_budget(pool=accounts, groups=group_count)
        result["budget"] = budget

        result["advice"] = _build_advice(
            member_max=member_max, perf_max=perf_max,
            member_verdict=verdicts[0], perf_verdict=verdicts[1],
            budget=budget, accounts=accounts, groups=group_count,
        )
        return result
    except Exception:  # noqa: BLE001
        logger.debug("[group_show.performance] exposure_report 失败（已忽略）",
                     exc_info=True)
        return result


def _build_advice(
    *,
    member_max: int,
    perf_max: int,
    member_verdict: str,
    perf_verdict: str,
    budget: Mapping[str, Any],
    accounts: int,
    groups: int,
) -> List[str]:
    """把两个读数翻译成运营真能执行的动作。顺序即紧要程度。

    这里刻意**不**给「让号退群」这类建议：集体退群本身就是可疑动作，而且成员面已经
    冻结的事实不会因为退群而消失（平台侧留有历史）。能动的只有演出面。
    """
    advice: List[str] = []
    try:
        max_speakers = _i(budget.get("max_speakers"))
        solo_only = bool(budget.get("solo_only"))

        if perf_verdict == VERDICT_DANGER:
            if solo_only:
                advice.append(
                    f"演出共现已到 {perf_max}——{accounts} 个号铺 {groups} 个群，"
                    f"两个号同场开口都撑不住；改成**每场只让一个号开口**（零号对），"
                    f"或者把号池补到能撑住两人同台再说")
            else:
                advice.append(
                    f"演出共现已到 {perf_max}——把每场开口人数压到 {max_speakers} 个"
                    f"（现在明显超了）。少让一个号开口比多养五个号还管用，且立刻生效")
        elif perf_verdict == VERDICT_WARN:
            advice.append(
                f"演出共现 {perf_max} 已过常人区间（{NORMAL_PAIR_CO}）——"
                f"每场开口人数控制在 {max_speakers} 个以内，并让开口的号轮着来")

        if member_verdict != VERDICT_SAFE:
            if perf_verdict == VERDICT_SAFE:
                advice.append(
                    f"成员共现 {member_max} 已超标，但那是**已经冻结的账**（集体退群"
                    f"本身更可疑，不要退）——目前靠轮换把实际开口共现压在 {perf_max}，"
                    f"这条路是对的。**别再往这批群里加号**，新群用新号铺")
            else:
                advice.append(
                    f"成员共现 {member_max} 超标且已冻结——新群一律换一批号去铺，"
                    f"不要继续拿这批号往上叠")

        if not advice:
            advice.append(
                f"成员共现 {member_max}／演出共现 {perf_max}，都在常人区间内。"
                f"当前号池每场最多可让 {max_speakers} 个号开口")
        return advice
    except Exception:  # noqa: BLE001 —— 建议生成炸了不该让整份报告消失
        return advice


# ── 选角侧：这一场该让谁开口 ────────────────────────────────────────────────


def added_co_performance(
    account: Any,
    chosen: Optional[Iterable[Any]],
    counts: Optional[Mapping[Tuple[str, str], int]],
) -> int:
    """这个号再上台，会给本场新增多少「号对共现」。越小越该选。

    :func:`rank_speakers` 与 :func:`~src.companion.group_show.casting.cast_roles` 共用
    这一处实现。两边各写一遍必然漂，而漂了之后「看板报的暴露」与「选角实际在优化的
    东西」就是两回事——那种不一致排查起来极其费劲，因为两边单独看都对。
    """
    try:
        me = _s(account)
        if not me or not isinstance(counts, Mapping):
            return 0
        total = 0
        for other in (chosen or ()):
            peer = _s(other)
            if peer and peer != me:
                total += _i(counts.get(_pair(me, peer)))
        return total
    except Exception:  # noqa: BLE001 —— 打分炸了按 0 分处理，退化成纯轮换
        return 0


def rank_speakers(
    members: Sequence[Any],
    *,
    co_performance_counts: Optional[Mapping[Tuple[str, str], int]] = None,
    chosen: Optional[Iterable[Any]] = None,
    seed: str = "",
) -> List[str]:
    """把一个群的可选号按「上台后新增多少共现」从小到大排。

    Args:
        members: 候选号（裸 id 或带 ``account_id`` 的行）。
        co_performance_counts: :func:`co_performance` 的产出。缺省 ＝ 全 0，退化成纯
            确定性轮换（与不接台账时的行为一致）。
        chosen: 本场已经定下来的号——排序是**相对已选集**算的，所以每选一个都要重排。
        seed: 打平局用的盐（一般传群标识），让不同群的顺位天然错开。

    这是 :func:`~src.companion.group_show.casting.cast_roles` 的排序依据，抽出来单放
    是为了能独立测：选角那边还叠着指纹硬约束与角色优先级，混在一起测不出这一层的对错。
    """
    out: List[str] = []
    try:
        counts = co_performance_counts if isinstance(
            co_performance_counts, Mapping) else {}
        picked = [_s(x) for x in (chosen or ())]
        picked = [x for x in picked if x]

        scored: List[Tuple[int, int, str]] = []
        seen: set = set()
        for raw in (members or ()):
            account = (_s(raw.get("account_id")) if isinstance(raw, Mapping)
                       else _s(getattr(raw, "account_id", None) or raw))
            if not account or account in seen:
                continue
            seen.add(account)
            added = added_co_performance(account, picked, counts)
            scored.append((added, _tie_break(seed, account), account))
        scored.sort()
        out = [account for _, _, account in scored]
        return out
    except Exception:  # noqa: BLE001 —— 排序炸了返回空，调用方回落既有顺序
        logger.debug("[group_show.performance] rank_speakers 失败（已忽略）",
                     exc_info=True)
        return out


def worst_verdict(*verdicts: Any) -> str:
    """取一组判词里**最差**的那个（safe < warn < danger），认不出来的一律当 safe。

    三条可检测轴（共现／角色／节奏）各出各的判词，看板顶部却只能亮一盏灯。取最差而
    不是取平均或多数：三轴里只要有一条已经露馅，另外两条再干净也救不回来——平台是按
    「最可疑的那个特征」下手的，不是按平均分。
    """
    rank = {VERDICT_SAFE: 0, VERDICT_WARN: 1, VERDICT_DANGER: 2}
    worst, score = VERDICT_SAFE, 0
    for v in verdicts:
        s = rank.get(_s(v), 0)
        if s > score:
            worst, score = _s(v), s
    return worst


__all__ = [
    "DEFAULT_WINDOW_DAYS",
    "MAX_HOT_GROUPS",
    "MAX_HOT_PAIRS",
    "MAX_SPEAKERS_CONSIDERED",
    "SOLO_SPEAKERS",
    "added_co_performance",
    "admits_speaker",
    "co_performance",
    "exposure_report",
    "hot_pairs",
    "pair_headroom",
    "performance_metrics",
    "rank_speakers",
    "resolve_speaker_cap",
    "speaker_budget",
    "worst_verdict",
]
