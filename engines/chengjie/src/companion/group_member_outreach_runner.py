"""同群开口调度器：把「已批准」的人在时段内、带随机间隔地自动发出去。

设计边界（和手动发一条完全同一条链）：
- 只跑**本实例编排器 owns 的** Telegram 号。双实例共用一个库，但 pyrogram client 只
  活在一个进程里；owns 就是天然分区，不另加分布式锁。
- 每 tick（默认 150s）每个号最多发 **1 条**；发完给这个号抽一个 25–90 分钟的下次时间。
  这个抖动叠在 ``prepare_release`` 的硬间隔（min_gap）之上，不是替代。
- 所有闸门不变：急停 / 风控熔断 / 时段 / 日额度（含新号爬坡）/ 间隔 / 总发送闸门。
  任一不过 → 这个号本 tick 跳过，不翻任何人的状态。
- ``approve`` 模式只发坐席批过的；``auto`` 模式队列空了会自己排队 + AI 拟稿 + 批准。
  ``manual`` 模式调度器完全不碰。
- 发送失败按 ``finalize_release`` 分桶；flood → 存储层已给这个号上熔断，下 tick 自动跳过。

全程防御：任何一个号的异常不影响其它号，也不影响下一 tick。
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional

from src.companion.group_members_store import (
    OUTREACH_MODE_APPROVE,
    OUTREACH_MODE_AUTO,
    OUTREACH_QUEUED,
    OUTREACH_SENDING,
)

logger = logging.getLogger("ai_chat_assistant.group_member_outreach_runner")

RUNNER_INTERVAL_SEC = 150.0
JITTER_MIN_SEC = 25 * 60
JITTER_MAX_SEC = 90 * 60


class OutreachRunner:
    """一个进程一个。``tick()`` 可单独调用（测试 / 诊断）。

    依赖全部可注入：
    - ``store``：GroupMembersStore
    - ``cfg_fn()``：全量配置 dict
    - ``accounts_fn()``：本实例此刻能发的 telegram account_id 列表
    - ``pyro_fn(account_id)``：(pyro, loop) 或 (None, None)
    - ``ai_fn()``：AIClient 或 None
    - ``inbox_fn()``：inbox store 或 None（读默认目标用）
    - ``gate_fn(account_id)``：(blocked, reason)
    - ``age_fn(account_id, now)``：天龄或 None
    - ``count_fn(account_id, now)``：成功后计入总发送闸门
    - ``audit_fn(action, target, detail)``：审计
    - ``deliver_fn(pyro, loop, user_id, access_hash, text)``：实际发送（默认跨 loop 调 deliver_outreach）
    """

    def __init__(self, store: Any, *, cfg_fn: Callable[[], Dict[str, Any]],
                 accounts_fn: Callable[[], List[str]],
                 pyro_fn: Callable[[str], Any],
                 ai_fn: Callable[[], Any] = lambda: None,
                 inbox_fn: Callable[[], Any] = lambda: None,
                 registry_fn: Callable[[], Any] = lambda: None,
                 gate_fn: Optional[Callable[[str], Any]] = None,
                 age_fn: Optional[Callable[[str, float], Any]] = None,
                 count_fn: Optional[Callable[[str, float], None]] = None,
                 audit_fn: Optional[Callable[[str, str, str], None]] = None,
                 deliver_fn: Optional[Callable[..., Awaitable[Dict[str, Any]]]] = None,
                 rng: Optional[random.Random] = None,
                 interval: float = RUNNER_INTERVAL_SEC) -> None:
        self.store = store
        self.cfg_fn = cfg_fn
        self.accounts_fn = accounts_fn
        self.pyro_fn = pyro_fn
        self.ai_fn = ai_fn
        self.inbox_fn = inbox_fn
        self.registry_fn = registry_fn
        self.gate_fn = gate_fn or (lambda _a: (False, ""))
        self.age_fn = age_fn or (lambda _a, _n: None)
        self.count_fn = count_fn or (lambda _a, _n: None)
        self.audit_fn = audit_fn or (lambda *_: None)
        self.deliver_fn = deliver_fn or _deliver_cross_loop
        self.rng = rng or random.Random()
        self.interval = float(interval)
        self._next_ok_at: Dict[str, float] = {}
        self._stalled_audit_day: Dict[str, float] = {}
        self.last_tick: Dict[str, Any] = {}
        self._task: Optional[asyncio.Task] = None

    # ── 配置 ──

    def _gm_cfg(self) -> Dict[str, Any]:
        try:
            return ((self.cfg_fn() or {}).get("companion") or {}).get("group_members") or {}
        except Exception:
            return {}

    def _policy(self):
        from src.companion.group_member_outreach import OutreachPolicy
        return OutreachPolicy.from_config(self._gm_cfg())

    def _jitter(self) -> float:
        g = self._gm_cfg()
        try:
            lo = float(g.get("outreach_auto_gap_min_sec", JITTER_MIN_SEC))
            hi = float(g.get("outreach_auto_gap_max_sec", JITTER_MAX_SEC))
        except Exception:
            lo, hi = JITTER_MIN_SEC, JITTER_MAX_SEC
        lo = max(60.0, lo)
        hi = max(lo, hi)
        return self.rng.uniform(lo, hi)

    # ── 一轮 ──

    async def tick(self, now: Optional[float] = None, *, hour: Optional[int] = None) -> Dict[str, Any]:
        now = time.time() if now is None else float(now)
        from src.companion.group_member_extract import local_midnight_ts
        since = local_midnight_ts(now)
        report: Dict[str, Any] = {"ts": now, "accounts": {}}
        try:
            accounts = list(self.accounts_fn() or [])
        except Exception:
            logger.debug("[gm_runner] 取账号列表失败", exc_info=True)
            accounts = []
        for acct in accounts:
            acct = str(acct or "").strip()
            if not acct:
                continue
            try:
                report["accounts"][acct] = await self._tick_account(acct, now, since, hour=hour)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[gm_runner] 账号 %s 本轮异常：%s", acct, exc, exc_info=True)
                report["accounts"][acct] = {"kind": "error", "detail": str(exc)[:200]}
        self.last_tick = report
        return report

    async def _tick_account(self, acct: str, now: float, since: float, *,
                            hour: Optional[int]) -> Dict[str, Any]:
        from src.companion.group_member_outreach import (
            auto_mode_block_reason,
            classify_send_error,
            finalize_release,
            followup_close_before,
            hold_block_reason,
            hours_block_reason,
            prepare_release,
        )
        st = self.store
        policy = self._policy()
        # 跟进过又过了 N 小时还没回音的 → 封存（manual 号也一起收，这步不发任何东西）
        try:
            st.close_unanswered(followup_close_before(policy, now), acct)
        except Exception:
            logger.debug("[gm_runner] 封存失败 account=%s", acct, exc_info=True)
        # 第二跳没接上（回了 N 小时我方没回话）：每号每天最多提醒一次进审计——这是「开口成功、
        # 后续掉地」的唯一信号，manual 号也查
        stalled = self._audit_stalled(acct, now, since, policy)
        mode = st.get_outreach_mode(acct)
        if mode not in (OUTREACH_MODE_APPROVE, OUTREACH_MODE_AUTO):
            return {"kind": "manual", "stalled": stalled}
        age = self.age_fn(acct, now)
        demoted = ""
        if mode == OUTREACH_MODE_AUTO:
            # 全自动门槛每轮复核：不达标自动退回「批准后发」，人重新接手
            gate = auto_mode_block_reason(st, acct, now=now, age_days=age, policy=policy)
            if not gate.get("ok"):
                st.set_outreach_mode(acct, OUTREACH_MODE_APPROVE)
                mode = OUTREACH_MODE_APPROVE
                demoted = str(gate.get("reason") or "")
                logger.warning("[gm_runner] 账号 %s 不再满足全自动门槛（%s），已退回 approve",
                               acct, demoted)
                try:
                    self.audit_fn("tg_members_outreach_auto_demote", acct,
                                  "reason=%s detail=%s" % (demoted, gate.get("detail")))
                except Exception:
                    pass
        blk = hold_block_reason(st, acct, now)
        if blk:
            return {"kind": blk, "demoted": demoted}
        blk = hours_block_reason(policy, now, hour=hour)
        if blk:
            return {"kind": blk, "demoted": demoted}
        nxt = self._next_ok_at_for(acct)
        if now < nxt:
            return {"kind": "jitter", "wait_sec": int(nxt - now), "demoted": demoted}
        try:
            blocked, reason = self.gate_fn(acct)
        except Exception:
            blocked, reason = False, ""
        if blocked:
            return {"kind": "gate", "detail": str(reason or ""), "demoted": demoted}

        if mode == OUTREACH_MODE_AUTO:
            await self._auto_fill(acct, now, since, policy, age)

        row = st.next_approved(acct)
        if not row:
            fu = await self._followup_one(acct, now, since, policy, hour=hour)
            if demoted:
                fu["demoted"] = demoted
            return fu
        gid, uid = str(row["group_id"]), str(row["user_id"])
        prep = prepare_release(
            st, account_id=acct, group_id=gid, user_id=uid, text="",
            now=now, since_ts=since, policy=policy, age_days=age, hour=hour)
        if not prep.get("ok"):
            kind = str(prep.get("kind") or "")
            if kind == "text":
                # 文案没过守卫（极少：批准后规则变了）→ 跳过这个人，别卡住整队
                st.skip_outreach(gid, uid, reason="opener_rejected")
            return {"kind": kind, "user_id": uid}
        pyro, loop = self.pyro_fn(acct)
        if pyro is None:
            st.cas_outreach(gid, uid, expect_states=(OUTREACH_SENDING,),
                            new_state=str(prep.get("prev_state") or OUTREACH_QUEUED),
                            account_id=acct, error="no_client", now=now)
            return {"kind": "no_client", "user_id": uid}
        try:
            sent = await self.deliver_fn(pyro, loop, user_id=prep["user_id"],
                                         access_hash=prep["access_hash"], text=prep["text"])
        except Exception as exc:  # noqa: BLE001
            sent = {"ok": False, "kind": classify_send_error(exc)}
        done = finalize_release(
            st, account_id=acct, group_id=gid, user_id=uid, now=now, text=prep["text"],
            result=sent if isinstance(sent, dict) else {"ok": False, "kind": "retryable"})
        kind = str(done.get("kind") or "")
        if done.get("ok"):
            self.count_fn(acct, now)
            nxt = self._set_next_ok_at(acct, now + self._jitter())
            logger.info("[gm_runner] 自动开口已发 account=%s user=%s 下次≥%ds 后",
                        acct, uid, int(nxt - now))
        else:
            # 失败也歇一会儿，别在同一 tick 链上连撞
            self._set_next_ok_at(acct, now + max(300.0, self._jitter() / 3))
        try:
            self.audit_fn("tg_members_outreach_auto_send", acct,
                          "group=%s user=%s kind=%s mode=%s" % (gid, uid, kind, mode))
        except Exception:
            pass
        out = {"kind": "sent" if done.get("ok") else kind, "user_id": uid}
        if demoted:
            out["demoted"] = demoted
        if stalled:
            out["stalled"] = stalled
        return out

    def _audit_stalled(self, acct: str, now: float, since: float, policy) -> int:
        """回了但没接上的人数；本轮还没打过标的给收件箱会话打「需人工」（每轮只打一次，
        坐席手动摘了不再打回去）；>0 且今天还没提醒过 → 写一条审计（带前几个 user_id）。"""
        from src.companion.group_member_outreach import flag_stalled_in_inbox, stalled_before
        before = stalled_before(policy, now)
        try:
            rows = self.store.list_stalled_replies(before, acct, limit=20)
        except Exception:
            logger.debug("[gm_runner] 查没接上失败 account=%s", acct, exc_info=True)
            return 0
        n = len(rows)
        if n:
            try:
                inbox = self.inbox_fn()
            except Exception:
                inbox = None
            if inbox is not None:
                try:
                    fresh = self.store.list_stalled_replies(before, acct, limit=20, unflagged_only=True)
                except Exception:
                    fresh = []
                for r in fresh:
                    flag_stalled_in_inbox(inbox, r, now=now)
                    self.store.mark_stalled_flagged(r.get("group_id"), r.get("user_id"), now)
        if n and self._stalled_audit_day.get(acct) != since:
            self._stalled_audit_day[acct] = since
            ids = ",".join(str(r.get("user_id") or "") for r in rows[:5])
            logger.warning("[gm_runner] 回音没接上 account=%s n=%d users=%s（>%dh 我方未回话）",
                           acct, n, ids, int(policy.stalled_after_hours))
            try:
                self.audit_fn("tg_members_outreach_stalled", acct,
                              "n=%d after_hours=%d users=%s" % (n, int(policy.stalled_after_hours), ids))
            except Exception:
                pass
        return n

    # 下次允许自动发的时刻落在 holds 表（重启不丢抖动），内存只是缓存
    def _next_ok_at_for(self, acct: str) -> float:
        if acct in self._next_ok_at:
            return self._next_ok_at[acct]
        try:
            v = float(self.store.get_hold(acct).get("next_auto_at") or 0.0)
        except Exception:
            v = 0.0
        self._next_ok_at[acct] = v
        return v

    def _set_next_ok_at(self, acct: str, ts: float) -> float:
        self._next_ok_at[acct] = float(ts)
        try:
            self.store.set_hold(acct, next_auto_at=float(ts))
        except Exception:
            logger.debug("[gm_runner] next_auto_at 落库失败", exc_info=True)
        return float(ts)

    async def _followup_one(self, acct: str, now: float, since: float, policy, *,
                            hour: Optional[int]) -> Dict[str, Any]:
        """开口队列空了才轮到跟进：给发出 ≥72h 没回音的人补一句，每 tick 最多一条。"""
        from src.companion.group_member_opener import build_opener_context, compose_followup
        from src.companion.group_member_outreach import (
            classify_send_error,
            finalize_followup,
            followup_due_before,
            prepare_followup,
        )
        st = self.store
        if not policy.followup_enabled:
            return {"kind": "idle"}
        due = st.list_followup_due(acct, followup_due_before(policy, now), limit=5)
        if not due:
            return {"kind": "idle"}
        if st.count_followups_since(acct, since) >= int(policy.followup_daily_cap):
            return {"kind": "followup_cap"}
        m = due[0]
        gid, uid = str(m["group_id"]), str(m["user_id"])
        try:
            ctx = build_opener_context(self.cfg_fn() or {}, "telegram", acct, self.inbox_fn(),
                                       registry=self.registry_fn())
            got = await compose_followup(self.ai_fn(), member=m, ctx=ctx)
        except Exception:
            logger.debug("[gm_runner] 跟进拟稿失败", exc_info=True)
            return {"kind": "followup_compose_failed", "user_id": uid}
        text = str(got.get("text") or "").strip()
        prep = prepare_followup(st, account_id=acct, group_id=gid, user_id=uid, text=text,
                                now=now, since_ts=since, policy=policy, hour=hour)
        if not prep.get("ok"):
            return {"kind": str(prep.get("kind") or ""), "user_id": uid}
        pyro, loop = self.pyro_fn(acct)
        if pyro is None:
            st.unclaim_followup(gid, uid)
            return {"kind": "no_client", "user_id": uid}
        try:
            sent = await self.deliver_fn(pyro, loop, user_id=prep["user_id"],
                                         access_hash=prep["access_hash"], text=prep["text"])
        except Exception as exc:  # noqa: BLE001
            sent = {"ok": False, "kind": classify_send_error(exc)}
        done = finalize_followup(
            st, account_id=acct, group_id=gid, user_id=uid, now=now, text=prep["text"],
            result=sent if isinstance(sent, dict) else {"ok": False, "kind": "retryable"})
        kind = str(done.get("kind") or "")
        if done.get("ok"):
            self.count_fn(acct, now)
            self._set_next_ok_at(acct, now + self._jitter())
            logger.info("[gm_runner] 自动跟进已发 account=%s user=%s source=%s",
                        acct, uid, got.get("source"))
        else:
            self._set_next_ok_at(acct, now + max(300.0, self._jitter() / 3))
        try:
            self.audit_fn("tg_members_outreach_auto_followup", acct,
                          "group=%s user=%s kind=%s source=%s" % (gid, uid, kind,
                                                                  got.get("source")))
        except Exception:
            pass
        return {"kind": "followup_sent" if done.get("ok") else kind, "user_id": uid}

    async def _auto_fill(self, acct: str, now: float, since: float, policy, age) -> None:
        """全自动：队列空了就排队 → 拟稿 → 批准。任何一步失败只记日志。"""
        from src.companion.group_member_opener import build_opener_context, compose_queue
        from src.companion.group_member_outreach import enqueue_today
        st = self.store
        try:
            if st.count_outreach_queued(acct) == 0:
                enqueue_today(st, acct, now=now, since_ts=since, policy=policy, age_days=age)
            ctx = build_opener_context(self.cfg_fn() or {}, "telegram", acct, self.inbox_fn(),
                                       registry=self.registry_fn())
            await compose_queue(st, self.ai_fn(), account_id=acct, ctx=ctx, since_ts=since)
            st.approve_queued(acct, now)
        except Exception:
            logger.debug("[gm_runner] 全自动补队失败 account=%s", acct, exc_info=True)

    # ── 循环 ──

    async def run_forever(self) -> None:
        # 启动先歇一拍，等编排器把 worker 拉起来
        await asyncio.sleep(min(60.0, self.interval / 2))
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("[gm_runner] tick 异常", exc_info=True)
            await asyncio.sleep(self.interval)

    def start(self) -> asyncio.Task:
        self._task = asyncio.create_task(self.run_forever(), name="group_outreach_runner")
        return self._task


async def _deliver_cross_loop(pyro: Any, loop: Any, *, user_id: int, access_hash: int,
                              text: str) -> Dict[str, Any]:
    from src.companion.group_member_outreach import deliver_outreach
    coro = deliver_outreach(pyro, user_id=user_id, access_hash=access_hash, text=text)
    if loop is None or loop is asyncio.get_running_loop():
        return await coro
    return await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(coro, loop))


def owned_telegram_accounts() -> List[str]:
    """本实例编排器里正在跑、能发的 Telegram 号。编排器没起 → 空。"""
    try:
        from src.integrations.account_orchestrator import get_orchestrator_if_running
        orch = get_orchestrator_if_running()
        if orch is None:
            return []
        out: List[str] = []
        for a in (orch.status() or {}).get("accounts") or []:
            if str(a.get("platform") or "") != "telegram":
                continue
            aid = str(a.get("account_id") or "")
            if aid and orch.owns("telegram", aid):
                out.append(aid)
        return out
    except Exception:
        logger.debug("[gm_runner] 读编排器失败", exc_info=True)
        return []


def build_runner_for_assistant(assistant: Any) -> Optional[OutreachRunner]:
    """从 main 的 assistant 装配一个 runner；功能没开或库没就绪 → None。"""
    try:
        full = assistant.config.config or {}
    except Exception:
        full = {}
    gm = ((full.get("companion") or {}).get("group_members") or {})
    enabled = bool(gm.get("enabled", False))
    if not enabled:
        try:
            from src.web.ui_visibility import resolve_ui_visibility
            enabled = bool(resolve_ui_visibility(full).get("group_extract"))
        except Exception:
            enabled = False
    if not enabled or not bool(gm.get("outreach_runner_enabled", True)):
        return None
    from src.companion.group_members_store import get_group_members_store
    store = get_group_members_store()
    if store is None:
        return None
    app = getattr(assistant, "_web_app", None)

    def _cfg() -> Dict[str, Any]:
        try:
            return assistant.config.config or {}
        except Exception:
            return {}

    def _pyro(acct: str):
        try:
            from src.web.routes.unified_inbox_account_routes import _get_tg_pyro_for_account
            pyro = _get_tg_pyro_for_account(app, acct)
        except Exception:
            pyro = None
        loop = getattr(pyro, "loop", None)
        if pyro is None or loop is None or not loop.is_running():
            return None, None
        return pyro, loop

    def _registry():
        try:
            reg = getattr(getattr(app, "state", None), "account_registry", None)
            if reg is not None:
                return reg
            from src.integrations.account_registry import get_account_registry
            return get_account_registry()
        except Exception:
            return None

    def _gate(acct: str):
        try:
            from src.integrations.shared.send_guard import send_blocked
            return send_blocked("telegram", acct, config=_cfg(), registry=_registry(),
                                chat_key="", notify=True, origin="auto")
        except Exception:
            return False, ""

    def _age(acct: str, now: float):
        try:
            reg = _registry()
            row = reg.get("telegram", acct) if reg is not None else None
            created = float((row or {}).get("created_at") or 0.0)
        except Exception:
            created = 0.0
        return None if created <= 0 else max(0.0, (now - created) / 86400.0)

    def _count(acct: str, now: float) -> None:
        try:
            from src.integrations.protocol_autoreply_limits import get_autoreply_limiter
            get_autoreply_limiter(_cfg()).record_sent("telegram:%s" % acct, now)
        except Exception:
            logger.debug("[gm_runner] 总发送计数失败", exc_info=True)

    def _audit(action: str, target: str, detail: str) -> None:
        store_a = getattr(getattr(app, "state", None), "audit_store", None) \
            or getattr(assistant, "audit_store", None)
        if store_a is None:
            return
        try:
            store_a.log("scheduler", action, target, "", detail)
        except Exception:
            pass

    try:
        interval = float(gm.get("outreach_runner_interval_sec", RUNNER_INTERVAL_SEC))
    except Exception:
        interval = RUNNER_INTERVAL_SEC
    return OutreachRunner(
        store, cfg_fn=_cfg, accounts_fn=owned_telegram_accounts, pyro_fn=_pyro,
        ai_fn=lambda: getattr(assistant, "ai_client", None),
        inbox_fn=lambda: getattr(assistant, "inbox_store", None),
        registry_fn=_registry,
        gate_fn=_gate, age_fn=_age, count_fn=_count, audit_fn=_audit,
        interval=max(30.0, interval),
    )


__all__ = [
    "RUNNER_INTERVAL_SEC", "JITTER_MIN_SEC", "JITTER_MAX_SEC",
    "OutreachRunner", "owned_telegram_accounts", "build_runner_for_assistant",
]
