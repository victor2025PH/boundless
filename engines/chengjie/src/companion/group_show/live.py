# -*- coding: utf-8 -*-
"""真发链路 —— 全系统**唯一**一处会把群戏真的发到群里的地方。

## 为什么不复用 ``runtime.rehearse``

让排练与真发共用一个函数、用 ``if dry_run:`` 决定最后要不要投递，看起来省一半代码，
实际是把「发不出去」这条**物理保证**换成了一个布尔判断。那个判断某天会被人改错、被
某个调用方漏传、被重构时合并掉；而它错一次的代价是一批号在真群里发出未经审核的消息。
所以这里是独立一条路径：``rehearse`` 连发送函数都拿不到（它自己构造记录器，签名里没有
投递出口），``perform`` 必须由调用方显式把出口递进来。

**但决策不许有两份。** 选角、导演队列、节奏、ECP 投影、台词生成、闸门判定全部来自与
排练一字不差的同一批模块；两条路径的差别只在「驱动」——虚拟时钟 vs 真等待、记录 vs
投递。防漂移靠门禁而不是靠自觉：``tests/test_group_show_live.py`` 里有一条**同种子对等
性**测试，同一剧本同一种子下真发（挂假出口）与排练必须逐行产出同一份台词。哪天有人只
改了一条路径的决策顺序，那条测试立刻红。

## 三条硬顶（读不到配置，改不动）

``MAX_LIVE_LINES`` / ``MAX_LIVE_SECONDS`` / ``MAX_ITERATIONS`` **刻意不读配置**。它们不是
业务限制（业务限制在 ``director.cfg.max_lines`` 那儿，运营可调），而是**配置写错时的兜底**
——``max_lines: 200`` 这样一个手滑，在排练里只是刷屏，在真群里是一个号连发两百条。能被
配置调大的护栏，在「配置写错」这个故障模式下等于不存在。

## 至多一次，绝不重发

投递失败**永不重试**，这一拍直接作废。理由是群聊里两种错的代价完全不对称：少说一句话
没人会注意（真人群里本来就有人潜水），而**重复一句话是最刺眼的机器痕迹**——真人不会把
同一句话说两遍。而「发送失败」这个信号本身是不可靠的：消息可能已经到了平台、只是回执
丢在网络上，此时重试就是实打实的重复发送。所以语义定为 at-most-once。

## 「被护栏拦下」与「发送失败」的处方相反

* ``blocked``（Kill-Switch / 反封号闸 / 会话不健康）→ **整场中止**。这是安全系统正在
  生效，继续演就是跟它对抗；它拦第一条，说明它会拦后面每一条。
* 普通失败（异常 / ``delivered=False``）→ 作废这一拍继续演。这是技术抖动，不是风控介入。

两者混成一类是很自然的错（都是「没发出去」），但混了之后要么因为一次网络抖动放弃整场
戏，要么在急停已经按下的情况下继续硬发十几条。

## 中途被杀也要留下正确记录

场次在**开演前**就落库（``dry_run=0``），事件在**每条发成功之后**立刻落库。所以进程被杀
／任务被取消时，台账里是「已经发出去的那几条」——不多不少。这件事关系到风险读数的正确
性：漏记会让共现矩阵低估暴露（以为很安全），多记会让运营对着不存在的消息排查。

**刻意不做续演。** 一场被打断的戏标记 ``aborted`` 后就此作废，不会被自动接着演。双发
bug 几乎全都住在「恢复」这条路上：判断「第 N 条到底发出去了没有」在分布式语义下无解，
而猜错的方向之一就是重复发送。少演半场戏的损失，远小于此。
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
import uuid
from dataclasses import dataclass, field, replace
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence

from src.companion.group_show.casting import cast_roles, validate_casting
from src.companion.group_show.director import GroupShowDirector
from src.companion.group_show.ecp import project_history_for
from src.companion.group_show.naturalness import line_similarity, naturalness_score
from src.companion.group_show.pacing import beat_interval_seconds
from src.companion.group_show.playbook import (
    Casting,
    Playbook,
    ShowEvent,
    ShowState,
    validate_playbook,
)
from src.companion.group_show.runtime import (
    MAX_CONSECUTIVE_SKIPS,
    GenerateFn,
    group_hint_text,
)
from src.companion.group_show.schedule import QUIET_HOURS, in_quiet_hours

logger = logging.getLogger(__name__)

#: 投递出口契约：``(account_id, text) -> {"delivered": bool, "message_id": str,
#: "blocked": str}``。平台与群在调用方那一侧就绑定好了（见 :func:`orchestrator_sender`），
#: 本模块因此**不 import 任何 worker / 编排器**——真发路径的依赖面越小越好审。
SendFn = Callable[[str, str], Awaitable[Dict[str, Any]]]

#: 一场真发最多几条。**兜底，不是业务限制**（业务限制是 ``director.cfg.max_lines``）。
MAX_LIVE_LINES = 20

#: 一场真发最长多久（秒）。同上，兜底 ``max_duration_seconds`` 写错。
MAX_LIVE_SECONDS = 45 * 60.0

#: 主循环最多转几轮。比 :data:`MAX_LIVE_LINES` 宽裕，留给「等冷却」「跳拍」这些不产出
#: 消息的轮次；导演逻辑真有回归时，宁可截断也不要挂住一个连着 worker 的协程。
MAX_ITERATIONS = 60

#: 跨群冷却最多等多久（秒）。超过就跳过这一拍——等半小时不如让这拍不说话，戏还能往下演。
MAX_GATE_WAIT_SEC = 180.0

#: 连续几次投递失败判定「链路挂了」。取 2 而不是 3：真发失败每一次都可能已经发出去了
#: （回执丢失），试探的成本比排练里高得多。
MAX_CONSECUTIVE_SEND_FAILURES = 2

#: 复读闸阈值：新台词与本场**任一已发**台词的内容词 overlap 系数（见
#: :func:`~src.companion.group_show.naturalness.line_similarity`）到这条线（含）即判复读。
#: 0.5 依 2026-07-27 首场灰度标定：真实复读对 = 0.50，全部正常拍两两 ≤ 0.25。
#: 复读的处置是「带禁令重试一次，仍雷同就弃拍」——同一场戏里反复念同一个卖点，是
#: 比发言频率更早暴露的水军特征（真人不会两分钟内换个句式再安利一遍）。
MAX_LINE_SIMILARITY = 0.5


@dataclass(frozen=True)
class LiveLine:
    """真发出去的一条（或一次作废的尝试）。"""

    seq: int
    at_seconds: float
    account_id: str
    display_name: str
    persona_id: str
    role: str
    beat_id: str
    text: str
    soft_level: int = 0
    media: str = ""
    kind: str = "line"
    delivered: bool = True
    message_id: str = ""


@dataclass
class LiveResult:
    """一场真发的完整产物。``dry_run`` 恒为 ``False`` —— 这条路径没有别的可能。"""

    session_id: str
    playbook_id: str
    group_key: str
    platform: str = "telegram"
    lines: List[LiveLine] = field(default_factory=list)
    casting: Optional[Casting] = None
    terminate_reason: str = ""
    naturalness: Dict[str, Any] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
    duration_seconds: float = 0.0
    #: 真送达条数（``lines`` 里 ``delivered`` 的那些）
    sent: int = 0
    #: 投递失败而作废的拍数
    failed: int = 0
    #: 生成不出台词 / 被闸门顶太久而跳过的拍数
    skipped: int = 0
    #: 因跨群冷却真等了几次（>0 说明这批号最近在别的群很活跃）
    gate_waits: int = 0
    #: 复读闸触发次数（生成的台词与本场已发内容雷同，带禁令重试）
    repetition_retries: int = 0
    #: 本场观察到的真人插话条数（喂进导演的 observe_human）
    humans_observed: int = 0
    #: 因真人让路窗而暂停的次数
    human_yields: int = 0
    #: 被护栏拦下时的原因码（``blocked`` 收场时才有值）
    blocked_by: str = ""
    dry_run: bool = False

    @property
    def armed(self) -> bool:
        """这场戏到底有没有获得真发许可（``False`` ＝ 一条都没发，双锁没开齐）。"""
        return self.terminate_reason != "not_armed"


# ── 双锁 ────────────────────────────────────────────────────────────────────


def live_enabled(app_config: Optional[Dict[str, Any]] = None) -> bool:
    """配置侧那把锁：``companion.group_show.live.enabled``（缺省 **关**）。

    与 ``confirm_live`` 参数构成两把**互相独立**的锁，两把都开才发得出去。为什么要两把：
    单靠配置，任何一个既有调用方在配置被打开的那一刻就会突然开始真发（它自己代码一行
    没改）；单靠参数，则线上没有一个「全局急停」——出事时要去改代码而不是改配置。两把锁
    分别封住这两个故障，缺一不可。
    """
    try:
        node = ((app_config or {}).get("companion") or {}).get("group_show") or {}
        return bool((node.get("live") or {}).get("enabled"))
    except Exception:  # noqa: BLE001 —— 配置读不出来一律按「没开」，安全侧默认
        return False


def live_quiet_hours(app_config: Optional[Dict[str, Any]] = None) -> Any:
    """禁演时段窗：``companion.group_show.live.quiet_hours``（缺省 :data:`QUIET_HOURS`）。

    配 ``[0, 0]`` ＝ 空窗 ＝ 不禁（见 :func:`~...schedule.in_quiet_hours`）。
    """
    try:
        node = ((app_config or {}).get("companion") or {}).get("group_show") or {}
        got = (node.get("live") or {}).get("quiet_hours")
        return got if got is not None else QUIET_HOURS
    except Exception:  # noqa: BLE001
        return QUIET_HOURS


def live_preflight_enabled(app_config: Optional[Dict[str, Any]] = None) -> bool:
    """开演前 peer 体检开关：``companion.group_show.live.preflight_peers``（缺省 **开**）。

    为什么默认开而不是随新功能惯例默认关：它是**纯只读**校验（resolve/预热 RPC，
    零发言），拦的是「演员根本不在群」这种必然翻车——2026-07-27 双号灰度连烧三场，
    每场都是首拍成功、第二号连炸两拍收场，群里留下一半的戏。带病开演没有任何
    合法场景，故默认拦。真要绕（如平台侧体检误报）配 ``false``。
    """
    try:
        node = ((app_config or {}).get("companion") or {}).get("group_show") or {}
        got = (node.get("live") or {}).get("preflight_peers")
        return True if got is None else bool(got)
    except Exception:  # noqa: BLE001
        return True


async def preflight_peers(
    orchestrator: Any, casting: Any, *, platform: str, group_key: str,
) -> Dict[str, Any]:
    """开演前逐演员做群 peer 可达性体检：``{ok, checked, unreachable: [...]}``。

    只拦**确定不可达**（编排器答复 ``ok=False``）；编排器缺此能力 / 单查异常
    → 视作未检出问题放行（发送路径仍有 peer 自愈兜底）。``send`` 直传（无
    编排器）的调用方天然跳过——它们自带出口，链路健康归它们自己管。
    """
    out: Dict[str, Any] = {"ok": True, "checked": 0, "unreachable": []}
    fn = getattr(orchestrator, "ensure_peer", None) if orchestrator is not None else None
    if fn is None:
        return out
    for member in list(getattr(casting, "members", ()) or ()):
        acct = str(getattr(member, "account_id", "") or "")
        if not acct:
            continue
        try:
            res = await fn(str(platform), acct, str(group_key))
        except Exception:  # noqa: BLE001 —— 体检自身故障不拦戏
            logger.debug("[group_show.live] preflight 查询异常 acct=%s", acct,
                         exc_info=True)
            continue
        if (res or {}).get("checked"):
            out["checked"] += 1
        if (res or {}).get("ok") is False:
            out["unreachable"].append(acct)
    out["ok"] = not out["unreachable"]
    return out


def looks_like_app_config(config: Optional[Dict[str, Any]]) -> bool:
    """这个 dict 像不像**整份 app 配置**被误传进了 ``config``（导演配置）位。

    存在的理由是一次真实的踩坑：``rehearse(config=…)`` 的既定含义是**扁平**的导演参数
    （``{"max_lines": 12}``），而真发还需要一份 app 配置来读开关。两个参数同名不同层时，
    传错的表现是 ``DirectorConfig`` 静默回落全部缺省——**戏照演，只是节奏和预算跟排练里
    看到的那份不一样**。没有报错、没有异常，只有一份对不上的时刻表；这种错误只能靠形状
    探测在入口处点名。
    """
    if not isinstance(config, dict):
        return False
    return any(k in config for k in ("companion", "inbox", "telegram", "ai"))


# ── 投递出口适配器 ──────────────────────────────────────────────────────────


def orchestrator_sender(orchestrator: Any, *, platform: str,
                        group_key: str) -> SendFn:
    """``AccountOrchestrator`` → :data:`SendFn`。

    唯一出口仍是 ``orchestrator.send``——它自带 Kill-Switch 与反封号闸，且会把出站消息
    回写收件箱线程。后者不只是「坐席能看见」：真发出去的话因此进了
    ``InboxStore.group_speech_ledger``，下一场的共现读数会**自动**把它算进暴露面
    （``ledgers.read_speech`` 合并两本账），不需要这里再记一遍。

    异常在这里就地转成 ``delivered=False``：主循环要能区分「护栏拦下」与「链路抖动」，
    让异常穿到主循环里就只剩一个 ``except``，两者又混成一类了。
    """

    async def _send(account_id: str, text: str) -> Dict[str, Any]:
        try:
            res = await orchestrator.send(platform, account_id, group_key, text)
            return dict(res or {})
        except Exception as exc:  # noqa: BLE001
            logger.warning("[group_show.live] 投递异常 %s:%s group=%s: %s",
                           platform, account_id, group_key, exc)
            return {"delivered": False, "error": str(exc)}

    return _send


@dataclass
class LivePlan:
    """真发开演前的体检结果。``ok=False`` 时一条都不该发；``ok=True`` 时
    ``casting`` 已定，调用方可以直接进主循环。

    把体检从主循环里拆出来，是为了让 ``--check`` / 导播台「先看一眼」与真发走**同一份**
    判定——否则就会出现「预检全绿、一按真发却 understaffed」这种最伤信任的错位。
    """

    ok: bool = False
    reason: str = ""
    warnings: List[str] = field(default_factory=list)
    casting: Optional[Casting] = None
    #: 投递出口是否齐备（预检模式可以不要求出口，真发必须有）
    has_outlet: bool = False
    #: 双锁是否齐开
    armed: bool = False
    #: 剧本是否是单角色形态（P3-1）
    solo: bool = False
    #: 此刻是否落在禁演时段（独立信号：即使因缺角先红，运营也要看见「同时还在静默窗」）
    in_quiet_hours: bool = False


def is_solo_playbook(playbook: Any) -> bool:
    """有戏份的角色恰好是一个 ``advocate`` —— 单号炒群的正确剧本形态。

    声明了却一拍都没安排的角色不算（与 :func:`_understaffed_roles` 同口径）。
    """
    try:
        with_beats = {str(getattr(b, "role", "") or "")
                      for b in (getattr(playbook, "beats", ()) or ())}
        with_beats.discard("")
        return with_beats == {"advocate"}
    except Exception:  # noqa: BLE001
        return False


def plan_live(
    playbook: Playbook,
    candidates: Sequence[Dict[str, Any]],
    *,
    group_key: str = "",
    send: Optional[SendFn] = None,
    orchestrator: Any = None,
    platform: str = "telegram",
    confirm_live: bool = False,
    app_config: Optional[Dict[str, Any]] = None,
    config: Optional[Dict[str, Any]] = None,
    cast_kwargs: Optional[Dict[str, Any]] = None,
    fingerprint_groups: Optional[Dict[str, str]] = None,
    persona_gender: Optional[Dict[str, str]] = None,
    require_outlet: bool = False,
    hour: Optional[int] = None,
    now: Callable[[], float] = time.time,
) -> LivePlan:
    """真发开演前的全部闸门，**一条消息都不发**。

    ``require_outlet=False``（预检缺省）时，没有投递出口也算过——运营想先看「角色齐不齐、
    指纹过不过、是不是禁演时段」，此时还没必要把编排器拉起来。真发路径传
    ``require_outlet=True``。

    返回的 ``reason`` 与 :func:`perform` 的 ``terminate_reason`` **同词汇表**，预检红灯
    与真发拒演可以对得上。
    """
    plan = LivePlan(solo=is_solo_playbook(playbook))
    sender = send or (orchestrator_sender(
        orchestrator, platform=platform, group_key=str(group_key))
        if orchestrator is not None else None)
    plan.has_outlet = sender is not None

    if not confirm_live:
        plan.reason = "not_armed"
        plan.warnings.append("真发未武装：调用方没有显式传 confirm_live=True")
        return plan
    if not live_enabled(app_config):
        plan.reason = "not_armed"
        plan.warnings.append(
            "真发未武装：配置 companion.group_show.live.enabled 未开")
        return plan
    plan.armed = True

    quiet = live_quiet_hours(app_config)
    at_hour = hour if hour is not None else _local_hour(now)
    plan.in_quiet_hours = bool(in_quiet_hours(at_hour, quiet_hours=quiet))
    if plan.in_quiet_hours:
        # 独立信号（warn 前缀）：缺角/指纹问题优先成 reason，但运营仍要看见静默窗。
        plan.warnings.append(
            f"warn: 现在是禁演时段（本地 {at_hour} 点，窗口 {tuple(quiet)}）——"
            "选角过关后仍会拒演；改时间或把 live.quiet_hours 配成 [0, 0]")

    if looks_like_app_config(config):
        plan.warnings.append(
            "warn: config 参数收到了整份 app 配置。它应该是**扁平的导演参数**"
            "（如 {'max_lines': 12}）；开关请走 app_config。本场导演参数按缺省执行")

    if require_outlet and sender is None:
        plan.reason = "not_armed"
        plan.warnings.append("真发未武装：没有投递出口（send / orchestrator 都没传）")
        return plan

    if not fingerprint_groups:
        plan.reason = "no_fingerprints"
        plan.warnings.append(
            "真发前置未满足：未传指纹表，同台关联复查无法执行"
            "（用 linkage.derive_fingerprint_groups 派生后再来）")
        return plan

    warnings: List[str] = list(validate_playbook(playbook))
    ck = dict(cast_kwargs or {})
    ck.setdefault("seed", str(group_key or ""))
    casting = cast_roles(playbook, candidates,
                         fingerprint_groups=fingerprint_groups,
                         persona_gender=persona_gender, **ck)
    warnings.extend(validate_casting(
        casting, playbook, fingerprint_groups=fingerprint_groups,
        persona_gender=persona_gender))
    plan.casting = casting
    plan.warnings.extend(warnings)

    hard = [w for w in warnings if not w.startswith("warn:")]
    if hard or not casting.members:
        plan.reason = "invalid"
        return plan

    short = _understaffed_roles(playbook, casting)
    if short:
        plan.reason = "understaffed"
        plan.warnings.append(
            "真发前置未满足：这些有戏份的角色没有号来演——"
            + "；".join(f"{role}（{_BLOCK_ADVICE.get(why, _BLOCK_ADVICE[''])}）"
                        for role, why in short)
            + "。真发不做降级顶替（一个号演两个角色＝自问自答，是最容易被认出的水军痕迹）")
        if not plan.solo and len(list(candidates or ())) <= 1:
            plan.warnings.append(
                "warn: 单号场景请改用 solo_* 剧本（如 solo_matrixx），"
                "不要拿四角剧本硬上")
        return plan

    # 选角全绿之后，禁演时段才升级成硬拒——此前只是 warn，不遮挡缺角诊断。
    if plan.in_quiet_hours:
        plan.reason = "quiet_hours"
        plan.warnings.append(
            f"现在是禁演时段（本地 {at_hour} 点，窗口 {tuple(quiet)}）——深夜群戏是"
            "不需要统计就能看出来的特征。改时间，或把 live.quiet_hours 配成 [0, 0] 解除")
        return plan

    plan.ok = True
    return plan


# ── 真发主循环 ──────────────────────────────────────────────────────────────


async def perform(
    playbook: Playbook,
    candidates: Sequence[Dict[str, Any]],
    *,
    group_key: str,
    send: Optional[SendFn] = None,
    orchestrator: Any = None,
    platform: str = "telegram",
    generate: Optional[GenerateFn] = None,
    confirm_live: bool = False,
    app_config: Optional[Dict[str, Any]] = None,
    config: Optional[Dict[str, Any]] = None,
    store: Any = None,
    watch: Any = None,
    human_feed: Optional[Callable[..., Any]] = None,
    cast_kwargs: Optional[Dict[str, Any]] = None,
    fingerprint_groups: Optional[Dict[str, str]] = None,
    persona_gender: Optional[Dict[str, str]] = None,
    seed: int = 0,
    session_id: str = "",
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    now: Callable[[], float] = time.time,
    hour: Optional[int] = None,
) -> LiveResult:
    """真的演一场戏，真的把消息发到 ``group_key``。

    :param send: 投递出口；不传则由 ``orchestrator`` 现场适配。两者都没有 ＝ 不武装。
    :param confirm_live: 参数侧那把锁，**必须显式传 ``True``**（见 :func:`live_enabled`）。
    :param app_config: 整份 app 配置，**只用来读那把锁**。
    :param config: **导演参数**（扁平，如 ``{"max_lines": 12}``）——与 ``rehearse`` 的
        ``config`` 一字不差，这样同一份参数喂两条路径才会得到同一场戏。传错层由
        :func:`looks_like_app_config` 在入口点名（症状是「节奏悄悄回落缺省」，不会报错）。
    :param watch: :class:`~...ledgers.ContextWatch`，逐拍重判跨群冷却，并
        ``drain_humans`` 拉取本群进向消息喂给导演（让路 / ``human_takeover``）。
        不传 ＝ 不挂这两条闸门（测试可用 duck 只实现 ``delay_for`` / ``drain_humans``）。
    :param human_feed: 可选 ``(since_ts) -> [(sender, text, ts), ...]``，优先于
        ``watch.drain_humans``——门禁用假时钟注入真人插话，不碰收件箱。
    :param cast_kwargs: ``ShowContext.cast_kwargs()`` 的产物——共现／预算／角色三条闸门的
        参数。**别自己拼**：那是全系统唯一一处决定「选角受哪些约束」的地方，绕过它的表现
        是「一切正常，只是有一天号被封了」。
    :param sleep: / :param now: 只为测试注入（真发时就是 ``asyncio.sleep`` / ``time.time``）。
    :param hour: 本地钟点（判禁演时段用）。不传则按 ``time.localtime(now())`` 取——本机
        时区即运营时区，这是本模块唯一能站得住的假设。显式传是为了让测试与时区解耦。

    绝不抛业务异常：任何拒演都以 ``terminate_reason`` 如实返回。``asyncio.CancelledError``
    例外——取消必须往上传，但传之前会把已发出的那几条落库并把场次标成 ``aborted``。
    """
    sid = str(session_id or f"live_{uuid.uuid4().hex[:12]}")
    result = LiveResult(session_id=sid, playbook_id=str(getattr(playbook, "id", "")),
                        group_key=str(group_key or ""), platform=str(platform))

    plan = plan_live(
        playbook, candidates, group_key=str(group_key or ""),
        send=send, orchestrator=orchestrator, platform=platform,
        confirm_live=confirm_live, app_config=app_config, config=config,
        cast_kwargs=cast_kwargs, fingerprint_groups=fingerprint_groups,
        persona_gender=persona_gender, require_outlet=True,
        hour=hour, now=now)
    result.casting = plan.casting
    result.warnings.extend(plan.warnings)
    if not plan.ok:
        result.terminate_reason = plan.reason or "not_armed"
        if plan.reason == "not_armed":
            logger.warning("[group_show.live] 拒演：未武装 group=%s", group_key)
        elif plan.reason == "quiet_hours":
            logger.warning("[group_show.live] 拒演：禁演时段 group=%s", group_key)
        elif plan.reason == "no_fingerprints":
            logger.warning("[group_show.live] 拒演：无指纹表 group=%s", group_key)
        elif plan.reason == "understaffed":
            logger.warning("[group_show.live] 拒演：角色缺人 group=%s", group_key)
        else:
            logger.warning("[group_show.live] 拒演：%s group=%s",
                           plan.reason, group_key)
        return result

    sender = send or orchestrator_sender(
        orchestrator, platform=platform, group_key=str(group_key))
    casting = plan.casting
    assert casting is not None  # plan.ok ⇒ casting 已定

    # 开演前 peer 体检：演员「不在群/解析不了」是必然翻车，宁可后台拒演也不上台
    # 演半场（首拍成功+第二号连炸=群里留一半戏，比不演更假）。只读零发言。
    if orchestrator is not None and live_preflight_enabled(app_config):
        pf = await preflight_peers(orchestrator, casting,
                                   platform=platform, group_key=str(group_key))
        if not pf["ok"]:
            result.terminate_reason = "peer_unreachable"
            result.warnings.append(
                "开演前体检：演员对群不可达（不在群内？）: "
                + ", ".join(pf["unreachable"]))
            logger.warning("[group_show.live] 拒演：演员 peer 不可达 %s group=%s",
                           pf["unreachable"], group_key)
            return result

    started = float(now())
    state = ShowState(
        session_id=sid, group_key=str(group_key), playbook=playbook,
        casting=casting, platform=str(platform), dry_run=False,
        started_at=started, status="pending")
    # 开演**前**落库：中途被杀时，台账里至少知道「有这么一场戏，演到哪儿了」。
    _safe(store, "save_session", state)
    # 上台即证明在群里 —— 成员关系必须登记，否则成员共现矩阵会**低估**暴露面
    #（「一场戏都没演过 ⇒ 安全」那类假安全的另一个变体）。登记幂等且不覆盖首次进群时间。
    for member in casting.members:
        _safe(store, "record_membership", str(group_key), member.account_id,
              platform=str(platform), source="live")

    gen: GenerateFn = generate or _refuse_to_improvise
    director = GroupShowDirector(state, config)
    rng = random.Random(int(seed))
    names = {m.account_id: (m.display_name or m.persona_id or m.account_id)
             for m in casting.members}
    hint = group_hint_text()

    prev_len = 0
    skips = 0
    fails = 0
    guard = 0
    sent_texts: List[str] = []   # 复读闸的比对面：本场已真发出去的台词
    cast_ids = {m.account_id for m in casting.members}
    # 开演前一秒起扫：捕获「点开演时群里刚好有人在说」的那几条
    human_cursor = started - 1.0

    def _ingest_humans(at: float) -> None:
        """把本群新进向消息喂给导演；失败软吞——让路丢了总好过整场崩。"""
        nonlocal human_cursor
        rows: List[Any] = []
        try:
            if human_feed is not None:
                got = human_feed(since_ts=human_cursor)
                rows = list(got or ())
            elif watch is not None and hasattr(watch, "drain_humans"):
                rows = list(watch.drain_humans(
                    since_ts=human_cursor, exclude_accounts=list(cast_ids)) or ())
        except Exception:  # noqa: BLE001
            logger.debug("[group_show.live] 拉取真人插话失败（已忽略）",
                         exc_info=True)
            return
        for item in rows:
            try:
                if isinstance(item, (tuple, list)) and len(item) >= 3:
                    who, said, ts = str(item[0]), str(item[1]), float(item[2])
                elif isinstance(item, dict):
                    who = str(item.get("sender_name") or item.get("sender_id")
                              or "human")
                    said = str(item.get("text") or "")
                    ts = float(item.get("ts") or at)
                else:
                    continue
            except Exception:  # noqa: BLE001
                continue
            if not str(said or "").strip():
                continue
            director.observe_human(said, sender=who, ts=ts)
            result.humans_observed += 1
            if ts > human_cursor:
                human_cursor = ts

    try:
        while guard < MAX_ITERATIONS:
            guard += 1
            t = float(now())

            # ⑥ 硬顶先判 —— 放在导演之前，因为导演的预算是可配的，这两条不是
            if result.sent >= MAX_LIVE_LINES:
                result.terminate_reason = "hard_line_cap"
                logger.warning("[group_show.live] 触到条数硬顶 %d group=%s",
                               MAX_LIVE_LINES, group_key)
                break
            if t - started > MAX_LIVE_SECONDS:
                result.terminate_reason = "hard_time_cap"
                logger.warning("[group_show.live] 触到时长硬顶 group=%s", group_key)
                break

            # ⑥b 真人插话：先喂导演，再判接管 / 让路——与排练 human_script 同序
            _ingest_humans(t)

            stop, reason = director.should_terminate(now=t)
            if stop:
                result.terminate_reason = reason
                break

            if director.should_yield_to_human(now=t):
                result.human_yields += 1
                gap = float(director.cfg.human_yield_seconds)
                logger.info("[group_show.live] 真人让路 %.0fs group=%s",
                            gap, group_key)
                await sleep(max(0.0, gap))
                continue

            speaker = director.select_next_speaker()
            directive = director.next_directive()
            if speaker is None or directive is None:
                result.terminate_reason = "no_speaker"
                break

            # ⑦ 逐拍闸门：这个号刚刚在别的群说过话吗
            #    在**选角那一刻**判一次是不够的：一场戏演几十分钟，期间别的群随时有号
            #    冒头。ContextWatch 按 TTL 续读，所以这里问到的是「此刻」而不是「开演时」。
            wait = int(watch.delay_for(speaker.account_id, t)) if watch is not None else 0
            if wait > 0:
                if wait <= MAX_GATE_WAIT_SEC:
                    # 真发有真时钟，所以能**等**——这比跳过好：跳过会让戏少一句话，
                    # 等一会儿则什么都不损失（排练里没这个选项，虚拟时钟等于没等）。
                    result.gate_waits += 1
                    logger.info("[group_show.live] 跨群冷却，等 %ds 再让 %s 开口",
                                wait, speaker.account_id)
                    await sleep(float(wait))
                    continue   # 等完重判：这期间导演状态/真人插话都可能变了
                result.skipped += 1
                logger.info("[group_show.live] %s 冷却还剩 %ds，跳过这一拍",
                            speaker.account_id, wait)
                director.advance_beat(_skip_event(state, t, speaker, directive))
                continue

            # ⑧ 生成台词（人设发声链 + persona_guard 都在 generate 里）
            gen_at = float(now())
            text = gen(speaker, project_history_for(
                speaker, state.events, directive=directive, group_hint=hint,
                name_resolver=lambda a: names.get(a, a)), directive)
            if _is_awaitable(text):
                text = await text
            text = str(text or "").strip()

            # ⑧b 复读闸：与本场已发台词雷同 → 带禁令重试一次，仍雷同就弃拍。
            #    LLM 看得见自己刚说过的话却仍会复读（首场灰度实录：b4 换个句式又
            #    安利了一遍 b3 的卖点）；「同一卖点两分钟内说两遍」是比频率更早
            #    暴露的水军特征，宁可这拍不说话。走 text="" 复用下方既有弃拍电路
            #    ——连续弃拍照样会触发 generation_failed 收场，复读退化有兜底。
            if text and sent_texts and max(
                    line_similarity(text, p) for p in sent_texts
            ) >= MAX_LINE_SIMILARITY:
                result.repetition_retries += 1
                logger.info("[group_show.live] 台词与已发内容雷同，带禁令重试"
                            " beat=%s group=%s", directive.beat_id, group_key)
                retry_directive = replace(directive, must_not=tuple(
                    directive.must_not or ()) + (
                    "重复你在这个群里刚说过的意思或细节——那个点已经说过了，"
                    "换一个新的角度或干脆聊别的",))
                text = gen(speaker, project_history_for(
                    speaker, state.events, directive=retry_directive,
                    group_hint=hint,
                    name_resolver=lambda a: names.get(a, a)), retry_directive)
                if _is_awaitable(text):
                    text = await text
                text = str(text or "").strip()
                if text and max(line_similarity(text, p)
                                for p in sent_texts) >= MAX_LINE_SIMILARITY:
                    logger.info("[group_show.live] 重试后仍雷同，弃拍 %s group=%s",
                                directive.beat_id, group_key)
                    text = ""
            gen_elapsed = max(0.0, float(now()) - gen_at)

            if not text:
                # 守卫弃发 / 生成失败：这一拍不说话。连续多次 ＝ 生成侧挂了，显式收场
                skips += 1
                result.skipped += 1
                director.advance_beat(_skip_event(state, t, speaker, directive))
                if skips >= MAX_CONSECUTIVE_SKIPS:
                    result.terminate_reason = "generation_failed"
                    logger.warning(
                        "[group_show.live] 连续 %d 拍生成不出台词，中止 group=%s",
                        skips, group_key)
                    break
                continue
            skips = 0

            # ⑨ 节奏：等够间隔再发。**扣掉生成耗时**——LLM 那 2~5 秒本来就相当于真人
            #    「正在打字」的时间，不扣的话每个间隔都被推长，实际节奏与排练里看到的
            #    那份时刻表越走越偏，而运营是照着排练那份验收的。
            beat = state.playbook.beat_at(state.beat_cursor) or state.current_beat
            interval = float(beat_interval_seconds(
                beat, prev_text_len=prev_len, next_text_len=len(text), rng=rng))
            pause = max(0.0, interval - gen_elapsed)
            if pause > 0:
                await sleep(pause)

            # ⑩ 真发 —— 唯一出口，且**绝不重试**
            res = await sender(speaker.account_id, text)
            blocked = str((res or {}).get("blocked") or "")
            delivered = bool((res or {}).get("delivered"))

            if blocked:
                # 护栏在生效。它拦第一条就会拦后面每一条，继续演只是跟安全系统对抗。
                result.terminate_reason = "blocked"
                result.blocked_by = blocked
                logger.warning("[group_show.live] 被护栏拦下 (%s)，整场中止 group=%s",
                               blocked, group_key)
                break
            if not delivered:
                fails += 1
                result.failed += 1
                # 作废这一拍而不是重发：消息可能已经到了、只是回执丢了。
                director.advance_beat(_skip_event(state, t, speaker, directive))
                if fails >= MAX_CONSECUTIVE_SEND_FAILURES:
                    result.terminate_reason = "send_failed"
                    logger.warning("[group_show.live] 连续 %d 次投递失败，中止 group=%s",
                                   fails, group_key)
                    break
                continue
            fails = 0

            sent_at = float(now())
            ev = ShowEvent(
                seq=state.next_seq, ts=sent_at,
                speaker_account=speaker.account_id, role=speaker.slot,
                beat_id=directive.beat_id, text=text,
                kind="media" if directive.media else "line")
            director.advance_beat(ev)
            # 发成功**之后**才落库：先落库的话，一条没发出去的消息会进共现台账，
            # 于是风险读数虚高、运营对着不存在的消息排查。
            _safe(store, "append_event", sid, ev)
            # 进度也要中途刷：灰度实战里只 append_event 不 save_session →
            # 坐席 API 全程 status=pending / beat_cursor=0，像戏没开演。
            _safe(store, "save_session", state)

            prev_len = len(text)
            sent_texts.append(text)
            result.sent += 1
            result.lines.append(LiveLine(
                seq=ev.seq, at_seconds=round(sent_at - started, 1),
                account_id=speaker.account_id,
                display_name=speaker.display_name or speaker.account_id,
                persona_id=speaker.persona_id, role=speaker.slot,
                beat_id=directive.beat_id, text=text,
                soft_level=directive.soft_level, media=directive.media,
                kind=ev.kind, delivered=True,
                message_id=str((res or {}).get("message_id") or "")))
        else:
            result.terminate_reason = result.terminate_reason or "guard_limit"
    except asyncio.CancelledError:
        # 取消必须往上传（否则调用方以为停干净了），但传之前要把记录做实：已经发出去的
        # 那几条是真的发出去了，台账不能装作没这回事。
        result.terminate_reason = "aborted"
        state.status = "aborted"
        state.ended_at = float(now())
        result.duration_seconds = round(state.ended_at - started, 1)
        _safe(store, "save_session", state)
        logger.warning("[group_show.live] 被取消，已发 %d 条 group=%s",
                       result.sent, group_key)
        raise
    except Exception as exc:  # noqa: BLE001 —— 意外崩了也要留下正确的台账
        result.terminate_reason = "error"
        result.warnings.append(f"真发异常中止: {exc!r}")
        state.status = "aborted"
        state.ended_at = float(now())
        result.duration_seconds = round(state.ended_at - started, 1)
        _safe(store, "save_session", state)
        logger.error("[group_show.live] 异常中止 group=%s: %s", group_key, exc,
                     exc_info=True)
        return result

    state.status = "done"
    state.ended_at = float(now())
    result.duration_seconds = round(state.ended_at - started, 1)
    result.naturalness = naturalness_score(
        [e for e in state.events if e.kind in ("line", "media")])
    _safe(store, "save_session", state)
    logger.info("[group_show.live] 收场 %s group=%s 已发 %d 条 失败 %d 跳过 %d",
                result.terminate_reason, group_key, result.sent, result.failed,
                result.skipped)
    return result


# ── 渲染 ────────────────────────────────────────────────────────────────────


def format_live(result: LiveResult) -> str:
    """真发结果渲染（CLI / 看板共用）。刻意与 ``format_rehearsal`` 长得不一样——
    运营一眼要能看出「这是真发过的」，而不是靠读标题里那两个字。"""
    out: List[str] = []
    out.append(f"███ 真发：{result.playbook_id} @ {result.group_key} "
               f"({result.platform}) ███")
    if not result.armed:
        out.append("⛔ 未武装，一条都没发")
        out.extend(f"　· {w}" for w in result.warnings)
        return "\n".join(out)
    if result.casting is not None:
        cast_desc = "  ".join(
            f"{m.slot}={m.display_name or m.account_id}({m.persona_id})"
            for m in result.casting.members)
        out.append(f"演员表：{cast_desc or '（空）'}")
    for w in result.warnings:
        out.append(("⚠ " if w.startswith("warn:") else "✖ ") + w)
    out.append("")
    for ln in result.lines:
        mm, ss = divmod(int(ln.at_seconds), 60)
        who = ln.display_name or ln.account_id
        media = f" 📎{ln.media}" if ln.media else ""
        out.append(f"[{mm:02d}:{ss:02d}] ✅ {who}（{ln.role}{media}）：{ln.text}")
    out.append("")
    tail = f"　拦截：{result.blocked_by}" if result.blocked_by else ""
    out.append(
        f"收场：{result.terminate_reason}　已发 {result.sent} 条　"
        f"失败 {result.failed}　跳过 {result.skipped}　"
        f"等冷却 {result.gate_waits} 次　"
        f"真人 {result.humans_observed} 条/让路 {result.human_yields}　"
        f"时长 {int(result.duration_seconds // 60)} 分{tail}")
    nat = result.naturalness or {}
    if nat:
        out.append(f"自然度：{nat.get('score', 0):.2f}（{nat.get('verdict', '?')}）")
    return "\n".join(out)


# ── 内部 ────────────────────────────────────────────────────────────────────


async def _refuse_to_improvise(*_args: Any, **_kwargs: Any) -> str:
    """没传生成器时的默认「生成器」：一个字都不产。

    ``rehearse`` 缺省用 ``stub_generator``（产 ``[persona·beat] intent`` 这种占位串），
    在排练里正好——秒出、可断言。真发里同一个缺省会把**占位串发进真群**。所以这里的缺省
    必须是「不说话」，让戏走 ``generation_failed`` 显式收场。
    """
    logger.error("[group_show.live] 未传 generate，拒绝即兴发挥（不会发占位串）")
    return ""


def _skip_event(state: ShowState, ts: float, speaker: Any,
                directive: Any) -> ShowEvent:
    """一拍作废的事件。``kind="skip"`` 不在 ``SPOKEN_KINDS`` 里，所以**不进**共现台账
    ——没发出去的话，平台侧一个字都看不到。"""
    return ShowEvent(
        seq=state.next_seq, ts=float(ts),
        speaker_account=str(getattr(speaker, "account_id", "")),
        role=str(getattr(speaker, "slot", "")),
        beat_id=str(getattr(directive, "beat_id", "")), text="", kind="skip")


def _is_awaitable(obj: Any) -> bool:
    return hasattr(obj, "__await__")


#: 空槽原因码 → 该做什么。**四种原因的处置完全相反**，所以拒演信息里必须带上归因：
#: 只报「缺角 advocate」时，运营最自然的动作是去买号——而这个动作在 ``budget`` / ``pair``
#: 两种原因下是**反的**（护栏正在正常工作，加号只会让它拦得更多）。归因错一次的代价是
#: 一次错误的采购加一次「怎么加了号还是演不了」的信任损失。
_BLOCK_ADVICE: Dict[str, str] = {
    "pool": "号不够，或剩下的号全被指纹组锁死了——补号或给号配独立出口",
    "budget": "已到本场开口人数预算，护栏在正常工作；要多上人得先降群数或加号池",
    "pair": "与场上的号同台已到线——换一批号来铺这个群，别再让这几个凑一桌",
    "role": "这个号当主推的群已经太多了——把种草角色让给别人，或本场干脆不排主推",
    "": "原因未记录（多半是剧本声明了角色但号池里没有可用的号）",
}


def _understaffed_roles(playbook: Any, casting: Any) -> List[tuple]:
    """有戏份、却没有号来演的角色 → ``[(角色, 原因码), ...]``（按剧本声明序）。

    只看**真有拍的角色**：剧本里声明了 ``skeptic`` 但一拍都没安排给它时，没人演它毫无
    影响；而 ``asker`` 有三拍却空着，导演就会拿别人顶上——那才是要拦的。

    原因码取自 ``Casting.reason_for``（``pool`` / ``budget`` / ``pair`` / ``role``），
    见 :data:`_BLOCK_ADVICE`。
    """
    try:
        with_beats = [str(getattr(b, "role", "") or "")
                      for b in (getattr(playbook, "beats", ()) or ())]
        filled = {str(getattr(m, "slot", "") or "")
                  for m in (getattr(casting, "members", ()) or ())}
        reason_for = getattr(casting, "reason_for", None)
        out: List[tuple] = []
        seen = set()
        for role in with_beats:
            if not role or role in filled or role in seen:
                continue
            seen.add(role)
            why = ""
            if callable(reason_for):
                try:
                    why = str(reason_for(role) or "")
                except Exception:  # noqa: BLE001 —— 归因失败不该让拒演本身失效
                    why = ""
            out.append((role, why))
        return out
    except Exception:  # noqa: BLE001 —— 算不出来按「不缺」，别用一个坏检查挡住整条链路
        logger.debug("[group_show.live] 角色齐备度判定失败（按不缺处理）",
                     exc_info=True)
        return []


def _local_hour(now: Callable[[], float]) -> Optional[int]:
    """本机时区下的钟点（算不出来返回 ``None`` ＝ 放行，不拿一个坏钟点去禁演）。"""
    try:
        return int(time.localtime(float(now())).tm_hour)
    except Exception:  # noqa: BLE001
        logger.debug("[group_show.live] 本地钟点解析失败（禁演闸门放行）", exc_info=True)
        return None


def _safe(store: Any, method: str, *args: Any, **kwargs: Any) -> None:
    """落库软失败：写库出错不该让一场**已经发出去**的戏演不下去（消息已在群里，
    此刻中止只会让记录更不完整）。"""
    if store is None:
        return
    try:
        fn = getattr(store, method, None)
        if callable(fn):
            fn(*args, **kwargs)
    except Exception:  # noqa: BLE001
        logger.debug("[group_show.live] %s 失败（已忽略）", method, exc_info=True)


__all__ = [
    "MAX_CONSECUTIVE_SEND_FAILURES",
    "MAX_GATE_WAIT_SEC",
    "MAX_ITERATIONS",
    "MAX_LIVE_LINES",
    "MAX_LIVE_SECONDS",
    "LiveLine",
    "LivePlan",
    "LiveResult",
    "SendFn",
    "format_live",
    "is_solo_playbook",
    "live_enabled",
    "live_quiet_hours",
    "looks_like_app_config",
    "orchestrator_sender",
    "perform",
    "plan_live",
]
