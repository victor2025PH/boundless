"""编排器发送侧统一反封号守卫（Stage M：让旁路发送入口也受护栏约束）。

A 线 mixin（``sender.py``）与三端 RPA（``rpa_send_guard``）已各自接 G1 Kill-Switch；
但**编排器受管 worker** 的发送（``AccountOrchestrator.send/send_media`` → 协议号 B 线 /
WhatsApp / 官方 API LINE·Messenger·WhatsApp Cloud）此前**直发裸 client**，绕过了
Kill-Switch + 反封号闸门——主动问候 / 唤醒 / 关怀 / 接管 等所有经编排器的外发都从这里走。

本守卫是编排器发送入口的薄封装：发送前查 ``global/platform/account`` 三级停发 +（开启时）
金丝雀放量白名单 + 反封号闸门（预热爬坡 / 健康红灯 / 配额）。命中即不发，让编排器返回
``{delivered: False, blocked: ...}``（调用方据此不记冷却、择机重试——冻结是暂态）。

设计：
- **Kill-Switch 恒查**（紧急急停，绝不可被任何发送路径绕过；模块级只读单例、零 DB、永不抛）。
- **金丝雀放量按 ``ops.canary.enabled`` 才查**（默认关 → 行为零变更）：启用时不在 cohort
  的账号一律 hold。此前 canary 只接在 B 线 ``protocol_autoreply`` 与官方 API/webhook 入站链，
  **编排器受管发送路径（L2 autosend deliver / 主动问候 / 唤醒 / 关怀经 orchestrator.send）
  绕过了它** → 放量爆炸半径控制对最主要的自动外发失效。此处补齐，使 canary 与 Kill-Switch/
  send-gate 一样成为编排器发送入口的统一护栏（一处接，覆盖 send/send_media + 适配器回落全路径）。
- **反封号闸门按 ``companion_send_gate.enabled`` 才查**（默认关 → 行为零变更）；
  复用 A/B 线同一份 ``build_account_signals``（registry 天龄/代理/封禁 + limiter 今日量），口径统一。
- **绝不抛异常**：守卫自身故障一律视为放行（broken guard 不得反过来把全部发送卡死）。
- 查序＝急停 → 金丝雀 → 反封号（最硬的先判；canary 是「放量圈」，send-gate 是「圈内节奏」）。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Tuple

# 2026-08-12 修既有隐患：notify_send_blocked 的 except 分支一直在用 logger，
# 但模块从未定义——告警链自身抛异常时，异常处理器 NameError 会穿透回
# send_blocked 的外层 except，把「拦截」判定静默翻转成「放行」。
logger = logging.getLogger(__name__)

# ── 拦截计数（P1 2026-08-12，ops「发送额度」观测地基）────────────────────────
# 进程级轻量计数：真实发送尝试被拦才计（notify=True 的判定路径；预判/横幅轮询
# 传 notify=False 不计——否则 45s 轮询会把计数刷成噪音）。distinct 键有上限防
# 撑爆（与 frontend_error_stats 同防御）。消费方：send-gate 状态接口 / ops 卡。
_BLOCK_MAX_KEYS = 200
_block_counts: dict = {}
_block_total: int = 0


def _record_block(platform: str, account_id: str, reason: str) -> None:
    global _block_total
    try:
        _block_total += 1
        key = f"{platform}:{account_id}|{reason}"
        if key in _block_counts or len(_block_counts) < _BLOCK_MAX_KEYS:
            _block_counts[key] = int(_block_counts.get(key, 0)) + 1
    except Exception:
        pass


def block_stats_snapshot() -> dict:
    """拦截计数快照（进程口径，重启清零；持久口径看 app.log WARNING 行）。"""
    return {"total": int(_block_total), "by_key": dict(_block_counts)}


def send_blocked(
    platform: str,
    account_id: str,
    *,
    config: Optional[Dict[str, Any]] = None,
    registry: Any = None,
    chat_key: str = "",
    notify: bool = True,
    origin: str = "auto",
) -> Tuple[bool, str]:
    """编排器发送前统一护栏。返回 ``(blocked, reason)``；任何异常 → ``(False, "")`` 放行。

    reason 形如 ``kill_switch:<scope>`` / ``canary_hold`` / ``send_gate:<reason>``，
    供日志/审计区分拦因。``chat_key``（可选）：send_gate 白名单豁免用——
    Kill-Switch/授权/金丝雀不豁免（那些是急停与放量控制，白名单只对限额有效）。

    ``notify``（2026-08-12）：预判/看板等**读路径**复用本判定时传 ``False``——
    同一份真相函数，但「看一眼状态」不该发运营告警（横幅 45s 轮询会把防抖窗
    口持续占住，真发送被拦时反而轮不到告警）。发送路径保持默认 True。

    ``origin``（P1 2026-08-12 人工预留额度）：``manual``＝坐席人工路径，额度
    比较用完整 recommended_cap；缺省 ``auto``＝自动链，比较 ``auto_cap =
    cap - reserve_for_manual``。**只影响反封号闸门的额度道**；Kill-Switch/
    金丝雀/授权对两道一视同仁（急停就是急停，不分人机）。
    """
    p = str(platform or "")
    a = str(account_id or "default")
    # 0) License 到期硬阻断（Sprint2）：enforce 开且授权失效(只读) → 阻断一切外发。
    #    默认 licensing.enforce=false → read_only 恒 False → 零破坏。与 Kill-Switch 同层、fail-open。
    try:
        from src.licensing.gate import is_outbound_blocked
        from src.licensing.license_manager import get_license_manager
        if is_outbound_blocked(get_license_manager().status()):
            if notify:
                _record_block(p, a, "license_readonly")
            return True, "license_readonly"
    except Exception:
        pass
    # 1) Kill-Switch（恒查，紧急急停）
    try:
        from src.ops.kill_switch import is_blocked as _ks_blocked
        on, scope, _reason = _ks_blocked(p, a)
        if on:
            if notify:
                _record_block(p, a, f"kill_switch:{scope or 'global'}")
            return True, f"kill_switch:{scope or 'global'}"
    except Exception:
        pass
    # 2) 金丝雀放量（仅 ops.canary.enabled 时；默认关 → 零破坏）：不在 cohort → hold
    try:
        from src.ops.canary import is_held as _canary_held
        held, _c_reason = _canary_held(p, a, config)
        if held:
            if notify:
                _record_block(p, a, _c_reason or "canary_hold")
            return True, _c_reason or "canary_hold"
    except Exception:
        pass
    # 3) 反封号闸门（仅 enabled 时；默认关 → 零破坏；exempt_peers 白名单豁免）
    try:
        from src.skills.companion_send_gate import (
            evaluate, gate_enabled, peer_exempt,
        )
        if gate_enabled(config) and not peer_exempt(config, chat_key):
            from src.skills.account_signals import build_account_signals
            limiter = None
            try:
                from src.integrations.protocol_autoreply_limits import (
                    get_autoreply_limiter,
                )
                limiter = get_autoreply_limiter(config or {})
            except Exception:
                limiter = None
            sig = build_account_signals(p, a, registry=registry, limiter=limiter)
            dec = evaluate(sig, config, origin=origin)
            if not dec.get("allowed", True):
                _reason = f"send_gate:{dec.get('reason') or 'blocked'}"
                if notify:
                    _record_block(p, a, f"{_reason}|{origin}")
                    notify_send_blocked(p, a, _reason)
                return True, _reason
    except Exception:
        pass
    return False, ""


def notify_send_blocked(platform: str, account_id: str, reason: str) -> None:
    """发送被限流/闸门拦截 → 运营可见告警（2026-07-22，用户指令：限制必须有提示）。

    真机事故：warmup_cap 静默拦截全部回复，运营以为系统坏了排查半天。
    双通道（best-effort、防抖、绝不抛）：
    - EventBus ``autoreply_alert``（WebhookNotifier → 钉钉/飞书/企微 + 工作台事件流）
    - ops_alert 集团 TG 中继（EVENT_INGEST_KEY 已配置的部署直达老板手机）
    Kill-Switch/授权类拦截不在此报（那是运营自己按的急停，不算意外）。
    """
    try:
        from src.integrations.protocol_autoreply import publish_alert
        publish_alert("send_gate_blocked",
                      {"platform": platform, "account_id": account_id},
                      f"发送被限流闸门拦截({reason})——回复未送出，请检查发送配额配置")
    except Exception:
        logger.debug("[send_guard] event_bus 告警失败", exc_info=True)
    try:
        from src.ops.ops_alert import notify
        notify(
            "send_gate_blocked",
            f"⛔ {platform}:{account_id} 自动回复被发送闸门拦截（{reason}）。"
            "客户消息不会得到回复；如非预期请调大/关闭 companion_send_gate 限额。",
            account_id=f"{platform}:{account_id}", reason=reason)
    except Exception:
        logger.debug("[send_guard] ops_alert 告警失败", exc_info=True)


__all__ = ["block_stats_snapshot", "notify_send_blocked", "send_blocked"]
