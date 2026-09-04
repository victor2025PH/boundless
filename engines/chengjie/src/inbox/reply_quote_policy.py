"""#37 AI 自动链「引用回复」决策（纯函数，保守）——2026-09-04 I-4 D2。

事故：客户连发 3 条、AI 回一条，对方不知道在回哪句。基建（原生 ``reply_to``：
TG ``reply_to_message_id`` / WA quoted / LINE ``related_message_id`` / Messenger 按文本
定位气泡）在编排器 worker 早已接通，缺的只是**自动链的决策**——AI 自动回复从不带引用。

决策原则（比「什么时候引用」更重要的是「什么时候不引用」）：

1. **单条入站一律不引用**。真人不会对着唯一一条消息按引用——那样很机械。
2. 未回复的入站 ≥ ``min_unanswered``（默认 2）条 → 引用**最相关**那条：按回复
   文本与候选入站的词汇重叠（CJK bigram / 拉丁词 / 数字）打分，问句加权；
   最高分低于 ``min_relevance`` ＝ 分不清在回哪句 → **不引用**（宁可少引用）。
3. 候选必须是**客户发的、有文字、未删、在 ``max_age_sec`` 内**的消息。媒体消息
   （无文字）分不出相关性，不做候选。
4. 平台能力：只在编排器拥有该账号（协议 worker 路径）时引用——RPA 回落适配器
   的 ``send()`` 不收 ``reply_to``，传了也是静默丢弃，不如如实不引用。
   ``platforms`` 非空时再按白名单收窄。worker 端本就 degrade-safe（id 解析不了 /
   定位不到 → 普通发送）；发送层再兜一次「带引用发送抛异常 → 去引用重发一次」。

本模块**不读库不发网**：输入是 ``store.list_recent_messages`` 的行（ts 升序）与
回复文本，输出 ``QuoteDecision``；接线在 ``autosend_helpers._autosend_deliver``
（``_send_one`` 首条带 ``reply_to``，分条其余条不带——与坐席手动分条同语义）。
"""
from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence

DEFAULTS: Dict[str, Any] = {
    "enabled": False,
    "min_unanswered": 2,       # 未回复入站至少几条才考虑引用（<2 恒不引用）
    "min_relevance": 0.15,     # 最相关候选的重叠分地板（低于＝分不清，不引用）
    "max_age_sec": 86400.0,    # 候选入站最老多久（更老的不算「刚连发」）
    "question_bonus": 0.15,    # 候选是问句 → 加分（多问齐发正是要引用的场景）
    "platforms": [],           # 空=编排器能发的平台都可；非空=白名单
}

_CJK_SEG_RE = re.compile(r"[\u3400-\u9fff]+")
_LATIN_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z']{1,}|\d+")
_QUESTION_RE = re.compile(
    r"[?？]|(?:吗|嗎|么|麼|呢|咩|嘛)\s*$"
    r"|(?:什么|什麼|怎么|怎麼|为什么|為什麼|哪|几|幾|多少|要不要|有没有|有沒有|"
    r"是不是|能不能|可不可以|会不会|會不會|點解|幾時|有冇|係唔係|得唔得|好唔好)",
)
_STOP_TOKENS = frozenset({
    "the", "and", "you", "are", "for", "that", "this", "with", "have", "your",
})


def parse_quote_cfg(config: Any) -> Dict[str, Any]:
    """从根配置取 ``inbox.l2_autosend.quote_reply``，缺省全走 DEFAULTS；非法值回缺省。"""
    root = config if isinstance(config, Mapping) else {}
    raw = (((root.get("inbox") or {}).get("l2_autosend") or {}).get("quote_reply")
           if isinstance(root.get("inbox"), Mapping) else None)
    if isinstance(raw, bool):
        raw = {"enabled": raw}
    raw = raw if isinstance(raw, Mapping) else {}
    out = dict(DEFAULTS)
    out["enabled"] = bool(raw.get("enabled", DEFAULTS["enabled"]))
    for k in ("min_unanswered",):
        try:
            out[k] = max(2, int(raw.get(k, DEFAULTS[k])))
        except (TypeError, ValueError):
            out[k] = DEFAULTS[k]
    for k in ("min_relevance", "max_age_sec", "question_bonus"):
        try:
            out[k] = float(raw.get(k, DEFAULTS[k]))
        except (TypeError, ValueError):
            out[k] = DEFAULTS[k]
    out["min_relevance"] = min(1.0, max(0.0, out["min_relevance"]))
    out["max_age_sec"] = max(60.0, out["max_age_sec"])
    plats = raw.get("platforms", DEFAULTS["platforms"])
    if isinstance(plats, str):
        plats = [plats]
    out["platforms"] = [str(p).strip().lower() for p in (plats or []) if str(p).strip()]
    return out


