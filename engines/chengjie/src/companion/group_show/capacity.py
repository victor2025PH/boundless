"""容量 —— 「这批号到底能铺多少个群」，取**最紧的那条轴**，而不是最乐观的那条。

为什么单独有这一层
------------------
前三条轴各自都能算出一个「能覆盖多少群」，而且它们给出的数**差好几倍**：

* :mod:`~src.companion.group_show.attendance` 成员轴：谁和谁在同一批群里；
* :mod:`~src.companion.group_show.performance` 发言轴：谁和谁在同一批群里开过口；
* :mod:`~src.companion.group_show.roles` 主推轴：一个号在多少个群里演过种草。

三个数分别摆在三张卡上，运营问的却是**一个**问题：「我 10 个号，能不能铺 100 个群？」
他会读到哪个数，取决于他先看到哪张卡——而这三个数里最显眼的偏偏是最乐观的那个。

最危险的一处：``speakers=1`` 时发言轴报「无上限」
------------------------------------------------
每场只让一个号开口确实产生**零个号对**，:func:`~...performance.speaker_budget` 因此
如实报 ``unlimited``。这句话在它自己那条轴上没错，但作为「能铺多少群」的答案是错的：

============  ==================  ==================  ==============
每场开口人数   发言轴（10 个号）    主推轴（10 个号）    **真实上限**
============  ==================  ==================  ==============
4             15 个群             30 个群             15
3             30 个群             30 个群             30
2             90 个群             30 个群             **30**
1             无上限              30 个群             **30**
============  ==================  ==================  ==============

也就是说压缩开口人数把发言轴一路放宽到「无上限」之后，**瓶颈早就换成主推轴了**，
真实上限从 15 涨到 30 就不动了。运营按「无上限」去铺 100 个群，等于让每个号在 10 个
群里当主推——这是最典型、最容易被人肉识别的托特征（见 :mod:`...roles` 模块开头）。

**一个只报最乐观那条轴的容量数字，比不报还糟**：不报的时候运营还会自己保守一点。

第二个杠杆：带货密度（这一层唯一的新知识）
------------------------------------------
发言轴的杠杆是「少一张嘴」（号对数对开口人数是二次的）。主推轴**没有**这个杠杆——
要种草就得有恰好一个主推，跟几个人开口无关，容量 ``号数 × NORMAL_ROLE_CO`` 是线性的。

但它有另一个杠杆：**不是每场戏都得带货**。真人社群里绝大多数对话本来就不推销。设带货
场次占比为 ``d``，主推轴容量就变成 ``号数 × NORMAL_ROLE_CO / d``：

============  ==========  ==========  ==========
带货密度 d     10 个号      15 个号     20 个号
============  ==========  ==========  ==========
100%          30          45          60
50%           60          90          120
30%           100         150         200
============  ==========  ==========  ==========

「10 个号铺 100 个群」于是有了确切答案：**每场 1 个号开口 + 只有 30% 的场次带货**。
这个答案现有任何一张卡都给不出来，而它恰恰是运营最想要的那句话。

Solo 只有在「**一个群只进一个号**」时才真的是 solo
--------------------------------------------------
「每场只让一个号开口」有两种截然不同的做法，安全性差一个数量级：

* **10 个号都加进这 100 个群、每场只让 1 个说话**——发言轴确实零号对，但成员轴上
  每一对号都共享 100 个群。而成员关系**一旦加群就冻结了**（集体退群本身更可疑），
  等于把最坏的账先付掉、再靠轮换掩盖；
* **每个群只加 1 个号**（``seats=1``）——成员轴与发言轴**同时**归零，剩下的约束只有
  单号自身的两条：主推集中度（本模块建模）与发言吞吐（见
  :mod:`~src.companion.group_show.schedule`）。

所以 ``seats`` 传不传是有语义的：``seats > 1`` 才会把成员轴纳入取最小。运营口中的
「solo 模式」如果指的是第一种，那它并不 solo，本模块会如实把成员轴的上限报出来。

设计取舍
--------
* **纯函数、无 I/O、不抛异常**：与本子系统一贯风格一致，脏输入降级成形状完整的空结果。
* **不自己算共现**：发言/成员轴直接调
  :func:`~src.companion.group_show.attendance.max_safe_groups`，主推轴直接用
  :data:`~src.companion.group_show.roles.NORMAL_ROLE_CO`。这一层只做**取最小**和翻译，
  一旦它自己重算一遍，看板上就会出现「容量卡说 30、角色卡说 25」这种没人能解释的分歧。
* **两条轴的安全余量各自保留**：发言轴用 ``NORMAL_PAIR_CO - 1``（贪心排班摸不到理论
  平均），主推轴用足 ``NORMAL_ROLE_CO``（主推可以严格轮转、能摸到平均）。两边余量本
  就该不同，强行统一反而会让某一条与它自己那张卡对不上。
"""
from __future__ import annotations

