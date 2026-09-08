"""AI 自动链「引用回复」决策（纯函数、确定性、不走 LLM）。

#37（I-4 D2，2026-09-04）立的基线：客户连发 ≥2 条未回、AI 回一条 → 引用最相关那条；
单条入站一律不引用。O-4（#254 / 47KNBV / 3HNCJ7，2026-09-08）在其上补成完整规则引擎：

必引
  * ``burst``  客户连发 ≥ ``min_unanswered`` 条未回且回其中某条（#37 原规则，相关性打分选目标）；
  * ``stale``  所回消息是旧消息（距今 > ``stale_sec``，默认 30 min）——真人隔半小时回一句
    必定按引用，否则对方不知道在回什么。
基线
  * ``random`` 10–15% 随机模拟习惯——**会话级种子**：会话 id 决定该会话的习惯率
    （落在 [random_lo, random_hi]），(会话, 目标消息) 决定本次掷骰；同一输入永远同一结果，
    tests 可钉、重试不抖。
禁引（绝对约束，压过必引与基线）
  * ``short_quick``     2 min 内对单条的短答不引（最像机器的一种引用）；
  * ``streak_cooldown`` 连引 ≤ ``max_streak`` 条后至少 ``cooldown_after_streak`` 条不引；
  * ``hourly_cap``      每会话每小时 ≤ ``hourly_cap`` 条；
  * ``self``            引自己消息只允许「补充上一条」（距自己上一条 ≤ ``self_quote_window_sec``
    且客户没插话）且 ≤ ``self_quote_pct``（默认 5%）。
能力位
  * 只有 Telegram（原生 ``reply_to_message_id``）/ WhatsApp（Baileys ``quoted``）进一期；
    LINE / Messenger 是 RPA 接入、无可靠能力位 → ``unsupported``，规则引擎直接 none 并落日志。
    静态表 ``QUOTE_CAPABLE_PLATFORMS`` 由门禁钉住与 ``surface_fusion`` 注册表一致。

「连引 / 每小时」的历史**从消息行推导**而不是进程内计数：编排器只在 worker 回执
``quote_applied`` 为真时才把 ``source.reply_to`` 镜像进出站行（``messages.reply_to_id``），
所以「出站行带 reply_to_id」＝这条真的带引用发出去了；重启不清零、坐席手动引用也算数。

本模块**不读库不发网**：输入是 ``store.list_recent_messages`` 的行（ts 升序）与回复文本，
输出 ``QuoteDecision``；接线在 ``autosend_helpers._autosend_deliver``（``_send_one`` 首条带
``reply_to``，分条其余条不带）。发送层再兜一次「带引用发送抛异常 → 去引用重发一次」
（与 M-3 A 出站结果口径一致：不丢消息）。
"""
from __future__ import annotations

import hashlib
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence

DEFAULTS: Dict[str, Any] = {
    "enabled": True,           # O-4 D-O6：出厂默认开（overlay quote_reply.enabled=false 关）
    "min_unanswered": 2,       # 连发几条起算 burst（<2 不走 burst 规则）
    "min_relevance": 0.15,     # burst 最相关候选的重叠分地板（低于＝分不清，不引用）
    "max_age_sec": 86400.0,    # 候选入站最老多久（更老的不做候选）
    "question_bonus": 0.15,    # 候选是问句 → 加分
    "platforms": [],           # 空=能力位允许的平台都可；非空=再按白名单收窄
    # ── O-4 规则引擎 ──
    "stale_sec": 1800.0,          # 所回消息距今 >30 min → 必引
    "random_lo": 0.10,            # 会话级随机基线区间下限
    "random_hi": 0.15,            # 上限
    "short_quick_sec": 120.0,     # 2 min 内…
    "short_answer_chars": 40,     # …对单条的短答（≤40 字符）不引
    "max_streak": 2,              # 连引最多几条
    "cooldown_after_streak": 2,   # 连引到顶后至少几条不引
    "hourly_cap": 6,              # 每会话每小时最多几条（0=不限）
    "self_quote_pct": 0.05,       # 引自己消息（补充上一条）的概率上限
    "self_quote_window_sec": 600.0,  # 「补充上一条」＝距自己上一条 ≤10 min 且客户没插话
}

