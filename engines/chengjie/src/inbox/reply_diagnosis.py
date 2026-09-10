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
cap_platform            block  平台封顶（platform_modes；params 另带 note=运营
                               备注 / channel_ok=发送通道健康，见 P2 2026-08-21）
cap_business_line       block  业务线封顶
guard_<reason>          block  peer_bot_guard 将拦下一条入站
deliver_off / l2_off    block  System-Z 架构下投递链没开
work_schedule           block  工作班表扣留
pending_drafts          warn   有待审草稿积压（params 另带 stale/stale_h=
                               陈旧护栏预判，超龄稿直接通过会被 409 拦）
media_block             warn   近窗（30min）有该会话的链路拦截明细（图片/语音
                               没被理解等；params: domain/n/reason/queued——
                               「这条为什么没回」的现场答案，实施56 P1）
managed_peer            warn   对端是本系统受管账号（AI 对聊，谨防互聊环）
peer_bot_flag           warn   会话被标记为机器人对方
conv_missing            warn   会话行不存在（从没收到过对方消息）
account_missing         warn   注册表无此账号行
customer_waiting_no_gate warn  无任何闸拦、也无待审草稿，但末条是入站且已超
                               宽限（600s）无回——「一片绿却不回」的定向诊断
                               （B113；params: wait_sec/effective）
ai_last_fail            warn   最近一次 AI 起草失败未被后续成功覆盖（Q-14 #262：
                               params: reason=timeout|connect|gateway_5xx|no_key|
                               empty|unknown / hhmm / ago_sec / latency_ms / draft_id）
