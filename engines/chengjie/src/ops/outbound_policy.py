"""出站策略单一开关 ``outbound.unlimited_mode`` + 拦截统一计数（2026-09-04）。

背景：全仓「限制发送」散在 30+ 处（skill 冷却 / S5 概率静默 / 入站限速 /
主动触达 8 层叠加降频 / care 联系人预算 / handoff 日配额 / 自拍与生活照日
预算 / RPA 日上限 …），每处一套开关一套「0 是不是不限」语义。运营要「有
token 就全自动发、不受业务频控」时得改十几个键、还漏一半。本模块把它们收成
**一个开关 + 一个判定函数 + 一个计数出口**：

- ``is_unlimited()``＝``outbound.unlimited_mode`` 实时值（跟随 overlay 热重载）。
  **只短路「业务频控」层**（防打扰 / 成本节流 / 演示档位那类）；
  **绝不短路「安全刹车」层**（Kill-Switch / 金丝雀 / 风控 FloodWait / 账号
  冷启动预热 / 对方机器人判定 / 危机情绪闸 / 会话不健康快败 / 授权与 token
  余额）——那些是账号与账单的保险丝，不归本开关管。
- ``business_cap(cap)``＝业务日上限的统一读法：unlimited → 0（=不限）；
  同时把负数归 0，消费方只认「<=0 就是不限」一种语义。
- ``record_block(layer, reason)``＝任何一处拦截/降频都记一笔（P5 观测），
  ``blocked_snapshot()``/``dump_prom()`` 出「今天到底被谁拦了多少」，
  不再靠翻日志猜「AI 为什么不回」。

接线范式与 ``src/compliance/runtime.py`` 同款：装配层注册 config provider
（指向 ConfigManager 实时合并配置），消费点惰性读；provider 未注册（单测 /
CLI / legacy 装配）＝开关恒关，行为与本模块不存在时逐字节一致。绝不抛。
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Dict, Mapping, Optional

logger = logging.getLogger(__name__)

# ── 层级常量（record_block 的 layer 维度；限死枚举防拼写漂移）────────────
LAYER_BUSINESS = "business"   # 业务频控：unlimited_mode 会短路
LAYER_SAFETY = "safety"       # 安全刹车：任何模式都拦
# 额度/余额：有 token 才发的那条线。值刻意用 ``tok``——本快照会随坐席可读的
# quota-state 端点下发，该端点有「响应体不含 token 子串」的防泄漏门禁
# （quota_state 的 tok/tok_out 同一缘由）；``record_block("token", …)`` 仍接受，归一到 tok。
LAYER_TOKEN = "tok"
_LAYERS = (LAYER_BUSINESS, LAYER_SAFETY, LAYER_TOKEN)
_LAYER_ALIASES = {"token": LAYER_TOKEN, "quota": LAYER_TOKEN}

# 「不限」哨兵：消费方需要一个**数值**上限做比较/展示时用（account_limiter 的
# effective_cap、send_gate 的 target_cap 等）。远大于任何真实日发量即可。
UNLIMITED_CAP = 10 ** 9

_PROVIDER: Optional[Callable[[], Optional[Mapping[str, Any]]]] = None


def set_config_provider(fn: Callable[[], Optional[Mapping[str, Any]]]) -> None:
    """装配层注册实时配置来源（幂等，后注册覆盖先注册）。"""
    global _PROVIDER
    _PROVIDER = fn


def runtime_config() -> Dict[str, Any]:
    """当前生效配置（provider 未注册/异常 → 空 dict）。"""
    try:
        if _PROVIDER is None:
            return {}
        cfg = _PROVIDER()
        return dict(cfg) if isinstance(cfg, Mapping) else {}
    except Exception:
        logger.debug("[outbound_policy] config provider 异常（按空配置处理）",
                     exc_info=True)
        return {}


def _outbound_section(config: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    try:
        sec = (config or {}).get("outbound")
        return dict(sec) if isinstance(sec, Mapping) else {}
    except Exception:
        return {}


def is_unlimited(config: Optional[Mapping[str, Any]] = None) -> bool:
    """``outbound.unlimited_mode`` 是否开启。

    ``config`` 显式传入（纯函数路径 / 测试）优先；缺省读 provider。任何异常
    → False（开关读取面故障不得放大成「所有频控失效」）。
    """
    try:
        cfg = config if config is not None else runtime_config()
        return bool(_outbound_section(cfg).get("unlimited_mode", False))
    except Exception:
        return False


def business_cap(cap: Any, config: Optional[Mapping[str, Any]] = None) -> int:
    """业务日上限统一读法：返回 ``<=0`` 即「不限」。

    - unlimited_mode → 0；
    - 非法/None/负数 → 0（「0=不限」是全仓唯一认可的语义，别再各处
      ``max(1, cap)`` / ``or 6`` 把 0 悄悄改成默认值）。
    """
    if is_unlimited(config):
        return 0
    try:
        v = int(cap if cap is not None else 0)
    except (TypeError, ValueError):
        return 0
    return v if v > 0 else 0


def cap_allows(cap: Any, used: int, config: Optional[Mapping[str, Any]] = None) -> bool:
    """``business_cap`` 的便捷判定：不限 → True；否则 ``used < cap``。"""
    eff = business_cap(cap, config)
    if eff <= 0:
        return True
    try:
        return int(used) < eff
    except (TypeError, ValueError):
        return True


def proactive_live_overrides(
    live: Optional[Dict[str, Any]],
    config: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """把 unlimited_mode 叠加进主动触达的 live pacing 覆盖字典。

    非 unlimited → 原样返回。unlimited → 关掉**叠加降频层**：自适应节奏 /
    未回退避 / 回复率反哺 / 已读分流 / 未回消息墙 / 每 tick 名额
    （``max_per_tick=0`` 经 ``plan_proactive_sends`` 的「0=不限」语义放开）。
    **刻意保留**：``min_silent_hours`` / ``cooldown_hours``（这是节奏不是
    限制——同一个人 24h 内不重复主动是对话常识）、安静时段（对方的凌晨）、
    dry_run（运营显式演练档）。
    """
    out = dict(live or {})
    if not is_unlimited(config):
        return out
    out.update({
        "max_per_tick": 0,
        "pacing_cfg": None,
        "backoff_cfg": None,
        "response_pacing_cfg": None,
        "read_aware_cfg": None,
        "wall_cfg": {"enabled": False, "max_trailing": 0},
        "unlimited": True,
    })
    return out


# ── P5 拦截统一计数（进程级，风格对齐 frontend_error_stats）───────────────
_LOCK = threading.Lock()
_MAX_KEYS = 200
_STATS: Dict[str, Any] = {
    "total": 0,
    "by_layer": {},      # layer -> n
    "by_reason": {},     # "layer:reason" -> n
    "by_platform": {},   # platform -> n
    "unlimited_bypass": 0,   # unlimited_mode 短路了几次本该拦的业务频控
}


def _bump(bucket: Dict[str, int], key: str) -> None:
    if key in bucket or len(bucket) < _MAX_KEYS:
        bucket[key] = int(bucket.get(key, 0)) + 1
    else:
        bucket["_other"] = int(bucket.get("_other", 0)) + 1


def record_block(layer: str, reason: str, *, platform: str = "") -> None:
    """记一笔「本该发/想发但被拦或降频」。绝不抛。

    ``layer`` ∈ {business, safety, token}（未知值归 business）；``reason`` 为
    短标识（如 ``skill_cooldown`` / ``kill_switch`` / ``token_exhausted``）。
    """
    try:
        lyr = str(layer or "").strip().lower()
        lyr = _LAYER_ALIASES.get(lyr, lyr)
        if lyr not in _LAYERS:
            lyr = LAYER_BUSINESS
        rsn = str(reason or "unknown").strip()[:64] or "unknown"
        pf = str(platform or "").strip().lower()[:32]
        with _LOCK:
            _STATS["total"] = int(_STATS["total"]) + 1
            _bump(_STATS["by_layer"], lyr)
            _bump(_STATS["by_reason"], f"{lyr}:{rsn}")
            if pf:
                _bump(_STATS["by_platform"], pf)
    except Exception:
        pass


def record_unlimited_bypass(reason: str = "") -> None:
    """unlimited_mode 放行了一次本会被业务频控拦下的发送（观测放量幅度）。"""
    try:
        with _LOCK:
            _STATS["unlimited_bypass"] = int(_STATS["unlimited_bypass"]) + 1
            if reason:
                _bump(_STATS["by_reason"], f"bypass:{str(reason)[:64]}")
    except Exception:
        pass


def blocked_snapshot() -> Dict[str, Any]:
    """只读快照（供 /api/workspace/metrics 与 ops 卡）。"""
    with _LOCK:
        return {
            "unlimited_mode": is_unlimited(),
            "total": int(_STATS["total"]),
            "unlimited_bypass": int(_STATS["unlimited_bypass"]),
            "by_layer": dict(_STATS["by_layer"]),
            "by_reason": dict(_STATS["by_reason"]),
            "by_platform": dict(_STATS["by_platform"]),
        }


def blocked_brief(top: int = 5) -> Dict[str, Any]:
    """给「AI 为什么不干活」类坐席可读面的精简视图（quota_state.outbound）。

    只出 unlimited 开关、总拦截数、按层分布、Top-N 原因；不含平台/密钥类字段。
    """
    snap = blocked_snapshot()
    reasons = [
        (k, int(n)) for k, n in snap["by_reason"].items()
        if not k.startswith("bypass:")
    ]
    reasons.sort(key=lambda kv: (-kv[1], kv[0]))
    return {
        "unlimited": bool(snap["unlimited_mode"]),
        "blocked_total": int(snap["total"]),
        "bypass": int(snap["unlimited_bypass"]),
        "by_layer": dict(snap["by_layer"]),
        "top_reasons": [{"reason": k, "n": n} for k, n in reasons[: max(0, int(top))]],
    }


def _prom_label(v: str) -> str:
    return str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def dump_prom() -> str:
    """Prometheus 文本：``outbound_blocked_total{layer,reason}`` +
    ``outbound_unlimited_mode`` + ``outbound_unlimited_bypass_total``。"""
    snap = blocked_snapshot()
    lines = [
        "# TYPE outbound_unlimited_mode gauge",
        f"outbound_unlimited_mode {1 if snap['unlimited_mode'] else 0}",
        "# TYPE outbound_unlimited_bypass_total counter",
        f"outbound_unlimited_bypass_total {snap['unlimited_bypass']}",
        "# TYPE outbound_blocked_total counter",
    ]
    for key, n in sorted(snap["by_reason"].items()):
        if key.startswith("bypass:"):
            continue
        lyr, _, rsn = key.partition(":")
        lines.append(
            'outbound_blocked_total{layer="%s",reason="%s"} %d'
            % (_prom_label(lyr), _prom_label(rsn), int(n)))
    lines.append("# TYPE outbound_blocked_by_platform_total counter")
    for pf, n in sorted(snap["by_platform"].items()):
        lines.append(
            'outbound_blocked_by_platform_total{platform="%s"} %d'
            % (_prom_label(pf), int(n)))
    return "\n".join(lines) + "\n"


def reset_for_tests() -> None:
    """测试隔离：清 provider + 计数。"""
    global _PROVIDER
    _PROVIDER = None
    with _LOCK:
        _STATS["total"] = 0
        _STATS["unlimited_bypass"] = 0
        _STATS["by_layer"].clear()
        _STATS["by_reason"].clear()
        _STATS["by_platform"].clear()


__all__ = [
    "LAYER_BUSINESS", "LAYER_SAFETY", "LAYER_TOKEN", "UNLIMITED_CAP",
    "set_config_provider", "runtime_config",
    "is_unlimited", "business_cap", "cap_allows", "proactive_live_overrides",
    "record_block", "record_unlimited_bypass", "blocked_snapshot", "blocked_brief",
    "dump_prom",
    "reset_for_tests",
]
