"""Q-8 D（#264 #263）：出站**退场闸**（exit_claim）——AI 不许自己结束对话。

事故（B9D8NW / 7C7SV2 / R6YW5D）：客户连发夸赞，AI 四轮后自己 "get back to work" 退场。
``commitment_guard`` 只管「我等下发你 X」类承诺，"gotta go / talk later / 我先忙了" 这类
**自己下班**的句子畅通无阻；工作时段内、客户没告别、用户没开留白策略，AI 却替坐席收工。

规则（挂 ``outbound_humanize.apply_draft_humanize``，commitment_guard 之后、humanize 之前）：

- 检出退场句（zh / en 词表 :data:`EXIT_PATTERNS`）→ 三种**合法退场**放行：
  ① 班表到点（``inbox.work_schedule`` 已过下班点或 ``shift_end_grace_min`` 内即将下班）；
  ② 客户先告别（最近一条入站命中告别词）；
  ③ 用户开启留白策略（``inbox.exit_gate.allow_silence: true``）。
  合法退场 → 保留原句 + 补一句**钩子**「等我忙完给你发消息接着聊」+ 在关怀排一条真实后续
  （``care_schedule.add_scheduled_care`` topic_norm ``exit_followup:<conv>:<day>``，默认 3h 后到点，
  经派发器既有闸真发）。
- 否则 → **改写**：删掉退场句；剩余文本没有问句则补一句把话题转回对方的问题 / 话头
  （对方语言，按会话确定性轮换）。
- 每次检出落 stats ``exit_claim``（质检卡多一格）；每次检出一行
  ``[exit] conv=… allowed=0|1 reason=… hit=…``。

纯函数 + 只读 store；任何失败 → 原文放行，绝不抛。
"""
from __future__ import annotations

import logging
import re
import time
import zlib
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("src.inbox.exit_gate")

CFG_PATH = "inbox.exit_gate"
DEFAULT_FOLLOWUP_HOURS = 3.0
DEFAULT_SHIFT_END_GRACE_MIN = 20
FOLLOWUP_NORM_PREFIX = "exit_followup:"

#: 退场句（AI 自己结束对话）。英文按词边界；中文按子串。
#: 只认**第一人称退场**：「are you going back to work?」「you've got to go see it」这类
#: 对客户说的话不算（``(?<!you\s)`` / ``go`` 后不跟 see/check/try…）。
_GO = r"go\b(?!\s+(?:see|check|try|watch|visit|for|with|get\s+it|ahead))"
EXIT_PATTERNS: Tuple[re.Pattern, ...] = tuple(re.compile(p, re.I) for p in (
    r"(?<!you\s)(?<!you've\s)\b(?:i(?:'ve| have)?\s+)?got(?:ta)?\s+(?:to\s+)?(?:" + _GO
    + r"|run\b|head\s+out\b|get\s+going\b|get\s+back\s+to\s+work\b)",
    r"\bi\s+(?:have|need|got)\s+to\s+(?:" + _GO + r"|run\b|get\s+going\b|get\s+back\s+to\s+work\b|head\s+(?:out|off)\b)",
    r"\bi\s+should\s+(?:" + _GO + r"|get\s+going\b|get\s+back\s+to\s+work\b|head\s+(?:out|off)\b)",
    r"\bi(?:'m| am|'ll| will)?\s+(?:better\s+)?(?:get(?:ting)?|go(?:ing)?)\s+back\s+to\s+work\b",
    r"(?<!you\s)(?<!you\sshould\s)(?<!you\sneed\sto\s)(?<!you\shave\sto\s)\b(?:time\s+to\s+|better\s+|off\s+to\s+)?get\s+back\s+to\s+work\b",
    r"\bback\s+to\s+work\s+(?:now|for\s+me)\b",
    r"(?<!did\syou\s)(?<!you\s)\bhave\s+a\s+good\s+one\b",
    r"\b(?:talk|chat|catch)\s+(?:to\s+you\s+|with\s+you\s+|you\s+)?later\b",
    r"\bttyl\b",
    r"\bi(?:'m| am)\s+heading\s+(?:out|off)\b",
    r"\bheading\s+(?:out|off)\s+now\b",
    r"\bi(?:'m| am)\s+(?:heading\s+)?off\s+(?:now|for\s+(?:now|the\s+day|tonight))\b",
    r"\bi(?:'ll| will)\s+let\s+you\s+go\b",
    r"\b(?:signing|logging)\s+off\b",
    r"我(?:先|得|要|该)(?:去)?忙(?:了|一下|会儿|去了)?",
    r"(?:我)?先(?:不聊|这样|走|下|撤)了",
    r"(?:我)?(?:回去|去|回)工作(?:了|去了)",
    r"下次(?:再)?聊", r"改天(?:再)?聊", r"(?:晚点|待会|等会)(?:再)?聊",
    r"我(?:得|要)走了", r"(?:先)?不打扰你了",
))

