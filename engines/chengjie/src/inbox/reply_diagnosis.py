# -*- coding: utf-8 -*-
"""「为什么没自动回」进程内诊断核心（P0 2026-08-09，why_no_reply 的 API 化）。

与 ``tools/why_no_reply.py``（离线 ro-SQLite CLI）同一套判定视角，但取数走
**在跑进程**的 store/config/registry——坐席在收件箱点「AI 状态」就能看到
CLI 级别的排障结论，不再需要工程师 SSH。

设计约束：

- **findings 只出码 + 参数，不出人话**：文案由前端 ``window.T('inbox.diag.<code>')``
  按 UI 语言渲染（后端零 CJK，天然过 route CJK ratchet；CLI 保留自己的中文人话）。
- **只读、fail-open**：任何一段取数失败 → 该段静默缺席，绝不 500 打断坐席；
  诊断是护栏的旁观者，不允许成为新故障点。
- **与护栏同源**：有效档位走 ``effective_automation``（与 A/B 两线同一实现）、
  预算走 ``peer_bot_guard.budget_state``、守卫复演走 ``peer_bot_guard.evaluate``
  纯函数——「诊断说的」恒等于「护栏做的」。

findings 码表（level ∈ ok/warn/block；params 供文案插值）：

======================  =====  ==========================================
code                    level  语义
======================  =====  ==========================================
account_status          block  账号非 online（worker 停了，收发全停）
takeover_manual         block  坐席手动出站触发「接管即静音」（可一键接回）
explicit_non_auto       block  显式非全自动档位（human/guard/sweep 等来源）
cap_warmup              block  冷启动预热封顶（新号 72h 人审）
cap_platform            block  平台封顶（platform_modes）
cap_business_line       block  业务线封顶
guard_<reason>          block  peer_bot_guard 将拦下一条入站
deliver_off / l2_off    block  System-Z 架构下投递链没开
work_schedule           block  工作班表扣留
pending_drafts          warn   有待审草稿积压（AI 在拟稿、没人点发送）
managed_peer            warn   对端是本系统受管账号（AI 对聊，谨防互聊环）
peer_bot_flag           warn   会话被标记为机器人对方
conv_missing            warn   会话行不存在（从没收到过对方消息）
account_missing         warn   注册表无此账号行
looks_alive             ok     未发现拦截
======================  =====  ==========================================
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def _finding(findings: List[Dict[str, Any]], level: str, code: str,
             **params: Any) -> None:
    findings.append({"level": level, "code": code, "params": params})


def diagnose_conversation(
    store: Any,
    config: Optional[Dict[str, Any]],
    *,
    platform: str,
    account_id: str,
    chat_key: str,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """单会话自动回复链路诊断（只读）。返回机读结构，findings 见模块码表。"""
    ts_now = float(now or time.time())
    platform = str(platform or "").lower()
    account_id = str(account_id or "default")
    chat_key = str(chat_key or "")
    cid = f"{platform}:{account_id}:{chat_key}"
    cfg = config if isinstance(config, dict) else {}
    findings: List[Dict[str, Any]] = []
    out: Dict[str, Any] = {"conversation_id": cid, "findings": findings}

    # ── 账号（注册表只读 peek，绝不隐式建库）───────────────────────────
    account: Dict[str, Any] = {}
    try:
        from src.integrations.account_registry import peek_account
        row = peek_account(platform, account_id)
        if row:
            account = {
                "status": str(row.get("status") or ""),
                "mode": str(row.get("mode") or ""),
                "created_at": float(row.get("created_at") or 0.0),
            }
            if account["status"] not in ("online", ""):
                _finding(findings, "block", "account_status",
                         status=account["status"])
        else:
            _finding(findings, "warn", "account_missing")
    except Exception:
        logger.debug("[reply_diag] 注册表读取失败（忽略）", exc_info=True)
    out["account"] = account

    # ── 会话行 + 显式档位（含来源）────────────────────────────────────
    conv: Dict[str, Any] = {}
    meta: Optional[Dict[str, Any]] = None
    if store is not None:
        try:
            conv = dict(store.get_conversation(cid) or {})
        except Exception:
            conv = {}
        try:
            if hasattr(store, "get_automation_mode_meta"):
                meta = store.get_automation_mode_meta(cid)
        except Exception:
            meta = None
    if not conv:
        _finding(findings, "warn", "conv_missing")
    out["conversation"] = {
        "exists": bool(conv),
        "chat_type": str(conv.get("chat_type") or ""),
        "display_name": str(conv.get("display_name") or ""),
        "peer_is_bot": int(conv.get("peer_is_bot") or 0),
    }
    out["mode_source"] = meta
    # 档位变更时间线（P2）：谁在什么时候把档位改成什么（human/bootstrap/
    # guard:*/sweep/bulk/takeover*/rearm 六族来源）。旧库无表/旧 store → 缺席。
    try:
        if store is not None and hasattr(store, "list_automation_mode_log"):
            out["mode_history"] = store.list_automation_mode_log(cid, limit=10)
    except Exception:
        pass

    # ── 有效档位（与 A/B 两线同一实现）＋ 封顶 findings ────────────────
    effective: Dict[str, Any] = {}
    base_mode = ""
    try:
        from src.inbox.effective_automation import effective_automation
        effective = effective_automation(
            store, cfg, conversation_id=cid, platform=platform,
            account_id=account_id) or {}
        base_mode = str(effective.get("mode") or "")
    except Exception:
        logger.debug("[reply_diag] 有效档位求值失败（忽略）", exc_info=True)
    out["effective"] = effective

    if base_mode in ("manual", "review", "multi_choice"):
        src = str((meta or {}).get("source") or "")
        set_at = float((meta or {}).get("updated_at") or 0.0)
        is_takeover = False
        try:
            from src.inbox.takeover_rearm import is_takeover_source, rearm_state
            is_takeover = base_mode == "manual" and is_takeover_source(src)
            if is_takeover:
                rs = rearm_state(meta, cfg, now=ts_now) or {}
                _finding(findings, "block", "takeover_manual",
                         taken_at=set_at,
                         restore_mode=str(rs.get("restore_mode") or ""),
                         rearm_enabled=bool(rs.get("enabled")),
                         eta_in_sec=rs.get("eta_in_sec"))
                out["rearm"] = rs
        except Exception:
            is_takeover = False
        if not is_takeover:
            _finding(findings, "block", "explicit_non_auto",
                     mode=base_mode, source=src, set_at=set_at)

    for cap in (effective.get("caps") or []):
        layer = str((cap or {}).get("layer") or "")
        if layer == "warmup":
            left_h = max(
                0.0, (float(cap.get("until_ts") or 0) - ts_now) / 3600.0)
            _finding(findings, "block", "cap_warmup",
                     left_h=round(left_h, 1),
                     ceiling=str(cap.get("ceiling") or ""),
                     until_ts=float(cap.get("until_ts") or 0.0))
        elif layer == "platform":
            _finding(findings, "block", "cap_platform",
                     detail=str(cap.get("detail") or ""),
                     ceiling=str(cap.get("ceiling") or ""))
        elif layer == "business_line":
            _finding(findings, "block", "cap_business_line",
                     detail=str(cap.get("detail") or ""),
                     ceiling=str(cap.get("ceiling") or ""))

    # ── peer_bot_guard：下一条入站会怎样（纯函数复演，零写路径）────────
    guard: Dict[str, Any] = {}
    try:
        from src.inbox import peer_bot_guard as pbg
        g_cfg = pbg.parse_cfg(cfg)
        guard["enabled"] = bool(g_cfg.get("enabled"))
        if g_cfg.get("enabled") and conv and store is not None:
            budget = pbg.budget_state(store, cid, cfg, now=ts_now)
            guard["budget"] = budget
            recent: List[Dict[str, Any]] = []
            try:
                recent = store.list_recent_messages(cid, limit=120) or []
            except Exception:
                recent = []
            v = pbg.evaluate(
                recent, g_cfg, platform=platform,
                username=str(conv.get("username") or ""),
                display_name=str(conv.get("display_name") or ""),
                chat_type=str(conv.get("chat_type") or ""),
                peer_override=int(conv.get("peer_is_bot") or 0),
                now=ts_now,
                auto_out_today=int(budget.get("used") or 0),
                budget_relieved=bool(budget.get("relieved")))
            guard["next_inbound_blocked"] = bool(v.blocked)
            guard["reason"] = str(v.reason or "")
            if v.blocked:
                _finding(findings, "block", f"guard_{v.reason}",
                         evidence=str(v.evidence or ""),
                         downgrade_to=str(v.downgrade_to or ""))
        elif int(conv.get("peer_is_bot") or 0) == 1:
            _finding(findings, "warn", "peer_bot_flag",
                     evidence=str(conv.get("bot_evidence") or ""))
    except Exception:
        logger.debug("[reply_diag] 守卫复演失败（忽略）", exc_info=True)
    out["guard"] = guard

    # ── 对端是否本系统受管账号（AI 对聊观测，P0-5 薄版）────────────────
    try:
        from src.integrations.account_registry import peek_account as _peek
        peer_row = _peek(platform, chat_key) if chat_key else None
        if peer_row:
            _finding(findings, "warn", "managed_peer",
                     peer_account_id=chat_key,
                     peer_status=str(peer_row.get("status") or ""))
    except Exception:
        pass

    # ── 部署形态 / 投递链（与 CLI 同判据）──────────────────────────────
    tg_login = ((cfg.get("platform_login") or {}).get("telegram") or {})
    companion = bool(tg_login.get("companion_runtime", False))
    l2 = ((cfg.get("inbox") or {}).get("l2_autosend") or {})
    l2_enabled = bool(l2.get("enabled", True))
    deliver = bool(l2.get("deliver", False))
    out["deploy"] = {
        "companion_runtime": companion,
        "l2_autosend_enabled": l2_enabled,
        "deliver": deliver,
        "warmup_review": bool(
            (((cfg.get("companion") or {}).get("proactive_topic") or {})
             .get("cold_start") or {}).get("warmup_review", True)),
    }
    if platform == "telegram" and not companion:
        if not deliver:
            _finding(findings, "block", "deliver_off")
        if not l2_enabled:
            _finding(findings, "block", "l2_off")
    try:
        from src.inbox.work_hours_gate import (
            should_hold_auto_reply,
            work_schedule_cfg,
        )
        hold = should_hold_auto_reply(
            work_schedule_cfg(cfg), platform, account_id, peer_text="")
        if hold:
            _finding(findings, "block", "work_schedule", hold=str(hold))
    except Exception:
        pass

    # ── 待审草稿积压（AI 在拟稿、没人点发送）──────────────────────────
    pending_n = 0
    oldest_h = 0.0
    if store is not None and hasattr(store, "list_drafts"):
        try:
            pend = store.list_drafts(
                status="pending", conversation_id=cid, limit=20) or []
            pending_n = len(pend)
            if pend:
                oldest = min(
                    float(d.get("created_at") or ts_now) for d in pend)
                oldest_h = round((ts_now - oldest) / 3600.0, 1)
        except Exception:
            pending_n = 0
    out["drafts_pending"] = {"n": pending_n, "oldest_h": oldest_h}
    if pending_n:
        _finding(findings, "warn", "pending_drafts",
                 n=pending_n, oldest_h=oldest_h)

    if not any(f["level"] == "block" for f in findings):
        _finding(findings, "ok", "looks_alive",
                 effective=str(effective.get("effective_mode")
                               or base_mode or ""))
    return out


__all__ = ["diagnose_conversation"]
