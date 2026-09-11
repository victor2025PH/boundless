"""会话「AI 会不会回」状态机（Q-26 #301 #302 · 2026-09-12）——**只读聚合**。

1.0.81 首日归类（docs/分析_1.0.81首日问题归类…§三 P1-7）：会话头同时挂着五个各说各话的
chip（ay- 让位 / rk- 风控 / aif- 起草失败 / ms- 边车 / lp- 语言），坐席要自己把它们拼成
「所以 AI 到底发不发」。本模块把六个既有状态源折成 **一个状态 + 一句话 + 一个动作**：

    compute(store, cid, ...) -> {
        will_send:  bool                       # AI 此刻会不会自动发
        state:      auto | deferred | held | human | manual |
                    off_hours | sidecar_down | lang_unknown | ai_fail
        tone:       ok | warn | danger | muted # 四色语义（绿 / 琥珀 / 红 / 灰）
        reason_code: str                       # 机器码（源侧原因码，可空）
        reason_text_key: str                   # i18n 键（inbox.cs.*），前端 Tf(key, params)
        until_ts:   float | None               # 到点自动恢复的时刻（让位 / 作息外）
        action:     resume | ack | retry | confirm_pin | none
        params:     dict                       # 文案插值 + 动作参数（draft_id / message_id …）
        sources:    dict                       # 六源原始快照（详情面板 / 门禁断言用）
    }

六源（全部**只读**，任一源异常 → 视为该源无信号，绝不抛、绝不写 KV）：
    mode          store.get_automation_mode + effective_automation（封顶后的有效档位）
    handoff/hold  「需人工」标 + handoff_meta（Q-17 level/category/hits） + risk_hold.active_record
    sidecar       PlatformSessionHealth.inbox_health（e2ee_pin_required / stall）+ 近 24h 边车七码失败留痕
    off_hours     work_hours_gate.off_hours_hold_info（班表休息期）
    agent_yield   worker.agent_yield_state（Q-18 坐席刚发过 → AI 让位 60s）
    lang_plan     Q-21 conv_lang_plan KV（若已落地）∨ l1_reason.peek == lang_unknown
    ai_last_fail  ai_fail_marker.get（Q-14 B 最近一次起草失败）

优先级（指令原文）：manual > held(needs_human) > sidecar_down > off_hours > deferred >
lang_unknown > ai_fail > auto。``human``（人审 / 多选档：AI 拟稿人工发）排在 ai_fail 之后、
auto 之前——它只是「没有更要紧的事」时对非全自动档的收口描述。

只在全自动档才有意义的源（让位 / 作息外 / 语言未知）在人审档不参与判定：人审档 AI 本来
就不自动发，再说「让位 23 秒」是噪音。held / sidecar_down / ai_fail 与档位无关（人审档也要
知道稿没生成 / 消息没送出 / 有人要处理）。

动作全部复用既有端点（红线：本模块不新增写路径）：
    ack          PUT /api/workspace/conv/{cid}/tags（摘「需人工」）—— 前端 _handoffBarAck
    resume       POST /api/unified-inbox/agent-yield/resume —— 前端 _ayResume
    retry        ai_fail → POST /api/drafts/{draft_id}/regenerate（aifRetryDraft）
                 sidecar send_fail → resendFailed(message_id)
    confirm_pin  setMessengerPin（Q-24 C）
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

STATES = ("auto", "deferred", "held", "human", "manual",
          "off_hours", "sidecar_down", "lang_unknown", "ai_fail")

#: 判定顺序（前者命中即定案）
PRIORITY = ("manual", "held", "sidecar_down", "off_hours", "deferred",
            "lang_unknown", "ai_fail", "human", "auto")

TONE = {
    "auto": "ok",
    "deferred": "warn", "off_hours": "warn", "lang_unknown": "warn",
    "held": "danger", "sidecar_down": "danger", "ai_fail": "danger",
    "manual": "muted", "human": "muted",
}

ACTIONS = ("resume", "ack", "retry", "confirm_pin", "none")

_AUTO_MODES = frozenset({"auto_ai"})
_HUMAN_MODES = frozenset({"review", "multi_choice"})


def _now(now: Optional[float]) -> float:
    try:
        return float(now) if now is not None else time.time()
    except Exception:
        return time.time()


# ── 六源采集（每源独立 try，返回 None＝无信号）─────────────────────────────


def _src_mode(store: Any, cid: str, platform: str, account_id: str,
              config: Any) -> Dict[str, Any]:
    base = "review"
    try:
        if store is not None and hasattr(store, "get_automation_mode"):
            base = str(store.get_automation_mode(cid) or "review")
    except Exception:
        base = "review"
    eff = base
    caps: List[Any] = []
    try:
        from src.inbox.effective_automation import effective_automation
        ea = effective_automation(store, config or {}, conversation_id=cid,
                                  platform=str(platform or "").lower(),
                                  account_id=str(account_id or "default"),
                                  base_mode=base) or {}
        eff = str(ea.get("effective_mode") or base)
        caps = list(ea.get("caps") or [])
    except Exception:
        eff = base
    return {"base": base, "effective": eff, "caps": caps}


def _src_handoff(store: Any, cid: str, now: float) -> Optional[Dict[str, Any]]:
    if store is None:
        return None
    hold: Dict[str, Any] = {}
    meta: Dict[str, Any] = {}
    tagged = False
    try:
        from src.inbox import risk_hold as _rh
        hold = dict(_rh.active_record(store, cid, now=now) or {})
    except Exception:
        hold = {}
    try:
        if hasattr(store, "get_handoff_meta"):
            meta = dict(store.get_handoff_meta(cid) or {})
    except Exception:
        meta = {}
    try:
        if hasattr(store, "get_conv_tags"):
            from src.integrations.protocol_autoreply import HANDOFF_TAG
            tagged = HANDOFF_TAG in list(store.get_conv_tags(cid) or [])
    except Exception:
        tagged = False
    if not (hold or tagged):
        return None
    hm_reason = str(meta.get("reason") or "")
    hold_reason = str(hold.get("reason") or "")
    category = str(meta.get("category") or "")
    level = str(meta.get("level") or "")
    hits = [str(h) for h in (meta.get("hits") or []) if str(h)]
    if not category:
        try:
            from src.inbox.risk_grader import classify_reason
            c3, l3 = classify_reason(hm_reason or hold_reason)
            category = str(c3 or "")
            level = level or str(l3 or "")
        except Exception:
            category = (hold_reason if hold_reason and hold_reason != "needs_human"
                        else (hm_reason.split(":", 1)[0] if hm_reason else ""))
    hit = str(hold.get("hit") or "") or (hits[0] if hits else "")
    return {
        "reason": hold_reason or hm_reason or "needs_human",
        "category": category, "level": level, "hit": hit, "hits": hits,
        "tagged": bool(tagged), "hold": bool(hold),
        "since_ts": float(meta.get("ts") or hold.get("set_ts") or 0.0),
        "source": str(meta.get("source") or hold.get("by") or ""),
    }


def _src_sidecar(store: Any, cid: str, platform: str, account_id: str,
                 health: Any, now: float) -> Optional[Dict[str, Any]]:
    plat = str(platform or "").lower()
    acct = str(account_id or "default")
    key = f"{plat}:{acct}"
    dump: Dict[str, Any] = {}
    try:
        if health is None:
            from src.integrations.platform_session_health import get_platform_session_health
            health = get_platform_session_health()
        dump = dict(health.dump() or {}) if health is not None else {}
    except Exception:
        dump = {}
    ih = dict((dump.get("inbox_health") or {}).get(key) or {})
    sess = dict((dump.get("sessions") or {}).get(key) or {})
    # ① PIN 缺失：确定性证据，优先（有明确动作）
    if str(ih.get("hint_code") or "") == "e2ee_pin_required":
        return {"kind": "pin", "code": "e2ee_pin_required",
                "since_ts": float(ih.get("stall_since") or 0.0)}
    # ② 近 24h 边车七码发送失败留痕（Messenger 边车）
    if plat == "messenger" and store is not None and hasattr(store, "recent_failed_outbound"):
        try:
            from src.inbox.send_failure_class import SIDECAR_SEND_FAIL_CODES as _SC
            rows = list(store.recent_failed_outbound(cid, within_sec=86400.0, limit=10) or [])
            hits: List[Dict[str, Any]] = []
            for r in rows:
                fr = str(r.get("fail_reason") or "").strip().lower()
                code = fr if fr in _SC else ""
                if not code and ("not attached" in fr or "detached" in fr or "composer" in fr):
                    code = "composer_detached"
                if code:
                    hits.append({**r, "code": code})
            if hits:
                h0 = hits[0]
                if str(h0["code"]) == "e2ee_pin_pending":
                    return {"kind": "pin", "code": "e2ee_pin_pending",
                            "since_ts": float(h0.get("ts") or 0.0), "n": len(hits),
                            "message_id": str(h0.get("message_id") or "")}
                return {"kind": "send_fail", "code": str(h0["code"]), "n": len(hits),
                        "since_ts": float(h0.get("ts") or 0.0),
                        "message_id": str(h0.get("message_id") or ""),
                        "preview": str(h0.get("text") or "")[:40]}
        except Exception:
            logger.debug("[conv_state] recent_failed_outbound 读取失败（忽略）", exc_info=True)
    # ③ 入站半死（E2EE 等）
    stall_since = 0.0
    try:
        stall_since = float(ih.get("stall_since") or 0.0)
    except (TypeError, ValueError):
        stall_since = 0.0
    if stall_since > 0:
        return {"kind": "stalled", "code": str(ih.get("hint_code") or ih.get("stall_kind")
                                                or "e2ee_relogin")[:64],
                "since_ts": stall_since}
    # ④ 会话不健康（掉线 / 被踢 / 登出）
    try:
        from src.integrations.platform_session_health import UNHEALTHY_STATUSES
        st = str(sess.get("status") or "")
        if st and st in UNHEALTHY_STATUSES:
            return {"kind": "offline", "code": st,
                    "since_ts": float(sess.get("unhealthy_since") or 0.0)}
    except Exception:
        pass
    return None


def _src_off_hours(platform: str, account_id: str, config: Any,
                   now: float) -> Optional[Dict[str, Any]]:
    try:
        from src.inbox.work_hours_gate import off_hours_hold_info, work_schedule_cfg
        ws = work_schedule_cfg(config or {})
        if not ws.get("enabled"):
            return None
        info = off_hours_hold_info(ws, str(platform or ""), str(account_id or "default"),
                                   now_ts=now)
        if not info:
            return None
        return {"until_ts": float(info.get("until_ts") or 0.0),
                "until_hhmm": str(info.get("until_hhmm") or ""),
                "tz": str(info.get("tz") or "")}
    except Exception:
        return None


def _src_agent_yield(worker: Any, cid: str) -> Optional[Dict[str, Any]]:
    try:
        if worker is None or not hasattr(worker, "agent_yield_state"):
            return None
        st = dict(worker.agent_yield_state(cid) or {})
        if not st.get("active"):
            return None
        return {"by": str(st.get("by") or ""), "since": float(st.get("since") or 0.0),
                "until": float(st.get("until") or 0.0),
                "remaining": max(0.0, float(st.get("remaining") or 0.0)),
                "draft_id": str(st.get("draft_id") or "")}
    except Exception:
        return None


_LANG_PLAN_KEYS = ("conv_lang_plan:{cid}", "lang_plan:{cid}")


def _src_lang(store: Any, cid: str) -> Optional[Dict[str, Any]]:
    """Q-21 语言计划（若已落地 KV）优先：能说出「按人设语言回」（会发）；否则回退到
    起草侧登记的 L1 原因码 ``lang_unknown``（现行行为＝稿转人审，不会自动发）。"""
    plan: Dict[str, Any] = {}
    try:
        getter = None
        for name in ("get_app_setting", "get_setting", "kv_get"):
            if store is not None and hasattr(store, name):
                getter = getattr(store, name)
                break
        if getter is not None:
            import json as _json
            for tpl in _LANG_PLAN_KEYS:
                raw = getter(tpl.format(cid=cid))
                if isinstance(raw, str) and raw.strip().startswith("{"):
                    raw = _json.loads(raw)
                if isinstance(raw, dict) and raw:
                    plan = dict(raw)
                    break
    except Exception:
        plan = {}
    if plan:
        unknown = bool(plan.get("peer_lang_unknown") or plan.get("lang_unknown")
                       or str(plan.get("decided_by") or "") in
                       ("lang_unknown", "persona_fallback", "persona_default"))
        if unknown:
            return {"fallback": True, "reply_lang": str(plan.get("reply_lang") or
                                                        plan.get("lang") or ""),
                    "via": "lang_plan"}
        return None
    try:
        from src.inbox import l1_reason
        if l1_reason.peek(cid) == "lang_unknown":
            return {"fallback": False, "reply_lang": "", "via": "l1_reason"}
    except Exception:
        pass
    return None


def _src_ai_fail(store: Any, cid: str, now: float) -> Optional[Dict[str, Any]]:
    try:
        from src.inbox.ai_fail_marker import get as _get, hhmm as _hhmm
        rec = _get(store, cid) if store is not None else None
        if not rec:
            return None
        ts = float(rec.get("ts") or 0.0)
        return {"reason": str(rec.get("reason") or "unknown"), "ts": ts,
                "hhmm": _hhmm(ts), "ago_sec": round(max(0.0, now - ts), 0),
                "draft_id": str(rec.get("draft_id") or ""),
                "latency_ms": int(rec.get("latency_ms") or 0)}
    except Exception:
        return None


# ── 汇总 ─────────────────────────────────────────────────────────────────


def compute(store: Any, cid: str, *, platform: str = "", account_id: str = "",
            worker: Any = None, config: Any = None, health: Any = None,
            now: Optional[float] = None) -> Dict[str, Any]:
    """六源只读聚合 → 单一状态。任何源失败都不影响其余源；整体绝不抛。"""
    cid = str(cid or "")
    ts_now = _now(now)
    cfg = config or {}
    mode = _src_mode(store, cid, platform, account_id, cfg)
    eff = mode["effective"]
    is_auto = eff in _AUTO_MODES
    src: Dict[str, Any] = {
        "mode": mode,
        "handoff": _src_handoff(store, cid, ts_now),
        "sidecar": _src_sidecar(store, cid, platform, account_id, health, ts_now),
        "off_hours": _src_off_hours(platform, account_id, cfg, ts_now) if is_auto else None,
        "agent_yield": _src_agent_yield(worker, cid) if is_auto else None,
        "lang_plan": _src_lang(store, cid) if is_auto else None,
        "ai_last_fail": _src_ai_fail(store, cid, ts_now),
    }
    out: Dict[str, Any] = {
        "cid": cid, "will_send": False, "state": "human", "tone": "muted",
        "reason_code": "", "reason_text_key": "inbox.cs.human", "until_ts": None,
        "action": "none", "params": {}, "sources": src, "ts": ts_now,
    }

    def _set(state: str, *, will_send: bool, reason_code: str = "", text_key: str = "",
             until_ts: Optional[float] = None, action: str = "none",
             **params: Any) -> Dict[str, Any]:
        out.update({
            "state": state, "tone": TONE.get(state, "muted"), "will_send": bool(will_send),
            "reason_code": str(reason_code or ""),
            "reason_text_key": text_key or f"inbox.cs.{state}",
            "until_ts": (float(until_ts) if until_ts else None),
            "action": action if action in ACTIONS else "none",
            "params": {k: v for k, v in params.items() if v is not None},
        })
        return out

    if eff == "manual":
        return _set("manual", will_send=False, reason_code="mode_manual",
                    base_mode=mode["base"], caps=mode["caps"])
    h = src["handoff"]
    if h:
        return _set("held", will_send=False, reason_code=str(h["reason"]),
                    action="ack", category=h["category"], level=h["level"],
                    hit=h["hit"], hits=h["hits"], since_ts=h["since_ts"],
                    source=h["source"], tagged=h["tagged"])
    sc = src["sidecar"]
    if sc:
        kind = str(sc.get("kind") or "")
        action = {"pin": "confirm_pin", "send_fail": "retry"}.get(kind, "none")
        return _set("sidecar_down", will_send=False, reason_code=str(sc.get("code") or kind),
                    text_key=f"inbox.cs.sidecar_down.{kind}", action=action,
                    kind=kind, code=sc.get("code"), n=sc.get("n"),
                    message_id=sc.get("message_id"), since_ts=sc.get("since_ts"),
                    preview=sc.get("preview"))
    oh = src["off_hours"]
    if oh:
        return _set("off_hours", will_send=False, reason_code="off_hours",
                    until_ts=oh["until_ts"], until_hhmm=oh["until_hhmm"], tz=oh["tz"])
    ay = src["agent_yield"]
    if ay:
        return _set("deferred", will_send=True, reason_code="agent_yield",
                    until_ts=ay["until"], action="resume", by=ay["by"],
                    remaining_sec=int(round(ay["remaining"])), since=ay["since"],
                    draft_id=ay["draft_id"] or None)
    lp = src["lang_plan"]
    if lp:
        fb = bool(lp.get("fallback"))
        return _set("lang_unknown", will_send=fb, reason_code="lang_unknown",
                    text_key="inbox.cs.lang_unknown" if fb else "inbox.cs.lang_unknown_l1",
                    reply_lang=lp.get("reply_lang") or None, via=lp.get("via"))
    af = src["ai_last_fail"]
    if af:
        return _set("ai_fail", will_send=False, reason_code=str(af["reason"]),
                    action="retry" if af["draft_id"] else "none",
                    reason=af["reason"], hhmm=af["hhmm"], ago_sec=af["ago_sec"],
                    draft_id=af["draft_id"] or None, latency_ms=af["latency_ms"])
    if is_auto:
        return _set("auto", will_send=True, reason_code="mode_auto_ai",
                    caps=mode["caps"])
    return _set("human", will_send=False, reason_code=f"mode_{eff}",
                base_mode=mode["base"], effective=eff, caps=mode["caps"])


__all__ = ["compute", "STATES", "PRIORITY", "TONE", "ACTIONS"]