#: 客户告别（合法退场条件②）
BYE_PATTERNS: Tuple[re.Pattern, ...] = tuple(re.compile(p, re.I) for p in (
    r"\b(?:bye|goodbye|good\s*night|nite|gn|gnight|see\s+ya|see\s+you|cya|ttyl)\b",
    r"\b(?:gotta|got\s+to|have\s+to|need\s+to)\s+(?:go|run|sleep)\b",
    r"\b(?:going|off)\s+to\s+(?:bed|sleep|work)\b",
    r"\btalk\s+(?:to\s+you\s+)?(?:later|tomorrow|soon)\b",
    r"晚安", r"拜拜", r"再见", r"我(?:先)?(?:去)?(?:睡|忙|走)了", r"先不聊了", r"下次聊", r"改天聊",
    r"去上班了", r"回去工作了",
))

_SENT_SPLIT = re.compile(r"(?<=[。！？])\s*|(?<=[.!?])\s+|\n+")
_CLAUSE_SPLIT = re.compile(r"\s*[，,;；]\s*")
_QUESTION_RE = re.compile(r"[?？]")

#: 改写时补的问题 / 话头（把话题转回对方；按会话确定性轮换）
TURN_BACK_LINES: Dict[str, Tuple[str, ...]] = {
    "en": (
        "so what does the rest of your day look like?",
        "wait, tell me more about that first — what happened next?",
        "what are you up to right now?",
        "ok but what did you end up doing after that?",
        "what's the best part of your day been so far?",
    ),
    "zh": (
        "对了，你今天接下来打算做什么？",
        "先别急，刚说的那个你再多讲讲？",
        "你现在在干嘛呢？",
        "那后来呢，你最后怎么处理的？",
        "你今天到现在最开心的是哪一段？",
    ),
}

#: 合法退场的钩子句（留下真实后续）
HOOK_LINES: Dict[str, Tuple[str, ...]] = {
    "en": (
        "I'll message you when I'm done and we pick this right back up, ok?",
        "let me get back to you in a bit — I still want to hear the rest.",
    ),
    "zh": (
        "等我忙完给你发消息，咱们接着聊～",
        "我待会回来找你，那件事还没听完呢。",
    ),
}


def _lang(lang: str, text: str) -> str:
    lg = str(lang or "").lower()
    if lg.startswith("zh"):
        return "zh"
    if lg.startswith("en"):
        return "en"
    return "zh" if re.search(r"[\u4e00-\u9fff]", str(text or "")) else "en"


def _pick(rows: Tuple[str, ...], seed: str) -> str:
    if not rows:
        return ""
    return rows[zlib.crc32(str(seed or "x").encode("utf-8", "ignore")) % len(rows)]


def detect_exit_claim(text: str) -> str:
    """命中的退场短语（原文片段）；无 → ""。"""
    s = str(text or "")
    if not s.strip():
        return ""
    for p in EXIT_PATTERNS:
        m = p.search(s)
        if m:
            return m.group(0)
    return ""


def is_goodbye(text: str) -> bool:
    s = str(text or "").strip()
    if not s:
        return False
    return any(p.search(s) for p in BYE_PATTERNS)


def split_sentences(text: str) -> List[str]:
    parts = [p.strip() for p in _SENT_SPLIT.split(str(text or "")) if p and p.strip()]
    return parts


