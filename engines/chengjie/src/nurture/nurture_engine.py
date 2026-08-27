"""智能养号 · 执行引擎（P1，2026-08-21）——镜像 CareDispatcher 的常备接线 + 配置热闸。

状态机（overlay 键，非内存）：
- ``ops.nurture.enabled=false``          → pause（默认；空转，零副作用）
- ``enabled=true`` + ``dry_run=true``    → dry_run（算计划 + 影子记录，**不碰账号**）
- ``enabled=true`` + ``dry_run=false``   → go_live（**仅金丝雀白名单**账号真动作）

安全不变量（养号是主动自动化，风险高于被动限流，逐条钉死）：
1. 默认全关；go_live 必须先 enabled；
2. go_live 真动作**只对 ``canary_accounts`` 内**的号（空名单=零真动作，非金丝雀号仍走影子）；
3. 动作前查 kill-switch / 生命周期风险态（banned/restricted），命中跳过；
4. read=标记已读（不过 send-gate，最安全）；self_chat=发到本号收藏消息('me')且受
   ``self_chat.enabled`` 额外总闸；browse/react/online **无现成高层 API** → 只影子规划、
   go_live 记 no_adapter（诚实，不假装做了）；
5. 每号每 tick 至多一条动作 + 全局 ``max_actions_per_tick`` 双封顶（细水长流不爆发）。

动作回调注入（testable：单测喂假 cb；bootstrap 接 orch.mark_read / orch.send + 跨 loop 封送）。
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional

from src.nurture.nurture_scheduler import plan_due_actions

logger = logging.getLogger("ai_chat_assistant.nurture_engine")

# 有现成高层动作 API 的行为（其余在 go_live 记 no_adapter）。
_EXECUTABLE_KINDS = frozenset({"read", "self_chat"})

# 回调类型
AccountsProvider = Callable[[], List[Dict[str, Any]]]
SignalsProvider = Callable[[List[Dict[str, Any]]], Dict[str, Dict[str, Any]]]
CfgProvider = Callable[[], Optional[Dict[str, Any]]]
ActionCb = Callable[[str, str], Awaitable[bool]]      # (platform, account_id) -> ok
BlockCheck = Callable[[str, str], Awaitable[bool]]     # (platform, account_id) -> blocked?


class NurtureEngine:
    def __init__(
        self,
        ledger,
        *,
        cfg_provider: CfgProvider,
        accounts_provider: AccountsProvider,
        signals_provider: SignalsProvider,
        read_cb: Optional[ActionCb] = None,
        self_chat_cb: Optional[ActionCb] = None,
        block_check: Optional[BlockCheck] = None,
        interval_sec: float = 900.0,
        max_actions_per_tick: int = 20,
    ) -> None:
        self._ledger = ledger
        self._cfg_provider = cfg_provider
        self._accounts_provider = accounts_provider
        self._signals_provider = signals_provider
        self._read_cb = read_cb
        self._self_chat_cb = self_chat_cb
        self._block_check = block_check
        self._interval = max(60.0, float(interval_sec))
        self._max_per_tick = max(1, int(max_actions_per_tick))
        self.last_tick_ts: float = 0.0
        self.last_tick_planned: int = 0
        self.last_tick_executed: int = 0
        self.last_tick_gated: bool = False
        self._stop_evt: Optional[asyncio.Event] = None
        self._task: Optional[asyncio.Task] = None

    def _live_cfg(self) -> Optional[dict]:
        try:
            cfg = self._cfg_provider()
            return cfg if isinstance(cfg, dict) else None
        except Exception:
            logger.debug("nurture cfg_provider 读取异常", exc_info=True)
            return None

    def is_running(self) -> bool:
        return bool(self._task and not self._task.done())

    def health_snapshot(self) -> dict:
        cfg = self._live_cfg() or {}
        canary = list(cfg.get("canary_accounts") or [])
        return {
            "running": self.is_running(),
            "enabled": bool(cfg.get("enabled", False)),
            "dry_run": bool(cfg.get("dry_run", True)),
            "interval_sec": self._interval,
            "canary_count": len(canary),
            "last_tick_ts": self.last_tick_ts,
            "last_tick_planned": self.last_tick_planned,
            "last_tick_executed": self.last_tick_executed,
            "last_tick_gated": self.last_tick_gated,
            "ledger": self._ledger.stats() if self._ledger else {},
        }

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop_evt = asyncio.Event()
        self._task = asyncio.create_task(self._loop(), name="nurture_engine")

    async def stop(self) -> None:
        if self._stop_evt:
            self._stop_evt.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=10.0)
            except asyncio.TimeoutError:
                self._task.cancel()
            except Exception:
                pass

    async def _loop(self) -> None:
        try:
            while not (self._stop_evt and self._stop_evt.is_set()):
                try:
                    n = await self.run_once()
                    if n:
                        logger.info("[nurture_engine] tick: %d actions (executed %d)",
                                    n, self.last_tick_executed)
                except Exception:
                    logger.exception("nurture_engine run_once 异常")
                try:
                    if self._stop_evt:
                        await asyncio.wait_for(self._stop_evt.wait(), timeout=self._interval)
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("nurture_engine 退出")

    async def probe_action(
        self, platform: str, account_id: str, kind: str,
        *, confirm: bool = False, now: Optional[float] = None,
    ) -> Dict[str, Any]:
        """**手动单动作探针**（go_live 真机验证路子）：对指定账号跑一次某养护动作。

        ``confirm=False``（默认）＝**预检**：只判「可执行(有适配器) + 未被 kill-switch 拦」，
        **不真跑**（零副作用）；``confirm=True``＝**真跑**一次（read=标记本号一条私聊已读 /
        self_chat=发一条到本号收藏消息，都只影响账号自身），落账便于对账。

        安全：不要求引擎 enabled / 金丝雀——这是运营**显式**的单次验证动作（像
        live_multiwin_drill --confirm），但仍恒查 kill-switch；只影响账号自身、绝不触达客户。
        browse/react/online 无适配器 → executable=False（诚实，别假装）。
        """
        n = float(now if now is not None else time.time())
        kind = str(kind or "").strip().lower()
        out: Dict[str, Any] = {
            "platform": platform, "account_id": account_id, "kind": kind,
            "executable": kind in _EXECUTABLE_KINDS, "blocked": False,
            "confirmed": bool(confirm), "ok": False, "detail": "", "latency_ms": 0,
        }
        try:
            out["blocked"] = bool(await self._block_check(platform, account_id)) \
                if self._block_check else False
        except Exception:
            out["blocked"] = True  # 查不清 kill-switch 一律保守当拦
        if not out["executable"]:
            out["detail"] = "no_adapter"
            return out
        if out["blocked"]:
            out["detail"] = "blocked"
            return out
        if not confirm:
            out["detail"] = "preview_ok"  # 可执行且未拦，但不真跑
            return out
        t0 = time.monotonic()
        ok = False
        try:
            if kind == "read" and self._read_cb:
                ok = bool(await self._read_cb(platform, account_id))
            elif kind == "self_chat" and self._self_chat_cb:
                ok = bool(await self._self_chat_cb(platform, account_id))
        except Exception as ex:  # noqa: BLE001
            out["detail"] = "error:" + type(ex).__name__
            out["latency_ms"] = int((time.monotonic() - t0) * 1000)
            return out
        out["ok"] = ok
        out["detail"] = "ok" if ok else "action_failed"
        out["latency_ms"] = int((time.monotonic() - t0) * 1000)
        try:
            self._ledger.record_action(
                f"{platform}:{account_id}", kind, now=n, dry_run=False, ok=ok,
                detail="probe:" + out["detail"])
        except Exception:
            pass
        return out

    async def run_once(self, *, now: Optional[float] = None) -> int:
        """一次养护 tick。配置热闸：enabled=false 直接空转（零副作用）。返回本 tick 处理动作数。"""
        n = float(now if now is not None else time.time())
        self.last_tick_ts = n
        self.last_tick_executed = 0
        cfg = self._live_cfg() or {}
        if not cfg.get("enabled", False):
            self.last_tick_gated = True
            self.last_tick_planned = 0
            return 0
        self.last_tick_gated = False
        dry_run = bool(cfg.get("dry_run", True))
        try:
            max_tick = max(1, int(cfg.get("max_actions_per_tick", self._max_per_tick)))
        except Exception:
            max_tick = self._max_per_tick
        canary = set(str(x) for x in (cfg.get("canary_accounts") or []))

        try:
            accounts = self._accounts_provider() or []
        except Exception:
            logger.debug("nurture accounts_provider 异常", exc_info=True)
            return 0
        try:
            signals_map = self._signals_provider(accounts) or {}
        except Exception:
            logger.debug("nurture signals_provider 异常", exc_info=True)
            signals_map = {}
        ledger_snap = self._ledger.snapshot(now=n) if self._ledger else {}

        due = plan_due_actions(accounts, ledger_snap, signals_map, n, cfg)
        if not due:
            self.last_tick_planned = 0
            return 0

        processed = 0
        executed = 0
        for act in due:
            if processed >= max_tick:
                break
            processed += 1
            key = act["key"]
            plat = act["platform"]
            acct = act["account_id"]
            kind = act["kind"]
            # dry_run，或 go_live 但非金丝雀 → 影子记录（推进节奏，不碰账号）
            if dry_run or (key not in canary):
                self._ledger.record_action(
                    key, kind, now=n, dry_run=True, ok=True,
                    detail=("dry_run" if dry_run else "non_canary"))
                continue
            # go_live + 金丝雀：动作前查 kill-switch / 风险态
            try:
                blocked = bool(await self._block_check(plat, acct)) if self._block_check else False
            except Exception:
                blocked = True  # 查不清一律保守跳过
            if blocked:
                self._ledger.record_action(
                    key, kind, now=n, dry_run=True, ok=False, detail="blocked")
                continue
            if kind not in _EXECUTABLE_KINDS:
                # browse/react/online 无现成高层 API → 诚实记 no_adapter（不假装做了）
                self._ledger.record_action(
                    key, kind, now=n, dry_run=True, ok=False, detail="no_adapter")
                continue
            ok = False
            try:
                if kind == "read" and self._read_cb:
                    ok = bool(await self._read_cb(plat, acct))
                elif kind == "self_chat" and self._self_chat_cb:
                    ok = bool(await self._self_chat_cb(plat, acct))
            except Exception:
                logger.debug("nurture 动作执行异常 kind=%s %s:%s", kind, plat, acct, exc_info=True)
                ok = False
            self._ledger.record_action(
                key, kind, now=n, dry_run=False, ok=ok,
                detail=("ok" if ok else "action_failed"))
            if ok:
                executed += 1
        self.last_tick_planned = processed
        self.last_tick_executed = executed
        return processed


__all__ = ["NurtureEngine"]
