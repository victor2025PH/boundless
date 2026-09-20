"""出席矩阵 —— 回答「这批号该进哪些群、在哪些群里演，才不像一伙的」。

与 :mod:`~src.companion.group_show.linkage` 的分工
--------------------------------------------------
``linkage`` 管**网络出口**（同一个 IP 出来的号算一组）。本模块管**共同出席**：同一批号
反复出现在同一批群里，这件事本身就是特征。两者正交，而在多号多群模型下，**共同出席是
主导风险，出口 IP 只是次要项**——这个主次关系与直觉相反，值得展开：

现实里两个朋友共享两三个群很常见，所以少量重合不构成信号。但 10 个账号共享 100 个群，
真人身上不会发生；平台一次成员表聚合就能把这一撮号圈出来，**根本不需要看 IP**。更麻烦
的是群成员列表是公开的，群主和反广告 bot 肉眼也能查。而且它会连锁——某个群出事，这批号
在其余 99 个群里同时暴露。出口可以买代理解决，「这 10 个号共享 100 个群」买什么都解决不了。

三个正交的可检测面
------------------
::

    出席矩阵   谁在哪些群里        plan_attendance   —— 本模块主线
    时间轴     什么时候进的群      schedule_joins    —— 静态矩阵覆盖不到
    内容指纹   在群里扮演什么角色  rotate_roles      —— 矩阵管不到

三者缺一不可：矩阵排得再稀疏，如果三个号同一天前后脚进同一个群、进去之后同一个号每次都
是夸产品那个，照样一眼假。

为什么目标函数是 min-max 而不是 min-sum
---------------------------------------
平台盯的是**最扎眼的那一对**。44 对号干净、1 对共享 80 个群，这副牌照样废。所以选人时先
压「加进去之后最坏的那一对会变成多少」，再谈总量。判词另外看**团伙率**（超标号对的占比）：
一对号重度超标只暴露那两个号，全班底两两轻度超标暴露的是整个班底，后者对聚类算法更致命，
所以它能独立把判词升级到 danger。

为什么不建议退群
----------------
退群动作本身有痕迹，而且消除不了已经产生的共同出席历史——收益远小于代价。存量超编的正解
是让多出来的号在该群**不发言**，靠后续新群把重合度稀释掉。

本模块是**纯函数**：无 I/O、无全局状态、不抛异常，号与群的数据由调用方准备好传进来。
"""
from __future__ import annotations

import zlib
from dataclasses import dataclass, field
from typing import (
    Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple,
)

# ── 风险刻度 ────────────────────────────────────────────────────────────────
#
# 这两档是**保守的经验判断，不是实测阈值**——平台的真实判据不可知，任何声称精确的数字
# 都是编的。定档依据：真人熟人之间共享群数通常个位数，且不随「铺了多少群」线性放大；
# 我们的号如果重合数随覆盖群数同步增长，那条增长曲线本身就是机器痕迹。

#: 与真人无异的共同出席上限（含）。超过即进 warn。
NORMAL_PAIR_CO = 3

#: 危险线（含）。到这个量级基本等于自曝是同一批号。
ALARM_PAIR_CO = 8

#: 超标号对占比达到这个比例 → 整个班底成簇，判词直接升 danger（不必等 max 到 ALARM）。
CLIQUE_DANGER_RATIO = 0.5

#: 团伙判据的最小规模。两个号只有一对，「100% 的号对都超标」是平凡真、不含信息——
#: 那是一对号的问题，``max_pair_co`` 已经说清楚了；**成簇至少要三个号**才谈得上。
CLIQUE_MIN_ACCOUNTS = 3

#: 单个号挂在超过这么多群里，加群动作本身就显眼（与号对重合是两回事）。
BUSY_ACCOUNT_GROUPS = 30

VERDICT_SAFE = "safe"
VERDICT_WARN = "warn"
VERDICT_DANGER = "danger"

#: 一场戏的默认在场人数。3 是「不像双簧」的下限；多群模型下在场越少重合越低，
#: 所以默认取下限而不是取大。
DEFAULT_SEATS = 3

#: 加群排期：每个号每天最多加几个群。
DEFAULT_JOINS_PER_DAY = 3

#: 角色占比超过它就提醒轮换不均（单槽剧本除外——那是没得轮换，不是失衡）。
ROLE_SHARE_WARN = 0.5


def _s(value: Any) -> str:
    """任意值 → 干净字符串（``None`` / 异常对象一律成空串，绝不抛）。"""
    if value is None:
        return ""
    try:
        return str(value).strip()
    except Exception:  # noqa: BLE001 —— 外部数据的 __str__ 都可能炸
        return ""


def _pair(a: str, b: str) -> Tuple[str, str]:
    """无向号对的规范键：``(a,b)`` 与 ``(b,a)`` 必须落到同一个桶，否则重合度算一半。"""
    return (a, b) if a <= b else (b, a)


def _tiebreak(*parts: str) -> int:
    """确定性打破平局。

    刻意不用内置 ``hash()``——它对 str 每进程加盐，换个进程就换一套排班表，而运营要照着
    这张表分几天手工加群，第二天重开页面换一套等于这份表作废。
    """
    return zlib.crc32("\x1f".join(parts).encode("utf-8", "replace"))