def strip_exit_sentences(text: str) -> Tuple[str, List[str]]:
    """删掉含退场短语的句子；返回 ``(剩余文本, 被删句子)``。"""
    kept: List[str] = []
    removed: List[str] = []
    for s in split_sentences(text):
        if not detect_exit_claim(s):
            kept.append(s)
            continue
        removed.append(s)
        # 句内只砍退场分句：「我先忙了，你今天打算吃什么？」→ 留「你今天打算吃什么？」
        clauses = [c.strip() for c in _CLAUSE_SPLIT.split(s) if c and c.strip()]
        rest = [c for c in clauses if not detect_exit_claim(c)]
        if rest and len(rest) < len(clauses):
            joined = ("，" if re.search(r"[\u4e00-\u9fff]", s) else ", ").join(rest)
            if joined and joined[-1] not in ".!?。！？":
                joined += "。" if re.search(r"[\u4e00-\u9fff]", joined) else "."
            kept.append(joined)
    return " ".join(kept).strip(), removed


def resolve_cfg(cfg_root: Any) -> Dict[str, Any]:
    try:
        node = cfg_root if isinstance(cfg_root, dict) else {}
        for part in CFG_PATH.split("."):
            node = node.get(part) if isinstance(node, dict) else None
        return node if isinstance(node, dict) else {}
    except Exception:
        return {}


def _split_conv(conversation_id: str) -> Tuple[str, str, str]:
    parts = str(conversation_id or "").split(":", 2)
    if len(parts) == 3:
        return parts[0], parts[1], parts[2]
    return "", "", ""


def shift_ending(cfg_root: Any, platform: str, account_id: str, *, now: Optional[float] = None,
                 grace_min: int = DEFAULT_SHIFT_END_GRACE_MIN) -> bool:
    """合法退场①：班表开着且（已下班 或 ``grace_min`` 内即将下班）。班表关 / 账号豁免 → False。"""
    try:
        from src.inbox.work_hours_gate import schedule_state, work_schedule_cfg
        ws = work_schedule_cfg(cfg_root if isinstance(cfg_root, dict) else {})
        st = schedule_state(ws, platform, account_id, now_ts=now)
        if not st.get("gated"):
            return False
        if not st.get("in_hours", True):
            return True
        n = float(now if now is not None else time.time())
        nxt = float(st.get("next_change_ts") or 0)
        return bool(st.get("next_change_kind") == "close" and nxt > 0 and nxt - n <= max(0, grace_min) * 60)
    except Exception:
        return False


def last_inbound_text(inbox_store: Any, conversation_id: str) -> str:
    if inbox_store is None or not conversation_id or not hasattr(inbox_store, "list_recent_messages"):
        return ""
    try:
        rows = inbox_store.list_recent_messages(str(conversation_id), limit=6) or []
        for r in reversed(rows):
            if isinstance(r, dict) and str(r.get("direction") or "") == "in":
                return str(r.get("text") or "")
    except Exception:
        return ""
    return ""


def legit_exit_reason(*, cfg_root: Any, conversation_id: str, inbox_store: Any,
                      now: Optional[float] = None) -> str:
    """"" = 不许退场；否则 ``allow_silence`` / ``customer_bye`` / ``shift_end``。"""
    cfg = resolve_cfg(cfg_root)
    if bool(cfg.get("allow_silence", False)):
        return "allow_silence"
    if is_goodbye(last_inbound_text(inbox_store, conversation_id)):
        return "customer_bye"
    plat, acct, _ck = _split_conv(conversation_id)
    if plat and acct and shift_ending(
            cfg_root, plat, acct, now=now,
            grace_min=int(cfg.get("shift_end_grace_min", DEFAULT_SHIFT_END_GRACE_MIN) or 0)):
        return "shift_end"
    return ""


def _followup_topic(inbox_store: Any, conversation_id: str, lang: str) -> str:
    last = last_inbound_text(inbox_store, conversation_id).strip()
    if last and not is_goodbye(last) and len(last) >= 6:
        return last[:60]
    return "接着上次的话题聊" if lang == "zh" else "pick up where we left off"


