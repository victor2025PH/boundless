"""角色集中度 —— 第三条暴露轴：「这个号在多少个群里演过**主推**」。

为什么成员轴与发言轴都守住了还不够
----------------------------------
:mod:`~src.companion.group_show.attendance` 数的是「谁和谁在同一批群里」，
:mod:`~src.companion.group_show.performance` 数的是「谁和谁在同一批群里开过口」。
两条都是**号对**口径——它们回答「这几个号是不是一伙的」。

但把一个号认成托，根本不需要先找出它的同伙：**一个号自己就够了**。它在 40 个群里
都是那个「我用了三个月，真的有效」的人，群主不需要拉成员表、不需要做交集分析、
甚至不需要看第二个号，翻一下这个人的历史发言就完了。这是**单号自曝**，与号对无关，
所以前两条轴无论排得多稀疏都拦不住它。

只有推销性角色是把柄（本模块最重要的一条）
------------------------------------------
角色槽有四个：advocate（主推/种草）、asker（提问）、skeptic（质疑）、bystander（路人）。
它们的暴露性**完全不对称**：

* 一个号在 50 个群里当路人、偶尔问一句「这个多少钱」——真人就是这样，零信号；
* 同一个号在 40 个群里都是主推——一眼假。

所以度量与闸门都必须**按角色区分**（:data:`SELLING_SLOTS`）。一视同仁的宽口径看起来
更安全，实际是最坏的选择：它会把 bystander / asker 这些**掩护**角色一起拦掉，导致
「号不够用了」，运营的下一步动作必然是把整条护栏关掉——那时连主推都不拦了。宁可只守
一个真把柄，也不要守一堆假把柄然后被整体绕过。

这条轴买不到杠杆（与发言轴的关键差别）
--------------------------------------
发言轴最强的产出是「少让一个号开口，安全容量翻三倍」——因为号对数对开口人数是二次的。
角色轴**没有**这个杠杆：每场戏要种草就得有**恰好一个**主推，跟这场几个人开口无关。

===============  ==================  ==================
号池              号对轴可覆盖群数     主推轴可覆盖群数
                 （每场 2 人开口）    （每场 1 个主推）
===============  ==================  ==================
10               90                  **30**
20               380                 **60**
30               870                 **90**
===============  ==================  ==================

也就是说压缩开口人数把号对轴放宽到 90 个群之后，**主推轴就成了新的瓶颈**（还是 30）。
容量是 ``号数 × NORMAL_ROLE_CO``，**线性**，只能靠加号。第二条路是让部分场次干脆不排
主推（只演提问/路人，戏照演但不种草）——听起来像放弃，其实是把种草密度摊薄，
真人社群里绝大多数对话本来就不带货。

设计取舍
--------
* **纯函数、无 I/O**：台账由调用方从发言/演出库派生后传进来。
* **不抛异常**：与本子系统一贯风格一致，脏输入一律降级成形状完整的空结果。这是旁路
  度量，挂了不该拖垮整条编排链。
* **判词复用 attendance 的三档常量**，不自己定义字符串：控制台三张卡（成员/发言/角色）
  要能并排看，判词值漂了颜色就对不上。
* ``counts`` 的形状 ``{(号, 角色槽): 群数}`` 与
  :func:`~src.companion.group_show.performance.co_performance` 返回的
  ``{(号A, 号B): 群数}`` 刻意对称——两张卡共用同一套渲染。
"""
from __future__ import annotations

import logging
import zlib
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from src.companion.group_show.attendance import (
    VERDICT_DANGER,
    VERDICT_SAFE,
    VERDICT_WARN,
)

logger = logging.getLogger(__name__)

#: 只有推销性角色会暴露「这是个托」。路人/提问/质疑是掩护，铺再多群都正常——把它们
#: 一起纳管会拦掉掩护角色，逼运营关掉整条护栏，比不设护栏更糟。
SELLING_SLOTS: Tuple[str, ...] = ("advocate",)

#: 一个号在几个群里演过主推还像真人。同
#: :data:`~src.companion.group_show.attendance.NORMAL_PAIR_CO` 的量级直觉：真心喜欢一个
#: 产品的人会在两三个群里安利，不会在四十个群里。与那边一样，这是**保守的经验判断，
#: 不是实测阈值**——平台的真实判据不可知，任何声称精确的数字都是编的。
NORMAL_ROLE_CO: int = 3

