"""通话运行时单一装配入口 —— 把散落的「塞真实实例」收敛成一次 ``build_call_runtime``。

真机接线曾散在 6 处（transport / brain / hooks / providers / lookups / 回调注册）；本模块把它们
收敛成一个装配器 + 一组 pytgcalls on_update 处理器该调的回调，让真机接线从「散落塞实例」变成
「调一个装配器 + 注册回调」。装配逻辑本身注入型（传 fake stores 即可离线测），真正碰 pytgcalls
的只有薄回调注册（在真机 wiring 处）。

产出 ``CallRuntime``：
  - ``bridge``               —— 配好 hooks/providers/stats 的 ``TelegramCallBridge``；
  - ``on_incoming(chat_id, account_id)``  —— 来电处理器（组装 ctx → 决策 → ACCEPT 则起会话）；
  - ``on_inbound_frame(chat_id, pcm)``    —— 进向音频路由；
  - ``on_user_speech_start(chat_id)``     —— VAD「对方开口」→ 驱动 backchannel；
  - ``readiness()``          —— 开闸前就绪度体检（复用 evaluate_call_readiness）；
  - ``dry_run_report()``     —— 接线完整性自检（不接真通话，投产前一键确认）。
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from src.voicecall.bridge import (
    CallContext,
    CallHooks,
    PcmProvider,
    TelegramCallBridge,
)
from src.voicecall.core import CallAction, CallsConfig
from src.voicecall.wiring import assemble_call_context, call_account_key, call_memory_key

logger = logging.getLogger(__name__)


@dataclass
class CallRuntimeDeps:
    """真机注入的所有依赖（测试传 fake）。缺省 None → 对应能力静默关（安全降级）。"""
    platform: str = "telegram"
    # 上下文 lookup（喂 assemble_call_context）
    conversation_lookup: Optional[Callable[[str, str], Optional[dict]]] = None
    usage_lookup: Optional[Callable[[str], Tuple[int, float]]] = None
    account_light_lookup: Optional[Callable[[str], str]] = None
    kill_switch_lookup: Optional[Callable[[str, str], bool]] = None
    memory_lookup: Optional[Callable[[str], str]] = None
    hour_fn: Optional[Callable[[], int]] = None            # 当前小时（安静时段）
    host_warm_fn: Optional[Callable[[], bool]] = None      # 主机是否热（决策用）
    # 收尾副作用
    memory_add: Optional[Callable[[str, str], Any]] = None
    usage_record: Optional[Callable[[str, float], Any]] = None
    follow_up: Optional[Callable[[Any, Any], Awaitable[None]]] = None
    compensate: Optional[Callable[[Any, str], Awaitable[None]]] = None
    on_human_escalation: Optional[Callable[[Any, str], Awaitable[None]]] = None
    # 拟人音频供给（预渲染克隆声 PCM）
    opener_provider: Optional[PcmProvider] = None
    filler_provider: Optional[PcmProvider] = None
    backchannel_provider: Optional[PcmProvider] = None
    # 就绪度体检辅助
    auto_ai_count_fn: Optional[Callable[[], int]] = None


class CallRuntime:
    """一次装配的通话运行时。持有 bridge + 供 pytgcalls 回调调用的处理器。"""

    def __init__(self, cfg: CallsConfig, bridge: TelegramCallBridge,
                 deps: CallRuntimeDeps, full_config: Optional[Dict[str, Any]]) -> None:
        self.cfg = cfg
        self.bridge = bridge
        self.deps = deps
        self._full_config = full_config
        self._tasks: set = set()

    def _build_ctx(self, chat_id: int, account_id: str) -> CallContext:
        d = self.deps
        return assemble_call_context(
            chat_id, account_id, platform=d.platform,
            conversation_lookup=d.conversation_lookup,
            usage_lookup=d.usage_lookup,
            account_light_lookup=d.account_light_lookup,
            kill_switch_lookup=d.kill_switch_lookup,
            memory_lookup=d.memory_lookup,
            host_warm=(d.host_warm_fn() if d.host_warm_fn else True),
            hour=(d.hour_fn() if d.hour_fn else 12),
            concurrent_active=self.bridge.active_calls())

    async def on_incoming(self, chat_id: int, account_id: str) -> str:
        """来电处理器：组装 ctx → 决策 → ACCEPT 则后台起会话。返回决策 action（观测）。

        run_session 以 fire-and-forget 后台任务跑（不阻塞 pytgcalls 事件循环）；任务集持有引用
        防 GC，完成自动摘除。
        """
        ctx = self._build_ctx(chat_id, account_id)
        decision = await self.bridge.handle_incoming(ctx)
        if decision.action == CallAction.ACCEPT:
            task = asyncio.ensure_future(self.bridge.run_session(ctx))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        return decision.action.value

    async def on_inbound_frame(self, chat_id: int, pcm_frame_48k: bytes,
                               *, channels: int = 1) -> None:
        await self.bridge.on_inbound_frame(chat_id, pcm_frame_48k, channels=channels)

    def on_user_speech_start(self, chat_id: int) -> None:
        self.bridge.on_user_speech_start(chat_id)

    def readiness(self, *, host_probe: Optional[Dict[str, Any]] = None,
                  ref_summary: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """开闸前就绪度体检（复用 evaluate_call_readiness，与 route/看门狗同口径）。"""
        from src.voicecall.health import evaluate_call_readiness, probe_call_host
        hp = host_probe if host_probe is not None else probe_call_host(self._full_config)
        auto_ai = self.deps.auto_ai_count_fn() if self.deps.auto_ai_count_fn else None
        return evaluate_call_readiness(
            self._full_config, host_probe=hp, ref_summary=ref_summary,
            auto_ai_conversations=auto_ai)

    def dry_run_report(self, *, host_probe: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """接线完整性自检（不接真通话）：哪些依赖已接、就绪度如何。投产前一键确认。

        产出 ``{enabled, wired{...bool}, missing[], readiness{...}}``——missing 列出「开了但没接」
        的关键依赖（如真发但没接记忆库/用量存储 → 环没闭）。
        """
        d = self.deps
        wired = {
            "transport": self.bridge.transport is not None,
            "brain": self.bridge.brain is not None,
            "conversation_lookup": d.conversation_lookup is not None,
            "usage_lookup": d.usage_lookup is not None,
            "usage_record": d.usage_record is not None,
            "account_light_lookup": d.account_light_lookup is not None,
            "kill_switch_lookup": d.kill_switch_lookup is not None,
            "memory_lookup": d.memory_lookup is not None,
            "memory_add": d.memory_add is not None,
            "follow_up": d.follow_up is not None,
            "compensate": d.compensate is not None,
            "opener": d.opener_provider is not None,
            "filler": d.filler_provider is not None,
            "backchannel": d.backchannel_provider is not None,
        }
        # 「开了必须接」的关键依赖（缺 → 环没闭，功能残废）：
        missing: List[str] = []
        if self.cfg.enabled:
            if not wired["conversation_lookup"]:
                missing.append("conversation_lookup（无会话画像→所有来电判陌生人静默拒接）")
            if not wired["usage_lookup"] or not wired["usage_record"]:
                missing.append("usage_lookup/usage_record（预算闸读不到/写不进→日预算失效）")
            if not wired["memory_lookup"]:
                missing.append("memory_lookup（通话大脑拿不到长期记忆→不「记得你」）")
        return {
            "enabled": self.cfg.enabled,
            "brain": self.cfg.brain,
            "transport": self.cfg.transport,
            "transport_verified": self.cfg.transport_verified,
            "wired": wired,
            "missing": missing,
            "readiness": self.readiness(host_probe=host_probe),
        }


def build_call_runtime(
    full_config: Optional[Dict[str, Any]],
    *,
    transport: Any,
    brain: Any,
    deps: Optional[CallRuntimeDeps] = None,
    stats: Any = None,
) -> CallRuntime:
    """装配通话运行时（单一入口）。``transport``/``brain`` 为真实适配器（或测试 fake）。

    hooks（compensate/human_escalation/wrapup）与 providers 从 deps 组装；wrapup 自动串起
    记忆落库 + 用量记账 + follow-up（复用 make_wrapup_hook 闭合预算环）。
    """
    from src.voicecall.wrapup import make_wrapup_hook
    cfg = CallsConfig.from_config(full_config)
    deps = deps or CallRuntimeDeps()
    plat = deps.platform

    wrapup = make_wrapup_hook(
        memory_key_fn=lambda ctx: call_memory_key(plat, str(ctx.chat_id)),
        memory_add=deps.memory_add,
        follow_up=deps.follow_up,
        usage_record=deps.usage_record,
        account_key_fn=lambda ctx: call_account_key(plat, ctx.account_id))
    hooks = CallHooks(
        compensate=deps.compensate,
        on_human_escalation=deps.on_human_escalation,
        on_wrapup=wrapup)
    bridge = TelegramCallBridge(
        cfg, transport, brain, hooks=hooks, stats=stats,
        opener_provider=deps.opener_provider,
        filler_provider=deps.filler_provider,
        backchannel_provider=deps.backchannel_provider)
    return CallRuntime(cfg, bridge, deps, full_config)


__all__ = ["CallRuntime", "CallRuntimeDeps", "build_call_runtime"]
