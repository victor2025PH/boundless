# -*- coding: utf-8 -*-
"""
统一任务派发门禁 — 在 run_task 真正执行前集中校验。

与 task_execution_policy.yaml 联动：
  gate_mode × 任务 tier → preflight 强度（full / network_only / none）与是否 GEO。
阻塞时任务记为 failed，错误信息带 [gate] 前缀；结构化详情写入 gate_evaluation。
"""

from __future__ import annotations

import copy
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)


def resolve_gate_hint_message(hint_code: str) -> str:
    """由 hint_code 生成固定中文指引（单一文案源，API/前端共用）。"""
    if not hint_code:
        return ""
    table = {
        "geo_public_ip_failed": "公网 IP 探测失败：检查外网与 VPN，或网络稳定后重试。",
        "geo_public_ip_unknown": "出口 IP 无法识别：先连接 VPN 再执行任务。",
        "geo_country_mismatch": "出口国家与任务目标不一致：切换与目标国一致的代理节点。",
        "geo_check_exception": "GEO 检测异常：查看主控日志与出站策略。",
        "preflight_network": "设备预检（网络）：确认 Wi‑Fi/蜂窝可用、未开飞行模式；若 HTTP 探测失败但浏览器能上外网，可能是 DNS/ROM 限制，请在设备页「诊断」。",
        "preflight_vpn": "设备预检（VPN）：需要与任务地区一致的出口时，请打开 v2rayNG 并连接对应节点。若本地 SIM/WiFi 出口已在目标国，系统会在全量预检中尝试跳过 VPN。",
        "preflight_account": "设备预检（账号/TikTok）：确认 TikTok 已安装，可手动启动一次 App。",
    }
    if hint_code in table:
        return table[hint_code]
    if hint_code.startswith("preflight_"):
        step = hint_code[len("preflight_") :] or "unknown"
        return f"设备预检未通过（{step}）：优先排查网络，再按需处理 VPN / App。"
    return "对照错误信息与运维文档排查。"


def result_dict_with_gate_hints(result: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """
    API 输出用：若 result.gate_evaluation 仅有 hint_code、无 hint_message，
    则 deepcopy result 并补 hint_message（旧任务/旧 Worker 落库数据兼容）。
    无需补全时返回原引用，避免无谓拷贝。
    """
    if not isinstance(result, dict):
        return result
    ge = result.get("gate_evaluation")
    if not isinstance(ge, dict):
        return result
    code = (ge.get("hint_code") or "").strip()
    if not code or (ge.get("hint_message") or "").strip():
        return result
    new_result = copy.deepcopy(result)
    new_ge = new_result["gate_evaluation"]
    assert isinstance(new_ge, dict)
    msg = resolve_gate_hint_message(code)
    if msg:
        new_ge["hint_message"] = msg
    return new_result


def enrich_task_payload_row(t: Dict[str, Any]) -> Dict[str, Any]:
    """HTTP 返回的「整任务 dict」：补 hint_message + 任务中心展示字段。"""
    if not isinstance(t, dict):
        return t
    base = dict(t)
    raw = base.get("result")
    nr = result_dict_with_gate_hints(raw)
    if nr is not None and nr is not raw:
        base["result"] = nr
    try:
        from src.host.task_ui_enrich import build_task_ui_enrichment

        ui = build_task_ui_enrichment(base)
        return {**base, **ui}
    except Exception:
        return base


# 需要纳入「风险前缀 + tier 矩阵」的任务类型前缀（未匹配 tier 时用 default_tier）
_DEFAULT_RISKY_PREFIXES: Tuple[str, ...] = (
    "tiktok_",
    "telegram_",
    "whatsapp_",
    "linkedin_",
    "facebook_",
    "instagram_",
    "studio_",
    "content_",
    "batch_",
    "auto_",
)


def _risky_prefixes_from_policy(policy: Dict[str, Any]) -> Tuple[str, ...]:
    extra = policy.get("manual_gate") or {}
    raw = extra.get("risky_task_prefixes")
    if isinstance(raw, list) and raw:
        return tuple(str(x) for x in raw)
    return _DEFAULT_RISKY_PREFIXES


def task_type_needs_gate(task_type: str, policy: Optional[Dict[str, Any]] = None) -> bool:
    if not task_type:
        return False
    prefs = _risky_prefixes_from_policy(policy or {})
    return any(task_type.startswith(p) for p in prefs)


def _policy_allows_param_bypass(policy: Dict[str, Any]) -> bool:
    mg = policy.get("manual_gate") or {}
    return bool(mg.get("allow_param_bypass", False))


def _geo_snapshot_reusable(
    pf: Any,
    expected_country: str,
    preflight_mode: str,
) -> Optional[Dict[str, Any]]:
    """full 预检已做同期望国 GEO 时，门禁可复用快照，省略二次公网 IP 查询。"""
    if preflight_mode != "full":
        return None
    snap = getattr(pf, "geo_snapshot", None)
    if not isinstance(snap, dict):
        return None
    exp = (snap.get("expected_country") or "").strip().lower()
    if exp != expected_country:
        return None
    return snap


@dataclass
class GateEvaluation:
    """门禁结构化结果（便于日志、任务详情、控制台调试）。"""

    allowed: bool
    reason: str
    task_type: str = ""
    tier: str = "L2"
    gate_mode: str = "strict"
    preflight_mode: str = "full"
    geo_enforced: bool = True
    connectivity: Dict[str, Any] = field(default_factory=dict)
    # 前端/运营可映射固定文案：preflight_* / geo_*
    hint_code: str = ""
    # 非空则覆盖 resolve_gate_hint_message(hint_code)；通常留空由后端统一生成
    hint_message: str = ""

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "allowed": self.allowed,
            "reason": self.reason,
            "task_type": self.task_type,
            "tier": self.tier,
            "gate_mode": self.gate_mode,
            "preflight_mode": self.preflight_mode,
            "geo_enforced": self.geo_enforced,
            "connectivity": self.connectivity,
        }
        if self.hint_code:
            d["hint_code"] = self.hint_code
            msg = (self.hint_message or "").strip() or resolve_gate_hint_message(self.hint_code)
            if msg:
                d["hint_message"] = msg
        return d