def enqueue_followup(*, conversation_id: str, hook_text: str, lang: str, inbox_store: Any,
                     cfg_root: Any, now: Optional[float] = None, care_store: Any = None) -> Optional[int]:
    """合法退场 → 关怀排一条真实后续（默认 3h 后到点；同会话同日去重）。失败 → None。"""
    try:
        n = float(now if now is not None else time.time())
        plat, acct, ck = _split_conv(conversation_id)
        if not (plat and ck):
            return None
        st = care_store
        if st is None:
            from src.contacts.care_schedule import get_care_schedule_store
            st = get_care_schedule_store()
        cfg = resolve_cfg(cfg_root)
        try:
            hours = float(cfg.get("followup_hours", DEFAULT_FOLLOWUP_HOURS) or DEFAULT_FOLLOWUP_HOURS)
        except (TypeError, ValueError):
            hours = DEFAULT_FOLLOWUP_HOURS
        day = time.strftime("%Y%m%d", time.localtime(n))
        topic = _followup_topic(inbox_store, conversation_id, lang)
        return st.add_scheduled_care(
            contact_key=ck, due_at=n + max(0.25, hours) * 3600.0,
            topic=topic, topic_norm=f"{FOLLOWUP_NORM_PREFIX}{conversation_id}:{day}"[:64],
            platform=plat, account_id=acct, chat_key=ck, event_at=n,
            source_text=str(hook_text or "")[:200], sentiment="neutral", confidence=1.0,
            dedup_days=1.0)
    except Exception:
        logger.debug("[exit] enqueue_followup failed", exc_info=True)
        return None


def guard_exit(text: str, *, conversation_id: str = "", lang: str = "", cfg_root: Any = None,
               inbox_store: Any = None, now: Optional[float] = None,
               care_store: Any = None) -> Tuple[str, Dict[str, Any]]:
    """出站退场闸。返回 ``(text, {action: clean|rewrite|allow, hit, reason, removed, care_id})``。绝不抛。"""
    rep: Dict[str, Any] = {"action": "clean", "hit": "", "reason": "", "removed": [], "care_id": None}
    src = str(text or "")
    if not src.strip():
        return src, rep
    try:
        cfg = resolve_cfg(cfg_root)
        if cfg.get("enabled", True) is False:
            return src, rep
        hit = detect_exit_claim(src)
        if not hit:
            return src, rep
        rep["hit"] = hit
        cid = str(conversation_id or "").strip()
        lg = _lang(lang, src)
        st = inbox_store
        if st is None and cid:
            try:
                from src.integrations.protocol_bridge import get_inbox_store
                st = get_inbox_store()
            except Exception:
                st = None
        try:
            from src.inbox import ai_fingerprint_stats as _fp
            _fp.record("exit_claim")
        except Exception:
            pass
        reason = legit_exit_reason(cfg_root=cfg_root, conversation_id=cid, inbox_store=st, now=now) if cid else ""
        if reason:
            out = src.strip()
            hook = _pick(HOOK_LINES.get(lg) or HOOK_LINES["en"], cid or src[:16])
            if hook and hook.lower() not in out.lower():
                out = f"{out} {hook}".strip()
            rep.update({"action": "allow", "reason": reason})
            if reason != "allow_silence" or bool(cfg.get("followup_on_silence", True)):
                rep["care_id"] = enqueue_followup(conversation_id=cid, hook_text=hook, lang=lg,
                                                  inbox_store=st, cfg_root=cfg_root, now=now,
                                                  care_store=care_store)
            logger.info("[exit] conv=%s allowed=1 reason=%s hit=%r care=%s", cid or "-", reason, hit[:40],
                        rep["care_id"] if rep["care_id"] is not None else "-")
            return out, rep
        kept, removed = strip_exit_sentences(src)
        rep["removed"] = removed[:4]
        if not _QUESTION_RE.search(kept):
            line = _pick(TURN_BACK_LINES.get(lg) or TURN_BACK_LINES["en"], (cid or "") + src[:16])
            kept = f"{kept} {line}".strip() if kept else line
        rep.update({"action": "rewrite", "reason": "in_shift_no_bye"})
        logger.info("[exit] conv=%s allowed=0 reason=in_shift_no_bye hit=%r removed=%d", cid or "-", hit[:40],
                    len(removed))
        return kept, rep
    except Exception:
        logger.debug("[exit] guard_exit failed（原文放行）", exc_info=True)
        return src, rep


__all__ = [
    "CFG_PATH", "EXIT_PATTERNS", "BYE_PATTERNS", "FOLLOWUP_NORM_PREFIX", "detect_exit_claim",
    "is_goodbye", "strip_exit_sentences", "legit_exit_reason", "shift_ending", "enqueue_followup",
    "guard_exit", "resolve_cfg",
]