#: 一期能力位：True＝规则引擎可给 reply_to；False/缺席＝unsupported。
#: 与 ``surface_fusion`` 注册表 quote_reply.workspace 同口径（门禁钉住）；LINE 未策划、
#: Messenger 明确 none。RPA 回落路径（编排器不拥有账号）另由 ``orch_owns`` 拦。
QUOTE_CAPABLE_PLATFORMS: Dict[str, bool] = {
    "telegram": True,
    "whatsapp": True,
    "line": False,
    "messenger": False,
}

#: 日志 reason 取值（coarse）；rule 是具体命中的规则名。
REASON_BURST = "burst"
REASON_STALE = "stale"
REASON_RANDOM = "random"
REASON_SELF = "self"
REASON_NONE = "none"

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


def _as_float(raw: Mapping[str, Any], key: str, lo: float, hi: Optional[float] = None) -> float:
    try:
        v = float(raw.get(key, DEFAULTS[key]))
    except (TypeError, ValueError):
        v = float(DEFAULTS[key])
    v = max(lo, v)
    return min(hi, v) if hi is not None else v


def _as_int(raw: Mapping[str, Any], key: str, lo: int) -> int:
    try:
        v = int(raw.get(key, DEFAULTS[key]))
    except (TypeError, ValueError):
        v = int(DEFAULTS[key])
    return max(lo, v)


def parse_quote_cfg(config: Any) -> Dict[str, Any]:
    """从根配置取 ``inbox.l2_autosend.quote_reply``，缺省全走 DEFAULTS；非法值回缺省并钳区间。"""
    root = config if isinstance(config, Mapping) else {}
    raw = (((root.get("inbox") or {}).get("l2_autosend") or {}).get("quote_reply")
           if isinstance(root.get("inbox"), Mapping) else None)
    if isinstance(raw, bool):
        raw = {"enabled": raw}
    raw = raw if isinstance(raw, Mapping) else {}
    out = dict(DEFAULTS)
    out["enabled"] = bool(raw.get("enabled", DEFAULTS["enabled"]))
    out["min_unanswered"] = _as_int(raw, "min_unanswered", 2)   # burst 铁律：<2 不算连发
    out["min_relevance"] = _as_float(raw, "min_relevance", 0.0, 1.0)
    out["max_age_sec"] = _as_float(raw, "max_age_sec", 60.0)
    out["question_bonus"] = _as_float(raw, "question_bonus", 0.0)
    out["stale_sec"] = _as_float(raw, "stale_sec", 60.0)
    out["random_lo"] = _as_float(raw, "random_lo", 0.0, 1.0)
    out["random_hi"] = max(out["random_lo"], _as_float(raw, "random_hi", 0.0, 1.0))
    out["short_quick_sec"] = _as_float(raw, "short_quick_sec", 0.0)
    out["short_answer_chars"] = _as_int(raw, "short_answer_chars", 0)
    out["max_streak"] = _as_int(raw, "max_streak", 1)
    out["cooldown_after_streak"] = _as_int(raw, "cooldown_after_streak", 0)
    out["hourly_cap"] = _as_int(raw, "hourly_cap", 0)
    out["self_quote_pct"] = _as_float(raw, "self_quote_pct", 0.0, 1.0)
    out["self_quote_window_sec"] = _as_float(raw, "self_quote_window_sec", 0.0)
    plats = raw.get("platforms", DEFAULTS["platforms"])
    if isinstance(plats, str):
        plats = [plats]
    out["platforms"] = [str(p).strip().lower() for p in (plats or []) if str(p).strip()]
    return out


def platform_quote_capable(platform: str) -> bool:
    """一期能力位：只有 Telegram / WhatsApp 协议路径有可靠的原生引用。"""
    return bool(QUOTE_CAPABLE_PLATFORMS.get(str(platform or "").strip().lower(), False))


def platform_allows_quote(platform: str, cfg: Mapping[str, Any], *, orch_owns: bool) -> bool:
    """能力位 ∧ 编排器路径 ∧ 配置白名单。RPA 回落适配器不收 reply_to → 如实不引用。"""
    if not orch_owns:
        return False
    if not platform_quote_capable(platform):
        return False
    plats = list(cfg.get("platforms") or [])
    if plats and str(platform or "").strip().lower() not in plats:
        return False
    return True


# ── 会话级种子 ────────────────────────────────────────────────────────────

