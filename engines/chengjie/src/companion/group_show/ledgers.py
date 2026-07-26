# -*- coding: utf-8 -*-
"""五条轴的**唯一一次读账**——谁开过口、演过什么角色、上次几点冒的头。

本模块存在的唯一理由是**防口径漂移**。到这一步为止，影子 CLI、导播台、以及即将到来
的真发链路，各自都要把同样四本账读一遍再拼成 ``cast_roles`` 的入参：

    速记表（谁跟谁同台过） · 角色台账（谁老演主推） · 开口预算 · 跨群冷却

三份实现只要有一处窗口不同、软失败姿势不同、或者少传一个 kwarg，就会出现那种最难
发现的事故：**排练里四个号演得热热闹闹，真发只有两个能上**——预览是假的，而它看起来
完全正常。所以「读账 → 拼参」这一整段只允许有一个实现，就是这里。

两条硬约束：

* **不 import 任何 store**。两个来源（场次库 / 收件箱库）全部鸭子类型传进来，本模块
  对 ``src.inbox`` / ``src.integrations`` 零依赖边——顺带让「排练侧模块物理上 import
  不到发送出口」这条不变量继续成立，无需靠人 review 维持。
* **任何一本账读挂都记进** :attr:`ShowContext.degraded`。少读一本的后果是共现表变空、
  冷却名单变空——跟「一切安全」长得一模一样。护栏悄悄失效比护栏不可用危险得多，
  因为前者是一片绿，没有人会去怀疑它。

纯计算仍然留在 ``performance`` / ``roles`` / ``schedule``：本模块只负责**取数**与
**拼参**，一条业务判断都不复制。
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from src.companion.group_show.capacity import observed_mix, plan_capacity
from src.companion.group_show.performance import (
    DEFAULT_WINDOW_DAYS,
    co_performance,
    exposure_report,
    resolve_speaker_cap,
    worst_verdict,
)
from src.companion.group_show.roles import NORMAL_ROLE_CO, role_report
from src.companion.group_show.schedule import next_allowed_ts

logger = logging.getLogger(__name__)

__all__ = [
    "ALL_HISTORY",
    "COOLDOWN_TTL_SEC",
    "ContextWatch",
    "ShowContext",
    "merge_speech",
    "read_last_spoke",
    "read_role_counts",
    "read_show_context",
    "read_speech",
    "speaking_groups",
]

#: 跨群冷却回看多久。闸门本身是十分钟级的，回看一天足够，再往前只是白扫库。
_COOLDOWN_LOOKBACK_SEC = 86400.0

#: ``window_days`` 传它 ＝ **不设窗，读全部历史**。
#:
#: 只给看板用，**闸门永远不要传**：共现是会饱和的，跑上几个月每一对都同过台，全历史
#: 口径下这个数会停在「危险」上再也下不来，于是运营学会了无视它。看板另说——运营需要
#: 一个「这批号从头到尾一共互相同框过多少次」的累计视角来决定要不要换号池。
ALL_HISTORY = -1


def _call(source: Any, name: str, degraded: Optional[List[str]],
          label: str, **kwargs: Any) -> Any:
    """调一个可选的台账方法：缺方法 → ``None``，抛错 → ``None`` + 记一笔降级。

    「没有这个方法」和「有但读挂了」要分开：前者是老库/降级实例的正常形态（不该报警），
    后者是真出了事（必须让人看见）。
    """
    fn = getattr(source, name, None)
    if not callable(fn):
        return None
    try:
        return fn(**kwargs)
    except TypeError:
        # 老版本 store 不认新 kwarg（如 exclude_group）——退一步用旧签名重试，
        # 拿到偏保守的读数总好过整本账变空。
        trimmed = {k: v for k, v in kwargs.items() if k != "exclude_group"}
        if len(trimmed) == len(kwargs):
            _note(degraded, label)
            logger.debug("[group_show.ledgers] %s 读取失败（已忽略）", name,
                         exc_info=True)
            return None
        try:
            return fn(**trimmed)
        except Exception:  # noqa: BLE001
            _note(degraded, label)
            logger.debug("[group_show.ledgers] %s 读取失败（已忽略）", name,
                         exc_info=True)
            return None
    except Exception:  # noqa: BLE001 —— 少一本账不该拦住整场戏
        _note(degraded, label)
        logger.debug("[group_show.ledgers] %s 读取失败（已忽略）", name, exc_info=True)
        return None


def _note(degraded: Optional[List[str]], label: str) -> None:
    if degraded is not None and label not in degraded:
        degraded.append(label)


def _since(window_days: int) -> float:
    """回看起点。``>0`` ＝那么多天，``0`` ＝默认窗，``<0``（:data:`ALL_HISTORY`）＝全历史。"""
    try:
        days = int(window_days)
    except (TypeError, ValueError):
        days = 0
    if days < 0:
        return 0.0
    return time.time() - float(days or DEFAULT_WINDOW_DAYS) * 86400.0


def merge_speech(*ledgers: Any) -> Dict[str, List[str]]:
    """把多本 ``{群: [号]}`` 并成一本，同群同号只留一次。

    去重是必须的：同一个号既演过戏又在这个群自动回复过，算两次会把它当成两个号，
    共现矩阵凭空虚高，运营会照着一个假读数去砍号池。
    """
    out: Dict[str, List[str]] = {}
    for ledger in ledgers:
        try:
            items = (ledger or {}).items()
        except Exception:  # noqa: BLE001 —— 外部数据的形状不由我们保证
            continue
        for group, accounts in items:
            g = str(group or "")
            if not g:
                continue
            bucket = out.setdefault(g, [])
            for a in accounts or ():
                s = str(a or "")
                if s and s not in bucket:
                    bucket.append(s)
    return out


def read_speech(shows: Any = None, inbox: Any = None, *, window_days: int = 0,
                platform: str = "telegram",
                degraded: Optional[List[str]] = None) -> Dict[str, List[str]]:
    """``{群: [在该群开过口的号]}``——**两个来源合并**：编排的戏 + 日常群内出向消息。

    只读场次库会得出「一场戏没演过 ⇒ 安全」，可这些号早就靠自动回复在几十个群里互相
    同框了；平台一视同仁地数消息，不区分是不是编排的。**假安全**是风险读数最不该犯的
    错，宁可把口径取全。
    """
    since = _since(window_days)
    return merge_speech(
        _call(shows, "performance_ledger", degraded, "shows",
              platform=platform, since=since),
        _call(inbox, "group_speech_ledger", degraded, "inbox", since_ts=since),
    )


def read_role_counts(shows: Any = None, *, window_days: int = 0,
                     platform: str = "telegram",
                     degraded: Optional[List[str]] = None
                     ) -> Dict[Tuple[str, str], int]:
    """``{(号, 角色槽): 在多少个不同的群演过这个角色}``。

    **只有场次库有这本账**：日常自动回复没有「角色」这个概念。这不是缺陷——日常回复
    本来也不是推销位，不进这本账正好。
    """
    rows = _call(shows, "role_ledger", degraded, "shows",
                 platform=platform, since=_since(window_days))
    return rows if isinstance(rows, dict) else {}


def read_last_spoke(shows: Any = None, inbox: Any = None, *, group_key: str = "",
                    platform: str = "telegram",
                    degraded: Optional[List[str]] = None) -> Dict[str, float]:
    """``{号: 最近一次在**别的群**开口的时刻}``——两本账取更晚的那个。

    ``group_key`` 是要**排除**的群（＝我们正打算开演的那个）。闸门问的是「有没有在别的
    群刚冒过头」；同一个群里连着说两句是正常对话，不排除的话，刚在本群自动回复过的号
    会被自己挡住——这条拦截毫无风险意义，只会让运营把闸门整个关掉。
    """
    since = time.time() - _COOLDOWN_LOOKBACK_SEC
    merged: Dict[str, float] = {}
    sources = (
        _call(shows, "last_spoke_at", degraded, "shows", platform=platform,
              since=since, exclude_group=group_key),
        _call(inbox, "group_last_spoke_at", degraded, "inbox", since_ts=since,
              exclude_group=group_key),
    )
    for rows in sources:
        try:
            items = (rows or {}).items()
        except Exception:  # noqa: BLE001
            continue
        for account, ts in items:
            a = str(account or "")
            try:
                t = float(ts or 0.0)
            except (TypeError, ValueError):
                continue
            if a and t > merged.get(a, 0.0):
                merged[a] = t
    return merged


def speaking_groups(speech: Any) -> Dict[str, int]:
    """``{号: 开过口的群数}``——角色集中度的分母。

    「在 3 个群当过主推」在只演过 3 个群时是 100%、在演过 50 个群时是 6%。没有分母，
    这条轴只会把干活最多的号一律报成最危险的号，而那恰恰是最健康的号。
    """
    out: Dict[str, int] = {}
    try:
        buckets = (speech or {}).values()
    except Exception:  # noqa: BLE001
        return {}
    for accounts in buckets:
        for a in accounts or ():
            s = str(a or "")
            if s:
                out[s] = out.get(s, 0) + 1
    return out


@dataclass(frozen=True)
class ShowContext:
    """一次读账的快照 + 由它推出的全部开演决策。

    调用方拿到它之后**不该再自己算任何东西**——想改哪条轴就改这里，三个消费方
    （影子 CLI / 导播台 / 真发链路）自动同步。
    """

    speech: Dict[str, List[str]] = field(default_factory=dict)
    co_performance: Dict[Tuple[str, str], int] = field(default_factory=dict)
    role_counts: Dict[Tuple[str, str], int] = field(default_factory=dict)
    last_spoke: Dict[str, float] = field(default_factory=dict)
    budget: Dict[str, Any] = field(default_factory=dict)
    advice: Tuple[str, ...] = ()
    degraded: Tuple[str, ...] = ()
    role_limit: int = 0
    window_days: int = DEFAULT_WINDOW_DAYS
    #: 共现轴完整报告（``exposure_report``）——看板直接渲染它，闸门不看。
    exposure: Dict[str, Any] = field(default_factory=dict)
    #: 角色轴完整报告（``role_report``）；算不出来 ＝ ``{}``（前端整块隐藏）。
    roles: Dict[str, Any] = field(default_factory=dict)
    #: 容量（``plan_capacity``）——三条轴取最紧的那条，**按台账反推的实际演法**算。
    #: 它与上面三份报告的关系是「读数」与「上限」：那三份说现在多危险，这份说还能铺多少。
    capacity: Dict[str, Any] = field(default_factory=dict)

    # ── 看板：多轴合并 ──────────────────────────────────────────────────────

    @property
    def verdict(self) -> str:
        """顶部那盏灯——取**最差**的一轴。

        平台是按最可疑的那个特征下手的，不是按平均分：共现干净但某个号在 8 个群都当
        主推，这批号照样一起没。
        """
        return worst_verdict(self.exposure.get("verdict"),
                             self.roles.get("verdict"))

    # ── 拼参：真发/预览唯一的入口 ───────────────────────────────────────────

    def speaker_cap(self, *, requested: int = 0,
                    allow_over: bool = False) -> Dict[str, Any]:
        """本场允许几张嘴开口，以及这个数是怎么来的（可审计）。"""
        return resolve_speaker_cap(
            requested=requested, budget=int(self.budget.get("max_speakers") or 0),
            allow_over=allow_over)

    def cast_kwargs(self, *, requested: int = 0, allow_over: bool = False,
                    seed: str = "") -> Dict[str, Any]:
        """喂给 ``cast_roles`` 的全套闸门参数。

        **这是全系统唯一一处**决定「选角要受哪些约束」的地方。谁绕过它自己拼 kwargs，
        谁就会在某个版本之后悄悄少挂一条闸门——而少挂闸门的表现是「一切正常，只是有
        一天号被封了」。
        """
        return {
            "co_performance": dict(self.co_performance),
            "max_speakers": int(self.speaker_cap(requested=requested,
                                                 allow_over=allow_over)["effective"]),
            "role_counts": dict(self.role_counts),
            "role_limit": int(self.role_limit),
            "seed": str(seed or ""),
        }

    # ── 时间轴：跨群间隔 ────────────────────────────────────────────────────

    def delay_for(self, account: Any, at: float) -> int:
        """这个号此刻开口会被往后顶多少秒（``0`` ＝ 不会被顶）。

        **缺记录一律放行**：这张表在冷启动、重启、换库之后必然是空的，保守解释会让整批
        号第一天全部开不了口，而运营对付「全都不发言」的唯一手段就是把护栏关掉。
        """
        a = str(account or "")
        if not a:
            return 0
        try:
            return max(0, int(next_allowed_ts(a, float(at), self.last_spoke)
                              - float(at)))
        except Exception:  # noqa: BLE001 —— 判不出来就当不顶，与闸门上线前一致
            return 0

    def waiting(self, accounts: Sequence[Any],
                at: Optional[float] = None) -> List[Dict[str, Any]]:
        """点名：这批号里谁还在冷却窗里，还要等多久。"""
        now = time.time() if at is None else float(at)
        out: List[Dict[str, Any]] = []
        for account in accounts or ():
            wait = self.delay_for(account, now)
            if wait > 0:
                out.append({"account": str(account), "wait_sec": wait,
                            "until": now + wait,
                            "last_spoke": self.last_spoke.get(str(account), 0.0)})
        return out


def read_show_context(
    shows: Any = None,
    inbox: Any = None,
    *,
    pool: Sequence[Any] = (),
    group_key: str = "",
    platform: str = "telegram",
    window_days: int = 0,
    memberships: Optional[Mapping[str, Sequence[str]]] = None,
) -> ShowContext:
    """读一次账，得到这场戏需要的全部风险读数。

    ``pool`` ＝ 本次可用的号（预算的分母之一）；``group_key`` ＝ 即将开演的群
    （跨群冷却要把它排除掉，见 :func:`read_last_spoke`）。``memberships`` 缺省从
    ``shows.memberships()`` 取——传进来只是为了让调用方能省掉重复的一次查询。

    读不到的部分一律退化成「不限」而不是「全禁」：台账都没有还硬拦，只会让运营认定
    工具在瞎拦，进而把整套护栏关掉——那才是真的没有护栏。
    """
    degraded: List[str] = []
    speech = read_speech(shows, inbox, window_days=window_days,
                         platform=platform, degraded=degraded)
    roles = read_role_counts(shows, window_days=window_days, platform=platform,
                             degraded=degraded)
    last = read_last_spoke(shows, inbox, group_key=group_key, platform=platform,
                           degraded=degraded)

    members = memberships
    if members is None:
        members = _call(shows, "memberships", degraded, "shows") or {}

    clean_pool = [str(a) for a in (pool or ()) if str(a or "")]
    exposure: Dict[str, Any] = {}
    budget: Dict[str, Any] = {}
    advice: List[str] = []
    if shows is not None:
        try:
            exposure = exposure_report(members, speech, pool=clean_pool)
            advice += [str(x) for x in (exposure.get("advice") or ())]
            # 号池为空不是「预算＝独白」，是**没有数据**：全新装机 / 号全下线时
            # ``speaker_budget`` 会算出 solo_only，于是每一场都被压成独白、还配一句
            # 「按预算自动限到 1 人」——看着像工具坏了。缺数据就别造约束。
            if clean_pool:
                budget = dict(exposure.get("budget") or {})
        except Exception:  # noqa: BLE001 —— 预算算不出来 ＝ 不限，与上线前一致
            _note(degraded, "budget")
            logger.debug("[group_show.ledgers] 开口预算失败（已忽略）", exc_info=True)
    roles_report: Dict[str, Any] = {}
    if roles:
        try:
            roles_report = role_report(roles,
                                       speaking_groups=speaking_groups(speech),
                                       pool=clean_pool)
            advice += [str(x) for x in (roles_report.get("advice") or ())]
        except Exception:  # noqa: BLE001 —— 报账算不出来，闸门照样要挂上
            _note(degraded, "roles")
            logger.debug("[group_show.ledgers] 角色读数失败（已忽略）", exc_info=True)

    capacity: Dict[str, Any] = {}
    if clean_pool and speech:
        # 号池为空或一场没演过时不算容量：那时反推出来的演法（0 张嘴、0 个群）会得出
        # 一个看着很吓人又完全没有依据的上限，而运营此刻正需要的是「先跑起来」。
        try:
            mix = observed_mix(speech=speech, memberships=members,
                               role_counts=roles)
            capacity = plan_capacity(pool=len(clean_pool), groups=mix["groups"],
                                     seats=mix["seats"], speakers=mix["speakers"],
                                     advocate_ratio=mix["advocate_ratio"])
            advice += [str(x) for x in (capacity.get("advice") or ())]
        except Exception:  # noqa: BLE001 —— 容量只是规划参考，算不出来不该拖垮开演
            _note(degraded, "capacity")
            logger.debug("[group_show.ledgers] 容量读数失败（已忽略）", exc_info=True)

    return ShowContext(
        speech=speech,
        co_performance=co_performance(speech) if speech else {},
        role_counts=roles,
        last_spoke=last,
        budget=budget,
        advice=tuple(advice),
        degraded=tuple(degraded),
        # 台账都没有就别挂角色闸门（0 ＝ 关），与这条轴上线前一字不差。
        role_limit=int(NORMAL_ROLE_CO) if roles else 0,
        window_days=(window_days if window_days and window_days > 0
                     else DEFAULT_WINDOW_DAYS),
        exposure=exposure,
        roles=roles_report,
        capacity=capacity,
    )


# ── 长跑：把开演那一刻的快照按 TTL 续上 ──────────────────────────────────────

#: 跨群间隔台账的重读间隔（秒）。一场戏／一次影子挂机就是几十分钟，期间别的群随时可能
#: 有号冒头；但每一拍都重扫两张表纯属浪费（闸门粒度本来就是十分钟级）。
COOLDOWN_TTL_SEC = 60.0


class ContextWatch:
    """跨群冷却的**长跑视图**：开演那一刻的快照会过期，这里负责按 TTL 重读。

    存在的理由只有一个：**开场那张快照撑不到收场**。一场戏演几十分钟，期间别的群随时
    有号冒头；拿开演时刻的读数去判半小时后的第十拍，等于把这条闸门关掉一半——而它恰恰
    是最锋利的一条（静态特征全洗干净了，同一个号前后脚在两个群冒头照样一眼假）。

    **只续 ``last_spoke`` 一条轴**，这是有意的。共现表与角色表也会变，但它们的粒度是
    「群」而不是「秒」，且本场的贡献在开演时就已经计过一次；更要紧的是演员表开演即定，
    中途重算共现改变不了任何决策，白付两次全表扫描。会随秒变、且真能改变**下一拍**
    决策的，只有「这个号刚刚是不是在别的群说过话」。

    判定口径（合并两本账、排除本群、``next_allowed_ts``）全部复用
    :class:`ShowContext`，与真发／影子／导播台一字不差；这里只管「什么时候该重读」。
    """

    def __init__(self, shows: Any = None, inbox: Any = None, *,
                 group_key: str = "", platform: str = "telegram",
                 ttl: float = COOLDOWN_TTL_SEC,
                 now: Callable[[], float] = time.time) -> None:
        self._shows = shows
        self._inbox = inbox
        self._group = str(group_key or "")
        self._platform = str(platform or "telegram")
        self._ttl = max(0.0, float(ttl))
        self._now = now
        self._ctx: Optional[ShowContext] = None
        self._read_at = 0.0

    def _fresh(self) -> ShowContext:
        now = float(self._now())
        if self._ctx is None or (now - self._read_at) >= self._ttl:
            self._ctx = ShowContext(last_spoke=read_last_spoke(
                self._shows, self._inbox, group_key=self._group,
                platform=self._platform))
            self._read_at = now
        return self._ctx

    def snapshot(self) -> Dict[str, float]:
        return dict(self._fresh().last_spoke)

    def delay_for(self, account: Any, at: float) -> int:
        """这个号此刻开口会被往后顶多少秒（``0`` ＝ 不会被顶）。"""
        return int(self._fresh().delay_for(account, at))

    def waiting(self, accounts: Sequence[Any],
                at: Optional[float] = None) -> List[Dict[str, Any]]:
        """点名：这批号里谁还在冷却窗里，还要等多久。"""
        return list(self._fresh().waiting(accounts, at))

    def drain_humans(
        self,
        *,
        since_ts: float,
        exclude_accounts: Sequence[Any] = (),
        limit: int = 20,
    ) -> List[Tuple[str, str, float]]:
        """本群自 ``since_ts`` 起新进的真人发言 → ``[(sender, text, ts), ...]``。

        真发主循环每拍喂给导演的 ``observe_human``；缺 inbox / 方法 / 读挂 → 空
        （让路失效，绝不把整场戏打崩）。演员号进 ``exclude_accounts``，避免把本场
        出站镜像误当成真人插话。
        """
        if not self._group or self._inbox is None:
            return []
        ban = [str(a) for a in (exclude_accounts or ()) if str(a or "")]
        rows = _call(
            self._inbox, "group_inbound_since", None, "inbox_inbound",
            chat_key=self._group, since_ts=float(since_ts or 0.0),
            platform=self._platform, exclude_senders=ban,
            limit=max(1, int(limit or 20)))
        if not rows:
            return []
        out: List[Tuple[str, str, float]] = []
        for r in rows:
            if not isinstance(r, Mapping):
                continue
            text = str(r.get("text") or "").strip()
            if not text:
                continue
            try:
                ts = float(r.get("ts") or 0.0)
            except Exception:  # noqa: BLE001
                continue
            if ts <= 0:
                continue
            who = (str(r.get("sender_name") or "").strip()
                   or str(r.get("sender_id") or "").strip()
                   or "human")
            out.append((who, text, ts))
        return out