def platform_allows_quote(platform: str, cfg: Mapping[str, Any], *, orch_owns: bool) -> bool:
    """引用只在协议 worker 路径有意义；RPA 回落适配器不收 reply_to → 如实不引用。"""
    if not orch_owns:
        return False
    plats = list(cfg.get("platforms") or [])
    if plats and str(platform or "").strip().lower() not in plats:
        return False
    return True


# ── 相关性 ────────────────────────────────────────────────────────────────

def _tokens(text: str) -> set:
    """内容级词元：CJK 用 bigram（单字太泛），拉丁词 ≥2 字符去停用词，数字整串。"""
    s = str(text or "")
    out: set = set()
    # 只在连续 CJK 段内取 bigram（跨段拼 bigram 是噪声）
    for seg in _CJK_SEG_RE.findall(s):
        if len(seg) == 1:
            out.add(seg)
        for i in range(len(seg) - 1):
            out.add(seg[i:i + 2])
    for tok in _LATIN_TOKEN_RE.findall(s):
        t = tok.lower()
        if t in _STOP_TOKENS:
            continue
        out.add(t)
    return out


def is_question(text: str) -> bool:
    """保守问句判定（中/粤/英标点 + 常见疑问词）。"""
    s = str(text or "").strip()
    if not s:
        return False
    return bool(_QUESTION_RE.search(s))


def relevance_score(reply_text: str, candidate_text: str) -> float:
    """回复对候选的覆盖度 ∈ [0,1]：候选词元被回复命中的比例（候选为空 → 0）。"""
    ct = _tokens(candidate_text)
    if not ct:
        return 0.0
    rt = _tokens(reply_text)
    if not rt:
        return 0.0
    hit = len(ct & rt)
    return hit / float(len(ct))


# ── 决策 ─────────────────────────────────────────────────────────────────

@dataclass
class QuoteDecision:
    quote: bool
    reason: str
    reply_to: Optional[Dict[str, Any]] = None
    score: float = 0.0
    burst_len: int = 0
    target_index: int = -1            # 被引用消息在未回复串里的下标（0=最早）
    candidates: List[Dict[str, Any]] = field(default_factory=list)

    def as_reply_to(self) -> Optional[Dict[str, Any]]:
        return dict(self.reply_to) if (self.quote and self.reply_to) else None


def _row_get(row: Any, key: str, default: Any = None) -> Any:
    if isinstance(row, Mapping):
        return row.get(key, default)
    return getattr(row, key, default)


def unanswered_burst(messages: Sequence[Any]) -> List[Any]:
    """末尾连续的入站串（最后一条出站之后的全部 ``direction=='in'`` 行），保持 ts 升序。"""
    out: List[Any] = []
    for row in reversed(list(messages or [])):
        d = str(_row_get(row, "direction", "") or "").lower()
        if d == "in":
            out.append(row)
            continue
        if d == "out":
            break
        # 未知方向（系统行等）：不计入串也不打断
    out.reverse()
    return out