def _n_pairs(n: int) -> int:
    """``C(n, 2)``。"""
    return n * (n - 1) // 2 if n > 1 else 0


def _account_id(raw: Any) -> str:
    """从「裸 id / 注册表行 / CastMember」里取账号 id，取不到返回空串。"""
    if isinstance(raw, Mapping):
        return _s(raw.get("account_id"))
    got = getattr(raw, "account_id", None)
    if got is not None:
        return _s(got)
    return _s(raw)


def _group_id(raw: Any) -> str:
    """从「裸 key / 群行」里取群标识。注册表与 inbox 两种命名都认。"""
    if isinstance(raw, Mapping):
        for key in ("group_id", "chat_key", "group_key", "id", "key"):
            got = _s(raw.get(key))
            if got:
                return got
        return ""
    return _s(raw)


def _as_list(raw: Any) -> List[Any]:
    """把外部传来的「一批东西」洗成 list，形状不对时返回空。

    单个字符串按**一个元素**处理：裸字符串是可遍历的，直接 ``iter`` 会把 ``"tg1"`` 拆成
    ``t``/``g``/``1`` 三个「账号」——排班表看起来完全正常，实际在给三个不存在的号排班，
    是最难发现的那类静默破口。
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


# ── 共同出席计数 ────────────────────────────────────────────────────────────


def _clean_rows(
    rows: Any,
) -> Dict[str, List[str]]:
    """洗成 ``{群: [号, ...]}``：去空、去重、保持顺序；脏行丢弃而不连累其余。"""
    out: Dict[str, List[str]] = {}
    if not isinstance(rows, Mapping):
        return out
    for raw_group, raw_members in rows.items():
        group = _group_id(raw_group)
        if not group:
            continue
        members: List[str] = []
        seen: set = set()
        for raw in _as_list(raw_members):
            account = _account_id(raw)
            if account and account not in seen:
                seen.add(account)
                members.append(account)
        out[group] = members
    return out


def co_attendance(rows: Any) -> Dict[Tuple[str, str], int]:
    """算 ``{(号A, 号B): 共同出席的群数}``；从没同框过的对不入表（省内存，别当 0 缺失）。"""
    counts: Dict[Tuple[str, str], int] = {}
    for members in _clean_rows(rows).values():
        ordered = sorted(set(members))
        for i, a in enumerate(ordered):
            for b in ordered[i + 1:]:
                key = _pair(a, b)
                counts[key] = counts.get(key, 0) + 1
    return counts


def shared_groups(rows: Any, a: Any, b: Any, *, limit: int = 0) -> List[str]:
    """这一对号**具体**在哪些群同框（成员面）／同时开过口（演出面）。

    Args:
        rows: ``{群: [号, ...]}``，与 :func:`co_attendance` 同一份台账。
        a, b: 两个号（顺序无关）。
        limit: 最多返回几个群（``<=0`` ＝不截断）。

    Returns:
        群标识列表，**按台账原序**——不排序是有意的：台账通常按加群/开口时间排下来，
        原序读出来就是「这一对是怎么一步步缠到一起的」，字典序会把这条线索洗掉。

    :func:`attendance_metrics` 只回答「最坏一对共享几个群」。那个数没法执行：运营看到
    「共现 8」既不知道是哪两个号，也不知道该动哪几个群，于是这条读数只能当心情指标。
    有了群清单，动作就具体了——**接下来别再让这两个号同时在这几个群里开口**。
    """
    try:
        me, peer = _account_id(a), _account_id(b)
        if not me or not peer or me == peer:
            return []
        out: List[str] = []
        for group, members in _clean_rows(rows).items():
            if me in members and peer in members:
                out.append(group)
                if limit > 0 and len(out) >= limit:
                    break
        return out
    except Exception:  # noqa: BLE001 —— 下钻只是诊断，读不出来不该连累整份报告
        return []


def _verdict(max_pair: int, clique_ratio: float, accounts: int = 0) -> str:
    """由「最坏一对」与「团伙率」共同定档，两条判据各自都能升级。"""
    if max_pair >= ALARM_PAIR_CO:
        return VERDICT_DANGER
    if max_pair > NORMAL_PAIR_CO:
        clique = (accounts >= CLIQUE_MIN_ACCOUNTS
                  and clique_ratio >= CLIQUE_DANGER_RATIO)
        return VERDICT_DANGER if clique else VERDICT_WARN
    return VERDICT_SAFE


def attendance_metrics(
    assignments: Any,
    *,
    pool: Optional[Iterable[Any]] = None,
) -> Dict[str, Any]:
    """出席度量：这批号在这批群里重合到什么程度。

    Args:
        assignments: ``{群: [号, ...]}``。
        pool: 可选的完整号池。**一个群都没排到的号也要计入分母**——只对「有交集的号对」
            求平均会把均值抬高，号池越大、排得越稀疏，被漏掉的 0 值对越多，于是「排得
            越好平均分越难看」，指标方向就反了。

    Returns:
        ``max_pair_co`` 最坏一对共享几个群（主指标）／``avg_pair_co`` 全体号对均值／
        ``zero_pairs`` 从没同框过的号对数／``hot_pair_count`` 超标号对数／
        ``clique_ratio`` 超标号对占比／``verdict`` 判词／``accounts`` 号数／
        ``groups`` 群数／``groups_per_account`` 每号进了几个群／``worst_pair`` 最坏的是哪一对。
    """
    result: Dict[str, Any] = {
        "groups": 0, "accounts": 0, "max_pair_co": 0, "worst_pair": None,
        "avg_pair_co": 0.0, "zero_pairs": 0, "hot_pair_count": 0,
        "clique_ratio": 0.0, "verdict": VERDICT_SAFE, "groups_per_account": {},
        "top_pairs": [],
    }
    try:
        clean = _clean_rows(assignments)
        per_account: Dict[str, int] = {}
        for members in clean.values():
            for account in members:
                per_account[account] = per_account.get(account, 0) + 1
        for raw in _as_list(pool):
            account = _account_id(raw)
            if account:
                per_account.setdefault(account, 0)

        counts = co_attendance(clean)
        ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        total_pairs = _n_pairs(len(per_account))
        max_pair = ranked[0][1] if ranked else 0
        hot = sum(1 for n in counts.values() if n > NORMAL_PAIR_CO)
        clique = (hot / total_pairs) if total_pairs else 0.0
        avg = (sum(counts.values()) / total_pairs) if total_pairs else 0.0

        result.update({
            "groups": len(clean),
            "accounts": len(per_account),
            "max_pair_co": max_pair,
            "worst_pair": list(ranked[0][0]) if ranked else None,
            "avg_pair_co": avg,
            "zero_pairs": max(total_pairs - len(counts), 0),
            "hot_pair_count": hot,
            "clique_ratio": clique,
            "verdict": _verdict(max_pair, clique, len(per_account)),
            "groups_per_account": dict(sorted(per_account.items())),
            "top_pairs": [[a, b, n] for (a, b), n in ranked[:8]],
        })
        return result
    except Exception:  # noqa: BLE001 —— 度量炸了也要给一份形状完整的空报告
        return result


# ── 号池测算 ────────────────────────────────────────────────────────────────
#
# 关系式：G 个群、每群 s 人 → 贡献 ``G * C(s,2)`` 个「对次」，号池 P 提供 ``C(P,2)`` 个
# 不同的号对，摊下来每对 ``G*C(s,2) / C(P,2)`` 次。**分母是号池的平方**，所以加号的收益
# 是二次的、砍群的收益只是线性的——运营的直觉往往是「那我少做几个群」，那条路性价比低
# 得多，两个测算函数都据此给建议。
#
# 反解时按 ``NORMAL_PAIR_CO - 1`` 留一格余量：公式算的是**平均**，而贪心达成的**最坏值**
# 通常比平均高约 1。余量给少了推荐值不达标，给多了白白多要号——两侧都有门禁钉着。

_SIZING_TARGET = max(NORMAL_PAIR_CO - 1, 1)


def recommend_pool_size(*, groups: int, seats: int = DEFAULT_SEATS) -> int:
    """要安全覆盖 ``groups`` 个群，号池至少要多大。"""
    try:
        g, s = int(groups), int(seats)
        if g <= 0 or s <= 1:
            return 0
        pair_slots = g * _n_pairs(s)
        need_pairs = pair_slots / _SIZING_TARGET
        pool = max(s, 2)
        while _n_pairs(pool) < need_pairs:
            pool += 1
            if pool > 100000:  # 防御：参数离谱时不转死循环
                break
        return pool
    except Exception:  # noqa: BLE001
        return 0


def max_safe_groups(*, pool: int, seats: int = DEFAULT_SEATS) -> int:
    """现有这些号，最多能安全铺几个群（:func:`recommend_pool_size` 的反函数）。"""
    try:
        p, s = int(pool), int(seats)
        if p <= 1 or s <= 1:
            return 0
        return int(_n_pairs(p) * _SIZING_TARGET // _n_pairs(s))
    except Exception:  # noqa: BLE001
        return 0


# ── 台账拆分 ────────────────────────────────────────────────────────────────


def split_ledger(
    ledger: Any,
    groups: Any,
) -> Tuple[Dict[str, List[str]], Dict[str, List[str]]]:
    """出席台账 → ``(existing, history)``：本次要排的群 vs 只计入风险的老群。

    这条规则**只能有一处实现**（导播台与 CLI 共用）：各写一遍必然漂，而漂的后果是两边
    给出不同的表，运营会以为其中一份算错了。
    """
    existing: Dict[str, List[str]] = {}
    history: Dict[str, List[str]] = {}
    try:
        if not isinstance(ledger, Mapping):
            return existing, history
        planned = {_group_id(g) for g in _as_list(groups)}
        planned.discard("")
        for raw_group, raw_members in ledger.items():
            group = _group_id(raw_group)
            if not group:
                continue
            members = [_account_id(m) for m in _as_list(raw_members)]
            members = [m for m in members if m]
            if group in planned:
                existing[group] = members
            else:
                history[group] = members
        return existing, history
    except Exception:  # noqa: BLE001 —— 台账来自库/前端 body，形状没保证
        return {}, {}


# ── 排班 ────────────────────────────────────────────────────────────────────


@dataclass
class AttendancePlan:
    """一张出席排班表。

    ``assignments`` 是**终态**（存量 + 新加），``already`` / ``joins`` 是它的两个切片：
    前者已经是既成事实，后者才是运营真正要去执行的动作。分开给是因为运营最需要的是
    「我今天还要加哪些」，而不是「最终应该长什么样」。
    """

    assignments: Dict[str, Tuple[str, ...]] = field(default_factory=dict)
    already: Dict[str, Tuple[str, ...]] = field(default_factory=dict)
    joins: Dict[str, Tuple[str, ...]] = field(default_factory=dict)
    metrics: Dict[str, Any] = field(default_factory=dict)
    problems: List[str] = field(default_factory=list)
    advice: List[str] = field(default_factory=list)
    #: 本表按每群几个号排的（回显用：页面上「3 人 × 12 群」要跟表对得上）
    seats: int = DEFAULT_SEATS

    @property
    def total_joins(self) -> int:
        """还要执行多少次「加群」动作。"""
        return sum(len(v) for v in self.joins.values())


def plan_attendance(
    pool: Any,
    groups: Any,
    *,
    seats: int = DEFAULT_SEATS,
    existing: Any = None,
    history: Any = None,
    fingerprint_groups: Optional[Mapping[str, str]] = None,
    max_groups_per_account: Optional[int] = None,
    seed: str = "",
) -> AttendancePlan:
    """排出席矩阵：每个群派号池里的哪几个号。

    Args:
        pool: 号池（裸 id / 注册表行都认）。
        groups: 要覆盖的群清单（裸 key / 群行都认）。
        seats: 每个群派几个号。
        existing: ``{群: [已在群里的号]}``——**存量不动，只排增量**。运营不可能为了重排
            而退群重加，不支持增量的话这张表第二次跑就没法用了。
        history: ``{老群: [号]}``——不排班，但**计入风险度量**。真实用法是分批铺群，
            只算本批 → 每批都显示「安全」，而这些号身上早压着几十个共同的老群。
        fingerprint_groups: ``{号: 出口组}``，次级优化项，见下。
        max_groups_per_account: 单号进群数硬上限；宁可留下排不满的群也不突破。
        seed: 换一个种子出另一套等价优度的表（同一份输入恒定复现）。

    出口分组为什么只是次级优化
    --------------------------
    两条轴冲突时**共同出席赢**。举例：4 个号只有 6 种配对，要铺 6 个群就必须每种配对都用
    一次，包括那对同宿主的；此时若坚持回避同出口，就得让某个配对重复出现，反而推高共同
    出席——拿主要风险去换次要风险，是亏的。何况把它做成硬闸的话，在「所有号都在同一台
    机器上」这个真实配置下每群只能派 1 个号，功能直接不可用。
    """
    plan = AttendancePlan()
    try:
        # ① 归一化输入 ------------------------------------------------------
        accounts: List[str] = []
        seen: set = set()
        for raw in _as_list(pool):
            account = _account_id(raw)
            if account and account not in seen:
                seen.add(account)
                accounts.append(account)

        group_keys: List[str] = []
        gseen: set = set()
        for raw in _as_list(groups):
            group = _group_id(raw)
            if group and group not in gseen:
                gseen.add(group)
                group_keys.append(group)

        want = 0
        try:
            want = int(seats)
        except (TypeError, ValueError):
            want = 0

        plan.seats = max(want, 0)
        problems: List[str] = []
        if want <= 0:
            problems.append("每群在场人数（seats）必须大于 0")
        if not accounts:
            problems.append("号池为空，排不了班")
        if not group_keys:
            problems.append("群清单为空，排不了班")
        if problems:
            plan.problems = problems
            plan.metrics = attendance_metrics({}, pool=accounts)
            return plan

        if len(accounts) < want:
            problems.append(
                f"号池只有 {len(accounts)} 个，每群要 {want} 个座位——凑不满，"
                f"这张表执行不了")

        cap = None
        if max_groups_per_account is not None:
            try:
                cap = max(int(max_groups_per_account), 0)
            except (TypeError, ValueError):
                cap = None

        allow = set(accounts)
        fp = {_s(k): _s(v) for k, v in (fingerprint_groups or {}).items() if _s(k)}
        planned_set = set(group_keys)

        # ② 存量：只认「在册的群 × 在册的号」，其余忽略（别让不相干的行污染排班） --
        existing_clean = {
            g: [m for m in members if m in allow]
            for g, members in _clean_rows(existing).items()
            if g in planned_set
        }
        existing_clean = {g: m for g, m in existing_clean.items() if m}

        # ③ 历史：本次要排的群不算历史（同一个群不能既是存量又是历史，会重复计数） --
        history_clean = {
            g: [m for m in members if m in allow]
            for g, members in _clean_rows(history).items()
            if g not in planned_set
        }

        counts: Dict[Tuple[str, str], int] = {}
        history_load: Dict[str, int] = {}

        def _absorb(members: Sequence[str]) -> None:
            ordered = sorted(set(members))
            for i, a in enumerate(ordered):
                for b in ordered[i + 1:]:
                    key = _pair(a, b)
                    counts[key] = counts.get(key, 0) + 1

        for members in history_clean.values():
            _absorb(members)
            for account in members:
                history_load[account] = history_load.get(account, 0) + 1

        # load ＝**总敞口**（历史 + 本次），不是「本次排了几个群」。已经挂在 50 个老群里
        # 的号不该因为「这张表里它还是 0」而被继续派活——负载均衡与单号上限都按总数算，
        # 否则每批新群都从零起算，一个号会被一批一批地喂到爆。
        load: Dict[str, int] = {
            account: history_load.get(account, 0) for account in accounts
        }

        # ④ 逐群排班 --------------------------------------------------------
        assignments: Dict[str, Tuple[str, ...]] = {}
        already: Dict[str, Tuple[str, ...]] = {}
        joins: Dict[str, Tuple[str, ...]] = {}
        overfull: List[str] = []
        unfilled: List[str] = []
        capped_out = False

        for group in group_keys:
            kept = list(existing_clean.get(group, ()))
            if kept:
                already[group] = tuple(kept)
            if len(kept) > want:
                overfull.append(group)

            chosen = list(kept)
            for account in kept:
                load[account] = load.get(account, 0) + 1

            new: List[str] = []
            while len(chosen) < want:
                best: Optional[str] = None
                best_key: Optional[Tuple[int, int, int, int, int]] = None
                for account in accounts:
                    if account in chosen:
                        continue
                    if cap is not None and load.get(account, 0) >= cap:
                        continue
                    # 主目标：加进去之后**最坏的那一对**会变成多少（min-max）
                    worst = 0
                    total = 0
                    for other in chosen:
                        n = counts.get(_pair(account, other), 0)
                        worst = max(worst, n)
                        total += n
                    # 次目标：同出口的号尽量别凑一组（软惩罚，不是硬闸）
                    my_fp = fp.get(account, "")
                    clash = 0
                    if my_fp:
                        clash = sum(
                            1 for other in chosen if fp.get(other, "") == my_fp)
                    key = (
                        worst, clash, total, load.get(account, 0),
                        _tiebreak(seed, group, account),
                    )
                    if best_key is None or key < best_key:
                        best, best_key = account, key
                if best is None:
                    capped_out = capped_out or cap is not None
                    break
                chosen.append(best)
                new.append(best)
                load[best] = load.get(best, 0) + 1

            if len(chosen) < want:
                unfilled.append(group)
            assignments[group] = tuple(chosen)
            joins[group] = tuple(new)
            _absorb(chosen)

        # ⑤ 度量与结论 ------------------------------------------------------
        merged = dict(history_clean)
        merged.update({g: list(v) for g, v in assignments.items()})
        metrics = attendance_metrics(merged, pool=accounts)
        metrics["groups_per_account"] = {
            account: sum(1 for v in assignments.values() if account in v)
            for account in accounts
        }
        # 本表派了几个群 vs 这个号总共挂在几个群里——后者才是「显不显眼」的口径。
        metrics["total_groups_per_account"] = {
            account: n + history_load.get(account, 0)
            for account, n in metrics["groups_per_account"].items()
        }
        metrics["planned_groups"] = len(assignments)
        metrics["history_groups"] = len(history_clean)
        metrics["understaffed"] = len(unfilled)
        metrics["staffed"] = not unfilled

        if overfull:
            problems.append(
                f"{len(overfull)} 个群的存量在场号已超过 {want} 个"
                f"（{', '.join(overfull[:5])}）——**不建议退群**：退群本身有痕迹，也消不掉"
                f"已经产生的共同出席；让多出来的号在这些群里不发言即可")
        if unfilled:
            head = f"{len(unfilled)} 个群排不满 {want} 个号"
            if capped_out:
                head += "（受单号进群上限所限，这是有意为之）"
            problems.append(f"{head}: {', '.join(unfilled[:5])}"
                            f"{' 等' if len(unfilled) > 5 else ''}")
        busiest = max(metrics["total_groups_per_account"].values(), default=0)
        if busiest > BUSY_ACCOUNT_GROUPS:
            tail = "（含历史群）" if history_load else ""
            problems.append(
                f"warn: 最忙的号挂在 {busiest} 个群里{tail}——号对重合达标不等于单号"
                f"安全，加群本身要分摊到多天（用 schedule_joins 排期）")

        verdict = metrics["verdict"]
        if verdict == VERDICT_DANGER:
            worst = metrics.get("worst_pair") or ["?", "?"]
            problems.append(
                f"共同出席已进危险区：{worst[0]} 与 {worst[1]} 共享 "
                f"{metrics['max_pair_co']} 个群"
                f"（团伙率 {metrics['clique_ratio']:.0%}）——这批号会被当成一伙")
        elif verdict == VERDICT_WARN:
            problems.append(
                f"warn: 最坏一对共享 {metrics['max_pair_co']} 个群，"
                f"偏高但还不至于一眼假")

        plan.assignments = assignments
        plan.already = already
        plan.joins = joins
        plan.metrics = metrics
        plan.problems = problems
        plan.advice = attendance_advice(
            pool_size=len(accounts),
            planned_groups=len(assignments),
            history_groups=len(history_clean),
            seats=want,
            verdict=verdict,
        )
        return plan
    except Exception as exc:  # noqa: BLE001 —— 排班器炸了给空表，不能交出半张
        plan.problems = [f"排班异常，本次不产出计划: {exc!r}"]
        return plan


def attendance_advice(
    *,
    pool_size: int,
    planned_groups: int,
    history_groups: int = 0,
    seats: int = DEFAULT_SEATS,
    verdict: str = VERDICT_SAFE,
) -> List[str]:
    """把「重合超标」翻译成两条**可执行**的路：加号，或砍覆盖。

    号池要按 **历史 + 本次** 的总覆盖来算。只按本次算会给出「20 个号够铺这 20 个新群」，
    而这些号身上已经压着 80 个老群——那个建议照做完了照样超标。
    """
    advice: List[str] = []
    if pool_size <= 0 or planned_groups <= 0:
        return advice
    if verdict == VERDICT_SAFE:
        return advice

    coverage = planned_groups + max(history_groups, 0)
    need = recommend_pool_size(groups=coverage, seats=seats)
    if need > pool_size:
        advice.append(
            f"号池从 {pool_size} 个补到 {need} 个——重合数与号池的平方成反比，"
            f"加号是唯一二次收益的杠杆（要撑住 {coverage} 个群的总覆盖）")
    safe_groups = max_safe_groups(pool=pool_size, seats=seats)
    if safe_groups < coverage:
        advice.append(
            f"或把覆盖砍到 {safe_groups} 个群——现有 {pool_size} 个号、每群 {seats} 人，"
            f"这是能保持在真人区间的上限")
    advice.append(
        "不要退群：退群本身有痕迹，且消除不了已经产生的共同出席历史；"
        "让超编的号在群里不发言，靠新群把重合稀释掉才是正路")
    return advice


# ── 加群排期（时间轴） ──────────────────────────────────────────────────────


def schedule_joins(
    plan: Any,
    *,
    per_account_per_day: int = DEFAULT_JOINS_PER_DAY,
    done_today: Optional[Mapping[str, int]] = None,
) -> Dict[str, Any]:
    """把「要加的群」摊到多天，两条硬约束：

    1. **同一个群每天只进一个我们的号**。一个群里前后脚进来三个陌生人、随后这三个人开始
       一唱一和——群主不需要任何技术手段就能看出来。这是出席矩阵覆盖不到的时间轴，而运营
       拿到静态表最自然的动作恰恰是「今天全加完」。
    2. 每个号每天加群数不超过 ``per_account_per_day``（加群速度本身是平台的风控信号）。

    Args:
        done_today: ``{号: 今天已经加了几个群}``——**第 1 天要按它预扣额度**。少了这一步，
            「做完今天 → 点已加入 → 重排」会把限速刷回满格，同一个号当天被派第二轮，
            限速形同虚设。额度被今天已加的挤满时直接顺延到下一天，不产出空的一天。

    Returns:
        ``{"days": [[{"group","account","day"}...], ...], "day_count": int,
        "total": int}``；确定性排序，运营执行到一半重开页面不会换一套。
    """
    out: Dict[str, Any] = {
        "days": [], "day_count": 0, "total": 0,
        "per_account_per_day": DEFAULT_JOINS_PER_DAY,
    }
    try:
        joins = getattr(plan, "joins", None)
        try:
            quota = max(int(per_account_per_day), 1)
        except (TypeError, ValueError):
            quota = DEFAULT_JOINS_PER_DAY
        out["per_account_per_day"] = quota
        if not isinstance(joins, Mapping):
            return out

        pending: List[Tuple[str, str]] = []
        for group in sorted(joins):
            for account in _as_list(joins.get(group)):
                account = _account_id(account)
                if account:
                    pending.append((group, account))
        if not pending:
            return out

        debit: Dict[str, int] = {}
        if isinstance(done_today, Mapping):
            for raw_account, raw_n in done_today.items():
                account = _account_id(raw_account)
                if not account:
                    continue
                try:
                    debit[account] = max(int(raw_n), 0)
                except (TypeError, ValueError):
                    continue

        days: List[List[Dict[str, Any]]] = []
        remaining = list(pending)
        guard = 0
        first_day = True
        while remaining and guard < 10000:
            guard += 1
            today: List[Dict[str, Any]] = []
            used_groups: set = set()
            # 首日从「今天已加过的」起算，之后每天从零开始
            per_account: Dict[str, int] = dict(debit) if first_day else {}
            leftover: List[Tuple[str, str]] = []
            for group, account in remaining:
                if group in used_groups or per_account.get(account, 0) >= quota:
                    leftover.append((group, account))
                    continue
                used_groups.add(group)
                per_account[account] = per_account.get(account, 0) + 1
                today.append(
                    {"group": group, "account": account, "day": len(days) + 1})
            if today:
                days.append(today)
                remaining = leftover
            elif first_day and debit:
                # 首日额度被今天已加的挤满 → 顺延，但**不能**产出空的一天
                # （空天会让「每天最多几个」这类逐日统计对空集求 max 而炸）
                pass
            else:  # 理论上不会发生；防御性退出，绝不空转
                break
            first_day = False

        out["days"] = days
        out["day_count"] = len(days)
        out["total"] = sum(len(d) for d in days)
        return out
    except Exception:  # noqa: BLE001 —— 排期是辅助信息，炸了不能拦住主流程
        return {"days": [], "day_count": 0, "total": 0,
                "per_account_per_day": DEFAULT_JOINS_PER_DAY}


# ── 跨群角色轮换（内容指纹） ────────────────────────────────────────────────


def rotate_roles(
    assignments: Any,
    roles: Sequence[str],
) -> Dict[str, Any]:
    """给每个群里的在场号分角色，并让同一个号**跨群轮着来**。

    出席矩阵管不到内容指纹：A 如果在每个群里都是夸产品那个，单群观察者不需要看别的群就能
    识破。所以角色要按「谁演这个角演得最少」轮换。

    Returns:
        ``{"casting": {群: {号: 角色}}, "role_counts": {号: {角色: 次数}},
        "max_role_share": float, "problems": [...]}``

    ``max_role_share`` ＝ 所有号里「最集中于单一角色」的那个占比。单槽剧本恒为 1.0——
    那是没得轮换，不是失衡，所以不报警。
    """
    out: Dict[str, Any] = {
        "casting": {}, "role_counts": {}, "max_role_share": 0.0, "problems": [],
    }
    try:
        slots = [_s(r) for r in _as_list(roles)]
        slots = [r for r in slots if r]
        if not slots:
            out["problems"] = ["没有可分配的角色槽，先在剧本里声明角色"]
            return out

        clean = _clean_rows(assignments)
        counts: Dict[str, Dict[str, int]] = {}
        casting: Dict[str, Dict[str, str]] = {}

        for group in sorted(clean):
            members = clean[group]
            taken: set = set()
            per_group: Dict[str, str] = {}
            # 在场人数少于槽位＝降级演，空着就空着；绝不靠一号两角凑满。
            for slot in slots[:len(members)]:
                best: Optional[str] = None
                best_key: Optional[Tuple[int, int, int]] = None
                for account in members:
                    if account in taken:
                        continue
                    played = counts.get(account, {}).get(slot, 0)
                    total = sum(counts.get(account, {}).values())
                    key = (played, total, _tiebreak(group, slot, account))
                    if best_key is None or key < best_key:
                        best, best_key = account, key
                if best is None:
                    break
                taken.add(best)
                per_group[best] = slot
                counts.setdefault(best, {})[slot] = (
                    counts.get(best, {}).get(slot, 0) + 1)
            casting[group] = per_group

        max_share = 0.0
        for account, played in counts.items():
            total = sum(played.values())
            if total:
                max_share = max(max_share, max(played.values()) / total)

        problems: List[str] = []
        if len(slots) > 1 and max_share > ROLE_SHARE_WARN:
            problems.append(
                f"warn: 有号 {max_share:.0%} 的场次都在演同一个角色——"
                f"单群观察者据此就能识破，建议扩号池或加长剧本角色表")

        out.update({
            "casting": casting,
            "role_counts": {k: dict(v) for k, v in sorted(counts.items())},
            "max_role_share": max_share,
            "problems": problems,
        })
        return out
    except Exception:  # noqa: BLE001
        return out


# ── 开演前的出席体检 ────────────────────────────────────────────────────────


def cast_entanglement(
    cast: Any,
    ledger: Any,
    *,
    group_key: str = "",
) -> Dict[str, Any]:
    """开演前查一次：**这台戏的这几个号**过去在多少个群里同框过。

    排班表是规划工具，开演时没人强制看它。不在开演前查，就会出现「矩阵建议 A/B 分开，
    实际却让 A/B 在第 40 个群里又一起演一台」——护栏存在但没接线，等于没有。

    这是**信息不是闸门**：任何输入异常都返回一份可读报告，绝不拦住整场戏。
    """
    out: Dict[str, Any] = {
        "verdict": VERDICT_SAFE, "max_pair_co": 0, "pairs": [], "problems": [],
    }
    try:
        members: List[str] = []
        seen: set = set()
        for raw in _as_list(cast):
            account = _account_id(raw)
            if account and account not in seen:
                seen.add(account)
                members.append(account)
        if len(members) < 2:
            return out  # 一个号没有号对可查

        here = _s(group_key)
        rows = {
            g: m for g, m in _clean_rows(ledger).items()
            # 「在这个群同框」是本场的前提而非历史证据，算进去等于自己吓自己
            if not here or g != here
        }
        counts = co_attendance(rows)

        pairs: List[List[Any]] = []
        for i, a in enumerate(sorted(members)):
            for b in sorted(members)[i + 1:]:
                pairs.append([a, b, counts.get(_pair(a, b), 0)])
        pairs.sort(key=lambda row: (-row[2], row[0], row[1]))

        max_pair = pairs[0][2] if pairs else 0
        hot = sum(1 for row in pairs if row[2] > NORMAL_PAIR_CO)
        clique = (hot / len(pairs)) if pairs else 0.0
        verdict = _verdict(max_pair, clique, len(members))

        problems: List[str] = []
        if verdict == VERDICT_DANGER:
            problems.append(
                f"这台戏的 {pairs[0][0]} 与 {pairs[0][1]} 已在 {max_pair} 个群里同框——"
                f"再同台一次会让这一对更扎眼，建议换人")
        elif verdict == VERDICT_WARN:
            problems.append(
                f"warn: {pairs[0][0]} 与 {pairs[0][1]} 已同框 {max_pair} 个群，"
                f"接近真人区间上限")

        out.update({
            "verdict": verdict, "max_pair_co": max_pair,
            "pairs": pairs, "problems": problems,
        })
        return out
    except Exception:  # noqa: BLE001 —— 体检挂了不该拦住整场戏
        return out


# ── 文本渲染 ────────────────────────────────────────────────────────────────


def format_plan(plan: Any, *, max_rows: int = 30) -> str:
    """排班表 → 人类可读文本（CLI 与日志用）。

    ``max_rows`` 截断逐群明细（页面卡片装不下几百行；要全表走 ``--json``）。

    与页面必须**同口径**：号池凑不满席位时判词恒为 safe（没有号对，共同出席确实是 0），
    页面已经用「号池不足」的红条盖掉绿灯，文本档漏掉同样会让人以为可以照着干。
    """
    lines: List[str] = []
    try:
        metrics = getattr(plan, "metrics", None) or {}
        assignments = getattr(plan, "assignments", None) or {}
        verdict = _s(metrics.get("verdict")) or VERDICT_SAFE
        mark = {VERDICT_SAFE: "✓", VERDICT_WARN: "⚠",
                VERDICT_DANGER: "✗"}.get(verdict, "?")

        lines.append(
            f"出席矩阵：{metrics.get('planned_groups', len(assignments))} 个群 / "
            f"{metrics.get('accounts', 0)} 个号  {mark} {verdict}")
        if metrics.get("history_groups"):
            lines.append(f"  （另计入 {metrics['history_groups']} 个历史群的重合）")
        worst = metrics.get("worst_pair")
        if worst:
            lines.append(
                f"  最坏一对：{worst[0]} × {worst[1]} 共享 "
                f"{metrics.get('max_pair_co', 0)} 个群"
                f"（全体均值 {metrics.get('avg_pair_co', 0):.2f}，"
                f"超标 {metrics.get('hot_pair_count', 0)} 对）")
        if metrics.get("staffed") is False:
            lines.append(
                f"  ✗ {metrics.get('understaffed', 0)} 个群凑不满在场人数——"
                f"这张表执行不了（判词的 safe 只说明共同出席无风险，别当成可以开演）")
        total_joins = getattr(plan, "total_joins", 0)
        lines.append(f"  待执行加群动作：{total_joins} 次")

        per = metrics.get("groups_per_account") or {}
        if per:
            busiest = sorted(per.items(), key=lambda kv: (-kv[1], kv[0]))[:5]
            lines.append("  单号进群数 Top5：" +
                         "  ".join(f"{k}={v}" for k, v in busiest))

        # 逐群明细：``+`` 标出这次新加的号，没标的是存量（运营只需执行带 + 的）
        joins = getattr(plan, "joins", None) or {}
        try:
            cap = max(int(max_rows), 0)
        except (TypeError, ValueError):
            cap = 30
        if assignments and cap:
            lines.append("  排班明细（+ ＝本次要加）：")
            for i, group in enumerate(sorted(assignments)):
                if i >= cap:
                    lines.append(
                        f"    …… 其余 {len(assignments) - cap} 个群略"
                        f"（--json 看全表）")
                    break
                fresh = set(joins.get(group) or ())
                cells = "  ".join(
                    (f"+{a}" if a in fresh else a)
                    for a in (assignments.get(group) or ()))
                lines.append(f"    {group}: {cells or '（空）'}")

        for problem in (getattr(plan, "problems", None) or ()):
            prefix = "  ⚠ " if str(problem).startswith("warn:") else "  ✗ "
            lines.append(prefix + str(problem).removeprefix("warn: "))
        advice = getattr(plan, "advice", None) or ()
        if advice:
            lines.append("  怎么解：")
            for item in advice:
                lines.append(f"    → {item}")
        return "\n".join(lines)
    except Exception:  # noqa: BLE001 —— 渲染器绝不能因为报告脏就抛
        return "\n".join(lines) or "出席矩阵：（无可用数据）"


__all__ = [
    "ALARM_PAIR_CO",
    "AttendancePlan",
    "BUSY_ACCOUNT_GROUPS",
    "CLIQUE_DANGER_RATIO",
    "DEFAULT_JOINS_PER_DAY",
    "DEFAULT_SEATS",
    "NORMAL_PAIR_CO",
    "ROLE_SHARE_WARN",
    "VERDICT_DANGER",
    "VERDICT_SAFE",
    "VERDICT_WARN",
    "attendance_advice",
    "attendance_metrics",
    "cast_entanglement",
    "co_attendance",
    "format_plan",
    "max_safe_groups",
    "plan_attendance",
    "recommend_pool_size",
    "rotate_roles",
    "schedule_joins",
    "shared_groups",
    "split_ledger",
]