def seeded_unit(*parts: Any) -> float:
    """确定性 [0,1) 伪随机：sha256(会话 id | 用途 | 目标) 前 8 字节。同输入恒同输出。"""
    key = "|".join(str(p if p is not None else "") for p in parts)
    h = hashlib.sha256(key.encode("utf-8")).digest()
    return int.from_bytes(h[:8], "big") / float(1 << 64)


def session_rate(conv_key: str, cfg: Mapping[str, Any]) -> float:
    """该会话的「引用习惯率」∈ [random_lo, random_hi]，由会话 id 决定（会话级种子）。"""
    lo = float(cfg.get("random_lo", DEFAULTS["random_lo"]))
    hi = max(lo, float(cfg.get("random_hi", DEFAULTS["random_hi"])))
    return lo + (hi - lo) * seeded_unit(conv_key, "rate")


# ── 相关性 ────────────────────────────────────────────────────────────────

def _tokens(text: str) -> set:
    """内容级词元：CJK 用 bigram（单字太泛），拉丁词 ≥2 字符去停用词，数字整串。"""
    s = str(text or "")
    out: set = set()
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
    reason: str                       # burst|stale|random|self|none（日志 reason=）
    reply_to: Optional[Dict[str, Any]] = None
    score: float = 0.0
    burst_len: int = 0
    target_index: int = -1            # 被引用消息在未回复串里的下标（0=最早）
    candidates: List[Dict[str, Any]] = field(default_factory=list)
    rule: str = ""                    # 具体命中的规则 / 不引用的具体原因（日志 rule=）
    roll: Optional[float] = None      # 掷骰值（random/self 才有）
    rate: Optional[float] = None      # 会话习惯率（random 才有）
    suppressed: str = ""              # 被禁引压掉的「本可引用」规则名
    quotes_last_hour: int = 0

    def as_reply_to(self) -> Optional[Dict[str, Any]]:
        return dict(self.reply_to) if (self.quote and self.reply_to) else None

    @property
    def skip_key(self) -> str:
        """不引用时的统计键：具体规则名优先（low_relevance / short_quick / …）。"""
        return self.rule or self.reason or "unknown"


def _row_get(row: Any, key: str, default: Any = None) -> Any:
    if isinstance(row, Mapping):
        return row.get(key, default)
    return getattr(row, key, default)


