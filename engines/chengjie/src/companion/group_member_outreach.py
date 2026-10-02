"""同群开口 —— 从成员库里挑人，由「当时拉到他的那个号」发第一条私聊。

和提取分开，也和主动触达（只扫已经有过私聊的会话）分开：

- 每天每号 5–10 个新人，默认 8。配置写 200 也会被夹到 10。
- 只对「这个号自己的 access_hash、且人还在可聊条件里」的成员发。
- 同一个人跨群只开口一次。
- 不在后台自己循环。今日队列可以生成；真正发出去要逐条确认。
- 撞 PEER_FLOOD / FloodWait：停这个号的开口（回复链不受这里控制），不换下一个人继续轰。
- 新号从每天 3 条在 14 天里爬到 8 条；只在本地 10:00–21:00 放行发送（排队不限时段）。
- 每条开口也计入这个号的总发送量（companion_send_gate），红灯 / 总额度满 / 急停都发不出。
- 对方回了私聊 → 这个人标 replied，之后由收件箱接管。

不写 inbox 的 outreach_log。那张表要 conversation_id，冷开口还没有会话，
写进去会污染回复率。账本就是成员行上的 outreach_state。
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from src.companion.group_members_store import (
    OUTREACH_APPROVED,
    OUTREACH_BLOCKED,
    OUTREACH_HOLD_ALL,
    OUTREACH_NONE,
    OUTREACH_QUEUED,
    OUTREACH_SENDING,
    OUTREACH_SENT,
)

logger = logging.getLogger("ai_chat_assistant.group_member_outreach")

OUTREACH_CAP_DEFAULT = 8
OUTREACH_CAP_MIN = 5
OUTREACH_CAP_MAX = 10
OUTREACH_MIN_GAP_SEC = 25 * 60
FLOOD_HOLD_SEC = 24 * 3600
OUTREACH_WARMUP_START = 3
OUTREACH_WARMUP_DAYS = 14
OUTREACH_HOURS = (10, 21)
# 全自动门槛：号够老、近期没撞过风控、回复率不太难看（样本够了才看）
AUTO_MIN_AGE_DAYS = 14
AUTO_FLOOD_LOOKBACK_DAYS = 7
AUTO_MIN_REPLY_RATE = 0.05
AUTO_MIN_SAMPLE = 10
# 跟进：发出后 72h 没回音补一句（只一次），再 72h 还没回音封存
FOLLOWUP_AFTER_HOURS = 72
FOLLOWUP_CLOSE_AFTER_HOURS = 72
FOLLOWUP_DAILY_CAP = 5
# 第二跳：对方回了、我方（AI / 坐席）超过这么久还没接上话 → 列「没接上」提醒
STALLED_AFTER_HOURS = 48


@dataclass(frozen=True)
class OutreachPolicy:
    """一次开口判定用到的全部阈值。路由从 config 读一次，纯函数只认它。"""

    cap: int = OUTREACH_CAP_DEFAULT
    min_gap_sec: int = OUTREACH_MIN_GAP_SEC
    warmup_start: int = OUTREACH_WARMUP_START
    warmup_days: int = OUTREACH_WARMUP_DAYS
    hours_start: int = OUTREACH_HOURS[0]
    hours_end: int = OUTREACH_HOURS[1]
    auto_min_age_days: int = AUTO_MIN_AGE_DAYS
    auto_flood_lookback_days: int = AUTO_FLOOD_LOOKBACK_DAYS
    auto_min_reply_rate: float = AUTO_MIN_REPLY_RATE
    auto_min_sample: int = AUTO_MIN_SAMPLE
    followup_enabled: bool = True
    followup_after_hours: int = FOLLOWUP_AFTER_HOURS
    followup_close_after_hours: int = FOLLOWUP_CLOSE_AFTER_HOURS
    followup_daily_cap: int = FOLLOWUP_DAILY_CAP
    stalled_after_hours: int = STALLED_AFTER_HOURS

    @classmethod
    def from_config(cls, gm_cfg: Any) -> "OutreachPolicy":
        """``companion.group_members`` 段 → 策略。坏值回默认，额度仍夹 5..10。"""
        cfg = gm_cfg if isinstance(gm_cfg, dict) else {}

        def _int(key: str, default: int, lo: int, hi: int) -> int:
            try:
                n = int(cfg.get(key, default))
            except (TypeError, ValueError):
                n = default
            return max(lo, min(n, hi))

        def _float(key: str, default: float, lo: float, hi: float) -> float:
            try:
                v = float(cfg.get(key, default))
            except (TypeError, ValueError):
                v = default
            return max(lo, min(v, hi))

        hours = cfg.get("outreach_hours")
        hs, he = OUTREACH_HOURS
        if isinstance(hours, (list, tuple)) and len(hours) == 2:
            try:
                hs, he = int(hours[0]), int(hours[1])
            except (TypeError, ValueError):
                hs, he = OUTREACH_HOURS
        hs = max(0, min(hs, 23))
        he = max(0, min(he, 24))
        if he <= hs:
            hs, he = OUTREACH_HOURS
        return cls(
            cap=clamp_outreach_cap(cfg.get("outreach_daily_cap", OUTREACH_CAP_DEFAULT)),
            min_gap_sec=_int("outreach_min_gap_sec", OUTREACH_MIN_GAP_SEC, 60, 6 * 3600),
            warmup_start=_int("outreach_warmup_start_cap", OUTREACH_WARMUP_START, 1,
                              OUTREACH_CAP_MAX),
            warmup_days=_int("outreach_warmup_ramp_days", OUTREACH_WARMUP_DAYS, 0, 90),
            hours_start=hs,
            hours_end=he,
            auto_min_age_days=_int("outreach_auto_min_age_days", AUTO_MIN_AGE_DAYS, 0, 365),
            auto_flood_lookback_days=_int("outreach_auto_flood_lookback_days",
                                          AUTO_FLOOD_LOOKBACK_DAYS, 0, 90),
            auto_min_reply_rate=_float("outreach_auto_min_reply_rate", AUTO_MIN_REPLY_RATE,
                                       0.0, 1.0),
            auto_min_sample=_int("outreach_auto_min_sample", AUTO_MIN_SAMPLE, 1, 1000),
            followup_enabled=bool(cfg.get("outreach_followup_enabled", True)),
            followup_after_hours=_int("outreach_followup_after_hours", FOLLOWUP_AFTER_HOURS,
                                      12, 24 * 30),
            followup_close_after_hours=_int("outreach_followup_close_after_hours",
                                            FOLLOWUP_CLOSE_AFTER_HOURS, 12, 24 * 30),
            followup_daily_cap=_int("outreach_followup_daily_cap", FOLLOWUP_DAILY_CAP, 0, 20),
            stalled_after_hours=_int("outreach_stalled_after_hours", STALLED_AFTER_HOURS, 1, 24 * 14),
        )


def effective_outreach_cap(policy: OutreachPolicy, age_days: Optional[float]) -> int:
    """新号从 warmup_start 在 warmup_days 内爬到 cap。天龄不明按新号算。"""
    from src.skills.account_health import warmup_cap
    cap = clamp_outreach_cap(policy.cap)
    if int(policy.warmup_days) <= 0:
        return cap
    age = float(age_days) if age_days is not None else 0.0
    return max(1, min(cap, warmup_cap(
        age, cap, start_cap=int(policy.warmup_start), ramp_days=int(policy.warmup_days))))


def local_hour(now: float) -> int:
    return int(time.localtime(float(now)).tm_hour)


def hours_block_reason(policy: OutreachPolicy, now: float,
                       hour: Optional[int] = None) -> str:
    """''=在放行时段。'hours'=本地时间不在 [hours_start, hours_end)。"""
    h = local_hour(now) if hour is None else int(hour)
    if int(policy.hours_start) <= h < int(policy.hours_end):
        return ""
    return "hours"

_PITCH_MARKERS = (
    "http://", "https://", "t.me/", "wa.me",
    "微信", "weixin", "wechat", "whatsapp", "加我",
)


def clamp_outreach_cap(raw: Any, default: int = OUTREACH_CAP_DEFAULT) -> int:
    """开口日额度夹在 5..10。提取用的 200 不能直接拿来开口。"""
    try:
        n = int(raw)
    except (TypeError, ValueError):
        n = int(default)
    if n < OUTREACH_CAP_MIN:
        n = OUTREACH_CAP_MIN
    if n > OUTREACH_CAP_MAX:
        n = OUTREACH_CAP_MAX
    return n


def suggest_opener(member: Dict[str, Any]) -> str:
    """同群短开场。不带链接、不推销。坐席可以改，但过不了 opener_block_reason 就发不出。"""
    name = str(member.get("first_name") or member.get("username") or "").strip()
    title = str(member.get("group_title") or "").strip()
    if name and title:
        return "%s，在「%s」看到你发言，过来打个招呼。" % (name, title)
    if title:
        return "在「%s」看到你发言，过来打个招呼。" % title
    if name:
        return "%s，看到你在群里发言，过来打个招呼。" % name
    return "看到你在群里发言，过来打个招呼。"


def opener_block_reason(text: Any) -> str:
    """空串=可以发。否则是拒绝原因：empty / too_long / pitch。"""
    s = str(text or "").strip()
    if not s:
        return "empty"
    if len(s) > 180:
        return "too_long"
    low = s.lower()
    for mark in _PITCH_MARKERS:
        if mark.lower() in low:
            return "pitch"
    return ""


def classify_send_error(exc: BaseException) -> str:
    """Telegram 错误分桶。flood 停号；privacy/deactivated/peer_invalid 停这个人。"""
    name = type(exc).__name__.upper().replace("_", "")
    text = str(exc).upper().replace("_", "")
    blob = name + " " + text
    if "PEERFLOOD" in blob or "FLOODWAIT" in blob or "SLOWMODEWAIT" in blob:
        return "flood"
    if "PRIVACY" in blob:
        return "privacy"
    if "DEACTIVATED" in blob:
        return "deactivated"
    if "PEERIDINVALID" in blob or "PEERINVALID" in blob:
        return "peer_invalid"
    return "retryable"


def hold_block_reason(store: Any, account_id: str, now: float) -> str:
    """''=可以开口。paused=人工急停。flood=风控熔断还没到期。"""
    global_hold = store.get_hold(OUTREACH_HOLD_ALL)
    own = store.get_hold(str(account_id))
    if global_hold.get("paused") or own.get("paused"):
        return "paused"
    until = max(float(global_hold.get("flood_until") or 0.0),
                float(own.get("flood_until") or 0.0))
    if until > float(now):
        return "flood"
    return ""


def _candidate_ok(member: Dict[str, Any], account_id: str,
                  touched: Set[str]) -> bool:
    if str(member.get("hash_account_id") or "") != str(account_id):
        return False
    if not str(member.get("access_hash") or "").strip():
        return False
    if member.get("is_bot") or member.get("is_admin") or not member.get("spoke"):
        return False
    if str(member.get("outreach_state") or OUTREACH_NONE) != OUTREACH_NONE:
        return False
    uid = str(member.get("user_id") or "")
    if not uid or uid in touched:
        return False
    return True


def select_candidates(members: Sequence[Dict[str, Any]], *, account_id: str,
                      slots: int, touched: Set[str],
                      intent: Optional[Dict[str, Sequence[str]]] = None) -> List[Dict[str, Any]]:
    """从 none 里挑今天还能排队的人。跨群 user_id 只留一条。

    排序：意向分（人设关键词命中，见 member_intent）> 有用户名 > 最近发言 > 静态分。
    命中排除词的人不排。
    """
    from src.companion.member_intent import member_intent
    slots = max(0, int(slots))
    if slots <= 0:
        return []
    ranked = []
    for m in members:
        if not _candidate_ok(m, account_id, touched):
            continue
        score, _hits = member_intent(m, intent)
        if score < 0:
            continue
        ranked.append((score, m))
    ranked.sort(key=lambda sm: (
        -sm[0],
        0 if str(sm[1].get("username") or "").strip() else 1,
        -float(sm[1].get("last_spoke_ts") or 0.0),
        -int(sm[1].get("score") or 0),
        str(sm[1].get("user_id") or ""),
    ))
    return _dedup_take([m for _s, m in ranked], touched, slots)


def _dedup_take(ranked: Sequence[Dict[str, Any]], touched: Set[str],
                slots: int) -> List[Dict[str, Any]]:
    seen = set(touched)
    picked: List[Dict[str, Any]] = []
    for m in ranked:
        uid = str(m.get("user_id") or "")
        if uid in seen:
            continue
        seen.add(uid)
        picked.append(m)
        if len(picked) >= slots:
            break
    return picked


def conversation_id_for(member: Dict[str, Any]) -> str:
    """这个人和开口号之间的收件箱会话 id（telegram:<号>:<user_id>）；还没发过就是空。"""
    acct = str(member.get("outreach_account_id") or "").strip()
    uid = str(member.get("user_id") or "").strip()
    if not acct or not uid:
        return ""
    try:
        from src.inbox.normalizer import conv_id
        return conv_id("telegram", acct, uid)
    except Exception:
        return "telegram:%s:%s" % (acct, uid)


def public_member(member: Dict[str, Any], *, with_opener: bool = False) -> Dict[str, Any]:
    """给管理台的行。access_hash 不出去。"""
    d = {
        "group_id": member.get("group_id") or "",
        "user_id": member.get("user_id") or "",
        "username": member.get("username") or "",
        "first_name": member.get("first_name") or "",
        "last_name": member.get("last_name") or "",
        "group_title": member.get("group_title") or "",
        "score": int(member.get("score") or 0),
        "spoke": bool(member.get("spoke")),
        "is_admin": bool(member.get("is_admin")),
        "outreach_state": member.get("outreach_state") or OUTREACH_NONE,
        "hash_account_id": member.get("hash_account_id") or "",
        "last_spoke_ts": float(member.get("last_spoke_ts") or 0.0),
        "outreach_error": member.get("outreach_error") or "",
        "last_msg_text": member.get("last_msg_text") or "",
        "last_msg_ts": float(member.get("last_msg_ts") or 0.0),
        "lang_code": member.get("lang_code") or "",
        "opener_text": member.get("opener_text") or "",
        "opener_source": member.get("opener_source") or "",
        "approved_at": float(member.get("approved_at") or 0.0),
        "opener_persona": member.get("opener_persona") or "",
        "outreach_at": float(member.get("outreach_at") or 0.0),
        "followup_at": float(member.get("followup_at") or 0.0),
        "followup_text": member.get("followup_text") or "",
        "replied_at": float(member.get("replied_at") or 0.0),
        "reply_text": member.get("reply_text") or "",
        "answered_at": float(member.get("answered_at") or 0.0),
        "last_in_at": float(member.get("last_in_at") or 0.0),
        "last_out_at": float(member.get("last_out_at") or 0.0),
        "opener_variant": member.get("opener_variant") or "",
        "conversation_id": conversation_id_for(member),
    }
    if with_opener:
        d["suggested_opener"] = d["opener_text"] or suggest_opener(member)
    return d


def _with_intent(row: Dict[str, Any], member: Dict[str, Any],
                 intent: Optional[Dict[str, Sequence[str]]]) -> Dict[str, Any]:
    from src.companion.member_intent import member_intent
    score, hits = member_intent(member, intent)
    row["intent"] = score
    row["intent_hits"] = hits
    return row


def build_preview(store: Any, account_id: str, *, now: float, since_ts: float,
                  policy: OutreachPolicy, age_days: Optional[float] = None,
                  hour: Optional[int] = None,
                  intent: Optional[Dict[str, Sequence[str]]] = None) -> Dict[str, Any]:
    store.reap_stale_sending(now)
    cap = clamp_outreach_cap(policy.cap)
    eff_cap = effective_outreach_cap(policy, age_days)
    used = store.count_outreach_sent_since(account_id, since_ts)
    queued_n = store.count_outreach_queued(account_id)
    slots = max(0, eff_cap - used - queued_n)
    owned = store.list_by_hash_account(account_id)
    touched = store.touched_user_ids()
    candidates = select_candidates(owned, account_id=account_id, slots=slots, touched=touched,
                                   intent=intent)
    queued = [m for m in owned
              if str(m.get("outreach_state") or "") in (OUTREACH_QUEUED, OUTREACH_APPROVED)]
    # 已批准的排前面（调度器就按这个顺序发），其余按近发言
    queued.sort(key=lambda m: (
        0 if str(m.get("outreach_state") or "") == OUTREACH_APPROVED else 1,
        float(m.get("approved_at") or 0.0),
        -float(m.get("last_spoke_ts") or 0.0)))
    last = store.last_outreach_sent_ts(account_id)
    next_at = (last + float(policy.min_gap_sec)) if last else 0.0
    gap_wait = max(0.0, next_at - float(now)) if next_at else 0.0
    return {
        "account_id": str(account_id),
        "mode": store.get_outreach_mode(account_id),
        "summary": store.outreach_summary(account_id, since_ts),
        "cap": cap,
        "effective_cap": eff_cap,
        "age_days": (round(float(age_days), 1) if age_days is not None else None),
        "warming_up": eff_cap < cap,
        "used_today": used,
        "queued": queued_n,
        "remaining": slots,
        "hold": hold_block_reason(store, account_id, now),
        "hours": {"start": int(policy.hours_start), "end": int(policy.hours_end),
                  "ok": not hours_block_reason(policy, now, hour)},
        "min_gap_sec": int(policy.min_gap_sec),
        "gap_wait_sec": int(gap_wait),
        "unhashed": store.count_unhashed_for_account(account_id),
        "candidates": [_with_intent(public_member(m, with_opener=True), m, intent)
                       for m in candidates],
        "queue": [_with_intent(public_member(m, with_opener=True), m, intent) for m in queued],
        "intent_configured": bool(intent and (intent.get("positive") or intent.get("negative"))),
        # 今日回音：对方回了什么、会话在哪——闭环的最后一眼
        "replied": [public_member(m) for m in store.list_replied_since(account_id, since_ts)],
        # 第二跳没接上：回了 N 小时我方还没回话（AI 没触发 / 坐席没接）——开口成功却掉地的人
        "stalled": [public_member(m) for m in
                    store.list_stalled_replies(stalled_before(policy, now), account_id)],
        "stalled_after_hours": int(policy.stalled_after_hours),
    }


def stalled_before(policy: OutreachPolicy, now: float) -> float:
    return float(now) - float(policy.stalled_after_hours) * 3600.0


def enqueue_today(store: Any, account_id: str, *, now: float, since_ts: float,
                  policy: OutreachPolicy, age_days: Optional[float] = None,
                  intent: Optional[Dict[str, Sequence[str]]] = None) -> Dict[str, Any]:
    """把今天还能开口的人标成 queued。不发送。时段外也可以先排好，发的时候再拦。"""
    block = hold_block_reason(store, account_id, now)
    if block:
        return {"ok": False, "kind": block, "queued_now": 0}
    preview = build_preview(store, account_id, now=now, since_ts=since_ts,
                            policy=policy, age_days=age_days, intent=intent)
    n = 0
    for m in preview["candidates"]:
        if store.cas_outreach(
            m["group_id"], m["user_id"], expect_states=(OUTREACH_NONE,),
            new_state=OUTREACH_QUEUED, account_id=account_id, now=now,
        ):
            n += 1
    out = {"ok": True, "kind": "queued", "queued_now": n,
           "remaining_before": preview["remaining"]}
    if not n and preview["remaining"] > 0:
        out["empty"] = explain_empty_pool(store, account_id)
    return out


def explain_empty_pool(store: Any, account_id: str) -> Dict[str, int]:
    """一个都排不进时说清楚卡在哪：这个号能私聊的人（pool）里多少没发过言 / 是管理员或 bot /
    已经开过口；另有多少人是这个号提的但没拿到私聊凭证（unhashed）。"""
    touched = store.touched_user_ids()
    out = {"pool": 0, "silent": 0, "admin_or_bot": 0, "touched": 0, "unhashed": 0}
    for m in store.list_by_hash_account(account_id):
        out["pool"] += 1
        if m.get("is_bot") or m.get("is_admin"):
            out["admin_or_bot"] += 1
        elif str(m.get("outreach_state") or OUTREACH_NONE) != OUTREACH_NONE \
                or str(m.get("user_id") or "") in touched:
            out["touched"] += 1
        elif not m.get("spoke"):
            out["silent"] += 1
    try:
        out["unhashed"] = int(store.count_unhashed_for_account(account_id) or 0)
    except Exception:
        pass
    return out


def prepare_release(store: Any, *, account_id: str, group_id: str, user_id: str,
                    text: str, now: float, since_ts: float, policy: OutreachPolicy,
                    age_days: Optional[float] = None,
                    hour: Optional[int] = None) -> Dict[str, Any]:
    """校验并占坑 sending。通过时带上发送所需的 user_id/access_hash，不把 hash 留给路由日志。

    顺序：文案 → 急停/风控 → 时段 → 今日额度（含新号爬坡）→ 间隔 → 这个人的状态。
    ``text`` 为空时用这个人已定稿的 ``opener_text``（调度器就这么发）。
    """
    store.reap_stale_sending(now)
    text = str(text or "").strip()
    if not text:
        pre = store.get_member(group_id, user_id)
        text = str((pre or {}).get("opener_text") or "").strip()
    if opener_block_reason(text):
        return {"ok": False, "http": 400, "kind": "text"}
    block = hold_block_reason(store, account_id, now)
    if block:
        return {"ok": False, "http": 409, "kind": block}
    if hours_block_reason(policy, now, hour):
        return {"ok": False, "http": 409, "kind": "hours"}
    cap = effective_outreach_cap(policy, age_days)
    used = store.count_outreach_sent_since(account_id, since_ts)
    if used >= cap:
        return {"ok": False, "http": 409, "kind": "cap"}
    last = store.last_outreach_sent_ts(account_id)
    min_gap_sec = float(policy.min_gap_sec)
    if last and float(now) < last + min_gap_sec:
        return {"ok": False, "http": 409, "kind": "gap",
                "gap_wait_sec": int(last + min_gap_sec - float(now))}
    row = store.get_member(group_id, user_id)
    if row is None or str(row.get("outreach_state") or "") not in (OUTREACH_QUEUED,
                                                                    OUTREACH_APPROVED):
        return {"ok": False, "http": 409, "kind": "state"}
    if str(row.get("hash_account_id") or "") != str(account_id):
        return {"ok": False, "http": 409, "kind": "state"}
    ah = str(row.get("access_hash") or "").strip()
    if not ah:
        return {"ok": False, "http": 409, "kind": "state"}
    if not store.cas_outreach(
        group_id, user_id, expect_states=(OUTREACH_QUEUED, OUTREACH_APPROVED),
        new_state=OUTREACH_SENDING, account_id=account_id, now=now,
    ):
        return {"ok": False, "http": 409, "kind": "state"}
    try:
        store.record_sent_text(group_id, user_id, text)
    except Exception:
        logger.debug("[gm_outreach] 记录实发文案失败", exc_info=True)
    return {"ok": True, "http": 200, "kind": "ready",
            "user_id": int(row["user_id"]), "access_hash": int(ah),
            "text": text, "prev_state": str(row.get("outreach_state") or OUTREACH_QUEUED)}


def finalize_release(store: Any, *, account_id: str, group_id: str, user_id: str,
                     now: float, result: Dict[str, Any], text: str = "") -> Dict[str, Any]:
    """按发送结果落状态。flood 停号并把人退回 none，避免熔断期间还挂在队列里。

    发出成功还会把这句镜像进收件箱（我方出站）：对方回话时 AI 回复链才知道我们先说了什么。
    """
    kind = str((result or {}).get("kind") or "retryable")
    if (result or {}).get("ok"):
        store.cas_outreach(
            group_id, user_id, expect_states=(OUTREACH_SENDING,),
            new_state=OUTREACH_SENT, account_id=account_id, error="", now=now,
        )
        mirror_outbound(store, account_id=account_id, group_id=group_id, user_id=user_id,
                        text=text, result=result, now=now)
        return {"ok": True, "http": 200, "kind": "sent"}
    if kind == "flood":
        store.set_hold(account_id, flood_until=float(now) + FLOOD_HOLD_SEC,
                       reason="peer_flood", last_flood_at=float(now))
        store.cas_outreach(
            group_id, user_id, expect_states=(OUTREACH_SENDING,),
            new_state=OUTREACH_NONE, account_id=account_id, error="flood", now=now,
        )
        return {"ok": False, "http": 200, "kind": "flood"}
    if kind in ("privacy", "deactivated", "peer_invalid"):
        store.cas_outreach(
            group_id, user_id, expect_states=(OUTREACH_SENDING,),
            new_state=OUTREACH_BLOCKED, account_id=account_id, error=kind, now=now,
        )
        return {"ok": False, "http": 200, "kind": kind}
    store.cas_outreach(
        group_id, user_id, expect_states=(OUTREACH_SENDING,),
        new_state=OUTREACH_QUEUED, account_id=account_id, error=kind, now=now,
    )
    return {"ok": False, "http": 200, "kind": kind}


# ── 全自动门槛 ───────────────────────────────────────────────────────────────

def auto_mode_block_reason(store: Any, account_id: str, *, now: float,
                           age_days: Optional[float], policy: OutreachPolicy) -> Dict[str, Any]:
    """这个号现在够不够格全自动。返回 {ok, reason, detail}。

    reason: ''=够格；age=号太新 / 天龄不明；flood=近 N 天撞过风控；reply_rate=样本够了但
    回复率低于地板。三条都是「人不盯着就别放手」的硬线，不可在页面上关。
    """
    age = float(age_days) if age_days is not None else None
    if age is None or age < float(policy.auto_min_age_days):
        return {"ok": False, "reason": "age",
                "detail": {"age_days": age, "min": int(policy.auto_min_age_days)}}
    hold = store.get_hold(str(account_id))
    last_flood = float(hold.get("last_flood_at") or 0.0)
    lookback = float(policy.auto_flood_lookback_days) * 86400.0
    if lookback > 0 and last_flood > 0 and float(now) - last_flood < lookback:
        return {"ok": False, "reason": "flood",
                "detail": {"last_flood_at": last_flood,
                           "lookback_days": int(policy.auto_flood_lookback_days)}}
    rr = store.reply_rate(str(account_id), float(now) - 7 * 86400.0)
    if int(rr.get("sent") or 0) >= int(policy.auto_min_sample) and \
            rr.get("rate") is not None and float(rr["rate"]) < float(policy.auto_min_reply_rate):
        return {"ok": False, "reason": "reply_rate",
                "detail": {"sent": rr["sent"], "replied": rr["replied"],
                           "rate": round(float(rr["rate"]), 3),
                           "min": float(policy.auto_min_reply_rate)}}
    return {"ok": True, "reason": "", "detail": {"reply_rate_7d": rr}}


# ── 72h 单次跟进 ─────────────────────────────────────────────────────────────

def followup_due_before(policy: OutreachPolicy, now: float) -> float:
    """outreach_at 早于这个时刻的 sent 才到跟进点。"""
    return float(now) - float(policy.followup_after_hours) * 3600.0


def followup_close_before(policy: OutreachPolicy, now: float) -> float:
    return float(now) - float(policy.followup_close_after_hours) * 3600.0


def prepare_followup(store: Any, *, account_id: str, group_id: str, user_id: str,
                     text: str, now: float, since_ts: float, policy: OutreachPolicy,
                     hour: Optional[int] = None) -> Dict[str, Any]:
    """校验并原子占跟进坑。顺序：开关 → 文案 → 急停/风控 → 时段 → 跟进日额 → 间隔 → 这个人。

    跟进不占「每天新人」额度（它不是新人），但有自己的小额度，并和开口共用最小间隔；
    总发送闸门由调用方（路由/调度器）在外层拦。
    """
    if not policy.followup_enabled or int(policy.followup_daily_cap) <= 0:
        return {"ok": False, "http": 409, "kind": "followup_off"}
    text = str(text or "").strip()
    if opener_block_reason(text):
        return {"ok": False, "http": 400, "kind": "text"}
    block = hold_block_reason(store, account_id, now)
    if block:
        return {"ok": False, "http": 409, "kind": block}
    if hours_block_reason(policy, now, hour):
        return {"ok": False, "http": 409, "kind": "hours"}
    if store.count_followups_since(account_id, since_ts) >= int(policy.followup_daily_cap):
        return {"ok": False, "http": 409, "kind": "followup_cap"}
    last = max(store.last_outreach_sent_ts(account_id), store.last_followup_ts(account_id))
    min_gap_sec = float(policy.min_gap_sec)
    if last and float(now) < last + min_gap_sec:
        return {"ok": False, "http": 409, "kind": "gap",
                "gap_wait_sec": int(last + min_gap_sec - float(now))}
    row = store.get_member(group_id, user_id)
    if (row is None or str(row.get("outreach_state") or "") != OUTREACH_SENT
            or float(row.get("followup_at") or 0.0) > 0
            or str(row.get("outreach_account_id") or "") != str(account_id)
            or str(row.get("outreach_error") or "") == "stop_contact"):
        return {"ok": False, "http": 409, "kind": "followup_state"}
    if float(row.get("outreach_at") or 0.0) > followup_due_before(policy, now):
        return {"ok": False, "http": 409, "kind": "followup_early"}
    ah = str(row.get("access_hash") or "").strip()
    if not ah:
        return {"ok": False, "http": 409, "kind": "followup_state"}
    if not store.claim_followup(group_id, user_id, now):
        return {"ok": False, "http": 409, "kind": "followup_state"}
    return {"ok": True, "http": 200, "kind": "ready",
            "user_id": int(row["user_id"]), "access_hash": int(ah), "text": text}


def finalize_followup(store: Any, *, account_id: str, group_id: str, user_id: str,
                      now: float, text: str, result: Dict[str, Any]) -> Dict[str, Any]:
    """跟进结果落库。成功记文案；flood 停号并还坑；对方不可达标 blocked；其它还坑。"""
    kind = str((result or {}).get("kind") or "retryable")
    if (result or {}).get("ok"):
        store.record_followup_text(group_id, user_id, text)
        mirror_outbound(store, account_id=account_id, group_id=group_id, user_id=user_id,
                        text=text, result=result, now=now)
        return {"ok": True, "http": 200, "kind": "sent"}
    if kind == "flood":
        store.set_hold(account_id, flood_until=float(now) + FLOOD_HOLD_SEC,
                       reason="peer_flood", last_flood_at=float(now))
        store.unclaim_followup(group_id, user_id)
        return {"ok": False, "http": 200, "kind": "flood"}
    if kind in ("privacy", "deactivated", "peer_invalid"):
        store.unclaim_followup(group_id, user_id)
        store.cas_outreach(group_id, user_id, expect_states=(OUTREACH_SENT,),
                           new_state=OUTREACH_BLOCKED, account_id=account_id, error=kind,
                           now=now)
        return {"ok": False, "http": 200, "kind": kind}
    store.unclaim_followup(group_id, user_id)
    return {"ok": False, "http": 200, "kind": kind}


async def deliver_outreach(client: Any, *, user_id: int, access_hash: int,
                           text: str) -> Dict[str, Any]:
    """在该号自己的 pyrogram 循环上发一条。失败只回种类，不回异常原文（可能含 peer）。

    走 raw ``messages.SendMessage``：高层 ``send_message`` 会先拿 chat_id 查 session 缓存，
    传 InputPeerUser 对象进去直接 SQLite 绑参失败（pyrogram 2.x 实测），传 int 又依赖缓存里有这个人。
    """
    try:
        from pyrogram.raw.functions.messages import SendMessage
        from pyrogram.raw.types import InputPeerUser
    except Exception:
        return {"ok": False, "kind": "retryable"}
    try:
        peer = InputPeerUser(user_id=int(user_id), access_hash=int(access_hash))
        r = await client.invoke(SendMessage(peer=peer, message=text, random_id=client.rnd_id()))
        out: Dict[str, Any] = {"ok": True, "kind": "sent"}
        # 消息 id / 时间带回去给收件箱镜像：和轮询兜底的 mirror_outgoing 同 id 落同键，不重复
        mid, ts = _sent_id_and_ts(r)
        if mid is not None:
            out["msg_id"] = str(mid)
        if ts:
            out["ts"] = ts
        return out
    except Exception as exc:  # noqa: BLE001 —— 分桶后再决定停号还是停人
        kind = classify_send_error(exc)
        logger.info("[gm_outreach] 开口未发出 kind=%s err=%s", kind, type(exc).__name__)
        return {"ok": False, "kind": kind}


def _sent_id_and_ts(r: Any) -> Tuple[Optional[int], float]:
    """raw SendMessage 的返回（UpdateShortSentMessage 或 Updates）里取消息 id 和发送时间。"""
    mid = getattr(r, "id", None)
    date = getattr(r, "date", None)
    for u in getattr(r, "updates", None) or ():
        msg = getattr(u, "message", None)
        if msg is not None and getattr(msg, "id", None) is not None:
            mid, date = msg.id, getattr(msg, "date", date)
            break
        if mid is None and type(u).__name__ == "UpdateMessageID":
            mid = getattr(u, "id", None)
    try:
        ts = float(date.timestamp() if hasattr(date, "timestamp") else (date or 0))
    except Exception:
        ts = 0.0
    return mid, ts


def member_display_name(member: Dict[str, Any]) -> str:
    first = str(member.get("first_name") or "").strip()
    last = str(member.get("last_name") or "").strip()
    name = (first + " " + last).strip()
    return name or str(member.get("username") or "").strip() or str(member.get("user_id") or "")


def mirror_outbound(store: Any, *, account_id: str, group_id: str, user_id: str,
                    text: str, result: Dict[str, Any], now: float) -> bool:
    """把刚发出去的开口 / 跟进镜像进统一收件箱（``direction="out"``）。

    为什么必须有：开口是用 pyrogram 直发的，默认不经收件箱；对方回话时回复链看到的是一条
    没有上文的入站，AI 会答非所问。镜像后会话里第一句就是我们说的那句，人设、话题都接得上。
    sink 没注册（单测 / 未接线）或任何异常 → False，不影响发送结果。
    """
    try:
        row = store.get_member(group_id, user_id) or {}
        body = str(text or "").strip() or str(row.get("opener_text") or "").strip()
        if not body:
            return False
        from src.integrations.protocol_bridge import emit_incoming, make_message
        ts = float((result or {}).get("ts") or now)
        emit_incoming(make_message(
            platform="telegram", account_id=str(account_id), chat_key=str(user_id),
            name=member_display_name(row), text=body, ts=ts,
            msg_id=str((result or {}).get("msg_id") or ""), direction="out",
            username=str(row.get("username") or ""),
            source={"chat_type": "private", "origin": "group_outreach"},
        ))
        return True
    except Exception:
        logger.debug("[gm_outreach] 出站镜像失败", exc_info=True)
        return False


def strip_access_hash(member: Dict[str, Any]) -> Dict[str, Any]:
    d = dict(member)
    d.pop("access_hash", None)
    return d


def note_inbound_reply(msg: Dict[str, Any], store: Any = None) -> int:
    """收件箱入站钩子：Telegram 私聊里对方回话了 → 对应开口行 sent → replied。

    只认 telegram、入站、非回填、正 chat_id（私聊对端就是 user_id；群是负数）。
    store 没配置就什么都不做。任何异常都吞——这是收件箱热路径的旁支。
    """
    try:
        m = msg if isinstance(msg, dict) else {}
        if str(m.get("platform") or "").lower() != "telegram":
            return 0
        if str(m.get("direction") or "in") == "out" or m.get("backfill"):
            return 0
        chat_key = str(m.get("chat_key") or "").strip()
        account_id = str(m.get("account_id") or "").strip()
        if not chat_key.isdigit() or not account_id:
            return 0
        if store is None:
            from src.companion.group_members_store import get_group_members_store
            store = get_group_members_store()
        if store is None:
            return 0
        text = str(m.get("text") or m.get("content") or "")
        try:
            ts = float(m.get("ts") or 0.0) or None
        except (TypeError, ValueError):
            ts = None
        n = int(store.mark_outreach_replied(account_id, chat_key, now=ts, text=text) or 0)
        if n:
            logger.info("[gm_outreach] 开口有回音 account=%s user=%s", account_id, chat_key)
        # 对方明确说「别发了」→ 这个人跨群跨号都不再碰（和回复链同一套判定）
        if text and is_stop_contact(text):
            k = int(store.mark_outreach_stop_contact(chat_key) or 0)
            if k:
                logger.info("[gm_outreach] 对方拒绝联系 user=%s rows=%d", chat_key, k)
        return n
    except Exception:
        logger.debug("[gm_outreach] 入站回音标记失败", exc_info=True)
        return 0


def outreach_context_note(account_id: str, user_id: str, store: Any = None) -> str:
    """同群开口来的会话 → 一句目标背景（目标块【背景】只留 80 字）：在哪个群认识、TA在群里说过什么、
    我们先开的口。让回复链知道「我们怎么认识的」，别装老熟人也别一上来推销。非开口会话 → ""。"""
    try:
        if store is None:
            from src.companion.group_members_store import get_group_members_store
            store = get_group_members_store()
        if store is None or not hasattr(store, "get_outreach_contact"):
            return ""
        row = store.get_outreach_contact(account_id, user_id)
        if not row:
            return ""
        if str(row.get("outreach_error") or "") == "inbound_first":
            first = "TA先私聊来找你"
        else:
            first = "你先私聊打了招呼、TA回了"
        title = " ".join(str(row.get("group_title") or "").split())[:16]
        where = f"你们同在「{title}」群" if title else "你们在同一个群"
        said = " ".join(str(row.get("last_msg_text") or "").split())[:18]
        heard = f"，TA在群里说过「{said}」" if said else ""
        return f"同群开口：{where}{heard}；{first}，先接话熟络，别急着推"
    except Exception:
        logger.debug("[gm_outreach] 开口背景生成失败", exc_info=True)
        return ""


STALLED_HANDOFF_REASON = "outreach_stalled"


def flag_stalled_in_inbox(inbox: Any, member: Dict[str, Any], *, now: float) -> bool:
    """「回了但没接上」→ 给收件箱会话打「需人工」（原因 outreach_stalled），进驾驶舱介入队列 /
    工作台「需人工」筛选，坐席不用来开口页也看得见。幂等（标在场即跳过）；inbox 缺席 → False。"""
    if inbox is None:
        return False
    try:
        from src.integrations.protocol_autoreply import tag_needs_human
        payload = {"platform": "telegram",
                   "account_id": str(member.get("outreach_account_id") or ""),
                   "chat_key": str(member.get("user_id") or "")}
        if not payload["account_id"] or not payload["chat_key"]:
            return False
        return bool(tag_needs_human(inbox, payload, reason=STALLED_HANDOFF_REASON,
                                    source="system", now=now))
    except Exception:
        logger.debug("[gm_outreach] 打需人工失败", exc_info=True)
        return False


def unflag_stalled_in_inbox(inbox: Any, conversation_id: str) -> bool:
    """接上话了 → 只摘**我们自己打的**那个「需人工」（原因必须是 outreach_stalled）；
    别的原因（隐私 / 承诺 / 拒绝联系）打的标不动。"""
    cid = str(conversation_id or "").strip()
    if inbox is None or not cid:
        return False
    try:
        meta = dict(inbox.get_handoff_meta(cid) or {}) if hasattr(inbox, "get_handoff_meta") else {}
        if str(meta.get("reason") or "") != STALLED_HANDOFF_REASON:
            return False
        from src.integrations.protocol_autoreply import clear_needs_human
        return bool(clear_needs_human(inbox, cid, actor="system:outreach_answered"))
    except Exception:
        logger.debug("[gm_outreach] 摘需人工失败", exc_info=True)
        return False


def note_outbound_answer(msg: Dict[str, Any], store: Any = None, inbox: Any = None) -> int:
    """收件箱出站钩子：对方回过之后我方回话（AI 自动回 / 坐席 / 手机镜像）→ 首句记 answered_at、
    每句推进 last_out_at，并摘掉「没接上 / 接上后又断了」打的需人工标。返回首次接上的行数。

    只认 telegram、出站、正 chat_id。开场 / 跟进自己的镜像发生在 sent 态，store 侧天然不算。
    任何异常都吞。
    """
    try:
        m = msg if isinstance(msg, dict) else {}
        if str(m.get("platform") or "").lower() != "telegram":
            return 0
        if str(m.get("direction") or "in") != "out":
            return 0
        chat_key = str(m.get("chat_key") or "").strip()
        account_id = str(m.get("account_id") or "").strip()
        if not chat_key.isdigit() or not account_id:
            return 0
        if store is None:
            from src.companion.group_members_store import get_group_members_store
            store = get_group_members_store()
        if store is None:
            return 0
        try:
            ts = float(m.get("ts") or 0.0) or time.time()
        except (TypeError, ValueError):
            ts = time.time()
        n, flagged = store.record_outreach_outbound(account_id, chat_key, ts)
        if n:
            logger.info("[gm_outreach] 回音已接上 account=%s user=%s", account_id, chat_key)
        if n or flagged:
            if inbox is None:
                from src.integrations.protocol_bridge import get_inbox_store
                inbox = get_inbox_store()
            unflag_stalled_in_inbox(inbox, conversation_id_for(
                {"outreach_account_id": account_id, "user_id": chat_key}))
        return n
    except Exception:
        logger.debug("[gm_outreach] 出站接话标记失败", exc_info=True)
        return 0


def is_stop_contact(text: str) -> bool:
    """「别再发了 / stop messaging me」这类拒绝。复用回复链的判定，取不到就不判。"""
    try:
        from src.ai.chat_assistant_service import _stop_contact_hit
        return bool(_stop_contact_hit(str(text or "")))
    except Exception:
        return False


def attach_won(stats: Dict[str, Any], contacted: Sequence[Dict[str, Any]],
               won: Dict[tuple, Dict[str, Any]]) -> Dict[str, Any]:
    """把成交（目标台账 done=order:/manual:）挂回开口统计：漏斗多一级、切入方式切片带成交数、
    外加按群拆分和最近成交名单。``won`` 来自 ``GoalStore.won_chats``，键 (账号, 对方 user_id)。

    只认开口之后才成交的（done_at ≥ outreach_at），之前就买过的不算这次开口的功劳。"""
    hits: List[Dict[str, Any]] = []
    for m in contacted or ():
        w = won.get((str(m.get("outreach_account_id") or ""), str(m.get("user_id") or "")))
        if w and float(w.get("done_at") or 0) >= float(m.get("outreach_at") or 0):
            hits.append((m, w))
    by_variant: Dict[str, int] = {}
    by_group: Dict[str, Dict[str, Any]] = {}
    for m, w in hits:
        v = str(m.get("opener_variant") or "") or "-"
        by_variant[v] = by_variant.get(v, 0) + 1
        g = by_group.setdefault(str(m.get("group_id") or ""), {
            "group_id": str(m.get("group_id") or ""), "group_title": str(m.get("group_title") or ""),
            "replied": 0, "won": 0})
        g["won"] += 1
    for m in contacted or ():
        gid = str(m.get("group_id") or "")
        if gid in by_group:
            by_group[gid]["replied"] += 1
    for row in stats.get("by_variant") or []:
        row["won"] = by_variant.get(str(row.get("key") or ""), 0)
    funnel = stats.setdefault("funnel", {})
    funnel["won"] = len(hits)
    sent = int(funnel.get("sent") or 0)
    stats["won"] = {
        "total": len(hits),
        "manual": sum(1 for _, w in hits if w.get("manual")),
        "rate": (len(hits) / sent) if sent else None,
        "by_group": sorted(by_group.values(), key=lambda g: (-g["won"], g["group_title"])),
        "recent": [{"user_id": str(m.get("user_id") or ""), "username": str(m.get("username") or ""),
                    "name": str(m.get("first_name") or ""), "group_title": str(m.get("group_title") or ""),
                    "account_id": str(m.get("outreach_account_id") or ""),
                    "variant": str(m.get("opener_variant") or ""), "done_at": float(w.get("done_at") or 0),
                    "manual": bool(w.get("manual"))}
                   for m, w in sorted(hits, key=lambda x: -float(x[1].get("done_at") or 0))[:20]],
    }
    return stats


__all__ = [
    "OUTREACH_CAP_DEFAULT", "OUTREACH_CAP_MIN", "OUTREACH_CAP_MAX",
    "OUTREACH_MIN_GAP_SEC", "FLOOD_HOLD_SEC",
    "OUTREACH_WARMUP_START", "OUTREACH_WARMUP_DAYS", "OUTREACH_HOURS",
    "OutreachPolicy", "effective_outreach_cap", "hours_block_reason", "local_hour",
    "clamp_outreach_cap", "suggest_opener", "opener_block_reason",
    "classify_send_error", "hold_block_reason", "select_candidates",
    "public_member", "build_preview", "enqueue_today", "explain_empty_pool",
    "prepare_release", "finalize_release", "deliver_outreach",
    "strip_access_hash", "note_inbound_reply", "is_stop_contact",
    "mirror_outbound", "member_display_name", "conversation_id_for",
    "note_outbound_answer", "stalled_before", "STALLED_AFTER_HOURS",
    "flag_stalled_in_inbox", "unflag_stalled_in_inbox", "STALLED_HANDOFF_REASON",
    "AUTO_MIN_AGE_DAYS", "AUTO_FLOOD_LOOKBACK_DAYS", "AUTO_MIN_REPLY_RATE", "AUTO_MIN_SAMPLE",
    "FOLLOWUP_AFTER_HOURS", "FOLLOWUP_CLOSE_AFTER_HOURS", "FOLLOWUP_DAILY_CAP",
    "auto_mode_block_reason", "followup_due_before", "followup_close_before",
    "prepare_followup", "finalize_followup", "attach_won",
]
