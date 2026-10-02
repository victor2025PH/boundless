"""群里接话 —— 先在群里公开回复 TA 说的那句，过几个小时再私聊。

为什么要有：冷私聊是封号风险最高、回复率最低的一步；设了隐私（只许联系人私信）的人私聊
永远到不了。先在群里帮上忙/接上话，是公开、有帮助的发言，不会被当骚扰；之后的私信顺着
「刚在群里回你那个」开口，对方知道你是谁。

边界（和私聊开口同一套纪律）：
- 只回这个号自己所在群里、近 N 小时的发言（回复挂在 TA 那条下面）；只回有意向分的人。
- 每号每天 5 条、每群每天 2 条、两条至少隔 10 分钟，只在开口时段内发；急停 / 风控熔断都拦。
- 只有坐席逐条点「发到群里」，不进调度器。AI 拟稿不推销、不带链接、不说「私聊我」。
- 群里发不出（被禁言 / 无发言权限）→ 这个号在这个群不再列人；慢速模式 → 还坑下次再发。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence

from src.companion.group_member_outreach import (
    _PITCH_MARKERS,
    FLOOD_HOLD_SEC,
    OutreachPolicy,
    hold_block_reason,
    hours_block_reason,
    public_member,
)

logger = logging.getLogger("ai_chat_assistant.group_member_gtouch")

GTOUCH_MAX_CHARS_ZH = 120
GTOUCH_MAX_CHARS_OTHER = 280
# 公开场合额外不许的：引人私聊 / @ 人（opener_block_reason 已管链接和加微信）
_GTOUCH_MARKERS = ("私聊我", "私信我", "私我", "找我聊", "联系我", "dm me", "pm me", "inbox me",
                   "message me", "@")


def gtouch_block_reason(text: Any) -> str:
    """空串 = 可以发到群里。否则：empty / too_long / pitch / dm_ask。"""
    s = str(text or "").strip()
    if not s:
        return "empty"
    if len(s) > GTOUCH_MAX_CHARS_OTHER:
        return "too_long"
    low = s.lower()
    for mark in _PITCH_MARKERS:
        if mark.lower() in low:
            return "pitch"
    for mark in _GTOUCH_MARKERS:
        if mark in low:
            return "dm_ask"
    return ""


def classify_group_send_error(exc: BaseException) -> str:
    """群发言错误分桶：flood 停号；slowmode 还坑；group_denied 这群不再接话；msg_gone 那条没了。"""
    blob = (type(exc).__name__ + " " + str(exc)).upper().replace("_", "")
    if "SLOWMODEWAIT" in blob:
        return "slowmode"
    if "PEERFLOOD" in blob or "FLOODWAIT" in blob:
        return "flood"
    for mark in ("CHATWRITEFORBIDDEN", "USERBANNEDINCHANNEL", "CHATADMINREQUIRED",
                 "CHATRESTRICTED", "CHANNELPRIVATE", "CHATSENDPLAINFORBIDDEN",
                 "CHATGUESTSENDFORBIDDEN", "USERNOTPARTICIPANT"):
        if mark in blob:
            return "group_denied"
    if "MSGIDINVALID" in blob or "MESSAGEIDINVALID" in blob or "REPLYMESSAGEIDINVALID" in blob:
        return "msg_gone"
    return "retryable"


def gtouch_preview(store: Any, account_id: str, *, now: float, since_ts: float,
                   policy: OutreachPolicy, intent: Optional[Dict[str, Sequence[str]]] = None,
                   limit: int = 30) -> Dict[str, Any]:
    """这个号今天能在群里接谁的话。只列有意向分的；私聊被隐私挡住的排前面。"""
    from src.companion.member_intent import member_intent
    store.reap_stale_gtouch(now)
    msg_since = float(now) - float(policy.gtouch_max_msg_age_hours) * 3600.0
    denied = set(store.gtouch_denied_groups(account_id))
    rows: List[Dict[str, Any]] = []
    seen = set()
    for m in store.list_gtouch_candidates(account_id, since_msg_ts=msg_since):
        uid = str(m.get("user_id") or "")
        if uid in seen or str(m.get("group_id") or "") in denied:
            continue
        score, hits = member_intent(m, intent)
        if score <= 0:
            continue
        seen.add(uid)
        row = public_member(m)
        row["intent"], row["intent_hits"] = score, hits
        row["dm_blocked"] = str(m.get("outreach_error") or "") == "privacy"
        rows.append(row)
    rows.sort(key=lambda r: (0 if r["dm_blocked"] else 1, -r["intent"], -r["last_msg_ts"]))
    used = store.count_gtouch_since(account_id, since_ts)
    last = store.last_gtouch_ts(account_id)
    gap_wait = max(0.0, last + float(policy.gtouch_min_gap_sec) - float(now)) if last else 0.0
    return {
        "account_id": str(account_id),
        "candidates": rows[:max(0, int(limit))],
        "cap": int(policy.gtouch_daily_cap),
        "group_cap": int(policy.gtouch_group_daily_cap),
        "used_today": used,
        "remaining": max(0, int(policy.gtouch_daily_cap) - used),
        "gap_wait_sec": int(gap_wait),
        "max_msg_age_hours": int(policy.gtouch_max_msg_age_hours),
        "dm_after_hours": int(policy.gtouch_dm_after_hours),
        "hold": hold_block_reason(store, account_id, now),
        "hours_ok": not hours_block_reason(policy, now),
        "denied_groups": sorted(denied),
        "sent": [public_member(m) for m in store.list_gtouch_sent_since(account_id, since_ts)],
        "intent_configured": bool(intent and intent.get("positive")),
    }


def prepare_gtouch(store: Any, *, account_id: str, group_id: str, user_id: str, text: str,
                   now: float, since_ts: float, policy: OutreachPolicy) -> Dict[str, Any]:
    """发到群里前的全部闸门 + 占坑。返回 {ok, http, kind, chat_id, reply_to, text}。"""
    store.reap_stale_gtouch(now)
    block = hold_block_reason(store, account_id, now)
    if block:
        return {"ok": False, "http": 409, "kind": block}
    if hours_block_reason(policy, now):
        return {"ok": False, "http": 409, "kind": "hours"}
    if int(policy.gtouch_daily_cap) <= 0 or \
            store.count_gtouch_since(account_id, since_ts) >= int(policy.gtouch_daily_cap):
        return {"ok": False, "http": 409, "kind": "gtouch_cap"}
    if store.count_gtouch_since(account_id, since_ts, group_id) >= int(policy.gtouch_group_daily_cap):
        return {"ok": False, "http": 409, "kind": "gtouch_group_cap"}
    last = store.last_gtouch_ts(account_id)
    if last and float(now) < last + float(policy.gtouch_min_gap_sec):
        return {"ok": False, "http": 409, "kind": "gap",
                "gap_wait_sec": int(last + float(policy.gtouch_min_gap_sec) - float(now))}
    if str(group_id) in set(store.gtouch_denied_groups(account_id)):
        return {"ok": False, "http": 409, "kind": "group_denied"}
    row = store.get_member(group_id, user_id)
    acct = str(account_id)
    if (row is None
            or acct not in (str(row.get("source_account_id") or ""), str(row.get("hash_account_id") or ""))
            or str(row.get("gtouch_state") or "") not in ("", "drafted")
            or str(row.get("outreach_error") or "") == "stop_contact"
            or not str(row.get("last_msg_id") or "").strip()
            or store.gtouched_elsewhere(user_id, group_id)):
        return {"ok": False, "http": 409, "kind": "gtouch_state"}
    msg_since = float(now) - float(policy.gtouch_max_msg_age_hours) * 3600.0
    if float(row.get("last_msg_ts") or 0.0) < msg_since:
        return {"ok": False, "http": 409, "kind": "gtouch_stale"}
    clean = " ".join(str(text or "").split())
    why = gtouch_block_reason(clean)
    if why:
        return {"ok": False, "http": 400, "kind": "gtouch_text_" + why}
    try:
        chat_id = int(str(group_id))
        reply_to = int(str(row.get("last_msg_id")))
    except (TypeError, ValueError):
        return {"ok": False, "http": 409, "kind": "gtouch_state"}
    if not store.claim_gtouch(group_id, user_id, acct, now):
        return {"ok": False, "http": 409, "kind": "gtouch_state"}
    return {"ok": True, "http": 200, "kind": "ready", "chat_id": chat_id,
            "reply_to": reply_to, "text": clean}


async def deliver_gtouch(client: Any, *, chat_id: int, reply_to: int, text: str) -> Dict[str, Any]:
    """在该号的 pyrogram 循环上回复到群里那条。失败只回种类。"""
    try:
        msg = await client.send_message(chat_id=int(chat_id), text=text,
                                        reply_to_message_id=int(reply_to))
        out: Dict[str, Any] = {"ok": True, "kind": "sent"}
        mid = getattr(msg, "id", None)
        if mid is not None:
            out["msg_id"] = str(mid)
        return out
    except Exception as exc:  # noqa: BLE001 —— 分桶后决定停号 / 停群 / 还坑
        kind = classify_group_send_error(exc)
        logger.info("[gm_gtouch] 群里接话未发出 kind=%s err=%s", kind, type(exc).__name__)
        return {"ok": False, "kind": kind}


def finalize_gtouch(store: Any, *, account_id: str, group_id: str, user_id: str, now: float,
                    text: str, result: Dict[str, Any]) -> Dict[str, Any]:
    kind = str((result or {}).get("kind") or "retryable")
    if (result or {}).get("ok"):
        store.finish_gtouch(group_id, user_id, state="sent", text=text, now=now)
        requeued = int(store.requeue_after_gtouch(user_id, account_id) or 0)
        logger.info("[gm_gtouch] 群里接话已发 account=%s group=%s user=%s requeued=%d",
                    account_id, group_id, user_id, requeued)
        return {"ok": True, "kind": "sent", "requeued": requeued}
    if kind == "flood":
        store.set_hold(account_id, flood_until=float(now) + FLOOD_HOLD_SEC,
                       reason="peer_flood", last_flood_at=float(now))
        store.finish_gtouch(group_id, user_id, state="drafted", error=kind)
    elif kind in ("group_denied", "msg_gone"):
        store.finish_gtouch(group_id, user_id, state="failed", error=kind)
    else:
        store.finish_gtouch(group_id, user_id, state="drafted", error=kind)
    return {"ok": False, "kind": kind}


def gtouch_prompt(*, persona_block: str, member: Dict[str, Any], lang: str,
                  public_ai: bool = False) -> str:
    from src.companion.group_member_opener import _LANG_NAMES, _identity_rule
    name = str(member.get("first_name") or member.get("username") or "TA").strip()
    group = str(member.get("group_title") or "").strip()
    said = " ".join(str(member.get("last_msg_text") or "").split())[:200]
    limit = GTOUCH_MAX_CHARS_ZH if lang == "zh" else GTOUCH_MAX_CHARS_OTHER
    parts: List[str] = []
    if persona_block:
        parts.append("你在 Telegram 上以下面这个人的身份说话，口吻、用词都要像TA：\n" + persona_block)
    parts.append("你在群「%s」里。群友 %s 刚说：「%s」" % (group or "-", name, said))
    parts.append("现在你要在群里**公开回复**TA这条（挂在TA那条下面，群里所有人都看得到）。"
                 "目标：真心帮上忙或接上话，让TA和群友觉得你是个靠谱、友好的群友。")
    rules = [
        "1–2 句，不超过 %d 个字符，用%s。" % (limit, _LANG_NAMES.get(lang, lang)),
        "直接回应TA说的内容：回答问题、给一个具体可行的小建议，或分享一句相关经验；没把握的别编。",
        "不推销，不提产品名和价格，不放链接，不说「私聊我」「加我」，不留任何联系方式，不 @ 任何人。",
        "群聊口吻，像群友随手回的，别像客服、别像广告。",
        _identity_rule(public_ai),
    ]
    parts.append("规则：\n" + "\n".join("- " + r for r in rules))
    parts.append("只输出回复本身，不要引号，不要解释。")
    return "\n\n".join(parts)


def postprocess_gtouch(raw: str, *, persona: Optional[Dict[str, Any]], lang: str) -> Dict[str, str]:
    from src.companion.group_member_opener import (
        _first_line,
        language_mismatch,
        persona_public_ai,
    )
    text = _first_line(raw)
    if not text:
        return {"text": "", "reason": "empty"}
    if persona:
        try:
            from src.utils.persona_guard import sanitize
            text, _hits = sanitize(text, persona)
            text = str(text or "").strip()
        except Exception:
            logger.debug("[gm_gtouch] persona_guard 跳过", exc_info=True)
    if not persona_public_ai(persona):
        try:
            from src.utils.persona_guard import matches_ai_self_identity
            if matches_ai_self_identity(text):
                return {"text": text, "reason": "ai_self_id"}
        except Exception:
            pass
    why = gtouch_block_reason(text)
    if why:
        return {"text": text, "reason": why}
    if len(text) > (GTOUCH_MAX_CHARS_ZH if lang == "zh" else GTOUCH_MAX_CHARS_OTHER):
        return {"text": text, "reason": "too_long"}
    if language_mismatch(text, lang):
        return {"text": text, "reason": "lang"}
    return {"text": text, "reason": ""}


async def compose_gtouch(ai: Any, *, member: Dict[str, Any], ctx: Dict[str, Any],
                         default_lang: str = "", attempts: int = 2) -> Dict[str, Any]:
    """AI 拟一条群里公开回复。返回 {text, reason}；拟不出 → text=''（群里不发模板话，坐席手写）。"""
    from src.companion.group_member_opener import member_lang, persona_public_ai
    lang = member_lang(member, default_lang)
    persona = ctx.get("persona") if isinstance(ctx, dict) else None
    if ai is None or not hasattr(ai, "chat"):
        return {"text": "", "reason": "no_ai", "lang": lang}
    last = ""
    for _ in range(max(1, int(attempts))):
        prompt = gtouch_prompt(persona_block=str(ctx.get("persona_block") or ""), member=member,
                               lang=lang, public_ai=persona_public_ai(persona))
        try:
            try:
                from src.ai.llm_purpose import purpose_scope
            except ImportError:  # pragma: no cover
                from contextlib import nullcontext as purpose_scope  # type: ignore
            with purpose_scope("customer_reply"):
                raw = await ai.chat(prompt)
        except Exception:
            logger.debug("[gm_gtouch] LLM 调用失败", exc_info=True)
            raw = ""
        got = postprocess_gtouch(str(raw or ""), persona=persona, lang=lang)
        if not got["reason"]:
            return {"text": got["text"], "reason": "", "lang": lang}
        last = got["reason"]
    return {"text": "", "reason": last, "lang": lang}


__all__ = [
    "GTOUCH_MAX_CHARS_ZH", "GTOUCH_MAX_CHARS_OTHER", "gtouch_block_reason",
    "classify_group_send_error", "gtouch_preview", "prepare_gtouch", "deliver_gtouch",
    "finalize_gtouch", "gtouch_prompt", "postprocess_gtouch", "compose_gtouch",
]
