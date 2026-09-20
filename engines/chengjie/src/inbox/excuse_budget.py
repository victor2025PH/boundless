# -*- coding: utf-8 -*-
"""Q-8 F（#264 #263）：当地时间 + 今日日程注入，借口只从日程取；「工作」类借口同客户每日 ≤1。

事故面：AI 找借口全靠现编——同一个客户一天里听到三次「我在上班 / 加班 / 开会」，与人设日程
（``companion_selfie.build_day_itinerary``：同日确定性动线）和当地时间都对不上，穿帮点越攒越多。

两段：

- **生成前**：:func:`build_time_schedule_addendum` 装配「当地时间 + 今日动线 + 借口纪律
  （只许从日程取；今天已用过工作借口 → 本轮禁用）」一段，由 ``persona_reply._prompt_addenda`` 的
  ④ try-block 调用（Q-6 单一接线点，本线不改 persona_reply 其它一字）。
- **出站**：:func:`guard_outbound` 挂 ``outbound_humanize.apply_draft_humanize``（退场闸之后）——
  检出「工作」类借口即计数（InboxStore ``app_settings`` KV ``excuse:work:<conv>:<day>``）；
  当日已达上限 → 砍掉借口句 / 分句（只剩借口 → 换一句不找理由的接话）。
  日志 ``[excuse] conv=… kind=work today=n cap=c action=pass|rewrite``。

配置 ``companion.time_schedule {enabled, work_excuse_cap}``：``enabled`` 缺席按业务域解释
（陪伴开 / 销售关——销售域「在开会」是正常客服话）。纯函数 + 只读 / 单键写 KV；任何失败原文放行。
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("src.inbox.excuse_budget")

CFG_PATH = "companion.time_schedule"
KEY_PREFIX = "excuse:work:"
DEFAULT_CAP = 1

#: 「工作」类借口——只认第一人称 / 自述（「你在上班吗」「你老板」不算）。
WORK_EXCUSE_PATTERNS: Tuple[re.Pattern, ...] = tuple(re.compile(p, re.I) for p in (
    r"\bi(?:'m| am)\s+(?:\w+\s+)?(?:busy|swamped|slammed|buried|tied\s+up|snowed\s+under)\s+(?:with|at)\s+work\b",
    r"\bwork(?:'s|\s+is|\s+was|\s+has\s+been)\s+(?:so\s+|really\s+|super\s+)?(?:crazy|insane|hectic|nuts|busy|brutal|a\s+lot|nonstop)\b",
    r"\b(?:i(?:'m| am)\s+)?(?:stuck|still)\s+at\s+(?:work|the\s+office)\b",
    r"\bi(?:'m| am)\s+(?:at|in)\s+(?:a\s+)?meetings?\b",
    r"\b(?:i(?:'m| am)\s+)?working\s+(?:late|overtime|a\s+double|through\s+lunch)\b",
    r"\bi(?:'m| am)\s+(?:still\s+)?(?:at\s+)?work(?:ing)?\s+(?:right\s+)?now\b",
    r"\b(?:got|have)\s+(?:a\s+)?(?:ton|lot|pile)\s+of\s+work\b",
    r"\bmy\s+boss\s+(?:needs|wants|is\s+on)\s+me\b",
    r"(?<![你您])(?:我)?(?:还|正)?在(?:上班|加班|开会)(?:呢|中|了|啦)?",
    r"(?<![你您])(?:我)?(?:这边|这儿)?工作(?:太|有点|好|很|特别)?忙",
    r"(?<![你您])(?:我)?(?:公司|老板)(?:这边|那边)?(?:有点|很|太)?(?:忙|催|事多)",
    r"(?<![你您])(?:我)?手头(?:有)?(?:一堆|点|些)?(?:工作|活|事)",
    r"(?<![你您])(?:我)?(?:要|得|先)(?:去)?(?:上班|加班|开会|忙工作)",
))

_FALLBACK_LINE = {
    "zh": ("刚走神了一下，你刚说到哪了？", "刚有点分心，你继续说？"),
    "en": ("sorry, zoned out for a sec. where were we?", "wait, got distracted, say that again?"),
}

_WEEKDAY_ZH = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
_WEEKDAY_EN = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def detect_work_excuse(text: str) -> str:
    s = str(text or "")
    if not s.strip():
        return ""
    for p in WORK_EXCUSE_PATTERNS:
        m = p.search(s)
        if m:
            return m.group(0)
    return ""


def resolve_cfg(config: Any) -> Dict[str, Any]:
    node: Any = config if isinstance(config, dict) else {}
    for part in CFG_PATH.split("."):
        node = node.get(part) if isinstance(node, dict) else None
    return node if isinstance(node, dict) else {}


def is_enabled(config: Any) -> bool:
    """显式 ``enabled`` 优先；缺席 → 陪伴域开、销售域关。"""
    cfg = resolve_cfg(config)
    if "enabled" in cfg and cfg.get("enabled") is not None:
        return bool(cfg.get("enabled"))
    try:
        from src.utils.business_domain import active_business_domain
        return active_business_domain(config if isinstance(config, dict) else None) == "companion"
    except Exception:
        return False


def work_excuse_cap(config: Any) -> int:
    try:
        return max(0, int(resolve_cfg(config).get("work_excuse_cap", DEFAULT_CAP)))
    except (TypeError, ValueError):
        return DEFAULT_CAP


def _day(now: Optional[float]) -> str:
    return time.strftime("%Y%m%d", time.localtime(float(now if now is not None else time.time())))


def excuse_key(conversation_id: str, now: Optional[float] = None) -> str:
    return f"{KEY_PREFIX}{conversation_id}:{_day(now)}"


def _store(inbox_store: Any) -> Any:
    if inbox_store is not None:
        return inbox_store
    try:
        from src.integrations.protocol_bridge import get_inbox_store
        return get_inbox_store()
    except Exception:
        return None


def count_today(inbox_store: Any, conversation_id: str, now: Optional[float] = None) -> int:
    st = _store(inbox_store)
    if st is None or not conversation_id or not hasattr(st, "get_app_setting"):
        return 0
    try:
        return max(0, int(str(st.get_app_setting(excuse_key(conversation_id, now), "0") or "0").strip() or 0))
    except Exception:
        return 0


def note_used(inbox_store: Any, conversation_id: str, now: Optional[float] = None) -> int:
    st = _store(inbox_store)
    if st is None or not conversation_id or not hasattr(st, "set_app_setting"):
        return 0
    n = count_today(st, conversation_id, now) + 1
    try:
        st.set_app_setting(excuse_key(conversation_id, now), str(n), updated_by="excuse_budget")
    except TypeError:
        st.set_app_setting(excuse_key(conversation_id, now), str(n))
    except Exception:
        logger.debug("[excuse] note_used failed", exc_info=True)
    return n


def _lang(lang: str, text: str = "") -> str:
    lg = str(lang or "").lower()
    if lg.startswith("zh"):
        return "zh"
    if lg.startswith("en"):
        return "en"
    return "zh" if re.search(r"[\u4e00-\u9fff]", str(text or "")) else "en"


def local_time_label(persona: Any, lang: str, *, now: Any = None) -> Tuple[str, Any, str]:
    """→ ``(短句, 当地 datetime|None, 当前时段名)``。无居住地 → ("", None, 服务器时段)。"""
    import datetime as _dt
    local_now = None
    label = ""
    try:
        from src.companion.persona_location import persona_now, resolve_place_with_fallback
        place = resolve_place_with_fallback(persona)
        if place is not None:
            local_now = persona_now(place, now if isinstance(now, _dt.datetime) else None)
            wd = local_now.weekday()
            if _lang(lang) == "en":
                label = f"{local_now.strftime('%H:%M')} {_WEEKDAY_EN[wd]}, {place.display('en')}"
            else:
                label = f"{local_now.strftime('%H:%M')} {_WEEKDAY_ZH[wd]}，{place.display('zh')}"
    except Exception:
        local_now, label = None, ""
    try:
        from src.ai.companion_selfie import _bucket_label
        t = local_now or (now if isinstance(now, _dt.datetime) else _dt.datetime.now())
        bucket = _bucket_label(t.hour)
    except Exception:
        bucket = ""
    return label, local_now, bucket


def today_itinerary(persona: Any, config: Any, *, local_now: Any = None) -> List[Tuple[str, str]]:
    try:
        from src.ai.companion_selfie import build_day_itinerary
        scfg = ((config or {}).get("companion") or {}).get("selfie") if isinstance(config, dict) else {}
        rows = build_day_itinerary(persona, scfg if isinstance(scfg, dict) else {}, now=local_now)
        return [(str(a), str(b)) for a, b in rows if str(b).strip()]
    except Exception:
        return []


def build_time_schedule_addendum(persona: Any, conversation_id: str, config: Any, *, lang: str = "zh",
                                 now: Optional[float] = None, inbox_store: Any = None) -> str:
    """生成前一段（persona_reply ④ 接线点唯一调用）。关 / 无素材 → ""。绝不抛。

    两块独立拼接（各自开关、各自 try）：
    - 人设当地时间 + 行程（Q-8 F，``companion.time_schedule``）；
    - **对方**当地时间（Q-29 #305，``companion.peer_time``）——客户城市 / 时间自述 → 「不要问几点 / 白天还是晚上」，
      未知则「顺口问一次，24h 内不再问」。经此函数接线，``persona_reply.py`` 零改动。
    """
    parts: List[str] = []
    try:
        if is_enabled(config):
            import datetime as _dt
            now_dt = _dt.datetime.fromtimestamp(float(now)) if now is not None else None
            label, local_now, bucket = local_time_label(persona, lang, now=now_dt)
            itin = today_itinerary(persona, config, local_now=local_now)
            used = count_today(inbox_store, str(conversation_id or ""), now) if conversation_id else 0
            from src.inbox.prompt_addenda import time_schedule_addendum
            block = time_schedule_addendum(label, itin, lang=lang, current_bucket=bucket,
                                           work_excuse_used=used, work_excuse_cap=work_excuse_cap(config))
            if block:
                logger.debug("[excuse] addendum conv=%s local=%r itinerary=%d used=%d", conversation_id or "-",
                             label, len(itin), used)
                parts.append(block)
    except Exception:
        logger.debug("[excuse] build_time_schedule_addendum failed", exc_info=True)
    try:
        from src.companion.peer_time import build_peer_time_addendum
        peer = build_peer_time_addendum(str(conversation_id or ""), config, lang=lang, now=now,
                                        inbox_store=inbox_store)
        if peer:
            parts.append(peer)
    except Exception:
        logger.debug("[peer-time] addendum failed", exc_info=True)
    return "\n".join(p for p in parts if p)


def _strip_excuse(text: str) -> Tuple[str, List[str]]:
    from src.inbox.exit_gate import _CLAUSE_SPLIT, split_sentences
    kept: List[str] = []
    removed: List[str] = []
    for s in split_sentences(text):
        if not detect_work_excuse(s):
            kept.append(s)
            continue
        removed.append(s)
        clauses = [c.strip() for c in _CLAUSE_SPLIT.split(s) if c and c.strip()]
        rest = [c for c in clauses if not detect_work_excuse(c)]
        if rest and len(rest) < len(clauses):
            zh = bool(re.search(r"[\u4e00-\u9fff]", s))
            joined = ("，" if zh else ", ").join(rest)
            if joined[-1] not in ".!?。！？":
                joined += "。" if zh else "."
            kept.append(joined)
    return " ".join(kept).strip(), removed


def note_outbound_excuse(text: str, *, conversation_id: str = "", cfg_root: Any = None,
                         inbox_store: Any = None, now: Optional[float] = None, origin: str = "") -> int:
    """发送门绕过路径（人工 / verbatim）只计数不改写：客户听到的借口就是借口。返回今日计数（未命中 0）。"""
    try:
        cid = str(conversation_id or "").strip()
        if not cid or not is_enabled(cfg_root):
            return 0
        hit = detect_work_excuse(text)
        if not hit:
            return 0
        n = note_used(inbox_store, cid, now)
        logger.info("[excuse] conv=%s kind=work today=%d cap=%d action=pass origin=%s hit=%r",
                    cid, n, work_excuse_cap(cfg_root), origin or "-", hit[:40])
        return n
    except Exception:
        return 0


def guard_outbound(text: str, *, conversation_id: str = "", lang: str = "", cfg_root: Any = None,
                   inbox_store: Any = None, now: Optional[float] = None,
                   count: bool = True) -> Tuple[str, Dict[str, Any]]:
    """借口预算闸。返回 ``(text, {action: clean|pass|rewrite, hit, today, cap, removed})``。绝不抛。

    ``count=True``（发送门 = 真出站）命中且未超额 → 计数 +1；``count=False``（起草层）只判不计——
    草稿可能被改 / 弃，客户没听到的借口不算。两层都在超额时改写。
    """
    rep: Dict[str, Any] = {"action": "clean", "hit": "", "today": 0, "cap": DEFAULT_CAP, "removed": []}
    src = str(text or "")
    if not src.strip():
        return src, rep
    try:
        if not is_enabled(cfg_root):
            return src, rep
        hit = detect_work_excuse(src)
        if not hit:
            return src, rep
        cid = str(conversation_id or "").strip()
        cap = work_excuse_cap(cfg_root)
        rep.update({"hit": hit, "cap": cap})
        used = count_today(inbox_store, cid, now) if cid else 0
        if used < cap:
            n = note_used(inbox_store, cid, now) if (cid and count) else used
            rep.update({"action": "pass", "today": n})
            logger.info("[excuse] conv=%s kind=work today=%d cap=%d action=pass counted=%d hit=%r",
                        cid or "-", n, cap, 1 if (cid and count) else 0, hit[:40])
            return src, rep
        kept, removed = _strip_excuse(src)
        lg = _lang(lang, src)
        if not kept:
            import zlib
            rows = _FALLBACK_LINE[lg]
            kept = rows[zlib.crc32((cid + src[:16]).encode("utf-8", "ignore")) % len(rows)]
        rep.update({"action": "rewrite", "today": used, "removed": removed[:4]})
        logger.info("[excuse] conv=%s kind=work today=%d cap=%d action=rewrite hit=%r removed=%d",
                    cid or "-", used, cap, hit[:40], len(removed))
        return kept, rep
    except Exception:
        logger.debug("[excuse] guard_outbound failed（原文放行）", exc_info=True)
        return src, rep


__all__ = [
    "CFG_PATH", "KEY_PREFIX", "WORK_EXCUSE_PATTERNS", "detect_work_excuse", "is_enabled", "work_excuse_cap",
    "excuse_key", "count_today", "note_used", "local_time_label", "today_itinerary",
    "build_time_schedule_addendum", "guard_outbound", "note_outbound_excuse",
]