import logging
import math
from typing import Any, Dict, List, Optional

from src.companion.group_show.attendance import (
    NORMAL_PAIR_CO,
    VERDICT_DANGER,
    VERDICT_SAFE,
    VERDICT_WARN,
    max_safe_groups,
)
from src.companion.group_show.roles import NORMAL_ROLE_CO, is_selling_slot

logger = logging.getLogger(__name__)

#: 轴标识。与看板/CLI 共用，别在渲染侧另起字符串（漂了就对不上颜色和下钻入口）。
AXIS_MEMBER = "member"
AXIS_PAIR = "pair"
AXIS_ROLE = "role"

#: 枚举到几个人同场开口为止。与
#: :data:`~src.companion.group_show.performance.MAX_SPEAKERS_CONSIDERED` 同义同值——
#: 超过 6 个号围着夸同一个产品，共现之外光看画面就假。
MAX_SPEAKERS_CONSIDERED = 6

#: 超出上限多少倍之内还算「差一点」（判词 warn），再多直接 danger。
#: 1.5 不是实测阈值，是个刻意保守的分界：超 50% 以内通常调一调带货密度就能进来，
#: 翻倍以上则一定得动号池或群数，两种处方完全不同，不该共用一盏灯。
OVER_WARN_RATIO = 1.5