def _row_ts(row: Any) -> float:
    try:
        return float(_row_get(row, "ts", 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def _row_deleted(row: Any) -> bool:
    try:
        return float(_row_get(row, "deleted_at", 0) or 0) > 0
    except (TypeError, ValueError):
        return False


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


def row_was_quoted(row: Any) -> bool:
    """出站行是否真带引用发出（编排器只在 quote_applied 回执为真时镜像 reply_to）。"""
    return bool(str(_row_get(row, "reply_to_id", "") or "").strip()
                or str(_row_get(row, "reply_to_text", "") or "").strip())


def outbound_quote_flags(messages: Sequence[Any]) -> List[bool]:
    """出站行的「带引用」标记，**最近的在前**。"""
    flags: List[bool] = []
    for row in reversed(list(messages or [])):
        if str(_row_get(row, "direction", "") or "").lower() == "out":
            flags.append(row_was_quoted(row))
    return flags


def quotes_in_window(messages: Sequence[Any], now_ts: float, window_sec: float = 3600.0) -> int:
    """窗口内带引用发出的出站条数（每会话每小时 ≤N 的分子）。"""
    n = 0
    for row in messages or ():
        if str(_row_get(row, "direction", "") or "").lower() != "out":
            continue
        if not row_was_quoted(row):
            continue
        ts = _row_ts(row)
        if ts and now_ts - ts <= window_sec:
            n += 1
    return n


def streak_blocked(flags_recent_first: Sequence[bool], max_streak: int, cooldown: int) -> bool:
    """「连引 ≤max_streak 后至少 cooldown 条不引」的判定。

    最近 ``cooldown`` 个位置里若坐着一段长度 ≥ ``max_streak`` 的连引（含刚刚到顶、
    0 条冷却的情形），本条就不能再引。cooldown=0 ＝只封顶不冷却。
    """
    flags = list(flags_recent_first)
    ms = max(1, int(max_streak))
    cd = max(0, int(cooldown))
    for start in range(0, max(cd, 1)):
        window = flags[start:start + ms]
        if len(window) == ms and all(window):
            return True
    return False


def _candidates(burst: Sequence[Any], reply_text: str, reply_alt: str,
                c: Mapping[str, Any], now_ts: float) -> List[Dict[str, Any]]:
    cands: List[Dict[str, Any]] = []
    for idx, row in enumerate(burst):
        text = str(_row_get(row, "text", "") or "").strip()
        if not text:
            continue                       # 媒体/空文本：分不出相关性，不做候选
        if _row_deleted(row):
            continue                       # 已删：引用会指向不存在的消息
        ts = _row_ts(row)
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
    return cands


def _reply_to_of(cand: Mapping[str, Any], *, from_me: bool = False) -> Dict[str, Any]:
    return {
        "id": str(cand.get("id") or ""),
        "from_me": bool(from_me),
        "participant": "",
        "text": str(cand.get("text") or "")[:200],
        "sender": "",
    }


def _forbid(dec: QuoteDecision, c: Mapping[str, Any], messages: Sequence[Any],
            now_ts: float) -> QuoteDecision:
    """禁引约束（绝对）：每小时上限 / 连引冷却。压掉时把本可引用的规则记进 suppressed。"""
    if not dec.quote:
        return dec
    cap = int(c["hourly_cap"])
    if cap > 0 and dec.quotes_last_hour >= cap:
        return QuoteDecision(
            False, REASON_NONE, score=dec.score, burst_len=dec.burst_len,
            target_index=dec.target_index, candidates=dec.candidates,
            rule="hourly_cap", suppressed=dec.rule, roll=dec.roll, rate=dec.rate,
            quotes_last_hour=dec.quotes_last_hour)
    if streak_blocked(outbound_quote_flags(messages), int(c["max_streak"]),
                      int(c["cooldown_after_streak"])):
        return QuoteDecision(
            False, REASON_NONE, score=dec.score, burst_len=dec.burst_len,
            target_index=dec.target_index, candidates=dec.candidates,
            rule="streak_cooldown", suppressed=dec.rule, roll=dec.roll, rate=dec.rate,
            quotes_last_hour=dec.quotes_last_hour)
    return dec


def _decide_self_supplement(messages: Sequence[Any], c: Mapping[str, Any],
                            now_ts: float, conv_key: str, qlh: int) -> QuoteDecision:
    """客户没新消息、我们接着自己上一条再发一条＝「补充上一条」：≤self_quote_pct 引自己。"""
    last_out = None
    for row in reversed(list(messages or [])):
        if str(_row_get(row, "direction", "") or "").lower() == "out":
            last_out = row
            break
    if last_out is None or float(c["self_quote_pct"]) <= 0:
        return QuoteDecision(False, REASON_NONE, rule="no_inbound", quotes_last_hour=qlh)
    ts = _row_ts(last_out)
    if not ts or now_ts - ts > float(c["self_quote_window_sec"]):
        return QuoteDecision(False, REASON_NONE, rule="no_inbound", quotes_last_hour=qlh)
    pmid = str(_row_get(last_out, "platform_msg_id", "") or "").strip()
    text = str(_row_get(last_out, "text", "") or "").strip()
    if not pmid or not text or _row_deleted(last_out):
        return QuoteDecision(False, REASON_NONE, rule="no_ref", quotes_last_hour=qlh)
    roll = seeded_unit(conv_key, "self", pmid)
    if roll >= float(c["self_quote_pct"]):
        return QuoteDecision(False, REASON_NONE, rule="self_roll_miss", roll=roll,
                             quotes_last_hour=qlh)
    return QuoteDecision(
        True, REASON_SELF, reply_to=_reply_to_of({"id": pmid, "text": text}, from_me=True),
        rule="self_supplement", roll=roll, quotes_last_hour=qlh)


def decide_quote(
    messages: Sequence[Any],
    reply_text: str,
    *,
    cfg: Optional[Mapping[str, Any]] = None,
    now: Optional[float] = None,
    reply_alt: str = "",
    conv_key: str = "",
) -> QuoteDecision:
    """核心纯函数。``messages``＝会话最近消息（ts 升序，store 行 dict）。

    ``reply_text``＝实发文本（出站翻译后与入站 ``text`` 同语）；``reply_alt``＝
    翻译前原文（与入站 ``translated_text`` 同语）。每个候选取两路比对的最高分——
    跨语会话（客户英文 / 人设中文）也能算出相关性，单语会话两路等价。
    ``conv_key``＝会话 id，随机基线的会话级种子（空串也确定，只是所有会话同习惯）。

    返回的 ``reply_to`` 形态与坐席手动引用一致：``{id, from_me, participant, text, sender}``。
    """
    c = dict(DEFAULTS)
    if cfg:
        c.update({k: v for k, v in dict(cfg).items() if k in DEFAULTS})
    now_ts = float(now if now is not None else time.time())
    qlh = quotes_in_window(messages, now_ts)

    burst = unanswered_burst(messages)
    n = len(burst)
    if n == 0:
        return _forbid(_decide_self_supplement(messages, c, now_ts, conv_key, qlh),
                       c, messages, now_ts)

    cands = _candidates(burst, str(reply_text or ""), str(reply_alt or ""), c, now_ts)
    if not cands:
        return QuoteDecision(False, REASON_NONE, burst_len=n, rule="no_candidate",
                             quotes_last_hour=qlh)

    if n >= int(c["min_unanswered"]):
        # 必引 burst：最高分；同分取更早那条（更早的更可能被后面几条淹没）
        best = max(cands, key=lambda x: (x["score"], -x["index"]))
        if best["score"] < float(c["min_relevance"]):
            return QuoteDecision(False, REASON_NONE, score=best["score"], burst_len=n,
                                 candidates=cands, rule="low_relevance",
                                 quotes_last_hour=qlh)
        if not best["id"] and not best["text"]:
            return QuoteDecision(False, REASON_NONE, burst_len=n, candidates=cands,
                                 rule="no_ref", quotes_last_hour=qlh)
        dec = QuoteDecision(
            True, REASON_BURST, reply_to=_reply_to_of(best), score=best["score"],
            burst_len=n, target_index=int(best["index"]), candidates=cands,
            rule="burst_relevant", quotes_last_hour=qlh)
        return _forbid(dec, c, messages, now_ts)

    # 单条入站：目标就是它
    target = cands[0]
    if not target["id"] and not target["text"]:
        return QuoteDecision(False, REASON_NONE, burst_len=n, candidates=cands,
                             rule="no_ref", quotes_last_hour=qlh)
    age = (now_ts - float(target["ts"])) if target["ts"] else 0.0
    base = dict(score=target["score"], burst_len=n, target_index=int(target["index"]),
                candidates=cands, quotes_last_hour=qlh)
    if age > float(c["stale_sec"]):
        # 必引 stale：回的是半小时前的旧消息
        return _forbid(QuoteDecision(True, REASON_STALE, reply_to=_reply_to_of(target),
                                     rule="stale_30m", **base), c, messages, now_ts)
    reply_len = len(str(reply_text or "").strip())
    if age <= float(c["short_quick_sec"]) and reply_len <= int(c["short_answer_chars"]):
        # 禁引：2 min 内对单条的短答
        return QuoteDecision(False, REASON_NONE, rule="short_quick", **base)
    rate = session_rate(conv_key, c)
    roll = seeded_unit(conv_key, "roll", target["id"] or f"ts:{target['ts']}")
    if roll >= rate:
        return QuoteDecision(False, REASON_NONE, rule="roll_miss", roll=roll, rate=rate, **base)
    return _forbid(QuoteDecision(True, REASON_RANDOM, reply_to=_reply_to_of(target),
                                 rule="random_baseline", roll=roll, rate=rate, **base),
                   c, messages, now_ts)


def unsupported_decision(platform: str, *, orch_owns: bool = True) -> QuoteDecision:
    """能力位关 / RPA 回落路径：规则引擎直接 none，rule=unsupported（日志可见）。"""
    return QuoteDecision(False, REASON_NONE, rule="unsupported",
                         suppressed=("rpa_fallback" if not orch_owns else str(platform or "")))


def format_log(dec: QuoteDecision, *, conv: str = "", platform: str = "") -> str:
    """统一 ``[quote]`` 日志行（47KNBV 口径）：conv / reply_to_mid / reason / rule + 观测字段。"""
    rt = dec.reply_to if (dec.quote and dec.reply_to) else None
    parts = [
        "[quote]",
        f"conv={conv or '-'}",
        f"reply_to_mid={(rt or {}).get('id') or '-'}",
        f"reason={dec.reason or REASON_NONE}",
        f"rule={dec.rule or '-'}",
        f"platform={platform or '-'}",
        f"burst={dec.burst_len}",
    ]
    if dec.quote or dec.rule in ("low_relevance",):
        parts.append(f"score={dec.score:.2f}")
    if dec.roll is not None:
        parts.append(f"roll={dec.roll:.3f}")
    if dec.rate is not None:
        parts.append(f"rate={dec.rate:.3f}")
    if dec.suppressed and dec.rule != "unsupported":
        parts.append(f"suppressed={dec.suppressed}")
    if dec.quotes_last_hour:
        parts.append(f"hour_quotes={dec.quotes_last_hour}")
    if rt and rt.get("from_me"):
        parts.append("self=1")
    return " ".join(parts)


# ── 观测（进程级，重启清零；与 autosend 其它计数同口径）───────────────────

_LOCK = threading.Lock()
_STATS: Dict[str, Any] = {
    "decided": 0,          # decide 次数（含 unsupported）
    "quoted": 0,           # 决策=引用
    "quoted_by": {},       # 引用原因分布 burst/stale/random/self
    "skipped": {},         # 不引用原因分布（按 rule）
    "applied": 0,          # worker 回执 quote_applied=True
    "fallback_plain": 0,   # 带引用发送异常 → 去引用重发
}


def _gray(rec: Dict[str, Any]) -> None:
    """持久灰度账本（logs/i4_gray/quote_*.jsonl）：进程计数重启即清零，调
    min_relevance 要的是跨重启样本。只落决策字段，不落用户原文。"""
    try:
        from src.ops import region_quote_gray
        region_quote_gray.append("quote", rec)
    except Exception:  # noqa: BLE001
        pass


def record_decision(dec: QuoteDecision) -> None:
    with _LOCK:
        _STATS["decided"] += 1
        if dec.quote:
            _STATS["quoted"] += 1
            r = dec.reason or "unknown"
            _STATS["quoted_by"][r] = int(_STATS["quoted_by"].get(r, 0)) + 1
        else:
            k = dec.skip_key
            _STATS["skipped"][k] = int(_STATS["skipped"].get(k, 0)) + 1
    _gray({
        "quote": bool(dec.quote),
        # 账本里 reason 沿用 I-4 口径＝具体原因（summarize_quote 按 low_relevance 做直方图）
        "reason": (dec.reason if dec.quote else dec.skip_key),
        "why": dec.reason, "rule": dec.rule,
        "score": dec.score if dec.score is not None else None,
        "burst_len": dec.burst_len,
        "n_cand": len(dec.candidates or ()),
        "roll": dec.roll, "rate": dec.rate,
    })


def record_applied(result: Any) -> None:
    if isinstance(result, Mapping) and result.get("quote_applied"):
        with _LOCK:
            _STATS["applied"] += 1
        _gray({"applied": True})


def record_fallback_plain() -> None:
    with _LOCK:
        _STATS["fallback_plain"] += 1
    _gray({"fallback_plain": True})


def stats_snapshot() -> Dict[str, Any]:
    with _LOCK:
        return {
            "decided": _STATS["decided"],
            "quoted": _STATS["quoted"],
            "quoted_by": dict(_STATS["quoted_by"]),
            "skipped": dict(_STATS["skipped"]),
            "applied": _STATS["applied"],
            "fallback_plain": _STATS["fallback_plain"],
        }


def reset_stats() -> None:
    with _LOCK:
        _STATS["decided"] = 0
        _STATS["quoted"] = 0
        _STATS["quoted_by"] = {}
        _STATS["skipped"] = {}
        _STATS["applied"] = 0
        _STATS["fallback_plain"] = 0


__all__ = [
    "DEFAULTS", "QUOTE_CAPABLE_PLATFORMS", "QuoteDecision",
    "REASON_BURST", "REASON_STALE", "REASON_RANDOM", "REASON_SELF", "REASON_NONE",
    "parse_quote_cfg", "platform_quote_capable", "platform_allows_quote",
    "seeded_unit", "session_rate",
    "decide_quote", "unsupported_decision", "format_log",
    "unanswered_burst", "outbound_quote_flags", "quotes_in_window", "streak_blocked",
    "row_was_quoted", "relevance_score", "is_question",
    "record_decision", "record_applied", "record_fallback_plain",
    "stats_snapshot", "reset_stats",
]
