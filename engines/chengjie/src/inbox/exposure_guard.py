# -*- coding: utf-8 -*-
"""识破守卫（Q-1 D · #264 #269 · 2026-09-10）。

事故：K24YJ2 客户说「this is like the 20th time you asked」「are you a bot」，我方下一轮**继续按目标
追问**，还出了「I promise I'm not a robot」这种自证句——越辩越像。识破词出现时唯一正确的动作是：
停手（目标全停 24h）、交人（needs_human + 打标「疑似识破」）、这一轮只出**一句轻松不辩解的挽回**，
且任何时候出站都不许出现自证句。

分工（不动 persona_reply / autosend_worker）：
- **入站 · 目标侧**：``goals.service.build_block_for_chat`` 拿到 inbound_text 即 :func:`detect_exposure`，
  命中 → :func:`pause_goal`（``params._paused_until = now+24h``、事件 ``exposure_pause``）并不注入；
  之后每轮 :func:`goal_paused_until` > now → 不注入（reason ``goals_paused``）。
- **入站 · 会话侧**：:func:`handle_inbound` → needs_human（``protocol_autoreply.tag_needs_human``
  reason=exposure）+ 会话标签 :data:`EXPOSURE_TAG` + KV 标记 ``exposure_hit:<conv>``（幂等：同一条
  入站只处理一次）。目标侧和出站侧都调它，谁先到谁做。
- **出站**：:func:`guard_outbound` 挂 ``outbound_humanize.apply_draft_humanize``（claim_guard 之前）：
  ① 标记未消费（且 ≤ :data:`RECOVER_WINDOW_SEC`）→ 整稿换成一句挽回（:func:`recovery_line`），消费标记；
  ② 任何时候删自证句（:data:`SELF_PROOF_PATTERNS`），删空 → 挽回句。

日志 ``[exposure] conv=… hit=… action=pause_goals|needs_human|recover|strip_selfproof``。绝不抛。
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

PAUSE_SEC = 24 * 3600.0
RECOVER_WINDOW_SEC = 6 * 3600.0
EXPOSURE_TAG = "疑似识破"
KV_PREFIX = "exposure_hit:"
GOAL_EVENT_PAUSE = "exposure_pause"
PARAM_PAUSED_UNTIL = "_paused_until"
PARAM_PAUSED_REASON = "_paused_reason"

# 识破词（保守：只收「明确指认」；「you told me about your dog」这类不收）
EXPOSURE_PATTERNS: List["re.Pattern[str]"] = [re.compile(p, re.I) for p in (
    r"\b(you|u)('?re| are)\s+(a\s+|an\s+)?(bot|robot|chat\s*bot|ai|a\.i\.)\b",
    r"\b(are|r)\s+(you|u)\s+(a\s+|an\s+)?(bot|robot|chat\s*bot|ai|a\.i\.|real)\b",
    r"\b(is this|am i talking to|talking to)\s+(a\s+)?(bot|robot|chat\s*bot|ai|machine)\b",
    r"\b(you|u)\s+sound\s+like\s+(a\s+)?(bot|robot|ai|script)\b",
    r"\bchat\s*gpt\b",
    r"\b(you|u)\s+(already|just)\s+asked\b",
    r"\basked\s+me\s+(that|this|it)\s+(already|before|again|earlier|twice)\b",
    r"\basking\s+me\s+(that\s+|this\s+|the same\s+)?(again|twice)\b",
    r"\b(keep|keeps)\s+asking\s+(me\s+)?(the\s+same|that|this)\b",
    r"\bsame\s+question\s+(again|twice)\b",
    r"\b(i|i've)\s+(already\s+)?told\s+(you|u)\s+(that|this|already|before|earlier|yesterday|twice)\b",
    r"\b(you|u)\s+told\s+me\s+(that|this)\s+(already|before|earlier|twice)\b",
    r"\b\d{1,3}(st|nd|rd|th)\s+time\b",
    r"\b(hundredth|tenth|fifth|third)\s+time\b",
    r"\b(you|u)\s+(don'?t|never)\s+(remember|listen)\b",
    r"机器人吧|是机器人|是不是机器人|是个机器人|是ai吧|是不是ai|是个ai|人工智能吧|不是真人|是真人吗|真人还是",
    r"你又问|又问一遍|又问我|问过了|都问过|问了好几遍|问了几遍|问第.{1,2}遍|第.{1,2}次问",
    r"我(都|已经|刚|刚才|昨天)?说过了|我不是说了|跟你说过|告诉过你|说了多少遍",
)]

# 出站自证句黑名单（句级删除）
SELF_PROOF_PATTERNS: List["re.Pattern[str]"] = [re.compile(p, re.I) for p in (
    r"\b(i|i'm|i am|im)\s+(promise\s+)?(i'?m\s+|i am\s+)?not\s+(a\s+|an\s+)?(bot|robot|chat\s*bot|ai|a\.i\.|machine|script)\b",
    r"\b(i|i'm|i am|im)\s+(a\s+)?(real|actual|genuine)\s+(person|human|girl|guy|woman|man)\b",
    r"\b(i'm|i am|im)\s+(100%\s+|totally\s+|actually\s+)?(real|human)\s*(lol|haha|btw|i swear|i promise)?\s*[.!,]*\s*$",
    r"\bnot\s+an?\s+(ai|bot|robot)\b.*\b(promise|swear|honest)",
    r"\b(promise|swear)\b.*\bnot\s+(a\s+)?(bot|robot|ai)\b",
    r"\breal\s+(person|human)\s+(here|typing|talking)\b",
    r"我不是机器人|我是真人|真人在|我不是ai|我不是人工智能|我是人不是|我真的是人|我发誓.{0,6}(真人|不是机器)|我保证.{0,6}(真人|不是机器)",
)]

_RECOVERY: Dict[str, List[str]] = {
    "en": [
        "lol sorry, long day. you did tell me",
        "haha my bad, brain's fried today",
        "oops yeah you did say that. ignore me lol",
        "fair, that's on me. long week",
    ],
    "zh": [
        "哈哈抱歉，今天脑子有点糊，你确实说过",
        "对对你说过，我这记性哈哈",
        "哈哈是我不对，忙了一天有点飘",
        "啊对，你说过，当我没问",
    ],
    "ja": [
        "あ、ごめん、今日ちょっと疲れてて。言ってたね",
        "そうだった、聞いてたね。ごめんごめん",
        "うわ、私のミス。頭が回ってない笑",
    ],
}

_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_KANA_RE = re.compile(r"[\u3040-\u30ff]")


def _lang(lang: Any, text: str) -> str:
    s = str(lang or "").strip().lower().replace("_", "-")
    if s and s != "unknown":
        return "zh" if s.startswith("zh") else s.split("-", 1)[0]
    if _KANA_RE.search(text):
        return "ja"
    if _CJK_RE.search(text):
        return "zh"
    return "en"


def _now(now: Optional[float]) -> float:
    return float(now if now is not None else time.time())


def detect_exposure(text: str) -> str:
    """命中的识破短语（原文片段）或 ""。纯函数。"""
    s = str(text or "")
    if not s.strip():
        return ""
    low = s.lower()
    for pat in EXPOSURE_PATTERNS:
        m = pat.search(low)
        if m:
            return s[m.start():m.end()].strip()[:60]
    return ""


def recovery_line(lang: str, seed: str = "") -> str:
    pool = _RECOVERY.get(lang) or _RECOVERY["en"]
    if not seed:
        return pool[0]
    h = int(hashlib.sha1(str(seed).encode("utf-8")).hexdigest()[:8], 16)
    return pool[h % len(pool)]


# ---------------------------------------------------------------- 目标侧

def goal_paused_until(goal: Any) -> float:
    try:
        return float(((goal or {}).get("params") or {}).get(PARAM_PAUSED_UNTIL) or 0)
    except (TypeError, ValueError, AttributeError):
        return 0.0


def pause_goal(store: Any, goal: Dict[str, Any], *, hit: str = "", now: Optional[float] = None,
               conversation_id: str = "", duration: float = PAUSE_SEC) -> float:
    """目标 ``params._paused_until = now + 24h``（+ 事件 ``exposure_pause``）。返回 paused_until。"""
    n = _now(now)
    until = n + float(duration)
    gid = str((goal or {}).get("goal_id") or "")
    params = dict((goal or {}).get("params") or {})
    params[PARAM_PAUSED_UNTIL] = until
    params[PARAM_PAUSED_REASON] = "exposure"
    try:
        if store is not None and gid:
            store.update_goal_fields(gid, params=params)
            try:
                store.add_event(gid, GOAL_EVENT_PAUSE, f"hit={hit[:40]} until={int(until)}",
                                conversation_id=conversation_id, now=n)
            except Exception:
                pass
        if isinstance(goal, dict):
            goal["params"] = params
    except Exception:
        logger.debug("[exposure] pause_goal 失败（忽略）", exc_info=True)
    logger.warning("[exposure] conv=%s hit=%r action=pause_goals goal=%s until=%s",
                   conversation_id or "-", hit[:40], gid[:12],
                   time.strftime("%m-%d %H:%M", time.localtime(until)))
    return until


# ---------------------------------------------------------------- 会话侧（KV 标记 + needs_human + 标签）

def _kv_get(store: Any, cid: str) -> Dict[str, Any]:
    try:
        raw = store.get_app_setting(KV_PREFIX + cid, "") if store is not None else ""
        d = json.loads(raw) if raw else {}
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _kv_set(store: Any, cid: str, d: Dict[str, Any]) -> None:
    try:
        if store is not None:
            store.set_app_setting(KV_PREFIX + cid, json.dumps(d, ensure_ascii=False),
                                  updated_by="exposure_guard")
    except Exception:
        logger.debug("[exposure] kv set 失败（忽略）", exc_info=True)


def _split_cid(cid: str) -> Dict[str, str]:
    parts = str(cid or "").split(":", 2)
    if len(parts) == 3:
        return {"platform": parts[0], "account_id": parts[1], "chat_key": parts[2]}
    return {}


def handle_inbound(text: str, *, conversation_id: str, inbox_store: Any = None,
                   inbound_ts: float = 0.0, now: Optional[float] = None) -> Dict[str, Any]:
    """入站识破处理（幂等）：needs_human + 标签「疑似识破」+ KV 标记（供出站挽回）。

    返回 ``{hit, handled, already}``；``handled`` 只在本次真做了动作时 True。"""
    hit = detect_exposure(text)
    cid = str(conversation_id or "").strip()
    if not hit or not cid:
        return {"hit": hit, "handled": False, "already": False}
    n = _now(now)
    st = inbox_store
    if st is None:
        try:
            from src.integrations.protocol_bridge import get_inbox_store
            st = get_inbox_store()
        except Exception:
            st = None
    mark = _kv_get(st, cid)
    key = f"{float(inbound_ts or 0):.0f}|{hit}"
    if mark and str(mark.get("key") or "") == key:
        return {"hit": hit, "handled": False, "already": True}
    _kv_set(st, cid, {"key": key, "ts": n, "hit": hit, "consumed": False})
    if st is not None:
        try:
            from src.integrations.protocol_autoreply import tag_needs_human
            trip = _split_cid(cid)
            if trip:
                tag_needs_human(st, trip, reason="exposure", source="exposure_guard", now=n)
        except Exception:
            logger.debug("[exposure] tag_needs_human 失败（忽略）", exc_info=True)
        try:
            tags = list(st.get_conv_tags(cid) or [])
            if EXPOSURE_TAG not in tags:
                tags.append(EXPOSURE_TAG)
                st.set_conv_tags(cid, tags)
        except Exception:
            logger.debug("[exposure] 打标失败（忽略）", exc_info=True)
    logger.warning("[exposure] conv=%s hit=%r action=needs_human tag=%s", cid, hit[:40], EXPOSURE_TAG)
    return {"hit": hit, "handled": True, "already": False}


# ---------------------------------------------------------------- 出站

def _split_sentences(text: str, lang: str) -> List[str]:
    t = str(text or "").strip()
    if not t:
        return []
    if lang in ("zh", "ja"):
        parts = re.split(r"(?<=[。！？!?；;\n])\s*", t)
    else:
        parts = re.split(r"(?<=[.!?])\s+(?=[A-Za-z0-9\"'(\[¿¡])|\n+", t)
    return [p for p in parts if p and p.strip()]


def find_self_proof(text: str) -> List[str]:
    out: List[str] = []
    for s in _split_sentences(text, _lang("", text)):
        if any(p.search(s) for p in SELF_PROOF_PATTERNS):
            out.append(s.strip())
    return out


def strip_self_proof(text: str, lang: str = "") -> Tuple[str, List[str]]:
    lg = _lang(lang, text)
    sents = _split_sentences(text, lg)
    hits = [s for s in sents if any(p.search(s) for p in SELF_PROOF_PATTERNS)]
    if not hits:
        return str(text or ""), []
    keep = [s for s in sents if s not in hits]
    sep = "" if lg in ("zh", "ja") else " "
    return sep.join(p.strip() for p in keep).strip(), [h[:80] for h in hits]


def guard_outbound(text: str, *, conversation_id: str = "", lang: str = "", inbox_store: Any = None,
                   last_inbound: Optional[Dict[str, Any]] = None,
                   now: Optional[float] = None) -> Tuple[str, Dict[str, Any]]:
    """出站：① 最近一条入站命中识破 → :func:`handle_inbound`；② 标记未消费 → 整稿换挽回句；
    ③ 删自证句。返回 ``(text, {action: clean|recover|strip_selfproof, hit, stripped})``。绝不抛。"""
    rep: Dict[str, Any] = {"action": "clean", "hit": "", "stripped": []}
    src = str(text or "")
    if not src.strip():
        return src, rep
    try:
        cid = str(conversation_id or "").strip()
        lg = _lang(lang, src)
        n = _now(now)
        st = inbox_store
        if st is None and cid:
            try:
                from src.integrations.protocol_bridge import get_inbox_store
                st = get_inbox_store()
            except Exception:
                st = None
        # ① 最近入站（调用方可直传；否则从 store 取）
        li = last_inbound
        if li is None and st is not None and cid and hasattr(st, "list_recent_messages"):
            try:
                rows = st.list_recent_messages(cid, limit=6) or []
                for r in reversed(rows):
                    if isinstance(r, dict) and str(r.get("direction") or "") == "in":
                        li = r
                        break
            except Exception:
                li = None
        if isinstance(li, dict) and cid:
            try:
                handle_inbound(str(li.get("text") or ""), conversation_id=cid, inbox_store=st,
                               inbound_ts=float(li.get("ts") or 0), now=n)
            except Exception:
                pass
        # ② 未消费标记 → 挽回句
        mark = _kv_get(st, cid) if cid else {}
        if mark and not mark.get("consumed") and n - float(mark.get("ts") or 0) <= RECOVER_WINDOW_SEC:
            mark["consumed"] = True
            mark["consumed_ts"] = n
            _kv_set(st, cid, mark)
            line = recovery_line(lg, cid)
            rep.update({"action": "recover", "hit": str(mark.get("hit") or "")})
            logger.warning("[exposure] conv=%s hit=%r action=recover line=%r", cid or "-",
                           rep["hit"][:40], line)
            return line, rep
        # ③ 自证句黑名单
        out, hits = strip_self_proof(src, lg)
        if hits:
            rep["stripped"] = hits
            rep["action"] = "strip_selfproof"
            if not out:
                out = recovery_line(lg, cid)
            logger.warning("[exposure] conv=%s hit=%r action=strip_selfproof n=%d", cid or "-",
                           hits[0][:40], len(hits))
            return out, rep
        return src, rep
    except Exception:
        logger.debug("[exposure] guard_outbound 异常（原文放行）", exc_info=True)
        return src, {"action": "clean", "hit": "", "stripped": []}


__all__ = [
    "PAUSE_SEC", "RECOVER_WINDOW_SEC", "EXPOSURE_TAG", "KV_PREFIX", "GOAL_EVENT_PAUSE",
    "PARAM_PAUSED_UNTIL", "EXPOSURE_PATTERNS", "SELF_PROOF_PATTERNS",
    "detect_exposure", "recovery_line", "goal_paused_until", "pause_goal", "handle_inbound",
    "find_self_proof", "strip_self_proof", "guard_outbound",
]