#: 危险线（含）。对齐 :data:`~src.companion.group_show.attendance.ALARM_PAIR_CO` 的定法：
#: 到这个量级，翻一下这个号的历史发言就能看出它是干什么的。
ALARM_ROLE_CO: int = 8

#: 超标号占比达到这个比例 → 判词直接升 danger（不必等最坏那个号到 :data:`ALARM_ROLE_CO`）。
#: 一个号超量主推只暴露那一个号；半个班底都在超量主推，暴露的是「这批号是一支带货队伍」，
#: 后者对聚类算法致命得多。
FLEET_DANGER_RATIO: float = 0.5

#: 「整支队伍都在带货」判据的最小规模。号少于这个数时「50% 的号超标」是平凡真、不含信息
#: ——那就是某一个号的问题，:data:`max_role_co` 已经说清楚了。
FLEET_MIN_ACCOUNTS: int = 3

#: 主推占比超过它就该稀释。这是**比绝对数更强的信号**：一个号只在 3 个群说过话、3 个群
#: 全在主推，等于它在这个平台上从没以别的身份出现过；同样主推 3 个群但一共说过 50 个群
#: 的号，看起来就是个普通热心人。
CONCENTRATION_WARN: float = 0.6

#: 占比低于这个主推群数不进建议：1/1 恒等于 100%，拿它报警只会刷屏。
CONCENTRATION_MIN_GROUPS: int = 2


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


def _tie_break(*parts: str) -> int:
    """确定性打破平局；不用内置 ``hash()``（对 str 每进程加盐，换进程换一套结果）。"""
    return zlib.crc32("\x1f".join(parts).encode("utf-8", "replace"))


