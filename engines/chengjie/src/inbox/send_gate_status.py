"""发送护栏「预判快照」——横幅/预检与真实拦截共用同一份判定（2026-08-12）。

背景（当日实锤）：坐席点「填入并发送」→ 编排器被 ``send_gate:warmup_cap``
拦下 → 路由把 ``{delivered:false, blocked}`` 包进 ``ok:true`` 返回 → 前端当
成功处理，乐观气泡静默消失。三层反馈（点击级 / 广播 toast / 事前预判）全部
失效，坐席以为产品坏了。

本模块给「事前预判 + 拦截应答富化」提供单一取数口：

- 判定走 ``send_guard.send_blocked(notify=False)`` —— 与编排器发送路径**同一个
  函数**（预判必须与护栏行为完全一致，绝不另算一套；notify=False 使读路径
  不占告警防抖窗）。
- 额度数字走 ``build_account_signals`` + ``companion_send_gate.evaluate`` ——
  与闸门同源（used/cap 就是闸门比较的那两个数）。
- ``frees_at`` 走 ``AutoReplyLimiter.quota_frees_at``：日额度是**滚动 24h 窗**
  （不是零点重置），给出首个空位的预计释放时刻，横幅不再撒「明天恢复」的谎。

全程 best-effort：任何一步异常都缩为「无该段信息」，绝不阻塞发送/轮询。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# 额度拦截 reason 双值（P3 2026-08-13 更名 warmup_cap→daily_cap）：新代码发
# daily_cap；历史审计行/旧进程仍有 warmup_cap——分支判定永远两个都认。
_QUOTA_REASONS = ("daily_cap", "warmup_cap")


def send_gate_snapshot(
    platform: str,
    account_id: str,
    chat_key: str = "",
    *,
    config: Optional[Dict[str, Any]] = None,
    registry: Any = None,
    now: Optional[float] = None,
    origin: str = "manual",
) -> Optional[Dict[str, Any]]:
    """返回 ``{blocked, reason, quota?, frees_at?}``；无可展示信息 → None。

    - ``blocked``/``reason``：与编排器发送护栏同一判定（含 Kill-Switch /
      金丝雀 / 授权 / 反封号闸门 + exempt_peers 白名单豁免）。``origin`` 缺省
      ``manual``——本快照的消费方（发送预检 / composer 横幅 / 409 富化）都是
      人工视角；人工预留额度（reserve_for_manual）下人工道用满 cap。
    - ``quota``：反封号闸门启用时的
      ``{used, cap, auto_cap, reserve, light, auto_blocked}``——cap=人工道
      上限（闸门比较的数），auto_cap=自动链让路线，auto_blocked=自动链已
      因额度让路（人工仍可发时横幅据此显示「AI 已让路，余量专供人工」）。
    - ``frees_at``：额度拦截（warmup_cap）时首个空位的预计释放时刻（epoch
      秒；算不出 → None）。
    - ``kill``（P0 2026-08-23 急停归因可见化）：Kill-Switch 拦截时附
      ``{scope, source: auto|manual, cause, actor, expires_at}``——横幅据此分
      「系统自动风控（PeerFlood…）+ 倒计时」vs「管理员手动冻结」，不再一律
      说「运营手动」（自动置位被错误归因是实录误导事故）。取不到详情时缺省
      不带该键，前端回落通用文案（旧后端兼容＝feat 特性探测同哲学）。
    - 返回 None ＝ 护栏没拦且闸门未启用——调用方（横幅/搭便车字段）直接略过。
    """
    p = str(platform or "").lower()
    a = str(account_id or "default")
    cfg = config or {}

    if registry is None:
        try:
            from src.integrations.account_registry import get_account_registry
            registry = get_account_registry()
        except Exception:
            registry = None

    blocked, reason = False, ""
    try:
        from src.integrations.shared.send_guard import send_blocked
        blocked, reason = send_blocked(
            p, a, config=cfg, registry=registry,
            chat_key=str(chat_key or ""), notify=False,
            origin=str(origin or "manual"))
    except Exception:
        logger.debug("[send-gate-status] send_blocked 判定失败（视为放行）",
                     exc_info=True)
        blocked, reason = False, ""

    quota: Optional[Dict[str, Any]] = None
    frees_at: Optional[float] = None
    try:
        from src.skills.companion_send_gate import evaluate, gate_enabled
        if gate_enabled(cfg):
            from src.skills.account_signals import build_account_signals
            limiter = None
            try:
                from src.integrations.protocol_autoreply_limits import (
                    get_autoreply_limiter,
                )
                limiter = get_autoreply_limiter(cfg)
            except Exception:
                limiter = None
            sig = build_account_signals(p, a, registry=registry, limiter=limiter)
            dec = evaluate(sig, cfg, origin="manual")
            dec_auto = evaluate(sig, cfg, origin="auto")
            quota = {
                "used": int(sig.get("sends_today") or 0),
                "cap": int(dec.get("recommended_cap") or 0),
                "auto_cap": int(dec_auto.get("auto_cap") or 0),
                "reserve": int(dec.get("reserve_for_manual") or 0),
                "light": str(dec.get("light") or ""),
                # 自动链已因**额度**让路（健康红灯/封禁会拦两道，走主 blocked 面）
                "auto_blocked": bool(
                    not dec_auto.get("allowed", True)
                    and dec_auto.get("reason") in _QUOTA_REASONS),
            }
            if quota["auto_blocked"] and limiter is not None:
                # P3：让路线恢复时刻——蓝色软信息条据此给出「AI 约几点恢复
                # 自动发送」（与人工道 frees_at 同一滚动窗数学，cap 换 auto_cap）
                try:
                    quota["auto_frees_at"] = limiter.quota_frees_at(
                        f"{p}:{a}", quota["auto_cap"], now)
                except Exception:
                    quota["auto_frees_at"] = None
            if (blocked and limiter is not None
                    and any(reason == f"send_gate:{r}" for r in _QUOTA_REASONS)):
                try:
                    frees_at = limiter.quota_frees_at(
                        f"{p}:{a}", quota["cap"], now)
                except Exception:
                    frees_at = None
    except Exception:
        logger.debug("[send-gate-status] 额度数字取数失败（忽略）", exc_info=True)
        quota = quota or None

    kill: Optional[Dict[str, Any]] = None
    if blocked and str(reason or "").startswith("kill_switch"):
        try:
            from src.ops.kill_switch import active_record, freeze_source
            rec = active_record(p, a, now=now)
            if rec:
                source, cause = freeze_source(rec.get("actor"), rec.get("reason"))
                kill = {
                    "scope": str(rec.get("scope") or ""),
                    "source": source,
                    "cause": cause,
                    "actor": str(rec.get("actor") or ""),
                    "expires_at": float(rec.get("expires_at") or 0) or None,
                }
        except Exception:
            logger.debug("[send-gate-status] 急停详情取数失败（回落通用文案）",
                         exc_info=True)
            kill = None

    if not blocked and quota is None:
        return None
    out: Dict[str, Any] = {
        "blocked": bool(blocked),
        "reason": str(reason or ""),
        "quota": quota,
        "frees_at": frees_at,
    }
    if kill:
        out["kill"] = kill
    return out


def blocked_reason_key(reason: str) -> str:
    """拦截原因 → i18n 键后缀（前后端共用同一族谱，别在两处各拼一套）。"""
    r = str(reason or "")
    if any(r.startswith(f"send_gate:{q}") for q in _QUOTA_REASONS):
        return "quota"
    if r.startswith("send_gate:health_red"):
        return "health"
    if r.startswith("send_gate:banned"):
        return "banned"
    if r.startswith("send_gate:"):
        return "gate"
    if r.startswith("kill_switch"):
        return "killswitch"
    if r.startswith("canary"):
        return "canary"
    if r.startswith("license"):
        return "license"
    if r.startswith("session_unhealthy"):
        return "session"
    # 实施96 P0-3：渠道出站策略（平台侧会拒收）——外链 / 字数 / 媒体类型分三条人话，
    # 其余策略原因归 policy 族；与 channel_policy.REASON_* 前缀同源
    if r.startswith("policy_window_no_inbound"):
        return "policy_window_no_inbound"
    if r.startswith("policy_window_expired"):
        return "policy_window_expired"
    if r.startswith("policy_window_"):
        return "policy_window_quota"   # exhausted / reserved_for_manual：都是「本轮条数」问题
    if r.startswith("policy_link_denied"):
        return "policy_link"
    if r.startswith("policy_text_too_long"):
        return "policy_len"
    if r.startswith("policy_media_type_denied"):
        return "policy_media"
    if r.startswith("policy_"):
        return "policy"
    return "generic"


__all__ = ["blocked_reason_key", "send_gate_snapshot"]