def decide_quote(
    messages: Sequence[Any],
    reply_text: str,
    *,
    cfg: Optional[Mapping[str, Any]] = None,
    now: Optional[float] = None,
    reply_alt: str = "",
) -> QuoteDecision:
    """核心纯函数。``messages``＝会话最近消息（ts 升序，store 行 dict）。

    ``reply_text``＝实发文本（出站翻译后与入站 ``text`` 同语）；``reply_alt``＝
    翻译前原文（与入站 ``translated_text`` 同语）。每个候选取两路比对的最高分——
    跨语会话（客户英文 / 人设中文）也能算出相关性，单语会话两路等价。

    返回的 ``reply_to`` 形态与坐席手动引用一致：``{id, from_me, participant, text, sender}``
    （``id``＝platform_msg_id；Messenger 无 id 只靠 text 定位——worker 侧自适配）。
    """
    c = dict(DEFAULTS)
    if cfg:
        c.update({k: v for k, v in dict(cfg).items() if k in DEFAULTS})
    now_ts = float(now if now is not None else time.time())

    burst = unanswered_burst(messages)
    n = len(burst)
    if n == 0:
        return QuoteDecision(False, "no_inbound", burst_len=0)
    if n < int(c["min_unanswered"]):
        # 单条入站一律不引用——本模块最重要的一条
        return QuoteDecision(False, "single_inbound", burst_len=n)

    cands: List[Dict[str, Any]] = []
    for idx, row in enumerate(burst):
        text = str(_row_get(row, "text", "") or "").strip()
        if not text:
            continue                       # 媒体/空文本：分不出相关性，不做候选
        try:
            if float(_row_get(row, "deleted_at", 0) or 0) > 0:
                continue                   # 已删（本端或对端）：引用会指向不存在的消息
        except (TypeError, ValueError):
            pass
        try:
            ts = float(_row_get(row, "ts", 0) or 0)
        except (TypeError, ValueError):
            ts = 0.0
        if ts and now_ts - ts > float(c["max_age_sec"]):
            continue
        pmid = str(_row_get(row, "platform_msg_id", "") or "").strip()
        alt_text = str(_row_get(row, "translated_text", "") or "").strip()
        sc = relevance_score(reply_text, text)
        if reply_alt:
            sc = max(sc, relevance_score(reply_alt, text))
            if alt_text:
                sc = max(sc, relevance_score(reply_alt, alt_text))
        if alt_text:
            sc = max(sc, relevance_score(reply_text, alt_text))
        q = is_question(text) or is_question(alt_text)
        if q and sc > 0:
            sc = min(1.0, sc + float(c["question_bonus"]))
        cands.append({
            "index": idx, "id": pmid, "text": text, "score": round(sc, 4),
            "question": q, "ts": ts,
        })
    if not cands:
        return QuoteDecision(False, "no_candidate", burst_len=n)

    # 最高分；同分取**更早**那条（更早的更可能被后面几条淹没，引用它才有信息量）
    best = max(cands, key=lambda x: (x["score"], -x["index"]))
    if best["score"] < float(c["min_relevance"]):
        return QuoteDecision(False, "low_relevance", score=best["score"],
                             burst_len=n, candidates=cands)
    if not best["id"] and not best["text"]:
        return QuoteDecision(False, "no_ref", burst_len=n, candidates=cands)
    reply_to = {
        "id": best["id"],
        "from_me": False,
        "participant": "",
        "text": best["text"][:200],
        "sender": "",
    }
    return QuoteDecision(
        True, "burst_relevant", reply_to=reply_to, score=best["score"],
        burst_len=n, target_index=int(best["index"]), candidates=cands,
    )


# ── 观测（进程级，重启清零；与 autosend 其它计数同口径）───────────────────

_LOCK = threading.Lock()
_STATS: Dict[str, Any] = {
    "decided": 0,          # decide_quote 被调用次数
    "quoted": 0,           # 决策=引用
    "skipped": {},         # 不引用原因分布
    "applied": 0,          # worker 回执 quote_applied=True
    "fallback_plain": 0,   # 带引用发送异常 → 去引用重发
}


def record_decision(dec: QuoteDecision) -> None:
    with _LOCK:
        _STATS["decided"] += 1
        if dec.quote:
            _STATS["quoted"] += 1
        else:
            r = dec.reason or "unknown"
            _STATS["skipped"][r] = int(_STATS["skipped"].get(r, 0)) + 1


def record_applied(result: Any) -> None:
    if isinstance(result, Mapping) and result.get("quote_applied"):
        with _LOCK:
            _STATS["applied"] += 1


def record_fallback_plain() -> None:
    with _LOCK:
        _STATS["fallback_plain"] += 1


def stats_snapshot() -> Dict[str, Any]:
    with _LOCK:
        return {
            "decided": _STATS["decided"],
            "quoted": _STATS["quoted"],
            "skipped": dict(_STATS["skipped"]),
            "applied": _STATS["applied"],
            "fallback_plain": _STATS["fallback_plain"],
        }


def reset_stats() -> None:
    with _LOCK:
        _STATS["decided"] = 0
        _STATS["quoted"] = 0
        _STATS["skipped"] = {}
        _STATS["applied"] = 0
        _STATS["fallback_plain"] = 0


__all__ = [
    "DEFAULTS", "QuoteDecision", "parse_quote_cfg", "platform_allows_quote",
    "decide_quote", "unanswered_burst", "relevance_score", "is_question",
    "record_decision", "record_applied", "record_fallback_plain",
    "stats_snapshot", "reset_stats",
]
