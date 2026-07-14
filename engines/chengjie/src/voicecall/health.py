"""通话主机健康探测 + 能力就绪度体检（纯决策 + 薄探针，复用 realtime_voice 主机）。

原生通话（brain=s2s）的大脑=`realtime_voice` 那台 MiniCPM-o 主机（176:7860），故健康探测
**复用** `RealtimeVoiceClient.model_status()`，不另起一套探针（单一事实源、防口径漂移）。

  - ``call_health_probe_target``  —— 纯决策：该不该探、探哪个 base_url（telegram_calls 关 /
    非 s2s → None，天然静默）；
  - ``probe_call_host``           —— 薄探针（60s TTL 缓存 + 超时），返回 {reachable, model_loaded}；
  - ``evaluate_call_readiness``   —— 纯函数：给定「配置 + 主机探测 + 参考音摘要 + 传输就绪」
    产出 {ready, blockers[], warnings[]}——「开关打开前先看差哪些」的体检单一入口。
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Tuple

from src.voicecall.core import CallsConfig

# 进程级健康缓存：base_url -> (expires_monotonic, result)
_PROBE_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}
_PROBE_TTL = 60.0


def call_health_probe_target(full_config: Optional[Dict[str, Any]]) -> Optional[str]:
    """该不该探通话主机、探哪个 base_url（纯决策）。

    - telegram_calls 未启用 → None（天然静默，不误报）；
    - brain=s2s → 复用 realtime_voice.base_url（同一 MiniCPM-o 主机）；
    - brain=cascade → 当前无独立通话主机（嘴走 CosyVoice，另有其健康灯）→ None（P1 未落地）。
    """
    cfg = CallsConfig.from_config(full_config)
    if not cfg.enabled:
        return None
    if cfg.brain != "s2s":
        return None
    rv = {}
    if isinstance(full_config, dict):
        rv = full_config.get("realtime_voice") or {}
    base = str((rv or {}).get("base_url") or "").strip().rstrip("/")
    return base or None


def probe_call_host(full_config: Optional[Dict[str, Any]], *,
                    use_cache: bool = True) -> Optional[Dict[str, Any]]:
    """探通话主机健康。返回 ``{reachable, model_loaded, url, error}``；不该探 → None。

    复用 ``RealtimeVoiceClient.model_status()``（读 /health 的 model_loaded/loading/vram）。
    60s TTL 缓存（tick 间隔 > TTL → 每 tick 新鲜）。任何异常 → reachable=False（不抛）。
    """
    base = call_health_probe_target(full_config)
    if not base:
        return None
    now = time.monotonic()
    if use_cache:
        hit = _PROBE_CACHE.get(base)
        if hit and hit[0] > now:
            return dict(hit[1])
    result: Dict[str, Any] = {"reachable": False, "model_loaded": False,
                              "url": base, "error": ""}
    try:
        from src.ai.realtime_voice_client import RealtimeVoiceClient
        from src.ai.realtime_voice import RealtimeVoiceConfig
        client = RealtimeVoiceClient(RealtimeVoiceConfig.from_config(full_config))
        status = client.model_status(timeout=3.0)
        if isinstance(status, dict) and not status.get("error"):
            result["reachable"] = True
            result["model_loaded"] = bool(status.get("model_loaded", True))
        else:
            result["error"] = str((status or {}).get("error") or "unreachable")[:160]
    except Exception as exc:  # noqa: BLE001
        result["error"] = str(exc)[:160]
    _PROBE_CACHE[base] = (now + _PROBE_TTL, dict(result))
    return result


def reset_probe_cache() -> None:
    _PROBE_CACHE.clear()


def evaluate_call_readiness(
    full_config: Optional[Dict[str, Any]],
    *,
    host_probe: Optional[Dict[str, Any]] = None,
    ref_summary: Optional[Dict[str, Any]] = None,
    transport_ready: Optional[bool] = None,
    auto_ai_conversations: Optional[int] = None,
) -> Dict[str, Any]:
    """原生通话开闸前的就绪度体检（纯函数）。

    产出 ``{enabled, ready, blockers[], warnings[]}``——blocker=开了也不工作（硬前置），
    warning=能工作但体验/安全打折。刻意**只读传入的探测结果**（不自己发探针），保持纯粹可测。

    检查面：
      - blocker：主机不可达 / 模型未载入（s2s）；传输层未就绪（显式传 False 时）；
        真发式自动接听开了却无 auto_ai 会话（不会接任何人）；
      - warning：无人设参考音（降级内置音色，非"她的声音"）；参考音体检红灯；
        cascade 脑但当前硬件嘴达不到实时（实测 TTFB 48-63s，见 docs）。
    """
    cfg = CallsConfig.from_config(full_config)
    blockers: List[str] = []
    warnings: List[str] = []
    if not cfg.enabled:
        return {"enabled": False, "ready": False, "blockers": [], "warnings": []}

    if cfg.brain == "s2s":
        hp = host_probe if host_probe is not None else {}
        if host_probe is not None:
            if not hp.get("reachable"):
                blockers.append("语音主机不可达（realtime_voice.base_url）→ 无法接听通话")
            elif not hp.get("model_loaded"):
                blockers.append("语音主机在线但模型未载入 → 接通会失败，先「启动引擎」载入")
    elif cfg.brain == "cascade":
        warnings.append("cascade 脑当前硬件「嘴」（CosyVoice 克隆流式）实测 TTFB 48-63s，"
                        "达不到实时；需专职流式克隆嘴 GPU 才建议用 cascade（见 docs）")

    # 传输层就绪：显式传入优先；否则读 config 的 transport_verified（运营跑过 PoC 三闸门才置 true）。
    tready = transport_ready if transport_ready is not None else cfg.transport_verified
    if not tready:
        blockers.append("传输层未验证（ntgcalls 进向音频/tg2sip 网关未跑通 tg_call_poc 三闸门）"
                        "→ 收不到来电；验证后置 telegram_calls.transport_verified: true")

    if cfg.require_auto_ai and auto_ai_conversations is not None and auto_ai_conversations <= 0:
        blockers.append("要求 auto_ai 会话才自动接，但当前无 auto_ai 会话 → 不会接任何人")

    refs = ref_summary or {}
    if int(refs.get("persona_count") or 0) > 0:
        if int(refs.get("with_reference") or 0) == 0:
            warnings.append("无人设参考音 → 通话降级内置音色（非「她的声音」），试拨页上传真人声")
        elif str(refs.get("worst_grade") or "") == "red":
            iss = (refs.get("sample_issues") or ["质量不佳"])[0]
            warnings.append(f"参考音体检红灯（{iss}）→ 重录后再做克隆通话")

    return {
        "enabled": True,
        "ready": not blockers,
        "blockers": blockers,
        "warnings": warnings,
    }


__all__ = [
    "call_health_probe_target",
    "probe_call_host",
    "reset_probe_cache",
    "evaluate_call_readiness",
]