def _as_list(raw: Any) -> List[Any]:
    """把外部传来的「一批东西」洗成 list（与 attendance 同款，含裸字符串特判）。

    单个字符串按**一个元素**处理：直接 ``iter`` 会把 ``"a01"`` 拆成三个「账号」，
    分母凭空多两个号、集中度被稀释——这是最难发现的那类静默破口。
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        return [raw] if raw.strip() else []
    if isinstance(raw, Mapping):
        return [raw]
    try:
        return list(raw)
    except TypeError:
        return []


def _account_id(raw: Any) -> str:
    """从「裸 id / 注册表行 / CastMember」里取账号 id，取不到返回空串。"""
    if isinstance(raw, Mapping):
        return _s(raw.get("account_id"))
    got = getattr(raw, "account_id", None)
    if got is not None:
        return _s(got)
    return _s(raw)


def is_selling_slot(slot: Any) -> bool:
    """这个角色槽是不是**推销性**的（＝会暴露「这是个托」的那一类）。

    大小写不敏感：剧本里的槽名是人手写的 YAML，``Advocate`` 与 ``advocate`` 必须同判。
    差一个大写就整条闸门失效，且失效方式是静默放行——最坏的一种。

    单独抽成公开函数是为了给「自定义推销槽」留一个唯一改点：某些剧本会声明
    ``promoter`` / ``reviewer`` 这类自造槽，它们同样是把柄，但
    :data:`SELLING_SLOTS` 是模块常量、不该在运行时改。真要扩，改这里一处即可。
    """
    try:
        name = _s(slot).lower()
        return bool(name) and name in SELLING_SLOTS
    except Exception:  # noqa: BLE001
        return False


def _key(raw: Any) -> Tuple[str, str]:
    """``(号, 角色槽)`` 规范键；形状不对返回 ``("", "")``，调用方丢弃这一行。

    刻意**不**接受字符串键：``tuple("ab")`` 会得到 ``("a", "b")``——一个两字母的脏键
    会被静默解析成「号 a 演过角色 b」混进度量，之后再也认不出来。
    """
    if isinstance(raw, (tuple, list)) and len(raw) == 2:
        return _s(raw[0]), _s(raw[1])
    return "", ""


def _count_of(
    counts: Optional[Mapping[Tuple[str, str], int]],
    account: str,
    slot: str,
) -> int:
    """查 ``(号, 槽)`` 的群数。槽名大小写与台账不一致时回落小写键，绝不因此漏判。"""
    if not isinstance(counts, Mapping):
        return 0
    got = counts.get((account, slot))
    if got is None:
        got = counts.get((account, slot.lower()))
    return max(0, _i(got))


def _clean_counts(counts: Any) -> Dict[Tuple[str, str], int]:
    """洗成 ``{(号, 槽): 群数}``：脏键/脏值丢弃而不连累其余行。"""
    out: Dict[Tuple[str, str], int] = {}
    if not isinstance(counts, Mapping):
        return out
    try:
        items = list(counts.items())
    except Exception:  # noqa: BLE001 —— 伪 Mapping 的 items() 也可能炸
        return out
    for raw_key, raw_n in items:
        account, slot = _key(raw_key)
        if not account or not slot:
            continue
        # 负数当 0：台账做差算出来的负值是上游 bug，不该反过来给这个号「加余量」
        out[(account, slot)] = max(0, _i(raw_n))
    return out


# ── 执行层闸门：让这个号演这个角色，会不会把它推过线 ────────────────────────


def role_headroom(
    account: Any,
    slot: Any,
    counts: Optional[Mapping[Tuple[str, str], int]],
    *,
    limit: int = NORMAL_ROLE_CO,
) -> int:
    """这个号**还能在几个新群里演这个角色**才到线（``<=0`` ＝到线，不该再排）。

    非推销角色恒返回 ``limit``（＝满余量、永不到线）：路人/提问/质疑铺多少群都是真人
    行为，把它们也扣余量会让掩护角色先耗尽，最后每场只剩主推能上——正好把风险拉满。
    """
    try:
        cap = max(0, _i(limit))
        who, name = _s(account), _s(slot)
        if not who or not name or not is_selling_slot(name):
            return cap
        # 夹到 0：余量是给人读的数，「还差 -37 个群」没有意义，越线就是越线
        return max(0, cap - _count_of(counts, who, name))
    except Exception:  # noqa: BLE001 —— 算不出来一律放行，护栏不该反过来卡死正常演出
        return max(0, _i(limit))


def admits_role(
    account: Any,
    slot: Any,
    counts: Optional[Mapping[Tuple[str, str], int]],
    *,
    limit: int = NORMAL_ROLE_CO,
) -> bool:
    """让这个号演这个角色，会不会把它的角色集中度推过线。

    ``limit <= 0`` ＝护栏关闭恒放行；没台账也恒放行（**无数据不该变成无演出**）。
    与 :func:`~src.companion.group_show.performance.admits_speaker` 一样，闸门最坏
    把主推换个人演，绝不能演变成「这场戏排不出来」——排不出来的护栏会被直接关掉。
    """
    try:
        cap = _i(limit)
        if cap <= 0 or not isinstance(counts, Mapping):
            return True
        name = _s(slot)
        if not _s(account) or not name or not is_selling_slot(name):
            return True
        return role_headroom(account, name, counts, limit=cap) > 0
    except Exception:  # noqa: BLE001
        return True


# ── 度量 ────────────────────────────────────────────────────────────────────


def _verdict(max_role: int, fleet_ratio: float, accounts: int) -> str:
    """由「最扎眼的那一个号」与「整支队伍的超标率」共同定档，两条判据各自都能升级。

    主指标取 max 而不是平均：平台/群主抓的是最扎眼的那一个。九个号干干净净、一个号在
    40 个群里主推，这副牌照样废——平均值 4 会把它报成「略高」，那是灾难性的误导。
    """
    if max_role >= ALARM_ROLE_CO:
        return VERDICT_DANGER
    if max_role > NORMAL_ROLE_CO:
        fleet = (accounts >= FLEET_MIN_ACCOUNTS
                 and fleet_ratio >= FLEET_DANGER_RATIO)
        return VERDICT_DANGER if fleet else VERDICT_WARN
    return VERDICT_SAFE


def role_metrics(
    counts: Any,
    *,
    pool: Optional[Iterable[Any]] = None,
) -> Dict[str, Any]:
    """角色集中度度量。``counts`` 形如 ``{(号, 槽): 该号在几个群演过这个角色}``。

    Args:
        counts: 角色台账。**掩护角色照样可以传进来**，本函数只统计
            :data:`SELLING_SLOTS`——调用方不必先过滤，也就不会两边过滤口径漂掉。
        pool: 完整号池。一个主推都没演过的号也要计入分母，否则「超标号占比」会随着
            排得越干净、干净的号越多而**上升**，指标方向就反了。

    Returns:
        ``max_role_co`` 最扎眼的号在几个群演过推销角色（**主指标**）／``worst``
        ``[号, 槽]`` 谁最扎眼／``by_account`` ``{号: 演推销角色的总群数}``（含 0）／
        ``accounts`` 参与统计的号数／``avg_role_co`` 全体均值／``hot_account_count``
        超标号数／``fleet_ratio`` 超标号占比／``used`` 已排出的主推场次总数／
        ``capacity`` ``号数 × NORMAL_ROLE_CO`` 的主推容量／``saturation`` 容量用了几成／
        ``verdict`` 判词／``top_roles`` ``[[号, 槽, 群数], ...]`` 最扎眼的前 8 条／
        ``selling_slots`` 本次按哪些槽算的（回显用，免得看板上的数说不清口径）。
    """
    result: Dict[str, Any] = {
        "accounts": 0, "max_role_co": 0, "worst": None, "avg_role_co": 0.0,
        "by_account": {}, "hot_account_count": 0, "fleet_ratio": 0.0,
        "used": 0, "capacity": 0, "saturation": 0.0,
        "verdict": VERDICT_SAFE, "top_roles": [],
        "selling_slots": list(SELLING_SLOTS),
    }
    try:
        clean = _clean_counts(counts)
        per_account: Dict[str, int] = {}
        selling: List[Tuple[str, str, int]] = []

        for (account, slot), n in clean.items():
            # 演过掩护角色的号同样是「在编的演员」，要进分母；只是不进分子
            per_account.setdefault(account, 0)
            if not is_selling_slot(slot) or n <= 0:
                continue
            selling.append((account, slot, n))
            per_account[account] = per_account.get(account, 0) + n

        for raw in _as_list(pool):
            account = _account_id(raw)
            if account:
                per_account.setdefault(account, 0)

        ranked = sorted(selling, key=lambda row: (-row[2], row[0], row[1]))
        accounts = len(per_account)
        used = sum(per_account.values())
        capacity = accounts * max(NORMAL_ROLE_CO, 0)
        hot = sum(1 for n in per_account.values() if n > NORMAL_ROLE_CO)
        fleet_ratio = (hot / accounts) if accounts else 0.0
        max_role = ranked[0][2] if ranked else 0

        result.update({
            "accounts": accounts,
            "max_role_co": max_role,
            "worst": [ranked[0][0], ranked[0][1]] if ranked else None,
            "avg_role_co": (used / accounts) if accounts else 0.0,
            "by_account": dict(sorted(per_account.items())),
            "hot_account_count": hot,
            "fleet_ratio": fleet_ratio,
            "used": used,
            "capacity": capacity,
            "saturation": (used / capacity) if capacity else 0.0,
            "verdict": _verdict(max_role, fleet_ratio, accounts),
            "top_roles": [[a, s, n] for a, s, n in ranked[:8]],
        })
        return result
    except Exception:  # noqa: BLE001 —— 度量炸了也要给形状完整的空报告
        logger.debug("[group_show.roles] role_metrics 失败（已忽略）", exc_info=True)
        return result


# ── 读数报告：控制台卡与 CLI 共用 ───────────────────────────────────────────


def _concentration_rows(
    by_account: Mapping[str, int],
    speaking_groups: Mapping[Any, Any],
) -> Tuple[Dict[str, float], List[Tuple[str, float, int, int]]]:
    """算 ``主推群数 / 开口群数``，并按占比从高到低排出明细行。

    分母缺失或为 0 而分子 > 0 时按 **1.0** 记：两份台账对不上时宁可高报——这是风险读数，
    漏报一个全职托的代价远大于多提醒一次。
    """
    spoke: Dict[str, int] = {}
    for raw_account, raw_n in speaking_groups.items():
        account = _account_id(raw_account)
        if account:
            spoke[account] = max(0, _i(raw_n))

    conc: Dict[str, float] = {}
    rows: List[Tuple[str, float, int, int]] = []
    for account, sold in by_account.items():
        total = spoke.get(account, 0)
        if sold <= 0:
            ratio = 0.0
        elif total <= 0:
            ratio = 1.0
        else:
            ratio = min(1.0, sold / total)
        conc[account] = ratio
        rows.append((account, ratio, sold, total))
    rows.sort(key=lambda row: (-row[1], -row[2], row[0]))
    return conc, rows


def _plan_handoffs(metrics: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """给每个超标的 ``(号, 槽)`` 点名一个**具体的接手人**。

    「建议注意风险」不是建议。运营要的是「下一场把 a03 的主推让给 a07」——所以这里必须
    真的挑出一个还有余量的号，挑不出来就如实说挑不出来（那是「该补号了」的硬信号）。

    一个接手人只推荐一次：把五个超标的主推全塞给同一个闲号，只是把问题挪个地方。
    """
    out: List[Dict[str, Any]] = []
    try:
        by_account = metrics.get("by_account")
        if not isinstance(by_account, Mapping):
            return out
        taken: set = set()
        for row in (metrics.get("top_roles") or ())[:5]:
            if not isinstance(row, (list, tuple)) or len(row) != 3:
                continue
            account, slot, n = _s(row[0]), _s(row[1]), _i(row[2])
            if n <= NORMAL_ROLE_CO:
                continue  # 没越线的不用换人
            best: Optional[str] = None
            best_key: Optional[Tuple[int, int, str]] = None
            for cand, sold in by_account.items():
                cand = _s(cand)
                if not cand or cand == account or cand in taken:
                    continue
                headroom = NORMAL_ROLE_CO - max(0, _i(sold))
                if headroom <= 0:
                    continue
                # 先挑最闲的；同样闲时按 crc32 轮换，避免总是字典序第一个号被喂满
                key = (_i(sold), _tie_break(slot, cand), cand)
                if best_key is None or key < best_key:
                    best, best_key = cand, key
            if best:
                taken.add(best)
            out.append({
                "account": account, "slot": slot, "count": n,
                "to": best or "",
                "to_headroom": (NORMAL_ROLE_CO - _i(by_account.get(best)))
                if best else 0,
            })
        return out
    except Exception:  # noqa: BLE001 —— 换人方案炸了不该让整份报告消失
        logger.debug("[group_show.roles] _plan_handoffs 失败（已忽略）",
                     exc_info=True)
        return out


def role_report(
    counts: Any,
    *,
    speaking_groups: Optional[Mapping[Any, Any]] = None,
    pool: Optional[Iterable[Any]] = None,
) -> Dict[str, Any]:
    """给控制台卡与 CLI 共用的一份读数 + 可执行建议。

    Args:
        counts: ``{(号, 槽): 群数}`` 角色台账。
        speaking_groups: ``{号: 开过口的群数}``（调用方从发言台账派生）。给了就多出
            ``concentration``／``worst_concentration`` 两项——占比比绝对数更有说服力：
            3 个群主推 / 一共只在 3 个群说过话 ＝ 100% 都在推销，比 3/50 可疑得多。
            **不传就不出这两项**（不猜分母：猜错会把干净的号报成全职托）。
        pool: 完整号池，理由同 :func:`role_metrics`。

    Returns:
        ``roles`` 一份 :func:`role_metrics`／``verdict`` 判词（与 ``roles.verdict``
        同值，卡片顶部直接读）／``handoffs`` ``[{account, slot, count, to,
        to_headroom}]`` 逐条换人方案（``to`` 为空 ＝ 号池里没人能接）／``advice``
        中文可执行建议（已按紧要程度排序）／``concentration`` ``{号: 主推占比}``
        与 ``worst_concentration`` ``[号, 占比]``——**仅在传了 ``speaking_groups``
        时存在**。
    """
    result: Dict[str, Any] = {
        "roles": {}, "verdict": VERDICT_SAFE, "handoffs": [], "advice": [],
    }
    try:
        metrics = role_metrics(counts, pool=pool)
        result["roles"] = metrics
        result["verdict"] = _s(metrics.get("verdict")) or VERDICT_SAFE

        rows: List[Tuple[str, float, int, int]] = []
        if isinstance(speaking_groups, Mapping):
            by_account = metrics.get("by_account")
            conc, rows = _concentration_rows(
                by_account if isinstance(by_account, Mapping) else {},
                speaking_groups,
            )
            result["concentration"] = conc
            hottest = next(
                (r for r in rows
                 if r[2] >= CONCENTRATION_MIN_GROUPS and r[1] > 0), None)
            result["worst_concentration"] = (
                [hottest[0], hottest[1]] if hottest else None)

        handoffs = _plan_handoffs(metrics)
        result["handoffs"] = handoffs
        result["advice"] = _build_advice(
            metrics=metrics, handoffs=handoffs, concentration_rows=rows)
        return result
    except Exception:  # noqa: BLE001
        logger.debug("[group_show.roles] role_report 失败（已忽略）", exc_info=True)
        return result


def _build_advice(
    *,
    metrics: Mapping[str, Any],
    handoffs: List[Dict[str, Any]],
    concentration_rows: List[Tuple[str, float, int, int]],
) -> List[str]:
    """把读数翻译成运营真能执行的动作。顺序即紧要程度。

    这里刻意**不**给「让这个号少发言」这类建议：少发言解决不了角色集中——它在的那几个群
    里照样是那个主推。能动的只有两件事：换个号来演，或者这一场干脆不排主推。
    """
    advice: List[str] = []
    try:
        verdict = _s(metrics.get("verdict")) or VERDICT_SAFE
        worst = metrics.get("worst") or None
        max_co = _i(metrics.get("max_role_co"))
        accounts = _i(metrics.get("accounts"))
        used = _i(metrics.get("used"))
        capacity = _i(metrics.get("capacity"))
        hot = _i(metrics.get("hot_account_count"))
        ratio = float(metrics.get("fleet_ratio") or 0.0)
        first = handoffs[0] if handoffs else None

        # ① 最扎眼的那一个号——先给具体的换人动作
        if verdict != VERDICT_SAFE and isinstance(worst, (list, tuple)) and worst:
            who, slot = _s(worst[0]), _s(worst[1] if len(worst) > 1 else "")
            if first and _s(first.get("to")):
                advice.append(
                    f"把 {who} 的 {slot} 让给 {first['to']}"
                    f"（它还能接 {_i(first.get('to_headroom'))} 个群）——{who} 已经在 "
                    f"{max_co} 个群里演{slot}，接下来几场只让它演 bystander／asker 打掩护")
            else:
                advice.append(
                    f"{who} 已经在 {max_co} 个群里演 {slot}，而号池里**没有还能接手的号**"
                    f"——先补号；在补上之前，这些群这一场干脆不排主推"
                    f"（只留提问／路人，戏照演但不种草）")

        # ② 整支队伍都在带货：换人换不动，这是编制问题
        if hot >= FLEET_MIN_ACCOUNTS and ratio >= FLEET_DANGER_RATIO:
            advice.append(
                f"{accounts} 个号里有 {hot} 个都在超量主推（{ratio:.0%}）——扎眼的已经不是"
                f"某一个号，而是整个班底看起来都在带货；让其中一半这几周只演路人／提问，"
                f"把主推集中到剩下的号并轮着来")

        # ③ 容量透支：这条轴唯一的杠杆就是号数，必须把数字算给运营看
        if capacity and used > capacity:
            need = -(-used // max(NORMAL_ROLE_CO, 1))
            advice.append(
                f"主推容量已经透支：{accounts} 个号 × {NORMAL_ROLE_CO} 个群 ＝ {capacity} "
                f"个群的容量，实际排了 {used} 个——号池要补到 {need} 个。这条轴**买不到"
                f"杠杆**：把每场开口人数从 3 压到 2 能让号对轴的容量翻三倍，但每场照样"
                f"只有一个主推，主推容量只跟号数成正比")

        # ④ 占比：绝对数不高但「这个号只干这个」，比绝对数更能说明问题
        for account, share, sold, total in concentration_rows[:2]:
            if share < CONCENTRATION_WARN or sold < CONCENTRATION_MIN_GROUPS:
                continue
            tail = (f"（{sold}/{total} 个群）" if total > 0
                    else f"（{sold} 个群主推，但发言台账里查不到它开过口）")
            advice.append(
                f"{account} 开过口的群里 {share:.0%} 都在演主推{tail}——绝对数不高，"
                f"但这个号从没以别的身份出现过，比「在 50 个群里主推 3 次」可疑得多；"
                f"给它排几场只演路人／提问的戏，把比例稀释下去")

        if not advice:
            if not used:
                advice.append(
                    f"还没有任何号演过推销角色（掩护角色不计入本轴）——这条轴零暴露；"
                    f"当前 {accounts} 个号的主推容量是 {capacity} 个群")
            else:
                who = (_s(worst[0])
                       if isinstance(worst, (list, tuple)) and worst else "?")
                advice.append(
                    f"最扎眼的是 {who} 在 {max_co} 个群演主推，仍在常人区间"
                    f"（≤{NORMAL_ROLE_CO}）内；{accounts} 个号的主推容量共 {capacity} "
                    f"个群，已用 {used}，还能再排 {max(capacity - used, 0)} 场")
        return advice
    except Exception:  # noqa: BLE001 —— 建议生成炸了不该让整份报告消失
        logger.debug("[group_show.roles] _build_advice 失败（已忽略）", exc_info=True)
        return advice


__all__ = [
    "ALARM_ROLE_CO",
    "CONCENTRATION_MIN_GROUPS",
    "CONCENTRATION_WARN",
    "FLEET_DANGER_RATIO",
    "FLEET_MIN_ACCOUNTS",
    "NORMAL_ROLE_CO",
    "SELLING_SLOTS",
    "admits_role",
    "is_selling_slot",
    "role_headroom",
    "role_metrics",
    "role_report",
]