high_risk               block  会话挂着风险持有 / 「需人工」标（Q-3 闸；Q-15 #271：params
                               reason / category（adult|privacy|…）/ level（成人四级）/ hit /
                               policy（adult 时 human|soft_reply|mark_only）/ held_min）
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
            # P2 2026-08-21（「封顶忘摘」实锤沉淀：8/18 WA 封顶的两个前提
            # 8/20 都已消除，却挂到 8/21 无人记得）：
            # - note：运营在 inbox.auto_draft.platform_mode_notes.<平台> 留的
            #   人话备注（为什么封/谁封的），面板原样展示——封顶自带说明书；
            # - channel_ok：本会话账号 registry online 且 mode 属编排模式
            #   （protocol/official/web）＝发送通道大概率活着 → 前端据此提示
            #   「封顶原因可能已消除，可评估解除」。求值失败一律 False（宁可
            #   不提示，绝不误导解除）。
            plat_detail = str(cap.get("detail") or "") or platform
            note = ""
            try:
                note = str((((cfg.get("inbox") or {}).get("auto_draft") or {})
                            .get("platform_mode_notes") or {})
                           .get(plat_detail) or "")
            except Exception:
                note = ""
            channel_ok = False
            try:
                from src.integrations.account_orchestrator import (
                    ORCHESTRATED_MODES,
                )
                channel_ok = bool(
                    account.get("status") == "online"
                    and str(account.get("mode") or "") in ORCHESTRATED_MODES)
            except Exception:
                channel_ok = False
            _finding(findings, "block", "cap_platform",
                     detail=plat_detail,
                     ceiling=str(cap.get("ceiling") or ""),
                     note=note, channel_ok=channel_ok)
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

    # ── 近窗链路拦截明细（delivery_block 环形缓存，实施56 P1）───────────
    # 链路级 findings 说的是「以后会不会回」；这里补「刚才那几条发生了什么」
    # ——图片/语音没被理解而拦下的消息，正是坐席点开面板最想问的那条。
    # warn 级（链路本身没死）；fail-open；进程重启即清（明细本就是近窗语义）。
    try:
        from src.ops.delivery_block import recent_blocks_for
        _evs = recent_blocks_for(cid, now=ts_now)
        if _evs:
            _by_dom: Dict[str, Dict[str, Any]] = {}
            for ev in _evs:
                d = str(ev.get("domain") or "") or "unknown"
                slot = _by_dom.setdefault(
                    d, {"n": 0, "queued": False, "reason": ""})
                slot["n"] += 1
                slot["queued"] = slot["queued"] or bool(ev.get("queued"))
                slot["reason"] = str(ev.get("reason") or "") or slot["reason"]
            out["recent_blocks"] = _by_dom
            for d in sorted(_by_dom):
                info = _by_dom[d]
                _finding(findings, "warn", "media_block",
                         domain=d, n=int(info["n"]),
                         reason=str(info["reason"]),
                         queued=bool(info["queued"]))
    except Exception:
        logger.debug("[reply_diag] 拦截明细读取失败（忽略）", exc_info=True)

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
        # P2 2026-08-21：待审草稿预判「过没过陈旧护栏」（stale_approve_hours，
        # 默认 24h；0=护栏关）。与 DraftService._stale_check 同阈值口径——
        # 面板引导坐席去点「通过」之前先说清「这条其实会被拦、该重新生成」，
        # 不然坐席白点一次 409（预判必须与护栏行为一致，approve_blocked 同哲学）。
        stale_h_cfg = 24.0
        try:
            stale_h_cfg = float(
                ((cfg.get("inbox") or {}).get("auto_draft") or {})
                .get("stale_approve_hours", 24.0) or 0.0)
        except Exception:
            stale_h_cfg = 24.0
        _finding(findings, "warn", "pending_drafts",
                 n=pending_n, oldest_h=oldest_h,
                 stale=bool(stale_h_cfg > 0 and oldest_h > stale_h_cfg),
                 stale_h=round(stale_h_cfg, 1))

    # ── B113（实施68 P1-17）：无闸拦、客户却在等 ────────────────────────
    # 钧 _543：会话全自动绿、客户已回「Oh really?/Haha…」、AI 未跟，右栏只有
    # 「初识 100%+确认进阶」（那是纯展示层，**不** gate 回复生成——回复链从不读
    # relationship_stage 的 needs_confirmation）。所以所有闸都开着却不回时，坐席
    # 面对一片绿一头雾水。这一档明说「没有闸拦下它，但这条确实还没回」——把排查
    # 从「猜是不是进阶确认卡住」变成「要么在途稍等、要么人工推一把」。
    # 判据：末条是入站 + 超宽限无出站 + 无 pending 草稿（有草稿走 pending_drafts）。
    silent_wait_sec = 0.0
    last_dir = ""
    if store is not None and not any(f["level"] == "block" for f in findings) \
            and not pending_n:
        try:
            _recent = []
            if hasattr(store, "list_recent_messages"):
                try:
                    _recent = store.list_recent_messages(
                        cid, limit=1, include_deleted=False) or []
                except TypeError:
                    _recent = store.list_recent_messages(cid, limit=1) or []
            if _recent:
                _last = _recent[-1]
                last_dir = str(_last.get("direction") or "")
                if last_dir == "in":
                    silent_wait_sec = max(
                        0.0, ts_now - float(_last.get("ts") or ts_now))
        except Exception:
            silent_wait_sec = 0.0
    out["silent_wait_sec"] = round(silent_wait_sec, 1)
    # 宽限＝轮询兜底 TTL 同刻度（600s）：超过它还没回＝不是「正在生成」能解释的
    _SILENT_GRACE_SEC = 600.0
    if silent_wait_sec >= _SILENT_GRACE_SEC:
        _finding(findings, "warn", "customer_waiting_no_gate",
                 wait_sec=round(silent_wait_sec, 1),
                 effective=str(effective.get("effective_mode")
                               or base_mode or ""))

    # ── Q-14 #262：最近一次 AI 起草失败（超时 / 网关不可达 / 空响应）──────
    # 「为什么没回」最直接的答案之一：AI 这一轮根本没生成。灰标由起草侧写、下次
    # 成功即清，所以这里有值 = 该会话最新状态仍是「未生成」。fail-open。
    try:
        from src.inbox.ai_fail_marker import get as _aif_get, hhmm as _aif_hhmm
        _aif = _aif_get(store, cid) if store is not None else None
        if _aif:
            out["ai_last_fail"] = _aif
            _aif_ts = float(_aif.get("ts") or 0.0)
            _finding(findings, "warn", "ai_last_fail",
                     reason=str(_aif.get("reason") or "unknown"),
                     hhmm=_aif_hhmm(_aif_ts),
                     ago_sec=round(max(0.0, ts_now - _aif_ts), 0),
                     latency_ms=int(_aif.get("latency_ms") or 0),
                     draft_id=str(_aif.get("draft_id") or ""))
    except Exception:
        logger.debug("[reply_diag] ai_last_fail 读取失败（忽略）", exc_info=True)

    # ── Q-15 #271：会话级风险持有 / 「需人工」原因 → high_risk（带类别 / 级别 / 命中词）──
    # Q-3 之后 risk_hold 与「需人工」标都是闸（worker 人工优先闸 / A 线 _risk_hold_gate）；
    # 此前体检对它们零感知——一片绿却不回。成人内容（adult_grader）带 level / hit / 人设政策，
    # 其余（privacy / commitment / stop_contact / 泛因 needs_human）只带 reason。fail-open。
    try:
        _hold: Dict[str, Any] = {}
        _hm: Dict[str, Any] = {}
        if store is not None:
            try:
                from src.inbox import risk_hold as _rh
                _hold = dict(_rh.active_record(store, cid, now=ts_now) or {})
            except Exception:
                _hold = {}
            try:
                if hasattr(store, "get_handoff_meta"):
                    _hm = dict(store.get_handoff_meta(cid) or {})
            except Exception:
                _hm = {}
            _tagged = False
            try:
                if hasattr(store, "get_conv_tags"):
                    from src.integrations.protocol_autoreply import HANDOFF_TAG as _hp_tag
                    _tagged = _hp_tag in list(store.get_conv_tags(cid) or [])
            except Exception:
                _tagged = False
            if _hold or _tagged:
                from src.inbox.adult_grader import (
                    CATEGORY as _ADULT, adult_policy_of, card_label_parts, resolve_persona,
                )
                _hm_reason = str(_hm.get("reason") or "")
                _hold_reason = str(_hold.get("reason") or "")
                _parts = card_label_parts(_hm_reason)
                _category = _parts["category"] or (
                    _hold_reason if _hold_reason and _hold_reason != "needs_human" else "")
                if not _category and _hm_reason:
                    _category = _hm_reason.split(":", 1)[0]
                _p: Dict[str, Any] = {
                    "reason": _hold_reason or (_hm_reason or "needs_human"),
                    "category": _category,
                    "level": _parts["level"],
                    "hit": _parts["hit"] or str(_hold.get("hit") or ""),
                    "tagged_ts": float(_hm.get("ts") or _hold.get("set_ts") or 0.0),
                    "source": str(_hm.get("source") or _hold.get("by") or ""),
                    "held_min": round(max(0.0, ts_now - float(_hold.get("set_ts") or _hm.get("ts") or ts_now)) / 60.0, 1),
                }
                if _category == _ADULT:
                    _pol, _pol_src = adult_policy_of(resolve_persona(
                        {"platform": platform, "account_id": account_id, "chat_key": chat_key}, cfg), cfg)
                    _p["policy"] = _pol
                    _p["policy_source"] = _pol_src
                out["risk_hold"] = {"hold": _hold or None, "handoff_meta": _hm or None, "tagged": _tagged}
                # params 里的 level 是成人四级（与 _finding 的严重度同名），直接组 dict
                findings.append({"level": "block", "code": "high_risk", "params": _p})
    except Exception:
        logger.debug("[reply_diag] risk_hold / needs_human 读取失败（忽略）", exc_info=True)

    if not any(f["level"] == "block" for f in findings):
        _finding(findings, "ok", "looks_alive",
                 effective=str(effective.get("effective_mode")
                               or base_mode or ""))
    return out


__all__ = ["diagnose_conversation"]