def _i(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def role_capacity(*, pool: Any, advocate_ratio: Any = 1.0) -> Optional[int]:
    """主推轴：这些号最多能覆盖几个群而不让任何一个号种草过量。

    Args:
        pool: 号池大小。
        advocate_ratio: 带货场次占比 ``d``（``1.0`` ＝每场都种草）。

    Returns:
        群数上限；``None`` ＝ 这条轴不设限（``d <= 0``，即压根不带货）。

    ``d <= 0`` 返回 ``None`` 而不是 0 或一个巨大的数：不带货就是真的不受主推集中度约束，
    报 0 会把「纯陪聊铺群」这种完全安全的玩法误伤成不可行。
    """
    try:
        p = max(0, _i(pool))
        d = _f(advocate_ratio, 1.0)
        if d <= 0:
            return None
        # 下取整（容量宁可小一格），但要先吸掉二进制误差：30/0.3 在浮点里是
        # 99.99999999999999，直接 int() 会得到 99——于是 recommend_mix 刚算出的
        # 「30% 密度」拿回来自检时会判自己不达标，建议与校验自相矛盾。
        return int(math.floor(p * NORMAL_ROLE_CO / d + 1e-9))
    except Exception:  # noqa: BLE001 —— 容量算不出来按「不设限」，不要反过来卡死运营
        logger.debug("[group_show.capacity] role_capacity 失败（已忽略）",
                     exc_info=True)
        return None


def pair_capacity(*, pool: Any, speakers: Any) -> Optional[int]:
    """发言轴：每场 ``speakers`` 个号开口时最多能覆盖几个群。

    ``speakers <= 1`` 返回 ``None``（零号对，这条轴确实不设限）。这个「不设限」**只对
    这一条轴成立**，别把它当成最终答案——见模块 docstring 那张对照表。
    """
    try:
        k = _i(speakers)
        if k <= 1:
            return None
        return max(0, max_safe_groups(pool=max(0, _i(pool)), seats=k))
    except Exception:  # noqa: BLE001
        logger.debug("[group_show.capacity] pair_capacity 失败（已忽略）",
                     exc_info=True)
        return None


def _axis(axis: str, cap: Optional[int], groups: int, lever: str) -> Dict[str, Any]:
    unlimited = cap is None
    return {
        "axis": axis,
        "max_groups": cap,
        "unlimited": unlimited,
        "fits": True if unlimited else (groups <= cap),
        "lever": lever,
    }


def plan_capacity(
    *,
    pool: Any,
    groups: Any = 0,
    seats: Any = 0,
    speakers: Any = 0,
    advocate_ratio: Any = 1.0,
) -> Dict[str, Any]:
    """给定一套演法，**这套演法**最多能铺多少群、卡在哪条轴上。

    Args:
        pool: 号池大小。
        groups: 想覆盖的群数（``0`` ＝只问上限不问够不够）。
        seats: 每群进几个号（成员轴；``<=1`` 或 ``0`` ＝不问这条轴）。
        speakers: 每场几个号开口（``0`` ＝不问发言轴）。
        advocate_ratio: 带货场次占比。

    Returns:
        ``max_groups`` 三条轴取最小（``None`` ＝全都不设限）／``binding`` 卡在哪条轴
        （``""`` ＝没有瓶颈）／``fits`` 目标群数进不进得来／``axes`` 逐轴明细／
        ``verdict``／``advice``。

    没问到的轴**不进 axes 列表**，而不是用一个大数占位：占位数会参与取最小，
    某天某条轴的缺省值一改，容量就会莫名其妙缩水，且没人查得出是哪来的。
    """
    out: Dict[str, Any] = {
        "pool": 0, "groups": 0, "seats": 0, "speakers": 0, "advocate_ratio": 1.0,
        "axes": [], "max_groups": None, "binding": "", "fits": True,
        "groups_per_account": 0, "verdict": VERDICT_SAFE, "advice": [],
    }
    try:
        p, g = max(0, _i(pool)), max(0, _i(groups))
        s, k = max(0, _i(seats)), max(0, _i(speakers))
        d = max(0.0, _f(advocate_ratio, 1.0))
        out.update(pool=p, groups=g, seats=s, speakers=k, advocate_ratio=d)

        axes: List[Dict[str, Any]] = []
        if s > 1:
            axes.append(_axis(AXIS_MEMBER, max_safe_groups(pool=p, seats=s), g,
                              "减少每群进几个号 / 补号池"))
        if k > 0:
            axes.append(_axis(AXIS_PAIR, pair_capacity(pool=p, speakers=k), g,
                              "减少每场开口人数"))
        axes.append(_axis(AXIS_ROLE, role_capacity(pool=p, advocate_ratio=d), g,
                          "降低带货场次占比 / 补号池"))
        out["axes"] = axes

        bounded = [a for a in axes if not a["unlimited"]]
        if bounded:
            worst = min(bounded, key=lambda a: (_i(a["max_groups"]), a["axis"]))
            out["max_groups"] = _i(worst["max_groups"])
            out["binding"] = str(worst["axis"])
            out["fits"] = g <= _i(worst["max_groups"]) if g else True
        # 单号负载：**只报事实、不判吉凶**。它约束的是「一个号一天说得完这么多场吗」，
        # 属于吞吐问题（:mod:`...schedule` 管节奏），跟共现不是一条轴。这里编一个阈值
        # 出来只会多一盏没有依据的灯；但完全不报也不行——solo 铺得越开，这个数越大，
        # 而它恰恰是 solo 唯一还在增长的成本。
        out["groups_per_account"] = int(math.ceil(g / p)) if (g and p) else 0
        out["verdict"] = _verdict(groups=g, cap=out["max_groups"])
        out["advice"] = _advice(out)
        return out
    except Exception:  # noqa: BLE001 —— 容量卡挂了只该少一块，别拖垮整份报告
        logger.debug("[group_show.capacity] plan_capacity 失败（已忽略）",
                     exc_info=True)
        return out


def _verdict(*, groups: int, cap: Optional[int]) -> str:
    """超出多少算危险。没设上限或没给目标 → 无从判断，一律 safe（不造警报）。"""
    if cap is None or groups <= 0:
        return VERDICT_SAFE
    if groups <= cap:
        return VERDICT_SAFE
    if cap <= 0:
        return VERDICT_DANGER
    return VERDICT_WARN if groups <= cap * OVER_WARN_RATIO else VERDICT_DANGER


_AXIS_NAME = {AXIS_MEMBER: "成员共现", AXIS_PAIR: "发言共现", AXIS_ROLE: "主推集中度"}


def _advice(plan: Dict[str, Any]) -> List[str]:
    """把「卡在哪条轴」翻译成动作。顺序即紧要程度。"""
    advice: List[str] = []
    try:
        g, cap = _i(plan.get("groups")), plan.get("max_groups")
        binding = str(plan.get("binding") or "")
        pool = _i(plan.get("pool"))
        if cap is None:
            advice.append(f"当前演法在三条轴上都不设限（{pool} 个号）——"
                          f"这通常意味着既不带货、每场也只有一个号开口")
            return advice
        name = _AXIS_NAME.get(binding, binding)
        seats = _i(plan.get("seats"))
        if g and g > cap:
            advice.append(f"{pool} 个号按这套演法最多铺 {cap} 个群，目标 {g} 个——"
                          f"卡在**{name}**这条轴上，超了 {g - cap} 个群")
            # seats 必须一起带过去：丢了它算出来的是「只看演出面」的建议，会得到
            # 「铺不动」和「这么改就能铺满」两句自相矛盾的话摆在同一屏上。
            mix = recommend_mix(pool=pool, groups=g, seats=seats)
            if mix.get("fits"):
                advice.append(f"要铺满 {g} 个群，改成：每场 {mix['speakers']} 个号开口，"
                              + _selling_phrase(mix["advocate_ratio"]))
            else:
                need = mix.get("pool_needed") or 0
                advice.append(f"即使压到每场 1 个号开口、带货密度降到 "
                              f"{_pct(mix.get('min_ratio'))}，{pool} 个号也铺不满 {g} 个群"
                              f"——号池至少要 {need} 个")
                if binding == AXIS_MEMBER and seats > 1:
                    # 成员轴是唯一能靠「换个加群方式」归零的轴：每群只进一个号，
                    # 号对根本不存在。这条比补号便宜得多，必须先说。
                    advice.append(f"成员轴的另一条出路是**每群少进几个号**——"
                                  f"每群只进 1 个号时这条轴直接归零，不用补号")
        else:
            advice.append(f"{pool} 个号按这套演法最多铺 {cap} 个群"
                          + (f"，目标 {g} 个，余量 {cap - g}" if g else "")
                          + f"；瓶颈在**{name}**")
        return advice
    except Exception:  # noqa: BLE001 —— 建议炸了不该让容量数字一起消失
        return advice


def _selling_phrase(ratio: Any) -> str:
    """带货密度的人话。``100%`` 说成「只有 100% 的场次带货」会读成病句，而这句话是
    整条建议的落点——读起来别扭运营就会怀疑整个数是不是也算错了。"""
    r = _f(ratio, 1.0)
    return "每场都可以带货" if r >= 1.0 else f"且只有 {_pct(r)} 的场次带货"


def _pct(ratio: Any) -> str:
    try:
        return f"{round(_f(ratio) * 100)}%"
    except Exception:  # noqa: BLE001
        return "?"


#: 带货密度不建议压到这个数以下。低于 5% 意味着 20 场戏才种一次草，那已经不算「炒群」
#: 而是养号——真要这么演，该做的是补号池而不是继续摊薄，否则投入产出比会崩。
MIN_ADVOCATE_RATIO = 0.05


def recommend_mix(*, pool: Any, groups: Any, seats: Any = 0) -> Dict[str, Any]:
    """要铺这么多群，**最不憋屈**的一套演法是什么。

    Args:
        pool: 号池大小。
        groups: 目标群数。
        seats: 每群进几个号（给了就一并校验成员轴）。

    Returns:
        ``speakers`` 每场开口人数／``advocate_ratio`` 带货场次占比／``fits`` 这套演法
        够不够／``pool_needed`` 不够时号池至少要多大／``min_ratio`` 已经压到的密度下限／
        ``binding``／``max_groups``。

    两条轴**互不影响**，所以不需要联合搜索：开口人数只动发言轴，带货密度只动主推轴。
    分别取各自能容下目标的最宽档位，就是同时满足两条轴的最宽演法。

    「最不憋屈」的定义：**开口人数优先于带货密度**。少一张嘴只是这场戏冷清一点，戏还在演、
    关系还在处；带货密度掉下去则直接少一次转化机会。先牺牲热闹度、后牺牲营收，这个次序
    如果反过来，工具给出的建议会一路把带货压到 5%，运营看一眼就知道不能用，然后整套护栏
    连同这条建议一起被绕开。
    """
    out: Dict[str, Any] = {
        "pool": 0, "groups": 0, "speakers": 1, "advocate_ratio": 1.0,
        "fits": False, "pool_needed": 0, "min_ratio": MIN_ADVOCATE_RATIO,
        "binding": "", "max_groups": None,
    }
    try:
        p, g = max(0, _i(pool)), max(0, _i(groups))
        s = max(0, _i(seats))
        out.update(pool=p, groups=g)
        if g <= 0:
            out.update(fits=True, speakers=MAX_SPEAKERS_CONSIDERED)
            return out

        # 发言轴：能撑住目标群数的最多开口人数（1 恒成立＝零号对）
        speakers = 1
        for k in range(2, MAX_SPEAKERS_CONSIDERED + 1):
            cap = pair_capacity(pool=p, speakers=k)
            if cap is not None and g <= cap:
                speakers = k
        out["speakers"] = speakers

        # 主推轴：能撑住目标群数的最高带货密度
        ratio = min(1.0, (p * NORMAL_ROLE_CO) / g) if g else 1.0
        out["advocate_ratio"] = round(ratio, 4)

        plan = plan_capacity(pool=p, groups=g, seats=s, speakers=speakers,
                             advocate_ratio=max(ratio, MIN_ADVOCATE_RATIO))
        out["binding"] = plan.get("binding") or ""
        out["max_groups"] = plan.get("max_groups")
        out["fits"] = bool(plan.get("fits")) and ratio >= MIN_ADVOCATE_RATIO
        if not out["fits"]:
            out["pool_needed"] = pool_needed(groups=g, seats=s)
        return out
    except Exception:  # noqa: BLE001
        logger.debug("[group_show.capacity] recommend_mix 失败（已忽略）",
                     exc_info=True)
        return out


def observed_mix(
    *,
    speech: Any = None,
    memberships: Any = None,
    role_counts: Any = None,
) -> Dict[str, Any]:
    """从台账反推**运营现在实际在用的演法**，而不是让人手填参数。

    Args:
        speech: ``{群: [号, ...]}`` 发言台账 → 每场几个号开口、一共几个群。
        memberships: ``{群: [号, ...]}`` 成员台账 → 每群进了几个号。
        role_counts: ``{(号, 角色槽): 群数}`` → 带货场次占比。

    Returns:
        ``groups``／``seats``／``speakers``／``advocate_ratio``，可直接喂
        :func:`plan_capacity`。

    为什么必须是**观测值**而不是配置值
    ----------------------------------
    「你打算每场让几个号开口」和「你实际每场让了几个号开口」经常不是一回事——护栏放行、
    人工补刀、日常自动回复都会把实际值顶上去。拿配置值算容量，会得出一份「按计划我们很
    安全」的报告，而平台数的是实际发生的消息。这层反推的意义就是让读数落在真实值上。

    代表值取**向上取整的均值**而不是最大值：偶尔一场五个人同台不该把整份容量报告打成
    灾难（那会让运营认定工具在夸大），但持续的平均水平必须如实反映。
    """
    out: Dict[str, Any] = {"groups": 0, "seats": 0, "speakers": 0,
                           "advocate_ratio": 1.0}
    try:
        speech_rows = dict(speech or {})
        groups = len(speech_rows)
        out["groups"] = groups
        if groups:
            total = sum(len(set(v or ())) for v in speech_rows.values())
            out["speakers"] = int(math.ceil(total / groups))
        member_rows = dict(memberships or {})
        if member_rows:
            total = sum(len(set(v or ())) for v in member_rows.values())
            out["seats"] = int(math.ceil(total / len(member_rows)))

        selling = 0
        for key, n in dict(role_counts or {}).items():
            try:
                _, slot = key
            except Exception:  # noqa: BLE001 —— 键形状不对就跳过这一条
                continue
            if is_selling_slot(slot):
                selling += max(0, _i(n))
        if groups:
            out["advocate_ratio"] = round(min(1.0, selling / groups), 4)
        return out
    except Exception:  # noqa: BLE001
        logger.debug("[group_show.capacity] observed_mix 失败（已忽略）",
                     exc_info=True)
        return out


def pool_needed(*, groups: Any, seats: Any = 0,
                advocate_ratio: Any = MIN_ADVOCATE_RATIO) -> int:
    """铺这么多群、号池至少要多大（已按最省的演法算：每场 1 个号开口）。

    成员轴给了 ``seats`` 就一并纳入——它常常才是真瓶颈：每群进 3 个号的话，光「谁和谁
    在同一批群里」这一条就先把号池要求顶上去了，跟开口人数无关。
    """
    try:
        g = max(0, _i(groups))
        if g <= 0:
            return 0
        d = max(_f(advocate_ratio, MIN_ADVOCATE_RATIO), MIN_ADVOCATE_RATIO)
        need = int(math.ceil(g * d / NORMAL_ROLE_CO))
        s = max(0, _i(seats))
        if s > 1:
            from src.companion.group_show.attendance import recommend_pool_size
            need = max(need, _i(recommend_pool_size(groups=g, seats=s)))
        return max(need, 1)
    except Exception:  # noqa: BLE001
        return 0


__all__ = [
    "AXIS_MEMBER",
    "AXIS_PAIR",
    "AXIS_ROLE",
    "MAX_SPEAKERS_CONSIDERED",
    "MIN_ADVOCATE_RATIO",
    "OVER_WARN_RATIO",
    "observed_mix",
    "pair_capacity",
    "plan_capacity",
    "pool_needed",
    "recommend_mix",
    "role_capacity",
]