def evaluate_task_gate_detailed(
    task: Dict[str, Any],
    resolved_device_id: str,
    config_path: str,
) -> GateEvaluation:
    """
    返回结构化门禁结果。allowed=False 时 reason 含 [gate] 前缀，可写入任务 error。
    """
    from src.host.gate_matrix import resolve_requirements
    from src.host.task_policy import load_task_execution_policy

    task_type = (task.get("type") or "").strip()
    params = task.get("params") or {}
    if isinstance(params, str):
        try:
            import json

            params = json.loads(params)
        except Exception:
            params = {}

    policy = load_task_execution_policy()
    empty_conn: Dict[str, Any] = {}

    try:
        from src.host.fb_playbook import local_rules_disabled
        if task_type.startswith("facebook_") and local_rules_disabled():
            logger.info("[gate] facebook local_rules_disabled=true，跳过统一门禁")
            return GateEvaluation(
                True,
                "",
                task_type=task_type,
                connectivity={"skipped": "local_rules_disabled", **empty_conn},
            )
    except Exception:
        pass

    if params.get("bypass_manual_gate") and _policy_allows_param_bypass(policy):
        logger.info("[gate] bypass via params (policy allows_param_bypass)")
        return GateEvaluation(
            True,
            "",
            task_type=task_type,
            connectivity={"bypass": "param", **empty_conn},
        )

    if not task_type_needs_gate(task_type, policy):
        return GateEvaluation(True, "", task_type=task_type, connectivity={"skipped": "not_risky_type"})

    mg = policy.get("manual_gate") or {}

    # ── 2026-04-21 P0-3: FB 风控红旗自动冷却 ──────────────────────────
    # 24h 内同设备累计 ≥ risk_threshold 次风控事件 → 拒绝派发 facebook_*，
    # 保护账号不被继续"硬怼"。阈值可通过 manual_gate.fb_risk_* 配置热加载。
    if task_type.startswith("facebook_"):
        risk_cfg = mg.get("fb_risk_cooldown") or {}
        enabled = bool(risk_cfg.get("enabled", True))
        hours = int(risk_cfg.get("window_hours", 24))
        threshold = int(risk_cfg.get("threshold", 3))
        if enabled and resolved_device_id:
            try:
                # 2026-04-24 v3: 只统计 CRITICAL 级风控事件 (identity_verify /
                # checkpoint / account_review 等), 不把 content_blocked 这类
                # message-level friction 当账号级风险算. 否则 3 条重复消息被
                # FB 拒发就冻结整个设备 24h, 过度保守.
                from src.host.fb_store import count_critical_risk_events_recent
                recent = count_critical_risk_events_recent(
                    resolved_device_id, hours=hours)
            except Exception:
                recent = 0
            if recent >= threshold:
                msg = (
                    f"[gate] FB 风控冷却中: 最近 {hours}h 累计 {recent} 次风控事件 "
                    f"(阈值 {threshold})，暂停该设备 facebook_* 任务以保护账号"
                )
                logger.warning("[gate] %s task_type=%s device=%s",
                               msg, task_type, resolved_device_id[:12])
                return GateEvaluation(
                    False,
                    msg,
                    task_type=task_type,
                    connectivity={
                        "fb_risk_cooldown": {
                            "recent_count": recent,
                            "threshold": threshold,
                            "window_hours": hours,
                        }
                    },
                    hint_code="fb_risk_cooldown",
                )

    # 全局关闭预检（兼容旧配置）
    if not mg.get("enforce_preflight", True):
        logger.info("[gate] manual_gate.enforce_preflight=false，跳过门禁")
        return GateEvaluation(
            True,
            "",
            task_type=task_type,
            connectivity={"skipped": "enforce_preflight_false"},
        )

    gate_mode, tier, preflight_mode, geo_from_matrix, _row = resolve_requirements(
        policy, task_type
    )
    geo_enforced = bool(geo_from_matrix)
    if not mg.get("enforce_geo_for_risky", True):
        geo_enforced = False

    raw_exp = (
        params.get("target_country")
        or params.get("country")
        or mg.get("default_expected_country")
        or "italy"
    )
    if isinstance(raw_exp, str):
        expected_for_gate = raw_exp.lower().strip()
    else:
        expected_for_gate = "italy"

    from src.host.preflight import run_preflight

    pf = run_preflight(
        resolved_device_id,
        skip_cache=True,
        mode=preflight_mode,  # type: ignore[arg-type]
        task_target_country=expected_for_gate if preflight_mode == "full" else None,
    )

    conn: Dict[str, Any] = {
        "tier": tier,
        "gate_mode": gate_mode,
        "preflight_mode": preflight_mode,
        "geo_enforced": geo_enforced,
        "preflight": pf.to_dict(),
    }

    if not pf.passed:
        msg = f"[gate] 预检未通过 ({pf.blocked_step}): {pf.blocked_reason}"
        logger.warning("[gate] %s task_type=%s tier=%s mode=%s", msg, task_type, tier, gate_mode)
        step = (pf.blocked_step or "unknown").strip().lower().replace(" ", "_")
        hint = f"preflight_{step}" if step else "preflight_unknown"
        return GateEvaluation(
            False,
            msg,
            task_type=task_type,
            tier=tier,
            gate_mode=gate_mode,
            preflight_mode=preflight_mode,
            geo_enforced=geo_enforced,
            connectivity=conn,
            hint_code=hint,
        )

    if not geo_enforced:
        logger.info(
            "[gate] 通过 task_type=%s tier=%s gate_mode=%s preflight=%s GEO=跳过",
            task_type,
            tier,
            gate_mode,
            preflight_mode,
        )
        return GateEvaluation(
            True,
            "",
            task_type=task_type,
            tier=tier,
            gate_mode=gate_mode,
            preflight_mode=preflight_mode,
            geo_enforced=False,
            connectivity=conn,
        )

    expected = expected_for_gate

    try:
        from src.behavior.geo_check import GeoCheckResult, check_device_geo
        from src.device_control.device_manager import get_device_manager

        manager = get_device_manager(config_path)
        snap = _geo_snapshot_reusable(pf, expected, preflight_mode)
        if snap is not None:
            geo = GeoCheckResult(
                device_id=resolved_device_id,
                public_ip=snap.get("public_ip") or "",
                detected_country=snap.get("detected_country") or "",
                detected_country_code=snap.get("detected_country_code") or "",
                expected_country=expected,
                matches=bool(snap.get("matches")),
                error=(snap.get("error") or "").strip(),
            )
            logger.info("[gate] GEO 复用预检快照（省略二次查询）task=%s device=%s", task_type, resolved_device_id[:8])
        else:
            geo = check_device_geo(resolved_device_id, expected, manager)
        conn["geo"] = {
            "expected_country": expected,
            "detected_country": geo.detected_country,
            "public_ip": geo.public_ip,
            "matches": geo.matches,
            "error": geo.error,
            "reused_from_preflight": snap is not None,
            # Sprint 4 P2: 双源交叉校验元信息
            "cross_checked": bool(getattr(geo, "cross_checked", False)),
            "source_conflict": bool(getattr(geo, "source_conflict", False)),
            "sources": getattr(geo, "sources", None) or [],
        }
        if geo.error:
            msg = f"[gate] GEO 检查失败: {geo.error}"
            logger.warning("[gate] %s", msg)
            if mg.get("geo_fail_open", False):
                logger.warning("[gate] geo_fail_open=True，允许继续")
                return GateEvaluation(
                    True,
                    "",
                    task_type=task_type,
                    tier=tier,
                    gate_mode=gate_mode,
                    preflight_mode=preflight_mode,
                    geo_enforced=geo_enforced,
                    connectivity=conn,
                )
            err_l = (geo.error or "").lower()
            geo_hint = "geo_public_ip_unknown"
            if "public" in err_l or "ip" in err_l or "determine" in err_l:
                geo_hint = "geo_public_ip_failed"
            return GateEvaluation(
                False,
                msg,
                task_type=task_type,
                tier=tier,
                gate_mode=gate_mode,
                preflight_mode=preflight_mode,
                geo_enforced=geo_enforced,
                connectivity=conn,
                hint_code=geo_hint,
            )
        if not geo.matches:
            msg = (
                f"[gate] 出口国家与任务不一致: 期望≈{expected} "
                f"实际={geo.detected_country or '?'} IP={geo.public_ip or '?'}"
            )
            logger.warning("[gate] %s", msg)
            return GateEvaluation(
                False,
                msg,
                task_type=task_type,
                tier=tier,
                gate_mode=gate_mode,
                preflight_mode=preflight_mode,
                geo_enforced=geo_enforced,
                connectivity=conn,
                hint_code="geo_country_mismatch",
            )
        # Sprint 4 P2: 即便 matches=True,若三源交叉有 conflict 也拒绝。
        # 动机:单源可能被劫持/撒谎;三源中仍有 1 票说出不同国家,安全起见不放行。
        # 用 geo_fail_open 语义反向:fail_open=True 时(运维宽松模式) 允许;
        # fail_open=False(合规优先) 则 conflict 也拒绝。
        if getattr(geo, "source_conflict", False) and not mg.get("geo_fail_open", False):
            sources_summary = ", ".join(
                f"{s['method']}={s['country_code'] or '?'}"
                for s in (getattr(geo, "sources", None) or [])
            )
            msg = (
                f"[gate] GEO 交叉校验冲突(matches={geo.matches} 但多源分歧): "
                f"{sources_summary}"
            )
            logger.warning("[gate] %s", msg)
            return GateEvaluation(
                False,
                msg,
                task_type=task_type,
                tier=tier,
                gate_mode=gate_mode,
                preflight_mode=preflight_mode,
                geo_enforced=geo_enforced,
                connectivity=conn,
                hint_code="geo_cross_source_conflict",
            )
    except Exception as e:
        msg = f"[gate] GEO 检查异常: {e}"
        logger.exception("[gate] geo check")
        if mg.get("geo_fail_open", False):
            logger.warning("[gate] geo_fail_open=True，允许继续")
            return GateEvaluation(
                True,
                "",
                task_type=task_type,
                tier=tier,
                gate_mode=gate_mode,
                preflight_mode=preflight_mode,
                geo_enforced=geo_enforced,
                connectivity=conn,
            )
        return GateEvaluation(
            False,
            msg,
            task_type=task_type,
            tier=tier,
            gate_mode=gate_mode,
            preflight_mode=preflight_mode,
            geo_enforced=geo_enforced,
            connectivity=conn,
            hint_code="geo_check_exception",
        )

    logger.info(
        "[gate] 通过 task_type=%s tier=%s gate_mode=%s preflight=%s GEO=OK",
        task_type,
        tier,
        gate_mode,
        preflight_mode,
    )
    return GateEvaluation(
        True,
        "",
        task_type=task_type,
        tier=tier,
        gate_mode=gate_mode,
        preflight_mode=preflight_mode,
        geo_enforced=geo_enforced,
        connectivity=conn,
    )


def evaluate_task_gate(
    task: Dict[str, Any],
    resolved_device_id: str,
    config_path: str,
) -> Tuple[bool, str]:
    ev = evaluate_task_gate_detailed(task, resolved_device_id, config_path)
    return ev.allowed, ev.reason


def last_gate_summary() -> Dict[str, Any]:
    """供 /task-dispatch/policy 调试。"""
    from src.host.gate_matrix import get_effective_gate_matrix, resolve_gate_mode
    from src.host.task_policy import load_task_execution_policy

    p = load_task_execution_policy()
    gm = resolve_gate_mode(p)
    matrix = get_effective_gate_matrix(p)
    return {
        "manual_execution_only": p.get("manual_execution_only"),
        "gate_mode": gm,
        "risky_prefixes": list(_risky_prefixes_from_policy(p)),
        "tier_by_prefix": p.get("tier_by_prefix") or {},
        "default_tier": p.get("default_tier") or "L2",
        "gate_matrix": matrix,
        "manual_gate": p.get("manual_gate") or {},
    }
